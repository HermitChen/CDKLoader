from __future__ import annotations

import hashlib
import math
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from curl_cffi import requests as cffi_requests
from curl_cffi.const import CurlECode
from curl_cffi.curl import CurlError
from curl_cffi.requests.exceptions import Timeout as CurlTimeout
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from ..models import ProxyGroup, ProxyNode, ProxyNodeHealth, SystemSetting, utcnow
from ..security import SecurityManager
from .openai_validation import latest_chrome_impersonate
from .proxy_transport import proxy_request_mapping, transport_proxy_url


PROBE_URL = "https://chatgpt.com/api/auth/csrf"
SETTING_PREFIX = "proxy_health_check."
DEFAULT_INTERVAL_MINUTES = 5
DEFAULT_THRESHOLD_MS = 1000
MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 30
MIN_THRESHOLD_MS = 1
MAX_THRESHOLD_MS = 60000
RECOVERY_SAMPLES = 2
OBSERVATION_RETENTION = timedelta(hours=24)
MAX_PARALLEL_PROBES = 3
MAX_PROBES_PER_SWEEP = 12
MANUAL_PROXY_TEST_TIMEOUT_MS = 15000


@dataclass(frozen=True)
class ProxyHealthPolicy:
    enabled: bool = True
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES
    threshold_ms: int = DEFAULT_THRESHOLD_MS

    @property
    def interval(self) -> timedelta:
        return timedelta(minutes=self.interval_minutes)

    @property
    def recovery_threshold_ms(self) -> float:
        return self.threshold_ms * 0.8

    @property
    def timeout_ms(self) -> int:
        return max(5000, self.threshold_ms)

    @property
    def max_sample_age(self) -> timedelta:
        return max(timedelta(minutes=15), self.interval * 3)

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "interval_minutes": self.interval_minutes,
            "threshold_ms": self.threshold_ms,
            "recovery_threshold_ms": self.recovery_threshold_ms,
            "recovery_checks": RECOVERY_SAMPLES,
            "timeout_ms": self.timeout_ms,
            "min_interval_minutes": MIN_INTERVAL_MINUTES,
            "max_interval_minutes": MAX_INTERVAL_MINUTES,
            "min_threshold_ms": MIN_THRESHOLD_MS,
            "max_threshold_ms": MAX_THRESHOLD_MS,
        }


@dataclass(frozen=True)
class ProxyProbeResult:
    latency_ms: int
    status_code: int | None = None
    error_code: str = ""
    inconclusive: bool = False

    @property
    def ok(self) -> bool:
        return not self.error_code and self.status_code is not None and 200 <= self.status_code < 300

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "latency_ms": self.latency_ms,
            "status": self.status_code,
            "error": self.error_code,
        }


@dataclass(frozen=True)
class _Target:
    node_id: str
    url: str
    fingerprint: str


def proxy_fingerprint(proxy_url: str) -> str:
    # Discard observations from the previous local-DNS SOCKS5 route after an
    # upgrade; they do not describe the transport now used by validation.
    return hashlib.sha256(transport_proxy_url(proxy_url).encode("utf-8")).hexdigest()


def read_proxy_health_policy(session: Session) -> ProxyHealthPolicy:
    rows = session.scalars(
        select(SystemSetting).where(SystemSetting.key.in_((
            f"{SETTING_PREFIX}enabled",
            f"{SETTING_PREFIX}interval_minutes",
            f"{SETTING_PREFIX}threshold_ms",
        )))
    ).all()
    values = {row.key: row.value for row in rows}
    try:
        interval = int(values.get(f"{SETTING_PREFIX}interval_minutes", DEFAULT_INTERVAL_MINUTES))
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL_MINUTES
    try:
        threshold = int(values.get(f"{SETTING_PREFIX}threshold_ms", DEFAULT_THRESHOLD_MS))
    except (TypeError, ValueError):
        threshold = DEFAULT_THRESHOLD_MS
    enabled_value = values.get(f"{SETTING_PREFIX}enabled", "true").strip().lower()
    return ProxyHealthPolicy(
        enabled=enabled_value in {"1", "true", "yes", "on"},
        interval_minutes=min(MAX_INTERVAL_MINUTES, max(MIN_INTERVAL_MINUTES, interval)),
        threshold_ms=min(MAX_THRESHOLD_MS, max(MIN_THRESHOLD_MS, threshold)),
    )


