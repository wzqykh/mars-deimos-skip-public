from __future__ import annotations

import tempfile
import unittest
from collections import Counter
from pathlib import Path
import random

from mars_deimos.api import ApiError
from mars_deimos.bank import AnswerPlan, BankQuestion, QuestionBank
from mars_deimos.guard import AuditChain, GuardPolicy, IntegrityGuard
from mars_deimos.workflow import MarsWorkflow


class FakeApi:
    @staticmethod
    def activity_kind(activity_type: object) -> str:
        return "exam" if int(activity_type) in {3, 4} else "practice"

    def __init__(self) -> None:
        self.recorded: list[dict[str, object]] = []
        self.saved: dict[str, set[str]] = {}
        self.submit_called = False

    def prepare_paper(self, activity_type: int, user_activity_id: int) -> dict[str, object]:
        return {"questionNum": 1, "answerTime": 30, "totalTimes": 0, "doneTimes": 0}

    def start_paper(self, activity_type: int, user_activity_id: int) -> dict[str, object]:
        return {
            "currentIndex": 1,
            "hasNext": 0,
            "questionList": [
                {
                    "id": 501,
                    "type": 1,
                    "title": "测试题",
                    "optionList": [
                        {
                            "id": "a",
                            "content": "甲",
                            "selected": int("a" in self.saved.get("501", set())),
                        },
                        {
                            "id": "b",
                            "content": "乙",
                            "selected": int("b" in self.saved.get("501", set())),
                        },
                    ],
                }
            ],
        }

    def record_question(self, *args: object, **kwargs: object) -> None:
        self.recorded.append({"args": args, "kwargs": kwargs})
        question_id = str(args[2])
        self.saved[question_id] = {item for item in str(args[3]).split(",") if item}

    def submit_paper(self, activity_type: int, user_activity_id: int) -> dict[str, object]:
        self.submit_called = True
        return {"score": 100, "rightNum": 1, "errorNum": 0}

    def list_history(self, activity_type: int, user_activity_id: int) -> list[dict[str, object]]:
        return []

    def list_error_questions(self, activity_type: int, user_exam_id: int) -> list[dict[str, object]]:
        return [
            {
                "id": 601,
                "type": 1,
                "title": "错题回收题",
                "optionList": [
                    {"id": "a", "content": "错误项", "isCorrect": 0},
                    {"id": "b", "content": "正确项", "isCorrect": 1},
                ],
            }
        ]


