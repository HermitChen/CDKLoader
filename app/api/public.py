from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..dependencies import get_db
from ..models import OperationTask, Redelivery, Redemption, RedemptionCDK, utcnow
from ..schemas import RedemptionCreateRequest
from ..services.exporter import export_media_type, persist_redelivery_download, persist_redemption_download
from ..services.operations import add_operation_log, create_operation_task, export_directory, serialize_operation_task
from ..services.redemption import RedemptionError, RedemptionService, serialize_redelivery, serialize_redemption


router = APIRouter(prefix="/api/v1", tags=["public"])


def _load_redemption(db: Session, redemption_id: str) -> Redemption | None:
    return db.scalar(
        select(Redemption)
        .options(selectinload(Redemption.cdks).selectinload(RedemptionCDK.cdk))
        .where(Redemption.id == redemption_id)
    )


def _load_redelivery(db: Session, redelivery_id: str) -> Redelivery | None:
    return db.scalar(
        select(Redelivery)
        .options(selectinload(Redelivery.items))
        .where(Redelivery.id == redelivery_id)
    )


def _ensure_task_token(request: Request | WebSocket, redemption: Redemption, task_token: str) -> None:
    expected = request.app.state.security.redemption_token(redemption.id, redemption.idempotency_key)
    if not task_token or not request.app.state.security.constant_time_equal(task_token, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="任务凭证无效")


def _ensure_redelivery_token(request: Request | WebSocket, redelivery: Redelivery, task_token: str) -> None:
    expected = request.app.state.security.redelivery_token(redelivery.id, redelivery.idempotency_key)
    if not task_token or not request.app.state.security.constant_time_equal(task_token, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="补发凭证无效")


def _ensure_redelivery_window(db: Session, redelivery: Redelivery) -> None:
    if redelivery.status == "expired" or redelivery.recovery_expires_at <= utcnow():
        redelivery.status = "expired"
        db.commit()
        raise HTTPException(status_code=410, detail="CDK 已兑换，补发时效已过，请联系管理员")


def _schedule_task(request: Request, redemption_id: str) -> None:
    request.app.state.redemption_service.process(redemption_id)


def _track_task(app, task: asyncio.Task) -> None:
    app.state.running_tasks.add(task)
    task.add_done_callback(app.state.running_tasks.discard)


def _load_export_task(
    db: Session,
    task_id: str,
    *,
    task_type: str,
    resource_id: str,
) -> OperationTask | None:
    return db.scalar(
        select(OperationTask).where(
            OperationTask.id == task_id,
            OperationTask.task_type == task_type,
            OperationTask.resource_id == resource_id,
        )
    )


def _export_payload(request: Request | WebSocket, task: OperationTask, *, resource_path: str, token: str) -> dict:
    payload = serialize_operation_task(task)
    payload["download_url"] = None
    if task.status == "completed" and task.file_name:
        payload["download_url"] = (
            f"/api/v1/{resource_path}/{task.id}/download?token={task_token_quote(token)}"
        )
    return payload


def task_token_quote(token: str) -> str:
    from urllib.parse import quote

    return quote(token, safe="")


async def _schedule_export_task(request: Request, task_id: str) -> None:
    task = asyncio.create_task(asyncio.to_thread(request.app.state.export_service.run, task_id))
    _track_task(request.app, task)


async def _begin_export_task(
    request: Request,
    db: Session,
    *,
    resource_id: str,
    task_token: str,
    task_type: str,
    total: int,
    resource_path: str,
) -> JSONResponse:
    current = db.scalar(
        select(OperationTask)
        .where(OperationTask.task_type == task_type, OperationTask.resource_id == resource_id)
        .order_by(OperationTask.created_at.desc())
    )
    if current and current.status in {"queued", "running"}:
        return JSONResponse(
            status_code=202,
            content=_export_payload(request, current, resource_path=resource_path, token=task_token),
        )
    if current and current.status == "completed" and current.file_name:
        artifact = export_directory(request.app.state.settings) / current.file_name
        if artifact.is_file():
            return JSONResponse(
                status_code=200,
                content=_export_payload(request, current, resource_path=resource_path, token=task_token),
            )

    task = create_operation_task(
        db,
        task_type=task_type,
        resource_id=resource_id,
        total=total,
    )
    add_operation_log(
        db,
        operation_type=task_type,
        outcome="queued",
        task_id=task.id,
        resource_id=resource_id,
        message="导出任务已排队",
        details={"total": total},
    )
    db.commit()
    await _schedule_export_task(request, task.id)
    return JSONResponse(
        status_code=202,
        content=_export_payload(request, task, resource_path=resource_path, token=task_token),
    )