def save_proxy_health_policy(session: Session, policy: ProxyHealthPolicy) -> None:
    previous = read_proxy_health_policy(session)
    now = utcnow()
    threshold_changed = previous.threshold_ms != policy.threshold_ms
    enabled_changed = previous.enabled != policy.enabled
    interval_changed = previous.interval_minutes != policy.interval_minutes
    values = {
        f"{SETTING_PREFIX}enabled": "true" if policy.enabled else "false",
        f"{SETTING_PREFIX}interval_minutes": str(policy.interval_minutes),
        f"{SETTING_PREFIX}threshold_ms": str(policy.threshold_ms),
    }
    rows = {
        row.key: row
        for row in session.scalars(select(SystemSetting).where(SystemSetting.key.in_(values))).all()
    }
    for key, value in values.items():
        if key in rows:
            rows[key].value = value
        else:
            session.add(SystemSetting(key=key, value=value))
    if threshold_changed or enabled_changed or interval_changed:
        for row in session.scalars(select(ProxyNodeHealth)).all():
            if threshold_changed or enabled_changed:
                row.recovery_successes = 0
            if threshold_changed:
                row.next_check_at = _staggered_time(now)
            elif enabled_changed and policy.enabled and (row.next_check_at is None or row.next_check_at <= now):
                row.next_check_at = _staggered_time(now)
            elif interval_changed and not row.blocked:
                if row.checked_at:
                    row.next_check_at = row.checked_at + timedelta(
                        milliseconds=random.uniform(0.8, 1.2) * policy.interval.total_seconds() * 1000
                    )
                else:
                    row.next_check_at = _staggered_time(now)
    session.flush()


def _probe_error_code(exc: Exception) -> tuple[str, bool]:
    """Return bounded diagnostics, never the upstream error or credentials."""
    code = getattr(exc, "code", None)
    if isinstance(exc, (CurlTimeout, TimeoutError)) or code == CurlECode.OPERATION_TIMEDOUT:
        return "proxy_connection_timeout", False
    if code == CurlECode.COULDNT_RESOLVE_PROXY:
        return "proxy_dns_failed", False
    if code == CurlECode.COULDNT_RESOLVE_HOST:
        return "proxy_target_dns_failed", False
    if code == CurlECode.PEER_FAILED_VERIFICATION:
        return "proxy_tls_verification_failed", False
    if code == CurlECode.SSL_CONNECT_ERROR:
        return "proxy_tls_handshake_failed", False
    if code == CurlECode.PROXY:
        detail = str(exc).lower()
        if any(marker in detail for marker in (
            "user was rejected", "authentication failed", "no authentication method",
            "no acceptable authentication", "username/password authentication",
        )):
            return "proxy_authentication_failed", False
        return "proxy_socks_handshake_failed", False
    if isinstance(exc, (CurlError, OSError)):
        return "proxy_connection_failed", False
    # A client/configuration/programming error is not evidence of a bad node.
    return "probe_internal_error", True


def probe_proxy(proxy_url: str, *, timeout_ms: int = 5000) -> ProxyProbeResult:
    timeout = max(1, timeout_ms) / 1000
    session = None
    response = None
    started = time.perf_counter()
    try:
        proxy_mapping = proxy_request_mapping(proxy_url)
        if proxy_mapping is None:
            return ProxyProbeResult(0, error_code="proxy_configuration_invalid", inconclusive=True)
        session = cffi_requests.Session(impersonate=latest_chrome_impersonate(), trust_env=False)
        response = session.get(
            PROBE_URL,
            headers={
                "accept": "application/json",
                "user-agent": "Mozilla/5.0 (CDK Loader proxy health check)",
            },
            proxies=proxy_mapping,
            timeout=timeout,
            stream=True,
            allow_redirects=False,
        )
        latency_ms = max(0, math.ceil((time.perf_counter() - started) * 1000))
        status_code = int(response.status_code)
        if 200 <= status_code < 300:
            return ProxyProbeResult(latency_ms, status_code)
        return ProxyProbeResult(latency_ms, status_code, f"probe_target_http_{status_code}", True)
    except Exception as exc:
        code, inconclusive = _probe_error_code(exc)
        latency_ms = max(0, math.ceil((time.perf_counter() - started) * 1000))
        return ProxyProbeResult(latency_ms, error_code=code, inconclusive=inconclusive)
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


def _staggered_time(now: datetime) -> datetime:
    return now + timedelta(seconds=random.randint(1, 30))


def _clear_observation(row: ProxyNodeHealth) -> None:
    row.checked_at = None
    row.latency_ms = None
    row.status_code = None
    row.error_code = ""
    row.blocked = False
    row.failures = 0
    row.recovery_successes = 0


