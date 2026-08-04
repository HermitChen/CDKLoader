from __future__ import annotations

from collections import deque
from dataclasses import replace
from datetime import timedelta
from typing import Any

from app.models import Account, utcnow
from app.security import SecurityManager
from app.services.validator import TokenValidator, apply_validation


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: Any = None,
        text: str = "",
        headers: dict[str, str] | None = None,
    ):
        self.status_code = status_code
        self.payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self) -> Any:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class ScriptedSessions:
    def __init__(self, steps: list[tuple[str, str, FakeResponse]]):
        self.steps = deque(steps)
        self.calls: list[dict[str, Any]] = []

    def __call__(self) -> "ScriptedSession":
        return ScriptedSession(self)

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        assert self.steps, f"unexpected {method} {url}"
        expected_method, expected_url, response = self.steps.popleft()
        assert (method, url) == (expected_method, expected_url)
        self.calls.append({"method": method, "url": url, **kwargs})
        return response

    def assert_complete(self) -> None:
        assert not self.steps


class ScriptedSession:
    def __init__(self, owner: ScriptedSessions):
        self.owner = owner

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.owner.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.owner.request("POST", url, **kwargs)

    def close(self) -> None:
        pass


def _response(
    status_code: int = 200,
    payload: Any = None,
    text: str = "",
    headers: dict[str, str] | None = None,
) -> FakeResponse:
    return FakeResponse(status_code, {} if payload is None else payload, text, headers)


def _account(
    security: SecurityManager,
    *,
    access_token: str = "old-access-token",
    refresh_token: str = "old-refresh-token",
    expires_at=None,
) -> Account:
    return Account(
        email="person@example.com",
        account_id="account-validator-test",
        access_token_encrypted=security.encrypt(access_token),
        refresh_token_encrypted=security.encrypt(refresh_token),
        expires_at=expires_at,
    )


def _remote_validator(settings, security: SecurityManager, steps: list[tuple[str, str, FakeResponse]]):
    validator = TokenValidator(replace(settings, validation_mode="remote", validation_attempts=1), security)
    sessions = ScriptedSessions(steps)
    validator._session = sessions
    return validator, sessions


def _userinfo() -> str:
    return TokenValidator.validation_url


def _conversation_init() -> str:
    return TokenValidator.chatgpt_base_url + TokenValidator.conversation_init_path


def _account_check() -> str:
    return (
        TokenValidator.chatgpt_base_url
        + TokenValidator.account_check_path
        + "?timezone_offset_min=-480"
    )


def test_remote_validation_requires_userinfo_and_both_product_probes(settings):
    security = SecurityManager("validator-secret", "validator-pepper")
    validator, sessions = _remote_validator(
        settings,
        security,
        [
            ("GET", _userinfo(), _response(payload={"sub": "user-1"})),
            ("POST", _conversation_init(), _response(payload={"limits_progress": []})),
            ("GET", _account_check(), _response(payload={"accounts": {}})),
        ],
    )

    result = validator.validate(_account(security))

    assert result.outcome == "valid"
    assert [(call["method"], call["url"]) for call in sessions.calls] == [
        ("GET", _userinfo()),
        ("POST", _conversation_init()),
        ("GET", _account_check()),
    ]
    conversation_headers = sessions.calls[1]["headers"]
    account_headers = sessions.calls[2]["headers"]
    assert conversation_headers["authorization"] == "Bearer old-access-token"
    assert conversation_headers["x-openai-target-path"] == TokenValidator.conversation_init_path
    assert account_headers["x-openai-target-route"] == TokenValidator.account_check_path
    assert conversation_headers["oai-device-id"] == account_headers["oai-device-id"]
    assert conversation_headers["oai-session-id"] == account_headers["oai-session-id"]
    sessions.assert_complete()


