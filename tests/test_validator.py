from __future__ import annotations

from collections import deque
from dataclasses import replace
from datetime import timedelta
import json
import re
from typing import Any

import pytest

from app.models import Account, ValidationAttempt, utcnow
from app.security import SecurityManager
from app.services.openai_validation import (
    CHATGPT_VALIDATION_IMPERSONATE,
    CHATGPT_VALIDATION_USER_AGENT,
    PLATFORM_OAUTH_CLIENT_ID,
    latest_chrome_impersonate,
)
from app.services.validator import TokenValidator, apply_validation, validation_result_details


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self.payload = {} if payload is None else payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class ScriptedSessions:
    def __init__(self, steps):
        self.steps = deque(steps)
        self.calls: list[dict[str, Any]] = []
        self.sessions: list[ScriptedSession] = []

    def __call__(self):
        session = ScriptedSession(self)
        self.sessions.append(session)
        return session

    def request(self, session, method, url, **kwargs):
        self.calls.append({"session": session, "method": method, "url": url, **kwargs})
        assert self.steps, f"unexpected {method} {url}"
        expected_method, expected_url, response = self.steps.popleft()
        assert (method, url) == (expected_method, expected_url)
        if isinstance(response, Exception):
            raise response
        return response

    def assert_complete(self):
        assert not self.steps
        assert all(session.closed for session in self.sessions)


