from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import re
import shutil
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

from .api import (
    ApiClient,
    ApiError,
    AuthenticationError,
    load_session,
    save_session,
)
from .bank import QuestionBank
from .config import load_config
from .guard import AuditChain, GuardError, GuardPolicy, IntegrityGuard
from .workflow import MarsWorkflow
from .xlsx import import_xlsx


_CAPTCHA_ARTIFACT_RE = re.compile(r"captcha-\d+-\d+\.(?:png|html)")


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _apply_overrides(config: dict[str, Any], args: argparse.Namespace) -> None:
    for key in ("base_url", "tenant", "username", "password", "bank_path"):
        value = getattr(args, key, None)
        if value not in {None, ""}:
            config[key] = value
    if getattr(args, "bank", None):
        bank_path = Path(args.bank).expanduser()
        if not bank_path.is_absolute():
            bank_path = Path.cwd() / bank_path
        config["bank_path"] = str(bank_path.resolve())


def _load_bank(config: dict[str, Any]) -> QuestionBank:
    return QuestionBank.from_files(
        [config["bank_path"], config.get("learned_path") or ""]
    )


def _write_captcha_artifacts(
    state_dir: Path,
    timestamp: int,
    attempt: int,
    image: bytes,
) -> tuple[Path, Path]:
    stem = f"captcha-{timestamp}-{attempt}"
    image_path = state_dir / f"{stem}.png"
    viewer_path = state_dir / f"{stem}.html"
    image_path.write_bytes(image)
    encoded = base64.b64encode(image).decode("ascii")
    viewer_path.write_text(
        (
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<title>本次验证码</title></head><body "
            "style=\"margin:0;background:#fff;font-family:sans-serif;text-align:center\">"
            "<h2>本次登录验证码</h2>"
            f"<img alt=\"captcha\" style=\"width:600px;max-width:95vw;image-rendering:pixelated\" "
            f"src=\"data:image/png;base64,{encoded}\">"
            "<p>此页面只显示本地保存的验证码，不会重新请求服务器。</p>"
            "</body></html>"
        ),
        encoding="utf-8",
    )
    return image_path, viewer_path


def _cleanup_captcha_artifacts(state_dir: Path) -> int:
    if not state_dir.exists():
        return 0
    removed = 0
    for path in state_dir.iterdir():
        if path.is_file() and _CAPTCHA_ARTIFACT_RE.fullmatch(path.name):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def _build_guard(config: dict[str, Any]) -> IntegrityGuard:
    return IntegrityGuard(GuardPolicy.from_config(config))


def _resolve_answer_mode(
    config: dict[str, Any],
    args: argparse.Namespace,
    activity_type: Any,
) -> str:
    try:
        if int(activity_type) != 4:
            return "perfect"
    except (TypeError, ValueError):
        return "perfect"
    mode = str(
        getattr(args, "answer_mode", None)
        or config.get("formal_exam_mode")
        or "ask"
    ).strip().lower()
    if mode != "ask":
        if mode not in {"perfect", "human"}:
            raise ValueError(f"unsupported formal exam mode: {mode}")
        return mode
    if getattr(args, "non_interactive", False):
        raise ValueError("--answer-mode is required in non-interactive mode")
    wrong_min = max(0, int(config.get("deliberate_wrong_min") or 0))
    wrong_max = max(wrong_min, int(config.get("deliberate_wrong_max") or 0))
    print("请选择正式考试作答策略：")
    print("[0] 全对（默认）")
    wrong_label = str(wrong_min) if wrong_min == wrong_max else f"{wrong_min}～{wrong_max}"
    print(f"[1] 拟人模式：随机故意错 {wrong_label} 题")
    while True:
        selected = input("策略序号（直接回车选择全对）: ").strip().lower()
        if selected in {"", "0", "全对", "perfect"}:
            return "perfect"
        if selected in {"1", "拟人", "human"}:
            return "human"
        print("请输入 0 或 1")


