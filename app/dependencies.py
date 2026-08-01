from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from .config import Settings
from .security import SecurityManager


def get_db(request: Request) -> Iterator[Session]:
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


def get_settings_from_app(request: Request) -> Settings:
    return request.app.state.settings


def get_security(request: Request) -> SecurityManager:
    return request.app.state.security


def require_admin(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    settings: Settings = request.app.state.settings
    expected = f"Bearer {settings.admin_token}"
    if not authorization or not request.app.state.security.constant_time_equal(authorization, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="管理员认证失败")


AdminRequired = Depends(require_admin)

