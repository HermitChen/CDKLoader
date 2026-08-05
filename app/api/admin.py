from __future__ import annotations

import json
import secrets

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..config import Settings
from ..dependencies import get_db, get_security, get_settings_from_app, require_admin
from ..models import (
    Account,
    AccountImport,
    AccountImportError,
    CDK,
    DeliveryItem,
    OperationLog,
    OperationTask,
    Redemption,
    RedemptionCDK,
    utcnow,
)
from ..schemas import AccountExportRequest, AccountValidateRequest, AdminLoginRequest, BulkDeleteRequest, CDKGenerateRequest, CDKImportRequest
from ..security import SecurityManager
from ..services.import_service import AccountImportService, serialize_import
from ..services.importers import ImportParseException, ParsedBatch, parse_import_files
from ..services.exporter import build_account_archive
from ..services.operations import (
    add_operation_log,
    complete_operation_task,
    create_operation_task,
    serialize_operation_log,
    serialize_operation_task,
    start_operation_task,
)
from ..services.redemption import refresh_cdk_status, serialize_redemption
from ..services.validator import apply_validation, validation_result_details
from ..time import china_day_bounds_utc, to_china_iso, to_utc_naive


router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

CDK_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _generate_cdk() -> str:
    groups = ["".join(secrets.choice(CDK_ALPHABET) for _ in range(4)) for _ in range(4)]
    return "CDK-" + "-".join(groups)


def _cdk_code(cdk: CDK, security: SecurityManager | None = None) -> str | None:
    if not security or not cdk.code_encrypted:
        return None
    try:
        return security.decrypt(cdk.code_encrypted)
    except Exception:
        return None


def _serialize_cdk(
    cdk: CDK,
    security: SecurityManager | None = None,
    *,
    delivery_count: int = 0,
) -> dict:
    refresh_cdk_status(cdk)
    return {
        "id": cdk.id,
        "code": _cdk_code(cdk, security),
        "prefix": cdk.code_prefix,
        "total_quota": cdk.total_quota,
        "remaining_quota": cdk.remaining_quota,
        "reserved_quota": cdk.reserved_quota,
        "status": cdk.status,
        "expires_at": to_china_iso(cdk.expires_at),
        "account_source": cdk.account_source,
        "registration_mode": cdk.registration_mode,
        "export_format": cdk.export_format,
        "export_fields": json.loads(cdk.export_fields or "[]"),
        "can_copy": bool(cdk.code_encrypted),
        "created_at": to_china_iso(cdk.created_at),
        "delivery_count": delivery_count,
    }


def _serialize_account(account: Account, security: SecurityManager) -> dict:
    delivery_item = account.delivery_item
    related_cdk = None
    if delivery_item and delivery_item.cdk:
        related_cdk = {
            "id": delivery_item.cdk.id,
            "code": _cdk_code(delivery_item.cdk, security),
            "prefix": delivery_item.cdk.code_prefix,
            "redemption_id": delivery_item.redemption_id,
            "delivered_at": to_china_iso(delivery_item.delivered_at),
        }
    return {
        "id": account.id,
        "email": account.email,
        "account_id": account.account_id,
        "workspace_id": account.workspace_id,
        "source": account.source,
        "registration_mode": account.registration_mode,
        "status": account.status,
        "has_access_token": bool(account.access_token_encrypted),
        "has_refresh_token": bool(account.refresh_token_encrypted),
        "validated_at": to_china_iso(account.validated_at),
        "delivered_at": to_china_iso(account.delivered_at),
        "created_at": to_china_iso(account.created_at),
        "related_cdk": related_cdk,
    }


def _cdk_search_condition(db: Session, security: SecurityManager, term: str):
    digest = security.cdk_digest(term)
    exact_match = db.scalar(select(CDK.id).where(CDK.code_hmac == digest).limit(1))
    if exact_match:
        return CDK.code_hmac == digest
    return CDK.code_prefix.ilike(f"%{term}%")


@router.post("/auth/login")
def login(payload: AdminLoginRequest, request: Request):
    settings = request.app.state.settings
    security: SecurityManager = request.app.state.security
    if not security.constant_time_equal(payload.password, settings.admin_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="密码错误")
    return {"token": settings.admin_token}


