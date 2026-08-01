from __future__ import annotations

import os
from dataclasses import dataclass
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
    public_base_url: str
    max_upload_bytes: int
    max_import_accounts: int
    max_zip_files: int
    max_zip_uncompressed_bytes: int
    oauth_client_id: str
    oauth_redirect_uri: str

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
        validation_attempts=max(1, int(os.getenv("VALIDATION_ATTEMPTS", "2"))),
        validation_concurrency=max(1, int(os.getenv("VALIDATION_CONCURRENCY", "6"))),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "http://localhost:1456").rstrip("/"),
        max_upload_bytes=int(os.getenv("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024))),
        max_import_accounts=int(os.getenv("MAX_IMPORT_ACCOUNTS", "5000")),
        max_zip_files=int(os.getenv("MAX_ZIP_FILES", "1000")),
        max_zip_uncompressed_bytes=int(
            os.getenv("MAX_ZIP_UNCOMPRESSED_BYTES", str(100 * 1024 * 1024))
        ),
        oauth_client_id=os.getenv("OPENAI_OAUTH_CLIENT_ID", "app_2SKx67EdpoN0G6j64rFvigXD"),
        oauth_redirect_uri=os.getenv("OPENAI_OAUTH_REDIRECT_URI", ""),
    )

