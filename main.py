from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _venv_python() -> Path:
    if os.name == "nt":
        return ROOT / ".venv" / "Scripts" / "python.exe"
    return ROOT / ".venv" / "bin" / "python"


def _same_executable(left: str | Path, right: str | Path) -> bool:
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return False


def _bootstrap() -> None:
    target = _venv_python()
    if not target.exists():
        print("首次运行：正在创建虚拟环境...")
        subprocess.check_call([sys.executable, "-m", "venv", str(ROOT / ".venv")])
        subprocess.check_call(
            [str(target), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")]
        )
    if not _same_executable(sys.executable, target):
        os.execv(str(target), [str(target), str(Path(__file__).resolve()), *sys.argv[1:]])
    if importlib.util.find_spec("requests") is None:
        print("正在安装运行依赖...")
        subprocess.check_call(
            [str(target), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")]
        )
        os.execv(str(target), [str(target), str(Path(__file__).resolve()), *sys.argv[1:]])


_bootstrap()

from mars_deimos.cli import main  # noqa: E402


if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.append("auto")
    raise SystemExit(main())
