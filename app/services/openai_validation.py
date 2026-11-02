"""Account checks ported from codex-manager's core/openai/token_refresh.py.

The request order, headers, browser selection and retry policy follow that
implementation. See THIRD_PARTY_NOTICES.md for its MIT license.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from typing import Any

from curl_cffi import requests as cffi_requests
from curl_cffi.requests.impersonate import resolve_latest_browser_type

from .token_artifacts import normalize_token_artifacts


TOKEN_VALIDATION_MAX_ATTEMPTS = 3
TOKEN_VALIDATION_RETRY_DELAYS_SECONDS = (0.25, 0.75)
CHATGPT_CLIENT_VERSION = "26.1022.91109"
PLATFORM_OAUTH_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"
CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_OAUTH_REDIRECT_URI = "http://localhost:1455/auth/callback"


@lru_cache(maxsize=1)
def latest_chrome_impersonate() -> str:
    return str(resolve_latest_browser_type("chrome"))


@lru_cache(maxsize=1)
def latest_chrome_user_agent() -> str:
    match = re.fullmatch(r"chrome(?P<major>\d+)[a-z_]*", latest_chrome_impersonate())
    if match is None:
        raise RuntimeError("curl_cffi did not provide a Chrome impersonation")
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{match.group('major')}.0.0.0 Safari/537.36"
    )


CHATGPT_VALIDATION_IMPERSONATE = latest_chrome_impersonate()
CHATGPT_VALIDATION_USER_AGENT = latest_chrome_user_agent()


def classify_token_error(message: str | None, *, default: str = "") -> str:
    text = str(message or "").lower()
    if any(marker in text for marker in (
        "验证暂不可用", "cloudflare", "cf-mitigated", "challenge", "http 408",
        "http 425", "http 429", "timeout", "timed out", "connection reset",
        "connection aborted", "temporarily unavailable", "temporary failure", "upstream timeout",
    )) or re.search(r"\bhttp\s+5\d\d\b", text):
        return "transient"
    if any(marker in text for marker in (
        "connect tunnel failed", "proxy connect aborted", "proxy authentication",
        "proxy-authenticate", "407 proxy", "tunnel failed", "curl: (56)", "socks", "代理",
    )):
        return "proxy"
    if any(marker in text for marker in (
        "callback", "state mismatch", "missing ?code", "missing ?state", "code_verifier",
        "oauth error:", "localhost oauth callback",
    )):
        return "callback"
    if any(marker in text for marker in (
        "workspace", "consent", "organization", "跟随重定向链失败",
        "选择 workspace 失败", "获取 workspace id 失败",
    )):
        return "workspace"
    if any(marker in text for marker in (
        "refresh_token", "refresh token", "token_expired", "invalid_grant",
        "一次性令牌", "could not validate your token", "重新登录账号",
    )):
        return "refresh_token"
    if any(marker in text for marker in (
        "access_token", "token 无效或已过期", "缺少 access_token", "未返回 access_token",
        "账号没有 access_token", "invalid_token", "unauthorized",
    )):
        return "access_token"
    return default


def is_inconclusive_error(message: str | None, *, error_type: str = "") -> bool:
    classified = error_type or classify_token_error(message)
    if classified in {"proxy", "callback", "workspace", "transient"}:
        return True
    if classified in {"access_token", "refresh_token"}:
        return False
    return not any(marker in str(message or "").lower() for marker in (
        "banned", "deactivated", "suspended", "terminated", "disabled", "封禁",
    ))


def redact_credentials(message: str, *credentials: str) -> str:
    for credential in sorted(filter(None, credentials), key=len, reverse=True):
        message = message.replace(credential, "[redacted]")
    return message


@dataclass
class TokenRefreshResult:
    success: bool = False
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    session_token: str = ""
    account_id: str = ""
    workspace_id: str = ""
    metadata: dict[str, Any] | None = None
    expires_at: datetime | None = None
    error_message: str = ""
    error_type: str = ""


class TokenRefreshManager:
    VALIDATION_URL = "https://auth.openai.com/api/accounts/oauth/userinfo"
    TOKEN_URL = "https://auth.openai.com/oauth/token"
    CHATGPT_CONVERSATION_INIT_URL = "https://chatgpt.com/backend-api/conversation/init"
    CHATGPT_ACCOUNT_CHECK_URL = (
        "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
        "?timezone_offset_min=-480"
    )

    def __init__(
        self,
        proxy_url: str | None = None,
        *,
        client_id: str = CODEX_OAUTH_CLIENT_ID,
        redirect_uri: str = CODEX_OAUTH_REDIRECT_URI,
    ):
        self.proxy_url = proxy_url
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.validation_details: dict[str, Any] = {}
        self.sensitive_credentials: tuple[str, ...] = ()

    def _create_session(self):
        return cffi_requests.Session(impersonate=CHATGPT_VALIDATION_IMPERSONATE)

    def _proxy_mapping(self) -> dict[str, str] | None:
        proxy = str(self.proxy_url or "").strip()
        return {"http": proxy, "https": proxy} if proxy else None

    @staticmethod
    def _close_session(session: Any) -> None:
        try:
            session.close()
        except Exception:
            pass

    @staticmethod
    def _seed_chatgpt_device_cookie(session: Any, device_id: str) -> None:
        cookies = getattr(session, "cookies", None)
        if cookies is not None:
            for domain in ("chatgpt.com", ".chatgpt.com"):
                try:
                    cookies.set("oai-did", device_id, domain=domain, path="/")
                except Exception:
                    continue

    def _response_error_detail(self, response: Any) -> str:
        try:
            body = response.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            for key in ("detail", "message", "error_description", "description", "error"):
                value = body.get(key)
                if isinstance(value, str) and value.strip():
                    return redact_credentials(value.strip(), *self.sensitive_credentials)
                if isinstance(value, dict):
                    nested = value.get("message") or value.get("detail") or value.get("description") or value.get("code")
                    if isinstance(nested, str) and nested.strip():
                        return redact_credentials(nested.strip(), *self.sensitive_credentials)
        return redact_credentials(str(getattr(response, "text", "") or "").strip(), *self.sensitive_credentials)[:200]

    def _validation_response_error(self, response: Any, *, stage: str = "") -> str | None:
        status = int(getattr(response, "status_code", 0) or 0)
        detail = self._response_error_detail(response)
        lower = detail.lower()
        suffix = f": {stage}" if stage else ""
        if status == 200:
            return None
        if status == 401:
            return f"Token 无效或已过期{suffix}"
        if status == 403:
            if any(word in lower for word in ("banned", "deactivated", "suspended", "terminated", "disabled")):
                return f"账号可能被封禁{suffix}"
            if any(word in lower for word in ("token", "access token", "access_token", "expired", "invalid", "unauthorized")):
                return f"Token 无效或已过期{suffix}"
        error = f"Token 验证暂不可用{suffix}: HTTP {status}"
        return f"{error} - {detail[:200]}" if detail else error

    def _request(self, session: Any, method: str, url: str, *, stage: str, headers: dict[str, str], **kwargs: Any):
        # Diagnostics are added without changing the source request or retry policy.
        attempts = int(self.validation_details.get("attempts", 0)) + 1
        self.validation_details = {"stage": stage, "attempts": attempts}
        proxy_mapping = self._proxy_mapping()
        if proxy_mapping:
            kwargs["proxies"] = proxy_mapping
        response = getattr(session, method)(url, headers=headers, timeout=30, **kwargs)
        response_headers = {
            str(name).lower(): value
            for name, value in (getattr(response, "headers", None) or {}).items()
        }

        def header(name: str) -> str:
            return redact_credentials(
                str(response_headers.get(name.lower()) or "").strip(), *self.sensitive_credentials,
            )

        retry_after = None
        raw_retry_after = header("Retry-After")
        if raw_retry_after:
            try:
                retry_after = max(0.0, float(raw_retry_after))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(raw_retry_after)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    retry_after = max(0.0, retry_at.timestamp() - time.time())
                except (TypeError, ValueError, OverflowError):
                    pass
        self.validation_details.update(
            status_code=int(response.status_code), content_type=header("content-type"),
            server=header("server"), cf_mitigated=header("cf-mitigated"),
            cf_ray=header("cf-ray"), retry_after=retry_after,
        )
        return response

    @staticmethod
    def _oauth_headers(access_token: str) -> dict[str, str]:
        return {"authorization": f"Bearer {access_token}", "accept": "application/json"}

    @staticmethod
    def _chatgpt_headers(access_token: str, path: str, device_id: str, session_id: str) -> dict[str, str]:
        return {
            "authorization": f"Bearer {access_token}",
            "accept": "application/json",
            "content-type": "application/json",
            "origin": "https://chatgpt.com",
            "referer": "https://chatgpt.com/",
            "user-agent": CHATGPT_VALIDATION_USER_AGENT,
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "oai-device-id": device_id,
            "oai-session-id": session_id,
            "oai-language": "zh-CN",
            "oai-client-version": CHATGPT_CLIENT_VERSION,
            "x-openai-target-path": path,
            "x-openai-target-route": path,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }

    def _validate_chatgpt_access(self, session: Any, access_token: str, device_id: str, session_id: str) -> str | None:
        path = "/backend-api/conversation/init"
        response = self._request(
            session, "post", self.CHATGPT_CONVERSATION_INIT_URL, stage="conversation/init",
            headers=self._chatgpt_headers(access_token, path, device_id, session_id),
            json={"gizmo_id": None, "requested_default_model": None, "conversation_id": None, "timezone_offset_min": -480},
        )
        error = self._validation_response_error(response, stage="ChatGPT 会话初始化")
        if error:
            return error
        path = "/backend-api/accounts/check/v4-2023-04-27"
        response = self._request(
            session, "get", self.CHATGPT_ACCOUNT_CHECK_URL, stage="accounts/check",
            headers=self._chatgpt_headers(access_token, path, device_id, session_id),
        )
        return self._validation_response_error(response, stage="ChatGPT 账号检查")

    def validate_token(self, access_token: str) -> tuple[bool, str | None]:
        last_error = "Token 验证暂不可用"
        self.sensitive_credentials += (access_token,)
        self.validation_details = {}
        device_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        for attempt in range(TOKEN_VALIDATION_MAX_ATTEMPTS):
            session = None
            try:
                session = self._create_session()
                self._seed_chatgpt_device_cookie(session, device_id)
                response = self._request(
                    session, "get", self.VALIDATION_URL, stage="userinfo",
                    headers=self._oauth_headers(access_token),
                )
                error = self._validation_response_error(response)
                if error is None:
                    error = self._validate_chatgpt_access(session, access_token, device_id, session_id)
                if error is None:
                    return True, None
                if not is_inconclusive_error(error):
                    return False, redact_credentials(error, *self.sensitive_credentials)
                last_error = error
            except Exception as exc:
                last_error = f"Token 验证暂不可用: {exc}"
            finally:
                if session is not None:
                    self._close_session(session)
            if attempt < TOKEN_VALIDATION_MAX_ATTEMPTS - 1:
                time.sleep(TOKEN_VALIDATION_RETRY_DELAYS_SECONDS[min(attempt, 1)])
        return False, redact_credentials(last_error, *self.sensitive_credentials)

    def _parse_oauth_error(self, response: Any) -> str:
        message = ""
        try:
            body = response.json()
            error = body.get("error") if isinstance(body, dict) else None
            if isinstance(error, dict):
                message = str(error.get("message") or "").strip()
            elif isinstance(body, dict):
                message = str(body.get("error_description") or body.get("message") or "").strip()
        except Exception:
            pass
        message = redact_credentials(message, *self.sensitive_credentials)
        if "refresh token has already been used" in message.lower():
            return "OAuth refresh_token 已失效（一次性令牌已被使用），请重新登录账号"
        if response.status_code == 401:
            return f"OAuth token 刷新失败: {message}" if message else "OAuth token 刷新失败: refresh_token 无效或已过期，请重新登录账号"
        if message:
            return f"OAuth token 刷新失败: {message}"
        text = redact_credentials(str(getattr(response, "text", "") or "").strip(), *self.sensitive_credentials)
        error = f"OAuth token 刷新失败: HTTP {response.status_code}"
        return f"{error}, 响应: {text[:200]}" if text else error

    def refresh_by_oauth_token(self, refresh_token: str, client_id: str | None = None, redirect_uri: str | None = None) -> TokenRefreshResult:
        session = None
        self.sensitive_credentials += (refresh_token,)
        try:
            session = self._create_session()
            client_id = client_id or self.client_id
            if redirect_uri is None:
                redirect_uri = "" if client_id == PLATFORM_OAUTH_CLIENT_ID else self.redirect_uri
            payload = {"client_id": client_id, "grant_type": "refresh_token", "refresh_token": refresh_token}
            if redirect_uri:
                payload["redirect_uri"] = redirect_uri
            response = self._request(
                session, "post", self.TOKEN_URL, stage="refresh_token",
                headers={"content-type": "application/x-www-form-urlencoded", "accept": "application/json"},
                data=payload,
            )
            if response.status_code != 200:
                error = self._parse_oauth_error(response)
                return TokenRefreshResult(error_message=error, error_type=classify_token_error(error, default="refresh_token"))
            data = response.json()
            access_token = data.get("access_token")
            if not access_token:
                return TokenRefreshResult(error_message="OAuth token 刷新失败: 未找到 access_token", error_type="refresh_token")
            expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=data.get("expires_in", 3600))
            normalized = normalize_token_artifacts(
                access_token=access_token, refresh_token=data.get("refresh_token", refresh_token),
                id_token=data.get("id_token", ""), expires_at=expires_at,
            )
            self.sensitive_credentials += (
                normalized["access_token"], normalized["refresh_token"], normalized["id_token"],
            )
            return TokenRefreshResult(
                success=True, access_token=normalized["access_token"],
                refresh_token=normalized["refresh_token"], id_token=normalized["id_token"],
                session_token=normalized["session_token"], account_id=normalized["account_id"],
                workspace_id=normalized["workspace_id"], metadata=normalized["extra_data"],
                expires_at=expires_at,
            )
        except Exception as exc:
            error = redact_credentials(f"OAuth token 刷新异常: {exc}", *self.sensitive_credentials)
            return TokenRefreshResult(error_message=error, error_type=classify_token_error(error, default="refresh_token"))
        finally:
            if session is not None:
                self._close_session(session)
