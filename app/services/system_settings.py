from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import (
    RUNTIME_SETTING_SPEC_BY_KEY,
    RUNTIME_SETTING_SPECS,
    RuntimeSettingSpec,
    Settings,
    parse_size_bytes,
)
from ..models import SystemSetting
from ..security import SecurityManager
from .proxy_groups import load_proxy_groups, proxy_group_options


def _spec(key: str) -> RuntimeSettingSpec:
    try:
        return RUNTIME_SETTING_SPEC_BY_KEY[key]
    except KeyError as exc:
        raise ValueError(f"不支持的系统参数：{key}") from exc


def _parse_number(spec: RuntimeSettingSpec, value: Any) -> int | float:
    if isinstance(value, bool):
        raise ValueError(f"参数 {spec.label} 必须是数字")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"参数 {spec.label} 必须是数字") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"参数 {spec.label} 必须是有限数字")
    if spec.value_type == "integer":
        if not parsed.is_integer():
            raise ValueError(f"参数 {spec.label} 必须是整数")
        parsed_value: int | float = int(parsed)
    else:
        parsed_value = parsed
    if spec.min_value is not None and parsed_value < spec.min_value:
        raise ValueError(f"参数 {spec.label} 不能小于 {spec.min_value}")
    if spec.max_value is not None and parsed_value > spec.max_value:
        raise ValueError(f"参数 {spec.label} 不能大于 {spec.max_value}")
    return parsed_value


def parse_setting_value(key: str, value: Any) -> Any:
    spec = _spec(key)
    if spec.value_type in {"integer", "number"}:
        return _parse_number(spec, value)
    if spec.value_type == "size":
        try:
            return parse_size_bytes(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"参数 {spec.label} 不是有效的文件大小") from exc
    if spec.value_type == "select":
        parsed = str(value or "").strip().lower()
        if parsed not in {option_value for _, option_value in spec.options}:
            raise ValueError(f"参数 {spec.label} 的取值无效")
        return parsed

    parsed = str(value or "").strip()
    if key == "public_base_url":
        parsed = parsed.rstrip("/")
        if not parsed:
            raise ValueError("公开服务地址不能为空")
    if key == "oauth_client_id" and not parsed:
        raise ValueError(f"参数 {spec.label} 不能为空")
    return parsed


def _format_size(value: int) -> str:
    units = ((1024**4, "T"), (1024**3, "G"), (1024**2, "M"), (1024, "K"))
    for factor, suffix in units:
        if value >= factor and value % factor == 0:
            return f"{value // factor}{suffix}"
    return f"{value}B"


def serialize_setting_value(key: str, value: Any) -> Any:
    spec = _spec(key)
    if spec.value_type == "size":
        return _format_size(int(value))
    if spec.value_type == "integer":
        return int(value)
    if spec.value_type == "number":
        return float(value)
    return value


def _storage_value(key: str, value: Any) -> str:
    parsed = parse_setting_value(key, value)
    return str(parsed)


def _setting_rows(session: Session) -> dict[str, SystemSetting]:
    return {row.key: row for row in session.scalars(select(SystemSetting)).all()}


def ensure_system_settings(session: Session, settings: Settings) -> None:
    rows = _setting_rows(session)
    for spec in RUNTIME_SETTING_SPECS:
        if spec.key in rows:
            continue
        value = getattr(settings, spec.key, spec.default)
        session.add(SystemSetting(key=spec.key, value=_storage_value(spec.key, value)))
    session.flush()


def load_system_settings(
    session: Session,
    base_settings: Settings,
    security: SecurityManager | None = None,
) -> Settings:
    rows = _setting_rows(session)
    overrides: dict[str, Any] = {}
    for spec in RUNTIME_SETTING_SPECS:
        row = rows.get(spec.key)
        if not row:
            continue
        try:
            stored_value = row.value
            if spec.key == "validation_egress_mode" and stored_value.strip().lower() == "pool":
                stored_value = "direct"
            overrides[spec.key] = parse_setting_value(spec.key, stored_value)
        except ValueError:
            # A hand-edited database value must not prevent the service from
            # starting. The API will reject the value if it is submitted again.
            continue
    if security is not None:
        overrides["validation_proxy_groups"] = load_proxy_groups(session, security)
    return replace(base_settings, **overrides)


def update_system_settings(
    session: Session,
    base_settings: Settings,
    values: dict[str, Any],
    security: SecurityManager | None = None,
) -> Settings:
    rows = _setting_rows(session)
    egress_mode: str | None = None
    for key, value in values.items():
        normalized = _storage_value(key, value)
        if key == "validation_egress_mode":
            egress_mode = normalized
        if key == "validation_proxy_group_id" and normalized:
            groups = load_proxy_groups(session, security) if security is not None else base_settings.validation_proxy_groups
            group = next((item for item in groups if item.id == normalized), None)
            if group is None:
                raise ValueError("参数 验活代理组不存在")
            if not group.enabled or not any(node.enabled and node.url for node in group.nodes):
                raise ValueError("参数 验活代理组当前没有可用节点")
        row = rows.get(key)
        if row:
            row.value = normalized
        else:
            row = SystemSetting(key=key, value=normalized)
            session.add(row)
    if egress_mode is not None and egress_mode != "group":
        group_row = rows.get("validation_proxy_group_id")
        if group_row:
            group_row.value = ""
    session.flush()
    return load_system_settings(session, base_settings, security)


def settings_payload(settings: Settings) -> dict[str, Any]:
    grouped: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for spec in RUNTIME_SETTING_SPECS:
        group = grouped.setdefault(
            spec.group,
            {"key": spec.group, "label": spec.group_label, "items": []},
        )
        options = [
            {"label": label, "value": value}
            for label, value in spec.options
        ]
        if spec.value_type == "proxy_group":
            options = [{"label": "不选择代理组", "value": ""}] + proxy_group_options(settings.validation_proxy_groups)
        group["items"].append(
            {
                "key": spec.key,
                "label": spec.label,
                "description": spec.description,
                "type": spec.value_type,
                "value": serialize_setting_value(spec.key, getattr(settings, spec.key)),
                "default": spec.default,
                "min": spec.min_value,
                "max": spec.max_value,
                "unit": spec.unit,
                "options": options,
            }
        )
    return {"groups": list(grouped.values())}
