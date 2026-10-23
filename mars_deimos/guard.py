from __future__ import annotations

import hashlib
import json
import os
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from .bank import AnswerPlan, normalize_text


class GuardError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class GuardPolicy:
    enabled: bool = True
    strict: bool = True
    require_token: bool = True
    reject_server_correct: bool = True
    abort_on_bank_miss: bool = False
    max_bank_miss_ratio: float = 0.05
    min_question_seconds: float = 0.0
    max_question_seconds: float = 0.25
    exam_min_question_seconds: float = 5.0
    exam_max_question_seconds: float = 7.0
    exam_min_seconds_per_question: float = 0.0
    explicit_exam_min_seconds: float = 613.0
    lock_timeout_seconds: float = 1.0
    audit_enabled: bool = True
    audit_path: str = ".state/audit.jsonl"
    lock_path: str = ".state/run.lock"

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "GuardPolicy":
        min_delay = float(config.get("question_delay_min_seconds", 0))
        max_delay = float(config.get("question_delay_max_seconds", max(min_delay, 0.25)))
        exam_min_delay = float(config.get("exam_question_delay_min_seconds", 5.0))
        exam_max_delay = float(
            config.get("exam_question_delay_max_seconds", max(exam_min_delay, 7.0))
        )
        if min_delay < 0 or max_delay < min_delay:
            raise ValueError("invalid question delay range")
        if exam_min_delay < 0 or exam_max_delay < exam_min_delay:
            raise ValueError("invalid exam question delay range")
        return cls(
            enabled=bool(config.get("guard_enabled", True)),
            strict=bool(config.get("guard_strict", True)),
            require_token=bool(config.get("guard_require_token", True)),
            reject_server_correct=bool(config.get("guard_reject_server_correct", True)),
            abort_on_bank_miss=bool(config.get("guard_abort_on_bank_miss", False)),
            max_bank_miss_ratio=float(config.get("max_bank_miss_ratio", 0.05)),
            min_question_seconds=min_delay,
            max_question_seconds=max_delay,
            exam_min_question_seconds=exam_min_delay,
            exam_max_question_seconds=exam_max_delay,
            exam_min_seconds_per_question=float(
                config.get("exam_min_seconds_per_question", 0.0)
            ),
            explicit_exam_min_seconds=float(config.get("exam_min_seconds", 613) or 0),
            lock_timeout_seconds=float(config.get("guard_lock_timeout_seconds", 1.0)),
            audit_enabled=bool(config.get("save_audit_log", False)),
            audit_path=str(config.get("audit_path") or ".state/audit.jsonl"),
            lock_path=str(config.get("lock_path") or ".state/run.lock"),
        )


