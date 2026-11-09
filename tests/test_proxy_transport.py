from __future__ import annotations

import queue
import socketserver
import ssl
import threading
from base64 import b64encode
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from curl_cffi import requests as cffi_requests
from curl_cffi.curl import CurlError

from app.services.openai_validation import TokenRefreshManager
from app.services.proxy_health import probe_proxy
from app.services.proxy_transport import transport_proxy_url


@pytest.fixture
def socks_proxy():
    observations = queue.Queue()

    class Handler(socketserver.BaseRequestHandler):
        def read_exact(self, count):
            data = b""
            while len(data) < count:
                chunk = self.request.recv(count - len(data))
                if not chunk:
                    raise ConnectionError("SOCKS client closed the test connection")
                data += chunk
            return data

        def handle(self):
            self.request.settimeout(2)
            try:
                version, method_count = self.read_exact(2)
                methods = self.read_exact(method_count)
                assert version == 5 and 2 in methods
                self.request.sendall(b"\x05\x02")
                auth_version, user_len = self.read_exact(2)
                username = self.read_exact(user_len)
                password = self.read_exact(self.read_exact(1)[0])
                assert auth_version == 1
                self.request.sendall(b"\x01\x00")
                version, command, _, address_type = self.read_exact(4)
                assert version == 5 and command == 1 and address_type == 3
                host = self.read_exact(self.read_exact(1)[0]).decode("ascii")
                port = int.from_bytes(self.read_exact(2), "big")
                observations.put((username, password, host, port))
                # Reject the tunnel after recording the real libcurl SOCKS
                # handshake. No DNS, target connection, or TLS service needed.
                self.request.sendall(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
            except Exception as exc:
                observations.put(exc)

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

    with Server(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()
        try:
            yield f"socks5://user:secret%40encoded@127.0.0.1:{server.server_address[1]}", observations
        finally:
            server.shutdown()
            worker.join(timeout=3)


@pytest.mark.parametrize("caller", ["health", "validation"])
def test_real_socks5_handshake_uses_remote_dns_and_preserves_auth(monkeypatch, socks_proxy, caller):
    proxy_url, observations = socks_proxy
    target = "https://proxy-health-target.invalid/api/auth/csrf"
    # Even NO_PROXY=* must not bypass an explicitly configured proxy.
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    if caller == "health":
        monkeypatch.setattr("app.services.proxy_health.PROBE_URL", target)
        result = probe_proxy(proxy_url, timeout_ms=2000)
        assert result.error_code == "proxy_socks_handshake_failed"
    else:
        manager = TokenRefreshManager(proxy_url=proxy_url)
        with manager._create_session() as session:
            with pytest.raises(CurlError):
                session.get(target, proxies=manager._proxy_mapping(), timeout=2)

    assert observations.get(timeout=3) == (b"user", b"secret@encoded", "proxy-health-target.invalid", 443)


@pytest.fixture
def http_proxy(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "proxy-health-target.invalid")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("proxy-health-target.invalid")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "target.pem", tmp_path / "target-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert_path, key_path)
    tls.set_alpn_protocols(["http/1.1"])
    observations = queue.Queue()

    class Handler(socketserver.BaseRequestHandler):
        def read_headers(self, connection):
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = connection.recv(4096)
                if not chunk or len(data) + len(chunk) > 65536:
                    raise ConnectionError("invalid HTTP proxy test request")
                data += chunk
            lines = data.decode("iso-8859-1").split("\r\n")
            return lines[0], {
                key.strip().lower(): value.strip()
                for line in lines[1:] if ":" in line
                for key, value in [line.split(":", 1)]
            }

        def handle(self):
            self.request.settimeout(3)
            try:
                connect_line, connect_headers = self.read_headers(self.request)
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                with tls.wrap_socket(self.request, server_side=True) as target:
                    request_line, request_headers = self.read_headers(target)
                    target.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        b"Content-Length: 2\r\nConnection: close\r\n\r\n{}"
                    )
                observations.put((connect_line, connect_headers, request_line, request_headers))
            except Exception as exc:
                observations.put(exc)

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

    with Server(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()
        try:
            yield server.server_address[1], cert_path, observations
        finally:
            server.shutdown()
            worker.join(timeout=3)


@pytest.mark.parametrize("caller", ["health", "validation"])
@pytest.mark.parametrize("authenticated", [False, True])
def test_real_http_proxy_connect_tls_and_auth_succeed(monkeypatch, http_proxy, caller, authenticated):
    port, cert_path, observations = http_proxy
    credentials = "user:secret%40encoded@" if authenticated else ""
    proxy_url = f"http://{credentials}127.0.0.1:{port}"
    target = "https://proxy-health-target.invalid/api/auth/csrf"
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    original_session = cffi_requests.Session

    def trusted_session(**options):
        # Trust only the test target certificate; retain TLS verification.
        return original_session(verify=str(cert_path), **options)

    monkeypatch.setattr(cffi_requests, "Session", trusted_session)
    if caller == "health":
        monkeypatch.setattr("app.services.proxy_health.PROBE_URL", target)
        result = probe_proxy(proxy_url, timeout_ms=3000)
        assert result.ok
        assert result.status_code == 200
    else:
        manager = TokenRefreshManager(proxy_url=proxy_url)
        with manager._create_session() as session:
            response = manager._request(
                session, "get", target, stage="proxy_diagnostic", headers={"accept": "application/json"},
            )
            assert response.status_code == 200
            assert response.json() == {}
        assert manager.proxy_url == proxy_url

    connect_line, connect_headers, request_line, request_headers = observations.get(timeout=3)
    assert connect_line == "CONNECT proxy-health-target.invalid:443 HTTP/1.1"
    expected_auth = "Basic " + b64encode(b"user:secret@encoded").decode() if authenticated else None
    assert connect_headers.get("proxy-authorization") == expected_auth
    assert request_line == "GET /api/auth/csrf HTTP/1.1"
    assert "proxy-authorization" not in request_headers
    assert "authorization" not in request_headers
    assert "cookie" not in request_headers


@pytest.mark.parametrize(("configured", "expected"), [
    (" socks5://user:p%40ss@proxy.example:1080 ", "socks5h://user:p%40ss@proxy.example:1080"),
    ("socks5h://proxy.example:1080", "socks5h://proxy.example:1080"),
    ("http://user:p%40ss@proxy.example:8080", "http://user:p%40ss@proxy.example:8080"),
    (None, ""),
])
def test_transport_proxy_url_preserves_configured_address_and_credentials(configured, expected):
    assert transport_proxy_url(configured) == expected