def _apply_result(row: ProxyNodeHealth, result: ProxyProbeResult, now: datetime, policy: ProxyHealthPolicy) -> None:
    if row.checked_at and now - row.checked_at > policy.max_sample_age:
        row.recovery_successes = 0
    row.checked_at = now
    row.latency_ms = result.latency_ms
    row.status_code = result.status_code
    row.error_code = result.error_code

    if result.inconclusive:
        row.recovery_successes = 0
        row.next_check_at = now + policy.interval
        return

    if result.error_code or result.latency_ms > policy.threshold_ms:
        row.blocked = True
        row.failures = min(row.failures + 1, 5)
        row.recovery_successes = 0
        if not result.error_code:
            row.error_code = "latency_exceeded"
        retry_delays = (30, 60, 120, 300)
        delay = retry_delays[min(row.failures - 1, len(retry_delays) - 1)]
        row.next_check_at = now + timedelta(seconds=delay)
        return

    if row.blocked:
        if result.latency_ms <= policy.recovery_threshold_ms:
            row.recovery_successes += 1
        else:
            row.recovery_successes = 0
        if row.recovery_successes < RECOVERY_SAMPLES:
            row.next_check_at = now + timedelta(seconds=30)
            return

    row.blocked = False
    row.failures = 0
    row.recovery_successes = 0
    interval_ms = policy.interval.total_seconds() * 1000
    jitter_ms = random.uniform(0.8, 1.2) * interval_ms
    row.next_check_at = now + timedelta(milliseconds=jitter_ms)


