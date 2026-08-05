from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..models import OperationTask, utcnow
from ..security import SecurityManager
from .exporter import (
    build_redelivery_archive_to_path,
    build_redemption_archive_to_path,
)
from .operations import (
    add_operation_log,
    complete_operation_task,
    export_directory,
    start_operation_task,
)


logger = logging.getLogger(__name__)


class ExportTaskService:
    def __init__(self, factory: sessionmaker[Session], security: SecurityManager, settings: Settings):
        self.factory = factory
        self.security = security
        self.settings = settings

    def _task_error(self, task_id: str, message: str) -> None:
        with self.factory.begin() as session:
            task = session.get(OperationTask, task_id)
            if not task or task.status in {"completed", "failed"}:
                return
            complete_operation_task(task, self.settings, status="failed", error_message=message)
            add_operation_log(
                session,
                operation_type=task.task_type,
                outcome="failed",
                task_id=task.id,
                resource_id=task.resource_id,
                message=message,
            )

    def _update_progress(self, task_id: str, processed: int, total: int) -> None:
        with self.factory.begin() as session:
            task = session.get(OperationTask, task_id)
            if not task or task.status != "running":
                return
            task.total = max(task.total, total)
            task.processed = min(total, processed)
            task.updated_at = utcnow()

    def run(self, task_id: str) -> None:
        with self.factory.begin() as session:
            task = session.get(OperationTask, task_id)
            if not task or task.status not in {"queued", "running"}:
                return
            start_operation_task(task)
            add_operation_log(
                session,
                operation_type=task.task_type,
                outcome="started",
                task_id=task.id,
                resource_id=task.resource_id,
                message="导出任务开始打包",
                details={"total": task.total},
            )
            task_type = task.task_type
            resource_id = task.resource_id

        directory = export_directory(self.settings)
        temporary_path = directory / f".{task_id}.tmp"
        final_path: Path | None = None
        total = 0
        shared_artifact = False
        source_redemption_id: str | None = None
        try:
            with self.factory() as session:
                if task_type == "redemption_export":
                    if not resource_id:
                        raise ValueError("兑换任务不存在")
                    shared_artifact = True
                    final_path, total, _ = build_redemption_archive_to_path(
                        session,
                        resource_id,
                        self.security,
                        directory,
                        temporary_path=temporary_path,
                        on_progress=lambda processed, count: self._update_progress(task_id, processed, count),
                    )
                    session.commit()
                elif task_type == "redelivery_export":
                    if not resource_id:
                        raise ValueError("补发任务当前不可导出")
                    final_path, total, shared_artifact, source_redemption_id = build_redelivery_archive_to_path(
                        session,
                        resource_id,
                        self.security,
                        directory,
                        temporary_path=temporary_path,
                        on_progress=lambda processed, count: self._update_progress(task_id, processed, count),
                    )
                    session.commit()
                else:
                    raise ValueError("不支持的导出任务类型")

            if final_path is None:
                raise ValueError("导出文件生成失败")
            with self.factory.begin() as session:
                task = session.get(OperationTask, task_id)
                if not task:
                    return
                task.file_name = final_path.name
                task.total = total
                task.processed = total
                complete_operation_task(task, self.settings)
                details = {"total": total, "file_name": final_path.name}
                message = "导出任务已完成，可下载文件"
                if source_redemption_id:
                    details["source_redemption_id"] = source_redemption_id
                    details["reused_source_artifact"] = True
                    message = "补发导出已复用首次兑换文件"
                add_operation_log(
                    session,
                    operation_type=task.task_type,
                    outcome="completed",
                    task_id=task.id,
                    resource_id=task.resource_id,
                    message=message,
                    details=details,
                )
        except Exception:
            temporary_path.unlink(missing_ok=True)
            if not shared_artifact and final_path is not None:
                final_path.unlink(missing_ok=True)
            logger.exception("export task %s failed", task_id)
            self._task_error(task_id, "导出任务执行失败，请重新发起导出")
