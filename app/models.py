from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AccountImport(Base):
    __tablename__ = "account_imports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    filename: Mapped[str] = mapped_column(String(255))
    file_hash: Mapped[str] = mapped_column(String(64), index=True)
    detected_format: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="processing", index=True)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    inserted_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    valid_count: Mapped[int] = mapped_column(Integer, default=0)
    invalid_count: Mapped[int] = mapped_column(Integer, default=0)
    inconclusive_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    accounts: Mapped[list["Account"]] = relationship(back_populates="account_import")
    errors: Mapped[list["AccountImportError"]] = relationship(
        back_populates="account_import", cascade="all, delete-orphan"
    )


class AccountImportError(Base):
    __tablename__ = "account_import_errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_id: Mapped[str] = mapped_column(ForeignKey("account_imports.id", ondelete="CASCADE"), index=True)
    locator: Mapped[str] = mapped_column(String(255))
    account_hint: Mapped[str] = mapped_column(String(255), default="")
    error_type: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(String(500))

    account_import: Mapped[AccountImport] = relationship(back_populates="errors")


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(96), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True, index=True)
    account_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)
    workspace_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(64), default="import", index=True)
    registration_mode: Mapped[str] = mapped_column(String(32), default="codex", index=True)
    proxy_used: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending_validation", index=True)
    password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    id_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    cookies_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    import_id: Mapped[str | None] = mapped_column(ForeignKey("account_imports.id"), nullable=True, index=True)
    reserved_by: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    reserved_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    registered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_refresh: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    account_import: Mapped[AccountImport | None] = relationship(back_populates="accounts")
    delivery_item: Mapped["DeliveryItem | None"] = relationship(back_populates="account", uselist=False)

    __table_args__ = (Index("ix_accounts_status_source", "status", "source"),)


class ValidationAttempt(Base):
    __tablename__ = "validation_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    outcome: Mapped[str] = mapped_column(String(32), index=True)
    error_type: Mapped[str] = mapped_column(String(64), default="")
    message: Mapped[str] = mapped_column(String(500), default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    validated_via: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class OperationTask(Base):
    __tablename__ = "operation_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_type: Mapped[str] = mapped_column(String(48), index=True)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    resource_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    valid_count: Mapped[int] = mapped_column(Integer, default=0)
    invalid_count: Mapped[int] = mapped_column(Integer, default=0)
    inconclusive_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    logs: Mapped[list["OperationLog"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_operation_tasks_type_status", "task_type", "status"),)


class OperationLog(Base):
    __tablename__ = "operation_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("operation_tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    operation_type: Mapped[str] = mapped_column(String(48), index=True)
    outcome: Mapped[str] = mapped_column(String(32), index=True)
    account_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    resource_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    message: Mapped[str] = mapped_column(String(500), default="")
    details: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    task: Mapped["OperationTask | None"] = relationship(back_populates="logs")

    __table_args__ = (Index("ix_operation_logs_type_created", "operation_type", "created_at"),)


class CDK(Base):
    __tablename__ = "cdks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code_hmac: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # The plaintext is never persisted. This sealed copy is only used by the
    # authenticated administrator bulk-copy endpoint.
    code_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_prefix: Mapped[str] = mapped_column(String(16), index=True)
    total_quota: Mapped[int] = mapped_column(Integer)
    remaining_quota: Mapped[int] = mapped_column(Integer)
    reserved_quota: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="unused", index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    account_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    registration_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    export_format: Mapped[str] = mapped_column(String(16), default="json")
    export_fields: Mapped[str] = mapped_column(Text, default="[]")
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Redemption(Base):
    __tablename__ = "redemptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    requested_count: Mapped[int] = mapped_column(Integer, default=0)
    delivered_count: Mapped[int] = mapped_column(Integer, default=0)
    input_count: Mapped[int] = mapped_column(Integer, default=0)
    client_ip_hash: Mapped[str] = mapped_column(String(64), default="")
    error_code: Mapped[str] = mapped_column(String(64), default="")
    error_message: Mapped[str] = mapped_column(String(500), default="")
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    export_file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    cdks: Mapped[list["RedemptionCDK"]] = relationship(
        back_populates="redemption", cascade="all, delete-orphan", order_by="RedemptionCDK.ordinal"
    )
    delivery_items: Mapped[list["DeliveryItem"]] = relationship(
        back_populates="redemption", cascade="all, delete-orphan"
    )


class RedemptionCDK(Base):
    __tablename__ = "redemption_cdks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    redemption_id: Mapped[str] = mapped_column(ForeignKey("redemptions.id", ondelete="CASCADE"), index=True)
    cdk_id: Mapped[str] = mapped_column(ForeignKey("cdks.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    reserved_quantity: Mapped[int] = mapped_column(Integer)
    debited_quantity: Mapped[int] = mapped_column(Integer, default=0)

    redemption: Mapped[Redemption] = relationship(back_populates="cdks")
    cdk: Mapped[CDK] = relationship()

    __table_args__ = (UniqueConstraint("redemption_id", "cdk_id", name="uq_redemption_cdk"),)


class Redelivery(Base):
    __tablename__ = "redeliveries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="ready", index=True)
    input_count: Mapped[int] = mapped_column(Integer, default=0)
    delivered_count: Mapped[int] = mapped_column(Integer, default=0)
    client_ip_hash: Mapped[str] = mapped_column(String(64), default="")
    recovery_expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    export_file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    items: Mapped[list["RedeliveryItem"]] = relationship(
        back_populates="redelivery", cascade="all, delete-orphan", order_by="RedeliveryItem.ordinal"
    )


class RedeliveryItem(Base):
    __tablename__ = "redelivery_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    redelivery_id: Mapped[str] = mapped_column(ForeignKey("redeliveries.id", ondelete="CASCADE"), index=True)
    source_redemption_id: Mapped[str] = mapped_column(ForeignKey("redemptions.id", ondelete="CASCADE"), index=True)
    cdk_id: Mapped[str] = mapped_column(ForeignKey("cdks.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    cdk_prefix: Mapped[str] = mapped_column(String(16))
    export_format: Mapped[str] = mapped_column(String(16), default="json")
    export_fields: Mapped[str] = mapped_column(Text, default="[]")
    ordinal: Mapped[int] = mapped_column(Integer)

    redelivery: Mapped[Redelivery] = relationship(back_populates="items")

    __table_args__ = (UniqueConstraint("redelivery_id", "account_id", name="uq_redelivery_account"),)


class DeliveryItem(Base):
    __tablename__ = "delivery_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    redemption_id: Mapped[str] = mapped_column(ForeignKey("redemptions.id", ondelete="CASCADE"), index=True)
    cdk_id: Mapped[str] = mapped_column(ForeignKey("cdks.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), unique=True, index=True)
    delivered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    redemption: Mapped[Redemption] = relationship(back_populates="delivery_items")
    account: Mapped[Account] = relationship(back_populates="delivery_item")
    cdk: Mapped[CDK] = relationship()