def test_product_401_forces_refresh_and_rechecks_all_probes(settings):
    security = SecurityManager("validator-secret", "validator-pepper")
    validator, sessions = _remote_validator(
        settings,
        security,
        [
            ("GET", _userinfo(), _response(payload={"sub": "user-1"})),
            ("POST", _conversation_init(), _response(401)),
            ("GET", _account_check(), _response(payload={"accounts": {}})),
            (
                "POST",
                TokenValidator.token_url,
                _response(payload={"access_token": "new-access-token", "refresh_token": "new-refresh-token", "expires_in": 3600}),
            ),
            ("GET", _userinfo(), _response(payload={"sub": "user-1"})),
            ("POST", _conversation_init(), _response(payload={"limits_progress": []})),
            ("GET", _account_check(), _response(payload={"accounts": {}})),
        ],
    )

    result = validator.validate(_account(security))

    assert result.outcome == "valid"
    assert result.validated_via == "refresh_token"
    assert result.access_token == "new-access-token"
    assert result.refresh_token == "new-refresh-token"
    assert [(call["method"], call["url"]) for call in sessions.calls] == [
        ("GET", _userinfo()),
        ("POST", _conversation_init()),
        ("GET", _account_check()),
        ("POST", TokenValidator.token_url),
        ("GET", _userinfo()),
        ("POST", _conversation_init()),
        ("GET", _account_check()),
    ]
    sessions.assert_complete()


def test_product_401_without_refresh_token_is_invalid(settings):
    security = SecurityManager("validator-secret", "validator-pepper")
    validator, sessions = _remote_validator(
        settings,
        security,
        [
            ("GET", _userinfo(), _response(payload={"sub": "user-1"})),
            ("POST", _conversation_init(), _response(401)),
            ("GET", _account_check(), _response(payload={"accounts": {}})),
        ],
    )

    result = validator.validate(_account(security, refresh_token=""))

    assert result.outcome == "invalid"
    assert result.error_type == "access_token"
    sessions.assert_complete()


def test_product_403_is_inconclusive_even_when_response_mentions_unauthorized(settings):
    security = SecurityManager("validator-secret", "validator-pepper")
    validator, sessions = _remote_validator(
        settings,
        security,
        [
            ("GET", _userinfo(), _response(payload={"sub": "user-1"})),
            ("POST", _conversation_init(), _response(403, text="unauthorized")),
        ],
    )

    result = validator.validate(_account(security))

    assert result.outcome == "inconclusive"
    assert result.error_type == "transient"
    assert all(call["url"] != TokenValidator.token_url for call in sessions.calls)
    sessions.assert_complete()


def test_product_gate_retries_with_retry_after_and_skips_second_probe(settings, monkeypatch):
    security = SecurityManager("validator-secret", "validator-pepper")
    validator = TokenValidator(
        replace(
            settings,
            validation_mode="remote",
            validation_attempts=2,
            validation_retry_base_seconds=1,
            validation_retry_max_seconds=10,
            validation_retry_jitter_seconds=0,
        ),
        security,
    )
    sessions = ScriptedSessions(
        [
            ("GET", _userinfo(), _response(payload={"sub": "user-1"})),
            ("POST", _conversation_init(), _response(403, headers={"Retry-After": "2"})),
            ("POST", _conversation_init(), _response(payload={"limits_progress": []})),
            ("GET", _account_check(), _response(payload={"accounts": {}})),
        ]
    )
    validator._session = sessions
    sleeps: list[float] = []
    monkeypatch.setattr("app.services.validator.time.sleep", sleeps.append)

    result = validator.validate(_account(security))

    assert result.outcome == "valid"
    assert sleeps == [2]
    assert [(call["method"], call["url"]) for call in sessions.calls] == [
        ("GET", _userinfo()),
        ("POST", _conversation_init()),
        ("POST", _conversation_init()),
        ("GET", _account_check()),
    ]
    conversation_calls = [
        call for call in sessions.calls if call["url"] == _conversation_init()
    ]
    assert conversation_calls[0]["headers"]["oai-device-id"] == conversation_calls[1]["headers"]["oai-device-id"]
    assert conversation_calls[0]["headers"]["oai-session-id"] == conversation_calls[1]["headers"]["oai-session-id"]
    sessions.assert_complete()


