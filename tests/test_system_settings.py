from __future__ import annotations

from datetime import timedelta

from app.models import (
    Account,
    CDK,
    OperationLog,
    OperationTask,
    Redemption,
    RedemptionCDK,
    SystemSetting,
    utcnow,
)
from app.services.operations import cleanup_inventory_data, cleanup_operation_data


def _setting_values(payload: dict) -> dict:
    return {
        item["key"]: item["value"]
        for group in payload["groups"]
        for item in group["items"]
    }


def test_system_settings_are_persisted_and_applied_immediately(client, admin_headers):
    response = client.get("/api/v1/admin/settings", headers=admin_headers)

    assert response.status_code == 200
    values = _setting_values(response.json())
    assert values["account_auto_delete_days"] == 30
    assert values["operation_log_retention_days"] == 30

    updated = client.put(
        "/api/v1/admin/settings",
        headers=admin_headers,
        json={
            "values": {
                "account_auto_delete_days": 14,
                "operation_log_retention_days": 7,
                "validation_timeout_seconds": 4.5,
                "max_upload_bytes": "25M",
            }
        },
    )

    assert updated.status_code == 200, updated.text
    values = _setting_values(updated.json())
    assert values["account_auto_delete_days"] == 14
    assert values["operation_log_retention_days"] == 7
    assert values["validation_timeout_seconds"] == 4.5
    assert values["max_upload_bytes"] == "25M"
    assert client.app.state.settings.account_auto_delete_days == 14
    assert client.app.state.settings.operation_log_retention_days == 7
    assert client.app.state.settings.validation_timeout_seconds == 4.5
    assert client.app.state.settings.max_upload_bytes == 25 * 1024 * 1024

    with client.app.state.session_factory() as session:
        stored = session.get(SystemSetting, "account_auto_delete_days")
        assert stored is not None
        assert stored.value == "14"


def test_runtime_settings_update_health_and_cors(client, admin_headers):
    updated = client.put(
        "/api/v1/admin/settings",
        headers=admin_headers,
        json={"values": {"validation_mode": "remote"}},
    )

    assert updated.status_code == 200
    assert client.get("/health").json()["validation_mode"] == "remote"

    preflight = client.options(
        "/api/v1/admin/settings",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert preflight.status_code == 200
    assert "PUT" in preflight.headers["access-control-allow-methods"]


def test_system_settings_reject_unknown_and_out_of_range_values(client, admin_headers):
    unknown = client.put(
        "/api/v1/admin/settings",
        headers=admin_headers,
        json={"values": {"not_a_setting": 1}},
    )
    assert unknown.status_code == 422

    invalid = client.put(
        "/api/v1/admin/settings",
        headers=admin_headers,
        json={"values": {"operation_log_retention_days": 0}},
    )
    assert invalid.status_code == 422
    assert client.app.state.settings.operation_log_retention_days == 30


def test_inventory_cleanup_removes_old_terminal_data_and_preserves_active_data(client, settings):
    now = utcnow()
    old = now - timedelta(days=31)
    fresh = now - timedelta(days=2)
    security = client.app.state.security

    with client.app.state.session_factory.begin() as session:
        delivered = Account(
            email="old-delivered@example.com",
            status="delivered",
            delivered_at=old,
            created_at=old,
            updated_at=old,
        )
        expired = Account(
            email="old-expired@example.com",
            status="expired",
            validated_at=old,
            created_at=old,
            updated_at=old,
        )
        fresh_delivered = Account(
            email="fresh-delivered@example.com",
            status="delivered",
            delivered_at=fresh,
        )
        quarantined = Account(
            email="old-quarantined@example.com",
            status="quarantined",
            validated_at=old,
            created_at=old,
            updated_at=old,
        )
        session.add_all([delivered, expired, fresh_delivered, quarantined])

        old_cdk = CDK(
            code_hmac=security.cdk_digest("CDK-OLD-EXHAUSTED"),
            code_encrypted=security.encrypt("CDK-OLD-EXHAUSTED"),
            code_prefix="CDK-OLD",
            total_quota=1,
            remaining_quota=0,
            status="exhausted",
            created_at=old,
            updated_at=old,
        )
        fresh_cdk = CDK(
            code_hmac=security.cdk_digest("CDK-FRESH-EXHAUSTED"),
            code_encrypted=security.encrypt("CDK-FRESH-EXHAUSTED"),
            code_prefix="CDK-FRESH",
            total_quota=1,
            remaining_quota=0,
            status="exhausted",
            updated_at=fresh,
        )
        active_cdk = CDK(
            code_hmac=security.cdk_digest("CDK-ACTIVE-EXHAUSTED"),
            code_encrypted=security.encrypt("CDK-ACTIVE-EXHAUSTED"),
            code_prefix="CDK-ACTIVE",
            total_quota=1,
            remaining_quota=0,
            reserved_quota=1,
            status="partial",
            created_at=old,
            updated_at=old,
        )
        session.add_all([old_cdk, fresh_cdk, active_cdk])
        session.flush()
        redemption = Redemption(idempotency_key="active-cleanup-redemption", status="processing")
        session.add(redemption)
        session.flush()
        session.add(
            RedemptionCDK(
                redemption_id=redemption.id,
                cdk_id=active_cdk.id,
                ordinal=0,
                reserved_quantity=1,
            )
        )
        old_delivered_id = delivered.id
        old_expired_id = expired.id
        fresh_delivered_id = fresh_delivered.id
        quarantined_id = quarantined.id
        old_cdk_id = old_cdk.id
        fresh_cdk_id = fresh_cdk.id
        active_cdk_id = active_cdk.id
        redemption_id = redemption.id

    current_settings = settings.__class__(
        **{
            **settings.__dict__,
            "account_auto_delete_days": 30,
            "exhausted_cdk_auto_delete_days": 30,
        }
    )
    deleted_accounts, deleted_cdks = cleanup_inventory_data(
        client.app.state.session_factory,
        current_settings,
    )

    assert deleted_accounts == 2
    assert deleted_cdks == 1

    with client.app.state.session_factory() as session:
        assert session.get(Account, old_delivered_id) is None
        assert session.get(Account, old_expired_id) is None
        assert session.get(Account, fresh_delivered_id) is not None
        assert session.get(Account, quarantined_id) is not None
        assert session.get(CDK, old_cdk_id) is None
        assert session.get(CDK, fresh_cdk_id) is not None
        assert session.get(CDK, active_cdk_id) is not None
        assert session.get(Redemption, redemption_id) is not None


def test_log_cleanup_uses_current_database_retention_setting(client, admin_headers):
    response = client.put(
        "/api/v1/admin/settings",
        headers=admin_headers,
        json={"values": {"operation_log_retention_days": 7}},
    )
    assert response.status_code == 200

    old = utcnow() - timedelta(days=8)
    with client.app.state.session_factory.begin() as session:
        task = OperationTask(
            task_type="settings_test",
            status="completed",
            created_at=old,
            completed_at=old,
        )
        session.add(task)
        session.flush()
        session.add(
            OperationLog(
                task_id=task.id,
                operation_type="settings_test",
                outcome="completed",
                created_at=old,
            )
        )
        task_id = task.id

    cleanup_operation_data(client.app.state.session_factory, client.app.state.settings)

    with client.app.state.session_factory() as session:
        assert session.get(OperationTask, task_id) is None
        assert session.query(OperationLog).filter(OperationLog.task_id == task_id).count() == 0
