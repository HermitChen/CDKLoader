from __future__ import annotations

from datetime import timedelta

import pytest
from curl_cffi.const import CurlECode
from curl_cffi.requests.exceptions import ProxyError, SSLError, Timeout as CurlTimeout

from app.models import Account, ProxyNodeHealth, utcnow
from app.security import SecurityManager
from app.services.proxy_health import (
    PROBE_URL,
    ProxyHealthPolicy,
    ProxyProbeResult,
    _apply_result,
    format_probe_error,
    probe_proxy,
    proxy_fingerprint,
)
from app.services.validator import TokenValidator


def test_probe_uses_fresh_proxy_request_without_account_credentials(monkeypatch):
    calls = []

    class Response:
        status_code = 204
        closed = False

        def close(self):
            self.closed = True

    class Session:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return response

        def close(self):
            calls.append("session-closed")

    response = Response()
    monkeypatch.setattr("app.services.proxy_health.cffi_requests.Session", lambda **_: Session())
    monkeypatch.setattr("app.services.proxy_health.time.perf_counter", iter((1.0, 1.018)).__next__)

    result = probe_proxy("http://user:secret@proxy.example:8080", timeout_ms=5000)

    assert result.ok
    assert result.status_code == 204
    assert result.latency_ms == 19
    url, kwargs = calls[0]
    assert url == PROBE_URL
    assert kwargs["proxies"] == {
        "http": "http://user:secret@proxy.example:8080",
        "https": "http://user:secret@proxy.example:8080",
    }
    assert kwargs["stream"] is True
    assert kwargs["allow_redirects"] is False
    assert not {key.lower() for key in kwargs["headers"]} & {"authorization", "cookie"}
    assert response.closed
    assert calls[-1] == "session-closed"


@pytest.mark.parametrize(("failure", "error_code", "inconclusive"), [
    (CurlTimeout("timeout", CurlECode.OPERATION_TIMEDOUT), "proxy_connection_timeout", False),
    (ProxyError("proxy DNS failure", CurlECode.COULDNT_RESOLVE_PROXY), "proxy_dns_failed", False),
    (ProxyError("User was rejected by the SOCKS5 server: private-password", CurlECode.PROXY),
     "proxy_authentication_failed", False),
    (ProxyError("cannot complete SOCKS5 connection", CurlECode.PROXY), "proxy_socks_handshake_failed", False),
    (SSLError("certificate verification failed", CurlECode.PEER_FAILED_VERIFICATION),
     "proxy_tls_verification_failed", False),
    (RuntimeError("client error with private-password"), "probe_internal_error", True),
])
def test_probe_classifies_failures_without_exposing_upstream_secrets(monkeypatch, failure, error_code, inconclusive):
    class Session:
        def get(self, *_args, **_kwargs):
            raise failure

        def close(self):
            pass

    monkeypatch.setattr("app.services.proxy_health.cffi_requests.Session", lambda **_: Session())
    result = probe_proxy("socks5://user:private-password@proxy.example:1080")

    assert result.error_code == error_code
    assert result.inconclusive is inconclusive
    assert "private-password" not in str(result.as_dict()) + format_probe_error(result)


def test_empty_proxy_is_not_accidentally_probed_as_direct(monkeypatch):
    def unexpected_session(**_kwargs):
        pytest.fail("an empty proxy must not create a direct session")

    monkeypatch.setattr("app.services.proxy_health.cffi_requests.Session", unexpected_session)

    result = probe_proxy(" ")

    assert result.ok is False
    assert result.error_code == "proxy_configuration_invalid"
    assert result.inconclusive is True


def test_target_rejection_is_inconclusive_not_a_proxy_failure():
    result = ProxyProbeResult(latency_ms=22, status_code=403, error_code="probe_target_http_403", inconclusive=True)
    for blocked in (False, True):
        row = ProxyNodeHealth(
            node_id="node-1", fingerprint="f" * 64, checked_at=utcnow(), next_check_at=utcnow(),
            latency_ms=1300 if blocked else None, status_code=200 if blocked else None,
            error_code="latency_exceeded" if blocked else "", blocked=blocked,
            failures=2 if blocked else 0, recovery_successes=1,
        )

        _apply_result(row, result, utcnow(), ProxyHealthPolicy())

        assert row.blocked is blocked
        assert row.failures == (2 if blocked else 0)
        assert row.recovery_successes == 0
        assert row.error_code == "probe_target_http_403"


