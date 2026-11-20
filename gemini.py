"""A single-use Gemini Live token for the app's voice companion.

The phone talks to Gemini Live directly, but the Google API key never leaves
this host: the app asks for a token here (through the Dashboard's own
sign-in), Google mints one that opens one Live session within a minute and
lasts half an hour, and the phone connects with that. Taken from
hermes-conduit-notifier's dashboard/api/gemini_qwen_live.py (MIT, (c) 2026
Hermes Conduit contributors), reduced to the token.

The key is read where Hermes keeps its own: GEMINI_API_KEY, else
GOOGLE_API_KEY, in the profile's environment (hermes_cli.config). It is never
returned, logged or written.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

DEFAULT_MODEL = "gemini-3.8-live"
# Ephemeral tokens are served on v1alpha only; the override covers a later rollout.
DEFAULT_API_VERSION = "v1alpha"
API_VERSION_ENV_VAR = "PLUK_GEMINI_LIVE_API_VERSION"
MODEL_ENV_VAR = "PLUK_GEMINI_LIVE_MODEL"
# Same lookup order as Hermes' Gemini text-to-speech provider.
API_KEY_ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
TOKEN_LIFETIME = timedelta(minutes=30)
NEW_SESSION_WINDOW = timedelta(minutes=1)
REQUEST_TIMEOUT_S = 15.0
# One token per conversation: this stops a client gone wrong from burning the host's Gemini quota.
MINT_LIMIT = 20
MINT_WINDOW_S = 60.0


class TokenError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _env_value(key: str) -> Optional[str]:
    try:
        from hermes_cli.config import get_env_value
    except ImportError:
        return os.environ.get(key)
    return get_env_value(key)


def resolve_api_key(get_env: Callable[[str], Optional[str]]) -> Optional[str]:
    for name in API_KEY_ENV_VARS:
        value = str(get_env(name) or "").strip()
        if value:
            return value
    return None


def resolve_model(get_env: Callable[[str], Optional[str]]) -> str:
    value = str(get_env(MODEL_ENV_VAR) or "").strip()
    return value.removeprefix("models/") or DEFAULT_MODEL


def resolve_api_version(get_env: Callable[[str], Optional[str]]) -> str:
    value = str(get_env(API_VERSION_ENV_VAR) or "").strip()
    return value if value in ("v1alpha", "v1beta", "v1") else DEFAULT_API_VERSION


def token_url(api_version: str) -> str:
    return f"https://generativelanguage.googleapis.com/{api_version}/auth_tokens"


def websocket_url(api_version: str) -> str:
    return ("wss://generativelanguage.googleapis.com/ws/"
            f"google.ai.generativelanguage.{api_version}.GenerativeService.BidiGenerateContentConstrained")


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def token_request_body(model: str, now: datetime) -> Dict[str, Any]:
    return {
        "uses": 1,
        "expireTime": _timestamp(now + TOKEN_LIFETIME),
        "newSessionExpireTime": _timestamp(now + NEW_SESSION_WINDOW),
        # The token service takes the Live setup under this name. fieldMask locks only the model: without it Google
        # locks the whole setup, and the app could not send its own tools and instructions.
        "bidiGenerateContentSetup": {"model": f"models/{model}"},
        "fieldMask": "model",
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # a redirect would resend the key to wherever it points
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _post_json(url: str, api_key: str, body: Dict[str, Any]) -> Dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "x-goog-api-key": api_key}, method="POST")
    try:
        with _opener.open(request, timeout=REQUEST_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Google's error body names the problem (bad key, quota) without echoing the key.
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001
            pass
        raise TokenError(502, f"Google rejected the token request ({exc.code}){': ' + detail[:200] if detail else ''}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise TokenError(502, "Could not reach Google") from None
    except ValueError:
        raise TokenError(502, "Google returned an unreadable token response") from None


_mints: list[float] = []
_mints_lock = threading.Lock()


def _limited(clock: Callable[[], float]) -> None:
    now = clock()
    with _mints_lock:
        _mints[:] = [t for t in _mints if now - t < MINT_WINDOW_S]
        if len(_mints) >= MINT_LIMIT:
            raise TokenError(429, "Too many voice conversations were started; try again in a minute")
        _mints.append(now)


def mint(
    get_env: Optional[Callable[[str], Optional[str]]] = None,
    post: Optional[Callable[[str, str, Dict[str, Any]], Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
    clock: Callable[[], float] = time.monotonic,
) -> Dict[str, Any]:
    get_env = get_env or _env_value
    api_key = resolve_api_key(get_env)
    if api_key is None:
        raise TokenError(503, "GEMINI_API_KEY is not set on this Hermes host")
    _limited(clock)  # counted only for requests that reach Google
    model = resolve_model(get_env)
    api_version = resolve_api_version(get_env)
    body = token_request_body(model, now or datetime.now(timezone.utc))
    payload = (post or _post_json)(token_url(api_version), api_key, body)
    token = payload.get("name") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise TokenError(502, "Google's token response had no token")
    return {
        "token": token,
        # Google may shorten the window it was asked for: what it granted is what counts.
        "expires_at": payload.get("expireTime") or body["expireTime"],
        "new_session_expires_at": payload.get("newSessionExpireTime") or body["newSessionExpireTime"],
        "model": model,
        "websocket_url": websocket_url(api_version),
    }


def available(get_env: Optional[Callable[[str], Optional[str]]] = None) -> bool:
    """Whether this host has a Google key at all, for the app to say so before the user tries to talk."""
    try:
        return resolve_api_key(get_env or _env_value) is not None
    except Exception:  # noqa: BLE001
        return False
