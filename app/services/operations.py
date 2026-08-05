from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..config import PROJECT_ROOT, Settings
from ..models import OperationLog, OperationTask, Redelivery, RedeliveryItem, Redemption, utcnow
from ..time import to_china_iso


logger = logging.getLogger(__name__)


def create_operation_task(
    session: Session,
    *,
    task_type: str,
    total: int = 0,
    resource_id: str | None = None,
    processed: int = 0,
    skipped_count: int = 0,
) -> OperationTask:
    task = OperationTask(
        task_type=task_type,
        status="queued",
        resource_id=resource_id,
        total=max(0, total),
        processed=max(0, processed),
        skipped_count=max(0, skipped_count),
    )
    session.add(task)
    session.flush()
    return task


def add_operation_log(
    session: Session,
    *,
    operation_type: str,
    outcome: str,
    message: str = "",
    task_id: str | None = None,
    account_id: str | None = None,
    resource_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> OperationLog:
    safe_details = details or {}
    log = OperationLog(
        task_id=task_id,
        operation_type=operation_type,
        outcome=outcome,
        account_id=account_id,
        resource_id=resource_id,
        message=(message or "")[:500],
        details=json.dumps(safe_details, ensure_ascii=False, separators=(",", ":")),
    )
    session.add(log)
    return log


def task_expiry(settings: Settings, completed_at: datetime | None = None) -> datetime:
    finished = completed_at or utcnow()
    return finished + timedelta(days=max(1, settings.operation_log_retention_days))


def start_operation_task(task: OperationTask, started_at: datetime | None = None) -> None:
    task.status = "running"
    task.started_at = started_at or utcnow()
    task.updated_at = utcnow()


def complete_operation_task(
    task: OperationTask,
    settings: Settings,
    *,
    status: str = "completed",
    error_message: str | None = None,
    completed_at: datetime | None = None,
) -> None:
    finished = completed_at or utcnow()
    task.status = status
    task.completed_at = finished
    task.expires_at = task_expiry(settings, finished)
    task.error_message = (error_message or "")[:500] or None
    task.updated_at = finished


def task_progress(task: OperationTask) -> int:
    if task.total <= 0:
        return 100 if task.status in {"completed", "failed"} else 0
    return min(100, max(0, int(task.processed * 100 / task.total)))


def serialize_operation_task(task: OperationTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "task_type": task.task_type,
        "status": task.status,
        "resource_id": task.resource_id,
        "total": task.total,
        "processed": task.processed,
        "percent": task_progress(task),
        "valid_count": task.valid_count,
        "invalid_count": task.invalid_count,
        "inconclusive_count": task.inconclusive_count,
        "skipped_count": task.skipped_count,
        "failed_count": task.failed_count,
        "file_name": task.file_name,
        "error_message": task.error_message,
        "created_at": to_china_iso(task.created_at),
        "started_at": to_china_iso(task.started_at),
        "completed_at": to_china_iso(task.completed_at),
        "expires_at": to_china_iso(task.expires_at),
        "downloaded_at": to_china_iso(task.downloaded_at),
    }


def serialize_operation_log(log: OperationLog, *, account_email: str | None = None) -> dict[str, Any]:
    try:
        details = json.loads(log.details or "{}")
    except (TypeError, json.JSONDecodeError):
        details = {}
    return {
        "id": log.id,
        "task_id": log.task_id,
        "operation_type": log.operation_type,
        "outcome": log.outcome,
        "account_id": log.account_id,
        "account_email": account_email,
        "resource_id": log.resource_id,
        "message": log.message,
        "details": details if isinstance(details, dict) else {},
        "created_at": to_china_iso(log.created_at),
    }


def export_directory(settings: Settings) -> Path:
    configured = settings.export_dir.strip() if settings.export_dir else ""
    directory = Path(configured).expanduser() if configured else PROJECT_ROOT / "data" / "exports"
    directory.mkdir(parents=True, exist_ok=True)
    return directory.resolve()


def _safe_artifact_path(directory: Path, file_name: str | None) -> Path | None:
    if not file_name:
        return None
    candidate = (directory / Path(file_name).name).resolve()
    try:
        candidate.relative_to(directory.resolve())
    except ValueError:
        return None
    return candidate


def _referenced_artifact_names(factory: sessionmaker[Session]) -> set[str]:
    now = utcnow()
    with factory() as session:
        names = set(
            name
            for name in session.scalars(
                select(Redemption.export_file_name)
                .join(RedeliveryItem, RedeliveryItem.source_redemption_id == Redemption.id)
                .join(Redelivery, Redelivery.id == RedeliveryItem.redelivery_id)
                .where(
                    Redelivery.recovery_expires_at > now,
                    Redemption.export_file_name.is_not(None),
                )
                .distinct()
            ).all()
            if name
        )
        names.update(
            name
            for name in session.scalars(
                select(Redelivery.export_file_name).where(
                    Redelivery.recovery_expires_at > now,
                    Redelivery.export_file_name.is_not(None),
                )
            ).all()
            if name
        )
        names.update(
            name
            for name in session.scalars(
                select(OperationTask.file_name).where(
                    OperationTask.file_name.is_not(None),
                    (
                        OperationTask.expires_at.is_(None)
                        | (OperationTask.expires_at > now)
                        | OperationTask.status.in_({"queued", "running"})
                    ),
                )
            ).all()
            if name
        )
        return names


def mark_interrupted_tasks(factory: sessionmaker[Session], settings: Settings) -> int:
    interrupted = 0
    with factory.begin() as session:
        tasks = session.scalars(
            select(OperationTask).where(OperationTask.status.in_({"queued", "running"}))
        ).all()
        for task in tasks:
            complete_operation_task(task, settings, status="failed", error_message="服务重启，任务已中断")
            add_operation_log(
                session,
                operation_type=task.task_type,
                outcome="failed",
                task_id=task.id,
                resource_id=task.resource_id,
                message="服务重启，任务已中断",
            )
            interrupted += 1
    return interrupted


def cleanup_operation_data(
    factory: sessionmaker[Session],
    settings: Settings,
) -> tuple[int, int]:
    """Delete expired task/log records and generated export artifacts."""
    now = utcnow()
    cutoff = now - timedelta(days=max(1, settings.operation_log_retention_days))
    directory = export_directory(settings)
    artifact_paths: list[Path] = []
    deleted_logs = 0
    deleted_tasks = 0

    with factory.begin() as session:
        old_logs = session.scalars(select(OperationLog).where(OperationLog.created_at < cutoff)).all()
        for log in old_logs:
            session.delete(log)
            deleted_logs += 1

        expired_tasks = session.scalars(
            select(OperationTask).where(
                OperationTask.expires_at.is_not(None),
                OperationTask.expires_at <= now,
            )
        ).all()
        expired_task_ids = {task.id for task in expired_tasks}
        for task in expired_tasks:
            if artifact := _safe_artifact_path(directory, task.file_name):
                artifact_paths.append(artifact)
            session.delete(task)
            deleted_tasks += 1
        if artifact_paths:
            candidate_names = {artifact.name for artifact in artifact_paths}
            referenced_names = set(
                name
                for name in session.scalars(
                    select(Redemption.export_file_name).where(
                        Redemption.export_file_name.in_(candidate_names)
                    )
                ).all()
                if name
            )
            referenced_names.update(
                name
                for name in session.scalars(
                    select(Redelivery.export_file_name).where(
                        Redelivery.export_file_name.in_(candidate_names)
                    )
                ).all()
                if name
            )
            if expired_task_ids:
                referenced_names.update(
                    name
                    for name in session.scalars(
                        select(OperationTask.file_name).where(
                            OperationTask.file_name.in_(candidate_names),
                            ~OperationTask.id.in_(expired_task_ids),
                        )
                    ).all()
                    if name
                )
            artifact_paths = [artifact for artifact in artifact_paths if artifact.name not in referenced_names]

    for artifact in artifact_paths:
        try:
            artifact.unlink(missing_ok=True)
        except OSError:
            logger.warning("unable to remove expired export artifact %s", artifact)

    referenced_names = _referenced_artifact_names(factory)
    artifact_cutoff = datetime.now().timestamp() - max(300, settings.export_retention_seconds)
    try:
        for artifact in directory.iterdir():
            if not artifact.is_file() or artifact.suffix.lower() not in {".zip", ".tmp", ".csv", ".txt", ".json"}:
                continue
            if artifact.name in referenced_names:
                continue
            try:
                if artifact.stat().st_mtime < artifact_cutoff:
                    artifact.unlink(missing_ok=True)
            except OSError:
                logger.warning("unable to remove stale export artifact %s", artifact)
    except OSError:
        logger.warning("unable to scan export directory %s", directory)

    return deleted_tasks, deleted_logs
