from __future__ import annotations

import io
import json
import threading
import zipfile
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.database import apply_compatibility_migrations
from app.models import OperationLog, OperationTask, utcnow
from app.services.operations import cleanup_operation_data, export_directory
from app.services.validator import ValidationResult


def _import_accounts(client, admin_headers, count: int = 1):
    records = [
        {
            "email": f"task-user-{index}@example.com",
            "account_id": f"task-account-{index}",
            "access_token": f"task-access-{index}",
            "refresh_token": f"task-refresh-{index}",
        }
        for index in range(count)
    ]
    response = client.post(
        "/api/v1/admin/account-imports",
        headers=admin_headers,
        data={"duplicate_strategy": "skip", "prevalidate": "true"},
        files={"file": ("task-accounts.json", json.dumps(records).encode(), "application/json")},
    )
    assert response.status_code == 200, response.text


def test_manual_validation_exposes_progress_task_without_changing_response_body(client, admin_headers):
    _import_accounts(client, admin_headers, count=2)
    accounts = client.get(
        "/api/v1/admin/accounts",
        headers=admin_headers,
        params={"limit": 0},
    ).json()["items"]

    response = client.post(
        "/api/v1/admin/accounts/validate",
        headers=admin_headers,
        json={"ids": [item["id"] for item in accounts]},
    )

    assert response.status_code == 200
    assert response.json() == {"accepted": 2, "skipped": []}
    task_id = response.headers["x-operation-task-id"]
    task = client.get(f"/api/v1/admin/operation-tasks/{task_id}", headers=admin_headers)
    assert task.status_code == 200
    assert task.json()["status"] == "completed"
    assert task.json()["processed"] == 2
    assert task.json()["valid_count"] == 2
    assert {item["account_email"] for item in task.json()["logs"] if item["account_email"]} == {
        "task-user-0@example.com",
        "task-user-1@example.com",
    }

    logs = client.get(
        "/api/v1/admin/operation-logs",
        headers=admin_headers,
        params={"task_id": task_id, "limit": 0},
    )
    assert logs.status_code == 200
    assert logs.json()["total"] >= 4
    assert any(item["outcome"] == "valid" for item in logs.json()["items"])


def test_manual_validation_runs_accounts_in_parallel(client, admin_headers, monkeypatch):
    _import_accounts(client, admin_headers, count=2)
    accounts = client.get(
        "/api/v1/admin/accounts",
        headers=admin_headers,
        params={"limit": 0},
    ).json()["items"]
    validator = client.app.state.validator
    barrier = threading.Barrier(2, timeout=2)

    def validate_in_parallel(_account, **_kwargs):
        barrier.wait()
        return ValidationResult("valid", message="parallel test")

    monkeypatch.setattr(validator, "validate", validate_in_parallel)
    response = client.post(
        "/api/v1/admin/accounts/validate",
        headers=admin_headers,
        json={"ids": [item["id"] for item in accounts]},
    )

    assert response.status_code == 200, response.text
    task_id = response.headers["x-operation-task-id"]
    task = client.get(f"/api/v1/admin/operation-tasks/{task_id}", headers=admin_headers)
    assert task.status_code == 200
    assert task.json()["status"] == "completed"
    assert task.json()["valid_count"] == 2
    assert task.json()["failed_count"] == 0


def test_manual_validation_keeps_manager_result_details_and_recovery_count(client, admin_headers, monkeypatch):
    _import_accounts(client, admin_headers, count=2)
    accounts = client.get("/api/v1/admin/accounts", headers=admin_headers, params={"limit": 0}).json()["items"]
    recovered_id = accounts[0]["id"]
    def validate(account, **_kwargs):
        if account.id == recovered_id:
            return ValidationResult(
                "valid", validated_via="refresh_token", refreshed=True, recoverable=True,
                current_access_token_valid=False, current_access_token_error="Token 无效或已过期",
                current_access_token_error_type="access_token", refreshed_access_token_valid=True,
            )
        return ValidationResult(
            "inconclusive", "transient", "Token 验证暂不可用: HTTP 429",
            current_access_token_error="Token 验证暂不可用: HTTP 429",
            current_access_token_error_type="transient",
        )
    monkeypatch.setattr(client.app.state.validator, "validate", validate)
    response = client.post(
        "/api/v1/admin/accounts/validate", headers=admin_headers,
        json={"ids": [account["id"] for account in accounts], "concurrency": 1},
    )
    assert response.status_code == 200
    task = client.get(
        f"/api/v1/admin/operation-tasks/{response.headers['x-operation-task-id']}", headers=admin_headers,
    ).json()
    assert task["valid_count"] == task["inconclusive_count"] == task["recoverable_count"] == 1
    assert task["invalid_count"] == task["failed_count"] == 0
    recovered = next(log["details"] for log in task["logs"] if log["account_id"] == recovered_id)
    assert recovered["recoverable"] and recovered["refreshed"]
    assert not recovered["current_access_token_valid"]
    assert recovered["refreshed_access_token_valid"]
    assert recovered["current_access_token_error_type"] == "access_token"