def test_slow_node_is_isolated_until_two_fast_recovery_samples():
    policy = ProxyHealthPolicy(enabled=True, interval_minutes=5, threshold_ms=1000)
    row = ProxyNodeHealth(
        node_id="node-1", fingerprint="f" * 64, checked_at=None, next_check_at=None,
        latency_ms=None, status_code=None, error_code="", blocked=False, failures=0,
        recovery_successes=0,
    )
    now = utcnow()

    _apply_result(row, ProxyProbeResult(1001, 200), now, policy)
    assert row.blocked is True
    assert row.failures == 1
    assert row.error_code == "latency_exceeded"
    assert row.next_check_at == now + timedelta(seconds=30)

    _apply_result(row, ProxyProbeResult(800, 200), now + timedelta(seconds=30), policy)
    assert row.blocked is True
    assert row.recovery_successes == 1

    _apply_result(row, ProxyProbeResult(800, 200), now + timedelta(seconds=60), policy)
    assert row.blocked is False
    assert row.failures == 0
    assert row.recovery_successes == 0


def test_repeated_connection_failures_back_off_to_one_minute():
    row = ProxyNodeHealth(
        node_id="node-1", fingerprint="f" * 64, checked_at=None, next_check_at=None,
        latency_ms=None, status_code=None, error_code="", blocked=False, failures=0,
        recovery_successes=0,
    )
    now = utcnow()
    failure = ProxyProbeResult(5000, error_code="proxy_connection_timeout")

    _apply_result(row, failure, now, ProxyHealthPolicy())
    _apply_result(row, failure, now + timedelta(seconds=30), ProxyHealthPolicy())

    assert row.blocked is True
    assert row.failures == 2
    assert row.next_check_at == now + timedelta(seconds=90)


def test_proxy_group_selection_skips_isolated_nodes(settings):
    from dataclasses import replace

    from app.config import ProxyGroupSetting, ProxyNodeSetting

    class Eligibility:
        def is_eligible(self, node_id, _proxy_url):
            return node_id == "healthy-node"

    group = ProxyGroupSetting(
        id="group-1", name="测试组", strategy="round_robin", rotation_interval_minutes=0,
        enabled=True, nodes=(
            ProxyNodeSetting(id="isolated-node", name="隔离", url="http://bad.example:8080"),
            ProxyNodeSetting(id="healthy-node", name="可用", url="http://good.example:8080"),
        ),
    )
    validator = TokenValidator(
        replace(settings, validation_egress_mode="group", validation_proxy_group_id=group.id,
                validation_proxy_groups=(group,)),
        SecurityManager("proxy-health-test", "proxy-health-pepper"),
        Eligibility(),
    )

    assert validator._validation_proxy_url(Account(email="person@example.com")) == "http://good.example:8080"


def test_proxy_health_settings_and_manual_test_are_diagnostic_only(client, admin_headers, monkeypatch):
    defaults = client.get("/api/v1/admin/proxy-health/settings", headers=admin_headers)
    assert defaults.status_code == 200
    assert defaults.json()["enabled"] is True
    assert defaults.json()["interval_minutes"] == 5
    assert defaults.json()["threshold_ms"] == 1000

    invalid = client.put(
        "/api/v1/admin/proxy-health/settings",
        headers=admin_headers,
        json={"enabled": True, "interval_minutes": 31, "threshold_ms": 1000},
    )
    assert invalid.status_code == 422

    saved = client.put(
        "/api/v1/admin/proxy-health/settings",
        headers=admin_headers,
        json={"enabled": False, "interval_minutes": 3, "threshold_ms": 800},
    )
    assert saved.status_code == 200
    assert saved.json()["enabled"] is False
    assert client.app.state.proxy_health_monitor.get_policy().threshold_ms == 800

    created = client.post(
        "/api/v1/admin/proxy-groups",
        headers=admin_headers,
        json={"name": "测活组", "nodes": [{"name": "节点一", "url": "http://user:secret@proxy.example:8080"}]},
    )
    assert created.status_code == 200
    group = created.json()["group"]
    node = group["nodes"][0]
    probe_options = []

    def test_probe(_url, **kwargs):
        probe_options.append(kwargs)
        return ProxyProbeResult(23, 403, "probe_target_http_403", True)

    monkeypatch.setattr(
        "app.api.admin.probe_proxy",
        test_probe,
    )

    tested = client.post(
        f"/api/v1/admin/proxy-groups/{group['id']}/nodes/{node['id']}/test",
        headers=admin_headers,
    )

    assert tested.status_code == 200
    assert tested.json()["result"]["ok"] is True
    assert tested.json()["result"]["latency_ms"] == 23
    assert tested.json()["result"]["status"] == 403
    assert tested.json()["result"]["inconclusive"] is True
    assert probe_options == [{"timeout_ms": 15000}]
    assert "secret" not in tested.text
    listed = client.get("/api/v1/admin/proxy-groups", headers=admin_headers).json()
    assert listed["groups"][0]["nodes"][0]["health"]["state"] == "unknown"


