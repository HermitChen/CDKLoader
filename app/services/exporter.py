from __future__ import annotations

import csv
import io
import json
import os
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from ..models import Account, DeliveryItem, OperationTask, Redelivery, Redemption, RedemptionCDK, utcnow
from ..security import SecurityManager
from ..time import CHINA_TIMEZONE, as_utc, china_now, to_china_iso


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


@dataclass
class ExportDelivery:
    account: Account
    cdk_id: str
    cdk_prefix: str
    export_format: str
    export_fields: str
    ordinal: int


@dataclass(frozen=True)
class StoredExportArtifact:
    path: Path
    media_type: str


def export_media_type(file_name: str) -> str:
    extension = Path(file_name).suffix.lower()
    return {
        ".json": "application/json",
        ".csv": "text/csv; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
        ".zip": "application/zip",
    }.get(extension, "application/octet-stream")


def canonical_redemption_archive_filename(redemption: Redemption) -> str:
    timestamp = redemption.completed_at or redemption.created_at or utcnow()
    local_timestamp = as_utc(timestamp).astimezone(CHINA_TIMEZONE)
    return f"accounts_{local_timestamp.strftime('%Y%m%d_%H%M%S')}_{redemption.id}.zip"


def canonical_redelivery_archive_filename(redelivery: Redelivery) -> str:
    local_timestamp = as_utc(redelivery.created_at or utcnow()).astimezone(CHINA_TIMEZONE)
    return f"accounts_{local_timestamp.strftime('%Y%m%d_%H%M%S')}_{redelivery.id}.zip"


def _artifact_path(directory: Path, file_name: str | None) -> Path | None:
    if not file_name:
        return None
    root = directory.resolve()
    candidate = (root / Path(file_name).name).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def publish_export_artifact(temporary: str | Path, target: str | Path) -> Path:
    temporary_path = Path(temporary)
    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(temporary_path, target_path)
    except FileExistsError:
        temporary_path.unlink(missing_ok=True)
    else:
        temporary_path.unlink(missing_ok=True)
    return target_path


def redemption_artifact_path(session: Session, redemption: Redemption, directory: str | Path) -> Path | None:
    export_directory = Path(directory).resolve()
    candidates: list[str | None] = [redemption.export_file_name]
    task_file_name = session.scalar(
        select(OperationTask.file_name)
        .where(
            OperationTask.task_type == "redemption_export",
            OperationTask.resource_id == redemption.id,
            OperationTask.status == "completed",
            OperationTask.file_name.is_not(None),
        )
        .order_by(OperationTask.created_at.desc())
    )
    candidates.append(task_file_name)
    candidates.append(canonical_redemption_archive_filename(redemption))
    for file_name in dict.fromkeys(name for name in candidates if name):
        if artifact := _artifact_path(export_directory, file_name):
            if redemption.export_file_name != artifact.name:
                redemption.export_file_name = artifact.name
            return artifact
    return None


def redelivery_artifact_path(session: Session, redelivery: Redelivery, directory: str | Path) -> Path | None:
    export_directory = Path(directory).resolve()
    candidates: list[str | None] = [redelivery.export_file_name]
    task_file_name = session.scalar(
        select(OperationTask.file_name)
        .where(
            OperationTask.task_type == "redelivery_export",
            OperationTask.resource_id == redelivery.id,
            OperationTask.status == "completed",
            OperationTask.file_name.is_not(None),
        )
        .order_by(OperationTask.created_at.desc())
    )
    candidates.append(task_file_name)
    candidates.append(canonical_redelivery_archive_filename(redelivery))
    for file_name in dict.fromkeys(name for name in candidates if name):
        if artifact := _artifact_path(export_directory, file_name):
            if redelivery.export_file_name != artifact.name:
                redelivery.export_file_name = artifact.name
            return artifact
    return None