def test_per_request_validation_concurrency_overrides_batch_default(client, admin_headers, monkeypatch):
    _import_accounts(client, admin_headers, count=2)
    settings_response = client.put(
        "/api/v1/admin/settings", headers=admin_headers, json={"values": {"validation_concurrency": 1}},
    )
    assert settings_response.status_code == 200
    accounts = client.get("/api/v1/admin/accounts", headers=admin_headers, params={"limit": 0}).json()["items"]
    barrier = threading.Barrier(2, timeout=2)
    def validate(_account, **_kwargs):
        barrier.wait()
        return ValidationResult("valid")
    monkeypatch.setattr(client.app.state.validator, "validate", validate)
    response = client.post(
        "/api/v1/admin/accounts/validate", headers=admin_headers,
        json={"ids": [account["id"] for account in accounts], "concurrency": 2},
    )
    task = client.get(
        f"/api/v1/admin/operation-tasks/{response.headers['x-operation-task-id']}", headers=admin_headers,
    ).json()
    assert task["valid_count"] == 2
    assert task["failed_count"] == 0


@pytest.mark.parametrize("concurrency", [0, 51])
def test_batch_validation_rejects_invalid_concurrency(client, admin_headers, concurrency):
    response = client.post(
        "/api/v1/admin/accounts/validate", headers=admin_headers,
        json={"ids": ["unused-account"], "concurrency": concurrency},
    )
    assert response.status_code == 422


def test_existing_sqlite_tasks_gain_recovery_counter_without_losing_progress(client):
    with client.app.state.session_factory.begin() as session:
        task = OperationTask(task_type="manual_validation", valid_count=2, total=2, processed=2)
        session.add(task)
        session.flush()
        task_id = task.id
        engine = session.get_bind()
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE operation_tasks DROP COLUMN recoverable_count"))
    apply_compatibility_migrations(engine)
    apply_compatibility_migrations(engine)
    with client.app.state.session_factory() as session:
        migrated = session.get(OperationTask, task_id)
        assert migrated.valid_count == migrated.processed == 2
        assert migrated.recoverable_count == 0


def test_public_export_task_reports_progress_and_supports_range_download(client, admin_headers):
    _import_accounts(client, admin_headers, count=2)
    generated = client.post(
        "/api/v1/admin/cdks/generate",
        headers=admin_headers,
        json={"count": 1, "quota": 2, "export_format": "json"},
    )
    assert generated.status_code == 200
    redemption = client.post(
        "/api/v1/redemptions",
        headers={"Idempotency-Key": "export-task-test", "Prefer": "wait=3"},
        json={"codes": generated.json()["codes"]},
    )
    assert redemption.status_code == 200
    redemption_body = redemption.json()

    started = client.post(
        f"/api/v1/redemptions/{redemption_body['id']}/export",
        params={"token": redemption_body["task_token"]},
    )
    assert started.status_code in {200, 202}
    export_task = started.json()
    with client.websocket_connect(
        f"/api/v1/redemptions/{redemption_body['id']}/export/{export_task['id']}/ws"
        f"?token={redemption_body['task_token']}"
    ) as websocket:
        for _ in range(50):
            current = websocket.receive_json()
            if current["status"] in {"completed", "failed"}:
                break

    assert current["status"] == "completed"
    assert current["processed"] == 2
    download = client.get(
        f"/api/v1/redemptions/{redemption_body['id']}/export/{export_task['id']}/download",
        params={"token": redemption_body["task_token"]},
    )
    assert download.status_code == 200
    assert download.headers["accept-ranges"] == "bytes"
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert len([name for name in archive.namelist() if name.startswith("cpa/")]) == 2

    partial = client.get(
        f"/api/v1/redemptions/{redemption_body['id']}/export/{export_task['id']}/download",
        params={"token": redemption_body["task_token"]},
        headers={"Range": "bytes=0-31"},
    )
    assert partial.status_code == 206
    assert partial.headers["content-range"].startswith("bytes 0-31/")
    assert len(partial.content) == 32


def test_operation_retention_removes_expired_logs_tasks_and_artifacts(client, settings):
    old_time = utcnow() - timedelta(days=settings.operation_log_retention_days + 1)
    with client.app.state.session_factory.begin() as session:
        task = OperationTask(
            task_type="manual_validation",
            status="completed",
            created_at=old_time,
            completed_at=old_time,
            expires_at=old_time,
            file_name="expired-task.zip",
        )
        session.add(task)
        session.flush()
        session.add(
            OperationLog(
                task_id=task.id,
                operation_type="manual_validation",
                outcome="completed",
                message="old",
                created_at=old_time,
            )
        )
        task_id = task.id

    artifact = export_directory(settings) / "expired-task.zip"
    artifact.write_bytes(b"expired")
    cleanup_operation_data(client.app.state.session_factory, settings)

    with client.app.state.session_factory() as session:
        assert session.get(OperationTask, task_id) is None
        assert session.query(OperationLog).filter(OperationLog.task_id == task_id).count() == 0
    assert not artifact.exists()
