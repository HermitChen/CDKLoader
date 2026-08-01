from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from collections import defaultdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..models import Account, CDK, DeliveryItem, Redemption, RedemptionCDK
from ..security import SecurityManager


ALL_EXPORT_FIELDS = {
    "email",
    "password",
    "client_id",
    "account_id",
    "workspace_id",
    "access_token",
    "refresh_token",
    "id_token",
    "session_token",
    "cookies",
    "source",
    "registration_mode",
    "registered_at",
    "last_refresh",
    "expires_at",
    "validated_at",
}
DEFAULT_EXPORT_FIELDS = [
    "email",
    "account_id",
    "workspace_id",
    "client_id",
    "access_token",
    "refresh_token",
    "expires_at",
    "validated_at",
]


def _date_value(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def serialize_account(account, security: SecurityManager, fields: list[str]) -> dict:
    safe_fields = [field for field in fields if field in ALL_EXPORT_FIELDS]
    if not safe_fields:
        safe_fields = DEFAULT_EXPORT_FIELDS
    encrypted_fields = {
        "password": "password_encrypted",
        "access_token": "access_token_encrypted",
        "refresh_token": "refresh_token_encrypted",
        "id_token": "id_token_encrypted",
        "session_token": "session_token_encrypted",
        "cookies": "cookies_encrypted",
    }
    payload: dict[str, str | None] = {}
    for field in safe_fields:
        if field in encrypted_fields:
            payload[field] = security.decrypt(getattr(account, encrypted_fields[field]))
        elif field in {"registered_at", "last_refresh", "expires_at", "validated_at"}:
            payload[field] = _date_value(getattr(account, field))
        else:
            payload[field] = getattr(account, field)
    return payload


def _render(records: list[dict], export_format: str) -> tuple[bytes, str, str]:
    if export_format == "csv":
        output = io.StringIO()
        fields = list(records[0].keys()) if records else DEFAULT_EXPORT_FIELDS
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)
        return output.getvalue().encode("utf-8-sig"), "csv", "text/csv; charset=utf-8"
    if export_format == "txt":
        lines = ["----".join(str(value or "") for value in row.values()) for row in records]
        return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8"), "txt", "text/plain; charset=utf-8"
    return json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8"), "json", "application/json"


def _decrypt(account, security: SecurityManager, field: str) -> str:
    try:
        return security.decrypt(getattr(account, f"{field}_encrypted"))
    except Exception:
        return ""


