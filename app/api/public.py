from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..dependencies import get_db
from ..models import Redemption, RedemptionCDK, utcnow
from ..schemas import RedemptionCreateRequest
from ..services.exporter import build_download
from ..services.redemption import RedemptionError, RedemptionService, serialize_redemption


router = APIRouter(prefix="/api/v1", tags=["public"])


def _load_redemption(db: Session, redemption_id: str) -> Redemption | None:
    return db.scalar(
        select(Redemption)
        .options(selectinload(Redemption.cdks).selectinload(RedemptionCDK.cdk))
        .where(Redemption.id == redemption_id)
    )


def _ensure_task_token(request: Request, redemption: Redemption, task_token: str) -> None:
    expected = request.app.state.security.redemption_token(redemption.id, redemption.idempotency_key)
    if not task_token or not request.app.state.security.constant_time_equal(task_token, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="任务凭证无效")


def _schedule_task(request: Request, redemption_id: str) -> None:
    request.app.state.redemption_service.process(redemption_id)


def _track_task(app, task: asyncio.Task) -> None:
    app.state.running_tasks.add(task)
    task.add_done_callback(app.state.running_tasks.discard)


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
        db.commit()
    except RedemptionError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": exc.message, "details": exc.details}) from exc

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
        content, filename, media_type = build_download(db, redemption_id, request.app.state.security)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    redemption.downloaded_at = utcnow()
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