def test_product_gate_uses_exponential_backoff_without_retry_after(settings, monkeypatch):
    security = SecurityManager("validator-secret", "validator-pepper")
    validator = TokenValidator(
        replace(
            settings,
            validation_mode="remote",
            validation_attempts=3,
            validation_retry_base_seconds=1,
            validation_retry_max_seconds=10,
            validation_retry_jitter_seconds=0,
        ),
        security,
    )
    sessions = ScriptedSessions(
        [
            ("GET", _userinfo(), _response(payload={"sub": "user-1"})),
            ("POST", _conversation_init(), _response(403)),
            ("POST", _conversation_init(), _response(403)),
            ("POST", _conversation_init(), _response(403)),
        ]
    )
    validator._session = sessions
    sleeps: list[float] = []
    monkeypatch.setattr("app.services.validator.time.sleep", sleeps.append)

    result = validator.validate(_account(security))

    assert result.outcome == "inconclusive"
    assert sleeps == [1, 2]
    assert [call["url"] for call in sessions.calls] == [
        _userinfo(),
        _conversation_init(),
        _conversation_init(),
        _conversation_init(),
    ]
    sessions.assert_complete()


def test_only_terminal_refresh_failure_marks_account_invalid(settings):
    security = SecurityManager("validator-secret", "validator-pepper")
    validator, sessions = _remote_validator(
        settings,
        security,
        [
            ("GET", _userinfo(), _response(payload={"sub": "user-1"})),
            ("POST", _conversation_init(), _response(401)),
            ("GET", _account_check(), _response(payload={"accounts": {}})),
            ("POST", TokenValidator.token_url, _response(400, payload={"error": "invalid_grant"})),
        ],
    )

    result = validator.validate(_account(security))

    assert result.outcome == "invalid"
    assert result.error_type == "refresh_token"
    sessions.assert_complete()


def test_nonterminal_refresh_failure_is_inconclusive(settings):
    security = SecurityManager("validator-secret", "validator-pepper")
    validator, sessions = _remote_validator(
        settings,
        security,
        [
            ("GET", _userinfo(), _response(payload={"sub": "user-1"})),
            ("POST", _conversation_init(), _response(401)),
            ("GET", _account_check(), _response(payload={"accounts": {}})),
            ("POST", TokenValidator.token_url, _response(400, payload={"error": "invalid_request"})),
        ],
    )

    result = validator.validate(_account(security))

    assert result.outcome == "inconclusive"
    assert result.error_type == "transient"
    sessions.assert_complete()


def test_expiring_access_token_refreshes_before_remote_probes(settings):
    security = SecurityManager("validator-secret", "validator-pepper")
    validator, sessions = _remote_validator(
        settings,
        security,
        [
            (
                "POST",
                TokenValidator.token_url,
                _response(payload={"access_token": "new-access-token", "refresh_token": "new-refresh-token", "expires_in": 3600}),
            ),
            ("GET", _userinfo(), _response(payload={"sub": "user-1"})),
            ("POST", _conversation_init(), _response(payload={"limits_progress": []})),
            ("GET", _account_check(), _response(payload={"accounts": {}})),
        ],
    )

    result = validator.validate(_account(security, expires_at=utcnow() - timedelta(seconds=1)))

    assert result.outcome == "valid"
    assert sessions.calls[0]["url"] == TokenValidator.token_url
    sessions.assert_complete()


def test_rejected_refreshed_token_is_quarantined_and_rotated_credentials_are_saved(settings, client):
    security = SecurityManager("validator-secret", "validator-pepper")
    validator, sessions = _remote_validator(
        settings,
        security,
        [
            ("GET", _userinfo(), _response(payload={"sub": "user-1"})),
            ("POST", _conversation_init(), _response(401)),
            ("GET", _account_check(), _response(payload={"accounts": {}})),
            (
                "POST",
                TokenValidator.token_url,
                _response(payload={"access_token": "new-access-token", "refresh_token": "new-refresh-token", "expires_in": 3600}),
            ),
            ("GET", _userinfo(), _response(payload={"sub": "user-1"})),
            ("POST", _conversation_init(), _response(401)),
            ("GET", _account_check(), _response(payload={"accounts": {}})),
        ],
    )
    account = _account(security)

    with client.app.state.session_factory.begin() as session:
        session.add(account)
        session.flush()
        result = apply_validation(session, account, validator)

        assert result.outcome == "inconclusive"
        assert account.status == "quarantined"
        assert security.decrypt(account.access_token_encrypted) == "new-access-token"
        assert security.decrypt(account.refresh_token_encrypted) == "new-refresh-token"
        assert account.last_refresh is not None
        assert account.version == 2
    sessions.assert_complete()
