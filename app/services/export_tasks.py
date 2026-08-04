from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..models import OperationTask, Redelivery, Redemption, utcnow
from ..security import SecurityManager
from .exporter import build_delivery_archive_to_path, load_export_deliveries
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
        display_name = f"accounts_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{task_id[:8]}.zip"
        final_path = directory / display_name
        try:
            with self.factory() as session:
                if task_type == "redemption_export":
                    redemption = session.get(Redemption, resource_id) if resource_id else None
                    if not redemption or redemption.status != "completed":
                        raise ValueError("兑换任务尚未完成")
                    deliveries = load_export_deliveries(session, redemption_id=resource_id)
                elif task_type == "redelivery_export":
                    redelivery = session.get(Redelivery, resource_id) if resource_id else None
                    if not redelivery or redelivery.status not in {"ready", "downloaded"}:
                        raise ValueError("补发任务当前不可导出")
                    deliveries = load_export_deliveries(session, redelivery_id=resource_id)
                else:
                    raise ValueError("不支持的导出任务类型")

            build_delivery_archive_to_path(
                deliveries,
                self.security,
                temporary_path,
                on_progress=lambda processed, total: self._update_progress(task_id, processed, total),
            )
            os.replace(temporary_path, final_path)
            with self.factory.begin() as session:
                task = session.get(OperationTask, task_id)
                if not task:
                    return
                task.file_name = final_path.name
                task.total = len(deliveries)
                task.processed = len(deliveries)
                complete_operation_task(task, self.settings)
                add_operation_log(
                    session,
                    operation_type=task.task_type,
                    outcome="completed",
                    task_id=task.id,
                    resource_id=task.resource_id,
                    message="导出任务已完成，可下载文件",
                    details={"total": len(deliveries), "file_name": final_path.name},
                )
        except Exception:
            temporary_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            logger.exception("export task %s failed", task_id)
            self._task_error(task_id, "导出任务执行失败，请重新发起导出")
