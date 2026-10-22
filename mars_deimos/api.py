from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any

import requests
from requests import Response, Session
from requests.utils import dict_from_cookiejar


DEFAULT_BASE_URL = "https://mars.mycourse.cn/marsapi"
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded;charset=utf8"


class MarsError(RuntimeError):
    pass


class ApiError(MarsError):
    def __init__(self, message: str, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class AuthenticationError(ApiError):
    pass


def unwrap_payload(payload: Any) -> Any:
    """Apply the frontend's Resp -> data.result unwrapping contract."""
    if not isinstance(payload, dict):
        return payload
    status = payload.get("status", payload.get("code"))
    if status is not None:
        try:
            status_number = int(status)
        except (TypeError, ValueError):
            status_number = None
        if status_number in {401, 403}:
            raise AuthenticationError(str(payload.get("message") or "authentication required"), status_number, payload)
        if status_number is not None and status_number != 200:
            raise ApiError(str(payload.get("message") or payload.get("msg") or "API error"), status_number, payload)
    data = payload.get("data")
    if isinstance(data, dict) and "result" in data:
        result = data["result"]
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
            except json.JSONDecodeError:
                return result
            return parsed
        return result
    if isinstance(data, (dict, list)):
        return data
    return payload


class ApiClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        token: str = "",
        timeout: float = 30,
        session: Session | None = None,
        user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MarsDeimos/1.0",
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or Session()
        self.token = token
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": FORM_CONTENT_TYPE,
                "User-Agent": user_agent,
                "Origin": "https://mars.mycourse.cn",
                "Referer": "https://mars.mycourse.cn/deimos/",
            }
        )
        if token:
            self.session.headers["Authorization"] = token

    def set_token(self, token: str) -> None:
        self.token = token
        if token:
            self.session.headers["Authorization"] = token
        else:
            self.session.headers.pop("Authorization", None)

    def load_cookies(self, cookies: dict[str, str] | None) -> None:
        for name, value in (cookies or {}).items():
            self.session.cookies.set(name, value)

    def export_cookies(self) -> dict[str, str]:
        return dict_from_cookiejar(self.session.cookies)

    def _raw_request(
        self,
        method: str,
        api: str,
        params: dict[str, Any] | None = None,
        *,
        binary: bool = False,
    ) -> Any:
        url = api if api.startswith("http") else f"{self.base_url}/{api.lstrip('/')}"
        request_kwargs: dict[str, Any] = {
            "timeout": self.timeout,
            "allow_redirects": True,
        }
        if method.upper() == "GET":
            request_kwargs["params"] = params or {}
        else:
            request_kwargs["data"] = params or {}
        response: Response = self.session.request(method, url, **request_kwargs)
        if binary:
            if response.status_code != 200:
                raise ApiError(f"HTTP {response.status_code} for {url}", response.status_code)
            return response.content
        try:
            payload = response.json()
        except ValueError as exc:
            snippet = response.text[:200].replace("\n", " ")
            raise ApiError(f"non-JSON response from {url}: {snippet}", response.status_code) from exc
        if response.status_code != 200:
            raise ApiError(
                str(payload.get("message") or f"HTTP {response.status_code}"),
                response.status_code,
                payload,
            )
        return unwrap_payload(payload)

    def post(self, api: str, params: dict[str, Any] | None = None) -> Any:
        return self._raw_request("POST", api, params)

    def get(self, api: str, params: dict[str, Any] | None = None) -> Any:
        return self._raw_request("GET", api, params)

    def list_tenants(self) -> list[dict[str, Any]]:
        result = self.post("/api/login/v1/listTenant")
        if not isinstance(result, list):
            raise ApiError("listTenant did not return a list", payload=result)
        return result

    def get_tenant_config(self, tenant_code: str) -> dict[str, Any]:
        result = self.post(
            "/api/login/v1/getTenantConfig",
            {"tenantCode": tenant_code},
        )
        return result if isinstance(result, dict) else {}

    @staticmethod
    def captcha_timestamp() -> int:
        return int(time.time() * 1000) + random.randint(0, 99_999)

    def get_captcha(self, timestamp: int | None = None) -> tuple[int, bytes]:
        value = timestamp or self.captcha_timestamp()
        content = self._raw_request(
            "GET",
            "/api/login/v1/getCaptcha",
            {"timestamp": value},
            binary=True,
        )
        return value, content

    def login(
        self,
        tenant_code: str,
        username: str,
        encrypted_password: str,
        captcha_code: str,
        timestamp: int,
    ) -> Any:
        result = self.post(
            "/api/login/v1/login",
            {
                "tenantCode": tenant_code,
                "username": username,
                "encryptedPassword": encrypted_password,
                "captchaCode": captcha_code,
                "timestamp": timestamp,
            },
        )
        if isinstance(result, dict):
            token = result.get("token")
            if token:
                self.set_token(str(token))
        return result

    def user_index(self) -> dict[str, Any]:
        result = self.post("/api/user/v1/index")
        return result if isinstance(result, dict) else {}

    def project_index(self) -> dict[str, Any]:
        result = self.post("/api/study/project/v1/index")
        return result if isinstance(result, dict) else {}

    def simpleorder_list(self, project_id: Any) -> list[dict[str, Any]]:
        result = self.post("/api/simpleorder/v1/list", {"projectId": project_id})
        return result if isinstance(result, list) else []

    @staticmethod
    def activity_kind(activity_type: Any) -> str:
        try:
            number = int(activity_type)
        except (TypeError, ValueError):
            number = 1
        return "exam" if number in {3, 4} else "practice"

    def prepare_paper(self, activity_type: Any, user_activity_id: Any) -> Any:
        kind = self.activity_kind(activity_type)
        return self.post(
            f"/api/study/activity/{kind}/v1/preparePaper",
            {"userActivityId": user_activity_id},
        )

    def start_paper(self, activity_type: Any, user_activity_id: Any) -> Any:
        kind = self.activity_kind(activity_type)
        return self.post(
            f"/api/study/activity/{kind}/v1/startPaper",
            {"userActivityId": user_activity_id},
        )

    def get_near(self, user_activity_id: Any, current_index: int, near_type: int) -> Any:
        return self.post(
            "/api/study/activity/practice/v1/getNear",
            {
                "userActivityId": user_activity_id,
                "currentIndex": current_index,
                "nearType": near_type,
            },
        )

    def record_question(
        self,
        activity_type: Any,
        user_activity_id: Any,
        question_id: Any,
        answer_ids: str,
        use_time: int,
        current_index: int | None = None,
    ) -> Any:
        kind = self.activity_kind(activity_type)
        params: dict[str, Any] = {
            "userActivityId": user_activity_id,
            "questionId": question_id,
            "answerIds": answer_ids,
            "useTime": max(0, int(use_time)),
        }
        if kind == "practice":
            params["currentIndex"] = current_index
        return self.post(f"/api/study/activity/{kind}/v1/recordQuestion", params)

    def submit_paper(self, activity_type: Any, user_activity_id: Any) -> Any:
        kind = self.activity_kind(activity_type)
        return self.post(
            f"/api/study/activity/{kind}/v1/submitPaper",
            {"userActivityId": user_activity_id},
        )

    def list_history(self, activity_type: Any, user_activity_id: Any) -> list[dict[str, Any]]:
        kind = self.activity_kind(activity_type)
        result = self.post(
            f"/api/study/activity/{kind}/v1/listHistory",
            {"userActivityId": user_activity_id},
        )
        return result if isinstance(result, list) else []

    def list_error_questions(self, activity_type: Any, user_exam_id: Any) -> list[dict[str, Any]]:
        kind = self.activity_kind(activity_type)
        result = self.post(
            f"/api/study/activity/{kind}/v1/listErrorQuestion.do",
            {"userExamId": user_exam_id},
        )
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and isinstance(result.get("questionList"), list):
            return result["questionList"]
        return []

    def logout(self) -> Any:
        result = self.post("/api/login/v1/logout")
        self.set_token("")
        return result


def save_session(
    path: str | Path,
    client: ApiClient,
    *,
    tenant: str,
    username: str,
    user: dict[str, Any],
) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "base_url": client.base_url,
        "tenant": tenant,
        "username": username,
        "token": client.token,
        "user": user,
        "cookies": client.export_cookies(),
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_session(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if not source.exists():
        return {}
    try:
        return json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