async def _export_events_response(
    request: Request,
    *,
    task_id: str,
    resource_id: str,
    task_type: str,
    task_token: str,
    resource_path: str,
    validate_token,
):
    with request.app.state.session_factory() as db:
        resource = db.get(Redemption if task_type == "redemption_export" else Redelivery, resource_id)
        if not resource:
            raise HTTPException(status_code=404, detail="导出资源不存在")
        validate_token(request, resource, task_token)
        if task_type == "redelivery_export" and isinstance(resource, Redelivery):
            _ensure_redelivery_window(db, resource)
        task = _load_export_task(db, task_id, task_type=task_type, resource_id=resource_id)
        if not task:
            raise HTTPException(status_code=404, detail="导出任务不存在")

    async def event_stream() -> AsyncIterator[str]:
        previous_signature = None
        for _ in range(3600):
            if await request.is_disconnected():
                return
            with request.app.state.session_factory() as stream_db:
                current = _load_export_task(
                    stream_db,
                    task_id,
                    task_type=task_type,
                    resource_id=resource_id,
                )
                if not current:
                    return
                payload = _export_payload(
                    request,
                    current,
                    resource_path=resource_path,
                    token=task_token,
                )
            signature = (
                payload["status"],
                payload["processed"],
                payload["total"],
                payload["file_name"],
                payload["error_message"],
            )
            if signature != previous_signature:
                yield f"event: export\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                previous_signature = signature
            if payload["status"] in {"completed", "failed"}:
                return
            await asyncio.sleep(0.8)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


async def _export_websocket_response(
    websocket: WebSocket,
    *,
    task_id: str,
    resource_id: str,
    task_type: str,
    task_token: str,
    resource_path: str,
    validate_token,
) -> None:
    """Stream export progress over a long-lived connection without a request timeout."""
    await websocket.accept()
    try:
        with websocket.app.state.session_factory() as db:
            resource = db.get(Redemption if task_type == "redemption_export" else Redelivery, resource_id)
            if not resource:
                await websocket.close(code=1008, reason="导出资源不存在")
                return
            try:
                validate_token(websocket, resource, task_token)
                if task_type == "redelivery_export" and isinstance(resource, Redelivery):
                    _ensure_redelivery_window(db, resource)
            except HTTPException as exc:
                await websocket.close(code=1008, reason=str(exc.detail)[:123])
                return
            task = _load_export_task(db, task_id, task_type=task_type, resource_id=resource_id)
            if not task:
                await websocket.close(code=1008, reason="导出任务不存在")
                return

        previous_signature = None
        for _ in range(3600):
            with websocket.app.state.session_factory() as stream_db:
                current = _load_export_task(
                    stream_db,
                    task_id,
                    task_type=task_type,
                    resource_id=resource_id,
                )
                if not current:
                    await websocket.close(code=1008, reason="导出任务不存在")
                    return
                payload = _export_payload(
                    websocket,
                    current,
                    resource_path=resource_path,
                    token=task_token,
                )
            signature = (
                payload["status"],
                payload["processed"],
                payload["total"],
                payload["file_name"],
                payload["error_message"],
            )
            if signature != previous_signature:
                await websocket.send_json(payload)
                previous_signature = signature
            else:
                await websocket.send_json({"heartbeat": True, **payload})
            if payload["status"] in {"completed", "failed"}:
                await websocket.close(code=1000)
                return
            await asyncio.sleep(0.8)
        await websocket.close(code=1013, reason="导出任务等待超时")
    except WebSocketDisconnect:
        return


