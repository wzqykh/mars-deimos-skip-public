from __future__ import annotations

import json
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .api import ApiClient, ApiError
from .bank import (
    AnswerPlan,
    QuestionBank,
    TYPE_NAMES,
    api_option_entries,
    normalize_text,
    parse_type,
)
from .guard import IntegrityGuard


Emitter = Callable[[str], None]


def _boolish(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _question_text(question: dict[str, Any]) -> str:
    for key in ("title", "content", "question", "stem"):
        value = question.get(key)
        if value:
            return str(value)
    return ""


def _selected_option_ids(question: dict[str, Any]) -> set[str]:
    options = question.get("optionList") or question.get("options") or []
    return {
        str(option.get("id"))
        for option in options
        if isinstance(option, dict) and _boolish(option.get("selected"))
    }


@dataclass
class QuestionResult:
    question_id: Any
    qtype: int
    title: str
    answer_ids: list[Any]
    matched: bool
    match_kind: str
    confidence: float
    deliberate_wrong: bool = False


@dataclass
class RunReport:
    activity_type: int = 0
    user_activity_id: Any = ""
    activity_name: str = ""
    prepared: dict[str, Any] = field(default_factory=dict)
    questions: list[QuestionResult] = field(default_factory=list)
    missing: int = 0
    submitted: bool = False
    result: Any = None
    wrong_learned: int = 0
    unrecorded_answers: int = 0
    answer_mode: str = "perfect"
    deliberate_wrong_planned: int = 0
    deliberate_wrong_sent: int = 0
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, state_dir: str | Path) -> Path:
        output = Path(state_dir) / "runs" / f"run-{int(time.time())}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return output


