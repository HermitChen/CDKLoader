from dataclasses import replace

from app.config import ProxyGroupSetting, ProxyNodeSetting
from app.models import Account, ProxyGroup, ProxyNode, SystemSetting
from app.security import SecurityManager
from app.services.validator import TokenValidator


def _settings_values(payload: dict) -> dict:
    return {
        item["key"]: item
        for group in payload["groups"]
        for item in group["items"]
    }


def test_proxy_group_crud_and_system_setting_reference(client, admin_headers):
    created = client.post(
        "/api/v1/admin/proxy-groups",
        headers=admin_headers,
        json={
            "name": "海外验活",
            "strategy": "round_robin",
            "nodes": [
                {"name": "主节点", "url": "http://user:secret@proxy.example:8080"},
                {"name": "备用节点", "url": "socks5://127.0.0.1:1080"},
            ],
        },
    )

    assert created.status_code == 200, created.text
    group = created.json()["group"]
    group_id = group["id"]
    assert group["nodes"][0]["url"] == "http://***:***@proxy.example:8080"
    assert "secret" not in created.text

    with client.app.state.session_factory() as session:
        node = session.query(ProxyNode).filter_by(group_id=group_id).first()
        assert node is not None
        assert "secret" not in node.url_encrypted

    settings = client.get("/api/v1/admin/settings", headers=admin_headers)
    assert settings.status_code == 200
    values = _settings_values(settings.json())
    group_setting = values["validation_proxy_group_id"]
    assert group_setting["type"] == "proxy_group"
    assert any(option["value"] == group_id for option in group_setting["options"])

    updated = client.put(
        "/api/v1/admin/settings",
        headers=admin_headers,
        json={"values": {"validation_egress_mode": "group", "validation_proxy_group_id": group_id}},
    )
    assert updated.status_code == 200, updated.text
    assert client.app.state.settings.validation_egress_mode == "group"
    assert client.app.state.settings.validation_proxy_group_id == group_id

    blocked_delete = client.delete(f"/api/v1/admin/proxy-groups/{group_id}", headers=admin_headers)
    assert blocked_delete.status_code == 409

    blocked_disable = client.put(
        f"/api/v1/admin/proxy-groups/{group_id}",
        headers=admin_headers,
        json={
            "name": "海外验活",
            "strategy": "round_robin",
            "enabled": False,
            "nodes": [{"id": group["nodes"][0]["id"], "name": "主节点", "url": group["nodes"][0]["url"], "enabled": True}],
        },
    )
    assert blocked_disable.status_code == 409

    cleared = client.put(
        "/api/v1/admin/settings",
        headers=admin_headers,
        json={"values": {"validation_egress_mode": "direct"}},
    )
    assert cleared.status_code == 200
    assert _settings_values(cleared.json())["validation_proxy_group_id"]["value"] == ""

    deleted = client.delete(f"/api/v1/admin/proxy-groups/{group_id}", headers=admin_headers)
    assert deleted.status_code == 200, deleted.text
    with client.app.state.session_factory() as session:
        assert session.get(ProxyGroup, group_id) is None
        assert session.get(SystemSetting, "validation_proxy_group_id").value == ""


def test_proxy_group_strategy_selects_nodes(settings):
    group = ProxyGroupSetting(
        id="group-1",
        name="测试组",
        strategy="round_robin",
        rotation_interval_minutes=0,
        enabled=True,
        nodes=(
            ProxyNodeSetting(id="node-1", name="一", url="http://one.example:8080"),
            ProxyNodeSetting(id="node-2", name="二", url="http://two.example:8080"),
        ),
    )
    validator = TokenValidator(
        replace(settings, validation_egress_mode="group", validation_proxy_group_id="group-1", validation_proxy_groups=(group,)),
        SecurityManager("proxy-test-secret", "proxy-test-pepper"),
    )
    account = Account(email="strategy@example.com")

    assert validator._validation_proxy_url(account) == "http://one.example:8080"
    assert validator._validation_proxy_url(account) == "http://two.example:8080"
    assert validator._validation_proxy_url(account) == "http://one.example:8080"