def _export_download_response(
    request: Request,
    db: Session,
    *,
    resource_id: str,
    task_id: str,
    task_token: str,
    task_type: str,
    validate_token,
) -> FileResponse:
    resource = db.get(Redemption if task_type == "redemption_export" else Redelivery, resource_id)
    if not resource:
        raise HTTPException(status_code=404, detail="导出资源不存在")
    validate_token(request, resource, task_token)
    if task_type == "redelivery_export" and isinstance(resource, Redelivery):
        _ensure_redelivery_window(db, resource)
    task = _load_export_task(db, task_id, task_type=task_type, resource_id=resource_id)
    if not task:
        raise HTTPException(status_code=404, detail="导出任务不存在")
    if task.status != "completed" or not task.file_name:
        raise HTTPException(status_code=409, detail="导出任务尚未完成")
    artifact = export_directory(request.app.state.settings) / task.file_name
    if not artifact.is_file():
        raise HTTPException(status_code=410, detail="导出文件已过期，请重新发起导出")
    task.downloaded_at = utcnow()
    if task_type == "redelivery_export" and isinstance(resource, Redelivery):
        resource.status = "downloaded"
        resource.downloaded_at = resource.downloaded_at or utcnow()
    db.commit()
    return FileResponse(
        artifact,
        media_type=export_media_type(task.file_name),
        filename=task.file_name,
        headers={
            "Cache-Control": "private, max-age=0, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/redemptions")
async def create_redemption(
    payload: RedemptionCreateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    prefer: str | None = Header(default=None, alias="Prefer"),
):
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="缺少 Idempotency-Key 请求头")
    if len(idempotency_key) > 128:
        raise HTTPException(status_code=400, detail="Idempotency-Key 过长")
    service: RedemptionService = request.app.state.redemption_service
    try:
        redemption = service.create(
            db,
            codes=payload.codes,
            idempotency_key=idempotency_key,
            client_ip=request.client.host if request.client else "unknown",
        )
    except RedemptionError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": exc.message, "details": exc.details}) from exc

    if isinstance(redemption, Redelivery):
        add_operation_log(
            db,
            operation_type="redemption",
            outcome="redelivery_ready",
            resource_id=redemption.id,
            message="兑换请求已创建补发任务",
            details={"delivery_type": "redelivery", "requested_count": redemption.delivered_count},
        )
        db.commit()
        db.expire_all()
        current_redelivery = _load_redelivery(db, redemption.id)
        if not current_redelivery:
            raise HTTPException(status_code=500, detail="补发任务创建失败")
        return JSONResponse(status_code=200, content=serialize_redelivery(current_redelivery, request.app.state.security))

    add_operation_log(
        db,
        operation_type="redemption",
        outcome=redemption.status,
        resource_id=redemption.id,
        message="兑换请求已创建",
        details={"requested_count": redemption.requested_count, "input_count": redemption.input_count},
    )
    db.commit()
    wait_requested = bool(prefer and "wait=3" in prefer)
    if redemption.status == "queued" and wait_requested:
        task = asyncio.create_task(asyncio.to_thread(service.process, redemption.id))
        _track_task(request.app, task)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=3)
        except TimeoutError:
            pass
    elif redemption.status == "queued":
        background_tasks.add_task(_schedule_task, request, redemption.id)

    db.expire_all()
    current = _load_redemption(db, redemption.id)
    if not current:
        raise HTTPException(status_code=500, detail="兑换任务创建失败")
    body = serialize_redemption(current, request.app.state.security)
    return JSONResponse(status_code=200 if current.status in {"completed", "failed"} else 202, content=body)


@router.get("/redemptions/{redemption_id}")
def get_redemption(
    redemption_id: str,
    request: Request,
    task_token: str = Query(alias="token"),
    db: Session = Depends(get_db),
):
    redemption = _load_redemption(db, redemption_id)
    if not redemption:
        raise HTTPException(status_code=404, detail="兑换任务不存在")
    _ensure_task_token(request, redemption, task_token)
    return serialize_redemption(redemption, request.app.state.security)


@router.get("/redemptions/{redemption_id}/events")
async def redemption_events(
    redemption_id: str,
    request: Request,
    task_token: str = Query(alias="token"),
):
    with request.app.state.session_factory() as db:
        redemption = _load_redemption(db, redemption_id)
        if not redemption:
            raise HTTPException(status_code=404, detail="兑换任务不存在")
        _ensure_task_token(request, redemption, task_token)

    async def event_stream() -> AsyncIterator[str]:
        previous_status = ""
        for _ in range(30):
            with request.app.state.session_factory() as stream_db:
                current = _load_redemption(stream_db, redemption_id)
                if not current:
                    return
                payload = serialize_redemption(current, request.app.state.security)
            if payload["status"] != previous_status or payload["status"] in {"completed", "failed"}:
                yield f"event: redemption\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                previous_status = payload["status"]
            if payload["status"] in {"completed", "failed"}:
                return
            await asyncio.sleep(1)

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


