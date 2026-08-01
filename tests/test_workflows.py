from __future__ import annotations

import io
import json
import re
import zipfile


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
        r'attachment; filename="accounts_\d{8}_\d{6}\.zip"',
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


def test_invalid_cdk_does_not_create_delivery(client, admin_headers):
    response = client.post(
        "/api/v1/redemptions",
        headers={"Idempotency-Key": "invalid-request"},
        json={"codes": ["CDK-NOT-VALID"]},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_cdk"
