from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULTS: dict[str, Any] = {
    "base_url": "https://mars.mycourse.cn/marsapi",
    "tenant": "",
    "username": "",
    "password": "",
    "bank_path": "data/questions.jsonl",
    "learned_path": "data/learned.jsonl",
    "state_dir": ".state",
    "request_timeout": 30,
    "question_delay_seconds": 0.0,
    "question_delay_min_seconds": 0.0,
    "question_delay_max_seconds": 0.25,
    "exam_question_delay_min_seconds": 5.0,
    "exam_question_delay_max_seconds": 7.0,
    "formal_exam_mode": "ask",
    "deliberate_wrong_min": 10,
    "deliberate_wrong_max": 10,
    "audit_submitted_answers": True,
    "guard_enabled": True,
    "guard_strict": True,
    "guard_require_token": True,
    "guard_reject_server_correct": True,
    "guard_abort_on_bank_miss": False,
    "guard_lock_timeout_seconds": 1.0,
    "max_bank_miss_ratio": 0.05,
    "exam_min_seconds": 613,
    "exam_min_seconds_per_question": 0.0,
    "submit_retry_seconds": 10,
    "submit_retry_count": 3,
    "max_pages": 50,
    "allow_server_correct": False,
    "collect_wrong_answers": True,
    "logout_after_exam": True,
    "open_captcha": False,
    "cleanup_captcha_files": True,
    "show_password_input": True,
    "verbose_questions": False,
    "save_run_log": False,
    "save_audit_log": False,
    "audit_path": ".state/audit.jsonl",
    "lock_path": ".state/run.lock",
}


def load_config(path: str | Path) -> tuple[dict[str, Any], Path]:
    config_path = Path(path).expanduser()
    config = dict(DEFAULTS)
    if config_path.exists():
        parsed = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("config root must be a JSON object")
        config.update(parsed)
    base = config_path.parent if str(config_path.parent) not in {"", "."} else Path.cwd()
    for key in ("bank_path", "learned_path", "state_dir", "audit_path", "lock_path"):
        value = Path(str(config[key])).expanduser()
        if not value.is_absolute():
            value = base / value
        config[key] = str(value.resolve())
    return config, config_path