def _resolve_tenant(tenants: list[dict[str, Any]], selector: str) -> dict[str, Any]:
    if not tenants:
        raise ApiError("tenant list is empty")
    normalized = selector.strip()
    exact = [
        item
        for item in tenants
        if str(item.get("code")) == normalized or str(item.get("name")) == normalized
    ]
    if len(exact) == 1:
        return exact[0]
    contains = [
        item
        for item in tenants
        if normalized.casefold() in str(item.get("name") or "").casefold()
        or normalized in str(item.get("code") or "")
    ]
    if len(contains) == 1:
        return contains[0]
    if not contains:
        raise ValueError(f"school not found: {selector}")
    print("匹配到多所学校：")
    for index, item in enumerate(contains[:100]):
        print(f"[{index}] {item.get('code')}  {item.get('name')}")
    if len(contains) > 100:
        print(f"... 其余 {len(contains) - 100} 所省略")
    raw = input("请输入序号: ").strip()
    try:
        return contains[int(raw)]
    except (ValueError, IndexError) as exc:
        raise ValueError("invalid school selection") from exc


def _session_path(config: dict[str, Any]) -> Path:
    return Path(str(config["state_dir"])) / "session.json"


def _new_client(config: dict[str, Any], session: dict[str, Any] | None = None) -> ApiClient:
    session = session or {}
    client = ApiClient(
        base_url=str(config.get("base_url") or session.get("base_url") or ""),
        token=str(session.get("token") or ""),
        timeout=float(config.get("request_timeout") or 30),
    )
    client.load_cookies(session.get("cookies") if isinstance(session.get("cookies"), dict) else None)
    return client


def _should_auto_logout(
    config: dict[str, Any],
    activity_type: Any,
    submitted: bool,
) -> bool:
    if not submitted or not bool(config.get("logout_after_exam", True)):
        return False
    try:
        return int(activity_type) in {3, 4}
    except (TypeError, ValueError):
        return False


def _logout_and_archive_session(
    config: dict[str, Any],
    client: ApiClient | None = None,
) -> bool:
    session_path = _session_path(config)
    session = load_session(session_path)
    if client is None:
        if not session:
            print("当前没有会话")
            return False
        client = _new_client(config, session)
    try:
        client.logout()
        print("已退出平台")
    except ApiError as exc:
        print(f"平台退出未完成: {exc}")
    if session_path.exists():
        archive = session_path.with_name(f"session.logged-out-{int(time.time())}.json")
        session_path.replace(archive)
        try:
            archive.chmod(0o600)
        except OSError:
            pass
        print(f"本地会话已归档: {archive}")
    return True


