from __future__ import annotations

import unittest

from mars_deimos.api import ApiClient, ApiError, AuthenticationError, unwrap_payload


class ApiTests(unittest.TestCase):
    def test_unwrap_result(self) -> None:
        payload = {
            "status": 200,
            "success": True,
            "data": {"result": '{"projectId":1,"name":"demo"}'},
        }
        self.assertEqual(unwrap_payload(payload), {"projectId": 1, "name": "demo"})

    def test_unauthorized_is_exception(self) -> None:
        with self.assertRaises(AuthenticationError) as context:
            unwrap_payload(
                {
                    "message": "未登录",
                    "status": 401,
                    "data": {},
                    "code": 401,
                    "success": False,
                }
            )
        self.assertEqual(context.exception.status, 401)

    def test_business_error(self) -> None:
        with self.assertRaises(ApiError):
            unwrap_payload({"status": 111, "message": "业务拒绝", "data": {}})

    def test_record_question_exam_omits_current_index(self) -> None:
        captured: list[tuple[str, dict[str, object]]] = []
        client = ApiClient()
        client.post = lambda api, params=None: captured.append((api, dict(params or {})))  # type: ignore[method-assign]
        client.record_question(3, 88, 99, "1", 2, current_index=7)
        client.record_question(1, 88, 99, "1", 2, current_index=7)
        self.assertNotIn("currentIndex", captured[0][1])
        self.assertEqual(captured[1][1]["currentIndex"], 7)
        self.assertIn("/exam/", captured[0][0])
        self.assertIn("/practice/", captured[1][0])


if __name__ == "__main__":
    unittest.main()