class ProxyHealthMonitor:
    def __init__(self, session_factory: sessionmaker[Session], security: SecurityManager):
        self._session_factory = session_factory
        self._security = security
        self._lock = threading.RLock()
        self._policy = ProxyHealthPolicy()
        self._generation = 0
        self._observations: dict[str, dict[str, Any]] = {}

    def set_policy(self, policy: ProxyHealthPolicy) -> None:
        with self._lock:
            if policy != self._policy:
                reset_recovery = (
                    policy.threshold_ms != self._policy.threshold_ms
                    or policy.enabled != self._policy.enabled
                )
                self._generation += 1
                if reset_recovery:
                    for item in self._observations.values():
                        item["recovery_successes"] = 0
            self._policy = policy

    def _policy_generation(self) -> int:
        with self._lock:
            return self._generation

    def get_policy(self) -> ProxyHealthPolicy:
        with self._lock:
            return self._policy

    def load(self) -> None:
        now = utcnow()
        with self._session_factory() as session:
            policy = read_proxy_health_policy(session)
            rows = session.execute(
                select(ProxyNodeHealth, ProxyNode)
                .join(ProxyNode, ProxyNode.id == ProxyNodeHealth.node_id)
            ).all()
        observations: dict[str, dict[str, Any]] = {}
        for row, node in rows:
            if row.checked_at and now - row.checked_at > OBSERVATION_RETENTION:
                continue
            url = self._security.decrypt(node.url_encrypted)
            if row.fingerprint == proxy_fingerprint(url):
                observations[node.id] = self._row_state(row)
        with self._lock:
            self._policy = policy
            self._observations = observations

    def snapshot(self, node_id: str, proxy_url: str) -> dict[str, Any]:
        fingerprint = proxy_fingerprint(proxy_url)
        now = utcnow()
        with self._lock:
            policy = self._policy
            item = self._observations.get(node_id)
            if not item or item["fingerprint"] != fingerprint:
                return self._unknown_state()
            checked_at = item["checked_at"]
            if checked_at and now - checked_at > OBSERVATION_RETENTION:
                return self._unknown_state()
            state = dict(item)
        state.pop("fingerprint", None)
        blocked = bool(state["blocked"])
        state.update(
            state="unhealthy" if blocked else (
                "healthy" if state["checked_at"] and not state["error"] else "unknown"
            ),
            eligible=not policy.enabled or not blocked,
        )
        return state

    def is_eligible(self, node_id: str, proxy_url: str) -> bool:
        return bool(self.snapshot(node_id, proxy_url)["eligible"])

    @staticmethod
    def _unknown_state() -> dict[str, Any]:
        return {
            "state": "unknown",
            "checked_at": None,
            "next_check_at": None,
            "latency_ms": None,
            "status_code": None,
            "error": "",
            "eligible": True,
            "blocked": False,
            "recovery_successes": 0,
        }

    @staticmethod
    def _row_state(row: ProxyNodeHealth) -> dict[str, Any]:
        return {
            "fingerprint": row.fingerprint,
            "checked_at": row.checked_at,
            "next_check_at": row.next_check_at,
            "latency_ms": row.latency_ms,
            "status_code": row.status_code,
            "error": row.error_code,
            "blocked": bool(row.blocked),
            "recovery_successes": int(row.recovery_successes or 0),
        }

    def _remember(self, row: ProxyNodeHealth) -> None:
        with self._lock:
            self._observations[row.node_id] = self._row_state(row)

    def run_once(self) -> None:
        now = utcnow()
        targets: list[_Target] = []
        with self._session_factory.begin() as session:
            policy = read_proxy_health_policy(session)
            self.set_policy(policy)
            policy_generation = self._policy_generation()
            if not policy.enabled:
                return
            groups = session.scalars(
                select(ProxyGroup)
                .options(selectinload(ProxyGroup.nodes))
                .where(ProxyGroup.enabled.is_(True))
            ).all()
            due: list[_Target] = []
            for group in groups:
                for node in group.nodes:
                    if not node.enabled:
                        continue
                    proxy_url = self._security.decrypt(node.url_encrypted)
                    fingerprint = proxy_fingerprint(proxy_url)
                    row = session.get(ProxyNodeHealth, node.id)
                    if row is None:
                        row = ProxyNodeHealth(
                            node_id=node.id,
                            fingerprint=fingerprint,
                            next_check_at=_staggered_time(now),
                        )
                        session.add(row)
                    elif row.fingerprint != fingerprint or (
                        row.checked_at and now - row.checked_at > OBSERVATION_RETENTION
                    ):
                        row.fingerprint = fingerprint
                        _clear_observation(row)
                        row.next_check_at = _staggered_time(now)
                    elif row.next_check_at is None:
                        row.next_check_at = _staggered_time(now)
                    elif row.next_check_at <= now:
                        due.append(_Target(node.id, proxy_url, fingerprint))
                    self._remember(row)
            due.sort(key=lambda item: item.node_id)
            targets = due[:MAX_PROBES_PER_SWEEP]

        if not targets:
            return
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_PROBES, thread_name_prefix="proxy-health") as pool:
            results = list(pool.map(
                lambda target: probe_proxy(target.url, timeout_ms=policy.timeout_ms),
                targets,
            ))
        for target, result in zip(targets, results, strict=True):
            state = self._save_result(target, result, policy_generation)
            if state is not None:
                with self._lock:
                    self._observations[target.node_id] = state

    def _save_result(
        self,
        target: _Target,
        result: ProxyProbeResult,
        policy_generation: int,
    ) -> dict[str, Any] | None:
        now = utcnow()
        with self._session_factory.begin() as session:
            policy = read_proxy_health_policy(session)
            self.set_policy(policy)
            if not policy.enabled or self._policy_generation() != policy_generation:
                return None
            node = session.get(ProxyNode, target.node_id)
            if node is None or not node.enabled or not node.group.enabled:
                return None
            if proxy_fingerprint(self._security.decrypt(node.url_encrypted)) != target.fingerprint:
                return None
            row = session.get(ProxyNodeHealth, target.node_id)
            if row is None or row.fingerprint != target.fingerprint:
                return None
            _apply_result(row, result, now, policy)
            session.flush()
            return self._row_state(row)


def format_probe_error(result: ProxyProbeResult) -> str:
    if not result.error_code:
        return ""
    if result.error_code == "proxy_connection_timeout":
        return "代理连接超时"
    if result.error_code == "proxy_connection_failed":
        return "代理连接失败"
    messages = {
        "proxy_dns_failed": "代理服务器域名解析失败",
        "proxy_target_dns_failed": "目标域名解析失败",
        "proxy_authentication_failed": "代理认证失败，请检查账号密码及认证方式",
        "proxy_socks_handshake_failed": "SOCKS 代理握手或目标隧道建立失败",
        "proxy_tls_verification_failed": "代理线路的目标 TLS 证书校验失败",
        "proxy_tls_handshake_failed": "代理线路的目标 TLS 握手失败",
        "proxy_configuration_invalid": "代理配置无效",
        "probe_internal_error": "检测客户端异常，本次不判定节点健康状态",
    }
    if result.error_code in messages:
        return messages[result.error_code]
    if result.error_code.startswith("probe_target_http_"):
        status = result.error_code.rsplit("_", 1)[-1]
        return f"目标站点返回 HTTP {status}"
    return "代理检测失败"