class MarsWorkflow:
    def __init__(
        self,
        api: ApiClient,
        bank: QuestionBank,
        config: dict[str, Any],
        *,
        emit: Emitter = print,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        guard: IntegrityGuard | None = None,
        random_source: random.Random | None = None,
    ):
        self.api = api
        self.bank = bank
        self.config = config
        self.emit = emit
        self.sleep = sleep
        self.clock = clock
        self.guard = guard
        self.random = random_source or random.SystemRandom()

    def home_snapshot(self) -> dict[str, Any]:
        project = self.api.project_index()
        activities = list(project.get("studyActivityList") or [])
        rankings: list[dict[str, Any]] = []
        if project.get("projectId"):
            rankings = self.api.simpleorder_list(project.get("projectId"))
        user = self.api.user_index()
        return {
            "project": project,
            "activities": activities,
            "rankings": rankings,
            "user": user,
        }

    @staticmethod
    def choose_activity(
        activities: list[dict[str, Any]],
        *,
        index: int = 0,
        user_activity_id: Any = "",
        activity_type: Any = None,
    ) -> dict[str, Any]:
        if user_activity_id not in {"", None}:
            for activity in activities:
                if str(activity.get("userActivityId")) == str(user_activity_id):
                    if activity_type is not None:
                        selected = dict(activity)
                        selected["activityType"] = int(activity_type)
                        return selected
                    return activity
            if activity_type is None:
                raise ValueError("--activity-type is required with an unmatched --user-activity-id")
            return {"userActivityId": user_activity_id, "activityType": int(activity_type), "name": ""}
        if not activities:
            raise ValueError("no study activities returned for this account")
        if index < 0 or index >= len(activities):
            raise ValueError(f"activity index {index} is out of range (0..{len(activities) - 1})")
        return activities[index]

    def execute(
        self,
        activity: dict[str, Any],
        *,
        dry_run: bool = False,
        collect_wrong: bool | None = None,
        answer_mode: str | None = None,
        exam_duration_mode: str | None = None,
    ) -> RunReport:
        activity_type = int(activity.get("activityType"))
        is_exam = self.api.activity_kind(activity_type) == "exam"
        user_activity_id = activity.get("userActivityId")
        name = str(activity.get("name") or "")
        answer_mode = str(
            answer_mode or self.config.get("formal_exam_mode") or "perfect"
        ).strip().lower()
        if activity_type != 4:
            answer_mode = "perfect"
        if answer_mode not in {"perfect", "human", "score96", "score75"}:
            raise ValueError(f"unsupported answer mode: {answer_mode}")
        exam_duration_mode = str(
            exam_duration_mode
            or self.config.get("formal_exam_duration")
            or "standard"
        ).strip().lower()
        if activity_type != 4 or exam_duration_mode == "ask":
            exam_duration_mode = "standard"
        if exam_duration_mode not in {"standard", "short"}:
            raise ValueError(f"unsupported exam duration mode: {exam_duration_mode}")
        short_exam = exam_duration_mode == "short"
        report = RunReport(
            activity_type=activity_type,
            user_activity_id=user_activity_id,
            activity_name=name,
            answer_mode=answer_mode,
        )
        if self.guard:
            self.guard.validate_activity(activity)
            self.guard.emit(
                "run_started",
                {"activity_type": activity_type, "activity_name": name},
            )
        started_at = self.clock()
        prepared = self.api.prepare_paper(activity_type, user_activity_id)
        report.prepared = prepared if isinstance(prepared, dict) else {}
        if self.guard:
            self.guard.validate_prepare(activity_type, prepared)
        expected = int(report.prepared.get("questionNum") or 0)
        answer_time = report.prepared.get("answerTime")
        total_times = report.prepared.get("totalTimes")
        done_times = report.prepared.get("doneTimes")
        self.emit(
            f"试卷 {name or user_activity_id}: 类型 {activity_type}, "
            f"{expected or '?'} 题, 限时 {answer_time or '?'} 分钟"
        )
        if total_times is not None and done_times is not None:
            remaining = int(total_times) - int(done_times) if int(total_times) > 0 else -1
            self.emit(f"答题次数: 总计 {total_times}, 已用 {done_times}, 剩余 {remaining if remaining >= 0 else '不限'}")
        if dry_run:
            report.elapsed_seconds = self.clock() - started_at
            return report

        paper = self.api.start_paper(activity_type, user_activity_id)
        if self.guard:
            self.guard.validate_start(paper)
        if not isinstance(paper, dict):
            raise ApiError("startPaper did not return a paper object", payload=paper)
        question_list = list(paper.get("questionList") or [])
        self.emit(f"开始答题: {expected or '?'} 题")
        current_index = int(paper.get("currentIndex") or 1)
        question_pages = (
            [(current_index, question_list)]
            if is_exam
            else self._collect_practice_question_pages(user_activity_id, paper)
        )
        seen: set[str] = set()
        legacy_delay = float(self.config.get("question_delay_seconds") or 0)
        allow_server_correct = bool(self.config.get("allow_server_correct", True))
        verbose_questions = bool(self.config.get("verbose_questions", False))
        if self.guard and self.guard.policy.reject_server_correct:
            allow_server_correct = False
        last_tick = self.clock()
        deliberate_wrong_plans: dict[str, AnswerPlan] = {}
        if activity_type == 4:
            duration_label = "4分30秒" if short_exam else "10分13秒"
            self.emit(f"正式考试完成时间: {duration_label}")
            if answer_mode in {"human", "score96", "score75"}:
                target_count = (
                    max(0, int(self.config.get("score96_wrong_count") or 4))
                    if answer_mode == "score96"
                    else max(0, int(self.config.get("score75_wrong_count") or 25))
                    if answer_mode == "score75"
                    else None
                )
                deliberate_wrong_plans = self._plan_deliberate_wrong_questions(
                    question_list,
                    allow_server_correct=allow_server_correct,
                    target_count=target_count,
                )
                report.deliberate_wrong_planned = len(deliberate_wrong_plans)
                if answer_mode in {"score96", "score75"}:
                    score_label = "96" if answer_mode == "score96" else "75"
                    self.emit(
                        f"正式考试策略: {score_label} 分模式，"
                        f"计划故意错 {report.deliberate_wrong_planned} 题"
                    )
                else:
                    self.emit(
                        f"正式考试策略: 拟人模式，计划故意错 {report.deliberate_wrong_planned} 题"
                    )
            else:
                self.emit("正式考试策略: 全对模式")

        for page_index, page_questions in question_pages:
            for question in page_questions:
                if not isinstance(question, dict):
                    continue
                question_id = question.get("id", question.get("questionId"))
                unique_key = str(question_id or _question_text(question))
                if unique_key in seen:
                    continue
                if self.guard:
                    unique_key = self.guard.validate_question(question, seen)
                seen.add(unique_key)
                try:
                    qtype = parse_type(question.get("type", question.get("questionType")))
                except ValueError:
                    qtype = 0
                plan = self.bank.resolve(question, allow_server_correct=allow_server_correct)
                deliberate_wrong = unique_key in deliberate_wrong_plans
                if deliberate_wrong:
                    plan = deliberate_wrong_plans[unique_key]
                if self.guard:
                    self.guard.validate_answer(question, plan)
                if not plan.answer_ids:
                    self.emit(f"[跳过] 题 {question_id} 没有可用选项")
                    continue
                now = self.clock()
                elapsed = now - last_tick
                delay = (
                    self.guard.next_question_delay(
                        activity_type,
                        short_exam=short_exam,
                    )
                    if self.guard
                    else legacy_delay
                )
                if delay > elapsed:
                    self.sleep(delay - elapsed)
                    now = self.clock()
                use_time = max(1, int(round(now - last_tick)))
                self.api.record_question(
                    activity_type,
                    user_activity_id,
                    question_id,
                    plan.payload_value,
                    use_time,
                    current_index=page_index,
                )
                if deliberate_wrong:
                    report.deliberate_wrong_sent += 1
                if self.guard:
                    self.guard.emit(
                        "question_recorded",
                        {
                            "question_id": question_id,
                            "question_type": qtype,
                            "answer_count": len(plan.answer_ids),
                            "match_kind": plan.match_kind,
                            "confidence": round(plan.confidence, 4),
                        },
                    )
                last_tick = now
                title = _question_text(question)
                result = QuestionResult(
                    question_id=question_id,
                    qtype=qtype,
                    title=title,
                    answer_ids=list(plan.answer_ids),
                    matched=plan.matched,
                    match_kind=plan.match_kind,
                    confidence=round(plan.confidence, 4),
                    deliberate_wrong=deliberate_wrong,
                )
                report.questions.append(result)
                if not plan.matched:
                    report.missing += 1
                if verbose_questions:
                    label = TYPE_NAMES.get(qtype, f"类型{qtype}")
                    preview = title[:40]
                    self.emit(
                        f"[{len(report.questions)}/{expected or len(seen)}] {label} "
                        f"{preview} -> {plan.payload_value} ({plan.match_kind})"
                    )

        if self.guard:
            self.guard.validate_coverage(report.missing, expected or len(report.questions))
            exam_min_seconds = (
                self.guard.minimum_exam_seconds(
                    expected or len(report.questions),
                    int(report.prepared.get("answerTime") or 0),
                    short_exam=short_exam,
                )
                if is_exam
                else 0.0
            )
        else:
            exam_min_seconds = (
                float(
                    self.config.get("exam_short_min_seconds", 270)
                    if short_exam
                    else self.config.get("exam_min_seconds", 613)
                )
                if is_exam
                else 0.0
            )
        self.emit(
            f"答题完成: {len(report.questions)}/{expected or len(report.questions)} 题, "
            f"题库未命中 {report.missing} 题"
        )
        if not is_exam and expected > 0 and len(report.questions) < expected:
            message = (
                f"practice is incomplete: {len(report.questions)}/{expected} questions"
            )
            if not bool(self.config.get("allow_partial_practice_submit", False)):
                raise ApiError(message)
            self.emit(f"警告: {message}; partial submission is enabled")
        if is_exam and exam_min_seconds > 0:
            elapsed = self.clock() - started_at
            if elapsed < exam_min_seconds:
                wait_for = exam_min_seconds - elapsed
                self.emit(f"考试流程最短用时等待 {wait_for:.1f} 秒")
                self.sleep(wait_for)

        elapsed_before_submit = self.clock() - started_at
        if self.guard and is_exam:
            self.guard.validate_submit_timing(
                elapsed_before_submit,
                expected or len(report.questions),
                int(report.prepared.get("answerTime") or 0),
                short_exam=short_exam,
            )
        report.result = self._submit_with_retry(activity_type, user_activity_id)
        report.submitted = True
        if self.guard:
            self.guard.validate_result(
                report.result,
                expected or len(report.questions),
                activity_type,
            )
        self.emit(f"交卷结果: {json.dumps(report.result, ensure_ascii=False)}")
        if is_exam:
            if bool(self.config.get("audit_submitted_answers", True)):
                report.unrecorded_answers = self._audit_submitted_answers(
                    activity_type,
                    user_activity_id,
                    report.result,
                )
                if report.unrecorded_answers:
                    raise ApiError(
                        f"服务端未记录 {report.unrecorded_answers} 道题的作答，本次成绩不可信"
                    )
        if answer_mode in {"human", "score96", "score75"}:
            self._audit_deliberate_mode(report)

        should_collect = (
            bool(self.config.get("collect_wrong_answers", True))
            if collect_wrong is None
            else collect_wrong
        )
        if should_collect:
            report.wrong_learned = self.collect_wrong(
                activity_type,
                user_activity_id,
                submit_result=report.result,
            )
            if report.wrong_learned:
                self.emit(f"错题回收: 新增 {report.wrong_learned} 条答案")

        report.elapsed_seconds = round(self.clock() - started_at, 3)
        if self.guard:
            self.guard.emit(
                "run_completed",
                {
                    "questions": len(report.questions),
                    "missing": report.missing,
                    "elapsed": report.elapsed_seconds,
                    "score": report.result.get("score") if isinstance(report.result, dict) else None,
                },
            )
        return report

    def _collect_practice_question_pages(
        self,
        user_activity_id: Any,
        paper: dict[str, Any],
    ) -> list[tuple[int, list[dict[str, Any]]]]:
        max_pages = int(self.config.get("max_pages") or 50)
        pages: dict[int, list[dict[str, Any]]] = {}

        def add_page(current: int, value: Any) -> None:
            question_list = (
                [item for item in value.get("questionList") or [] if isinstance(item, dict)]
                if isinstance(value, dict)
                else []
            )
            pages[current] = question_list

        current_index = int(paper.get("currentIndex") or 1)
        has_before = _boolish(paper.get("hasBefore"))
        has_next = _boolish(paper.get("hasNext"))
        add_page(current_index, paper)
        requests_made = 0

        while has_before:
            requests_made += 1
            if requests_made > max_pages:
                raise ApiError(f"practice pagination exceeded max_pages={max_pages}")
            near = self.api.get_near(user_activity_id, current_index, 1)
            if not isinstance(near, dict):
                break
            next_index = int(near.get("currentIndex") or 0)
            if next_index <= 0 or next_index == current_index:
                raise ApiError(f"practice previous-page index did not advance: {current_index}")
            current_index = next_index
            has_before = _boolish(near.get("hasBefore"))
            has_next = _boolish(near.get("hasNext"))
            add_page(current_index, near)

        while has_next:
            requests_made += 1
            if requests_made > max_pages:
                raise ApiError(f"practice pagination exceeded max_pages={max_pages}")
            near = self.api.get_near(user_activity_id, current_index, 2)
            if not isinstance(near, dict):
                break
            next_index = int(near.get("currentIndex") or 0)
            if next_index <= 0 or next_index == current_index:
                raise ApiError(f"practice next-page index did not advance: {current_index}")
            current_index = next_index
            has_before = _boolish(near.get("hasBefore"))
            has_next = _boolish(near.get("hasNext"))
            add_page(current_index, near)

        return [(index, pages[index]) for index in sorted(pages)]

    def _plan_deliberate_wrong_questions(
        self,
        questions: list[dict[str, Any]],
        *,
        allow_server_correct: bool,
        target_count: int | None = None,
    ) -> dict[str, AnswerPlan]:
        minimum = max(0, int(self.config.get("deliberate_wrong_min") or 0))
        maximum = max(0, int(self.config.get("deliberate_wrong_max") or 0))
        if maximum < minimum:
            raise ValueError("deliberate_wrong_max must be >= deliberate_wrong_min")
        eligible: dict[str, AnswerPlan] = {}
        for question in questions:
            if not isinstance(question, dict):
                continue
            question_id = str(question.get("id", question.get("questionId")))
            if not question_id or question_id in eligible:
                continue
            plan = self.bank.resolve(question, allow_server_correct=allow_server_correct)
            wrong_plan = self._build_deliberate_wrong_plan(question, plan)
            if wrong_plan is not None:
                eligible[question_id] = wrong_plan
        requested = (
            max(0, target_count)
            if target_count is not None
            else self.random.randint(minimum, maximum)
        )
        target = min(requested, len(eligible))
        if target < minimum:
            self.emit(
                f"拟人模式: 仅找到 {target} 道可构造错误的题目，低于计划下限 {minimum}"
            )
        if target <= 0:
            return {}
        selected = self.random.sample(list(eligible), target)
        return {question_id: eligible[question_id] for question_id in selected}

    def _build_deliberate_wrong_plan(
        self,
        question: dict[str, Any],
        correct_plan: AnswerPlan,
    ) -> AnswerPlan | None:
        options = api_option_entries(question)
        option_ids = [str(option["id"]) for option in options]
        correct_ids = [str(value) for value in correct_plan.answer_ids]
        wrong_ids = [option_id for option_id in option_ids if option_id not in correct_ids]
        try:
            qtype = parse_type(question.get("type", question.get("questionType")))
        except ValueError:
            qtype = 0

        answer_ids: list[str] = []
        if qtype in {1, 4}:
            if wrong_ids:
                answer_ids = [self.random.choice(wrong_ids)]
        elif qtype == 2:
            candidates: list[list[str]] = []
            if len(correct_ids) > 1:
                removed = self.random.choice(correct_ids)
                candidates.append([item for item in correct_ids if item != removed])
            if wrong_ids:
                candidates.append([*correct_ids, self.random.choice(wrong_ids)])
                if correct_ids:
                    replaced = self.random.choice(correct_ids)
                    candidates.append(
                        [
                            self.random.choice(wrong_ids) if item == replaced else item
                            for item in correct_ids
                        ]
                    )
            valid = [
                candidate
                for candidate in candidates
                if candidate and set(candidate) != set(correct_ids)
            ]
            if valid:
                answer_ids = self.random.choice(valid)
        elif wrong_ids:
            answer_ids = [self.random.choice(wrong_ids)]

        if not answer_ids or set(answer_ids) == set(correct_ids):
            return None
        return AnswerPlan(
            tuple(dict.fromkeys(answer_ids)),
            correct_plan.matched,
            correct_plan.confidence,
            "deliberate-wrong",
            correct_plan.bank_question,
        )

    def _audit_submitted_answers(
        self,
        activity_type: int,
        user_activity_id: Any,
        submit_result: Any,
    ) -> int:
        user_exam_id = None
        if isinstance(submit_result, dict):
            user_exam_id = submit_result.get("userExamId", submit_result.get("id"))
        if user_exam_id in {"", None}:
            history = self.api.list_history(activity_type, user_activity_id)
            if not history:
                self.emit("交卷审计: 未找到历史记录，未执行答案落库检查")
                return 0
            latest = max(
                history,
                key=lambda item: (
                    str(item.get("submitTime") or ""),
                    str(item.get("id") or item.get("userExamId") or ""),
                ),
            )
            user_exam_id = latest.get("id", latest.get("userExamId"))
        if user_exam_id in {"", None}:
            self.emit("交卷审计: 历史记录缺少试卷 ID，未执行答案落库检查")
            return 0
        wrong_questions = self.api.list_error_questions(activity_type, user_exam_id)
        unrecorded = sum(
            1
            for question in wrong_questions
            if isinstance(question, dict) and not _selected_option_ids(question)
        )
        self.emit(
            f"交卷审计: 错题 {len(wrong_questions)} 道，"
            f"其中服务端未记录选择 {unrecorded} 道"
        )
        if self.guard:
            self.guard.emit(
                "submitted_answers_audited",
                {
                    "wrong_questions": len(wrong_questions),
                    "unrecorded_answers": unrecorded,
                },
            )
        return unrecorded

    def _audit_deliberate_mode(self, report: RunReport) -> None:
        if report.deliberate_wrong_sent != report.deliberate_wrong_planned:
            raise ApiError(
                "错题策略未完整执行: "
                f"计划 {report.deliberate_wrong_planned} 题，实际发送 {report.deliberate_wrong_sent} 题"
            )
        server_errors = int(
            report.result.get("errorNum") or 0
        ) if isinstance(report.result, dict) else 0
        self.emit(
            f"错题策略审计: 计划故意错 {report.deliberate_wrong_sent} 题，"
            f"服务端实际错 {server_errors} 题"
        )
        if server_errors != report.deliberate_wrong_sent:
            raise ApiError(
                "错题策略结果不符: "
                f"计划 {report.deliberate_wrong_sent} 题，服务端实际 {server_errors} 题"
            )
        if self.guard:
            self.guard.emit(
                "human_mode_validated",
                {
                    "planned_wrong": report.deliberate_wrong_planned,
                    "sent_wrong": report.deliberate_wrong_sent,
                    "server_errors": server_errors,
                },
            )

    def _submit_with_retry(self, activity_type: int, user_activity_id: Any) -> Any:
        retries = int(self.config.get("submit_retry_count") or 0)
        retry_seconds = float(self.config.get("submit_retry_seconds") or 0)
        for attempt in range(retries + 1):
            try:
                return self.api.submit_paper(activity_type, user_activity_id)
            except ApiError as exc:
                retryable = exc.status in {1006, 1007, 429} or any(
                    word in str(exc) for word in ("时间过短", "答题时间", "频繁")
                )
                if not retryable or attempt >= retries:
                    raise
                self.emit(f"交卷暂未通过 ({exc}); {retry_seconds:.0f} 秒后重试")
                self.sleep(retry_seconds)
        raise ApiError("submit retries exhausted")

    def collect_wrong(
        self,
        activity_type: int,
        user_activity_id: Any,
        *,
        submit_result: Any = None,
    ) -> int:
        user_exam_id = None
        if isinstance(submit_result, dict):
            user_exam_id = submit_result.get("userExamId", submit_result.get("id"))
        if user_exam_id in {"", None}:
            history = self.api.list_history(activity_type, user_activity_id)
            if not history:
                return 0
            latest = max(
                history,
                key=lambda item: (
                    str(item.get("submitTime") or ""),
                    str(item.get("id") or item.get("userExamId") or ""),
                ),
            )
            user_exam_id = latest.get("id", latest.get("userExamId"))
        if user_exam_id in {"", None}:
            return 0
        wrong_questions = self.api.list_error_questions(activity_type, user_exam_id)
        learned_path = Path(str(self.config["learned_path"]))
        learned_path.parent.mkdir(parents=True, exist_ok=True)
        added: list[dict[str, Any]] = []
        for question in wrong_questions:
            title = _question_text(question)
            try:
                qtype = parse_type(question.get("type", question.get("questionType")))
            except ValueError:
                qtype = None
            existing, _, _ = self.bank.find(title, qtype)
            learned = self.bank.add_server_question(question)
            if not learned:
                continue
            if existing and normalize_text(existing.title) == normalize_text(learned.title):
                if existing.answers == learned.answers:
                    continue
            added.append(learned.to_mapping())
        if added:
            with learned_path.open("a", encoding="utf-8") as handle:
                for row in added:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return len(added)