def _date_value(value: datetime | None) -> str | None:
    return to_china_iso(value)


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
        expires_in = max(0, int((account.expires_at - utcnow()).total_seconds()))
    payload = {
        "exported_at": china_now().isoformat(timespec="milliseconds"),
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
    return f"accounts_{china_now().strftime('%Y%m%d_%H%M%S')}.zip"


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


def build_delivery_archive_to_path(
    deliveries: list[ExportDelivery],
    security: SecurityManager,
    path: str | Path,
    *,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[str, str, int]:
    """Write a delivery archive incrementally and report account-level progress."""
    if not deliveries:
        raise ValueError("没有可导出的关联账号")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    deliveries_by_cdk: defaultdict[str, list[ExportDelivery]] = defaultdict(list)
    cdk_settings: dict[str, ExportDelivery] = {}
    for delivery in sorted(deliveries, key=lambda item: item.ordinal):
        deliveries_by_cdk[delivery.cdk_id].append(delivery)
        cdk_settings.setdefault(delivery.cdk_id, delivery)

    total = len(deliveries)
    processed = 0
    used_stems: set[str] = set()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for cdk_id, settings in sorted(cdk_settings.items(), key=lambda item: item[1].ordinal):
            cdk_deliveries = deliveries_by_cdk[cdk_id]
            if settings.export_format == "json":
                for index, delivery in enumerate(cdk_deliveries, start=1):
                    stem = _archive_stem(delivery.account, index, used_stems)
                    archive.writestr(f"cpa/{stem}.json", _render_cpa(delivery.account, security))
                    archive.writestr(
                        f"sub2api/{stem}_sub2api.json",
                        _render_sub2api(delivery.account, security),
                    )
                    processed += 1
                    if on_progress:
                        on_progress(processed, total)
                continue

            records = []
            for delivery in cdk_deliveries:
                records.append(serialize_account(delivery.account, security, _export_fields(settings.export_fields)))
                processed += 1
                if on_progress:
                    on_progress(processed, total)
            body, extension, _ = _render(records, settings.export_format)
            archive.writestr(f"accounts_{settings.cdk_prefix}.{extension}", body)

    return target.name, "application/zip", total


def _export_fields(value: str) -> list[str]:
    try:
        fields = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return fields if isinstance(fields, list) else []


def _build_delivery_download(deliveries: list[ExportDelivery], security: SecurityManager) -> tuple[bytes, str, str]:
    if not deliveries:
        raise ValueError("没有可导出的关联账号")
    deliveries_by_cdk: defaultdict[str, list[ExportDelivery]] = defaultdict(list)
    cdk_settings: dict[str, ExportDelivery] = {}
    for delivery in sorted(deliveries, key=lambda item: item.ordinal):
        deliveries_by_cdk[delivery.cdk_id].append(delivery)
        cdk_settings.setdefault(delivery.cdk_id, delivery)
    rendered: list[tuple[str, bytes]] = []
    json_items: list[ExportDelivery] = []
    for cdk_id, settings in sorted(cdk_settings.items(), key=lambda item: item[1].ordinal):
        if settings.export_format == "json":
            json_items.extend(deliveries_by_cdk[cdk_id])
            continue
        fields = _export_fields(settings.export_fields)
        records = [serialize_account(item.account, security, fields) for item in deliveries_by_cdk[cdk_id]]
        body, extension, _ = _render(records, settings.export_format)
        rendered.append((f"accounts_{settings.cdk_prefix}.{extension}", body))

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


def _load_redemption_export_deliveries(session: Session, redemption_id: str) -> list[ExportDelivery]:
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
    deliveries_by_cdk: defaultdict[str, list[DeliveryItem]] = defaultdict(list)
    for item in items:
        deliveries_by_cdk[item.cdk_id].append(item)
    export_deliveries: list[ExportDelivery] = []
    for relation in sorted(redemption.cdks, key=lambda item: item.ordinal):
        cdk = relation.cdk
        for index, item in enumerate(deliveries_by_cdk.get(cdk.id, [])):
            export_deliveries.append(
                ExportDelivery(
                    account=item.account,
                    cdk_id=cdk.id,
                    cdk_prefix=cdk.code_prefix,
                    export_format=cdk.export_format,
                    export_fields=cdk.export_fields or "[]",
                    ordinal=relation.ordinal * 100_000 + index,
                )
            )
    return export_deliveries


def build_download(session: Session, redemption_id: str, security: SecurityManager) -> tuple[bytes, str, str]:
    return _build_delivery_download(_load_redemption_export_deliveries(session, redemption_id), security)


def _load_redelivery_export_deliveries(session: Session, redelivery_id: str) -> list[ExportDelivery]:
    redelivery = session.scalar(
        select(Redelivery).options(selectinload(Redelivery.items)).where(Redelivery.id == redelivery_id)
    )
    if not redelivery:
        raise ValueError("补发任务不存在")
    if not redelivery.items:
        raise ValueError("关联交付记录已删除，无法补发")
    account_ids = {item.account_id for item in redelivery.items}
    accounts = session.scalars(select(Account).where(Account.id.in_(account_ids))).all()
    accounts_by_id = {account.id: account for account in accounts}
    if len(accounts_by_id) != len(account_ids):
        raise ValueError("关联账号已删除，无法补发")
    export_deliveries = [
        ExportDelivery(
            account=accounts_by_id[item.account_id],
            cdk_id=item.cdk_id,
            cdk_prefix=item.cdk_prefix,
            export_format=item.export_format,
            export_fields=item.export_fields or "[]",
            ordinal=item.ordinal,
        )
        for item in redelivery.items
    ]
    return export_deliveries


def load_export_deliveries(
    session: Session,
    *,
    redemption_id: str | None = None,
    redelivery_id: str | None = None,
) -> list[ExportDelivery]:
    if bool(redemption_id) == bool(redelivery_id):
        raise ValueError("必须指定一种导出任务")
    if redemption_id:
        return _load_redemption_export_deliveries(session, redemption_id)
    return _load_redelivery_export_deliveries(session, redelivery_id or "")


def _canonical_download_filename(redemption: Redemption, original_name: str) -> str:
    suffix = Path(original_name).suffix.lower()
    if suffix == ".zip":
        return canonical_redemption_archive_filename(redemption)
    stem = Path(original_name).stem or "accounts"
    return f"{stem}_{redemption.id}{suffix}"


def persist_redemption_download(
    session: Session,
    redemption_id: str,
    security: SecurityManager,
    directory: str | Path,
) -> StoredExportArtifact:
    redemption = session.get(Redemption, redemption_id)
    if not redemption:
        raise ValueError("兑换任务不存在")
    if redemption.status != "completed":
        raise ValueError("兑换任务尚未完成")

    export_directory = Path(directory).resolve()
    export_directory.mkdir(parents=True, exist_ok=True)
    if artifact := redemption_artifact_path(session, redemption, export_directory):
        return StoredExportArtifact(artifact, export_media_type(artifact.name))

    content, original_name, media_type = build_download(session, redemption_id, security)
    file_name = _canonical_download_filename(redemption, original_name)
    target = export_directory / file_name
    temporary = export_directory / f".{redemption.id}.{uuid4().hex}.tmp"
    try:
        temporary.write_bytes(content)
        publish_export_artifact(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    redemption.export_file_name = target.name
    session.flush()
    return StoredExportArtifact(target, media_type)


def build_redemption_archive_to_path(
    session: Session,
    redemption_id: str,
    security: SecurityManager,
    directory: str | Path,
    *,
    temporary_path: str | Path | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[Path, int, bool]:
    redemption = session.get(Redemption, redemption_id)
    if not redemption:
        raise ValueError("兑换任务不存在")
    if redemption.status != "completed":
        raise ValueError("兑换任务尚未完成")

    export_directory = Path(directory).resolve()
    export_directory.mkdir(parents=True, exist_ok=True)
    if artifact := redemption_artifact_path(session, redemption, export_directory):
        return artifact, redemption.delivered_count, True

    deliveries = load_export_deliveries(session, redemption_id=redemption_id)
    if not deliveries:
        raise ValueError("没有可导出的关联账号")
    file_name = redemption.export_file_name
    if not file_name or Path(file_name).suffix.lower() != ".zip":
        file_name = canonical_redemption_archive_filename(redemption)
    target = export_directory / Path(file_name).name
    temporary = Path(temporary_path) if temporary_path else export_directory / f".{redemption.id}.tmp"
    temporary.parent.mkdir(parents=True, exist_ok=True)
    build_delivery_archive_to_path(
        deliveries,
        security,
        temporary,
        on_progress=on_progress,
    )
    publish_export_artifact(temporary, target)
    redemption.export_file_name = target.name
    session.flush()
    return target, len(deliveries), False


def reusable_redemption_for_redelivery(session: Session, redelivery_id: str) -> Redemption | None:
    redelivery = session.scalar(
        select(Redelivery).options(selectinload(Redelivery.items)).where(Redelivery.id == redelivery_id)
    )
    if not redelivery or not redelivery.items:
        return None
    source_ids = {item.source_redemption_id for item in redelivery.items}
    if len(source_ids) != 1:
        return None
    source = session.get(Redemption, next(iter(source_ids)))
    if not source or source.status != "completed":
        return None

    source_deliveries = _load_redemption_export_deliveries(session, source.id)
    source_scope = sorted(
        (
            delivery.account.id,
            delivery.cdk_id,
            delivery.export_format,
            delivery.export_fields or "[]",
        )
        for delivery in source_deliveries
    )
    redelivery_scope = sorted(
        (
            item.account_id,
            item.cdk_id,
            item.export_format,
            item.export_fields or "[]",
        )
        for item in redelivery.items
    )
    return source if source_scope == redelivery_scope else None


def build_redelivery_archive_to_path(
    session: Session,
    redelivery_id: str,
    security: SecurityManager,
    directory: str | Path,
    *,
    temporary_path: str | Path | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[Path, int, bool, str | None]:
    redelivery = session.get(Redelivery, redelivery_id)
    if not redelivery:
        raise ValueError("补发任务不存在")
    if redelivery.status not in {"ready", "downloaded"}:
        raise ValueError("补发任务当前不可导出")

    export_directory = Path(directory).resolve()
    export_directory.mkdir(parents=True, exist_ok=True)
    if artifact := redelivery_artifact_path(session, redelivery, export_directory):
        return artifact, redelivery.delivered_count, True, None

    source_redemption = reusable_redemption_for_redelivery(session, redelivery_id)
    if source_redemption:
        artifact, total, _ = build_redemption_archive_to_path(
            session,
            source_redemption.id,
            security,
            export_directory,
            temporary_path=temporary_path,
            on_progress=on_progress,
        )
        redelivery.export_file_name = artifact.name
        session.flush()
        return artifact, total, True, source_redemption.id

    deliveries = load_export_deliveries(session, redelivery_id=redelivery_id)
    if not deliveries:
        raise ValueError("没有可导出的关联账号")
    file_name = redelivery.export_file_name or canonical_redelivery_archive_filename(redelivery)
    target = export_directory / Path(file_name).name
    temporary = Path(temporary_path) if temporary_path else export_directory / f".{redelivery.id}.tmp"
    temporary.parent.mkdir(parents=True, exist_ok=True)
    build_delivery_archive_to_path(
        deliveries,
        security,
        temporary,
        on_progress=on_progress,
    )
    publish_export_artifact(temporary, target)
    redelivery.export_file_name = target.name
    session.flush()
    return target, len(deliveries), False, None


def persist_redelivery_download(
    session: Session,
    redelivery_id: str,
    security: SecurityManager,
    directory: str | Path,
) -> StoredExportArtifact:
    artifact, _, _, _ = build_redelivery_archive_to_path(
        session,
        redelivery_id,
        security,
        directory,
    )
    return StoredExportArtifact(artifact, export_media_type(artifact.name))
