"""Single-use authenticated, encrypted captures over the local HTTP adapter.

The dedicated pairing key never travels over HTTP. Encrypting the payload also
keeps a replacement/relaying local listener from reading private conversations.
"""
from dataclasses import dataclass
import hashlib
import hmac
import re
import secrets
import threading
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43,128}\Z")
HEX_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
IV_PATTERN = re.compile(r"[0-9a-f]{24}\Z")
AUTH_HEADERS = ("X-Workflowy-Client", "X-Workflowy-Challenge", "X-Workflowy-IV",
                "X-Workflowy-Hash", "X-Workflowy-Proof")


class CaptureAuthError(ValueError):
    def __init__(self):
        super().__init__("Capture authorization failed")


def transcript(domain: str, *fields: str) -> bytes:
    return "\0".join((domain, *fields)).encode("ascii")


@dataclass(frozen=True)
class CaptureReceipt:
    client: str
    server: str
    iv: str
    digest: str


class CaptureAuthority:
    def __init__(self, token: str, endpoint: str):
        if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
            raise CaptureAuthError()
        self._key = token.encode("ascii")
        self.endpoint = endpoint
        self._pending = {}
        self._lock = threading.Lock()

    def _mac(self, domain: str, *fields: str) -> bytes:
        return hmac.digest(self._key, transcript(domain, self.endpoint, *fields), "sha256")

    def issue(self, client: str) -> dict:
        if not isinstance(client, str) or not HEX_PATTERN.fullmatch(client):
            raise CaptureAuthError()
        with self._lock:
            now = time.monotonic()
            self._pending = {pair: expiry for pair, expiry in self._pending.items() if expiry > now}
            if len(self._pending) >= 256:
                raise CaptureAuthError()
            server = secrets.token_hex(32)
            self._pending[(client, server)] = now + 60
        return {"server_nonce": server, "endpoint": self.endpoint,
                "proof": self._mac("workflowy-server-v1", client, server).hex()}

    def authorize(self, headers) -> CaptureReceipt:
        values = []
        for name in AUTH_HEADERS:
            present = headers.get_all(name, [])
            if len(present) != 1:
                raise CaptureAuthError()
            values.append(present[0])
        client, server, iv, digest, proof = values
        if not all(HEX_PATTERN.fullmatch(value) for value in (client, server, digest, proof)) or not IV_PATTERN.fullmatch(iv):
            raise CaptureAuthError()
        expected = self._mac("workflowy-capture-v1", client, server, iv, digest).hex()
        with self._lock:
            expiry = self._pending.get((client, server), 0)
            if expiry <= time.monotonic() or not hmac.compare_digest(expected, proof):
                raise CaptureAuthError()
            del self._pending[(client, server)]
        return CaptureReceipt(client, server, iv, digest)

    def decrypt(self, receipt: CaptureReceipt, body: bytes) -> bytes:
        if not hmac.compare_digest(hashlib.sha256(body).hexdigest(), receipt.digest):
            raise CaptureAuthError()
        key = self._mac("workflowy-encryption-v1", receipt.client, receipt.server)
        aad = transcript("workflowy-body-v1", self.endpoint, receipt.client, receipt.server)
        try:
            return AESGCM(key).decrypt(bytes.fromhex(receipt.iv), body, aad)
        except (InvalidTag, ValueError):
            raise CaptureAuthError() from None
