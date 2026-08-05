from __future__ import annotations

import io
import json
import zipfile

import pytest

from app.services.importers import ImportParseException, parse_import_file


def _json_account(email: str = "person@example.com") -> dict:
    return {
        "email": email,
        "account_id": "account-1",
        "access_token": "access-token",
        "refresh_token": "refresh-token",
    }


@pytest.mark.parametrize(
    ("filename", "content", "expected_format"),
    [
        ("accounts.json", json.dumps([_json_account()]).encode(), "json"),
        (
            "accounts.csv",
            b"Email,Account ID,Access Token,Refresh Token\nperson@example.com,account-1,access-token,refresh-token\n",
            "csv",
        ),
        ("accounts.txt", b"person@example.com----password----refresh-token\n", "txt"),
        (
            "account.json",
            json.dumps({"type": "codex", **_json_account(), "credentials": {"access_token": "access-token", "refresh_token": "refresh-token"}}).encode(),
            "cpa",
        ),
        (
            "sub2api.json",
            json.dumps({"accounts": [{"name": "person@example.com", "account_id": "account-1", "credentials": {"email": "person@example.com", "access_token": "access-token", "refresh_token": "refresh-token"}}]}).encode(),
            "sub2api",
        ),
    ],
)
def test_supported_formats_parse_to_canonical_accounts(settings, filename, content, expected_format):
    batch = parse_import_file(filename, content, settings)

    assert batch.detected_format == expected_format
    assert len(batch.records) == 1
    assert not batch.errors
    assert batch.records[0].email == "person@example.com"
    assert batch.records[0].refresh_token == "refresh-token"


def test_zip_import_and_path_safety(settings):
    safe = io.BytesIO()
    with zipfile.ZipFile(safe, "w") as archive:
        archive.writestr("one.json", json.dumps([_json_account()]))
    batch = parse_import_file("accounts.zip", safe.getvalue(), settings)
    assert len(batch.records) == 1

    unsafe = io.BytesIO()
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../outside.json", json.dumps([_json_account()]))
    with pytest.raises(ImportParseException, match="不安全路径"):
        parse_import_file("unsafe.zip", unsafe.getvalue(), settings)


def test_invalid_txt_does_not_echo_token(settings):
    batch = parse_import_file("accounts.txt", b"person@example.com----only-two-fields\n", settings)
    assert not batch.records
    assert batch.errors[0].error_type == "invalid_txt"
    assert "only-two-fields" not in batch.errors[0].message


def test_cpa_proxy_url_is_stored_as_account_proxy(settings):
    batch = parse_import_file(
        "account.json",
        json.dumps(
            {
                "type": "web",
                "email": "person@example.com",
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "proxy_url": "http://account-proxy.example:8080",
            }
        ).encode(),
        settings,
    )

    assert batch.records[0].proxy_used == "http://account-proxy.example:8080"