def _interactive_login(config: dict[str, Any], args: argparse.Namespace) -> ApiClient:
    client = _new_client(config)
    tenants = client.list_tenants()
    state_dir = Path(str(config["state_dir"]))
    state_dir.mkdir(parents=True, exist_ok=True)
    cleanup_captcha = bool(config.get("cleanup_captcha_files", True))
    if cleanup_captcha:
        removed = _cleanup_captcha_artifacts(state_dir)
        if removed:
            print(f"已清理旧验证码文件: {removed} 个")
    (state_dir / "tenants.json").write_text(
        json.dumps(tenants, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    selector = str(config.get("tenant") or "").strip()
    if not selector:
        if getattr(args, "non_interactive", False):
            raise ValueError("--tenant is required in non-interactive mode")
        selector = input(f"学校名称/代码（共 {len(tenants)} 所）: ").strip()
    tenant = _resolve_tenant(tenants, selector)
    tenant_code = str(tenant.get("code"))
    username = str(config.get("username") or os.environ.get("MARS_USERNAME") or "").strip()
    if not username:
        if getattr(args, "non_interactive", False):
            raise ValueError("--username or MARS_USERNAME is required")
        username = input("账号: ").strip()
    password = str(config.get("password") or os.environ.get("MARS_PASSWORD") or "")
    if not password:
        if getattr(args, "non_interactive", False):
            raise ValueError("--password or MARS_PASSWORD is required")
        show_password = bool(config.get("show_password_input", True))
        if getattr(args, "hide_password", False):
            show_password = False
        password = input("密码: ") if show_password else getpass.getpass("密码: ")
    encrypted = hashlib.md5(password.encode("utf-8")).hexdigest().upper()

    result: dict[str, Any] = {}
    for attempt in range(1, 4):
        timestamp, captcha = client.get_captcha()
        captcha_path, viewer_path = _write_captcha_artifacts(
            state_dir,
            timestamp,
            attempt,
            captcha,
        )
        try:
            print(f"本次验证码图片: {captcha_path}")
            print(f"本地大图页面: {viewer_path}")
            print("大图已内嵌 Base64，不会重新请求验证码接口。")
            if bool(config.get("open_captcha", False)):
                try:
                    webbrowser.open(viewer_path.resolve().as_uri())
                except Exception:
                    pass
            code = str(getattr(args, "captcha_code", "") or "").strip()
            if not code:
                if getattr(args, "non_interactive", False):
                    raise ValueError("--captcha-code is required in non-interactive mode")
                code = input("验证码: ").strip()
            try:
                response = client.login(tenant_code, username, encrypted, code, timestamp)
                result = response if isinstance(response, dict) else {"result": response}
                if not client.token:
                    token = result.get("token")
                    if token:
                        client.set_token(str(token))
                if not client.token:
                    raise ApiError(f"login response contains no token: {result}")
                break
            except (ApiError, AuthenticationError) as exc:
                if attempt >= 3:
                    raise
                print(f"登录未完成: {exc}; 旧验证码已失效，将生成新的唯一文件")
        finally:
            if cleanup_captcha:
                for path in (captcha_path, viewer_path):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass

    user = result.get("user")
    if not isinstance(user, dict):
        user = client.user_index()
    save_session(
        _session_path(config),
        client,
        tenant=tenant_code,
        username=username,
        user=user,
    )
    try:
        _session_path(config).chmod(0o600)
    except OSError:
        pass
    print(f"登录成功: {user.get('userName') or user.get('realName') or username} / {tenant.get('name')}")
    if result.get("passwordChanged") == 2:
        print(
            "平台标记了初始密码待修改；本项目已自动跳过修改密码，"
            "网页端若弹出窗口选择“暂不修改”即可，后续流程可直接继续。"
        )
    return client


def _authenticated_client(
    config: dict[str, Any],
    args: argparse.Namespace,
    *,
    force_login: bool = False,
) -> ApiClient:
    session_path = _session_path(config)
    session = {} if force_login else load_session(session_path)
    if session and session.get("base_url") != config.get("base_url"):
        session = {}
    client = _new_client(config, session)
    if client.token:
        try:
            client.user_index()
            return client
        except AuthenticationError:
            pass
    return _interactive_login(config, args)


def cmd_probe(config: dict[str, Any], args: argparse.Namespace) -> int:
    client = _new_client(config)
    tenants = client.list_tenants()
    timestamp, captcha = client.get_captcha()
    output = Path(str(config["state_dir"])) / "probe-captcha.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(captcha)
    unauth_status = None
    try:
        client.user_index()
    except AuthenticationError as exc:
        unauth_status = exc.status
    print(
        json.dumps(
            {
                "base_url": config["base_url"],
                "tenant_count": len(tenants),
                "tenants_with_password_prompt": sum(bool(item.get("passwordPrompt")) for item in tenants),
                "captcha_timestamp": timestamp,
                "captcha_bytes": len(captcha),
                "captcha_path": str(output),
                "unauth_user_index_status": unauth_status,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_login(config: dict[str, Any], args: argparse.Namespace) -> int:
    _interactive_login(config, args)
    return 0


def cmd_list(config: dict[str, Any], args: argparse.Namespace) -> int:
    client = _authenticated_client(config, args)
    workflow = MarsWorkflow(client, _load_bank(config), config)
    snapshot = workflow.home_snapshot()
    user = snapshot["user"]
    project = snapshot["project"]
    print(
        f"用户: {user.get('userName') or user.get('realName') or '-'} / "
        f"{user.get('tenantName') or '-'}"
    )
    print(f"项目: {project.get('projectId')} {project.get('projectName') or ''}")
    activities = snapshot["activities"]
    if not activities:
        print("没有返回活动")
    for index, activity in enumerate(activities):
        print(
            f"[{index}] type={activity.get('activityType')} "
            f"id={activity.get('userActivityId')} {activity.get('name') or ''}"
        )
    if getattr(args, "json", False):
        _print_json(snapshot)
    return 0


def cmd_run(config: dict[str, Any], args: argparse.Namespace) -> int:
    client = _authenticated_client(config, args)
    bank = _load_bank(config)
    if not len(bank):
        raise ValueError(f"answer bank is empty: {config['bank_path']}")
    workflow = MarsWorkflow(client, bank, config)
    snapshot = workflow.home_snapshot()
    activity = workflow.choose_activity(
        snapshot["activities"],
        index=int(args.activity_index),
        user_activity_id=args.user_activity_id,
        activity_type=args.activity_type,
    )
    guard = _build_guard(config)
    workflow.guard = guard
    answer_mode = (
        "perfect"
        if args.dry_run
        else _resolve_answer_mode(config, args, activity.get("activityType"))
    )
    with guard.run_lock():
        guard.ensure_session(client.token, snapshot["user"])
        report = workflow.execute(
            activity,
            dry_run=bool(args.dry_run),
            collect_wrong=False if args.no_collect_wrong else None,
            answer_mode=answer_mode,
        )
    if bool(config.get("save_run_log", False)):
        log_path = report.save(config["state_dir"])
        print(f"运行记录: {log_path}")
    if report.missing:
        print(f"题库未命中 {report.missing} 题，已按首选项提交")
    if args.logout or _should_auto_logout(
        config,
        report.activity_type,
        report.submitted,
    ):
        if not args.logout:
            print("考试已结束，正在自动退出登录")
        _logout_and_archive_session(config, client)
    return 0


def _runnable_auto_activities(activities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for activity in activities:
        try:
            activity_type = int(activity.get("activityType"))
        except (TypeError, ValueError):
            continue
        if activity_type not in {1, 2, 3, 4}:
            continue
        result.append(activity)
    return result


def cmd_auto(config: dict[str, Any], args: argparse.Namespace) -> int:
    client = _authenticated_client(config, args)
    bank = _load_bank(config)
    workflow = MarsWorkflow(client, bank, config)
    snapshot = workflow.home_snapshot()
    activities = _runnable_auto_activities(snapshot["activities"])
    if not activities:
        raise ValueError("没有可运行的练习或考试活动")
    user = snapshot["user"]
    print(
        f"当前账号: {user.get('userName') or '-'} / "
        f"{user.get('tenantName') or '-'}"
    )
    for index, activity in enumerate(activities):
        print(
            f"[{index}] type={activity.get('activityType')} "
            f"{activity.get('name') or ''}"
        )
    default_index = next(
        (
            index
            for index, activity in enumerate(activities)
            if int(activity.get("activityType")) == 3
        ),
        0,
    )
    selected_index = args.activity_index
    if selected_index is None:
        raw = input(f"请选择活动序号（直接回车选择默认活动 {default_index}）: ").strip()
        selected_index = int(raw) if raw else default_index
    if selected_index < 0 or selected_index >= len(activities):
        raise ValueError(f"活动序号超出范围: {selected_index}")
    activity = activities[selected_index]
    if int(activity.get("activityType")) not in {1, 2, 3, 4}:
        raise ValueError("不支持的活动类型")
    guard = _build_guard(config)
    workflow.guard = guard
    answer_mode = (
        "perfect"
        if args.dry_run
        else _resolve_answer_mode(config, args, activity.get("activityType"))
    )
    with guard.run_lock():
        guard.ensure_session(client.token, user)
        report = workflow.execute(
            activity,
            dry_run=bool(args.dry_run),
            collect_wrong=False if args.no_collect_wrong else None,
            answer_mode=answer_mode,
        )
    if bool(config.get("save_run_log", False)):
        log_path = report.save(config["state_dir"])
        print(f"运行记录: {log_path}")
    if report.missing:
        print(f"题库未命中 {report.missing} 题")
    if guard.audit is not None:
        valid, message = AuditChain.verify(config["audit_path"])
        print(f"审计链: {'通过' if valid else '失败'} ({message})")
    if _should_auto_logout(config, report.activity_type, report.submitted):
        print("考试已结束，正在自动退出登录")
        _logout_and_archive_session(config, client)
    return 0


def cmd_wrong(config: dict[str, Any], args: argparse.Namespace) -> int:
    client = _authenticated_client(config, args)
    bank = _load_bank(config)
    workflow = MarsWorkflow(client, bank, config)
    snapshot = workflow.home_snapshot()
    activity = workflow.choose_activity(
        snapshot["activities"],
        index=int(args.activity_index),
        user_activity_id=args.user_activity_id,
        activity_type=args.activity_type,
    )
    guard = _build_guard(config)
    with guard.run_lock():
        guard.ensure_session(client.token, snapshot["user"])
        guard.emit(
            "wrong_collection_started",
            {"activity_type": int(activity["activityType"])},
        )
        count = workflow.collect_wrong(int(activity["activityType"]), activity["userActivityId"])
        guard.emit("wrong_collection_completed", {"learned": count})
    print(f"新增错题答案 {count} 条 -> {config['learned_path']}")
    return 0


def cmd_import_bank(config: dict[str, Any], args: argparse.Namespace) -> int:
    questions = import_xlsx(args.xlsx)
    output = Path(args.output or config["bank_path"])
    if output.exists():
        backup = output.with_name(f"{output.name}.bak-{int(time.time())}")
        shutil.copy2(output, backup)
        print(f"原题库已备份: {backup}")
    count = QuestionBank.save_jsonl(questions, output)
    bank = QuestionBank(questions)
    print(f"已导入 {count} 题 -> {output}")
    _print_json(bank.stats())
    return 0


def cmd_bank_stats(config: dict[str, Any], args: argparse.Namespace) -> int:
    bank = _load_bank(config)
    _print_json(bank.stats())
    return 0


def cmd_logout(config: dict[str, Any], args: argparse.Namespace) -> int:
    _logout_and_archive_session(config)
    return 0


def cmd_audit_verify(config: dict[str, Any], args: argparse.Namespace) -> int:
    path = args.audit or config["audit_path"]
    valid, message = AuditChain.verify(path)
    print(json.dumps({"path": path, "valid": valid, "message": message}, ensure_ascii=False, indent=2))
    return 0 if valid else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mars Deimos answer-bank workflow client",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default="config.json", help="JSON config path")
    common.add_argument("--base-url")
    common.add_argument("--tenant")
    common.add_argument("--username")
    common.add_argument("--password")
    common.add_argument("--hide-password", action="store_true")
    common.add_argument("--bank")
    common.add_argument("--non-interactive", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    probe = subparsers.add_parser("probe", parents=[common], help="check public endpoints")
    probe.set_defaults(func=cmd_probe)

    login = subparsers.add_parser("login", parents=[common], help="interactive captcha login")
    login.add_argument("--captcha-code", default="")
    login.set_defaults(func=cmd_login)

    list_parser = subparsers.add_parser("list", parents=[common], help="list activities")
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(func=cmd_list)

    run = subparsers.add_parser("run", parents=[common], help="answer and submit a paper")
    run.add_argument("--activity-index", type=int, default=0)
    run.add_argument("--user-activity-id", default="")
    run.add_argument("--activity-type", type=int)
    run.add_argument("--answer-mode", choices=("perfect", "human"))
    run.add_argument("--dry-run", action="store_true", help="prepare only; do not start a paper")
    run.add_argument("--no-collect-wrong", action="store_true")
    run.add_argument("--logout", action="store_true")
    run.add_argument("--captcha-code", default="")
    run.set_defaults(func=cmd_run)

    auto = subparsers.add_parser("auto", parents=[common], help="one-command practice or exam")
    auto.add_argument("--activity-index", type=int)
    auto.add_argument("--answer-mode", choices=("perfect", "human"))
    auto.add_argument("--dry-run", action="store_true")
    auto.add_argument("--no-collect-wrong", action="store_true")
    auto.add_argument("--captcha-code", default="")
    auto.set_defaults(func=cmd_auto)

    wrong = subparsers.add_parser("wrong", parents=[common], help="learn correct answers")
    wrong.add_argument("--activity-index", type=int, default=0)
    wrong.add_argument("--user-activity-id", default="")
    wrong.add_argument("--activity-type", type=int)
    wrong.add_argument("--captcha-code", default="")
    wrong.set_defaults(func=cmd_wrong)

    importer = subparsers.add_parser("import-bank", parents=[common], help="import an XLSX bank")
    importer.add_argument("xlsx")
    importer.add_argument("--output")
    importer.set_defaults(func=cmd_import_bank)

    stats = subparsers.add_parser("bank-stats", parents=[common], help="show bank totals")
    stats.set_defaults(func=cmd_bank_stats)

    logout = subparsers.add_parser("logout", parents=[common], help="logout and archive session")
    logout.set_defaults(func=cmd_logout)

    audit = subparsers.add_parser("audit-verify", parents=[common], help="verify the audit hash chain")
    audit.add_argument("--audit")
    audit.set_defaults(func=cmd_audit_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config, _ = load_config(args.config)
        _apply_overrides(config, args)
        return int(args.func(config, args) or 0)
    except (
        ApiError,
        AuthenticationError,
        EOFError,
        GuardError,
        ValueError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        print(f"执行失败: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
