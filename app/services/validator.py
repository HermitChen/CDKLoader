from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from curl_cffi import requests as cffi_requests
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Account, ValidationAttempt, utcnow
from ..security import SecurityManager
from .openai_validation import (
    CHATGPT_CLIENT_VERSION,
    CHATGPT_VALIDATION_IMPERSONATE,
    CHATGPT_VALIDATION_USER_AGENT,
    PLATFORM_OAUTH_CLIENT_ID,
    TokenRefreshManager,
    classify_token_error,
    is_inconclusive_error,
    redact_credentials,
)
from .token_artifacts import normalize_token_artifacts


@dataclass
class ValidationResult:
    outcome: str
    error_type: str = ""
    message: str = ""
    validated_via: str = "access_token"
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    session_token: str = ""
    account_id: str = ""
    workspace_id: str = ""
    extra_data: dict[str, Any] | None = None
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
    current_access_token_valid: bool = False
    current_access_token_error: str = ""
    current_access_token_error_type: str = ""
    refreshed_access_token_valid: bool = False
    refreshed_access_token_error: str = ""
    refreshed_access_token_error_type: str = ""
    refreshed: bool = False
    recoverable: bool = False
    status: str = ""


def _metadata_without_credentials(value: Any) -> Any:
    # Manager stores normalized artifacts in extra_data. Here those secrets
    # belong exclusively in the existing encrypted credential columns.
    secret_keys = {
        "accesstoken", "refreshtoken", "idtoken", "sessiontoken", "cookies",
        "password", "twofactor", "totpsecret", "2fa",
    }
    if isinstance(value, dict):
        return {
            key: _metadata_without_credentials(item)
            for key, item in value.items()
            if "".join(char for char in str(key).lower() if char.isalnum()) not in secret_keys
        }
    if isinstance(value, list):
        return [_metadata_without_credentials(item) for item in value]
    return value


