from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import Settings


class Base(DeclarativeBase):
    pass


def create_database(settings: Settings) -> tuple[Engine, sessionmaker[Session]]:
    if settings.database_url.startswith("sqlite:///"):
        raw_path = settings.database_url.removeprefix("sqlite:///")
        if raw_path != ":memory:":
            Path(raw_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    connect_args = {"check_same_thread": False} if settings.is_sqlite else {}
    engine = create_engine(
        settings.database_url,
        connect_args=connect_args,
        pool_pre_ping=True,
    )

    if settings.is_sqlite:
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    return engine, factory


def apply_compatibility_migrations(engine: Engine) -> None:
    """Apply the small SQLite compatibility migration needed by the MVP."""
    if engine.dialect.name != "sqlite":
        return
    inspector = inspect(engine)
    cdk_columns = {column["name"] for column in inspector.get_columns("cdks")}
    account_columns = {column["name"] for column in inspector.get_columns("accounts")}
    redemption_columns = {column["name"] for column in inspector.get_columns("redemptions")}
    redelivery_columns = {column["name"] for column in inspector.get_columns("redeliveries")}
    if (
        "code_encrypted" not in cdk_columns
        or "email_type" not in cdk_columns
        or "proxy_used" not in account_columns
        or "export_file_name" not in redemption_columns
        or "export_file_name" not in redelivery_columns
    ):
        with engine.begin() as connection:
            if "code_encrypted" not in cdk_columns:
                connection.execute(text("ALTER TABLE cdks ADD COLUMN code_encrypted TEXT"))
            if "email_type" not in cdk_columns:
                connection.execute(text("ALTER TABLE cdks ADD COLUMN email_type VARCHAR(16) DEFAULT 'generic'"))
            if "proxy_used" not in account_columns:
                connection.execute(text("ALTER TABLE accounts ADD COLUMN proxy_used VARCHAR(1024)"))
            if "export_file_name" not in redemption_columns:
                connection.execute(text("ALTER TABLE redemptions ADD COLUMN export_file_name VARCHAR(255)"))
            if "export_file_name" not in redelivery_columns:
                connection.execute(text("ALTER TABLE redeliveries ADD COLUMN export_file_name VARCHAR(255)"))


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
