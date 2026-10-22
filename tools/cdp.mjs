import fs from "node:fs";

const DEBUG_PORT = process.env.MARS_CDP_PORT || "9222";
const DEBUG_HOST = process.env.MARS_CDP_HOST || "127.0.0.1";

async function listTargets() {
  const response = await fetch(`http://${DEBUG_HOST}:${DEBUG_PORT}/json/list`);
  if (!response.ok) {
    throw new Error(`CDP target list failed: ${response.status}`);
  }
  return response.json();
}

async function findPage() {
  const targets = await listTargets();
  const page = targets.find(
    (item) =>
      item.type === "page" &&
      (item.url.includes("mars.mycourse.cn/deimos") ||
        item.title.includes("安全知识竞赛")),
  );
  if (!page) {
    throw new Error("Mars Deimos page target not found");
  }
  return page;
}

class CDP {
  constructor(url) {
    this.url = url;
    this.nextId = 1;
    this.pending = new Map();
  }

  async open() {
    this.socket = new WebSocket(this.url);
    await new Promise((resolve, reject) => {
      this.socket.addEventListener("open", resolve, { once: true });
      this.socket.addEventListener("error", reject, { once: true });
    });
    this.socket.addEventListener("message", (event) => {
      const message = JSON.parse(String(event.data));
      if (!message.id) {
        return;
      }
      const waiter = this.pending.get(message.id);
      if (!waiter) {
        return;
      }
      this.pending.delete(message.id);
      if (message.error) {
        waiter.reject(new Error(JSON.stringify(message.error)));
      } else {
        waiter.resolve(message.result);
      }
    });
  }

  send(method, params = {}) {
    const id = this.nextId++;
    this.socket.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
  }

  close() {
    this.socket.close();
  }
}

function printValue(value) {
  if (typeof value === "string") {
    console.log(value);
  } else {
    console.log(JSON.stringify(value, null, 2));
  }
}

async function evaluate(cdp, expression) {
  const result = await cdp.send("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
    userGesture: true,
  });
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.text || "Runtime.evaluate failed");
  }
  return result.result?.value;
}

async function main() {
  const [command = "dom", ...args] = process.argv.slice(2);
  const page = await findPage();
  const cdp = new CDP(page.webSocketDebuggerUrl);
  await cdp.open();
  try {
    if (command === "dom") {
      printValue(await evaluate(cdp, "document.body.innerText"));
    } else if (command === "url") {
      printValue(await evaluate(cdp, "location.href"));
    } else if (command === "eval") {
      printValue(await evaluate(cdp, args.join(" ")));
    } else if (command === "click-text") {
      const wanted = args.join(" ");
      printValue(
        await evaluate(
          cdp,
          `(() => {
            const wanted = ${JSON.stringify(wanted)};
            const nodes = [...document.querySelectorAll("button,a,div,span")];
            const node = nodes.find((item) => (item.innerText || "").trim() === wanted);
            if (!node) return false;
            node.click();
            return true;
          })()`,
        ),
      );
    } else if (command === "screenshot") {
      const output = args[0] || "page.png";
      const result = await cdp.send("Page.captureScreenshot", { format: "png" });
      fs.writeFileSync(output, Buffer.from(result.data, "base64"));
      console.log(output);
    } else if (command === "export-session") {
      const output = args[0];
      if (!output) {
        throw new Error("export-session requires an output path");
      }
      const info = await evaluate(
        cdp,
        `(() => {
          const stored = JSON.parse(sessionStorage.getItem("zddClient") || "{}");
          return stored.loginInfo?.info || {};
        })()`,
      );
      if (!info?.token) {
        throw new Error("logged-in token not found in sessionStorage");
      }
      const user = { ...info };
      delete user.token;
      const session = {
        base_url: "https://mars.mycourse.cn/marsapi",
        tenant: info.tenantCode || "",
        username: info.userName || "",
        token: info.token,
        user,
        cookies: {},
      };
      fs.writeFileSync(output, JSON.stringify(session, null, 2), { mode: 0o600 });
      console.log(output);
    } else {
      throw new Error(`unknown command: ${command}`);
    }
  } finally {
    cdp.close();
  }
}

main().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