def _extra(account) -> dict:
    try:
        value = json.loads(account.extra_data or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _number(value, default: int | float) -> int | float:
    try:
        return int(value) if isinstance(default, int) else float(value)
    except (TypeError, ValueError):
        return default


def _delivery_identity(account, security: SecurityManager) -> dict:
    extra = _extra(account)
    account_id = account.account_id or str(extra.get("chatgpt_account_id") or "")
    chatgpt_account_id = str(extra.get("chatgpt_account_id") or account_id)
    return {
        "email": account.email or str(extra.get("email") or ""),
        "account_id": account_id,
        "chatgpt_account_id": chatgpt_account_id,
        "chatgpt_user_id": str(extra.get("chatgpt_user_id") or ""),
        "client_id": account.client_id or str(extra.get("client_id") or ""),
        "organization_id": account.workspace_id or str(extra.get("organization_id") or ""),
        "access_token": _decrypt(account, security, "access_token"),
        "refresh_token": _decrypt(account, security, "refresh_token"),
        "id_token": _decrypt(account, security, "id_token"),
        "session_token": _decrypt(account, security, "session_token"),
        "expires_at": _date_value(account.expires_at) or "",
        "last_refresh": _date_value(account.last_refresh) or "",
        "extra": extra,
    }


def _render_cpa(account, security: SecurityManager) -> bytes:
    values = _delivery_identity(account, security)
    payload = {
        "type": "web",
        "email": values["email"],
        "expired": values["expires_at"],
        "id_token": values["id_token"],
        "account_id": values["account_id"],
        "chatgpt_account_id": values["chatgpt_account_id"],
        "chatgpt_user_id": values["chatgpt_user_id"],
        "session_token": values["session_token"],
        "access_token": values["access_token"],
        "last_refresh": values["last_refresh"],
        "refresh_token": values["refresh_token"],
        "credentials": {
            "id_token": values["id_token"],
            "session_token": values["session_token"],
            "chatgpt_account_id": values["chatgpt_account_id"],
            "chatgpt_user_id": values["chatgpt_user_id"],
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _render_sub2api(account, security: SecurityManager) -> bytes:
    values = _delivery_identity(account, security)
    extra = values["extra"]
    expires_in = 0
    if account.expires_at:
        expires_in = max(0, int((account.expires_at - datetime.now()).total_seconds()))
    payload = {
        "exported_at": f"{datetime.now().isoformat(timespec='milliseconds')}Z",
        "proxies": [],
        "accounts": [{
            "name": values["email"],
            "platform": "openai",
            "type": "oauth",
            "account_id": values["account_id"],
            "chatgpt_account_id": values["chatgpt_account_id"],
            "chatgpt_user_id": values["chatgpt_user_id"],
            "session_token": values["session_token"],
            "credentials": {
                "email": values["email"],
                "access_token": values["access_token"],
                "refresh_token": values["refresh_token"],
                "id_token": values["id_token"],
                "session_token": values["session_token"],
                "client_id": values["client_id"],
                "chatgpt_account_id": values["chatgpt_account_id"],
                "chatgpt_user_id": values["chatgpt_user_id"],
                "organization_id": values["organization_id"],
                "expires_at": values["expires_at"],
                "expires_in": expires_in,
                "model_mapping": extra.get("model_mapping") if isinstance(extra.get("model_mapping"), dict) else {},
                "plan_type": str(extra.get("plan_type") or ""),
            },
            "extra": {
                "email": values["email"],
                "auth_provider": str(extra.get("auth_provider") or "platform"),
                "source": account.source,
                "plan_type": str(extra.get("plan_type") or ""),
            },
            "concurrency": _number(extra.get("concurrency"), 10),
            "priority": _number(extra.get("priority"), 1),
            "rate_multiplier": _number(extra.get("rate_multiplier"), 1),
            "auto_pause_on_expired": bool(extra.get("auto_pause_on_expired", True)),
        }],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _archive_stem(account, index: int, used: set[str]) -> str:
    raw = account.email or account.account_id or f"account-{index}"
    base = re.sub(r"[^A-Za-z0-9@._+-]", "_", raw).strip("._") or f"account-{index}"
    stem = base
    suffix = 2
    while stem in used:
        stem = f"{base}-{suffix}"
        suffix += 1
    used.add(stem)
    return stem


def _timestamped_zip_filename() -> str:
    return f"accounts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"


def build_account_archive(
    accounts: list[Account],
    security: SecurityManager,
    additional_files: list[tuple[str, bytes]] | None = None,
) -> tuple[bytes, str, str]:
    buffer = io.BytesIO()
    used_stems: set[str] = set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, account in enumerate(accounts, start=1):
            stem = _archive_stem(account, index, used_stems)
            cpa_name = f"cpa/{stem}.json"
            sub2api_name = f"sub2api/{stem}_sub2api.json"
            archive.writestr(cpa_name, _render_cpa(account, security))
            archive.writestr(sub2api_name, _render_sub2api(account, security))
        for filename, content in additional_files or []:
            archive.writestr(filename, content)
    return buffer.getvalue(), _timestamped_zip_filename(), "application/zip"


def build_download(session: Session, redemption_id: str, security: SecurityManager) -> tuple[bytes, str, str]:
    redemption = session.scalar(
        select(Redemption)
        .options(joinedload(Redemption.cdks).joinedload(RedemptionCDK.cdk))
        .where(Redemption.id == redemption_id)
    )
    if not redemption:
        raise ValueError("兑换任务不存在")
    items = session.scalars(
        select(DeliveryItem)
        .options(joinedload(DeliveryItem.account), joinedload(DeliveryItem.cdk))
        .where(DeliveryItem.redemption_id == redemption_id)
        .order_by(DeliveryItem.id)
    ).all()
    grouped: dict[str, list] = defaultdict(list)
    deliveries_by_cdk: dict[str, list[DeliveryItem]] = defaultdict(list)
    cdk_by_id: dict[str, CDK] = {}
    for item in items:
        grouped[item.cdk_id].append(item.account)
        deliveries_by_cdk[item.cdk_id].append(item)
        cdk_by_id[item.cdk_id] = item.cdk

    rendered: list[tuple[str, bytes]] = []
    json_items: list[DeliveryItem] = []
    for relation in sorted(redemption.cdks, key=lambda item: item.ordinal):
        cdk = cdk_by_id.get(relation.cdk_id) or relation.cdk
        if cdk.export_format == "json":
            json_items.extend(deliveries_by_cdk.get(cdk.id, []))
            continue
        fields = json.loads(cdk.export_fields or "[]")
        records = [serialize_account(account, security, fields) for account in grouped.get(cdk.id, [])]
        body, extension, media_type = _render(records, cdk.export_format)
        rendered.append((f"accounts_{cdk.code_prefix}.{extension}", body))

    if json_items:
        return build_account_archive([item.account for item in json_items], security, rendered)

    if len(rendered) == 1:
        filename, content = rendered[0]
        _, extension = filename.rsplit(".", 1)
        media = {"json": "application/json", "csv": "text/csv; charset=utf-8", "txt": "text/plain; charset=utf-8"}[extension]
        return content, filename, media

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, content in rendered:
            archive.writestr(filename, content)
    return buffer.getvalue(), _timestamped_zip_filename(), "application/zip"
