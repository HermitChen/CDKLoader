from __future__ import annotations

import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


_SIZE_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt](?:i?b)?|b)?\s*$", re.IGNORECASE)
_SIZE_UNITS = {
    "B": 1,
    "K": 1024,
    "KB": 1024,
    "KIB": 1024,
    "M": 1024**2,
    "MB": 1024**2,
    "MIB": 1024**2,
    "G": 1024**3,
    "GB": 1024**3,
    "GIB": 1024**3,
    "T": 1024**4,
    "TB": 1024**4,
    "TIB": 1024**4,
}


def parse_size_bytes(value: str | int) -> int:
    """Parse a byte setting such as ``10M``, ``1.5G`` or ``10485760``."""
    if isinstance(value, int):
        if value < 0:
            raise ValueError("文件大小不能为负数")
        return value

    match = _SIZE_PATTERN.fullmatch(str(value))
    if not match:
        raise ValueError(f"无效的文件大小：{value!r}，示例：100M、1G 或 104857600")
    try:
        amount = Decimal(match.group(1))
    except InvalidOperation as exc:
        raise ValueError(f"无效的文件大小：{value!r}") from exc

    unit = (match.group(2) or "B").upper()
    size = int(amount * _SIZE_UNITS[unit])
    if size < 1:
        raise ValueError("文件大小必须大于 0")
    return size


@dataclass(frozen=True)
class Settings:
    database_url: str
    admin_password: str
    admin_token: str
    credential_secret: str
    cdk_pepper: str
    validation_mode: str
    validation_timeout_seconds: float
    validation_attempts: int
    validation_concurrency: int
    redelivery_window_seconds: int
    public_base_url: str
    max_upload_bytes: int
    max_import_accounts: int
    max_zip_files: int
    max_zip_uncompressed_bytes: int
    oauth_client_id: str
    oauth_redirect_uri: str
    operation_log_retention_days: int = 30
    export_retention_seconds: int = 86400
    export_dir: str = ""
    validation_proxy: str = ""
    validation_retry_base_seconds: float = 1.0
    validation_retry_max_seconds: float = 30.0
    validation_retry_jitter_seconds: float = 0.25

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    default_db = f"sqlite:///{PROJECT_ROOT / 'data' / 'cdkloader.db'}"
    return Settings(
        database_url=os.getenv("DATABASE_URL", default_db),
        admin_password=os.getenv("ADMIN_PASSWORD", "change-me"),
        admin_token=os.getenv("ADMIN_TOKEN", "change-me-admin-token"),
        credential_secret=os.getenv("CREDENTIAL_SECRET", "development-credential-secret"),
        cdk_pepper=os.getenv("CDK_PEPPER", "development-cdk-pepper"),
        validation_mode=os.getenv("VALIDATION_MODE", "remote").strip().lower(),
        validation_timeout_seconds=float(os.getenv("VALIDATION_TIMEOUT_SECONDS", "5")),
        validation_attempts=max(1, int(os.getenv("VALIDATION_ATTEMPTS", "3"))),
        validation_concurrency=max(1, int(os.getenv("VALIDATION_CONCURRENCY", "6"))),
        redelivery_window_seconds=max(0, int(os.getenv("REDELIVERY_WINDOW_SECONDS", "1800"))),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "http://localhost:1456").rstrip("/"),
        max_upload_bytes=parse_size_bytes(os.getenv("MAX_UPLOAD_BYTES", "100M")),
        max_import_accounts=int(os.getenv("MAX_IMPORT_ACCOUNTS", "5000")),
        max_zip_files=int(os.getenv("MAX_ZIP_FILES", "1000")),
        max_zip_uncompressed_bytes=int(
            os.getenv("MAX_ZIP_UNCOMPRESSED_BYTES", str(100 * 1024 * 1024))
        ),
        oauth_client_id=os.getenv("OPENAI_OAUTH_CLIENT_ID", "app_2SKx67EdpoN0G6j64rFvigXD"),
        oauth_redirect_uri=os.getenv("OPENAI_OAUTH_REDIRECT_URI", ""),
        operation_log_retention_days=max(1, int(os.getenv("OPERATION_LOG_RETENTION_DAYS", "30"))),
        export_retention_seconds=max(300, int(os.getenv("EXPORT_RETENTION_SECONDS", "86400"))),
        export_dir=os.getenv("EXPORT_DIR", str(PROJECT_ROOT / "data" / "exports")),
        validation_proxy=os.getenv("VALIDATION_PROXY", "").strip(),
        validation_retry_base_seconds=max(
            0.0, float(os.getenv("VALIDATION_RETRY_BASE_SECONDS", "1"))
        ),
        validation_retry_max_seconds=max(
            0.0, float(os.getenv("VALIDATION_RETRY_MAX_SECONDS", "30"))
        ),
        validation_retry_jitter_seconds=max(
            0.0, float(os.getenv("VALIDATION_RETRY_JITTER_SECONDS", "0.25"))
        ),
    )
