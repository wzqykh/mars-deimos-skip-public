from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mars_deimos.bank import BankQuestion, QuestionBank, normalize_text, parse_answers


class BankTests(unittest.TestCase):
    def test_answer_formats(self) -> None:
        self.assertEqual(parse_answers("A|B|D"), ("A", "B", "D"))
        self.assertEqual(parse_answers("正确"), ("Y",))
        self.assertEqual(parse_answers("错"), ("N",))
        self.assertEqual(parse_answers("AC"), ("A", "C"))

    def test_shuffled_option_mapping(self) -> None:
        bank = QuestionBank(
            [
                BankQuestion(
                    number="1",
                    qtype=1,
                    category="测试",
                    title="示例题的答案是？",
                    options=("甲", "乙", "丙", "丁"),
                    answers=("B",),
                )
            ]
        )
        question = {
            "id": 9001,
            "type": 1,
            "title": "示例题的答案是？",
            "optionList": [
                {"id": 14, "content": "丁"},
                {"id": 12, "content": "乙"},
                {"id": 11, "content": "甲"},
                {"id": 13, "content": "丙"},
            ],
        }
        plan = bank.resolve(question)
        self.assertTrue(plan.matched)
        self.assertEqual(plan.answer_ids, (12,))
        self.assertEqual(plan.match_kind, "exact")

    def test_multi_choice_and_judgement(self) -> None:
        bank = QuestionBank(
            [
                BankQuestion("1", 2, "", "多选题", ("一", "二", "三", "四"), ("A", "C")),
                BankQuestion("2", 4, "", "判断题", ("Y", "N"), ("N",)),
            ]
        )
        multi = bank.resolve(
            {
                "id": 1,
                "type": 2,
                "title": "多选题",
                "optionList": [
                    {"id": "a", "content": "一"},
                    {"id": "b", "content": "二"},
                    {"id": "c", "content": "三"},
                    {"id": "d", "content": "四"},
                ],
            }
        )
        judge = bank.resolve(
            {
                "id": 2,
                "type": 4,
                "title": "判断题",
                "optionList": [{"id": "yes", "content": "正确"}, {"id": "no", "content": "错误"}],
            }
        )
        self.assertEqual(multi.payload_value, "a,c")
        self.assertEqual(judge.payload_value, "no")

    def test_punctuation_tolerant_match(self) -> None:
        title = "遇到火灾时，下列做法正确的是____。"
        bank = QuestionBank([BankQuestion("1", 1, "", title, ("甲", "乙"), ("A",))])
        question = {
            "id": 7,
            "type": 1,
            "title": "遇到火灾时 下列做法正确的是。",
            "optionList": [{"id": "x", "content": "甲"}, {"id": "y", "content": "乙"}],
        }
        self.assertEqual(normalize_text(title)[:3], "遇到火")
        plan = bank.resolve(question)
        self.assertTrue(plan.matched)
        self.assertIn(plan.match_kind, {"exact", "normalized", "fuzzy"})

    def test_server_correct_fallback(self) -> None:
        bank = QuestionBank()
        question = {
            "id": 10,
            "type": 1,
            "title": "不在题库",
            "optionList": [
                {"id": "a", "content": "一", "isCorrect": 0},
                {"id": "b", "content": "二", "isCorrect": 1},
            ],
        }
        plan = bank.resolve(question, allow_server_correct=True)
        self.assertTrue(plan.matched)
        self.assertEqual(plan.answer_ids, ("b",))
        self.assertEqual(plan.match_kind, "server")

    def test_learned_bank_overrides_base(self) -> None:
        base = BankQuestion("1", 1, "", "会变化的题", ("甲", "乙"), ("A",), source="xlsx")
        learned = BankQuestion("1", 1, "", "会变化的题", ("甲", "乙"), ("B",), source="learned")
        with tempfile.TemporaryDirectory() as directory:
            base_path = Path(directory) / "base.jsonl"
            learned_path = Path(directory) / "learned.jsonl"
            base_path.write_text(json.dumps(base.to_mapping(), ensure_ascii=False) + "\n", encoding="utf-8")
            learned_path.write_text(json.dumps(learned.to_mapping(), ensure_ascii=False) + "\n", encoding="utf-8")
            bank = QuestionBank.from_files([base_path, learned_path])
        plan = bank.resolve(
            {
                "id": 3,
                "type": 1,
                "title": "会变化的题",
                "optionList": [{"id": "a", "content": "甲"}, {"id": "b", "content": "乙"}],
            }
        )
        self.assertEqual(plan.payload_value, "b")


if __name__ == "__main__":
    unittest.main()
