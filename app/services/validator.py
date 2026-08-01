from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
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

    def __init__(self, settings: Settings, security: SecurityManager):
        self.settings = settings
        self.security = security

    def _session(self):
        return cffi_requests.Session(impersonate="chrome120")

    def _validate_access_token(self, access_token: str) -> ValidationResult:
        started = time.perf_counter()
        last_message = "远端验证暂不可用"
        for attempt in range(self.settings.validation_attempts):
            try:
                response = self._session().get(
                    self.validation_url,
                    headers={"authorization": f"Bearer {access_token}", "accept": "application/json"},
                    timeout=self.settings.validation_timeout_seconds,
                )
                elapsed = int((time.perf_counter() - started) * 1000)
                if response.status_code == 200:
                    return ValidationResult("valid", latency_ms=elapsed)
                if response.status_code == 401:
                    return ValidationResult("invalid", "access_token", "Access Token 无效或已过期", latency_ms=elapsed)
                if response.status_code == 403:
                    detail = (response.text or "").lower()
                    if any(word in detail for word in ("banned", "suspended", "deactivated", "disabled")):
                        return ValidationResult("invalid", "banned", "账号已被封禁或停用", latency_ms=elapsed)
                    if any(word in detail for word in ("token", "expired", "invalid", "unauthorized")):
                        return ValidationResult("invalid", "access_token", "Access Token 无效或已过期", latency_ms=elapsed)
                if response.status_code == 429:
                    last_message = "上游限流，暂时无法确认"
                elif response.status_code >= 500:
                    last_message = f"上游服务暂不可用: HTTP {response.status_code}"
                else:
                    last_message = f"上游验证暂不可用: HTTP {response.status_code}"
            except Exception:
                last_message = "网络或 TLS 错误，暂时无法确认"
            if attempt < self.settings.validation_attempts - 1:
                time.sleep(0.25 * (attempt + 1))
        return ValidationResult(
            "inconclusive",
            "transient",
            last_message,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    def _refresh(self, refresh_token: str, client_id: str | None) -> ValidationResult:
        started = time.perf_counter()
        try:
            payload: dict[str, Any] = {
                "client_id": client_id or self.settings.oauth_client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
            if self.settings.oauth_redirect_uri:
                payload["redirect_uri"] = self.settings.oauth_redirect_uri
            response = self._session().post(
                self.token_url,
                headers={"content-type": "application/x-www-form-urlencoded", "accept": "application/json"},
                data=payload,
                timeout=self.settings.validation_timeout_seconds,
            )
            elapsed = int((time.perf_counter() - started) * 1000)
            if response.status_code != 200:
                if response.status_code in {400, 401}:
                    return ValidationResult("invalid", "refresh_token", "Refresh Token 无效或已过期", "refresh_token", latency_ms=elapsed)
                return ValidationResult("inconclusive", "transient", f"刷新服务暂不可用: HTTP {response.status_code}", "refresh_token", latency_ms=elapsed)
            data = response.json()
            access_token = str(data.get("access_token") or "")
            if not access_token:
                return ValidationResult("invalid", "refresh_token", "刷新响应缺少 Access Token", "refresh_token", latency_ms=elapsed)
            expires_in = int(data.get("expires_in") or 3600)
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

    def validate(self, account: Account) -> ValidationResult:
        access_token = self.security.decrypt(account.access_token_encrypted)
        refresh_token = self.security.decrypt(account.refresh_token_encrypted)
        if self.settings.validation_mode == "structural":
            if access_token or refresh_token:
                return ValidationResult("valid", "", "结构校验通过", "structural")
            return ValidationResult("invalid", "credentials", "缺少可验证凭据", "structural")

        if access_token:
            access_result = self._validate_access_token(access_token)
            if access_result.outcome == "valid" or access_result.outcome == "inconclusive":
                return access_result
            if access_result.error_type == "banned" or not refresh_token:
                return access_result
        elif not refresh_token:
            return ValidationResult("invalid", "credentials", "缺少可验证凭据")

        refresh_result = self._refresh(refresh_token, account.client_id)
        if refresh_result.outcome != "valid":
            return refresh_result
        confirmation = self._validate_access_token(refresh_result.access_token)
        confirmation.validated_via = "refresh_token"
        confirmation.refresh_token = refresh_result.refresh_token
        confirmation.id_token = refresh_result.id_token
        confirmation.expires_at = refresh_result.expires_at
        if confirmation.outcome == "valid":
            confirmation.access_token = refresh_result.access_token
        return confirmation


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
    if result.outcome == "valid":
        if result.access_token:
            account.access_token_encrypted = validator.security.encrypt(result.access_token)
        if result.refresh_token:
            account.refresh_token_encrypted = validator.security.encrypt(result.refresh_token)
        if result.id_token:
            account.id_token_encrypted = validator.security.encrypt(result.id_token)
        if result.expires_at:
            account.expires_at = result.expires_at
        if result.validated_via == "refresh_token":
            account.last_refresh = utcnow()
            account.version += 1
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
