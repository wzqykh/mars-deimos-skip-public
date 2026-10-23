from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mars_deimos.bank import AnswerPlan
from mars_deimos.guard import AuditChain, GuardError, GuardPolicy, IntegrityGuard


def make_guard(directory: str, **overrides: object) -> IntegrityGuard:
    values: dict[str, object] = {
        "min_question_seconds": 0,
        "max_question_seconds": 0,
        "exam_min_seconds_per_question": 0,
        "explicit_exam_min_seconds": 0,
        "audit_path": str(Path(directory) / "audit.jsonl"),
        "lock_path": str(Path(directory) / "run.lock"),
    }
    values.update(overrides)
    policy = GuardPolicy(**values)
    return IntegrityGuard(policy)


class GuardTests(unittest.TestCase):
    def test_audit_chain_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            chain = AuditChain(path)
            chain.append("one", {"value": 1})
            chain.append("two", {"value": 2})
            valid, message = AuditChain.verify(path)
            self.assertTrue(valid, message)
            record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            record["data"]["value"] = 99
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            valid, _ = AuditChain.verify(path)
            self.assertFalse(valid)

    def test_question_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            guard = make_guard(directory)
            question = {
                "id": "q1",
                "type": 1,
                "optionList": [
                    {"id": "a", "content": "甲"},
                    {"id": "b", "content": "乙"},
                ],
            }
            seen: set[str] = set()
            self.assertEqual(guard.validate_question(question, seen), "q1")
            seen.add("q1")
            with self.assertRaises(GuardError):
                guard.validate_question(question, seen)

    def test_answer_validation_rejects_foreign_option(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            guard = make_guard(directory)
            question = {
                "id": "q1",
                "type": 1,
                "optionList": [
                    {"id": "a", "content": "甲"},
                    {"id": "b", "content": "乙"},
                ],
            }
            with self.assertRaises(GuardError):
                guard.validate_answer(
                    question,
                    AnswerPlan(("x",), True, 1.0, "exact"),
                )

    def test_server_correct_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            guard = make_guard(directory)
            question = {
                "id": "q1",
                "type": 1,
                "optionList": [
                    {"id": "a", "content": "甲"},
                    {"id": "b", "content": "乙"},
                ],
            }
            with self.assertRaises(GuardError):
                guard.validate_answer(
                    question,
                    AnswerPlan(("a",), True, 1.0, "server"),
                )

    def test_coverage_and_timing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            guard = make_guard(
                directory,
                exam_min_seconds_per_question=5,
                max_bank_miss_ratio=0,
                abort_on_bank_miss=True,
            )
            with self.assertRaises(GuardError):
                guard.validate_coverage(1, 100)
            self.assertEqual(guard.minimum_exam_seconds(100, 30), 500)
            with self.assertRaises(GuardError):
                guard.validate_submit_timing(100, 100, 30)

    def test_default_policy_sets_613_second_exam_floor(self) -> None:
        policy = GuardPolicy.from_config({})
        self.assertEqual(policy.exam_min_seconds_per_question, 0)
        self.assertEqual(policy.explicit_exam_min_seconds, 613)
        self.assertEqual(policy.min_question_seconds, 0)
        self.assertEqual(policy.max_question_seconds, 0.25)
        self.assertEqual(policy.exam_min_question_seconds, 5.0)
        self.assertEqual(policy.exam_max_question_seconds, 7.0)
        self.assertEqual(policy.max_bank_miss_ratio, 0.05)
        self.assertFalse(policy.abort_on_bank_miss)

    def test_single_run_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = make_guard(directory, lock_timeout_seconds=0)
            second = make_guard(directory, lock_timeout_seconds=0)
            with first.run_lock():
                with self.assertRaises(GuardError):
                    with second.run_lock():
                        pass

    def test_different_profile_locks_can_run_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = make_guard(directory, lock_path=str(Path(directory) / "a.lock"))
            second = make_guard(directory, lock_path=str(Path(directory) / "b.lock"))
            with first.run_lock():
                with second.run_lock():
                    pass


if __name__ == "__main__":
    unittest.main()