class AuditChain:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.sequence = 0
        self.previous_hash = "0" * 64
        if self.path.exists():
            self._load_tail()

    def _load_tail(self) -> None:
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self.sequence = max(self.sequence, int(record.get("sequence") or 0))
                self.previous_hash = str(record.get("hash") or self.previous_hash)

    def append(self, event: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.sequence += 1
        body = {
            "sequence": self.sequence,
            "timestamp": round(time.time(), 6),
            "event": event,
            "data": data or {},
            "previous_hash": self.previous_hash,
        }
        encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        record = {**body, "hash": digest}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        self.previous_hash = digest
        return record

    @staticmethod
    def verify(path: str | Path) -> tuple[bool, str]:
        source = Path(path)
        if not source.exists():
            return False, "audit file does not exist"
        previous = "0" * 64
        expected_sequence = 1
        with source.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    return False, f"line {line_number}: invalid JSON"
                stored_hash = record.pop("hash", None)
                if int(record.get("sequence") or 0) != expected_sequence:
                    return False, f"line {line_number}: sequence mismatch"
                if record.get("previous_hash") != previous:
                    return False, f"line {line_number}: previous hash mismatch"
                encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                calculated = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                if stored_hash != calculated:
                    return False, f"line {line_number}: hash mismatch"
                previous = stored_hash
                expected_sequence += 1
        return True, f"verified {expected_sequence - 1} records"


class IntegrityGuard:
    def __init__(
        self,
        policy: GuardPolicy,
        *,
        random_source: random.Random | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.policy = policy
        self.random = random_source or random.Random()
        self.clock = clock
        self.audit = (
            AuditChain(policy.audit_path)
            if policy.enabled and policy.audit_enabled
            else None
        )

    def next_question_delay(self, activity_type: Any = None) -> float:
        if not self.policy.enabled:
            return 0.0
        try:
            is_exam = int(activity_type) in {3, 4}
        except (TypeError, ValueError):
            is_exam = False
        if is_exam:
            return self.random.uniform(
                self.policy.exam_min_question_seconds,
                self.policy.exam_max_question_seconds,
            )
        return self.random.uniform(
            self.policy.min_question_seconds,
            self.policy.max_question_seconds,
        )

    def ensure_session(self, token: str, user: dict[str, Any]) -> None:
        if not self.policy.enabled:
            return
        if self.policy.require_token and len(str(token or "")) < 20:
            raise GuardError("session-token", "missing or invalid authentication token")
        if not user or not (user.get("userName") or user.get("tenantCode")):
            raise GuardError("session-user", "authenticated user identity is missing")
        self.emit("session_validated", {"user": bool(user), "token_length": len(str(token))})

    def validate_activity(self, activity: dict[str, Any]) -> None:
        if not self.policy.enabled:
            return
        try:
            activity_type = int(activity.get("activityType"))
        except (TypeError, ValueError) as exc:
            raise GuardError("activity-type", "invalid activity type") from exc
        if activity_type not in {1, 2, 3, 4}:
            raise GuardError("activity-type", f"unsupported activity type: {activity_type}")
        if activity.get("userActivityId") in {"", None}:
            raise GuardError("activity-id", "missing userActivityId")
        self.emit("activity_validated", {"activity_type": activity_type})

    def validate_prepare(self, activity_type: int, prepared: Any) -> None:
        if not self.policy.enabled:
            return
        if not isinstance(prepared, dict):
            raise GuardError("prepare-shape", "preparePaper response is not an object")
        question_num = int(prepared.get("questionNum") or 0)
        if question_num <= 0:
            raise GuardError("paper-size", "questionNum must be positive")
        answer_time = int(prepared.get("answerTime") or 0)
        if activity_type in {3, 4} and answer_time <= 0:
            raise GuardError("paper-time", "exam answerTime must be positive")
        paper_score = float(prepared.get("paperScore") or 100)
        pass_score = float(prepared.get("passScore") or 0)
        if pass_score < 0 or pass_score > paper_score:
            raise GuardError("paper-score", "passScore is outside the paper score range")
        total_times = int(prepared.get("totalTimes") or 0)
        done_times = int(prepared.get("doneTimes") or 0)
        if total_times > 0 and done_times >= total_times:
            raise GuardError("attempt-limit", "no remaining exam attempts")
        self.emit(
            "paper_prepared",
            {
                "activity_type": activity_type,
                "question_num": question_num,
                "answer_time": answer_time,
                "remaining_attempts": total_times - done_times if total_times > 0 else -1,
            },
        )

    def validate_start(self, paper: Any) -> None:
        if not self.policy.enabled:
            return
        if not isinstance(paper, dict):
            raise GuardError("start-shape", "startPaper response is not an object")
        questions = paper.get("questionList")
        if not isinstance(questions, list) or not questions:
            raise GuardError("paper-empty", "startPaper returned no questions")
        self.emit(
            "paper_started",
            {
                "batch_size": len(questions),
                "current_index": paper.get("currentIndex"),
                "has_next": paper.get("hasNext"),
            },
        )

    def validate_question(self, question: dict[str, Any], seen: set[str]) -> str:
        if not self.policy.enabled:
            return str(question.get("id") or "")
        question_id = question.get("id", question.get("questionId"))
        if question_id in {"", None}:
            raise GuardError("question-id", "question has no id")
        question_key = str(question_id)
        if question_key in seen:
            raise GuardError("question-duplicate", f"duplicate question id: {question_key}")
        try:
            qtype = int(question.get("type", question.get("questionType")))
        except (TypeError, ValueError) as exc:
            raise GuardError("question-type", f"invalid question type for {question_key}") from exc
        if qtype not in {1, 2, 4}:
            raise GuardError("question-type", f"unsupported question type: {qtype}")
        options = question.get("optionList")
        if not isinstance(options, list) or len(options) < 2:
            raise GuardError("question-options", f"question {question_key} has too few options")
        option_ids = [str(item.get("id")) for item in options if isinstance(item, dict)]
        if len(option_ids) != len(options) or len(set(option_ids)) != len(option_ids):
            raise GuardError("question-options", f"question {question_key} has invalid option ids")
        contents = [
            normalize_text(item.get("content"))
            for item in options
            if isinstance(item, dict)
        ]
        if any(not text for text in contents):
            raise GuardError("question-options", f"question {question_key} has invalid option text")
        return question_key

    def validate_answer(
        self,
        question: dict[str, Any],
        plan: AnswerPlan,
    ) -> None:
        if not self.policy.enabled:
            return
        if not plan.answer_ids:
            raise GuardError("answer-empty", "answer plan has no option ids")
        option_ids = {str(item.get("id")) for item in question.get("optionList", []) if isinstance(item, dict)}
        if not {str(value) for value in plan.answer_ids}.issubset(option_ids):
            raise GuardError("answer-option", "answer contains an option outside the question")
        qtype = int(question.get("type", question.get("questionType")))
        if qtype in {1, 4} and len(plan.answer_ids) != 1:
            raise GuardError("answer-count", "single-choice question must have exactly one option")
        if qtype == 2 and len(plan.answer_ids) < 1:
            raise GuardError("answer-count", "multiple-choice question has no options")
        if plan.match_kind == "server" and self.policy.reject_server_correct:
            raise GuardError("server-correct", "server-side correct-answer flags are not trusted")

    def validate_coverage(self, missing: int, total: int) -> None:
        if not self.policy.enabled or total <= 0:
            return
        ratio = missing / total
        if ratio > self.policy.max_bank_miss_ratio:
            raise GuardError(
                "bank-coverage",
                f"bank coverage check failed: missing={missing}, total={total}, ratio={ratio:.4f}",
            )
        if missing and self.policy.abort_on_bank_miss:
            raise GuardError("bank-coverage", f"bank missing {missing} questions")
        self.emit("coverage_validated", {"missing": missing, "total": total})

    def minimum_exam_seconds(self, question_num: int, answer_time: int) -> float:
        explicit = max(0.0, self.policy.explicit_exam_min_seconds)
        per_question = max(0.0, question_num * self.policy.exam_min_seconds_per_question)
        minimum = max(explicit, per_question)
        if answer_time > 0:
            minimum = min(minimum, answer_time * 60)
        return minimum

    def validate_submit_timing(
        self,
        elapsed_seconds: float,
        question_num: int,
        answer_time: int,
    ) -> None:
        if not self.policy.enabled:
            return
        minimum = self.minimum_exam_seconds(question_num, answer_time)
        maximum = answer_time * 60 if answer_time > 0 else float("inf")
        if elapsed_seconds + 0.01 < minimum:
            raise GuardError(
                "submit-too-fast",
                f"submission is too fast: {elapsed_seconds:.1f}s < {minimum:.1f}s",
            )
        if elapsed_seconds > maximum:
            raise GuardError(
                "submit-too-slow",
                f"submission exceeded time limit: {elapsed_seconds:.1f}s > {maximum:.1f}s",
            )
        self.emit(
            "timing_validated",
            {"elapsed": round(elapsed_seconds, 3), "minimum": round(minimum, 3)},
        )

    def validate_result(
        self,
        result: Any,
        question_num: int,
        activity_type: int | None = None,
    ) -> None:
        if not self.policy.enabled:
            return
        if not isinstance(result, dict):
            raise GuardError("submit-shape", "submitPaper response is not an object")
        right = int(result.get("rightNum") or 0)
        error = int(result.get("errorNum") or 0)
        score = float(result.get("score") or 0)
        if right + error != question_num:
            raise GuardError(
                "result-count",
                f"result count mismatch: right+error={right + error}, expected={question_num}",
            )
        maximum_score = question_num if activity_type in {1, 2} else 100
        if score < 0 or score > maximum_score:
            raise GuardError("result-score", f"invalid result score: {score}")
        self.emit(
            "result_validated",
            {"right": right, "error": error, "score": score},
        )

    def emit(self, event: str, data: dict[str, Any] | None = None) -> None:
        if self.audit is not None:
            self.audit.append(event, data)

    @contextmanager
    def run_lock(self) -> Iterator[None]:
        if not self.policy.enabled:
            yield
            return
        lock_path = Path(self.policy.lock_path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+", encoding="utf-8")
        acquired = False
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("\0")
                handle.flush()
            deadline = self.clock() + self.policy.lock_timeout_seconds
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except (BlockingIOError, OSError):
                    if self.clock() >= deadline:
                        raise GuardError("run-lock", "another Mars run is already active")
                    time.sleep(0.05)
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
            self.emit("run_lock_acquired", {"pid": os.getpid()})
            yield
        finally:
            if acquired:
                self.emit("run_lock_released", {"pid": os.getpid()})
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()
