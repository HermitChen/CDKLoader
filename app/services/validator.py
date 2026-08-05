from __future__ import annotations

import json
import random
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from curl_cffi import requests as cffi_requests
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Account, ValidationAttempt, utcnow
from ..security import SecurityManager


@dataclass
class ValidationResult:
    outcome: str
    error_type: str = ""
    message: str = ""
    validated_via: str = "access_token"
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    expires_at: datetime | None = None
    latency_ms: int = 0
    stage: str = ""
    status_code: int | None = None
    content_type: str = ""
    server: str = ""
    cf_mitigated: str = ""
    cf_ray: str = ""
    retry_after: float | None = None
    attempts: int = 0


class TokenValidator:
    validation_url = "https://auth.openai.com/api/accounts/oauth/userinfo"
    token_url = "https://auth.openai.com/oauth/token"
    chatgpt_base_url = "https://chatgpt.com"
    conversation_init_path = "/backend-api/conversation/init"
    account_check_path = "/backend-api/accounts/check/v4-2023-04-27"
    chatgpt_validation_impersonate = "chrome146"
    chatgpt_validation_user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    )
    chatgpt_validation_client_version = "prod-497f333866796e100096ad083b51ca949d22e751"
    chatgpt_validation_build_number = "7646290"
    access_token_expiry_skew = timedelta(seconds=60)

    _terminal_refresh_error_codes = frozenset(
        {
            "invalid_grant",
            "invalid_refresh_token",
            "login_required",
            "reauthentication_required",
            "refresh_token_expired",
            "refresh_token_invalidated",
        }
    )
    _terminal_refresh_message_fragments = (
        "invalid refresh token",
        "please log in again",
        "refresh token has expired",
        "refresh token invalidated",
        "refresh token is invalid",
        "refresh token has already been used",
        "session expired",
        "session has ended",
    )

    def __init__(self, settings: Settings, security: SecurityManager):
        self.settings = settings
        self.security = security
        self._gate_lock = threading.Lock()
        self._gate_hits: deque[float] = deque()
        self._cooldown_until = 0.0
        self._account_lock_guard = threading.Lock()
        self._account_locks: dict[str, threading.Lock] = {}
        self._validation_semaphore = threading.BoundedSemaphore(
            max(1, int(getattr(settings, "validation_concurrency", 1)))
        )

    def _session(self):
        # Keep the browser profile configurable while using the newest target
        # supported by the bundled curl_cffi release by default.
        impersonate = str(
            getattr(self.settings, "validation_impersonate", "")
            or self.chatgpt_validation_impersonate
        ).strip()
        return cffi_requests.Session(impersonate=impersonate)

    @staticmethod
    def _proxy_value(value: Any) -> str:
        if isinstance(value, dict):
            for key in ("url", "proxy_url", "proxy", "value"):
                candidate = value.get(key)
                if candidate is not None:
                    return str(candidate).strip()
            return ""
        return str(value or "").strip()

    def _account_validation_proxy(self, account: Account) -> str:
        direct_proxy = self._proxy_value(getattr(account, "proxy_used", ""))
        if direct_proxy:
            return direct_proxy

        extra_data = getattr(account, "extra_data", None)
        if isinstance(extra_data, str):
            try:
                extra_data = json.loads(extra_data or "{}")
            except (TypeError, json.JSONDecodeError):
                extra_data = {}
        if not isinstance(extra_data, dict):
            return ""

        for key in ("proxy_used", "proxy_url", "proxy"):
            proxy = self._proxy_value(extra_data.get(key))
            if proxy:
                return proxy
        return ""

    def _validation_proxy_url(
        self,
        account: Account,
        explicit_proxy: str | None = None,
    ) -> str | None:
        explicit = self._proxy_value(explicit_proxy)
        if explicit:
            return explicit

        raw_pool = getattr(self.settings, "validation_proxy_pool", "")
        if isinstance(raw_pool, str):
            candidates = raw_pool.replace(";", "\n").replace(",", "\n").splitlines()
        elif isinstance(raw_pool, (list, tuple, set)):
            candidates = list(raw_pool)
        else:
            candidates = []
        candidates = [self._proxy_value(candidate) for candidate in candidates]
        candidates = [candidate for candidate in candidates if candidate]
        configured_proxy = self._proxy_value(getattr(self.settings, "validation_proxy", ""))
        account_proxy = self._account_validation_proxy(account)
        mode = str(getattr(self.settings, "validation_egress_mode", "direct") or "direct").strip().lower()

        if mode == "direct":
            return None
        if mode == "pool":
            return random.choice(candidates) if candidates else configured_proxy or None
        if mode == "account":
            return account_proxy or configured_proxy or (random.choice(candidates) if candidates else None)

        # Unknown values fail closed to direct access instead of silently
        # inheriting a historical account proxy.
        return None

    def _proxy_mapping(self, proxy_url: str | None = None) -> dict[str, str] | None:
        proxy = self._proxy_value(proxy_url)
        if not proxy:
            return None
        return {"http": proxy, "https": proxy}

    def _request_kwargs(
        self,
        headers: dict[str, str],
        *,
        proxy_mapping: dict[str, str] | None = None,
        use_proxy: bool = True,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "headers": headers,
            "timeout": self.settings.validation_timeout_seconds,
        }
        if use_proxy and proxy_mapping:
            kwargs["proxies"] = proxy_mapping
        return kwargs

    @staticmethod
    def _close_session(session: Any) -> None:
        try:
            session.close()
        except Exception:
            pass

    @staticmethod
    def _seed_chatgpt_device_cookie(session: Any, device_id: str) -> None:
        cookies = getattr(session, "cookies", None)
        if cookies is None:
            return
        for domain in ("chatgpt.com", ".chatgpt.com"):
            try:
                cookies.set("oai-did", device_id, domain=domain, path="/")
            except Exception:
                continue

    @staticmethod
    def _response_retry_after(response: Any) -> float | None:
        headers = getattr(response, "headers", None) or {}
        try:
            value = str(headers.get("Retry-After") or "").strip()
        except Exception:
            return None
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, retry_at.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None

    def _retry_delay_seconds(self, attempt: int, retry_after: float | None = None) -> float:
        base = max(0.0, float(self.settings.validation_retry_base_seconds))
        maximum = max(base, float(self.settings.validation_retry_max_seconds))
        delay = min(maximum, base * (2 ** max(0, attempt)))
        if retry_after is not None:
            delay = max(delay, max(0.0, retry_after))
        jitter = max(0.0, float(self.settings.validation_retry_jitter_seconds))
        if delay > 0 and jitter > 0:
            delay += random.uniform(0.0, jitter)
        return delay

    def _sleep_before_retry(self, attempt: int, retry_after: float | None = None) -> None:
        delay = self._retry_delay_seconds(attempt, retry_after)
        if delay > 0:
            time.sleep(delay)

    def _validation_cooldown_remaining(self) -> float:
        now = time.monotonic()
        with self._gate_lock:
            return max(0.0, self._cooldown_until - now)

    def _record_gate_hit(self) -> None:
        now = time.monotonic()
        threshold = max(1, int(getattr(self.settings, "validation_gate_threshold", 5)))
        cooldown = max(0.0, float(getattr(self.settings, "validation_cooldown_seconds", 60.0)))
        with self._gate_lock:
            window_start = now - 60.0
            while self._gate_hits and self._gate_hits[0] < window_start:
                self._gate_hits.popleft()
            self._gate_hits.append(now)
            if len(self._gate_hits) >= threshold and cooldown > 0:
                self._cooldown_until = max(self._cooldown_until, now + cooldown)

    def _account_lock(self, account: Account) -> threading.Lock:
        account_key = str(
            getattr(account, "id", None)
            or getattr(account, "account_id", None)
            or getattr(account, "email", None)
            or "unknown"
        )
        with self._account_lock_guard:
            return self._account_locks.setdefault(account_key, threading.Lock())

    @staticmethod
    def _oauth_headers(access_token: str) -> dict[str, str]:
        return {
            "authorization": f"Bearer {access_token}",
            "accept": "application/json",
        }

    def _chatgpt_headers(
        self,
        access_token: str,
        path: str,
        *,
        device_id: str,
        session_id: str,
        content_type: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "authorization": f"Bearer {access_token}",
            "accept": "application/json",
            "content-type": content_type or "application/json",
            "origin": self.chatgpt_base_url,
            "referer": f"{self.chatgpt_base_url}/",
            "user-agent": self.chatgpt_validation_user_agent,
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "oai-device-id": device_id,
            "oai-session-id": session_id,
            "oai-language": "zh-CN",
            "oai-client-version": self.chatgpt_validation_client_version,
            "oai-client-build-number": self.chatgpt_validation_build_number,
            "x-openai-target-path": path,
            "x-openai-target-route": path,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        return headers

    @staticmethod
    def _response_metadata(response: Any) -> dict[str, Any]:
        headers = getattr(response, "headers", None) or {}
        def header(name: str) -> str:
            try:
                return str(headers.get(name) or headers.get(name.lower()) or "").strip()
            except Exception:
                return ""

        return {
            "status_code": int(getattr(response, "status_code", 0) or 0),
            "content_type": header("content-type"),
            "server": header("server"),
            "cf_mitigated": header("cf-mitigated"),
            "cf_ray": header("cf-ray"),
            "retry_after": TokenValidator._response_retry_after(response),
        }

    @staticmethod
    def _response_error_detail(response: Any) -> str:
        try:
            payload = response.json()
        except Exception:
            payload = None

        if isinstance(payload, dict):
            for key in ("detail", "message", "error_description", "description"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
                if isinstance(value, dict):
                    nested = (
                        value.get("message")
                        or value.get("detail")
                        or value.get("description")
                        or value.get("code")
                    )
                    if isinstance(nested, str) and nested.strip():
                        return nested.strip()

            error = payload.get("error")
            if isinstance(error, str) and error.strip():
                return error.strip()
            if isinstance(error, dict):
                nested = (
                    error.get("message")
                    or error.get("detail")
                    or error.get("description")
                    or error.get("code")
                )
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()

        body_text = str(getattr(response, "text", "") or "").strip()
        return body_text[:200]

    @staticmethod
    def _response_is_json_object(response: Any) -> bool:
        try:
            return isinstance(response.json(), dict)
        except Exception:
            return False

    def _probe_mode(self, probe_mode: str | None = None) -> str:
        value = str(
            probe_mode
            or getattr(self.settings, "validation_probe_mode", "fast")
            or "fast"
        ).strip().lower()
        return "strict" if value == "strict" else "fast"

    @staticmethod
    def _format_validation_http_error(status_code: int, detail: str) -> str:
        detail = str(detail or "").strip()
        if detail:
            return f"验证失败: HTTP {status_code} - {detail[:200]}"
        return f"验证失败: HTTP {status_code}"

    def _validation_response_error(self, response: Any, *, stage: str = "") -> str | None:
        status_code = int(getattr(response, "status_code", 0) or 0)
        detail = self._response_error_detail(response)
        detail_lower = detail.casefold()
        stage_suffix = f": {stage}" if stage else ""
        metadata = self._response_metadata(response)
        content_type = str(metadata["content_type"]).casefold()
        is_html = "text/html" in content_type or detail_lower.startswith("<!doctype html") or "<html" in detail_lower[:100]
        is_json = self._response_is_json_object(response)
        is_challenge = bool(
            metadata["cf_mitigated"].casefold() == "challenge"
            or "cloudflare" in detail_lower
            or "challenge" in detail_lower
            or is_html
            or (status_code in {403, 503} and metadata["server"].casefold() == "cloudflare")
        )

        # A challenge page can contain words such as "unauthorized" or
        # "invalid". Classify the response as an upstream access problem
        # before looking at token-related text so it is never persisted as a
        # token failure.
        if is_challenge:
            if metadata["cf_mitigated"].casefold() == "challenge" or "cloudflare" in detail_lower:
                reason = "Cloudflare Challenge"
            else:
                reason = "上游风控或访问限制"
            return f"Token 验证暂不可用: {stage or 'validation'}: {reason}"

        if 200 <= status_code < 300:
            if status_code == 204 or not self._response_is_json_object(response):
                return f"Token 验证暂不可用: {stage or 'validation'}: 响应格式暂时无法确认"
            return None
        if status_code == 401:
            return f"Token 无效或已过期{stage_suffix}"
        if status_code == 403:
            if any(
                keyword in detail_lower
                for keyword in ("banned", "deactivated", "suspended", "terminated", "disabled")
            ) and is_json:
                return f"账号可能被封禁{stage_suffix}"
            if any(
                keyword in detail_lower
                for keyword in ("token", "access token", "access_token", "expired", "invalid", "unauthorized")
            ) and is_json:
                return f"Token 无效或已过期{stage_suffix}"

        if status_code == 403:
            return f"Token 验证暂不可用: {stage or 'validation'}: 上游风控或访问限制"
        if status_code in {408, 425, 429} or status_code >= 500:
            if status_code == 429:
                return f"Token 验证暂不可用: {stage or 'validation'}: 上游限流"
            return f"Token 验证暂不可用: {stage or 'validation'}: HTTP {status_code}"

        return f"Token 验证暂不可用: {stage + ': ' if stage else ''}HTTP {status_code}"

    @staticmethod
    def _validation_error_result(
        error: str,
        *,
        latency_ms: int = 0,
        stage: str = "",
        response: Any | None = None,
        attempts: int = 0,
    ) -> ValidationResult:
        metadata = TokenValidator._response_metadata(response) if response is not None else {}
        if error.startswith("账号可能被封禁"):
            return ValidationResult("invalid", "banned", error, latency_ms=latency_ms, stage=stage, attempts=attempts, **metadata)
        if error.startswith("Token 无效或已过期"):
            return ValidationResult("invalid", "access_token", error, latency_ms=latency_ms, stage=stage, attempts=attempts, **metadata)
        return ValidationResult("inconclusive", "transient", error, latency_ms=latency_ms, stage=stage, attempts=attempts, **metadata)

    @staticmethod
    def _is_gate_response(response: Any) -> bool:
        metadata = TokenValidator._response_metadata(response)
        status_code = int(metadata.get("status_code") or 0)
        detail = TokenValidator._response_error_detail(response).casefold()
        content_type = str(metadata.get("content_type") or "").casefold()
        return bool(
            status_code in {403, 408, 425, 429} or status_code >= 500
            or metadata.get("cf_mitigated", "").casefold() == "challenge"
            or "challenge" in detail
            or "cloudflare" in detail
            or "text/html" in content_type
        )

    def _request_validation_stage(
        self,
        session: Any,
        access_token: str,
        *,
        stage: str,
        device_id: str,
        session_id: str,
        proxy_mapping: dict[str, str] | None,
    ) -> tuple[str, Any | None, float | None]:
        if stage == "userinfo":
            response = session.get(
                self.validation_url,
                **self._request_kwargs(
                    self._oauth_headers(access_token),
                    proxy_mapping=proxy_mapping,
                ),
            )
        else:
            path = self.conversation_init_path if stage == "conversation/init" else self.account_check_path
            url = self.chatgpt_base_url + path
            payload = None
            method = "get"
            if stage == "conversation/init":
                method = "post"
                payload = {
                    "gizmo_id": None,
                    "requested_default_model": None,
                    "conversation_id": None,
                    "timezone_offset_min": -480,
                }
            else:
                url += "?timezone_offset_min=-480"
            kwargs = self._request_kwargs(
                self._chatgpt_headers(
                    access_token,
                    path,
                    device_id=device_id,
                    session_id=session_id,
                    content_type="application/json",
                ),
                proxy_mapping=proxy_mapping,
            )
            response = session.post(url, json=payload, **kwargs) if method == "post" else session.get(url, **kwargs)

        error = self._validation_response_error(response, stage=stage)
        return ("valid" if error is None else "invalid" if error.startswith(("Token 无效", "账号可能")) else "inconclusive"), response, self._response_retry_after(response)

    def _validate_access_token(
        self,
        access_token: str,
        *,
        proxy_mapping: dict[str, str] | None = None,
        session: Any | None = None,
        probe_mode: str | None = None,
    ) -> ValidationResult:
        started = time.perf_counter()
        last_message = "Token 验证暂不可用"
        last_response: Any | None = None
        total_attempts = 0
        device_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        owns_session = session is None
        session = session or self._session()
        self._seed_chatgpt_device_cookie(session, device_id)
        stages = ["userinfo", "conversation/init"]
        if self._probe_mode(probe_mode) == "strict":
            stages.append("accounts/check")

        try:
            for stage in stages:
                for attempt in range(max(1, int(self.settings.validation_attempts))):
                    try:
                        total_attempts += 1
                        outcome, response, retry_after = self._request_validation_stage(
                            session,
                            access_token,
                            stage=stage,
                            device_id=device_id,
                            session_id=session_id,
                            proxy_mapping=proxy_mapping,
                        )
                        last_response = response
                        error = self._validation_response_error(response, stage=stage)
                        if outcome == "valid":
                            break
                        if outcome == "invalid":
                            return self._validation_error_result(
                                error or f"Token 无效或已过期: {stage}",
                                latency_ms=int((time.perf_counter() - started) * 1000),
                                stage=stage,
                                response=response,
                                attempts=total_attempts,
                            )
                        last_message = error or f"Token 验证暂不可用: {stage}"
                        if self._is_gate_response(response):
                            self._record_gate_hit()
                        if self._validation_cooldown_remaining() > 0:
                            return self._validation_error_result(
                                last_message,
                                latency_ms=int((time.perf_counter() - started) * 1000),
                                stage=stage,
                                response=response,
                                attempts=total_attempts,
                            )
                        if attempt < max(1, int(self.settings.validation_attempts)) - 1:
                            self._sleep_before_retry(attempt, retry_after)
                            continue
                        return self._validation_error_result(
                            last_message,
                            latency_ms=int((time.perf_counter() - started) * 1000),
                            stage=stage,
                            response=response,
                            attempts=total_attempts,
                        )
                    except Exception:
                        last_message = f"Token 验证暂不可用: {stage}: 网络或 TLS 错误"
                        if attempt < max(1, int(self.settings.validation_attempts)) - 1:
                            self._sleep_before_retry(attempt)
                            continue
                        return ValidationResult(
                            "inconclusive",
                            "transient",
                            last_message,
                            latency_ms=int((time.perf_counter() - started) * 1000),
                            stage=stage,
                            attempts=total_attempts,
                        )
            metadata = self._response_metadata(last_response) if last_response is not None else {}
            return ValidationResult(
                "valid",
                latency_ms=int((time.perf_counter() - started) * 1000),
                stage=stages[-1],
                attempts=total_attempts,
                **metadata,
            )
        finally:
            if owns_session:
                self._close_session(session)

    def _cooldown_result(self, remaining: float) -> ValidationResult:
        return ValidationResult(
            "inconclusive",
            "transient",
            f"Token 验证暂不可用: 上游验证处于冷却期，请约 {max(1, int(remaining + 0.999))} 秒后重试",
            stage="cooldown",
        )

    @classmethod
    def _is_terminal_refresh_failure(cls, response: Any) -> bool:
        try:
            payload = response.json()
        except Exception:
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        error = payload.get("error")
        if isinstance(error, dict):
            error_code = str(error.get("code") or error.get("type") or "").strip().casefold()
            nested_description = str(
                error.get("message") or error.get("description") or error.get("detail") or ""
            ).strip()
        else:
            error_code = str(error or "").strip().casefold()
            nested_description = ""
        description = str(
            payload.get("error_description")
            or payload.get("message")
            or nested_description
            or getattr(response, "text", "")
            or ""
        ).strip().casefold()
        return error_code in cls._terminal_refresh_error_codes or any(
            fragment in description for fragment in cls._terminal_refresh_message_fragments
        )

    def _refresh(self, refresh_token: str, client_id: str | None) -> ValidationResult:
        started = time.perf_counter()
        session = self._session()
        try:
            payload: dict[str, Any] = {
                "client_id": client_id or self.settings.oauth_client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
            if self.settings.oauth_redirect_uri:
                payload["redirect_uri"] = self.settings.oauth_redirect_uri
            response = session.post(
                self.token_url,
                data=payload,
                **self._request_kwargs(
                    {"content-type": "application/x-www-form-urlencoded", "accept": "application/json"},
                    use_proxy=False,
                ),
            )
            elapsed = int((time.perf_counter() - started) * 1000)
            if response.status_code != 200:
                if self._is_terminal_refresh_failure(response):
                    return ValidationResult("invalid", "refresh_token", "Refresh Token 无效或已过期", "refresh_token", latency_ms=elapsed)
                return ValidationResult("inconclusive", "transient", f"刷新服务暂不可用: HTTP {response.status_code}", "refresh_token", latency_ms=elapsed)
            try:
                data = response.json()
            except Exception:
                return ValidationResult("inconclusive", "transient", "刷新响应无效，暂时无法确认", "refresh_token", latency_ms=elapsed)
            if not isinstance(data, dict):
                return ValidationResult("inconclusive", "transient", "刷新响应无效，暂时无法确认", "refresh_token", latency_ms=elapsed)
            access_token = str(data.get("access_token") or "")
            if not access_token:
                return ValidationResult("inconclusive", "transient", "刷新响应缺少 Access Token，暂时无法确认", "refresh_token", latency_ms=elapsed)
            try:
                expires_in = int(data.get("expires_in") or 3600)
            except (TypeError, ValueError):
                expires_in = 3600
            return ValidationResult(
                "valid",
                validated_via="refresh_token",
                access_token=access_token,
                refresh_token=str(data.get("refresh_token") or refresh_token),
                id_token=str(data.get("id_token") or ""),
                expires_at=utcnow() + timedelta(seconds=expires_in),
                latency_ms=elapsed,
            )
        except Exception:
            return ValidationResult(
                "inconclusive",
                "transient",
                "刷新 Token 时网络或 TLS 错误",
                "refresh_token",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        finally:
            self._close_session(session)

    def _access_token_needs_refresh(self, account: Account) -> bool:
        return bool(account.expires_at and account.expires_at <= utcnow() + self.access_token_expiry_skew)

    def _confirm_refreshed_access(
        self,
        refresh_result: ValidationResult,
        *,
        proxy_mapping: dict[str, str] | None = None,
        probe_mode: str | None = None,
    ) -> ValidationResult:
        confirmation = self._validate_access_token(
            refresh_result.access_token,
            proxy_mapping=proxy_mapping,
            probe_mode=probe_mode,
        )
        confirmation.validated_via = "refresh_token"
        confirmation.access_token = refresh_result.access_token
        confirmation.refresh_token = refresh_result.refresh_token
        confirmation.id_token = refresh_result.id_token
        confirmation.expires_at = refresh_result.expires_at
        if confirmation.outcome == "invalid":
            # A newly issued token rejected by the product is not proof that its RT is terminal.
            confirmation.outcome = "inconclusive"
            confirmation.error_type = "transient"
            confirmation.message = "刷新后的 Access Token 仍被实际接口拒绝，暂时无法确认"
        return confirmation

    def _validate_unlocked(
        self,
        account: Account,
        *,
        validation_proxy: str | None = None,
        probe_mode: str | None = None,
    ) -> ValidationResult:
        access_token = self.security.decrypt(account.access_token_encrypted)
        refresh_token = self.security.decrypt(account.refresh_token_encrypted)
        if self.settings.validation_mode == "structural":
            if access_token or refresh_token:
                return ValidationResult("valid", "", "结构校验通过", "structural")
            return ValidationResult("invalid", "credentials", "缺少可验证凭据", "structural")

        # Select one validation exit for the whole operation. Refresh-token
        # requests intentionally remain direct; only token validation uses it.
        proxy_mapping = self._proxy_mapping(
            self._validation_proxy_url(account, validation_proxy)
        )

        if refresh_token and (not access_token or self._access_token_needs_refresh(account)):
            refresh_result = self._refresh(refresh_token, account.client_id)
            if refresh_result.outcome != "valid":
                return refresh_result
            return self._confirm_refreshed_access(
                refresh_result,
                proxy_mapping=proxy_mapping,
                probe_mode=probe_mode,
            )

        if access_token:
            access_result = self._validate_access_token(
                access_token,
                proxy_mapping=proxy_mapping,
                probe_mode=probe_mode,
            )
            if access_result.outcome == "valid" or access_result.outcome == "inconclusive":
                return access_result
            if not refresh_token:
                return access_result
        elif not refresh_token:
            return ValidationResult("invalid", "credentials", "缺少可验证凭据")

        refresh_result = self._refresh(refresh_token, account.client_id)
        if refresh_result.outcome != "valid":
            return refresh_result
        return self._confirm_refreshed_access(
            refresh_result,
            proxy_mapping=proxy_mapping,
            probe_mode=probe_mode,
        )

    def validate(
        self,
        account: Account,
        *,
        validation_proxy: str | None = None,
        probe_mode: str | None = None,
    ) -> ValidationResult:
        account_lock = self._account_lock(account)
        account_lock.acquire()
        try:
            if self.settings.validation_mode == "structural":
                return self._validate_unlocked(
                    account,
                    validation_proxy=validation_proxy,
                    probe_mode=probe_mode,
                )

            cooldown = self._validation_cooldown_remaining()
            if cooldown > 0:
                return self._cooldown_result(cooldown)

            self._validation_semaphore.acquire()
            try:
                # Another validation may have tripped the gate while this
                # operation was waiting for a concurrency slot.
                cooldown = self._validation_cooldown_remaining()
                if cooldown > 0:
                    return self._cooldown_result(cooldown)
                return self._validate_unlocked(
                    account,
                    validation_proxy=validation_proxy,
                    probe_mode=probe_mode,
                )
            finally:
                self._validation_semaphore.release()
        finally:
            account_lock.release()


def apply_validation(
    session: Session,
    account: Account,
    validator: TokenValidator,
    *,
    preserve_reservation: bool = False,
    validation_proxy: str | None = None,
    probe_mode: str | None = None,
) -> ValidationResult:
    result = validator.validate(
        account,
        validation_proxy=validation_proxy,
        probe_mode=probe_mode,
    )
    persist_validation_result(session, account, validator, result, preserve_reservation=preserve_reservation)
    return result


def validation_result_details(result: ValidationResult) -> dict[str, Any]:
    """Return safe diagnostic fields for operation logs; never include credentials."""
    return {
        "error_type": result.error_type,
        "validated_via": result.validated_via,
        "latency_ms": result.latency_ms,
        "stage": result.stage,
        "status_code": result.status_code,
        "content_type": result.content_type,
        "server": result.server,
        "cf_mitigated": result.cf_mitigated,
        "cf_ray": result.cf_ray,
        "retry_after": result.retry_after,
        "attempts": result.attempts,
    }


def persist_validation_result(
    session: Session,
    account: Account,
    validator: TokenValidator,
    result: ValidationResult,
    *,
    preserve_reservation: bool = False,
) -> None:
    account.validated_at = utcnow()
    refreshed_credentials = result.validated_via == "refresh_token" and bool(result.access_token)
    if result.outcome == "valid" or refreshed_credentials:
        if result.access_token:
            account.access_token_encrypted = validator.security.encrypt(result.access_token)
        if result.refresh_token:
            account.refresh_token_encrypted = validator.security.encrypt(result.refresh_token)
        if result.id_token:
            account.id_token_encrypted = validator.security.encrypt(result.id_token)
        if result.expires_at:
            account.expires_at = result.expires_at
        if refreshed_credentials:
            account.last_refresh = utcnow()
            account.version += 1
    if result.outcome == "valid":
        if not preserve_reservation or account.status != "reserved":
            account.status = "available"
    elif result.outcome == "invalid":
        account.status = "banned" if result.error_type == "banned" else "expired"
    else:
        account.status = "quarantined"

    if result.outcome != "valid":
        account.reserved_by = None
        account.reserved_until = None

    session.add(
        ValidationAttempt(
            account_id=account.id,
            outcome=result.outcome,
            error_type=result.error_type,
            message=result.message[:500],
            latency_ms=result.latency_ms,
            validated_via=result.validated_via,
        )
    )