@router.get("/dashboard", dependencies=[Depends(require_admin)])
def dashboard(request: Request, db: Session = Depends(get_db)):
    def count_accounts(current_status: str) -> int:
        return int(db.scalar(select(func.count()).select_from(Account).where(Account.status == current_status)) or 0)

    day_start, day_end = china_day_bounds_utc()
    return {
        "accounts": {
            "available": count_accounts("available"),
            "reserved": count_accounts("reserved"),
            "quarantined": count_accounts("quarantined"),
            "delivered": count_accounts("delivered"),
        },
        "cdk_remaining_quota": int(db.scalar(select(func.coalesce(func.sum(CDK.remaining_quota), 0))) or 0),
        "today_redemptions": int(
            db.scalar(
                select(func.count()).select_from(Redemption).where(Redemption.created_at >= day_start, Redemption.created_at < day_end)
            )
            or 0
        ),
        "validation_mode": request.app.state.settings.validation_mode,
    }


@router.get("/operation-tasks", dependencies=[Depends(require_admin)])
def list_operation_tasks(
    db: Session = Depends(get_db),
    task_type: str | None = Query(default=None, max_length=48),
    current_status: str | None = Query(default=None, alias="status", max_length=24),
    resource_id: str | None = Query(default=None, max_length=36),
    limit: int = Query(default=20, ge=0, le=200),
    offset: int = Query(default=0, ge=0),
):
    query = select(OperationTask)
    if task_type:
        query = query.where(OperationTask.task_type == task_type)
    if current_status:
        query = query.where(OperationTask.status == current_status)
    if resource_id:
        query = query.where(OperationTask.resource_id == resource_id)
    total = int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    if limit == 0:
        offset = 0
    items_query = query.order_by(OperationTask.created_at.desc()).offset(offset)
    if limit:
        items_query = items_query.limit(limit)
    items = db.scalars(items_query).all()
    return {"total": total, "items": [serialize_operation_task(item) for item in items]}


@router.get("/operation-tasks/{task_id}", dependencies=[Depends(require_admin)])
def get_operation_task(task_id: str, db: Session = Depends(get_db)):
    task = db.get(OperationTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="操作任务不存在")
    logs = db.execute(
        select(OperationLog, Account.email)
        .outerjoin(Account, Account.id == OperationLog.account_id)
        .where(OperationLog.task_id == task_id)
        .order_by(OperationLog.created_at.desc())
        .limit(100)
    ).all()
    payload = serialize_operation_task(task)
    payload["logs"] = [serialize_operation_log(log, account_email=email) for log, email in logs]
    return payload


@router.get("/operation-logs", dependencies=[Depends(require_admin)])
def list_operation_logs(
    db: Session = Depends(get_db),
    operation_type: str | None = Query(default=None, max_length=48),
    outcome: str | None = Query(default=None, max_length=32),
    task_id: str | None = Query(default=None, max_length=36),
    q: str | None = Query(default=None, max_length=320),
    limit: int = Query(default=30, ge=0, le=200),
    offset: int = Query(default=0, ge=0),
):
    query = select(OperationLog, Account.email).outerjoin(Account, Account.id == OperationLog.account_id)
    if operation_type:
        query = query.where(OperationLog.operation_type == operation_type)
    if outcome:
        query = query.where(OperationLog.outcome == outcome)
    if task_id:
        query = query.where(OperationLog.task_id == task_id)
    if q and (term := q.strip()):
        pattern = f"%{term}%"
        query = query.where(
            or_(
                OperationLog.task_id.ilike(pattern),
                OperationLog.account_id.ilike(pattern),
                OperationLog.resource_id.ilike(pattern),
                OperationLog.message.ilike(pattern),
                Account.email.ilike(pattern),
            )
        )
    count_query = query.with_only_columns(OperationLog.id).order_by(None).subquery()
    total = int(db.scalar(select(func.count()).select_from(count_query)) or 0)
    if limit == 0:
        offset = 0
    items_query = query.order_by(OperationLog.created_at.desc()).offset(offset)
    if limit:
        items_query = items_query.limit(limit)
    items = db.execute(items_query).all()
    return {
        "total": total,
        "items": [serialize_operation_log(log, account_email=email) for log, email in items],
    }


