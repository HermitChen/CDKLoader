from __future__ import annotations

import random
import time
import uuid
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


class TokenValidator:
    validation_url = "https://auth.openai.com/api/accounts/oauth/userinfo"
    token_url = "https://auth.openai.com/oauth/token"
    chatgpt_base_url = "https://chatgpt.com"
    conversation_init_path = "/backend-api/conversation/init"
    account_check_path = "/backend-api/accounts/check/v4-2023-04-27"
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

    _shared_gate_statuses = frozenset({403, 429})

    def __init__(self, settings: Settings, security: SecurityManager):
        self.settings = settings
        self.security = security

    def _session(self):
        kwargs: dict[str, Any] = {"impersonate": "chrome120"}
        proxy = str(self.settings.validation_proxy or "").strip()
        if proxy:
            kwargs["proxy"] = proxy
        return cffi_requests.Session(**kwargs)

    @staticmethod
    def _close_session(session: Any) -> None:
        try:
            session.close()
        except Exception:
            pass

    @staticmethod
    def _response_json_object(response: Any) -> bool:
        try:
            return isinstance(response.json(), dict)
        except Exception:
            return False

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

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code in {403, 408, 425, 429} or status_code >= 500

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

    def _validate_oauth_identity(self, access_token: str) -> ValidationResult:
        started = time.perf_counter()
        last_message = "远端验证暂不可用"
        retry_after: float | None = None
        for attempt in range(self.settings.validation_attempts):
            session = self._session()
            try:
                retry_after = None
                response = session.get(
                    self.validation_url,
                    headers={"authorization": f"Bearer {access_token}", "accept": "application/json"},
                    timeout=self.settings.validation_timeout_seconds,
                )
                elapsed = int((time.perf_counter() - started) * 1000)
                if response.status_code == 200:
                    if self._response_json_object(response):
                        return ValidationResult("valid", latency_ms=elapsed)
                    last_message = "OAuth 身份响应无效，暂时无法确认"
                elif response.status_code == 401:
                    return ValidationResult("invalid", "access_token", "Access Token 无效或已过期", latency_ms=elapsed)
                elif response.status_code == 429:
                    last_message = "上游限流，暂时无法确认"
                    retry_after = self._response_retry_after(response)
                elif response.status_code == 403:
                    last_message = "上游风控或访问限制，暂时无法确认"
                    retry_after = self._response_retry_after(response)
                elif self._is_retryable_status(response.status_code):
                    last_message = f"上游服务暂不可用: HTTP {response.status_code}"
                    retry_after = self._response_retry_after(response)
                else:
                    last_message = f"上游验证暂不可用: HTTP {response.status_code}"
                    retry_after = None
            except Exception:
                last_message = "网络或 TLS 错误，暂时无法确认"
                retry_after = None
            finally:
                self._close_session(session)
            if attempt < self.settings.validation_attempts - 1:
                self._sleep_before_retry(attempt, retry_after)
        return ValidationResult(
            "inconclusive",
            "transient",
            last_message,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

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
            "origin": self.chatgpt_base_url,
            "referer": f"{self.chatgpt_base_url}/",
            "oai-device-id": device_id,
            "oai-session-id": session_id,
            "x-openai-target-path": path,
            "x-openai-target-route": path,
        }
        if content_type:
            headers["content-type"] = content_type
        return headers

    def _probe_chatgpt_once(
        self,
        access_token: str,
        *,
        session: Any | None = None,
        device_id: str | None = None,
        session_id: str | None = None,
    ) -> tuple[str, str, float | None]:
        """Probe product endpoints while avoiding duplicate requests during a shared gate block."""
        owns_session = session is None
        session = session or self._session()
        device_id = device_id or str(uuid.uuid4())
        session_id = session_id or str(uuid.uuid4())
        self._seed_chatgpt_device_cookie(session, device_id)
        failures: list[str] = []
        retry_after_hint: float | None = None
        rejected_probe = ""
        probes = (
            (
                "conversation/init",
                "post",
                self.conversation_init_path,
                self.chatgpt_base_url + self.conversation_init_path,
                {
                    "gizmo_id": None,
                    "requested_default_model": None,
                    "conversation_id": None,
                    "timezone_offset_min": -480,
                },
            ),
            (
                "accounts/check",
                "get",
                self.account_check_path,
                self.chatgpt_base_url + self.account_check_path + "?timezone_offset_min=-480",
                None,
            ),
        )
        try:
            for name, method, path, url, payload in probes:
                try:
                    headers = self._chatgpt_headers(
                        access_token,
                        path,
                        device_id=device_id,
                        session_id=session_id,
                        content_type="application/json" if payload is not None else None,
                    )
                    if method == "post":
                        response = session.post(
                            url,
                            headers=headers,
                            json=payload,
                            timeout=self.settings.validation_timeout_seconds,
                        )
                    else:
                        response = session.get(
                            url,
                            headers=headers,
                            timeout=self.settings.validation_timeout_seconds,
                        )
                except Exception:
                    failures.append(f"{name} 网络或 TLS 错误")
                    continue

                if response.status_code == 401:
                    rejected_probe = rejected_probe or name
                    continue
                if response.status_code != 200:
                    retry_after = self._response_retry_after(response)
                    if retry_after is not None:
                        retry_after_hint = max(retry_after_hint or 0.0, retry_after)
                    if response.status_code == 403:
                        failure = f"{name} 上游风控或访问限制"
                    elif response.status_code == 429:
                        failure = f"{name} 上游限流"
                    elif response.status_code >= 500:
                        failure = f"{name} 上游服务暂不可用: HTTP {response.status_code}"
                    else:
                        failure = f"{name} 验证暂不可用: HTTP {response.status_code}"
                    if response.status_code in self._shared_gate_statuses:
                        if rejected_probe:
                            return "invalid", f"ChatGPT {rejected_probe} 拒绝 Access Token", None
                        return "inconclusive", failure, retry_after
                    failures.append(failure)
                    continue
                if not self._response_json_object(response):
                    failures.append(f"{name} 响应无效")
            if rejected_probe:
                return "invalid", f"ChatGPT {rejected_probe} 拒绝 Access Token", None
            if not failures:
                return "valid", "", None
            return "inconclusive", "；".join(failures), retry_after_hint
        finally:
            if owns_session:
                self._close_session(session)

    def _validate_chatgpt_access(self, access_token: str) -> ValidationResult:
        started = time.perf_counter()
        last_message = "ChatGPT 实际能力验证暂不可用"
        retry_after: float | None = None
        session = self._session()
        device_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        try:
            for attempt in range(self.settings.validation_attempts):
                outcome, message, retry_after = self._probe_chatgpt_once(
                    access_token,
                    session=session,
                    device_id=device_id,
                    session_id=session_id,
                )
                elapsed = int((time.perf_counter() - started) * 1000)
                if outcome == "valid":
                    return ValidationResult("valid", latency_ms=elapsed)
                if outcome == "invalid":
                    return ValidationResult("invalid", "access_token", message, latency_ms=elapsed)
                last_message = message or last_message
                if attempt < self.settings.validation_attempts - 1:
                    self._sleep_before_retry(attempt, retry_after)
            return ValidationResult(
                "inconclusive",
                "transient",
                last_message,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        finally:
            self._close_session(session)

    def _validate_access_token(self, access_token: str) -> ValidationResult:
        started = time.perf_counter()
        identity = self._validate_oauth_identity(access_token)
        if identity.outcome != "valid":
            return identity
        product = self._validate_chatgpt_access(access_token)
        product.latency_ms = int((time.perf_counter() - started) * 1000)
        return product

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
                headers={"content-type": "application/x-www-form-urlencoded", "accept": "application/json"},
                data=payload,
                timeout=self.settings.validation_timeout_seconds,
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

    def _confirm_refreshed_access(self, refresh_result: ValidationResult) -> ValidationResult:
        confirmation = self._validate_access_token(refresh_result.access_token)
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

    def validate(self, account: Account) -> ValidationResult:
        access_token = self.security.decrypt(account.access_token_encrypted)
        refresh_token = self.security.decrypt(account.refresh_token_encrypted)
        if self.settings.validation_mode == "structural":
            if access_token or refresh_token:
                return ValidationResult("valid", "", "结构校验通过", "structural")
            return ValidationResult("invalid", "credentials", "缺少可验证凭据", "structural")

        if refresh_token and (not access_token or self._access_token_needs_refresh(account)):
            refresh_result = self._refresh(refresh_token, account.client_id)
            if refresh_result.outcome != "valid":
                return refresh_result
            return self._confirm_refreshed_access(refresh_result)

        if access_token:
            access_result = self._validate_access_token(access_token)
            if access_result.outcome == "valid" or access_result.outcome == "inconclusive":
                return access_result
            if not refresh_token:
                return access_result
        elif not refresh_token:
            return ValidationResult("invalid", "credentials", "缺少可验证凭据")

        refresh_result = self._refresh(refresh_token, account.client_id)
        if refresh_result.outcome != "valid":
            return refresh_result
        return self._confirm_refreshed_access(refresh_result)


def apply_validation(
    session: Session,
    account: Account,
    validator: TokenValidator,
    *,
    preserve_reservation: bool = False,
) -> ValidationResult:
    result = validator.validate(account)
    persist_validation_result(session, account, validator, result, preserve_reservation=preserve_reservation)
    return result


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
