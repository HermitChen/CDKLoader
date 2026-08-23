from __future__ import annotations

import io
import zipfile
from datetime import datetime, timedelta, timezone

from sqlalchemy.dialects import sqlite

from app.models import Account, CDK, DeliveryItem, Redemption, RedemptionCDK, utcnow
from app.services.redemption import RedemptionService
from app.time import china_day_bounds_utc, to_china_iso

from test_workflows import _generate_cdks, _import_accounts


def test_account_filter_and_bulk_delete_skip_reserved(client, admin_headers):
    _import_accounts(client, admin_headers, count=2)
    accounts = client.get("/api/v1/admin/accounts", headers=admin_headers).json()["items"]
    reserved_id = accounts[0]["id"]
    deletable_id = accounts[1]["id"]

    with client.app.state.session_factory.begin() as session:
        session.get(Account, reserved_id).status = "reserved"

    filtered = client.get(
        "/api/v1/admin/accounts",
        headers=admin_headers,
        params={"q": "user-0@example.com"},
    )
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1

    deleted = client.post(
        "/api/v1/admin/accounts/bulk-delete",
        headers=admin_headers,
        json={"ids": [reserved_id, deletable_id]},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"] == 1
    assert deleted.json()["skipped"] == [{"id": reserved_id, "reason": "账号正在被兑换任务预约"}]


def test_manual_batch_validation_skips_unavailable_accounts(client, admin_headers):
    _import_accounts(client, admin_headers, count=3)
    accounts = client.get(
        "/api/v1/admin/accounts",
        headers=admin_headers,
        params={"limit": 0},
    ).json()["items"]
    reserved_id, delivered_id, eligible_id = [account["id"] for account in accounts]

    with client.app.state.session_factory.begin() as session:
        session.get(Account, reserved_id).status = "reserved"
        session.get(Account, delivered_id).status = "delivered"
        session.get(Account, eligible_id).status = "quarantined"

    response = client.post(
        "/api/v1/admin/accounts/validate",
        headers=admin_headers,
        json={"ids": [reserved_id, delivered_id, eligible_id, "missing-account", eligible_id]},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "accepted": 1,
        "skipped": [
            {"id": reserved_id, "reason": "账号正在被兑换任务预约"},
            {"id": delivered_id, "reason": "账号已交付"},
            {"id": "missing-account", "reason": "账号不存在"},
        ],
    }
    with client.app.state.session_factory() as session:
        assert session.get(Account, eligible_id).status == "available"


def test_account_filter_by_refresh_token(client, admin_headers):
    _import_accounts(client, admin_headers, count=3)
    accounts = client.get(
        "/api/v1/admin/accounts",
        headers=admin_headers,
        params={"limit": 0},
    ).json()["items"]

    without_refresh_token_id = accounts[0]["id"]
    with client.app.state.session_factory.begin() as session:
        session.get(Account, without_refresh_token_id).refresh_token_encrypted = None

    with_refresh_token = client.get(
        "/api/v1/admin/accounts",
        headers=admin_headers,
        params={"has_refresh_token": "true", "limit": 0},
    )
    assert with_refresh_token.status_code == 200
    assert with_refresh_token.json()["total"] == 2
    assert all(item["has_refresh_token"] for item in with_refresh_token.json()["items"])

    without_refresh_token = client.get(
        "/api/v1/admin/accounts",
        headers=admin_headers,
        params={"has_refresh_token": "false", "limit": 0},
    )
    assert without_refresh_token.status_code == 200
    assert without_refresh_token.json()["total"] == 1
    assert without_refresh_token.json()["items"][0]["id"] == without_refresh_token_id
    assert without_refresh_token.json()["items"][0]["has_refresh_token"] is False


def test_account_filter_by_email_type(client, admin_headers):
    _import_accounts(client, admin_headers, count=5)
    accounts = client.get(
        "/api/v1/admin/accounts",
        headers=admin_headers,
        params={"limit": 0},
    ).json()["items"]
    emails = [
        "hotmail-user@hotmail.com",
        "outlook-user@outlook.com.gr",
        "icloud-user@icloud.com",
        "gmail-user@gmail.com",
        "generic-user@proton.me",
    ]
    with client.app.state.session_factory.begin() as session:
        for account, email in zip(accounts, emails, strict=True):
            session.get(Account, account["id"]).email = email

    for email_type, expected_emails in {
        "ms": {"hotmail-user@hotmail.com", "outlook-user@outlook.com.gr"},
        "icloud": {"icloud-user@icloud.com"},
        "gmail": {"gmail-user@gmail.com"},
        "generic": {"generic-user@proton.me"},
    }.items():
        response = client.get(
            "/api/v1/admin/accounts",
            headers=admin_headers,
            params={"email_type": email_type, "limit": 0},
        )
        assert response.status_code == 200, response.text
        assert {item["email"] for item in response.json()["items"]} == expected_emails


def test_random_account_selection_honors_filter_and_count(client, admin_headers):
    _import_accounts(client, admin_headers, count=5)
    accounts = client.get(
        "/api/v1/admin/accounts",
        headers=admin_headers,
        params={"limit": 0},
    ).json()["items"]
    available_ids = {account["id"] for account in accounts[:3]}

    with client.app.state.session_factory.begin() as session:
        for account in accounts:
            session.get(Account, account["id"]).status = "available" if account["id"] in available_ids else "expired"

    response = client.get(
        "/api/v1/admin/accounts/random-selection",
        headers=admin_headers,
        params={"status": "available", "q": "user-", "count": 2},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 3
    assert payload["count"] == 2
    assert len(payload["ids"]) == 2
    assert len(set(payload["ids"])) == 2
    assert set(payload["ids"]) <= available_ids
    assert set(payload) == {"total", "count", "ids"}

    too_many = client.get(
        "/api/v1/admin/accounts/random-selection",
        headers=admin_headers,
        params={"status": "available", "count": 4},
    )
    assert too_many.status_code == 400
    assert "不能超过当前筛选结果" in too_many.json()["detail"]


def test_redemption_account_query_uses_random_order():
    query = RedemptionService._account_query(CDK(account_source="import", registration_mode="codex"))
    assert "order by random()" in str(query.compile(dialect=sqlite.dialect())).lower()


def test_account_export_only_includes_selected_filtered_accounts(client, admin_headers):
    _import_accounts(client, admin_headers, count=3)
    all_accounts = client.get(
        "/api/v1/admin/accounts",
        headers=admin_headers,
        params={"limit": 0},
    ).json()["items"]
    without_refresh_token = all_accounts[0]

    with client.app.state.session_factory.begin() as session:
        session.get(Account, without_refresh_token["id"]).refresh_token_encrypted = None

    filtered = client.get(
        "/api/v1/admin/accounts",
        headers=admin_headers,
        params={
            "status": "available",
            "has_refresh_token": "true",
            "limit": 0,
        },
    )
    assert filtered.status_code == 200, filtered.text
    filtered_accounts = filtered.json()["items"]
    assert len(filtered_accounts) == 2
    exported_account = filtered_accounts[0]

    response = client.post(
        "/api/v1/admin/accounts/export",
        headers=admin_headers,
        json={"ids": [exported_account["id"]]},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["content-disposition"].startswith('attachment; filename="accounts_')

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = sorted(archive.namelist())
    assert names == [
        f"cpa/{exported_account['email']}.json",
        f"sub2api/{exported_account['email']}_sub2api.json",
    ]
    assert "manifest.json" not in names


def test_account_export_rejects_missing_selected_account(client, admin_headers):
    response = client.post(
        "/api/v1/admin/accounts/export",
        headers=admin_headers,
        json={"ids": ["missing-account"]},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "部分选中账号不存在，请刷新后重试"


def test_admin_lists_support_pagination(client, admin_headers):
    _import_accounts(client, admin_headers, count=3)
    _generate_cdks(client, admin_headers, count=3)
    with client.app.state.session_factory.begin() as session:
        session.add_all(
            [
                Redemption(idempotency_key=f"pagination-redemption-{index}", status="completed")
                for index in range(3)
            ]
        )

    for path in ("accounts", "cdks", "redemptions"):
        first_page = client.get(f"/api/v1/admin/{path}", headers=admin_headers, params={"limit": 2, "offset": 0})
        assert first_page.status_code == 200
        assert first_page.json()["total"] == 3
        assert len(first_page.json()["items"]) == 2

        second_page = client.get(f"/api/v1/admin/{path}", headers=admin_headers, params={"limit": 2, "offset": 2})
        assert second_page.status_code == 200
        assert second_page.json()["total"] == 3
        assert len(second_page.json()["items"]) == 1

        all_items = client.get(f"/api/v1/admin/{path}", headers=admin_headers, params={"limit": 0, "offset": 2})
        assert all_items.status_code == 200
        assert all_items.json()["total"] == 3
        assert len(all_items.json()["items"]) == 3


def test_cdk_filter_copy_and_bulk_delete_skip_frozen(client, admin_headers):
    codes = _generate_cdks(client, admin_headers, count=2)
    items = client.get("/api/v1/admin/cdks", headers=admin_headers).json()["items"]
    frozen_id = items[0]["id"]
    deletable_id = items[1]["id"]

    generated = client.post(
        "/api/v1/admin/cdks/generate",
        headers=admin_headers,
        json={"count": 1, "quota": 10, "export_format": "json"},
    )
    assert generated.status_code == 200
    quota_cdk_id = generated.json()["items"][0]["id"]

    filtered = client.get(
        "/api/v1/admin/cdks",
        headers=admin_headers,
        params={"q": items[0]["prefix"], "status": "unused"},
    )
    assert filtered.status_code == 200
    assert filtered.json()["items"][0]["id"] == frozen_id
    assert filtered.json()["items"][0]["can_copy"] is True

    by_total_quota = client.get("/api/v1/admin/cdks", headers=admin_headers, params={"quota": "10"})
    assert by_total_quota.status_code == 200
    assert [item["id"] for item in by_total_quota.json()["items"]] == [quota_cdk_id]

    with client.app.state.session_factory.begin() as session:
        session.get(CDK, quota_cdk_id).remaining_quota = 0

    by_remaining_and_total = client.get("/api/v1/admin/cdks", headers=admin_headers, params={"quota": "0/10"})
    assert by_remaining_and_total.status_code == 200
    assert [item["id"] for item in by_remaining_and_total.json()["items"]] == [quota_cdk_id]

    copied = client.post(
        "/api/v1/admin/cdks/copy",
        headers=admin_headers,
        json={"ids": [item["id"] for item in items]},
    )
    assert copied.status_code == 200
    assert set(copied.json()["codes"]) == set(codes)
    assert copied.json()["unavailable_ids"] == []

    with client.app.state.session_factory.begin() as session:
        session.get(CDK, frozen_id).reserved_quota = 1

    deleted = client.post(
        "/api/v1/admin/cdks/bulk-delete",
        headers=admin_headers,
        json={"ids": [frozen_id, deletable_id]},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"] == 1
    assert deleted.json()["skipped"] == [{"id": frozen_id, "reason": "CDK 额度已被冻结"}]


def test_cdk_filter_by_email_type(client, admin_headers):
    generated = {}
    for email_type in ("generic", "ms", "icloud", "gmail"):
        response = client.post(
            "/api/v1/admin/cdks/generate",
            headers=admin_headers,
            json={"count": 1, "quota": 1, "email_type": email_type, "export_format": "json"},
        )
        assert response.status_code == 200, response.text
        generated[email_type] = response.json()["items"][0]["id"]

    for email_type, cdk_id in generated.items():
        response = client.get(
            "/api/v1/admin/cdks",
            headers=admin_headers,
            params={"email_type": email_type, "limit": 0},
        )
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 1
        assert response.json()["items"][0]["id"] == cdk_id


def test_reissue_legacy_unused_cdk_makes_it_copyable(client, admin_headers):
    legacy_code = "CDK-AAAA-BBBB-CCCC-DDDD"
    security = client.app.state.security
    with client.app.state.session_factory.begin() as session:
        cdk = CDK(
            code_hmac=security.cdk_digest(legacy_code),
            code_encrypted=None,
            code_prefix="CDK-AAAA",
            total_quota=1,
            remaining_quota=1,
        )
        session.add(cdk)
        session.flush()
        cdk_id = cdk.id

    reissued = client.post(
        "/api/v1/admin/cdks/reissue",
        headers=admin_headers,
        json={"ids": [cdk_id]},
    )
    assert reissued.status_code == 200, reissued.text
    payload = reissued.json()
    assert len(payload["codes"]) == 1
    assert payload["codes"][0] != legacy_code
    assert payload["items"][0]["can_copy"] is True
    assert payload["skipped"] == []

    copied = client.post(
        "/api/v1/admin/cdks/copy",
        headers=admin_headers,
        json={"ids": [cdk_id]},
    )
    assert copied.status_code == 200
    assert copied.json() == {"codes": payload["codes"], "unavailable_ids": []}


def test_admin_list_shows_full_cdk_and_email(client, admin_headers):
    codes = _generate_cdks(client, admin_headers, count=1)
    _import_accounts(client, admin_headers, count=1)

    cdks = client.get("/api/v1/admin/cdks", headers=admin_headers).json()["items"]
    accounts = client.get("/api/v1/admin/accounts", headers=admin_headers).json()["items"]

    assert cdks[0]["code"] == codes[0]
    assert accounts[0]["email"] == "user-0@example.com"


def test_completed_history_and_delivered_account_can_be_deleted(client, admin_headers):
    security = client.app.state.security
    with client.app.state.session_factory.begin() as session:
        cdk = CDK(
            code_hmac=security.cdk_digest("CDK-ZZZZ-YYYY-XXXX-WWWW"),
            code_encrypted=security.encrypt("CDK-ZZZZ-YYYY-XXXX-WWWW"),
            code_prefix="CDK-ZZZZ",
            total_quota=1,
            remaining_quota=0,
            status="exhausted",
        )
        account = Account(email="delivered@example.com", status="delivered")
        redemption = Redemption(idempotency_key="completed-history", status="completed")
        session.add_all([cdk, account, redemption])
        session.flush()
        session.add(RedemptionCDK(redemption_id=redemption.id, cdk_id=cdk.id, ordinal=0, reserved_quantity=1, debited_quantity=1))
        session.add(DeliveryItem(redemption_id=redemption.id, cdk_id=cdk.id, account_id=account.id))
        cdk_id = cdk.id
        account_id = account.id
        redemption_id = redemption.id

    deleted_cdk = client.post(
        "/api/v1/admin/cdks/bulk-delete",
        headers=admin_headers,
        json={"ids": [cdk_id]},
    )
    assert deleted_cdk.status_code == 200, deleted_cdk.text
    assert deleted_cdk.json() == {"deleted": 1, "skipped": []}

    deleted_account = client.post(
        "/api/v1/admin/accounts/bulk-delete",
        headers=admin_headers,
        json={"ids": [account_id]},
    )
    assert deleted_account.status_code == 200, deleted_account.text
    assert deleted_account.json() == {"deleted": 1, "skipped": []}

    with client.app.state.session_factory() as session:
        assert session.get(CDK, cdk_id) is None
        assert session.get(Account, account_id) is None
        assert session.get(Redemption, redemption_id) is None


def test_redemption_filter_and_bulk_delete_skip_active(client, admin_headers):
    with client.app.state.session_factory.begin() as session:
        completed = Redemption(idempotency_key="completed-redemption", status="completed")
        active = Redemption(idempotency_key="active-redemption", status="processing")
        session.add_all([completed, active])
        session.flush()
        completed_id = completed.id
        active_id = active.id

    filtered = client.get(
        "/api/v1/admin/redemptions",
        headers=admin_headers,
        params={"q": completed_id[:8], "status": "completed"},
    )
    assert filtered.status_code == 200
    assert [item["id"] for item in filtered.json()["items"]] == [completed_id]

    deleted = client.post(
        "/api/v1/admin/redemptions/bulk-delete",
        headers=admin_headers,
        json={"ids": [completed_id, active_id]},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"] == 1
    assert deleted.json()["skipped"] == [{"id": active_id, "reason": "兑换任务正在执行"}]

    remaining = client.get("/api/v1/admin/redemptions", headers=admin_headers).json()["items"]
    assert [item["id"] for item in remaining] == [active_id]


def test_today_redemption_filter_excludes_earlier_records(client, admin_headers):
    with client.app.state.session_factory.begin() as session:
        today = Redemption(idempotency_key="today-redemption", status="completed")
        earlier = Redemption(
            idempotency_key="earlier-redemption",
            status="completed",
            created_at=utcnow() - timedelta(days=1),
        )
        session.add_all([today, earlier])
        session.flush()
        today_id = today.id

    filtered = client.get("/api/v1/admin/redemptions", headers=admin_headers, params={"today": "true"})
    assert filtered.status_code == 200
    assert [item["id"] for item in filtered.json()["items"]] == [today_id]

    dashboard = client.get("/api/v1/admin/dashboard", headers=admin_headers)
    assert dashboard.status_code == 200
    assert dashboard.json()["today_redemptions"] == 1


def test_full_cdk_search_and_delivery_trace_support_reexport(client, admin_headers):
    exact_codes = ["CDK-TRACE-AAAA-BBBB-CCCC", "CDK-TRACE-DDDD-EEEE-FFFF"]
    imported = client.post(
        "/api/v1/admin/cdks/import",
        headers=admin_headers,
        json={"codes": exact_codes, "quota": 1},
    )
    assert imported.status_code == 200, imported.text

    exact_match = client.get(
        "/api/v1/admin/cdks",
        headers=admin_headers,
        params={"q": exact_codes[0]},
    )
    assert exact_match.status_code == 200
    assert exact_match.json()["total"] == 1
    assert exact_match.json()["items"][0]["code"] == exact_codes[0]

    _import_accounts(client, admin_headers, count=1)
    delivered_code = _generate_cdks(client, admin_headers, count=1)[0]
    redeemed = client.post(
        "/api/v1/redemptions",
        headers={"Idempotency-Key": "delivery-trace", "Prefer": "wait=3"},
        json={"codes": [delivered_code]},
    )
    assert redeemed.status_code == 200, redeemed.text
    public_record = redeemed.json()
    assert public_record["cdks"][0]["code"] is None

    cdk_result = client.get(
        "/api/v1/admin/cdks",
        headers=admin_headers,
        params={"q": delivered_code},
    )
    assert cdk_result.status_code == 200
    cdk = cdk_result.json()["items"][0]
    assert cdk["code"] == delivered_code
    assert cdk["delivery_count"] == 1

    accounts_by_cdk = client.get(
        "/api/v1/admin/accounts",
        headers=admin_headers,
        params={"cdk_id": cdk["id"]},
    )
    assert accounts_by_cdk.status_code == 200
    assert accounts_by_cdk.json()["total"] == 1
    account = accounts_by_cdk.json()["items"][0]
    assert account["related_cdk"]["code"] == delivered_code
    assert account["related_cdk"]["redemption_id"] == public_record["id"]
    assert account["created_at"].endswith("+08:00")

    reexported = client.post(
        "/api/v1/admin/accounts/export",
        headers=admin_headers,
        json={"ids": [account["id"]]},
    )
    assert reexported.status_code == 200, reexported.text
    assert reexported.headers["content-type"] == "application/zip"

    redemptions = client.get(
        "/api/v1/admin/redemptions",
        headers=admin_headers,
        params={"q": delivered_code},
    )
    assert redemptions.status_code == 200
    assert redemptions.json()["total"] == 1
    record = redemptions.json()["items"][0]
    assert record["id"] == public_record["id"]
    assert record["cdks"] == [
        {
            "id": cdk["id"],
            "code": delivered_code,
            "prefix": cdk["prefix"],
            "email_type": "generic",
            "reserved_quantity": 1,
            "debited_quantity": 1,
        }
    ]
    assert record["created_at"].endswith("+08:00")

    accounts_by_redemption = client.get(
        "/api/v1/admin/accounts",
        headers=admin_headers,
        params={"redemption_id": public_record["id"]},
    )
    assert accounts_by_redemption.status_code == 200
    assert [item["id"] for item in accounts_by_redemption.json()["items"]] == [account["id"]]


def test_china_time_serialization_and_day_boundaries():
    source = datetime(2026, 8, 3, 2, 11)
    assert to_china_iso(source) == "2026-08-03T10:11:00+08:00"

    start, end = china_day_bounds_utc(datetime(2026, 8, 3, 0, 30, tzinfo=timezone.utc))
    assert start == datetime(2026, 8, 2, 16, 0)
    assert end == datetime(2026, 8, 3, 16, 0)
