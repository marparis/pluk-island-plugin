"""Sending through Firebase Cloud Messaging (HTTP v1), with the user's own Firebase project.

Only outgoing HTTPS, to two fixed Google hosts; nothing listens. The service
account key signs a short-lived request for an access token (RS256, with
`cryptography`, a core dependency of Hermes); the token is kept in memory.
Nothing here logs or returns the key, an access token or a device token.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

TOKEN_URL = "https://oauth2.googleapis.com/token"
SEND_URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"
SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
TIMEOUT_S = 15.0

OK = "ok"
# Firebase no longer knows the device token: the app was removed or its data cleared.
GONE = "gone"


class FcmError(Exception):
    def __init__(self, message: str, *, retry: bool = False):
        super().__init__(message)
        self.retry = retry


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # a credential never follows a redirect
        return None


_opener = urllib.request.build_opener(_NoRedirect)
Transport = Callable[[str, dict[str, str], bytes], tuple[int, bytes]]


def _http(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with _opener.open(request, timeout=TIMEOUT_S) as response:
            return response.status, response.read(65536)
    except urllib.error.HTTPError as error:
        return error.code, error.read(65536)
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise FcmError(f"network: {type(error).__name__}", retry=True) from None


def _b64u(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def signed_assertion(account: dict[str, Any], now: float) -> str:
    """The JWT that asks Google for an access token in the service account's name."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    header = {"alg": "RS256", "typ": "JWT"}
    if isinstance(account.get("private_key_id"), str) and account["private_key_id"]:
        header["kid"] = account["private_key_id"]
    claims = {"iss": account["client_email"], "scope": SCOPE, "aud": TOKEN_URL, "iat": int(now), "exp": int(now) + 3600}
    signing_input = (_b64u(json.dumps(header, separators=(",", ":")).encode()) + "."
                     + _b64u(json.dumps(claims, separators=(",", ":")).encode()))
    try:
        key = serialization.load_pem_private_key(account["private_key"].encode("utf-8"), password=None)
        signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    except Exception:  # noqa: BLE001 - never echo the key or the library's message about it
        raise FcmError("the service account key could not be used") from None
    return signing_input + "." + _b64u(signature)


class Sender:
    """One per process. [transport] is replaced in the tests."""

    def __init__(self, transport: Transport = _http, clock: Callable[[], float] = time.time):
        self._transport = transport
        self._clock = clock
        self._lock = threading.Lock()
        self._token: tuple[str, str, float] | None = None  # (client_email, access token, expiry)

    def _access_token(self, account: dict[str, Any]) -> str:
        with self._lock:
            now = self._clock()
            if self._token and self._token[0] == account["client_email"] and self._token[2] - now > 120:
                return self._token[1]
            body = urllib.parse.urlencode({
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": signed_assertion(account, now),
            }).encode("ascii")
            status, raw = self._transport(TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, body)
            if status != 200:
                raise FcmError(f"Google refused the service account (HTTP {status})", retry=status >= 500)
            try:
                reply = json.loads(raw.decode("utf-8"))
                token, lifetime = reply["access_token"], float(reply.get("expires_in", 3600))
            except Exception:  # noqa: BLE001
                raise FcmError("Google's token reply could not be read", retry=True) from None
            if not isinstance(token, str) or not token:
                raise FcmError("Google's token reply could not be read", retry=True)
            self._token = (account["client_email"], token, now + lifetime)
            return token

    def send(self, account: dict[str, Any], device_token: str, data: dict[str, str], *, high: bool = True,
             ttl_s: int = 3600) -> str:
        """Sends one data message. Returns OK or GONE; raises FcmError otherwise."""
        message = {"message": {
            "token": device_token,
            "data": data,
            "android": {"priority": "HIGH" if high else "NORMAL", "ttl": f"{int(ttl_s)}s"},
        }}
        url = SEND_URL.format(project=urllib.parse.quote(account["project_id"], safe=""))
        body = json.dumps(message, separators=(",", ":")).encode("utf-8")
        for attempt in (0, 1):
            headers = {"Authorization": "Bearer " + self._access_token(account), "Content-Type": "application/json; charset=utf-8"}
            status, raw = self._transport(url, headers, body)
            if status == 401 and attempt == 0:
                with self._lock:
                    self._token = None  # an access token that ended early: ask once more
                continue
            break
        if 200 <= status < 300:
            return OK
        code = _error_code(raw)
        if status == 404 or code == "UNREGISTERED":
            return GONE
        raise FcmError(f"Firebase refused the message (HTTP {status} {code})".strip(), retry=status == 429 or status >= 500)


def _error_code(raw: bytes) -> str:
    """Firebase's own word for a refusal (UNREGISTERED, INVALID_ARGUMENT, SENDER_ID_MISMATCH...), never its message."""
    try:
        error = json.loads(raw.decode("utf-8")).get("error", {})
        for detail in error.get("details", []) or []:
            if isinstance(detail, dict) and isinstance(detail.get("errorCode"), str):
                return detail["errorCode"][:40]
        status = error.get("status")
        return status[:40] if isinstance(status, str) else ""
    except Exception:  # noqa: BLE001
        return ""
