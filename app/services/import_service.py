from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..models import Account, AccountImport, AccountImportError, OperationTask, utcnow
from ..security import SecurityManager, mask_email, token_hint
from ..time import to_china_iso
from .importers import ParseError, ParsedAccount, ParsedBatch
from .operations import add_operation_log, complete_operation_task, start_operation_task
from .validator import TokenValidator, apply_validation, validation_result_details


@dataclass
class ImportCommitResult:
    account_import: AccountImport
    account_ids: list[str]


class AccountImportService:
    def __init__(self, security: SecurityManager, validator: TokenValidator):
        self.security = security
        self.validator = validator

    @staticmethod
    def file_hash(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    @staticmethod
    def _find_existing(session: Session, record: ParsedAccount) -> Account | None:
        clauses = []
        if record.account_id:
            clauses.append(Account.account_id == record.account_id)
        if record.email and not record.account_id:
            clauses.append(Account.email == record.email)
        if not clauses:
            return None
        return session.scalar(select(Account).where(or_(*clauses)).limit(1))

    def preview(self, session: Session, batch: ParsedBatch) -> dict:
        seen_account_ids: set[str] = set()
        seen_emails: set[str] = set()
        duplicate_count = 0
        insertable_count = 0
        samples: list[dict] = []

        for record in batch.records:
            local_duplicate = bool(
                (record.account_id and record.account_id in seen_account_ids)
                or (not record.account_id and record.email and record.email in seen_emails)
            )
            if record.account_id:
                seen_account_ids.add(record.account_id)
            if record.email:
                seen_emails.add(record.email)
            existing = self._find_existing(session, record)
            duplicate = local_duplicate or existing is not None
            if duplicate:
                duplicate_count += 1
            else:
                insertable_count += 1
            if len(samples) < 8:
                samples.append(
                    {
                        "locator": record.locator,
                        "email": mask_email(record.email),
                        "duplicate": duplicate,
                        "has_access_token": bool(record.access_token),
                        "has_refresh_token": bool(record.refresh_token),
                        "access_token_hint": token_hint(record.access_token),
                        "refresh_token_hint": token_hint(record.refresh_token),
                    }
                )

        errors = [self._serialize_error(item) for item in batch.errors[:100]]
        return {
            "detected_format": batch.detected_format,
            "parsed_count": len(batch.records) + len(batch.errors),
            "insertable_count": insertable_count,
            "duplicate_count": duplicate_count,
            "failed_count": len(batch.errors),
            "samples": samples,
            "errors": errors,
        }

    @staticmethod
    def _serialize_error(error: ParseError | AccountImportError) -> dict:
        return {
            "locator": error.locator,
            "account_hint": getattr(error, "account_hint", ""),
            "error_type": error.error_type,
            "message": error.message,
        }

    def _apply_record(self, account: Account, record: ParsedAccount, *, mode: str) -> bool:
        changed = False

        def apply(name: str, value, encrypted: bool = False) -> None:
            nonlocal changed
            target = f"{name}_encrypted" if encrypted else name
            current = getattr(account, target)
            if value is None:
                return
            stored_value = self.security.encrypt(value) if encrypted else value
            should_update = mode == "replace" or current in (None, "")
            if should_update and current != stored_value:
                setattr(account, target, stored_value)
                changed = True

        apply("email", record.email)
        apply("account_id", record.account_id)
        apply("workspace_id", record.workspace_id)
        apply("client_id", record.client_id)
        apply("source", record.source)
        apply("registration_mode", record.registration_mode)
        apply("proxy_used", record.proxy_used)
        apply("password", record.password, encrypted=True)
        apply("access_token", record.access_token, encrypted=True)
        apply("refresh_token", record.refresh_token, encrypted=True)
        apply("id_token", record.id_token, encrypted=True)
        apply("session_token", record.session_token, encrypted=True)
        apply("cookies", record.cookies, encrypted=True)
        apply("registered_at", record.registered_at)
        apply("last_refresh", record.last_refresh)
        apply("expires_at", record.expires_at)
        if record.extra_data:
            current_extra = json.loads(account.extra_data or "{}")
            merged = {**current_extra, **record.extra_data}
            if merged != current_extra:
                account.extra_data = json.dumps(merged, ensure_ascii=False)
                changed = True
        if changed:
            account.version = (account.version or 0) + 1
        return changed

    def _new_account(self, record: ParsedAccount, import_id: str) -> Account:
        account = Account(
            import_id=import_id,
            status="pending_validation",
            source=record.source,
            registration_mode=record.registration_mode,
        )
        self._apply_record(account, record, mode="replace")
        account.version = 1
        return account

    def commit(
        self,
        session: Session,
        *,
        filename: str,
        content: bytes,
        batch: ParsedBatch,
        duplicate_strategy: str,
    ) -> ImportCommitResult:
        account_import = AccountImport(
            filename=filename[:255],
            file_hash=self.file_hash(content),
            detected_format=batch.detected_format,
            total_count=len(batch.records) + len(batch.errors),
            status="processing",
        )
        session.add(account_import)
        session.flush()

        for error in batch.errors:
            session.add(
                AccountImportError(
                    import_id=account_import.id,
                    locator=error.locator[:255],
                    account_hint=mask_email(error.account_hint),
                    error_type=error.error_type[:64],
                    message=error.message[:500],
                )
            )
        account_import.failed_count = len(batch.errors)

        local_account_ids: set[str] = set()
        local_emails: set[str] = set()
        imported_ids: list[str] = []
        for record in batch.records:
            duplicate_in_file = bool(
                (record.account_id and record.account_id in local_account_ids)
                or (not record.account_id and record.email and record.email in local_emails)
            )
            if record.account_id:
                local_account_ids.add(record.account_id)
            if record.email:
                local_emails.add(record.email)

            existing = self._find_existing(session, record)
            if duplicate_in_file or existing:
                if duplicate_strategy == "skip" or existing is None:
                    account_import.duplicate_count += 1
                    continue
                mode = "replace" if duplicate_strategy == "replace" and existing.status not in {"reserved", "delivered"} else "fill_missing"
                if self._apply_record(existing, record, mode=mode):
                    account_import.updated_count += 1
                    imported_ids.append(existing.id)
                else:
                    account_import.duplicate_count += 1
                continue

            account = self._new_account(record, account_import.id)
            session.add(account)
            session.flush()
            account_import.inserted_count += 1
            imported_ids.append(account.id)

        account_import.status = "validating" if imported_ids else "completed"
        if not imported_ids:
            account_import.completed_at = utcnow()
        session.flush()
        return ImportCommitResult(account_import=account_import, account_ids=imported_ids)

    def prevalidate(
        self,
        factory: sessionmaker[Session],
        import_id: str,
        account_ids: list[str],
        task_id: str | None = None,
        settings: Settings | None = None,
    ) -> None:
        valid_count = 0
        invalid_count = 0
        inconclusive_count = 0
        if task_id:
            if settings is None:
                raise ValueError("预验活任务缺少运行配置")
            with factory.begin() as session:
                task = session.get(OperationTask, task_id)
                if not task or task.status not in {"queued", "running"}:
                    return
                start_operation_task(task)
                add_operation_log(
                    session,
                    operation_type="account_import_validation",
                    outcome="started",
                    task_id=task_id,
                    resource_id=import_id,
                    message="导入后的预验活任务开始执行",
                    details={"total": task.total},
                )
        for account_id in account_ids:
            try:
                with factory.begin() as session:
                    account = session.get(Account, account_id)
                    task = session.get(OperationTask, task_id) if task_id else None
                    if task_id and (not task or task.status != "running"):
                        return
                    if not account or account.status in {"delivered", "reserved"}:
                        if task:
                            task.processed += 1
                            task.skipped_count += 1
                            add_operation_log(
                                session,
                                operation_type="account_import_validation",
                                outcome="skipped",
                                task_id=task_id,
                                resource_id=import_id,
                                account_id=account_id,
                                message="账号在预验活前已不可用",
                            )
                        continue
                    result = apply_validation(session, account, self.validator)
                    if result.outcome == "valid":
                        valid_count += 1
                    elif result.outcome == "invalid":
                        invalid_count += 1
                    else:
                        inconclusive_count += 1
                    if task:
                        task.processed += 1
                        if result.outcome == "valid":
                            task.valid_count += 1
                        elif result.outcome == "invalid":
                            task.invalid_count += 1
                        else:
                            task.inconclusive_count += 1
                        add_operation_log(
                            session,
                            operation_type="account_import_validation",
                            outcome=result.outcome,
                            task_id=task_id,
                            resource_id=import_id,
                            account_id=account.id,
                            message=result.message or f"验活结果：{result.outcome}",
                            details=validation_result_details(result),
                        )
            except Exception:
                if not task_id:
                    continue
                with factory.begin() as session:
                    task = session.get(OperationTask, task_id)
                    if not task or task.status != "running":
                        return
                    task.processed += 1
                    task.failed_count += 1
                    add_operation_log(
                        session,
                        operation_type="account_import_validation",
                        outcome="failed",
                        task_id=task_id,
                        resource_id=import_id,
                        account_id=account_id,
                        message="预验活执行异常",
                    )

        with factory.begin() as session:
            account_import = session.get(AccountImport, import_id)
            if account_import:
                account_import.valid_count += valid_count
                account_import.invalid_count += invalid_count
                account_import.inconclusive_count += inconclusive_count
                account_import.status = "completed"
                account_import.completed_at = utcnow()
                if task_id:
                    add_operation_log(
                        session,
                        operation_type="account_import",
                        outcome="completed",
                        resource_id=import_id,
                        message="账号导入及预验活已完成",
                        details={
                            "valid_count": valid_count,
                            "invalid_count": invalid_count,
                            "inconclusive_count": inconclusive_count,
                        },
                    )
            if task_id:
                task = session.get(OperationTask, task_id)
                if task and task.status == "running":
                    task.processed = max(task.processed, task.total)
                    complete_operation_task(task, settings)
                    add_operation_log(
                        session,
                        operation_type="account_import_validation",
                        outcome="completed",
                        task_id=task_id,
                        resource_id=import_id,
                        message="导入后的预验活任务已完成",
                        details={
                            "valid_count": task.valid_count,
                            "invalid_count": task.invalid_count,
                            "inconclusive_count": task.inconclusive_count,
                            "skipped_count": task.skipped_count,
                            "failed_count": task.failed_count,
                        },
                    )


def serialize_import(account_import: AccountImport, errors: list[AccountImportError] | None = None) -> dict:
    payload = {
        "id": account_import.id,
        "filename": account_import.filename,
        "detected_format": account_import.detected_format,
        "status": account_import.status,
        "total_count": account_import.total_count,
        "inserted_count": account_import.inserted_count,
        "updated_count": account_import.updated_count,
        "duplicate_count": account_import.duplicate_count,
        "failed_count": account_import.failed_count,
        "valid_count": account_import.valid_count,
        "invalid_count": account_import.invalid_count,
        "inconclusive_count": account_import.inconclusive_count,
        "created_at": to_china_iso(account_import.created_at),
        "completed_at": to_china_iso(account_import.completed_at),
    }
    if errors is not None:
        payload["errors"] = [AccountImportService._serialize_error(item) for item in errors]
    return payload