class WorkflowTests(unittest.TestCase):
    def test_practice_collects_pages_before_and_after_resume_position(self) -> None:
        class PracticePagesApi(FakeApi):
            pages = {
                1: [1, 2],
                2: [3, 4],
                3: [5, 6],
            }

            @staticmethod
            def page(index: int) -> dict[str, object]:
                return {
                    "currentIndex": index,
                    "hasBefore": 2 if index == 1 else 1,
                    "hasNext": 2 if index == 3 else 1,
                    "questionList": [
                        {
                            "id": question_id,
                            "type": 1,
                            "title": "测试题",
                            "optionList": [
                                {"id": "a", "content": "甲"},
                                {"id": "b", "content": "乙"},
                            ],
                        }
                        for question_id in PracticePagesApi.pages[index]
                    ],
                }

            def prepare_paper(
                self,
                activity_type: int,
                user_activity_id: int,
            ) -> dict[str, object]:
                return {
                    "questionNum": 6,
                    "answerTime": 0,
                    "totalTimes": 0,
                    "doneTimes": 0,
                }

            def start_paper(
                self,
                activity_type: int,
                user_activity_id: int,
            ) -> dict[str, object]:
                return self.page(3)

            def get_near(
                self,
                user_activity_id: int,
                current_index: int,
                near_type: int,
            ) -> dict[str, object]:
                return self.page(current_index + (-1 if near_type == 1 else 1))

            def submit_paper(
                self,
                activity_type: int,
                user_activity_id: int,
            ) -> dict[str, object]:
                self.submit_called = True
                return {"score": 100, "rightNum": 6, "errorNum": 0}

        bank = QuestionBank([BankQuestion("1", 1, "", "测试题", ("甲", "乙"), ("B",))])
        api = PracticePagesApi()
        workflow = MarsWorkflow(
            api,
            bank,
            {
                "collect_wrong_answers": False,
                "allow_partial_practice_submit": False,
                "max_pages": 50,
            },
            emit=lambda _: None,
        )
        report = workflow.execute(
            {"activityType": 1, "userActivityId": 20, "name": "顺序练习"},
            collect_wrong=False,
        )
        self.assertEqual(len(report.questions), 6)
        self.assertTrue(report.submitted)
        page_counts = Counter(item["kwargs"]["current_index"] for item in api.recorded)
        self.assertEqual(page_counts, {1: 2, 2: 2, 3: 2})

    def test_practice_skips_formal_exam_timing_and_strategy_message(self) -> None:
        bank = QuestionBank([BankQuestion("1", 1, "", "测试题", ("甲", "乙"), ("B",))])
        class PracticeApi(FakeApi):
            def submit_paper(
                self,
                activity_type: int,
                user_activity_id: int,
            ) -> dict[str, object]:
                self.submit_called = True
                return {"score": 1, "rightNum": 1, "errorNum": 0}

        api = PracticeApi()
        messages: list[str] = []
        now = [0.0]

        def clock() -> float:
            return now[0]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        with tempfile.TemporaryDirectory() as directory:
            policy = GuardPolicy(
                min_question_seconds=0,
                max_question_seconds=0,
                exam_min_question_seconds=0,
                exam_max_question_seconds=0,
                exam_min_seconds_per_question=0,
                explicit_exam_min_seconds=613,
                audit_path=str(Path(directory) / "audit.jsonl"),
                lock_path=str(Path(directory) / "run.lock"),
            )
            workflow = MarsWorkflow(
                api,
                bank,
                {"collect_wrong_answers": False},
                emit=messages.append,
                clock=clock,
                sleep=sleep,
                guard=IntegrityGuard(policy),
            )
            report = workflow.execute(
                {"activityType": 1, "userActivityId": 20, "name": "顺序练习"},
                collect_wrong=False,
            )
        self.assertTrue(report.submitted)
        self.assertEqual(report.elapsed_seconds, 0)
        self.assertNotIn("正式考试策略", "\n".join(messages))

    def test_incomplete_practice_is_not_submitted_by_default(self) -> None:
        class IncompletePracticeApi(FakeApi):
            def prepare_paper(
                self,
                activity_type: int,
                user_activity_id: int,
            ) -> dict[str, object]:
                return {
                    "questionNum": 2,
                    "answerTime": 0,
                    "totalTimes": 0,
                    "doneTimes": 0,
                }

        bank = QuestionBank([BankQuestion("1", 1, "", "测试题", ("甲", "乙"), ("B",))])
        api = IncompletePracticeApi()
        with tempfile.TemporaryDirectory() as directory:
            workflow = MarsWorkflow(
                api,
                bank,
                {
                    "collect_wrong_answers": False,
                    "allow_partial_practice_submit": False,
                    "learned_path": str(Path(directory) / "learned.jsonl"),
                },
                emit=lambda _: None,
            )
            with self.assertRaises(ApiError):
                workflow.execute(
                    {"activityType": 1, "userActivityId": 20, "name": "顺序练习"},
                    collect_wrong=False,
                )
        self.assertFalse(api.submit_called)

    def test_human_mode_selects_between_eight_and_eleven_distinct_questions(self) -> None:
        workflow = MarsWorkflow(
            FakeApi(),
            QuestionBank([BankQuestion("1", 1, "", "测试题", ("甲", "乙"), ("B",))]),
            {"deliberate_wrong_min": 8, "deliberate_wrong_max": 11},
            emit=lambda _: None,
            random_source=random.Random(11),
        )
        questions = [
            {
                "id": 700 + index,
                "type": 1,
                "title": "测试题",
                "optionList": [
                    {"id": "a", "content": "甲"},
                    {"id": "b", "content": "乙"},
                ],
            }
            for index in range(20)
        ]
        plans = workflow._plan_deliberate_wrong_questions(
            questions,
            allow_server_correct=False,
        )
        self.assertGreaterEqual(len(plans), 8)
        self.assertLessEqual(len(plans), 11)
        self.assertEqual(len(set(plans)), len(plans))
        self.assertTrue(all(plan.match_kind == "deliberate-wrong" for plan in plans.values()))

    def test_human_mode_builds_valid_wrong_multi_choice_answer(self) -> None:
        workflow = MarsWorkflow(
            FakeApi(),
            QuestionBank(),
            {},
            emit=lambda _: None,
            random_source=random.Random(9),
        )
        question = {
            "id": 900,
            "type": 2,
            "optionList": [
                {"id": "a", "content": "甲"},
                {"id": "b", "content": "乙"},
                {"id": "c", "content": "丙"},
                {"id": "d", "content": "丁"},
            ],
        }
        correct = AnswerPlan(("a", "b"), True, 1.0, "exact")
        for _ in range(20):
            wrong = workflow._build_deliberate_wrong_plan(question, correct)
            self.assertIsNotNone(wrong)
            self.assertTrue(wrong.answer_ids)
            self.assertNotEqual(set(wrong.answer_ids), {"a", "b"})

    def test_score96_mode_plans_exactly_four_wrong_questions(self) -> None:
        workflow = MarsWorkflow(
            FakeApi(),
            QuestionBank([BankQuestion("1", 1, "", "测试题", ("甲", "乙"), ("B",))]),
            {"score96_wrong_count": 4},
            emit=lambda _: None,
            random_source=random.Random(13),
        )
        questions = [
            {
                "id": 800 + index,
                "type": 1,
                "title": "测试题",
                "optionList": [
                    {"id": "a", "content": "甲"},
                    {"id": "b", "content": "乙"},
                ],
            }
            for index in range(20)
        ]
        plans = workflow._plan_deliberate_wrong_questions(
            questions,
            allow_server_correct=False,
            target_count=4,
        )
        self.assertEqual(len(plans), 4)
        self.assertEqual(len(set(plans)), 4)

    def test_human_mode_plans_and_submits_two_wrong_answers(self) -> None:
        class HumanApi(FakeApi):
            def prepare_paper(
                self,
                activity_type: int,
                user_activity_id: int,
            ) -> dict[str, object]:
                return {"questionNum": 3, "answerTime": 30, "totalTimes": 10, "doneTimes": 0}

            def start_paper(
                self,
                activity_type: int,
                user_activity_id: int,
            ) -> dict[str, object]:
                return {
                    "currentIndex": 1,
                    "hasNext": 0,
                    "questionList": [
                        {
                            "id": 501 + index,
                            "type": 1,
                            "title": "测试题",
                            "optionList": [
                                {"id": "a", "content": "甲"},
                                {"id": "b", "content": "乙"},
                            ],
                        }
                        for index in range(3)
                    ],
                }

            def submit_paper(
                self,
                activity_type: int,
                user_activity_id: int,
            ) -> dict[str, object]:
                self.submit_called = True
                return {"score": 98, "rightNum": 1, "errorNum": 2}

        bank = QuestionBank([BankQuestion("1", 1, "", "测试题", ("甲", "乙"), ("B",))])
        api = HumanApi()
        with tempfile.TemporaryDirectory() as directory:
            config = {
                "question_delay_seconds": 0,
                "allow_server_correct": False,
                "collect_wrong_answers": False,
                "exam_min_seconds": 0,
                "submit_retry_count": 0,
                "submit_retry_seconds": 0,
                "max_pages": 2,
                "deliberate_wrong_min": 2,
                "deliberate_wrong_max": 2,
                "audit_submitted_answers": False,
                "learned_path": str(Path(directory) / "learned.jsonl"),
            }
            workflow = MarsWorkflow(
                api,
                bank,
                config,
                emit=lambda _: None,
                random_source=random.Random(7),
            )
            report = workflow.execute(
                {"activityType": 4, "userActivityId": 20, "name": "正式考试"},
                collect_wrong=False,
                answer_mode="human",
            )
        self.assertTrue(report.submitted)
        self.assertEqual(report.deliberate_wrong_planned, 2)
        self.assertEqual(report.deliberate_wrong_sent, 2)
        self.assertEqual(report.result["errorNum"], 2)
        self.assertEqual(sum(item.deliberate_wrong for item in report.questions), 2)

    def test_execute_records_and_submits(self) -> None:
        bank = QuestionBank([BankQuestion("1", 1, "", "测试题", ("甲", "乙"), ("B",))])
        api = FakeApi()
        with tempfile.TemporaryDirectory() as directory:
            config = {
                "question_delay_seconds": 0,
                "allow_server_correct": True,
                "collect_wrong_answers": False,
                "exam_min_seconds": 0,
                "submit_retry_count": 0,
                "submit_retry_seconds": 0,
                "max_pages": 2,
                "learned_path": str(Path(directory) / "learned.jsonl"),
            }
            workflow = MarsWorkflow(api, bank, config, emit=lambda _: None)
            report = workflow.execute(
                {"activityType": 3, "userActivityId": 20, "name": "测试考试"},
                collect_wrong=False,
            )
        self.assertTrue(report.submitted)
        self.assertEqual(report.result["score"], 100)
        self.assertEqual(len(api.recorded), 1)
        self.assertEqual(api.recorded[0]["args"][3], "b")

    def test_collect_wrong_appends_learned_answer(self) -> None:
        bank = QuestionBank()
        api = FakeApi()
        with tempfile.TemporaryDirectory() as directory:
            learned_path = Path(directory) / "learned.jsonl"
            config = {"learned_path": str(learned_path)}
            workflow = MarsWorkflow(api, bank, config, emit=lambda _: None)
            count = workflow.collect_wrong(3, 20, submit_result={"userExamId": 77})
            rows = learned_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(count, 1)
        self.assertEqual(len(rows), 1)
        self.assertIn('"answers": ["B"]', rows[0])

    def test_collect_wrong_does_not_duplicate_semantically_equal_judgement(self) -> None:
        class JudgementApi(FakeApi):
            def list_error_questions(
                self,
                activity_type: int,
                user_exam_id: int,
            ) -> list[dict[str, object]]:
                return [
                    {
                        "id": 602,
                        "type": 4,
                        "title": "判断题",
                        "optionList": [
                            {"id": "yes", "content": "正确", "isCorrect": 0},
                            {"id": "no", "content": "错误", "isCorrect": 1},
                        ],
                    }
                ]

        bank = QuestionBank(
            [BankQuestion("1", 4, "", "判断题", ("正确", "错误"), ("N",))]
        )
        api = JudgementApi()
        with tempfile.TemporaryDirectory() as directory:
            learned_path = Path(directory) / "learned.jsonl"
            config = {"learned_path": str(learned_path)}
            workflow = MarsWorkflow(api, bank, config, emit=lambda _: None)
            count = workflow.collect_wrong(3, 20, submit_result={"userExamId": 77})
        self.assertEqual(count, 0)
        self.assertFalse(learned_path.exists())

    def test_execute_with_integrity_guard_creates_valid_audit_chain(self) -> None:
        bank = QuestionBank([BankQuestion("1", 1, "", "测试题", ("甲", "乙"), ("B",))])
        api = FakeApi()
        with tempfile.TemporaryDirectory() as directory:
            audit_path = Path(directory) / "audit.jsonl"
            policy = GuardPolicy(
                min_question_seconds=0,
                max_question_seconds=0,
                exam_min_seconds_per_question=0,
                explicit_exam_min_seconds=0,
                exam_min_question_seconds=0,
                exam_max_question_seconds=0,
                audit_path=str(audit_path),
                lock_path=str(Path(directory) / "run.lock"),
            )
            guard = IntegrityGuard(policy)
            config = {
                "question_delay_seconds": 0,
                "allow_server_correct": False,
                "collect_wrong_answers": False,
                "exam_min_seconds": 0,
                "submit_retry_count": 0,
                "submit_retry_seconds": 0,
                "max_pages": 2,
                "learned_path": str(Path(directory) / "learned.jsonl"),
            }
            workflow = MarsWorkflow(api, bank, config, emit=lambda _: None, guard=guard)
            report = workflow.execute(
                {"activityType": 3, "userActivityId": 20, "name": "测试考试"},
                collect_wrong=False,
            )
            valid, message = AuditChain.verify(audit_path)
        self.assertTrue(report.submitted)
        self.assertTrue(valid, message)

    def test_default_guard_waits_until_613_seconds(self) -> None:
        bank = QuestionBank([BankQuestion("1", 1, "", "测试题", ("甲", "乙"), ("B",))])
        api = FakeApi()
        now = [0.0]

        def clock() -> float:
            return now[0]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        with tempfile.TemporaryDirectory() as directory:
            policy = GuardPolicy(
                min_question_seconds=0,
                max_question_seconds=0,
                exam_min_seconds_per_question=0,
                explicit_exam_min_seconds=613,
                exam_min_question_seconds=0,
                exam_max_question_seconds=0,
                audit_path=str(Path(directory) / "audit.jsonl"),
                lock_path=str(Path(directory) / "run.lock"),
            )
            guard = IntegrityGuard(policy)
            config = {
                "question_delay_seconds": 0,
                "allow_server_correct": False,
                "collect_wrong_answers": False,
                "exam_min_seconds": 0,
                "submit_retry_count": 0,
                "submit_retry_seconds": 0,
                "max_pages": 2,
                "learned_path": str(Path(directory) / "learned.jsonl"),
            }
            workflow = MarsWorkflow(
                api,
                bank,
                config,
                emit=lambda _: None,
                sleep=sleep,
                clock=clock,
                guard=guard,
            )
            report = workflow.execute(
                {"activityType": 3, "userActivityId": 20, "name": "测试考试"},
                collect_wrong=False,
            )
        self.assertEqual(report.elapsed_seconds, 613)

    def test_short_formal_exam_waits_until_270_seconds(self) -> None:
        bank = QuestionBank([BankQuestion("1", 1, "", "测试题", ("甲", "乙"), ("B",))])
        api = FakeApi()
        now = [0.0]

        def clock() -> float:
            return now[0]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        with tempfile.TemporaryDirectory() as directory:
            policy = GuardPolicy(
                min_question_seconds=0,
                max_question_seconds=0,
                exam_min_question_seconds=0,
                exam_max_question_seconds=0,
                short_exam_min_question_seconds=0,
                short_exam_max_question_seconds=0,
                exam_min_seconds_per_question=0,
                explicit_exam_min_seconds=613,
                short_explicit_exam_min_seconds=270,
                audit_path=str(Path(directory) / "audit.jsonl"),
                lock_path=str(Path(directory) / "run.lock"),
            )
            workflow = MarsWorkflow(
                api,
                bank,
                {
                    "question_delay_seconds": 0,
                    "collect_wrong_answers": False,
                    "exam_min_seconds": 0,
                    "submit_retry_count": 0,
                    "submit_retry_seconds": 0,
                    "max_pages": 2,
                    "learned_path": str(Path(directory) / "learned.jsonl"),
                },
                emit=lambda _: None,
                clock=clock,
                sleep=sleep,
                guard=IntegrityGuard(policy),
            )
            report = workflow.execute(
                {"activityType": 4, "userActivityId": 20, "name": "正式考试"},
                collect_wrong=False,
                answer_mode="perfect",
                exam_duration_mode="short",
            )
        self.assertEqual(report.elapsed_seconds, 270)

    def test_quiet_mode_hides_per_question_output(self) -> None:
        bank = QuestionBank([BankQuestion("1", 1, "", "测试题", ("甲", "乙"), ("B",))])
        api = FakeApi()
        messages: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            config = {
                "question_delay_seconds": 0,
                "allow_server_correct": False,
                "collect_wrong_answers": False,
                "exam_min_seconds": 0,
                "submit_retry_count": 0,
                "submit_retry_seconds": 0,
                "max_pages": 2,
                "learned_path": str(Path(directory) / "learned.jsonl"),
                "verbose_questions": False,
            }
            workflow = MarsWorkflow(api, bank, config, emit=messages.append)
            workflow.execute(
                {"activityType": 3, "userActivityId": 20, "name": "测试考试"},
                collect_wrong=False,
            )
        joined = "\n".join(messages)
        self.assertNotIn("测试题", joined)
        self.assertIn("开始答题", joined)
        self.assertIn("答题完成", joined)

    def test_exam_audit_detects_unrecorded_answers(self) -> None:
        class UnrecordedApi(FakeApi):
            def list_history(
                self,
                activity_type: int,
                user_activity_id: int,
            ) -> list[dict[str, object]]:
                return [{"id": 77, "submitTime": "2026-10-22 12:00:00"}]

            def list_error_questions(
                self,
                activity_type: int,
                user_exam_id: int,
            ) -> list[dict[str, object]]:
                return [
                    {
                        "id": 601,
                        "type": 1,
                        "title": "未落库题",
                        "optionList": [
                            {"id": "a", "content": "甲", "isCorrect": 0, "selected": 0},
                            {"id": "b", "content": "乙", "isCorrect": 1, "selected": 0},
                        ],
                    }
                ]

        bank = QuestionBank([BankQuestion("1", 1, "", "测试题", ("甲", "乙"), ("B",))])
        api = UnrecordedApi()
        messages: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            config = {
                "question_delay_seconds": 0,
                "allow_server_correct": False,
                "collect_wrong_answers": False,
                "exam_min_seconds": 0,
                "submit_retry_count": 0,
                "submit_retry_seconds": 0,
                "max_pages": 2,
                "audit_submitted_answers": True,
                "learned_path": str(Path(directory) / "learned.jsonl"),
            }
            workflow = MarsWorkflow(api, bank, config, emit=messages.append)
            with self.assertRaises(ApiError):
                workflow.execute(
                    {"activityType": 4, "userActivityId": 20, "name": "测试考试"},
                    collect_wrong=False,
                )
        self.assertTrue(api.submit_called)
        self.assertIn("服务端未记录选择 1 道", "\n".join(messages))


if __name__ == "__main__":
    unittest.main()
