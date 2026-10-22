from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable


TYPE_NAMES = {
    1: "单选题",
    2: "多选题",
    4: "判断题",
}

TYPE_ALIASES = {
    "1": 1,
    "单选": 1,
    "单选题": 1,
    "single": 1,
    "2": 2,
    "多选": 2,
    "多选题": 2,
    "multiple": 2,
    "multi": 2,
    "4": 4,
    "判断": 4,
    "判断题": 4,
    "judge": 4,
    "judgement": 4,
    "truefalse": 4,
}

_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200d\ufeff]")
_SPACE_RE = re.compile(r"\s+")
_OPTION_SPLIT_RE = re.compile(r"\s*[|｜]\s*|\r?\n+")
_ANSWER_SPLIT_RE = re.compile(r"[\s,，、;；/|｜]+")


def normalize_text(value: Any) -> str:
    """Normalize text for stable question matching."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _ZERO_WIDTH_RE.sub("", text)
    return _SPACE_RE.sub("", text).strip().lower()


def question_key(value: Any) -> str:
    """Drop punctuation and common blank markers before matching."""
    text = normalize_text(value)
    kept: list[str] = []
    for char in text:
        if char in "_＿":
            continue
        if unicodedata.category(char).startswith("P"):
            continue
        kept.append(char)
    return "".join(kept)


def parse_type(value: Any) -> int:
    key = normalize_text(value)
    if key in TYPE_ALIASES:
        return TYPE_ALIASES[key]
    try:
        number = int(key)
    except ValueError as exc:
        raise ValueError(f"unsupported question type: {value!r}") from exc
    if number not in TYPE_NAMES:
        raise ValueError(f"unsupported question type: {value!r}")
    return number


def split_options(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    text = str(value or "").strip()
    if not text:
        return ()
    return tuple(item.strip() for item in _OPTION_SPLIT_RE.split(text) if item.strip())


def parse_answers(value: Any) -> tuple[str, ...]:
    """Return option letters, normalizing common judgment answers to Y/N."""
    if isinstance(value, (list, tuple)):
        parsed: list[str] = []
        for item in value:
            parsed.extend(parse_answers(item))
        return tuple(parsed)
    text = normalize_text(value).upper()
    if not text:
        return ()
    true_words = {"对", "正确", "是", "√", "TRUE", "T"}
    false_words = {"错", "错误", "否", "×", "FALSE", "F"}
    if text in true_words:
        return ("Y",)
    if text in false_words:
        return ("N",)

    pieces = [piece for piece in _ANSWER_SPLIT_RE.split(text) if piece]
    if len(pieces) > 1:
        return tuple(piece for piece in pieces if piece in {"A", "B", "C", "D", "E", "F", "Y", "N"})

    compact = "".join(pieces)
    if compact in {"Y", "N"}:
        return (compact,)
    if compact and all(char in "ABCDEF" for char in compact):
        return tuple(compact)
    return ()


@dataclass(frozen=True)
class BankQuestion:
    number: str
    qtype: int
    category: str
    title: str
    options: tuple[str, ...]
    answers: tuple[str, ...]
    source: str = "xlsx"

    @classmethod
    def from_mapping(cls, row: dict[str, Any], source: str = "xlsx") -> "BankQuestion":
        title = str(row.get("title") or row.get("题目") or "").strip()
        if not title:
            raise ValueError("question title is empty")
        return cls(
            number=str(row.get("number") or row.get("序号") or "").strip(),
            qtype=parse_type(row.get("qtype") or row.get("type") or row.get("题型")),
            category=str(row.get("category") or row.get("类别") or "").strip(),
            title=title,
            options=split_options(row.get("options") or row.get("选项")),
            answers=parse_answers(row.get("answers") or row.get("answer") or row.get("答案")),
            source=source,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "qtype": self.qtype,
            "category": self.category,
            "title": self.title,
            "options": list(self.options),
            "answers": list(self.answers),
            "source": self.source,
        }


@dataclass(frozen=True)
class AnswerPlan:
    answer_ids: tuple[Any, ...]
    matched: bool
    confidence: float
    match_kind: str
    bank_question: BankQuestion | None = None

    @property
    def payload_value(self) -> str:
        return ",".join(str(item) for item in self.answer_ids)


def _first(mapping: dict[str, Any], names: Iterable[str], default: Any = "") -> Any:
    for name in names:
        value = mapping.get(name)
        if value is not None and value != "":
            return value
    return default


def api_option_entries(question: dict[str, Any]) -> list[dict[str, Any]]:
    raw = question.get("optionList") or question.get("options") or []
    entries: list[dict[str, Any]] = []
    for index, option in enumerate(raw):
        if isinstance(option, dict):
            option_id = _first(option, ("id", "optionId", "value", "code"), index)
            content = _first(
                option,
                ("content", "title", "text", "name", "label", "optionContent"),
                "",
            )
            selected = _first(option, ("selected", "isSelected"), 0)
            correct = _first(option, ("isCorrect", "correct"), 0)
            entries.append(
                {
                    "index": index,
                    "id": option_id,
                    "content": str(content),
                    "selected": selected,
                    "correct": correct,
                    "raw": option,
                }
            )
        else:
            entries.append(
                {
                    "index": index,
                    "id": index,
                    "content": str(option),
                    "selected": 0,
                    "correct": 0,
                    "raw": option,
                }
            )
    return entries


class QuestionBank:
    def __init__(self, questions: Iterable[BankQuestion] | None = None, fuzzy_threshold: float = 0.96):
        self.fuzzy_threshold = fuzzy_threshold
        self._exact: dict[str, list[BankQuestion]] = {}
        self._loose: dict[str, list[BankQuestion]] = {}
        self._all: list[BankQuestion] = []
        for question in questions or ():
            self.add(question)

    def __len__(self) -> int:
        return len(self._all)

    @classmethod
    def from_jsonl(cls, path: str | Path, fuzzy_threshold: float = 0.96) -> "QuestionBank":
        bank = cls(fuzzy_threshold=fuzzy_threshold)
        file_path = Path(path)
        if not file_path.exists():
            return bank
        with file_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                try:
                    bank.add(BankQuestion.from_mapping(row, source=str(row.get("source") or "jsonl")))
                except ValueError:
                    continue
        return bank

    @classmethod
    def from_files(
        cls,
        paths: Iterable[str | Path],
        fuzzy_threshold: float = 0.96,
    ) -> "QuestionBank":
        """Load several banks; later files override earlier duplicate questions."""
        merged: dict[tuple[str, int], BankQuestion] = {}
        for path in paths:
            loaded = cls.from_jsonl(path, fuzzy_threshold=fuzzy_threshold)
            for question in loaded._all:
                merged[(question_key(question.title), question.qtype)] = question
        return cls(merged.values(), fuzzy_threshold=fuzzy_threshold)

    def add(self, question: BankQuestion) -> None:
        self._all.append(question)
        self._exact.setdefault(normalize_text(question.title), []).append(question)
        self._loose.setdefault(question_key(question.title), []).append(question)

    def _unique(self, candidates: list[BankQuestion]) -> BankQuestion | None:
        if not candidates:
            return None
        signatures = {(item.qtype, item.answers, item.options) for item in candidates}
        return candidates[0] if len(signatures) == 1 else None

    def find(self, title: str, qtype: int | None = None) -> tuple[BankQuestion | None, str, float]:
        candidates = list(self._exact.get(normalize_text(title), ()))
        if not candidates:
            candidates = list(self._loose.get(question_key(title), ()))
        if qtype is not None:
            candidates = [item for item in candidates if item.qtype == qtype]
        question = self._unique(candidates)
        if question:
            kind = "exact" if normalize_text(title) in self._exact else "normalized"
            return question, kind, 1.0

        target = question_key(title)
        best: BankQuestion | None = None
        best_score = 0.0
        for item in self._all:
            if qtype is not None and item.qtype != qtype:
                continue
            score = SequenceMatcher(None, target, question_key(item.title)).ratio()
            if score > best_score:
                best, best_score = item, score
        if best is not None and best_score >= self.fuzzy_threshold:
            return best, "fuzzy", best_score
        return None, "miss", best_score

    def resolve(self, question: dict[str, Any], allow_server_correct: bool = False) -> AnswerPlan:
        title = _first(question, ("title", "content", "question", "stem"), "")
        try:
            qtype = parse_type(_first(question, ("type", "questionType", "qtype"), ""))
        except ValueError:
            qtype = None
        bank_question, kind, confidence = self.find(str(title), qtype)
        options = api_option_entries(question)

        if bank_question and bank_question.answers:
            answer_ids = self._map_bank_answers(bank_question, options)
            if answer_ids:
                return AnswerPlan(tuple(answer_ids), True, confidence, kind, bank_question)

        if allow_server_correct:
            answer_ids = [
                option["id"]
                for option in options
                if str(option.get("correct")).lower() in {"1", "true"}
            ]
            if answer_ids:
                return AnswerPlan(tuple(answer_ids), True, 1.0, "server", None)

        fallback = (options[0]["id"],) if options else ()
        return AnswerPlan(tuple(fallback), False, confidence, "fallback", bank_question)

    @staticmethod
    def _map_bank_answers(bank_question: BankQuestion, options: list[dict[str, Any]]) -> list[Any]:
        if not options:
            return []
        answer_ids: list[Any] = []
        used_indexes: set[int] = set()
        for letter in bank_question.answers:
            if letter in {"Y", "N"}:
                index = 0 if letter == "Y" else 1
                if index < len(options) and index not in used_indexes:
                    answer_ids.append(options[index]["id"])
                    used_indexes.add(index)
                continue
            if letter not in "ABCDEF":
                continue
            bank_index = ord(letter) - ord("A")
            if bank_index >= len(bank_question.options):
                if bank_index < len(options):
                    answer_ids.append(options[bank_index]["id"])
                    used_indexes.add(bank_index)
                continue
            bank_text = normalize_text(bank_question.options[bank_index])
            found_index = next(
                (
                    option["index"]
                    for option in options
                    if normalize_text(option["content"]) == bank_text
                ),
                bank_index if bank_index < len(options) else None,
            )
            if found_index is not None and found_index not in used_indexes:
                answer_ids.append(options[found_index]["id"])
                used_indexes.add(found_index)
        return answer_ids

    def add_server_question(self, question: dict[str, Any], source: str = "learned") -> BankQuestion | None:
        title = str(_first(question, ("title", "content", "question", "stem"), "")).strip()
        try:
            qtype = parse_type(_first(question, ("type", "questionType", "qtype"), ""))
        except ValueError:
            return None
        options = api_option_entries(question)
        letters: list[str] = []
        for option in options:
            if str(option.get("correct")).lower() in {"1", "true"}:
                letters.append(chr(ord("A") + option["index"]))
        if not title or not letters:
            return None
        learned = BankQuestion(
            number="",
            qtype=qtype,
            category="错题回收",
            title=title,
            options=tuple(option["content"] for option in options),
            answers=tuple(letters),
            source=source,
        )
        self.add(learned)
        return learned

    @staticmethod
    def save_jsonl(questions: Iterable[BankQuestion], path: str | Path) -> int:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with output.open("w", encoding="utf-8") as handle:
            for question in questions:
                handle.write(json.dumps(question.to_mapping(), ensure_ascii=False) + "\n")
                count += 1
        return count

    def stats(self) -> dict[str, Any]:
        by_type: dict[int, int] = {}
        by_source: dict[str, int] = {}
        for item in self._all:
            by_type[item.qtype] = by_type.get(item.qtype, 0) + 1
            by_source[item.source] = by_source.get(item.source, 0) + 1
        return {
            "total": len(self),
            "by_type": {TYPE_NAMES[key]: value for key, value in sorted(by_type.items())},
            "by_source": by_source,
        }