class ScriptedSession:
    def __init__(self, owner):
        self.owner = owner
        self.closed = False
        self.cookie_values = []
        self.cookies = self

    def set(self, name, value, **kwargs):
        self.cookie_values.append((name, value, kwargs))

    def get(self, url, **kwargs):
        return self.owner.request(self, "GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.owner.request(self, "POST", url, **kwargs)

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def retry_delays(monkeypatch):
    delays = []
    monkeypatch.setattr("app.services.openai_validation.time.sleep", delays.append)
    return delays


@pytest.fixture
def security():
    return SecurityManager("validator-secret", "validator-pepper")


def _account(security, *, access_token="old-access-token", refresh_token="old-refresh-token",
             status="available", extra_data=None, **kwargs):
    return Account(
        email="person@example.com", account_id=kwargs.pop("account_id", "account-validator-test"),
        access_token_encrypted=security.encrypt(access_token),
        refresh_token_encrypted=security.encrypt(refresh_token),
        status=status, extra_data=json.dumps(extra_data) if extra_data else None, **kwargs,
    )


def _remote_validator(settings, security, steps):
    validator = TokenValidator(replace(settings, validation_mode="remote"), security)
    sessions = ScriptedSessions(steps)
    validator._session = sessions
    return validator, sessions


USERINFO = TokenValidator.validation_url
INIT = TokenValidator.chatgpt_base_url + TokenValidator.conversation_init_path
CHECK = TokenValidator.chatgpt_base_url + TokenValidator.account_check_path + "?timezone_offset_min=-480"
REFRESH = TokenValidator.token_url


def _success():
    return [("GET", USERINFO, FakeResponse()), ("POST", INIT, FakeResponse()), ("GET", CHECK, FakeResponse())]


def _refresh_success():
    return ("POST", REFRESH, FakeResponse(payload={
        "access_token": "new-access-token", "refresh_token": "new-refresh-token",
        "id_token": "new-id-token", "expires_in": 3600,
    }))


def test_full_manager_request_chain_headers_payload_and_device_cookies(settings, security):
    validator, sessions = _remote_validator(settings, security, _success())
    result = validator.validate(_account(security))
    assert result.outcome == "valid"
    assert result.current_access_token_valid is True
    assert result.stage == "accounts/check"
    assert result.attempts == 3
    assert sessions.calls[0]["headers"] == {
        "authorization": "Bearer old-access-token", "accept": "application/json",
    }
    headers = sessions.calls[1]["headers"]
    device_id, session_id = headers["oai-device-id"], headers["oai-session-id"]
    assert headers == {
        "authorization": "Bearer old-access-token",
        "accept": "application/json", "content-type": "application/json",
        "origin": "https://chatgpt.com", "referer": "https://chatgpt.com/",
        "user-agent": CHATGPT_VALIDATION_USER_AGENT,
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "oai-device-id": device_id, "oai-session-id": session_id,
        "oai-language": "zh-CN", "oai-client-version": "26.1022.91109",
        "x-openai-target-path": TokenValidator.conversation_init_path,
        "x-openai-target-route": TokenValidator.conversation_init_path,
        "sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin",
    }
    assert sessions.calls[2]["headers"] == {
        **headers, "x-openai-target-path": TokenValidator.account_check_path,
        "x-openai-target-route": TokenValidator.account_check_path,
    }
    assert sessions.calls[1]["json"] == {
        "gizmo_id": None, "requested_default_model": None,
        "conversation_id": None, "timezone_offset_min": -480,
    }
    assert all(call["timeout"] == 30 for call in sessions.calls)
    assert len(sessions.sessions) == 1
    assert sessions.sessions[0].cookie_values == [
        ("oai-did", device_id, {"domain": "chatgpt.com", "path": "/"}),
        ("oai-did", device_id, {"domain": ".chatgpt.com", "path": "/"}),
    ]
    sessions.assert_complete()


def test_browser_profile_and_user_agent_follow_installed_curl(settings, security, monkeypatch):
    captured = []
    monkeypatch.setattr("app.services.validator.cffi_requests.Session", lambda **kwargs: captured.append(kwargs))
    validator = TokenValidator(settings, security)
    validator._session()
    assert captured == [{"impersonate": latest_chrome_impersonate()}]
    assert CHATGPT_VALIDATION_IMPERSONATE == latest_chrome_impersonate()
    major = re.fullmatch(r"chrome(\d+)[a-z_]*", CHATGPT_VALIDATION_IMPERSONATE)[1]
    assert f"Chrome/{major}.0.0.0" in CHATGPT_VALIDATION_USER_AGENT


@pytest.mark.parametrize("stage", ["userinfo", "conversation/init", "accounts/check"])
def test_temporary_failure_restarts_entire_chain_with_new_session_and_same_device(
    settings, security, retry_delays, stage,
):
    prefix = [] if stage == "userinfo" else _success()[:1 if stage == "conversation/init" else 2]
    method, url = {"userinfo": ("GET", USERINFO), "conversation/init": ("POST", INIT),
                   "accounts/check": ("GET", CHECK)}[stage]
    failed = prefix + [(method, url, FakeResponse(429, headers={"Retry-After": "20"}))]
    validator, sessions = _remote_validator(settings, security, failed + failed + _success())
    result = validator.validate(_account(security))
    assert result.outcome == "valid"
    assert retry_delays == [0.25, 0.75]
    assert len(sessions.sessions) == 3
    for session in sessions.sessions:
        assert next(call for call in sessions.calls if call["session"] is session)["url"] == USERINFO
    assert len({session.cookie_values[0][1] for session in sessions.sessions}) == 1
    product_headers = [call["headers"] for call in sessions.calls if call["url"] in {INIT, CHECK}]
    assert len({headers["oai-device-id"] for headers in product_headers}) == 1
    assert len({headers["oai-session-id"] for headers in product_headers}) == 1
    sessions.assert_complete()


@pytest.mark.parametrize("failure", [
    FakeResponse(403, text="<html>Just a moment...</html>", headers={
        "Content-Type": "text/html", "Server": "cloudflare",
        "cf-mitigated": "challenge", "cf-ray": "test-ray", "Retry-After": "10",
    }),
    FakeResponse(429), FakeResponse(503), TimeoutError("upstream timeout"),
])
def test_transient_checks_do_not_refresh_or_change_previous_status(settings, client, security, failure, retry_delays):
    validator, sessions = _remote_validator(settings, security, [("GET", USERINFO, failure)] * 3)
    with client.app.state.session_factory.begin() as session:
        account = _account(security, status="available")
        session.add(account)
        session.flush()
        result = apply_validation(session, account, validator)
        assert result.outcome == "inconclusive"
        assert result.error_type == "transient"
        assert account.status == "available"
        assert account.validated_at is not None
        assert account.last_refresh is None
        if getattr(failure, "status_code", None) == 403:
            assert result.status_code == 403
            assert result.stage == "userinfo"
            assert result.cf_mitigated == "challenge"
            assert result.cf_ray == "test-ray"
            assert result.server == "cloudflare"
            assert result.content_type == "text/html"
            assert result.retry_after == 10
    assert retry_delays == [0.25, 0.75]
    assert len(sessions.calls) == 3
    sessions.assert_complete()


@pytest.mark.parametrize("status", ["pending_validation", "quarantined", "expired", "banned"])
def test_inconclusive_preserves_each_original_inventory_status(settings, client, security, status):
    validator, sessions = _remote_validator(settings, security, [("GET", USERINFO, FakeResponse(503))] * 3)
    with client.app.state.session_factory.begin() as session:
        account = _account(security, status=status)
        session.add(account)
        session.flush()
        assert apply_validation(session, account, validator).outcome == "inconclusive"
        assert account.status == status
    sessions.assert_complete()


def test_failures_never_block_another_account_with_global_cooldown(settings, security):
    failed = [("GET", USERINFO, FakeResponse(429))] * 3
    validator, sessions = _remote_validator(settings, security, failed + failed + _success())
    assert validator.validate(_account(security)).outcome == "inconclusive"
    assert validator.validate(_account(security, account_id="another-account")).outcome == "inconclusive"
    assert validator.validate(_account(security, account_id="healthy-account")).outcome == "valid"
    assert len(sessions.calls) == 9
    sessions.assert_complete()


@pytest.mark.parametrize(("response", "outcome", "error_type"), [
    (FakeResponse(401), "invalid", "access_token"),
    (FakeResponse(403, payload={"detail": "Unauthorized"}), "invalid", "access_token"),
    (FakeResponse(403, payload={"error": {"code": "account_deactivated"}}), "invalid", "access_token"),
])
def test_terminal_failures_stop_without_retries_when_no_rt(settings, security, response, outcome, error_type, retry_delays):
    validator, sessions = _remote_validator(settings, security, [
        ("GET", USERINFO, FakeResponse()), ("POST", INIT, response),
    ])
    result = validator.validate(_account(security, refresh_token=""))
    assert result.outcome == outcome
    assert result.error_type == error_type
    assert retry_delays == []
    sessions.assert_complete()


def test_locally_expired_at_is_checked_before_consuming_rt(settings, security):
    validator, sessions = _remote_validator(settings, security, _success())
    result = validator.validate(_account(security, expires_at=utcnow() - timedelta(hours=1)))
    assert result.outcome == "valid"
    assert result.validated_via == "access_token"
    assert result.refreshed is False
    sessions.assert_complete()


def test_terminal_at_refreshes_once_then_checks_all_endpoints_and_persists_encrypted_rotation(settings, client, security):
    validator, sessions = _remote_validator(settings, security, [
        ("GET", USERINFO, FakeResponse()), ("POST", INIT, FakeResponse(401)),
        _refresh_success(), *_success(),
    ])
    with client.app.state.session_factory.begin() as session:
        account = _account(security, status="expired", proxy_used="http://account-proxy.example:8080")
        session.add(account)
        session.flush()
        result = apply_validation(session, account, validator)
        assert result.outcome == "valid"
        assert result.validated_via == "refresh_token"
        assert result.refreshed and result.recoverable
        assert result.current_access_token_valid is False
        assert result.current_access_token_error_type == "access_token"
        assert account.status == "available"
        assert security.decrypt(account.access_token_encrypted) == "new-access-token"
        assert security.decrypt(account.refresh_token_encrypted) == "new-refresh-token"
        assert security.decrypt(account.id_token_encrypted) == "new-id-token"
        assert "new-access-token" not in account.access_token_encrypted
        assert account.expires_at > utcnow() + timedelta(minutes=59)
        assert account.last_refresh is not None and account.version == 2
    assert all(call["proxies"] == {
        "http": "http://account-proxy.example:8080", "https": "http://account-proxy.example:8080",
    } for call in sessions.calls)
    assert sessions.calls[2]["data"] == {
        "client_id": "test-client", "grant_type": "refresh_token", "refresh_token": "old-refresh-token",
    }
    assert sessions.calls[3]["headers"]["authorization"] == "Bearer new-access-token"
    sessions.assert_complete()


@pytest.mark.parametrize(("confirmation", "expected_outcome", "expected_status"), [
    ([("GET", USERINFO, FakeResponse(401))], "invalid", "expired"),
    ([("GET", USERINFO, FakeResponse(403, text="account banned"))], "invalid", "banned"),
    ([("GET", USERINFO, FakeResponse(503))] * 3, "inconclusive", "available"),
])
def test_rotated_credentials_saved_even_when_new_at_check_fails(
    settings, client, security, confirmation, expected_outcome, expected_status,
):
    validator, sessions = _remote_validator(settings, security, [
        ("GET", USERINFO, FakeResponse(401)), _refresh_success(), *confirmation,
    ])
    with client.app.state.session_factory.begin() as session:
        account = _account(security)
        session.add(account)
        session.flush()
        result = apply_validation(session, account, validator)
        assert result.outcome == expected_outcome
        assert account.status == expected_status
        assert security.decrypt(account.access_token_encrypted) == "new-access-token"
        assert security.decrypt(account.refresh_token_encrypted) == "new-refresh-token"
        assert account.version == 2 and account.last_refresh is not None
    sessions.assert_complete()


@pytest.mark.parametrize(("failure", "outcome", "error_type"), [
    (FakeResponse(400, payload={"error": "invalid_grant"}), "invalid", "refresh_token"),
    (FakeResponse(400, payload={"error": "invalid_request"}), "invalid", "refresh_token"),
    (FakeResponse(401), "invalid", "refresh_token"),
    (FakeResponse(200), "invalid", "refresh_token"),
    (FakeResponse(429), "inconclusive", "transient"),
    (FakeResponse(503), "inconclusive", "transient"),
    (TimeoutError("timeout"), "inconclusive", "transient"),
])
def test_refresh_error_classification_matches_manager(settings, security, failure, outcome, error_type):
    validator, sessions = _remote_validator(settings, security, [
        ("GET", USERINFO, FakeResponse(401)), ("POST", REFRESH, failure),
    ])
    result = validator.validate(_account(security))
    assert result.outcome == outcome
    assert result.error_type == error_type
    assert result.validated_via == "access_token"
    sessions.assert_complete()


@pytest.mark.parametrize(("extra", "account_client", "expected_client", "expected_redirect"), [
    ({"refresh_token_client_id": "rt-client", "oauth_client_id": "oauth-client",
      "oauth_redirect_uri": "https://callback.example/return"}, "account-client", "rt-client", "https://callback.example/return"),
    ({"oauth_client_id": "oauth-client"}, "account-client", "oauth-client", "http://localhost:1455/auth/callback"),
    ({}, "account-client", "account-client", "http://localhost:1455/auth/callback"),
    ({}, None, "default-client", "http://localhost:1455/auth/callback"),
    ({"refresh_token_client_id": PLATFORM_OAUTH_CLIENT_ID, "oauth_redirect_uri": "https://ignored.example"},
     "account-client", PLATFORM_OAUTH_CLIENT_ID, None),
    ({}, PLATFORM_OAUTH_CLIENT_ID, PLATFORM_OAUTH_CLIENT_ID, None),
])
def test_refresh_client_and_redirect_resolution(settings, security, extra, account_client, expected_client, expected_redirect):
    settings = replace(settings, oauth_client_id="default-client",
                       oauth_redirect_uri="http://localhost:1455/auth/callback")
    validator, sessions = _remote_validator(settings, security, [_refresh_success(), *_success()])
    result = validator.validate(_account(security, access_token="", extra_data=extra, client_id=account_client))
    assert result.outcome == "valid"
    data = sessions.calls[0]["data"]
    assert data["client_id"] == expected_client
    assert data.get("redirect_uri") == expected_redirect
    sessions.assert_complete()


@pytest.mark.parametrize(("mode", "explicit", "expected"), [
    ("direct", None, None), ("account", None, "http://account.example:8080"),
    ("pool", None, "http://pool.example:8080"),
    ("direct", "http://override.example:8080", "http://override.example:8080"),
    ("unknown", None, None),
])
def test_exit_selection_is_shared_by_checks_refresh_and_retries(settings, security, monkeypatch, mode, explicit, expected):
    choices = []
    def choose(pool):
        choices.append(pool)
        return pool[0]
    monkeypatch.setattr("app.services.validator.random.choice", choose)
    validator, sessions = _remote_validator(replace(
        settings, validation_egress_mode=mode, validation_proxy="http://global.example:8080",
        validation_proxy_pool="http://pool.example:8080,http://other.example:8080",
    ), security, [
        ("GET", USERINFO, FakeResponse(429)), ("GET", USERINFO, FakeResponse(401)),
        _refresh_success(), *_success(),
    ])
    result = validator.validate(_account(security, extra_data={"proxy_url": "http://account.example:8080"}),
                                validation_proxy=explicit)
    assert result.outcome == "valid"
    for call in sessions.calls:
        if expected:
            assert call["proxies"] == {"http": expected, "https": expected}
        else:
            assert "proxies" not in call
    assert len(choices) == (1 if mode == "pool" and not explicit else 0)
    sessions.assert_complete()


def test_missing_both_credentials_is_invalid_without_network(settings, security):
    validator, sessions = _remote_validator(settings, security, [])
    result = validator.validate(_account(security, access_token="", refresh_token=""))
    assert result.outcome == "invalid"
    assert not sessions.calls


def test_diagnostic_messages_do_not_persist_echoed_credentials(settings, client, security):
    failure = FakeResponse(503, text="old-access-token old-refresh-token new-access-token new-refresh-token new-id-token")
    validator, sessions = _remote_validator(settings, security, [
        ("GET", USERINFO, FakeResponse(401)), _refresh_success(),
        *[("GET", USERINFO, failure)] * 3,
    ])
    with client.app.state.session_factory.begin() as session:
        account = _account(security)
        session.add(account)
        session.flush()
        result = apply_validation(session, account, validator)
        session.flush()
        attempt = session.query(ValidationAttempt).filter_by(account_id=account.id).one()
        diagnostic = json.dumps(validation_result_details(result)) + result.message + attempt.message
        for credential in ("old-access-token", "old-refresh-token", "new-access-token", "new-refresh-token", "new-id-token"):
            assert credential not in diagnostic
    sessions.assert_complete()


def test_refresh_normalizes_jwt_artifacts_without_plaintext_credentials_in_metadata(settings, client, security):
    from app.services.token_artifacts import build_compat_id_token, decode_jwt_payload

    access_token = build_compat_id_token(
        email="person@example.com", exp=2000000000,
        account_id="refreshed-account", user_id="chatgpt-user", plan_type="plus",
    )
    validator, sessions = _remote_validator(settings, security, [
        ("GET", USERINFO, FakeResponse(401)),
        ("POST", REFRESH, FakeResponse(payload={
            "access_token": access_token, "refresh_token": "rotated-rt", "expires_in": 3600,
        })), *_success(),
    ])
    with client.app.state.session_factory.begin() as session:
        account = _account(
            security, extra_data={"oauth_client_id": "metadata-client",
                                  "credentials": {"session_token": "saved-session"},
                                  "proxy_used": "http://historical.example:8080"},
        )
        session.add(account)
        session.flush()
        result = apply_validation(session, account, validator)
        assert result.outcome == "valid"
        assert result.refreshed_access_token_valid is True
        assert result.current_access_token_valid is False
        assert account.account_id == account.workspace_id == "refreshed-account"
        assert account.proxy_used is None
        assert security.decrypt(account.session_token_encrypted) == "saved-session"
        compat_id = security.decrypt(account.id_token_encrypted)
        assert decode_jwt_payload(compat_id)["https://api.openai.com/auth"]["chatgpt_account_id"] == "refreshed-account"
        metadata = json.loads(account.extra_data)
        assert metadata["plan_type"] == "plus"
        assert metadata["oauth_client_id"] == "metadata-client"
        assert metadata["token_artifacts"]["chatgpt_user_id"] == "chatgpt-user"
        assert "proxy_used" not in metadata
        for credential in (access_token, "rotated-rt", "saved-session", compat_id):
            assert credential not in account.extra_data
        assert "session_token" not in metadata["credentials"]
        assert "id_token" not in metadata["token_artifacts"]
    sessions.assert_complete()


def test_current_at_diagnostic_redacts_all_known_credentials(settings, security):
    failure = FakeResponse(503, text="old-access-token old-refresh-token")
    validator, sessions = _remote_validator(settings, security, [("GET", USERINFO, failure)] * 3)
    result = validator.validate(_account(security))
    assert "old-access-token" not in json.dumps(validation_result_details(result))
    assert "old-refresh-token" not in result.message
    sessions.assert_complete()


def test_redaction_precedes_upstream_body_truncation(settings, security):
    token = "long-private-access-token-" * 50
    failure = FakeResponse(503, text=f"upstream echoed {token}")
    validator, sessions = _remote_validator(settings, security, [("GET", USERINFO, failure)] * 3)
    result = validator.validate(_account(security, access_token=token))
    assert "long-private-access-token" not in result.message
    assert "[redacted]" in result.current_access_token_error
    sessions.assert_complete()