def test_automatic_monitor_persists_isolation_and_excludes_node(client, admin_headers, monkeypatch):
    created = client.post(
        "/api/v1/admin/proxy-groups",
        headers=admin_headers,
        json={"name": "自动检测组", "strategy": "round_robin", "nodes": [
            {"name": "慢节点", "url": "http://slow.example:8080"},
            {"name": "快节点", "url": "http://fast.example:8080"},
        ]},
    )
    group = created.json()["group"]
    nodes = group["nodes"]
    client.put(
        "/api/v1/admin/settings",
        headers=admin_headers,
        json={"values": {"validation_egress_mode": "group", "validation_proxy_group_id": group["id"]}},
    )
    with client.app.state.session_factory.begin() as session:
        for node in nodes:
            session.add(ProxyNodeHealth(
                node_id=node["id"],
                fingerprint=proxy_fingerprint(node["url"]),
                checked_at=utcnow() - timedelta(minutes=1),
                next_check_at=utcnow() - timedelta(seconds=1),
                blocked=False,
                failures=0,
                recovery_successes=0,
            ))

    monkeypatch.setattr(
        "app.services.proxy_health.probe_proxy",
        lambda url, **_kwargs: ProxyProbeResult(1500 if "slow." in url else 150, 200),
    )
    client.app.state.proxy_health_monitor.run_once()

    group_state = client.get("/api/v1/admin/proxy-groups", headers=admin_headers).json()["groups"][0]
    health_by_name = {node["name"]: node["health"] for node in group_state["nodes"]}
    assert health_by_name["慢节点"]["blocked"] is True
    assert health_by_name["慢节点"]["eligible"] is False
    assert health_by_name["快节点"]["state"] == "healthy"
    assert client.app.state.validator._validation_proxy_url(Account(email="person@example.com")) == "http://fast.example:8080"


def test_previous_local_dns_isolation_is_reset_after_transport_fix(client, admin_headers):
    import hashlib

    proxy = "socks5://proxy.example:1080"
    group = client.post(
        "/api/v1/admin/proxy-groups", headers=admin_headers,
        json={"name": "DNS 迁移组", "nodes": [{"url": proxy}]},
    ).json()["group"]
    node_id = group["nodes"][0]["id"]
    with client.app.state.session_factory.begin() as session:
        session.add(ProxyNodeHealth(
            node_id=node_id, fingerprint=hashlib.sha256(proxy.encode()).hexdigest(),
            checked_at=utcnow(), next_check_at=utcnow() + timedelta(minutes=5),
            error_code="proxy_connection_failed", blocked=True, failures=3, recovery_successes=0,
        ))

    client.app.state.proxy_health_monitor.load()
    client.app.state.proxy_health_monitor.run_once()

    with client.app.state.session_factory() as session:
        health = session.get(ProxyNodeHealth, node_id)
        assert health.fingerprint == proxy_fingerprint(proxy)
        assert health.blocked is False
        assert health.checked_at is None
        assert health.next_check_at > utcnow()
    snapshot = client.app.state.proxy_health_monitor.snapshot(node_id, proxy)
    assert snapshot["state"] == "unknown"
    assert snapshot["eligible"] is True
