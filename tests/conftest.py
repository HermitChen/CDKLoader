from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        admin_password="test-password",
        admin_token="test-token",
        credential_secret="test-credential-secret",
        cdk_pepper="test-cdk-pepper",
        validation_mode="structural",
        validation_timeout_seconds=1,
        validation_attempts=1,
        validation_concurrency=2,
        redelivery_window_seconds=1800,
        public_base_url="http://testserver",
        max_upload_bytes=20 * 1024 * 1024,
        max_import_accounts=100,
        max_zip_files=20,
        max_zip_uncompressed_bytes=1024 * 1024,
        oauth_client_id="test-client",
        oauth_redirect_uri="",
        export_dir=str(tmp_path / "exports"),
    )


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}
