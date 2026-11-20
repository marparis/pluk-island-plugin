"""End-to-end sealing of a notification's content.

Method taken from hermes-conduit-notifier (MIT, (c) 2026 Hermes Conduit
contributors; its sealing.py and e2e.py): a per-device secret, a key derived
with HKDF-SHA256, the body sealed with ChaCha20-Poly1305, and associated data
that binds the envelope to its device. Reduced to what PLUK Island needs: the
secret is made here at pairing and handed to the app once, over the
Dashboard's own authenticated HTTPS connection, so it never passes through
Firebase. Google carries the envelope and cannot read it.

Pure functions; the only dependency is `cryptography`, which Hermes pins as a
core dependency. `available()` says whether it can be used.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
from typing import Any

VERSION = "1"
SALT = b"pluk-notify-v1"
INFO_PUSH = b"pluk-notify-v1 push server-to-device"
AAD_TAG = b"pluk-notify/1"
NONCE_BYTES = 12
SECRET_BYTES = 32
KEY_BYTES = 32

_B64URL = re.compile(r"^[A-Za-z0-9_-]*\Z")
DEVICE_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}\Z")


class SealError(Exception):
    """An envelope could not be built or opened."""


def available() -> bool:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305  # noqa: F401
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def b64u(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def unb64u(value: Any) -> bytes:
    if not isinstance(value, str) or not _B64URL.match(value) or len(value) % 4 == 1:
        raise SealError("malformed base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def new_secret() -> str:
    return b64u(secrets.token_bytes(SECRET_BYTES))


def new_device_id() -> str:
    return b64u(secrets.token_bytes(16))


def push_key(secret: str, device_id: str) -> bytes:
    """The key notifications to one device are sealed with."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    raw = unb64u(secret)
    if len(raw) != SECRET_BYTES or not DEVICE_ID.match(device_id or ""):
        raise SealError("malformed pairing")
    info = INFO_PUSH + b"\n" + device_id.encode("ascii")
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=SALT, info=info).derive(raw)


def _aad(device_id: str) -> bytes:
    return AAD_TAG + b"\n" + device_id.encode("ascii")


def seal(secret: str, device_id: str, event: dict[str, Any], nonce: bytes | None = None) -> dict[str, str]:
    """The FCM data fields for [event]: only strings, as FCM wants them."""
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    nonce = nonce if nonce is not None else secrets.token_bytes(NONCE_BYTES)
    plaintext = json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ciphertext = ChaCha20Poly1305(push_key(secret, device_id)).encrypt(nonce, plaintext, _aad(device_id))
    return {"v": VERSION, "d": device_id, "n": b64u(nonce), "c": b64u(ciphertext)}


def open_sealed(secret: str, data: dict[str, Any]) -> dict[str, Any]:
    """What the app does on its side (core/NotifySeal.kt); here for the tests and `hermes pluk-notify`."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    if data.get("v") != VERSION:
        raise SealError("unknown version")
    device_id = data.get("d") or ""
    nonce = unb64u(data.get("n"))
    if len(nonce) != NONCE_BYTES:
        raise SealError("nonce must be 12 bytes")
    try:
        plaintext = ChaCha20Poly1305(push_key(secret, device_id)).decrypt(nonce, unb64u(data.get("c")), _aad(device_id))
    except InvalidTag:
        raise SealError("the envelope did not open with this device's key") from None
    value = json.loads(plaintext.decode("utf-8"))
    if not isinstance(value, dict):
        raise SealError("not an event")
    return value