@router.post("/redemptions/{redemption_id}/export")
async def create_redemption_export(
    redemption_id: str,
    request: Request,
    task_token: str = Query(alias="token"),
    db: Session = Depends(get_db),
):
    redemption = _load_redemption(db, redemption_id)
    if not redemption:
        raise HTTPException(status_code=404, detail="兑换任务不存在")
    _ensure_task_token(request, redemption, task_token)
    if redemption.status != "completed":
        raise HTTPException(status_code=409, detail="兑换任务尚未完成")
    return await _begin_export_task(
        request,
        db,
        resource_id=redemption_id,
        task_token=task_token,
        task_type="redemption_export",
        total=redemption.delivered_count,
        resource_path=f"redemptions/{redemption_id}/export",
    )


@router.get("/redemptions/{redemption_id}/export/{export_task_id}")
def get_redemption_export(
    redemption_id: str,
    export_task_id: str,
    request: Request,
    task_token: str = Query(alias="token"),
    db: Session = Depends(get_db),
):
    redemption = _load_redemption(db, redemption_id)
    if not redemption:
        raise HTTPException(status_code=404, detail="兑换任务不存在")
    _ensure_task_token(request, redemption, task_token)
    task = _load_export_task(db, export_task_id, task_type="redemption_export", resource_id=redemption_id)
    if not task:
        raise HTTPException(status_code=404, detail="导出任务不存在")
    return _export_payload(
        request,
        task,
        resource_path=f"redemptions/{redemption_id}/export",
        token=task_token,
    )


@router.get("/redemptions/{redemption_id}/export/{export_task_id}/events")
async def redemption_export_events(
    redemption_id: str,
    export_task_id: str,
    request: Request,
    task_token: str = Query(alias="token"),
):
    return await _export_events_response(
        request,
        task_id=export_task_id,
        resource_id=redemption_id,
        task_type="redemption_export",
        task_token=task_token,
        resource_path=f"redemptions/{redemption_id}/export",
        validate_token=_ensure_task_token,
    )


@router.websocket("/redemptions/{redemption_id}/export/{export_task_id}/ws")
async def redemption_export_websocket(
    redemption_id: str,
    export_task_id: str,
    websocket: WebSocket,
    task_token: str = Query(alias="token"),
):
    return await _export_websocket_response(
        websocket,
        task_id=export_task_id,
        resource_id=redemption_id,
        task_type="redemption_export",
        task_token=task_token,
        resource_path=f"redemptions/{redemption_id}/export",
        validate_token=_ensure_task_token,
    )


@router.get("/redemptions/{redemption_id}/export/{export_task_id}/download")
def download_redemption_export(
    redemption_id: str,
    export_task_id: str,
    request: Request,
    task_token: str = Query(alias="token"),
    db: Session = Depends(get_db),
):
    return _export_download_response(
        request,
        db,
        resource_id=redemption_id,
        task_id=export_task_id,
        task_token=task_token,
        task_type="redemption_export",
        validate_token=_ensure_task_token,
    )