async def _read_upload(upload: UploadFile, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(1024 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="上传文件超过大小限制")
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_import_files(uploads: list[UploadFile], max_bytes: int) -> list[tuple[str, bytes]]:
    return [
        (upload.filename or "upload", await _read_upload(upload, max_bytes))
        for upload in uploads
    ]


def _parse_import_files(files: list[tuple[str, bytes]], settings: Settings) -> ParsedBatch:
    try:
        return parse_import_files(files, settings)
    except ImportParseException as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/account-imports/preview", dependencies=[Depends(require_admin)])
async def preview_account_import(
    request: Request,
    file: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    files = await _read_import_files(file, request.app.state.settings.max_upload_bytes)
    batch = _parse_import_files(files, request.app.state.settings)
    service: AccountImportService = request.app.state.import_service
    return service.preview(db, batch)


@router.post("/account-imports", dependencies=[Depends(require_admin)])
async def create_account_import(
    request: Request,
    background_tasks: BackgroundTasks,
    file: list[UploadFile] = File(...),
    duplicate_strategy: str = Form("skip"),
    prevalidate: bool = Form(True),
    db: Session = Depends(get_db),
):
    if duplicate_strategy not in {"skip", "fill_missing", "replace"}:
        raise HTTPException(status_code=400, detail="不支持的重复处理策略")
    files = await _read_import_files(file, request.app.state.settings.max_upload_bytes)
    batch = _parse_import_files(files, request.app.state.settings)
    content = b"".join(item[1] for item in files)
    service: AccountImportService = request.app.state.import_service
    result = service.commit(
        db,
        filename=", ".join(item[0] for item in files),
        content=content,
        batch=batch,
        duplicate_strategy=duplicate_strategy,
    )
    validation_task: OperationTask | None = None
    add_operation_log(
        db,
        operation_type="account_import",
        outcome="submitted",
        resource_id=result.account_import.id,
        message="账号导入已提交",
        details={
            "file_count": len(files),
            "inserted_count": result.account_import.inserted_count,
            "updated_count": result.account_import.updated_count,
            "duplicate_count": result.account_import.duplicate_count,
            "failed_count": result.account_import.failed_count,
        },
    )
    if prevalidate and result.account_ids:
        validation_task = create_operation_task(
            db,
            task_type="account_import_validation",
            resource_id=result.account_import.id,
            total=len(result.account_ids),
        )
        add_operation_log(
            db,
            operation_type="account_import_validation",
            outcome="queued",
            task_id=validation_task.id,
            resource_id=result.account_import.id,
            message="导入后的预验活任务已排队",
            details={"total": len(result.account_ids)},
        )
    db.commit()
    if validation_task:
        background_tasks.add_task(
            service.prevalidate,
            request.app.state.session_factory,
            result.account_import.id,
            result.account_ids,
            validation_task.id,
            request.app.state.settings,
        )
    elif result.account_import.status == "validating":
        result.account_import.status = "completed"
        result.account_import.completed_at = utcnow()
        db.add(result.account_import)
        add_operation_log(
            db,
            operation_type="account_import",
            outcome="completed",
            resource_id=result.account_import.id,
            message="账号导入已完成",
        )
        db.commit()
    elif result.account_import.status == "completed":
        add_operation_log(
            db,
            operation_type="account_import",
            outcome="completed",
            resource_id=result.account_import.id,
            message="账号导入已完成",
        )
        db.commit()
    payload = serialize_import(result.account_import)
    if validation_task:
        payload["validation_task_id"] = validation_task.id
    return payload


@router.get("/account-imports/{import_id}", dependencies=[Depends(require_admin)])
def get_account_import(import_id: str, db: Session = Depends(get_db)):
    account_import = db.get(AccountImport, import_id)
    if not account_import:
        raise HTTPException(status_code=404, detail="导入任务不存在")
    errors = db.scalars(
        select(AccountImportError).where(AccountImportError.import_id == import_id).limit(100)
    ).all()
    return serialize_import(account_import, errors)


@router.get("/accounts", dependencies=[Depends(require_admin)])
def list_accounts(
    db: Session = Depends(get_db),
    security: SecurityManager = Depends(get_security),
    current_status: str | None = Query(default=None, alias="status"),
    has_refresh_token: bool | None = Query(default=None),
    q: str | None = Query(default=None, max_length=320),
    cdk_id: str | None = Query(default=None, max_length=36),
    redemption_id: str | None = Query(default=None, max_length=36),
    limit: int = Query(default=15, ge=0, le=200),
    offset: int = Query(default=0, ge=0),
):
    query = _account_query(current_status, has_refresh_token, q, cdk_id, redemption_id)
    total = int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    if limit == 0:
        offset = 0
    accounts_query = query.options(selectinload(Account.delivery_item).selectinload(DeliveryItem.cdk)).offset(offset)
    if limit:
        accounts_query = accounts_query.limit(limit)
    accounts = db.scalars(accounts_query).all()
    return {"total": total, "items": [_serialize_account(item, security) for item in accounts]}


def _account_query(
    current_status: str | None,
    has_refresh_token: bool | None,
    q: str | None,
    cdk_id: str | None = None,
    redemption_id: str | None = None,
):
    query = select(Account)
    if cdk_id or redemption_id:
        query = query.join(Account.delivery_item)
    if cdk_id:
        query = query.where(DeliveryItem.cdk_id == cdk_id)
    if redemption_id:
        query = query.where(DeliveryItem.redemption_id == redemption_id)
    if current_status:
        query = query.where(Account.status == current_status)
    if has_refresh_token is True:
        query = query.where(Account.refresh_token_encrypted.is_not(None))
    elif has_refresh_token is False:
        query = query.where(Account.refresh_token_encrypted.is_(None))
    if q and (term := q.strip()):
        pattern = f"%{term}%"
        query = query.where(
            or_(
                Account.email.ilike(pattern),
                Account.account_id.ilike(pattern),
                Account.workspace_id.ilike(pattern),
                Account.source.ilike(pattern),
                Account.registration_mode.ilike(pattern),
            )
        )
    return query.order_by(Account.created_at.desc())


@router.post("/accounts/export", dependencies=[Depends(require_admin)])
def export_accounts(
    payload: AccountExportRequest,
    db: Session = Depends(get_db),
    security: SecurityManager = Depends(get_security),
):
    account_ids = set(payload.ids)
    accounts = db.scalars(
        select(Account)
        .where(Account.id.in_(account_ids))
        .order_by(Account.created_at.desc())
    ).all()
    if len(accounts) != len(account_ids):
        raise HTTPException(status_code=409, detail="部分选中账号不存在，请刷新后重试")
    content, filename, media_type = build_account_archive(accounts, security)
    add_operation_log(
        db,
        operation_type="account_export",
        outcome="completed",
        message="管理员账号导出已完成",
        details={"count": len(accounts), "filename": filename},
    )
    db.commit()
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store, private",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/accounts/bulk-delete", dependencies=[Depends(require_admin)])
def bulk_delete_accounts(payload: BulkDeleteRequest, db: Session = Depends(get_db)):
    deleted = 0
    skipped: list[dict[str, str]] = []
    account_ids = set(payload.ids)
    accounts = db.scalars(select(Account).where(Account.id.in_(account_ids))).all()
    found_ids = {account.id for account in accounts}
    for account_id in account_ids - found_ids:
        skipped.append({"id": account_id, "reason": "账号不存在"})
    for account in accounts:
        if account.status == "reserved" or account.reserved_by:
            skipped.append({"id": account.id, "reason": "账号正在被兑换任务预约"})
        else:
            if account.delivery_item:
                db.delete(account.delivery_item)
            db.delete(account)
            deleted += 1
    db.commit()
    return {"deleted": deleted, "skipped": skipped}


def _validate_accounts_task(
    factory,
    validator,
    settings: Settings,
    task_id: str,
    account_ids: list[str],
    validation_proxy: str | None = None,
    probe_mode: str = "strict",
) -> None:
    with factory.begin() as session:
        task = session.get(OperationTask, task_id)
        if not task or task.status not in {"queued", "running"}:
            return
        start_operation_task(task)
        add_operation_log(
            session,
            operation_type="manual_validation",
            outcome="started",
            task_id=task.id,
            message="批量验活任务开始执行",
            details={"total": task.total},
        )

    for account_id in account_ids:
        try:
            with factory.begin() as session:
                task = session.get(OperationTask, task_id)
                if not task or task.status != "running":
                    return
                account = session.get(Account, account_id)
                if not account:
                    task.processed += 1
                    task.skipped_count += 1
                    add_operation_log(
                        session,
                        operation_type="manual_validation",
                        outcome="skipped",
                        task_id=task_id,
                        account_id=account_id,
                        message="账号不存在",
                    )
                    continue
                if account.status in {"reserved", "delivered"}:
                    task.processed += 1
                    task.skipped_count += 1
                    add_operation_log(
                        session,
                        operation_type="manual_validation",
                        outcome="skipped",
                        task_id=task_id,
                        account_id=account.id,
                        message="账号状态不允许验活",
                        details={"status": account.status},
                    )
                    continue

                result = apply_validation(
                    session,
                    account,
                    validator,
                    validation_proxy=validation_proxy,
                    probe_mode=probe_mode,
                )
                task.processed += 1
                if result.outcome == "valid":
                    task.valid_count += 1
                elif result.outcome == "invalid":
                    task.invalid_count += 1
                else:
                    task.inconclusive_count += 1
                add_operation_log(
                    session,
                    operation_type="manual_validation",
                    outcome=result.outcome,
                    task_id=task_id,
                    account_id=account.id,
                    message=result.message or f"验活结果：{result.outcome}",
                    details=validation_result_details(result),
                )
        except Exception:
            with factory.begin() as session:
                task = session.get(OperationTask, task_id)
                if not task or task.status != "running":
                    return
                task.processed += 1
                task.failed_count += 1
                add_operation_log(
                    session,
                    operation_type="manual_validation",
                    outcome="failed",
                    task_id=task_id,
                    account_id=account_id,
                    message="验活执行异常",
                )

    with factory.begin() as session:
        task = session.get(OperationTask, task_id)
        if not task or task.status != "running":
            return
        task.processed = max(task.processed, task.total)
        complete_operation_task(task, settings)
        add_operation_log(
            session,
            operation_type="manual_validation",
            outcome="completed",
            task_id=task_id,
            message="批量验活任务已完成",
            details={
                "total": task.total,
                "valid_count": task.valid_count,
                "invalid_count": task.invalid_count,
                "inconclusive_count": task.inconclusive_count,
                "skipped_count": task.skipped_count,
                "failed_count": task.failed_count,
            },
        )


@router.post("/accounts/validate", dependencies=[Depends(require_admin)])
def validate_accounts(
    payload: AccountValidateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    account_ids = list(dict.fromkeys(payload.ids))
    accounts = db.scalars(select(Account).where(Account.id.in_(account_ids))).all()
    accounts_by_id = {account.id: account for account in accounts}
    accepted_ids: list[str] = []
    skipped: list[dict[str, str]] = []

    for account_id in account_ids:
        account = accounts_by_id.get(account_id)
        if not account:
            skipped.append({"id": account_id, "reason": "账号不存在"})
        elif account.status == "reserved":
            skipped.append({"id": account_id, "reason": "账号正在被兑换任务预约"})
        elif account.status == "delivered":
            skipped.append({"id": account_id, "reason": "账号已交付"})
        else:
            accepted_ids.append(account_id)

    task = create_operation_task(
        db,
        task_type="manual_validation",
        total=len(account_ids),
        processed=len(skipped),
        skipped_count=len(skipped),
    )
    for item in skipped:
        add_operation_log(
            db,
            operation_type="manual_validation",
            outcome="skipped",
            task_id=task.id,
            account_id=item["id"],
            message=item["reason"],
        )

    if accepted_ids:
        add_operation_log(
            db,
            operation_type="manual_validation",
            outcome="queued",
            task_id=task.id,
            message="批量验活任务已排队",
            details={"total": len(account_ids), "accepted": len(accepted_ids), "skipped": len(skipped)},
        )
    else:
        complete_operation_task(task, request.app.state.settings)
        add_operation_log(
            db,
            operation_type="manual_validation",
            outcome="completed",
            task_id=task.id,
            message="没有可验活的账号",
        )
    db.commit()
    if accepted_ids:
        background_tasks.add_task(
            _validate_accounts_task,
            request.app.state.session_factory,
            request.app.state.validator,
            request.app.state.settings,
            task.id,
            accepted_ids,
            payload.proxy,
            payload.probe_mode or "strict",
        )
    return JSONResponse(
        content={"accepted": len(accepted_ids), "skipped": skipped},
        headers={"X-Operation-Task-ID": task.id},
    )


@router.post("/cdks/generate", dependencies=[Depends(require_admin)])
def generate_cdks(payload: CDKGenerateRequest, db: Session = Depends(get_db), security: SecurityManager = Depends(get_security)):
    if payload.expires_at and to_utc_naive(payload.expires_at) <= utcnow():
        raise HTTPException(status_code=400, detail="有效期必须晚于当前时间")
    codes: list[str] = []
    items: list[CDK] = []
    for _ in range(payload.count):
        for _attempt in range(10):
            code = _generate_cdk()
            digest = security.cdk_digest(code)
            if not db.scalar(select(CDK.id).where(CDK.code_hmac == digest)):
                break
        else:
            raise HTTPException(status_code=500, detail="生成 CDK 冲突过多")
        item = CDK(
            code_hmac=digest,
            code_encrypted=security.encrypt(code),
            code_prefix="-".join(code.split("-")[:2]),
            total_quota=payload.quota,
            remaining_quota=payload.quota,
            expires_at=to_utc_naive(payload.expires_at) if payload.expires_at else None,
            account_source=payload.account_source,
            registration_mode=payload.registration_mode,
            export_format=payload.export_format,
            export_fields=json.dumps(payload.export_fields, ensure_ascii=False),
        )
        db.add(item)
        codes.append(code)
        items.append(item)
    db.commit()
    return {"codes": codes, "items": [_serialize_cdk(item) for item in items]}


@router.post("/cdks/import", dependencies=[Depends(require_admin)])
def import_cdks(payload: CDKImportRequest, db: Session = Depends(get_db), security: SecurityManager = Depends(get_security)):
    created = 0
    duplicates = 0
    for raw in payload.codes:
        code = raw.strip().upper()
        if not code:
            continue
        digest = security.cdk_digest(code)
        if db.scalar(select(CDK.id).where(CDK.code_hmac == digest)):
            duplicates += 1
            continue
        db.add(
            CDK(
                code_hmac=digest,
                code_encrypted=security.encrypt(code),
                code_prefix="-".join(code.split("-")[:2])[:16],
                total_quota=payload.quota,
                remaining_quota=payload.quota,
                expires_at=to_utc_naive(payload.expires_at) if payload.expires_at else None,
            )
        )
        created += 1
    db.commit()
    return {"created": created, "duplicates": duplicates}


@router.get("/cdks", dependencies=[Depends(require_admin)])
def list_cdks(
    db: Session = Depends(get_db),
    security: SecurityManager = Depends(get_security),
    current_status: str | None = Query(default=None, alias="status"),
    q: str | None = Query(default=None, max_length=64),
    quota: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=15, ge=0, le=500),
    offset: int = Query(default=0, ge=0),
):
    query = select(CDK).order_by(CDK.created_at.desc())
    if current_status:
        query = query.where(CDK.status == current_status)
    if q and (term := q.strip().upper()):
        query = query.where(_cdk_search_condition(db, security, term))
    if quota:
        term = quota.strip()
        if "/" in term:
            parts = [part.strip() for part in term.split("/")]
            if len(parts) != 2 or not all(part.isdecimal() for part in parts):
                raise HTTPException(status_code=422, detail="额度筛选请输入 10 或 0/10")
            query = query.where(CDK.remaining_quota == int(parts[0]), CDK.total_quota == int(parts[1]))
        elif term.isdecimal():
            query = query.where(CDK.total_quota == int(term))
        else:
            raise HTTPException(status_code=422, detail="额度筛选请输入 10 或 0/10")
    total = int(db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0)
    if limit == 0:
        offset = 0
    items_query = query.offset(offset)
    if limit:
        items_query = items_query.limit(limit)
    items = db.scalars(items_query).all()
    delivery_counts: dict[str, int] = {}
    if items:
        delivery_counts = dict(
            db.execute(
                select(DeliveryItem.cdk_id, func.count(DeliveryItem.id))
                .where(DeliveryItem.cdk_id.in_([item.id for item in items]))
                .group_by(DeliveryItem.cdk_id)
            ).all()
        )
    return {
        "total": total,
        "items": [_serialize_cdk(item, security, delivery_count=delivery_counts.get(item.id, 0)) for item in items],
    }


@router.post("/cdks/copy", dependencies=[Depends(require_admin)])
def copy_cdks(payload: BulkDeleteRequest, db: Session = Depends(get_db), security: SecurityManager = Depends(get_security)):
    codes: list[str] = []
    unavailable_ids: list[str] = []
    requested_ids = set(payload.ids)
    cdks = db.scalars(select(CDK).where(CDK.id.in_(requested_ids)).order_by(CDK.created_at.desc())).all()
    for cdk in cdks:
        if not cdk.code_encrypted:
            unavailable_ids.append(cdk.id)
            continue
        try:
            codes.append(security.decrypt(cdk.code_encrypted))
        except Exception:
            unavailable_ids.append(cdk.id)
    unavailable_ids.extend(requested_ids - {cdk.id for cdk in cdks})
    return {"codes": codes, "unavailable_ids": unavailable_ids}


@router.post("/cdks/reissue", dependencies=[Depends(require_admin)])
def reissue_cdks(payload: BulkDeleteRequest, db: Session = Depends(get_db), security: SecurityManager = Depends(get_security)):
    codes: list[str] = []
    items: list[CDK] = []
    skipped: list[dict[str, str]] = []
    requested_ids = list(dict.fromkeys(payload.ids))
    cdks = db.scalars(select(CDK).where(CDK.id.in_(requested_ids))).all()
    cdks_by_id = {cdk.id: cdk for cdk in cdks}

    for cdk_id in requested_ids:
        cdk = cdks_by_id.get(cdk_id)
        if not cdk:
            skipped.append({"id": cdk_id, "reason": "CDK 不存在"})
            continue
        refresh_cdk_status(cdk)
        has_redemption = db.scalar(select(RedemptionCDK.id).where(RedemptionCDK.cdk_id == cdk.id).limit(1))
        if cdk.code_encrypted:
            skipped.append({"id": cdk.id, "reason": "CDK 已支持复制"})
            continue
        if cdk.reserved_quota > 0:
            skipped.append({"id": cdk.id, "reason": "CDK 额度已被冻结"})
            continue
        if has_redemption or cdk.status != "unused" or cdk.remaining_quota != cdk.total_quota:
            skipped.append({"id": cdk.id, "reason": "仅可重新签发从未使用的历史 CDK"})
            continue

        for _attempt in range(10):
            code = _generate_cdk()
            digest = security.cdk_digest(code)
            if not db.scalar(select(CDK.id).where(CDK.code_hmac == digest)):
                break
        else:
            raise HTTPException(status_code=500, detail="重新签发 CDK 冲突过多")

        cdk.code_hmac = digest
        cdk.code_encrypted = security.encrypt(code)
        cdk.code_prefix = "-".join(code.split("-")[:2])
        cdk.updated_at = utcnow()
        codes.append(code)
        items.append(cdk)

    db.commit()
    return {"codes": codes, "items": [_serialize_cdk(item) for item in items], "skipped": skipped}


@router.post("/cdks/bulk-delete", dependencies=[Depends(require_admin)])
def bulk_delete_cdks(payload: BulkDeleteRequest, db: Session = Depends(get_db)):
    deleted = 0
    skipped: list[dict[str, str]] = []
    cdk_ids = set(payload.ids)
    cdks = db.scalars(select(CDK).where(CDK.id.in_(cdk_ids))).all()
    found_ids = {cdk.id for cdk in cdks}
    for cdk_id in cdk_ids - found_ids:
        skipped.append({"id": cdk_id, "reason": "CDK 不存在"})
    for cdk in cdks:
        if cdk.reserved_quota > 0:
            skipped.append({"id": cdk.id, "reason": "CDK 额度已被冻结"})
            continue

        active_redemption = db.scalar(
            select(Redemption.id)
            .join(RedemptionCDK)
            .where(RedemptionCDK.cdk_id == cdk.id, Redemption.status.in_({"queued", "processing"}))
            .limit(1)
        )
        if active_redemption:
            skipped.append({"id": cdk.id, "reason": "CDK 正被执行中的兑换任务使用"})
            continue

        relations = db.scalars(select(RedemptionCDK).where(RedemptionCDK.cdk_id == cdk.id)).all()
        redemption_ids = {relation.redemption_id for relation in relations}
        delivery_items = db.scalars(select(DeliveryItem).where(DeliveryItem.cdk_id == cdk.id)).all()
        for item in delivery_items:
            db.delete(item)
        for relation in relations:
            db.delete(relation)
        db.flush()
        for redemption_id in redemption_ids:
            remaining_relation = db.scalar(
                select(RedemptionCDK.id).where(RedemptionCDK.redemption_id == redemption_id).limit(1)
            )
            if not remaining_relation and (redemption := db.get(Redemption, redemption_id)):
                db.delete(redemption)
        db.delete(cdk)
        deleted += 1
    db.commit()
    return {"deleted": deleted, "skipped": skipped}


@router.get("/redemptions", dependencies=[Depends(require_admin)])
def list_redemptions(
    request: Request,
    db: Session = Depends(get_db),
    current_status: str | None = Query(default=None, alias="status"),
    q: str | None = Query(default=None, max_length=64),
    today: bool = Query(default=False),
    limit: int = Query(default=15, ge=0, le=500),
    offset: int = Query(default=0, ge=0),
):
    query = select(Redemption).options(selectinload(Redemption.cdks).selectinload(RedemptionCDK.cdk))
    if today:
        day_start, day_end = china_day_bounds_utc()
        query = query.where(Redemption.created_at >= day_start, Redemption.created_at < day_end)
    if current_status:
        query = query.where(Redemption.status == current_status)
    if q and (term := q.strip().upper()):
        pattern = f"%{term}%"
        query = query.outerjoin(RedemptionCDK).outerjoin(CDK).where(
            or_(Redemption.id.ilike(pattern), _cdk_search_condition(db, request.app.state.security, term))
        ).distinct()
    total = int(db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0)
    if limit == 0:
        offset = 0
    items_query = query.order_by(Redemption.created_at.desc()).offset(offset)
    if limit:
        items_query = items_query.limit(limit)
    items = db.scalars(items_query).unique().all()
    return {
        "total": total,
        "items": [serialize_redemption(item, request.app.state.security, include_cdk_codes=True) for item in items],
    }


@router.post("/redemptions/bulk-delete", dependencies=[Depends(require_admin)])
def bulk_delete_redemptions(payload: BulkDeleteRequest, db: Session = Depends(get_db)):
    deleted = 0
    skipped: list[dict[str, str]] = []
    redemption_ids = set(payload.ids)
    redemptions = db.scalars(
        select(Redemption)
        .options(selectinload(Redemption.cdks), selectinload(Redemption.delivery_items))
        .where(Redemption.id.in_(redemption_ids))
    ).all()
    found_ids = {redemption.id for redemption in redemptions}
    for redemption_id in redemption_ids - found_ids:
        skipped.append({"id": redemption_id, "reason": "兑换记录不存在"})
    for redemption in redemptions:
        if redemption.status in {"queued", "processing"}:
            skipped.append({"id": redemption.id, "reason": "兑换任务正在执行"})
        else:
            db.delete(redemption)
            deleted += 1
    db.commit()
    return {"deleted": deleted, "skipped": skipped}
