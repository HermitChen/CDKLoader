from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class AdminLoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class CDKGenerateRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=5000)
    quota: int = Field(ge=1, le=10000)
    expires_at: datetime | None = None
    account_source: str | None = Field(default=None, max_length=64)
    registration_mode: str | None = Field(default=None, max_length=32)
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


class AccountExportRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=5000)


class BulkDeleteRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=5000)
