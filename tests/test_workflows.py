from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import timedelta
from pathlib import Path

from app.models import Redelivery, Redemption, utcnow


def _import_accounts(client, admin_headers, count: int = 1):
    records = [
        {
            "email": f"user-{index}@example.com",
            "account_id": f"account-{index}",
            "access_token": f"access-{index}",
            "refresh_token": f"refresh-{index}",
        }
        for index in range(count)
    ]
    payload = json.dumps(records).encode()
    response = client.post(
        "/api/v1/admin/account-imports",
        headers=admin_headers,
        data={"duplicate_strategy": "skip", "prevalidate": "true"},
        files={"file": ("accounts.json", payload, "application/json")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _generate_cdks(client, admin_headers, count: int):
    response = client.post(
        "/api/v1/admin/cdks/generate",
        headers=admin_headers,
        json={"count": count, "quota": 1, "export_format": "json"},
    )
    assert response.status_code == 200, response.text
    return response.json()["codes"]


def test_import_preview_encrypts_credentials_and_preserves_duplicate_policy(client, admin_headers):
    source = b'[{"email":"person@example.com","account_id":"account-1","access_token":"secret-access","refresh_token":"secret-refresh"}]'
    preview = client.post(
        "/api/v1/admin/account-imports/preview",
        headers=admin_headers,
        files={"file": ("accounts.json", source, "application/json")},
    )
    assert preview.status_code == 200
    assert preview.json()["samples"][0]["email"] == "pe***@example.com"
    assert "secret-access" not in preview.text

    first = client.post(
        "/api/v1/admin/account-imports",
        headers=admin_headers,
        data={"duplicate_strategy": "skip", "prevalidate": "false"},
        files={"file": ("accounts.json", source, "application/json")},
    )
    assert first.status_code == 200
    assert first.json()["inserted_count"] == 1

    second = client.post(
        "/api/v1/admin/account-imports",
        headers=admin_headers,
        data={"duplicate_strategy": "skip", "prevalidate": "false"},
        files={"file": ("accounts.json", source, "application/json")},
    )
    assert second.status_code == 200
    assert second.json()["duplicate_count"] == 1

    accounts = client.get("/api/v1/admin/accounts", headers=admin_headers).json()
    assert accounts["total"] == 1
    assert accounts["items"][0]["status"] == "pending_validation"


def test_import_accepts_multiple_json_files(client, admin_headers):
    files = [
        (
            "file",
            (
                "accounts-one.json",
                json.dumps(
                    [{"email": "one@example.com", "account_id": "account-one", "refresh_token": "refresh-one"}]
                ).encode(),
                "application/json",
            ),
        ),
        (
            "file",
            (
                "accounts-two.json",
                json.dumps(
                    [{"email": "two@example.com", "account_id": "account-two", "refresh_token": "refresh-two"}]
                ).encode(),
                "application/json",
            ),
        ),
    ]

    preview = client.post("/api/v1/admin/account-imports/preview", headers=admin_headers, files=files)
    assert preview.status_code == 200, preview.text
    assert preview.json()["detected_format"] == "json"
    assert preview.json()["parsed_count"] == 2

    result = client.post(
        "/api/v1/admin/account-imports",
        headers=admin_headers,
        data={"duplicate_strategy": "skip", "prevalidate": "false"},
        files=files,
    )
    assert result.status_code == 200, result.text
    assert result.json()["inserted_count"] == 2


def test_import_preview_accepts_json_larger_than_ten_megabytes(client, admin_headers):
    source = json.dumps(
        [
            {
                "email": "large@example.com",
                "account_id": "large-account",
                "refresh_token": "refresh-large",
                "metadata": "x" * (11 * 1024 * 1024),
            }
        ],
        separators=(",", ":"),
    ).encode()

    response = client.post(
        "/api/v1/admin/account-imports/preview",
        headers=admin_headers,
        files={"file": ("large.json", source, "application/json")},
    )

    assert response.status_code == 200, response.text
    assert response.json()["detected_format"] == "json"
    assert response.json()["parsed_count"] == 1


def test_multi_cdk_redemption_delivers_zip_once(client, admin_headers):
    _import_accounts(client, admin_headers, count=2)
    codes = _generate_cdks(client, admin_headers, count=2)

    redemption = client.post(
        "/api/v1/redemptions",
        headers={"Idempotency-Key": "request-1", "Prefer": "wait=3"},
        json={"codes": codes},
    )
    assert redemption.status_code == 200, redemption.text
    body = redemption.json()
    assert body["status"] == "completed"
    assert body["delivered_count"] == 2

    repeated = client.post(
        "/api/v1/redemptions",
        headers={"Idempotency-Key": "request-1", "Prefer": "wait=3"},
        json={"codes": codes},
    )
    assert repeated.status_code == 200
    assert repeated.json()["id"] == body["id"]

    result = client.get(
        f"/api/v1/redemptions/{body['id']}/download",
        params={"token": body["task_token"]},
    )
    assert result.status_code == 200
    assert result.headers["content-type"].startswith("application/zip")
    assert re.fullmatch(
        r'attachment; filename="accounts_\d{8}_\d{6}_[0-9a-f-]{36}\.zip"',
        result.headers["content-disposition"],
    )
    with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
        cpa_files = [name for name in archive.namelist() if name.startswith("cpa/")]
        sub2api_files = [name for name in archive.namelist() if name.startswith("sub2api/")]
        assert len(cpa_files) == 2
        assert len(sub2api_files) == 2
        cpa_account = json.loads(archive.read(cpa_files[0]))
        sub2api_account = json.loads(archive.read(sub2api_files[0]))
        assert cpa_account["type"] == "web"
        assert sub2api_account["accounts"][0]["type"] == "oauth"
        assert sub2api_account["accounts"][0]["credentials"]["access_token"].startswith("access-")

    duplicate_download = client.get(
        f"/api/v1/redemptions/{body['id']}/download",
        params={"token": body["task_token"]},
    )
    assert duplicate_download.status_code == 410


def test_redeemed_cdk_redelivers_only_its_accounts_without_validation(client, admin_headers, monkeypatch):
    _import_accounts(client, admin_headers, count=2)
    codes = _generate_cdks(client, admin_headers, count=2)
    initial = client.post(
        "/api/v1/redemptions",
        headers={"Idempotency-Key": "initial-multi-cdk", "Prefer": "wait=3"},
        json={"codes": codes},
    )
    assert initial.status_code == 200, initial.text
    source = initial.json()
    assert source["status"] == "completed"

    cdk = client.get(
        "/api/v1/admin/cdks",
        headers=admin_headers,
        params={"q": codes[0]},
    ).json()["items"][0]
    delivered_accounts = client.get(
        "/api/v1/admin/accounts",
        headers=admin_headers,
        params={"cdk_id": cdk["id"]},
    ).json()["items"]
    other_cdk = client.get(
        "/api/v1/admin/cdks",
        headers=admin_headers,
        params={"q": codes[1]},
    ).json()["items"][0]
    other_accounts = client.get(
        "/api/v1/admin/accounts",
        headers=admin_headers,
        params={"cdk_id": other_cdk["id"]},
    ).json()["items"]
    assert len(delivered_accounts) == len(other_accounts) == 1

    def should_not_validate(_account):
        raise AssertionError("补发不应触发账号验活")

    monkeypatch.setattr(client.app.state.validator, "validate", should_not_validate)
    redelivery = client.post(
        "/api/v1/redemptions",
        headers={"Idempotency-Key": "redelivery-single-cdk", "Prefer": "wait=3"},
        json={"codes": [codes[0]]},
    )
    assert redelivery.status_code == 200, redelivery.text
    payload = redelivery.json()
    assert payload["delivery_type"] == "redelivery"
    assert payload["status"] == "redelivery_ready"
    assert payload["delivered_count"] == 1
    assert payload["message"] == "CDK 已兑换，正在补发首次交付的关联账号。"

    download = client.get(
        f"/api/v1/redeliveries/{payload['id']}/download",
        params={"token": payload["task_token"]},
    )
    assert download.status_code == 200, download.text
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        names = archive.namelist()
    expected_email = delivered_accounts[0]["email"]
    other_email = other_accounts[0]["email"]
    assert names == [f"cpa/{expected_email}.json", f"sub2api/{expected_email}_sub2api.json"]
    assert all(other_email not in name for name in names)
    cdk_after_redelivery = client.get(
        "/api/v1/admin/cdks",
        headers=admin_headers,
        params={"q": codes[0]},
    ).json()["items"][0]
    assert cdk_after_redelivery["remaining_quota"] == 0
    assert cdk_after_redelivery["delivery_count"] == 1

    used_download = client.get(
        f"/api/v1/redeliveries/{payload['id']}/download",
        params={"token": payload["task_token"]},
    )
    assert used_download.status_code == 410


def test_redeemed_cdk_redelivery_window_expiry_and_mixed_submission(client, admin_headers, settings, monkeypatch):
    _import_accounts(client, admin_headers, count=1)
    used_code = _generate_cdks(client, admin_headers, count=1)[0]
    initial = client.post(
        "/api/v1/redemptions",
        headers={"Idempotency-Key": "initial-expiring-cdk", "Prefer": "wait=3"},
        json={"codes": [used_code]},
    )
    assert initial.status_code == 200, initial.text

    fresh_code = _generate_cdks(client, admin_headers, count=1)[0]
    mixed = client.post(
        "/api/v1/redemptions",
        headers={"Idempotency-Key": "mixed-cdk-submission", "Prefer": "wait=3"},
        json={"codes": [used_code, fresh_code]},
    )
    assert mixed.status_code == 400
    assert mixed.json()["detail"]["code"] == "mixed_cdk_state"

    with client.app.state.session_factory.begin() as session:
        redemption = session.get(Redemption, initial.json()["id"])
        assert redemption
        redemption.completed_at = utcnow() - timedelta(seconds=settings.redelivery_window_seconds + 1)

    def should_not_validate(_account):
        raise AssertionError("过期补发不应触发账号验活")

    monkeypatch.setattr(client.app.state.validator, "validate", should_not_validate)
    expired = client.post(
        "/api/v1/redemptions",
        headers={"Idempotency-Key": "expired-redelivery", "Prefer": "wait=3"},
        json={"codes": [used_code]},
    )
    assert expired.status_code == 400
    detail = expired.json()["detail"]
    assert detail["code"] == "cdk_redelivery_expired"
    assert detail["details"][0]["code"] == "redelivery_expired"


def test_redelivery_export_reuses_archive_and_enforces_window(client, admin_headers, settings, monkeypatch):
    _import_accounts(client, admin_headers, count=1)
    code = _generate_cdks(client, admin_headers, count=1)[0]
    initial = client.post(
        "/api/v1/redemptions",
        headers={"Idempotency-Key": "initial-export-redelivery", "Prefer": "wait=3"},
        json={"codes": [code]},
    )
    assert initial.status_code == 200, initial.text
    initial_body = initial.json()
    initial_export_path = f"/api/v1/redemptions/{initial_body['id']}/export"
    initial_started = client.post(initial_export_path, params={"token": initial_body["task_token"]})
    assert initial_started.status_code in {200, 202}
    initial_task = initial_started.json()
    with client.websocket_connect(
        f"{initial_export_path}/{initial_task['id']}/ws?token={initial_body['task_token']}"
    ) as websocket:
        for _ in range(50):
            initial_current = websocket.receive_json()
            if initial_current["status"] in {"completed", "failed"}:
                break
    assert initial_current["status"] == "completed"
    initial_file_name = initial_current["file_name"]
    initial_artifact = Path(settings.export_dir) / initial_file_name
    assert initial_artifact.is_file()
    initial_bytes = initial_artifact.read_bytes()

    def fail_repack(*_args, **_kwargs):
        raise AssertionError("补发不应重新打包首次兑换文件")

    monkeypatch.setattr("app.services.exporter.build_delivery_archive_to_path", fail_repack)

    redelivery = client.post(
        "/api/v1/redemptions",
        headers={"Idempotency-Key": "export-redelivery-request", "Prefer": "wait=3"},
        json={"codes": [code]},
    )
    assert redelivery.status_code == 200, redelivery.text
    redelivery_body = redelivery.json()
    export_path = f"/api/v1/redeliveries/{redelivery_body['id']}/export"
    started = client.post(export_path, params={"token": redelivery_body["task_token"]})
    assert started.status_code in {200, 202}
    task = started.json()

    with client.websocket_connect(
        f"{export_path}/{task['id']}/ws?token={redelivery_body['task_token']}"
    ) as websocket:
        for _ in range(50):
            current = websocket.receive_json()
            if current["status"] in {"completed", "failed"}:
                break
    assert current["status"] == "completed"
    assert current["file_name"] == initial_file_name
    assert (Path(settings.export_dir) / current["file_name"]).read_bytes() == initial_bytes

    reused = client.post(export_path, params={"token": redelivery_body["task_token"]})
    assert reused.status_code == 200
    assert reused.json()["id"] == task["id"]
    assert reused.json()["file_name"] == current["file_name"]

    downloaded = client.get(
        f"{export_path}/{task['id']}/download",
        params={"token": redelivery_body["task_token"]},
    )
    assert downloaded.status_code == 200
    assert downloaded.headers["content-disposition"] == f'attachment; filename="{initial_file_name}"'
    assert downloaded.content == initial_bytes

    with client.app.state.session_factory.begin() as session:
        redelivery_row = session.get(Redelivery, redelivery_body["id"])
        assert redelivery_row
        redelivery_row.recovery_expires_at = utcnow() - timedelta(seconds=1)

    expired_download = client.get(
        f"{export_path}/{task['id']}/download",
        params={"token": redelivery_body["task_token"]},
    )
    assert expired_download.status_code == 410
    expired_reuse = client.post(export_path, params={"token": redelivery_body["task_token"]})
    assert expired_reuse.status_code == 410


def test_redelivery_direct_download_reuses_initial_archive(client, admin_headers):
    _import_accounts(client, admin_headers, count=1)
    code = _generate_cdks(client, admin_headers, count=1)[0]
    initial = client.post(
        "/api/v1/redemptions",
        headers={"Idempotency-Key": "initial-direct-redelivery", "Prefer": "wait=3"},
        json={"codes": [code]},
    )
    assert initial.status_code == 200, initial.text
    initial_body = initial.json()
    initial_download = client.get(
        f"/api/v1/redemptions/{initial_body['id']}/download",
        params={"token": initial_body["task_token"]},
    )
    assert initial_download.status_code == 200

    redelivery = client.post(
        "/api/v1/redemptions",
        headers={"Idempotency-Key": "direct-redelivery-request", "Prefer": "wait=3"},
        json={"codes": [code]},
    )
    assert redelivery.status_code == 200, redelivery.text
    redelivery_body = redelivery.json()
    redelivery_download = client.get(
        f"/api/v1/redeliveries/{redelivery_body['id']}/download",
        params={"token": redelivery_body["task_token"]},
    )
    assert redelivery_download.status_code == 200, redelivery_download.text
    assert redelivery_download.headers["content-disposition"] == initial_download.headers["content-disposition"]
    assert redelivery_download.content == initial_download.content


def test_single_json_delivery_defaults_to_cpa_and_sub2api_zip(client, admin_headers):
    _import_accounts(client, admin_headers)
    code = _generate_cdks(client, admin_headers, count=1)[0]

    redemption = client.post(
        "/api/v1/redemptions",
        headers={"Idempotency-Key": "single-json-request", "Prefer": "wait=3"},
        json={"codes": [code]},
    )
    assert redemption.status_code == 200, redemption.text
    body = redemption.json()

    result = client.get(
        f"/api/v1/redemptions/{body['id']}/download",
        params={"token": body["task_token"]},
    )
    assert result.status_code == 200
    assert result.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
        names = archive.namelist()
        assert names == ["cpa/user-0@example.com.json", "sub2api/user-0@example.com_sub2api.json"]


def test_typed_cdks_generate_prefixes_and_deliver_matching_email_accounts(client, admin_headers):
    records = [
        {
            "email": "hotmail-user@hotmail.com",
            "account_id": "hotmail-account",
            "access_token": "access-hotmail",
            "refresh_token": "refresh-hotmail",
        },
        {
            "email": "outlook-user@outlook.cl",
            "account_id": "outlook-account",
            "access_token": "access-outlook",
            "refresh_token": "refresh-outlook",
        },
        {
            "email": "icloud-user@icloud.com",
            "account_id": "icloud-account",
            "access_token": "access-icloud",
            "refresh_token": "refresh-icloud",
        },
        {
            "email": "gmail-user@gmail.com",
            "account_id": "gmail-account",
            "access_token": "access-gmail",
            "refresh_token": "refresh-gmail",
        },
        {
            "email": "other-user@proton.me",
            "account_id": "other-account",
            "access_token": "access-other",
            "refresh_token": "refresh-other",
        },
    ]
    imported = client.post(
        "/api/v1/admin/account-imports",
        headers=admin_headers,
        data={"duplicate_strategy": "skip", "prevalidate": "true"},
        files={"file": ("typed-accounts.json", json.dumps(records).encode(), "application/json")},
    )
    assert imported.status_code == 200, imported.text

    generated = {}
    for email_type, quota in (("ms", 2), ("icloud", 1), ("gmail", 1)):
        response = client.post(
            "/api/v1/admin/cdks/generate",
            headers=admin_headers,
            json={"count": 1, "quota": quota, "email_type": email_type, "export_format": "json"},
        )
        assert response.status_code == 200, response.text
        code = response.json()["codes"][0]
        expected_prefix = {"ms": "CDK-MS", "icloud": "CDK-IC", "gmail": "CDK-GM"}[email_type]
        assert code.startswith(f"{expected_prefix}-")
        assert response.json()["items"][0]["email_type"] == email_type
        generated[email_type] = code

    for email_type, expected_emails in {
        "ms": {"hotmail-user@hotmail.com", "outlook-user@outlook.cl"},
        "icloud": {"icloud-user@icloud.com"},
        "gmail": {"gmail-user@gmail.com"},
    }.items():
        redeemed = client.post(
            "/api/v1/redemptions",
            headers={"Idempotency-Key": f"typed-{email_type}", "Prefer": "wait=3"},
            json={"codes": [generated[email_type]]},
        )
        assert redeemed.status_code == 200, redeemed.text
        assert redeemed.json()["status"] == "completed"
        assert redeemed.json()["delivered_count"] == len(expected_emails)

        accounts = client.get(
            "/api/v1/admin/accounts",
            headers=admin_headers,
            params={"redemption_id": redeemed.json()["id"], "limit": 0},
        )
        assert accounts.status_code == 200, accounts.text
        assert {item["email"] for item in accounts.json()["items"]} == expected_emails


def test_invalid_cdk_does_not_create_delivery(client, admin_headers):
    response = client.post(
        "/api/v1/redemptions",
        headers={"Idempotency-Key": "invalid-request"},
        json={"codes": ["CDK-NOT-VALID"]},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_cdk"
