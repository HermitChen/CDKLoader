from __future__ import annotations


def transport_proxy_url(proxy_url: str | None) -> str:
    """Match FlowRealm's SOCKS5 dialer by resolving targets at the proxy."""
    value = str(proxy_url or "").strip()
    if value.lower().startswith("socks5://"):
        # libcurl's socks5:// resolves locally; Go's SOCKS5 dialer sends the
        # hostname to the proxy. Preserve the configured credentials verbatim.
        return "socks5h://" + value[len("socks5://"):]
    return value


def proxy_request_mapping(proxy_url: str | None) -> dict[str, str] | None:
    value = transport_proxy_url(proxy_url)
    return {"http": value, "https": value} if value else None
