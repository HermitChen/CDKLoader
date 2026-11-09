from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import ProxyGroupSetting, ProxyNodeSetting
from ..models import ProxyGroup, SystemSetting
from ..security import SecurityManager

SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}


def normalize_proxy_url(value: str) -> str:
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in SUPPORTED_PROXY_SCHEMES or not parsed.hostname:
        raise ValueError("代理地址必须包含有效的 HTTP/HTTPS/SOCKS 地址")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("代理地址不能包含路径、查询参数或片段")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("代理地址端口无效") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("代理地址端口必须在 1 到 65535 之间")
    return urlunparse((parsed.scheme.lower(), parsed.netloc, "", "", "", ""))


def mask_proxy_url(value: str) -> str:
    normalized = normalize_proxy_url(value)
    parsed = urlparse(normalized)
    if parsed.username is None and parsed.password is None:
        return normalized
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunparse((parsed.scheme, "***:***@" + host, "", "", "", ""))


def load_proxy_groups(session: Session, security: SecurityManager) -> tuple[ProxyGroupSetting, ...]:
    rows = session.scalars(
        select(ProxyGroup).options(selectinload(ProxyGroup.nodes)).order_by(ProxyGroup.name, ProxyGroup.id)
    ).all()
    return tuple(
        ProxyGroupSetting(
            id=group.id,
            name=group.name,
            strategy=group.strategy,
            rotation_interval_minutes=group.rotation_interval_minutes,
            enabled=group.enabled,
            nodes=tuple(
                ProxyNodeSetting(
                    id=node.id,
                    name=node.name,
                    url=security.decrypt(node.url_encrypted),
                    enabled=node.enabled,
                )
                for node in group.nodes
            ),
        )
        for group in rows
    )


def group_is_referenced(session: Session, group_id: str) -> bool:
    setting = session.get(SystemSetting, "validation_proxy_group_id")
    mode = session.get(SystemSetting, "validation_egress_mode")
    return bool(setting and mode and mode.value.strip().lower() == "group" and setting.value.strip() == group_id)


def serialize_proxy_group(
    group: ProxyGroup,
    security: SecurityManager,
    *,
    can_delete: bool = True,
    health_lookup: Callable[[str, str], dict[str, Any]] | None = None,
) -> dict:
    def serialize_node(node: Any) -> dict[str, Any]:
        url = security.decrypt(node.url_encrypted)
        health = health_lookup(node.id, url) if health_lookup else {
            "state": "unknown", "checked_at": None, "next_check_at": None,
            "latency_ms": None, "status_code": None, "error": "",
            "eligible": True, "blocked": False, "recovery_successes": 0,
        }
        return {
            "id": node.id,
            "name": node.name,
            "url": mask_proxy_url(url),
            "enabled": node.enabled,
            "has_credentials": bool(urlparse(url).username),
            "health": health,
        }

    return {
        "id": group.id,
        "name": group.name,
        "strategy": group.strategy,
        "rotation_interval_minutes": group.rotation_interval_minutes,
        "enabled": group.enabled,
        "can_delete": can_delete,
        "nodes": [serialize_node(node) for node in group.nodes],
    }


def proxy_group_options(groups: tuple[ProxyGroupSetting, ...]) -> list[dict[str, str]]:
    return [
        {
            "label": f"{'停用 · ' if not group.enabled else ''}{group.name} · {sum(1 for node in group.nodes if node.enabled)} 个节点",
            "value": group.id,
        }
        for group in groups
    ]
