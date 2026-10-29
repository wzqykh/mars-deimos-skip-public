from __future__ import annotations

import base64
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from mars_deimos.api import ApiError
from mars_deimos.cli import (
    _apply_overrides,
    _cleanup_captcha_artifacts,
    _resolve_exam_duration,
    _logout_and_archive_session,
    _resolve_answer_mode,
    _runnable_auto_activities,
    _should_auto_logout,
    _write_captcha_artifacts,
)
from mars_deimos.config import DEFAULTS


class CaptchaArtifactTests(unittest.TestCase):
    def test_formal_exam_duration_selection(self) -> None:
        args = type(
            "Args",
            (),
            {"exam_duration": "short", "non_interactive": False},
        )()
        self.assertEqual(
            _resolve_exam_duration({"formal_exam_duration": "ask"}, args, 4),
            "short",
        )
        self.assertEqual(
            _resolve_exam_duration({"formal_exam_duration": "ask"}, args, 3),
            "standard",
        )
        self.assertEqual(DEFAULTS["exam_short_min_seconds"], 270)

    def test_profile_isolates_session_lock_audit_and_learned_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = {
                "base_url": "https://example.test",
                "tenant": "",
                "username": "",
                "password": "",
                "bank_path": str(base / "questions.jsonl"),
                "state_dir": str(base / ".state"),
                "audit_path": str(base / ".state" / "audit.jsonl"),
                "lock_path": str(base / ".state" / "run.lock"),
                "learned_path": str(base / "learned.jsonl"),
            }
            args = SimpleNamespace(
                base_url=None,
                tenant=None,
                username=None,
                password=None,
                bank_path=None,
                bank=None,
                profile="account-a",
            )
            _apply_overrides(config, args)
            profile_dir = base / ".state" / "profiles" / "account-a"
            self.assertEqual(Path(config["state_dir"]), profile_dir)
            self.assertEqual(Path(config["audit_path"]), profile_dir / "audit.jsonl")
            self.assertEqual(Path(config["lock_path"]), profile_dir / "run.lock")
            self.assertEqual(Path(config["learned_path"]), profile_dir / "learned.jsonl")

    def test_profile_can_come_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = {
                "base_url": "https://example.test",
                "tenant": "",
                "username": "",
                "password": "",
                "bank_path": str(Path(directory) / "questions.jsonl"),
                "state_dir": str(Path(directory) / ".state"),
                "audit_path": str(Path(directory) / "audit.jsonl"),
                "lock_path": str(Path(directory) / "run.lock"),
                "learned_path": str(Path(directory) / "learned.jsonl"),
            }
            args = SimpleNamespace(
                base_url=None,
                tenant=None,
                username=None,
                password=None,
                bank_path=None,
                bank=None,
                profile=None,
            )
            previous = os.environ.get("MARS_PROFILE")
            os.environ["MARS_PROFILE"] = "account-b"
            try:
                _apply_overrides(config, args)
            finally:
                if previous is None:
                    os.environ.pop("MARS_PROFILE", None)
                else:
                    os.environ["MARS_PROFILE"] = previous
            self.assertEqual(config["profile"], "account-b")
            self.assertEqual(Path(config["state_dir"]).parts[-2:], ("profiles", "account-b"))

    def test_formal_exam_answer_mode_selection(self) -> None:
        args = type("Args", (), {"answer_mode": "human", "non_interactive": False})()
        self.assertEqual(
            _resolve_answer_mode({"formal_exam_mode": "ask"}, args, 4),
            "human",
        )
        self.assertEqual(
            _resolve_answer_mode({"formal_exam_mode": "ask"}, args, 3),
            "perfect",
        )
        self.assertEqual(DEFAULTS["deliberate_wrong_min"], 8)
        self.assertEqual(DEFAULTS["deliberate_wrong_max"], 11)
        self.assertEqual(DEFAULTS["score96_wrong_count"], 4)
        self.assertEqual(DEFAULTS["score75_wrong_count"], 25)
        score96_args = type(
            "Args",
            (),
            {"answer_mode": "score96", "non_interactive": False},
        )()
        self.assertEqual(
            _resolve_answer_mode({"formal_exam_mode": "ask"}, score96_args, 4),
            "score96",
        )
        score75_args = type(
            "Args",
            (),
            {"answer_mode": "score75", "non_interactive": False},
        )()
        self.assertEqual(
            _resolve_answer_mode({"formal_exam_mode": "ask"}, score75_args, 4),
            "score75",
        )

    def test_cleanup_removes_only_per_attempt_captcha_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            generated = [
                state_dir / "captcha-123-1.png",
                state_dir / "captcha-123-1.html",
                state_dir / "captcha-456-2.png",
            ]
            preserved = [
                state_dir / "captcha.png",
                state_dir / "probe-captcha.png",
                state_dir / "session.json",
            ]
            for path in [*generated, *preserved]:
                path.write_text("test", encoding="utf-8")
            removed = _cleanup_captcha_artifacts(state_dir)
            self.assertEqual(removed, 3)
            self.assertFalse(any(path.exists() for path in generated))
            self.assertTrue(all(path.exists() for path in preserved))

    def test_viewer_embeds_image_without_remote_url(self) -> None:
        image = b"\x89PNG\r\n\x1a\nlocal-captcha"
        with tempfile.TemporaryDirectory() as directory:
            png_path, html_path = _write_captcha_artifacts(
                Path(directory),
                123456,
                1,
                image,
            )
            png = png_path.read_bytes()
            html = html_path.read_text(encoding="utf-8")
        self.assertEqual(png, image)
        self.assertIn(base64.b64encode(image).decode("ascii"), html)
        self.assertIn("data:image/png;base64,", html)
        self.assertNotIn("marsapi", html)
        self.assertNotIn("https://", html)

    def test_auto_activities_include_formal_exam(self) -> None:
        activities = [
            {"activityType": 1, "name": "顺序练习"},
            {"activityType": 2, "name": "随机练习"},
            {"activityType": 3, "name": "模拟考试"},
            {"activityType": 4, "name": "正式考试"},
        ]
        selected = _runnable_auto_activities(activities)
        self.assertEqual([item["activityType"] for item in selected], [1, 2, 3, 4])

    def test_auto_logout_only_after_submitted_exam(self) -> None:
        config = {"logout_after_exam": True}
        self.assertTrue(_should_auto_logout(config, 4, True))
        self.assertTrue(_should_auto_logout(config, 3, True))
        self.assertFalse(_should_auto_logout(config, 1, True))
        self.assertFalse(_should_auto_logout(config, 4, False))
        self.assertFalse(_should_auto_logout({"logout_after_exam": False}, 4, True))

    def test_logout_archives_session_even_when_server_logout_fails(self) -> None:
        class FailingClient:
            def logout(self) -> None:
                raise ApiError("offline")

        with tempfile.TemporaryDirectory() as directory:
            config = {"state_dir": directory}
            session_path = Path(directory) / "session.json"
            session_path.write_text('{"token": "test"}', encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                changed = _logout_and_archive_session(config, FailingClient())
            archives = list(Path(directory).glob("session.logged-out-*.json"))
            self.assertTrue(changed)
            self.assertFalse(session_path.exists())
            self.assertEqual(len(archives), 1)


if __name__ == "__main__":
    unittest.main()
