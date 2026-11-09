from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class AdminLoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class CDKGenerateRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=5000)
    quota: int = Field(ge=1, le=10000)
    expires_at: datetime | None = None
    account_source: str | None = Field(default=None, max_length=64)
    registration_mode: str | None = Field(default=None, max_length=32)
    email_type: Literal["generic", "ms", "icloud", "gmail"] = "generic"
    export_format: Literal["json", "csv", "txt"] = "json"
    export_fields: list[str] = Field(default_factory=list)


class CDKImportRequest(BaseModel):
    codes: list[str] = Field(min_length=1, max_length=5000)
    quota: int = Field(ge=1, le=10000)
    expires_at: datetime | None = None


class ImportOptions(BaseModel):
    duplicate_strategy: Literal["skip", "fill_missing", "replace"] = "skip"
    prevalidate: bool = True


class RedemptionCreateRequest(BaseModel):
    codes: list[str] = Field(min_length=1, max_length=100)


class AccountValidateRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=5000)
    proxy: str | None = Field(default=None, max_length=1024)
    concurrency: int | None = Field(default=None, ge=1, le=50)


class AccountExportRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=5000)


class BulkDeleteRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=5000)


class SystemSettingsUpdate(BaseModel):
    values: dict[str, Any] = Field(min_length=1, max_length=64)


class ProxyNodeUpdate(BaseModel):
    id: str | None = Field(default=None, max_length=36)
    name: str = Field(default="", max_length=100)
    url: str = Field(min_length=1, max_length=1024)
    enabled: bool = True
    clear_credentials: bool = False


class ProxyGroupUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    strategy: Literal["request_random", "round_robin", "time_window"] = "request_random"
    rotation_interval_minutes: int = Field(default=0, ge=0, le=1440)
    enabled: bool = True
    nodes: list[ProxyNodeUpdate] = Field(min_length=1, max_length=256)


class ProxyHealthSettingsUpdate(BaseModel):
    enabled: bool = True
    interval_minutes: int = Field(default=5, ge=1, le=30)
    threshold_ms: int = Field(default=1000, ge=1, le=60000)