class TokenValidator:
    """Adapt codex-manager's account checks to encrypted inventory and delivery."""

    validation_url = TokenRefreshManager.VALIDATION_URL
    token_url = TokenRefreshManager.TOKEN_URL
    chatgpt_base_url = "https://chatgpt.com"
    conversation_init_path = "/backend-api/conversation/init"
    account_check_path = "/backend-api/accounts/check/v4-2023-04-27"
    chatgpt_validation_impersonate = CHATGPT_VALIDATION_IMPERSONATE
    chatgpt_validation_user_agent = CHATGPT_VALIDATION_USER_AGENT
    chatgpt_validation_client_version = CHATGPT_CLIENT_VERSION

    def __init__(self, settings: Settings, security: SecurityManager):
        self.settings = settings
        self.security = security
        self._account_lock_guard = threading.Lock()
        self._account_locks: dict[str, threading.Lock] = {}

    def _session(self):
        return cffi_requests.Session(impersonate=CHATGPT_VALIDATION_IMPERSONATE)

    def _account_lock(self, account: Account) -> threading.Lock:
        key = str(account.id or account.account_id or account.access_token_encrypted or account.email)
        with self._account_lock_guard:
            return self._account_locks.setdefault(key, threading.Lock())

    @staticmethod
    def _extra_data(account: Account) -> dict[str, Any]:
        value = account.extra_data
        if isinstance(value, str):
            try:
                value = json.loads(value or "{}")
            except (TypeError, json.JSONDecodeError):
                return {}
        return value if isinstance(value, dict) else {}

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
        direct = self._proxy_value(account.proxy_used)
        if direct:
            return direct
        extra = self._extra_data(account)
        for key in ("proxy_used", "proxy_url", "proxy"):
            proxy = self._proxy_value(extra.get(key))
            if proxy:
                return proxy
        return ""

    def _validation_proxy_url(self, account: Account, explicit_proxy: str | None = None) -> str | None:
        explicit = self._proxy_value(explicit_proxy)
        if explicit:
            return explicit
        raw_pool = self.settings.validation_proxy_pool
        if isinstance(raw_pool, str):
            candidates = raw_pool.replace(";", "\n").replace(",", "\n").splitlines()
        elif isinstance(raw_pool, (list, tuple, set)):
            candidates = list(raw_pool)
        else:
            candidates = []
        candidates = [self._proxy_value(candidate) for candidate in candidates]
        candidates = [candidate for candidate in candidates if candidate]
        configured = self._proxy_value(self.settings.validation_proxy)
        mode = str(self.settings.validation_egress_mode or "direct").strip().lower()
        if mode == "pool":
            return random.choice(candidates) if candidates else configured or None
        if mode == "account":
            return self._account_validation_proxy(account) or configured or (random.choice(candidates) if candidates else None)
        return None

    @staticmethod
    def _error_result(manager: TokenRefreshManager, message: str | None, error_type: str = "") -> ValidationResult:
        error_type = error_type or classify_token_error(message)
        inconclusive = is_inconclusive_error(message, error_type=error_type)
        banned = not inconclusive and any(word in str(message or "").lower() for word in (
            "banned", "deactivated", "suspended", "terminated", "disabled", "封禁",
        ))
        return ValidationResult(
            "inconclusive" if inconclusive else "invalid",
            error_type or ("transient" if inconclusive else "access_token"),
            message or "",
            status="banned" if banned else "",
            **manager.validation_details,
        )

    def _validate_unlocked(self, account: Account, *, validation_proxy: str | None = None) -> ValidationResult:
        access_token = self.security.decrypt(account.access_token_encrypted)
        refresh_token = self.security.decrypt(account.refresh_token_encrypted)
        if self.settings.validation_mode == "structural":
            if access_token or refresh_token:
                return ValidationResult("valid", message="结构校验通过", validated_via="structural")
            return ValidationResult("invalid", "credentials", "缺少可验证凭据", "structural")

        # One exit is selected for AT checks, RT refresh and the new AT checks.
        manager = TokenRefreshManager(
            proxy_url=self._validation_proxy_url(account, validation_proxy),
            client_id=self.settings.oauth_client_id,
            redirect_uri=self.settings.oauth_redirect_uri,
        )
        manager._create_session = self._session
        manager.sensitive_credentials = (
            access_token, refresh_token,
            self.security.decrypt(account.id_token_encrypted),
            self.security.decrypt(account.session_token_encrypted),
        )
        current_error = "账号没有 access_token"
        current_valid = False
        current_error_type = "access_token"
        if access_token.strip():
            current_valid, current_error = manager.validate_token(access_token)
            current_error_type = classify_token_error(current_error)
            if current_valid:
                return ValidationResult("valid", current_access_token_valid=True, **manager.validation_details)
            current_result = self._error_result(manager, current_error)
            current_result.current_access_token_valid = False
            current_result.current_access_token_error = current_error or ""
            current_result.current_access_token_error_type = current_error_type
            # Match manager: temporary failures never consume a refresh token.
            if current_result.outcome == "inconclusive" or not refresh_token.strip():
                return current_result

        if not refresh_token.strip():
            return ValidationResult(
                "invalid", "refresh_token", "账号没有 access_token 和 refresh_token", "",
                current_access_token_error=current_error,
                current_access_token_error_type=current_error_type,
            )

        extra = self._extra_data(account)
        client_id = str(extra.get("refresh_token_client_id") or extra.get("oauth_client_id") or account.client_id or "").strip()
        redirect_uri = str(extra.get("oauth_redirect_uri") or "").strip()
        if client_id == PLATFORM_OAUTH_CLIENT_ID:
            redirect_uri = ""
        refresh = manager.refresh_by_oauth_token(
            refresh_token, client_id=client_id or account.client_id, redirect_uri=redirect_uri or None,
        )
        if not refresh.success or not refresh.access_token:
            result = self._error_result(manager, refresh.error_message, refresh.error_type)
            result.validated_via = "access_token" if access_token.strip() else ""
            result.message = "；".join(filter(None, (current_error, refresh.error_message)))
        else:
            refreshed_valid, refreshed_error = manager.validate_token(refresh.access_token)
            if refreshed_valid:
                result = ValidationResult(
                    "valid", current_error_type or "access_token",
                    "；".join(filter(None, (current_error, "已通过 refresh_token 刷新恢复"))),
                    **manager.validation_details,
                )
                result.recoverable = True
            else:
                result = self._error_result(manager, refreshed_error)
                summary = "refresh_token 刷新成功，但新 AT 暂时无法确认" if result.outcome == "inconclusive" else "refresh_token 刷新后验证失败"
                result.message = "；".join(filter(None, (current_error, f"{summary}: {refreshed_error}")))
            # Persist rotated credentials even if confirmation is inconclusive/invalid.
            result.validated_via = "refresh_token"
            result.refreshed = True
            result.refreshed_access_token_valid = refreshed_valid
            result.refreshed_access_token_error = refreshed_error or ""
            result.refreshed_access_token_error_type = classify_token_error(refreshed_error)
            normalized = normalize_token_artifacts(
                email=account.email, access_token=refresh.access_token,
                refresh_token=refresh.refresh_token or refresh_token,
                id_token=refresh.id_token or self.security.decrypt(account.id_token_encrypted),
                session_token=refresh.session_token or self.security.decrypt(account.session_token_encrypted),
                account_id=refresh.account_id or account.account_id,
                workspace_id=refresh.workspace_id or account.workspace_id,
                extra_data=extra, expires_at=refresh.expires_at or account.expires_at,
            )
            normalized["extra_data"].pop("proxy_used", None)
            result.access_token = normalized["access_token"]
            result.refresh_token = normalized["refresh_token"]
            result.id_token = normalized["id_token"]
            result.session_token = normalized["session_token"]
            result.account_id = normalized["account_id"]
            result.workspace_id = normalized["workspace_id"]
            result.extra_data = _metadata_without_credentials(normalized["extra_data"])
            result.expires_at = refresh.expires_at

        result.current_access_token_valid = current_valid
        result.current_access_token_error = current_error or ""
        result.current_access_token_error_type = current_error_type
        result.message = redact_credentials(
            result.message, access_token, refresh_token, result.access_token, result.refresh_token, result.id_token,
        )
        return result

    def validate(self, account: Account, *, validation_proxy: str | None = None) -> ValidationResult:
        started = time.perf_counter()
        with self._account_lock(account):
            result = self._validate_unlocked(account, validation_proxy=validation_proxy)
        credentials = (
            self.security.decrypt(account.access_token_encrypted),
            self.security.decrypt(account.refresh_token_encrypted),
            self.security.decrypt(account.id_token_encrypted),
            self.security.decrypt(account.session_token_encrypted),
            result.access_token, result.refresh_token, result.id_token, result.session_token,
        )
        for field in ("message", "current_access_token_error", "refreshed_access_token_error"):
            setattr(result, field, redact_credentials(getattr(result, field), *credentials))
        if result.outcome == "valid":
            result.status = "available"
        elif result.outcome == "invalid":
            result.status = "banned" if result.status == "banned" or result.error_type == "banned" else "expired"
        else:
            result.status = account.status or ""
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result


