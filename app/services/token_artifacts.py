"""Token normalization ported from codex-manager (MIT).

See THIRD_PARTY_NOTICES.md. Returned credentials must be encrypted by the caller.
"""
import base64
import json
import time
from datetime import datetime
from typing import Any, Dict, Optional


AUTH_CLAIMS_KEY = "https://api.openai.com/auth"


def decode_jwt_payload(token: str) -> Dict[str, Any]:
    """在不验签的前提下解码 JWT payload。"""
    text = str(token or "").strip()
    if text.count(".") < 2:
        return {}

    payload_part = text.split(".")[1]
    padding = "=" * (-len(payload_part) % 4)
    try:
        raw = base64.urlsafe_b64decode((payload_part + padding).encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _first_nonempty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _extract_auth_claims(payload: Dict[str, Any]) -> Dict[str, Any]:
    claims = payload.get(AUTH_CLAIMS_KEY) or {}
    return claims if isinstance(claims, dict) else {}


def _as_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _b64url_json(data: Dict[str, Any]) -> str:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def build_compat_id_token(
    *,
    email: str = "",
    exp: int = 0,
    account_id: str = "",
    user_id: str = "",
    plan_type: str = "",
) -> str:
    """生成一个仅用于兼容下游字段解析的最小 JWT。"""
    header = {"alg": "none", "typ": "JWT"}
    effective_exp = exp if exp > 0 else int(time.time()) + 3600
    payload = {
        "email": str(email or "").strip(),
        "exp": effective_exp,
        AUTH_CLAIMS_KEY: {
            "chatgpt_account_id": str(account_id or "").strip(),
            "chatgpt_user_id": str(user_id or "").strip(),
            "plan_type": str(plan_type or "").strip(),
        },
    }
    if str(user_id or "").strip():
        payload["sub"] = str(user_id).strip()

    return f"{_b64url_json(header)}.{_b64url_json(payload)}."


def normalize_token_artifacts(
    *,
    email: str = "",
    access_token: str = "",
    refresh_token: str = "",
    id_token: str = "",
    session_token: str = "",
    account_id: str = "",
    workspace_id: str = "",
    extra_data: Optional[Dict[str, Any]] = None,
    expires_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """统一补齐 token 相关字段，兼容 fastpath 只返回 access_token/session_token 的场景。"""
    access_payload = decode_jwt_payload(access_token)
    id_payload = decode_jwt_payload(id_token)
    access_auth = _extract_auth_claims(access_payload)
    id_auth = _extract_auth_claims(id_payload)
    existing_extra = dict(extra_data or {})
    existing_artifacts = _as_dict(existing_extra.get("token_artifacts"))
    existing_credentials = _as_dict(existing_extra.get("credentials"))
    existing_artifact_credentials = _as_dict(existing_artifacts.get("credentials"))

    resolved_email = _first_nonempty(
        email,
        access_payload.get("email"),
        id_payload.get("email"),
        existing_extra.get("email"),
        existing_artifacts.get("email"),
        existing_credentials.get("email"),
        existing_artifact_credentials.get("email"),
    )
    resolved_account_id = _first_nonempty(
        account_id,
        access_auth.get("chatgpt_account_id"),
        access_auth.get("account_id"),
        access_auth.get("workspace_id"),
        id_auth.get("chatgpt_account_id"),
        id_auth.get("account_id"),
        id_auth.get("workspace_id"),
        existing_extra.get("chatgpt_account_id"),
        existing_extra.get("account_id"),
        existing_artifacts.get("chatgpt_account_id"),
        existing_artifacts.get("account_id"),
        existing_credentials.get("chatgpt_account_id"),
        existing_credentials.get("account_id"),
        existing_artifact_credentials.get("chatgpt_account_id"),
        existing_artifact_credentials.get("account_id"),
        workspace_id,
    )
    resolved_workspace_id = _first_nonempty(
        workspace_id,
        access_auth.get("workspace_id"),
        id_auth.get("workspace_id"),
        existing_extra.get("workspace_id"),
        existing_artifacts.get("workspace_id"),
        existing_credentials.get("organization_id"),
        existing_artifact_credentials.get("organization_id"),
        resolved_account_id,
    )
    resolved_user_id = _first_nonempty(
        access_auth.get("chatgpt_user_id"),
        id_auth.get("chatgpt_user_id"),
        access_payload.get("sub"),
        id_payload.get("sub"),
        existing_extra.get("chatgpt_user_id"),
        existing_artifacts.get("chatgpt_user_id"),
        existing_credentials.get("chatgpt_user_id"),
        existing_artifact_credentials.get("chatgpt_user_id"),
    )
    resolved_plan_type = _first_nonempty(
        access_auth.get("plan_type"),
        id_auth.get("plan_type"),
        access_payload.get("plan_type"),
        id_payload.get("plan_type"),
        existing_extra.get("plan_type"),
        existing_artifacts.get("plan_type"),
        existing_credentials.get("plan_type"),
        existing_artifact_credentials.get("plan_type"),
    )
    resolved_session_token = _first_nonempty(
        session_token,
        existing_extra.get("session_token"),
        existing_artifacts.get("session_token"),
        existing_credentials.get("session_token"),
        existing_artifact_credentials.get("session_token"),
    )

    resolved_exp = max(
        _int_or_zero(access_payload.get("exp")),
        _int_or_zero(id_payload.get("exp")),
        _int_or_zero(existing_extra.get("exp")),
        _int_or_zero(existing_artifacts.get("exp")),
        _int_or_zero(existing_credentials.get("exp")),
        _int_or_zero(existing_artifact_credentials.get("exp")),
        int(expires_at.timestamp()) if expires_at else 0,
    )

    compat_id_token_generated = False
    normalized_id_token = _first_nonempty(
        id_token,
        existing_extra.get("id_token"),
        existing_artifacts.get("id_token"),
        existing_credentials.get("id_token"),
        existing_artifact_credentials.get("id_token"),
    )
    if not normalized_id_token and (resolved_email or resolved_account_id or resolved_user_id or resolved_plan_type or resolved_exp):
        normalized_id_token = build_compat_id_token(
            email=resolved_email,
            exp=resolved_exp,
            account_id=resolved_account_id,
            user_id=resolved_user_id,
            plan_type=resolved_plan_type,
        )
        compat_id_token_generated = True

    normalized_extra = dict(existing_extra)
    normalized_credentials = dict(existing_credentials)
    normalized_credentials.update({
        "id_token": normalized_id_token,
        "session_token": resolved_session_token,
        "chatgpt_account_id": resolved_account_id,
        "chatgpt_user_id": resolved_user_id,
    })
    if resolved_account_id:
        normalized_credentials["account_id"] = resolved_account_id
    if resolved_workspace_id:
        normalized_credentials["organization_id"] = resolved_workspace_id
    if resolved_plan_type:
        normalized_credentials["plan_type"] = resolved_plan_type
    if resolved_email:
        normalized_credentials["email"] = resolved_email
    if resolved_exp:
        normalized_credentials["exp"] = resolved_exp

    token_artifacts = dict(existing_artifacts)
    token_artifacts.update({
        "email": resolved_email,
        "id_token": normalized_id_token,
        "account_id": resolved_account_id,
        "workspace_id": resolved_workspace_id,
        "chatgpt_account_id": resolved_account_id,
        "chatgpt_user_id": resolved_user_id,
        "session_token": resolved_session_token,
        "plan_type": resolved_plan_type,
        "exp": resolved_exp,
        "compat_id_token_generated": compat_id_token_generated,
        "credentials": dict(normalized_credentials),
    })
    normalized_extra["token_artifacts"] = token_artifacts
    normalized_extra["credentials"] = dict(normalized_credentials)
    if normalized_id_token:
        normalized_extra["id_token"] = normalized_id_token
    if resolved_session_token:
        normalized_extra["session_token"] = resolved_session_token
    if resolved_account_id:
        normalized_extra["account_id"] = resolved_account_id
    if resolved_account_id:
        normalized_extra["chatgpt_account_id"] = resolved_account_id
    if resolved_user_id:
        normalized_extra["chatgpt_user_id"] = resolved_user_id
    if resolved_plan_type:
        normalized_extra["plan_type"] = resolved_plan_type
    if resolved_exp:
        normalized_extra["exp"] = resolved_exp
    normalized_extra["compat_id_token_generated"] = compat_id_token_generated

    return {
        "email": resolved_email,
        "access_token": str(access_token or "").strip(),
        "refresh_token": str(refresh_token or "").strip(),
        "id_token": normalized_id_token,
        "session_token": resolved_session_token,
        "account_id": resolved_account_id,
        "workspace_id": resolved_workspace_id,
        "chatgpt_account_id": resolved_account_id,
        "chatgpt_user_id": resolved_user_id,
        "plan_type": resolved_plan_type,
        "exp": resolved_exp,
        "credentials": normalized_credentials,
        "extra_data": normalized_extra,
        "compat_id_token_generated": compat_id_token_generated,
    }