@router.get("/redemptions/{redemption_id}/download")
def download_redemption(
    redemption_id: str,
    request: Request,
    task_token: str = Query(alias="token"),
    db: Session = Depends(get_db),
):
    redemption = db.scalar(select(Redemption).where(Redemption.id == redemption_id).with_for_update())
    if not redemption:
        raise HTTPException(status_code=404, detail="兑换任务不存在")
    _ensure_task_token(request, redemption, task_token)
    if redemption.status != "completed":
        raise HTTPException(status_code=409, detail="兑换任务尚未完成")
    if redemption.downloaded_at:
        raise HTTPException(status_code=410, detail="下载链接已使用")
    try:
        stored_artifact = persist_redemption_download(
            db,
            redemption_id,
            request.app.state.security,
            export_directory(request.app.state.settings),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    redemption.downloaded_at = utcnow()
    db.commit()
    return FileResponse(
        stored_artifact.path,
        media_type=stored_artifact.media_type,
        filename=stored_artifact.path.name,
        headers={
            "Cache-Control": "no-store, private",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/redeliveries/{redelivery_id}/export")
async def create_redelivery_export(
    redelivery_id: str,
    request: Request,
    task_token: str = Query(alias="token"),
    db: Session = Depends(get_db),
):
    redelivery = _load_redelivery(db, redelivery_id)
    if not redelivery:
        raise HTTPException(status_code=404, detail="补发任务不存在")
    _ensure_redelivery_token(request, redelivery, task_token)
    _ensure_redelivery_window(db, redelivery)
    if redelivery.status not in {"ready", "downloaded"}:
        raise HTTPException(status_code=409, detail="关联账号已不可用，无法导出")
    return await _begin_export_task(
        request,
        db,
        resource_id=redelivery_id,
        task_token=task_token,
        task_type="redelivery_export",
        total=redelivery.delivered_count,
        resource_path=f"redeliveries/{redelivery_id}/export",
    )


@router.get("/redeliveries/{redelivery_id}/export/{export_task_id}")
def get_redelivery_export(
    redelivery_id: str,
    export_task_id: str,
    request: Request,
    task_token: str = Query(alias="token"),
    db: Session = Depends(get_db),
):
    redelivery = _load_redelivery(db, redelivery_id)
    if not redelivery:
        raise HTTPException(status_code=404, detail="补发任务不存在")
    _ensure_redelivery_token(request, redelivery, task_token)
    _ensure_redelivery_window(db, redelivery)
    task = _load_export_task(db, export_task_id, task_type="redelivery_export", resource_id=redelivery_id)
    if not task:
        raise HTTPException(status_code=404, detail="导出任务不存在")
    return _export_payload(
        request,
        task,
        resource_path=f"redeliveries/{redelivery_id}/export",
        token=task_token,
    )


@router.get("/redeliveries/{redelivery_id}/export/{export_task_id}/events")
async def redelivery_export_events(
    redelivery_id: str,
    export_task_id: str,
    request: Request,
    task_token: str = Query(alias="token"),
):
    return await _export_events_response(
        request,
        task_id=export_task_id,
        resource_id=redelivery_id,
        task_type="redelivery_export",
        task_token=task_token,
        resource_path=f"redeliveries/{redelivery_id}/export",
        validate_token=_ensure_redelivery_token,
    )


@router.websocket("/redeliveries/{redelivery_id}/export/{export_task_id}/ws")
async def redelivery_export_websocket(
    redelivery_id: str,
    export_task_id: str,
    websocket: WebSocket,
    task_token: str = Query(alias="token"),
):
    return await _export_websocket_response(
        websocket,
        task_id=export_task_id,
        resource_id=redelivery_id,
        task_type="redelivery_export",
        task_token=task_token,
        resource_path=f"redeliveries/{redelivery_id}/export",
        validate_token=_ensure_redelivery_token,
    )


@router.get("/redeliveries/{redelivery_id}/export/{export_task_id}/download")
def download_redelivery_export(
    redelivery_id: str,
    export_task_id: str,
    request: Request,
    task_token: str = Query(alias="token"),
    db: Session = Depends(get_db),
):
    return _export_download_response(
        request,
        db,
        resource_id=redelivery_id,
        task_id=export_task_id,
        task_token=task_token,
        task_type="redelivery_export",
        validate_token=_ensure_redelivery_token,
    )


@router.get("/redeliveries/{redelivery_id}/download")
def download_redelivery(
    redelivery_id: str,
    request: Request,
    task_token: str = Query(alias="token"),
    db: Session = Depends(get_db),
):
    redelivery = db.scalar(select(Redelivery).where(Redelivery.id == redelivery_id).with_for_update())
    if not redelivery:
        raise HTTPException(status_code=404, detail="补发任务不存在")
    _ensure_redelivery_token(request, redelivery, task_token)
    if redelivery.status == "downloaded" or redelivery.downloaded_at:
        raise HTTPException(status_code=410, detail="补发下载链接已使用")
    if redelivery.status == "expired" or redelivery.recovery_expires_at <= utcnow():
        redelivery.status = "expired"
        db.commit()
        raise HTTPException(status_code=410, detail="CDK 已兑换，补发时效已过，请联系管理员")
    if redelivery.status != "ready":
        raise HTTPException(status_code=409, detail="关联账号已不可用，无法补发")
    try:
        stored_artifact = persist_redelivery_download(
            db,
            redelivery_id,
            request.app.state.security,
            export_directory(request.app.state.settings),
        )
    except ValueError as exc:
        redelivery.status = "unavailable"
        db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    redelivery.status = "downloaded"
    redelivery.downloaded_at = utcnow()
    db.commit()
    return FileResponse(
        stored_artifact.path,
        media_type=stored_artifact.media_type,
        filename=stored_artifact.path.name,
        headers={
            "Cache-Control": "no-store, private",
            "X-Content-Type-Options": "nosniff",
        },
    )