def apply_validation(
    session: Session,
    account: Account,
    validator: TokenValidator,
    *,
    preserve_reservation: bool = False,
    validation_proxy: str | None = None,
) -> ValidationResult:
    result = validator.validate(
        account,
        validation_proxy=validation_proxy,
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
        "current_access_token_valid": result.current_access_token_valid,
        "current_access_token_error": result.current_access_token_error,
        "current_access_token_error_type": result.current_access_token_error_type,
        "refreshed_access_token_valid": result.refreshed_access_token_valid,
        "refreshed_access_token_error": result.refreshed_access_token_error,
        "refreshed_access_token_error_type": result.refreshed_access_token_error_type,
        "refreshed": result.refreshed,
        "recoverable": result.recoverable,
        "valid": result.outcome == "valid",
        "inconclusive": result.outcome == "inconclusive",
        "status": result.status,
    }


def persist_validation_result(
    session: Session,
    account: Account,
    validator: TokenValidator,
    result: ValidationResult,
    *,
    preserve_reservation: bool = False,
    previous_status: str | None = None,
) -> None:
    account.validated_at = utcnow()
    account.proxy_used = None
    refreshed_credentials = result.validated_via == "refresh_token" and bool(result.access_token)
    if result.outcome == "valid" or refreshed_credentials:
        if result.access_token:
            account.access_token_encrypted = validator.security.encrypt(result.access_token)
        if result.refresh_token:
            account.refresh_token_encrypted = validator.security.encrypt(result.refresh_token)
        if result.id_token:
            account.id_token_encrypted = validator.security.encrypt(result.id_token)
        if result.session_token:
            account.session_token_encrypted = validator.security.encrypt(result.session_token)
        if result.account_id:
            account.account_id = result.account_id
        if result.workspace_id:
            account.workspace_id = result.workspace_id
        if result.extra_data is not None:
            account.extra_data = json.dumps(result.extra_data, ensure_ascii=False)
        if result.expires_at:
            account.expires_at = result.expires_at
        if refreshed_credentials:
            account.last_refresh = utcnow()
            account.version += 1
    if result.outcome == "valid":
        if not preserve_reservation or account.status != "reserved":
            account.status = "available"
    elif result.outcome == "invalid":
        account.status = "banned" if result.status == "banned" or result.error_type == "banned" else "expired"
    elif account.status == "reserved":
        # Release a temporary delivery lease without declaring the account invalid.
        account.status = previous_status or "available"

    if result.outcome != "valid":
        account.reserved_by = None
        account.reserved_until = None
    result.status = account.status

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
