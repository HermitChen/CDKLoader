from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclass(frozen=True)
class SecurityManager:
    credential_secret: str
    cdk_pepper: str

    @property
    def _encryption_key(self) -> bytes:
        return hashlib.sha256(self.credential_secret.encode("utf-8")).digest()

    def encrypt(self, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        nonce = secrets.token_bytes(12)
        encrypted = AESGCM(self._encryption_key).encrypt(nonce, value.encode("utf-8"), None)
        return base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")

    def decrypt(self, value: str | None) -> str:
        if not value:
            return ""
        payload = base64.urlsafe_b64decode(value.encode("ascii"))
        return AESGCM(self._encryption_key).decrypt(payload[:12], payload[12:], None).decode("utf-8")

    def cdk_digest(self, code: str) -> str:
        normalized = code.strip().upper()
        return hmac.new(
            self.cdk_pepper.encode("utf-8"), normalized.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def opaque_digest(self, value: str) -> str:
        return hmac.new(
            self.cdk_pepper.encode("utf-8"), value.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def redemption_token(self, redemption_id: str, idempotency_key: str) -> str:
        raw = f"{redemption_id}:{idempotency_key}"
        digest = hmac.new(
            self.cdk_pepper.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256
        ).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def redelivery_token(self, redelivery_id: str, idempotency_key: str) -> str:
        raw = f"redelivery:{redelivery_id}:{idempotency_key}"
        digest = hmac.new(
            self.cdk_pepper.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256
        ).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    @staticmethod
    def constant_time_equal(left: str, right: str) -> bool:
        return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def mask_email(email: str | None) -> str:
    if not email or "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    visible = local[:2] if len(local) > 2 else local[:1]
    return f"{visible}***@{domain}"


def token_hint(value: str | None) -> str:
    if not value:
        return ""
    return f"***{value[-4:]}" if len(value) > 4 else "***"
