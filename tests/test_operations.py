from __future__ import annotations

import io
import json
import zipfile
from datetime import timedelta

from app.models import OperationLog, OperationTask, utcnow
from app.services.operations import cleanup_operation_data, export_directory


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
