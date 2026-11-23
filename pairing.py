"""Pairing the PC with the phone, without any outside mailbox.

The PC's Cockpit signs in with Hermes' own native flow (RFC 8252: PKCE, a one-time code redeemed at
/auth/native/token), but the browser part runs on the phone, which is already signed in:

1. The PC makes the PKCE pair and a long random pairing secret, and shows a QR code with the challenge, its state
   and the secret. The verifier never leaves the PC.
2. The phone scans it and runs the native sign-in with the PC's challenge (its browser, its own loopback listener),
   catches the one-time code, and leaves it here: POST /pair-desktop, signed in as the phone, under the secret's
   SHA-256.
3. The PC collects it: GET /pair-desktop/claim with the secret as its bearer token. That one route accepts this token
   only (Hermes' token-auth seam, a provider of ours that knows nothing else), and only while a code waits under it.
4. The PC redeems the code with its verifier and has its own session; the phone's sign-in is never copied.

A code is single use, PKCE-bound (useless without the PC's verifier) and kept two minutes, in this process's memory.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import threading
import time
from typing import Any, Optional

CLAIM_PATH = "/api/plugins/pluk_notify/pair-desktop/claim"
PROVIDER_NAME = "pluk_pair"
TTL_S = 120.0
MAX_WAITING = 20
_HASH = re.compile(r"^[0-9a-f]{64}$")
_SECRET = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
_CODE = re.compile(r"^[\x21-\x7e]{1,512}$")

_lock = threading.Lock()
_waiting: dict[str, tuple[float, str, str]] = {}  # sha256(secret) hex -> (when, code, state)


class PairingError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def key_of(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def _prune(now: float) -> None:
    for key in [k for k, (at, _, _) in _waiting.items() if now - at > TTL_S]:
        del _waiting[key]


def deposit(key_hash: Any, code: Any, state: Any, now: Optional[float] = None) -> None:
    """The phone leaves the PC's one-time code under the secret's hash."""
    if not isinstance(key_hash, str) or not _HASH.match(key_hash):
        raise PairingError(400, "key_hash is the pairing secret's SHA-256, in hex")
    if not isinstance(code, str) or not _CODE.match(code) or not isinstance(state, str) or not _CODE.match(state):
        raise PairingError(400, "code and state are the sign-in's own")
    moment = time.time() if now is None else now
    with _lock:
        _prune(moment)
        if key_hash not in _waiting and len(_waiting) >= MAX_WAITING:
            raise PairingError(429, "Too many pairings waiting; try again shortly")
        _waiting[key_hash] = (moment, code, state)


def waiting(secret: Any, now: Optional[float] = None) -> bool:
    """Whether a code waits under this secret (the provider's check; compared in constant time)."""
    if not isinstance(secret, str) or not _SECRET.match(secret):
        return False
    want = key_of(secret)
    moment = time.time() if now is None else now
    with _lock:
        _prune(moment)
        return any(hmac.compare_digest(want, key) for key in _waiting)


def claim(secret: Any, now: Optional[float] = None) -> Optional[dict[str, str]]:
    """The PC takes its code, once."""
    if not isinstance(secret, str) or not _SECRET.match(secret):
        return None
    want = key_of(secret)
    moment = time.time() if now is None else now
    with _lock:
        _prune(moment)
        for key in list(_waiting):
            if hmac.compare_digest(want, key):
                _, code, state = _waiting.pop(key)
                return {"code": code, "state": state}
    return None


def bearer(request: Any) -> str:
    value = ""
    try:
        value = str(request.headers.get("authorization") or "")
    except Exception:  # noqa: BLE001
        return ""
    return value[7:].strip() if value.lower().startswith("bearer ") else ""


def make_provider() -> Any:
    """Hermes' token-auth provider that recognises a pairing secret, and nothing else. None on a Hermes without the
    seam (pairing then says it is unavailable)."""
    try:
        from hermes_cli.dashboard_auth.base import DashboardAuthProvider, TokenPrincipal
    except Exception:  # noqa: BLE001
        return None

    class PairingProvider(DashboardAuthProvider):
        name = PROVIDER_NAME
        display_name = "PLUK Island pairing"
        supports_token = True
        supports_session = False
        interactive_login = False

        def start_login(self, *, redirect_uri: str) -> Any:
            raise NotImplementedError("pairing has no login")

        def complete_login(self, *, code: str, state: str, code_verifier: str, redirect_uri: str) -> Any:
            raise NotImplementedError("pairing has no login")

        def verify_session(self, *, access_token: str) -> Any:
            return None

        def refresh_session(self, *, refresh_token: str) -> Any:
            from hermes_cli.dashboard_auth.base import RefreshExpiredError

            raise RefreshExpiredError("not a pairing token")

        def revoke_session(self, *, refresh_token: str) -> None:
            return None

        def verify_token(self, *, token: str) -> Any:
            return TokenPrincipal(principal="pluk-pair", provider=PROVIDER_NAME, scopes=("pluk-pair",)) if waiting(token) else None

    return PairingProvider()


_registered = False


def register() -> bool:
    """Registers the provider and opens the claim route to it (once per process). False when Hermes lacks the seam."""
    global _registered
    if _registered:
        return True
    provider = make_provider()
    if provider is None:
        return False
    try:
        from hermes_cli.dashboard_auth import registry, token_auth

        try:
            registry.register_provider(provider)
        except ValueError:
            pass  # registered by an earlier load of this plugin in this process
        token_auth.register_token_route(CLAIM_PATH)
    except Exception:  # noqa: BLE001
        return False
    _registered = True
    return True
