"""pluk_notify's routes on the Hermes Dashboard, mounted at /api/plugins/pluk_notify/.

They sit behind the Dashboard's own sign-in, like every plugin route: only
the app that signed in (the same session it reads sessions with) reaches
them. Nothing new listens on the network.

    GET  /status    what is set up (no secret)
    GET  /config    the public Firebase settings the app starts Firebase with
    POST /pair      a device gives its Firebase token and gets its id and, once, its secret
    POST /unpair    a device leaves
    GET  /waiting   what waits for the user, on every profile
    POST /test      a test notification to one device
    GET  /settings  what the user chose on the server's side (calls on or off)
    POST /settings  changes it
    POST /voice/token  a single-use Gemini Live token for the voice companion (the Google key stays on this host)
    POST /update    the app brings a newer version of this plugin (every release of the app carries one); it is
                    checked, written, and each Hermes service restarts itself when it is idle (updater.py)
    GET  /artifacts what a profile's chats produced: files sent or written, pages, attachments, links
    POST /predict   suggested replies: what the user will most likely send next in a chat
    POST /side-question  a question about a chat (a run's steps summed up), answered only to the app
    GET  /calls     the calls Hermes made after `after` (seconds), for the PC to ring them too
    POST /pair-desktop        the phone leaves the PC's one-time sign-in code (pairing.py)
    GET  /pair-desktop/claim  the PC takes it with its pairing secret as bearer token
    GET  /mutes     the muted chats
    POST /mute      mutes a chat for a while or until unmuted, or unmutes it
    /pulse...       Pulse, the assistant that speaks first (pulse_api.py lists its routes)
    POST /answer    deny one waiting approval (the "Deny" button of a notification). It can never allow one:
                    allowing is done in the app, on the card that shows the whole command, behind the fingerprint.

Hermes loads this file by path, outside any package, so the plugin's own
modules (one folder up) are loaded here as a private package (the way
hermes-conduit-notifier's dashboard/plugin_api.py loads its own; MIT,
(c) 2026 Hermes Conduit contributors).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import threading
import time
import types
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query, Request, Response

_PACKAGE = "_pluk_notify_lib"


def _library() -> Any:
    package = sys.modules.get(_PACKAGE)
    if package is None:
        package = types.ModuleType(_PACKAGE)
        package.__path__ = [os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]  # type: ignore[attr-defined]
        sys.modules[_PACKAGE] = package
    return types.SimpleNamespace(**{name: importlib.import_module(f"{_PACKAGE}.{name}")
                                    for name in ("store", "delivery", "sealing", "events", "cli", "gemini", "updater",
                                                 "artifacts_index", "predict", "side", "pulse_api", "pairing")})


lib = _library()
router = APIRouter()
# Pulse's routes (pulse_api.py, after Hermes Conduit's; MIT, (c) 2026 Hermes Conduit contributors)
router.include_router(lib.pulse_api.router)
# Pairing the PC with the phone (pairing.py): the claim route accepts the pairing secret, and only that
PAIRING_READY = lib.pairing.register()

MAX_BODY = 16 * 1024
# The whole plugin, as base64 in JSON.
MAX_UPDATE_BODY = 3 * 1024 * 1024
try:
    lib.updater.start_watch()  # the Dashboard's own service restarts itself too, when no turn runs in it
except Exception:  # noqa: BLE001
    pass
# Generous for an app, tight for a loop gone wrong: per route, for the whole Dashboard.
_LIMITS = {"pair": (20, 600.0), "test": (6, 60.0), "read": (240, 60.0), "answer": (60, 60.0), "voice": (30, 60.0), "update": (6, 600.0)}
_hits: dict[str, list[float]] = {}
_hits_lock = threading.Lock()


def _limited(bucket: str) -> None:
    limit, window = _LIMITS[bucket]
    now = time.monotonic()
    with _hits_lock:
        recent = [t for t in _hits.get(bucket, []) if now - t < window]
        if len(recent) >= limit:
            raise HTTPException(status_code=429, detail="Too many requests; try again shortly")
        recent.append(now)
        _hits[bucket] = recent


async def _body(request: Request, limit: int = MAX_BODY) -> Dict[str, Any]:
    raw = await request.body()
    if len(raw) > limit:
        raise HTTPException(status_code=413, detail="Body too large")
    try:
        value = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Body is not JSON") from None
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="Body is not a JSON object")
    return value


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _status() -> Dict[str, Any]:
    info = lib.cli.status()
    info.pop("folder", None)  # a host path is nothing the phone needs
    info["voice"] = lib.gemini.available()  # whether this host has a Google key for the voice companion
    info.update(lib.updater.state())  # version (on disk), running (in this process), auto_restart
    return info


@router.post("/update")
async def post_update(request: Request, response: Response) -> Dict[str, Any]:
    _no_store(response)
    _limited("update")
    body = await _body(request, MAX_UPDATE_BODY)
    try:
        version = await asyncio.to_thread(lambda: lib.updater.apply(lib.updater.decode(body.get("archive"))))
    except lib.updater.UpdateError as error:
        raise HTTPException(status_code=error.status, detail=str(error), headers={"Cache-Control": "no-store"}) from None
    return {"ok": True, **lib.updater.state(), "version": version}


@router.post("/voice/token")
async def post_voice_token(response: Response) -> Dict[str, Any]:
    _no_store(response)
    _limited("voice")
    try:
        return {"ok": True, **(await asyncio.to_thread(lib.gemini.mint))}
    except lib.gemini.TokenError as error:
        raise HTTPException(status_code=error.status, detail=str(error), headers={"Cache-Control": "no-store"}) from None


@router.get("/status")
async def get_status(response: Response) -> Dict[str, Any]:
    _no_store(response)
    _limited("read")
    return {"ok": True, **(await asyncio.to_thread(_status))}


@router.get("/config")
async def get_config(response: Response) -> Dict[str, Any]:
    _no_store(response)
    _limited("read")
    config = await asyncio.to_thread(lib.store.firebase_config)
    if config is None:
        raise HTTPException(status_code=409, detail="google-services.json is missing on the server, or it has no Android app com.pluk.island")
    return {"ok": True, "firebase": config, "ready": bool(await asyncio.to_thread(lib.store.service_account)) and lib.sealing.available()}


@router.post("/pair")
async def post_pair(request: Request, response: Response) -> Dict[str, Any]:
    _no_store(response)
    _limited("pair")
    body = await _body(request)
    if not lib.sealing.available():
        raise HTTPException(status_code=503, detail="The cryptography library is missing on the server")

    def pair() -> tuple:
        return lib.store.Store().pair(body.get("token"), body.get("name"), body.get("kinds"), body.get("device_id"))

    try:
        device_id, secret = await asyncio.to_thread(pair)
    except lib.store.StoreError as error:
        raise HTTPException(status_code=error.status, detail=str(error)) from None
    reply: Dict[str, Any] = {"ok": True, "device_id": device_id}
    if secret is not None:
        reply["secret"] = secret
    return reply


@router.post("/unpair")
async def post_unpair(request: Request, response: Response) -> Dict[str, Any]:
    _no_store(response)
    _limited("pair")
    body = await _body(request)
    removed = await asyncio.to_thread(lambda: lib.store.Store().unpair(body.get("device_id")))
    return {"ok": True, "removed": bool(removed)}


@router.get("/waiting")
async def get_waiting(response: Response) -> Dict[str, Any]:
    _no_store(response)
    _limited("read")
    return {"ok": True, "waiting": await asyncio.to_thread(lambda: lib.store.Store().waiting_list())}


@router.post("/test")
async def post_test(request: Request, response: Response) -> Dict[str, Any]:
    _no_store(response)
    _limited("test")
    body = await _body(request)
    device_id = body.get("device_id")
    if not isinstance(device_id, str) or device_id not in await asyncio.to_thread(lambda: lib.store.Store().devices()):
        raise HTTPException(status_code=404, detail="This device is not paired")
    result = await asyncio.to_thread(lambda: lib.delivery.deliver(lib.cli.test_event(), only_device=device_id))
    return {"ok": result["sent"] == 1, "error": result["error"]}


@router.get("/settings")
async def get_settings(response: Response) -> Dict[str, Any]:
    _no_store(response)
    _limited("read")
    settings = await asyncio.to_thread(lambda: lib.store.Store().settings())
    return {"ok": True, "calls": settings["calls"]}


@router.post("/settings")
async def post_settings(request: Request, response: Response) -> Dict[str, Any]:
    _no_store(response)
    _limited("pair")
    body = await _body(request)
    if not isinstance(body.get("calls"), bool):
        raise HTTPException(status_code=400, detail="`calls` must be true or false")
    await asyncio.to_thread(lambda: lib.store.Store().set_calls(body["calls"]))
    return {"ok": True, "calls": body["calls"]}


# ------------------------------------------------------------ artifacts
# What a profile's chats produced, read from its state.db (read only) by artifacts_index.py, taken from Hermes
# Conduit's notifier plugin (MIT, (c) 2026 Hermes Conduit contributors). Paged by message id: `before` is the previous
# page's next_before. One chat: its session ids as `session_id` (the chats it continues are read with it).
ARTIFACTS_MAX_SESSIONS = 16
ARTIFACTS_TIMEOUT_S = 25.0
_artifacts_slots = threading.BoundedSemaphore(2)


def _profile_home(profile: str):
    """A profile's Hermes home, as Hermes names it ("default" is the root)."""
    name = lib.events.profile_name(profile)
    try:
        from hermes_cli.profiles import get_profile_dir

        return get_profile_dir(name)
    except Exception:  # noqa: BLE001 - an older Hermes, or tests
        root = lib.store.hermes_root()
        return root if name == "default" else root / "profiles" / name.lower()


def read_artifacts(profile: str, *, before: Any = None, session_ids: Any = None, kind: Any = None, q: Any = "",
                   limit: Any = None, missing: Any = None) -> Dict[str, Any]:
    index = lib.artifacts_index
    if kind not in (None, "") and kind not in index.GROUPS:
        raise HTTPException(status_code=400, detail="kind is images, pages, files or links")
    if missing not in (None, "", "show", "hide"):
        raise HTTPException(status_code=400, detail="missing is show or hide")
    ids = [s.strip() for s in (session_ids or []) if isinstance(s, str) and s.strip() and len(s) <= 200][:ARTIFACTS_MAX_SESSIONS]
    if session_ids and not ids:
        return {"items": [], "next_before": None}  # a chat no id can name: never every chat instead
    try:
        conn = index.open_db(_profile_home(profile))
    except FileNotFoundError:
        return {"items": [], "next_before": None}
    try:
        return index.list_artifacts(conn, before=before, session_ids=ids, group=kind or None, query=q or "",
                                    limit=index.DEFAULT_LIMIT if limit is None else limit,
                                    include_missing=missing != "hide")
    finally:
        conn.close()


@router.get("/artifacts")
async def get_artifacts(response: Response, profile: str = "default", before: int | None = None,
                        session_id: list[str] | None = Query(None), kind: str | None = None, q: str | None = None,
                        limit: int | None = None, missing: str | None = None) -> Dict[str, Any]:
    _no_store(response)
    _limited("read")
    if not _artifacts_slots.acquire(blocking=False):
        raise HTTPException(status_code=503, detail="Still reading artifacts; try again shortly")
    try:
        page = await asyncio.wait_for(asyncio.to_thread(lambda: read_artifacts(
            profile, before=before, session_ids=session_id, kind=kind, q=q, limit=limit, missing=missing)),
            timeout=ARTIFACTS_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Reading artifacts took too long") from None
    finally:
        _artifacts_slots.release()
    return {"ok": True, **page}


# ------------------------------------------------------------ suggested replies and step summaries
# Hermes' own /btw answer about a chat, to the app that asked only (side.py; predict.py and the idea from Hermes
# Conduit's notifier plugin, MIT, (c) 2026 Hermes Conduit contributors). Only when the app asks: nothing runs alone.
SIDE_MAX_BODY = 6 * 40_000 + 1024
SIDE_WAIT_S = 90.0


async def _side(question: str, body: Dict[str, Any], wait_s: float) -> str:
    profile = body.get("profile") if isinstance(body.get("profile"), str) else "default"
    session_id = body.get("session_id")
    try:
        return await asyncio.wait_for(asyncio.to_thread(
            lambda: lib.side.ask(question, session_id, _profile_home(profile))), timeout=wait_s + 1.0)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="The answer took too long") from None
    except lib.side.SideError as error:
        raise HTTPException(status_code=error.status, detail=str(error)) from None


@router.post("/predict")
async def post_predict(request: Request, response: Response) -> Dict[str, Any]:
    """{profile, session_id, count?, language?, custom_instructions?}: up to `count` replies the user may send next."""
    _no_store(response)
    _limited("answer")
    body = await _body(request)
    try:
        asked = lib.predict.parse({**body, "kind": "replies", "model": "chat"})
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    text = await _side(lib.predict.question(asked), {**body, "session_id": asked.session_id}, asked.wait_s)
    return {"ok": True, **lib.predict.answer(asked, text)}


@router.post("/side-question")
async def post_side_question(request: Request, response: Response) -> Dict[str, Any]:
    """{profile, session_id, question}: Hermes' answer about that chat."""
    _no_store(response)
    _limited("answer")
    body = await _body(request, SIDE_MAX_BODY)
    question = body.get("question")
    if not isinstance(question, str) or not question.strip() or len(question) > lib.side.MAX_QUESTION_CHARS:
        raise HTTPException(status_code=400, detail="question must be text of at most 40000 characters")
    return {"ok": True, "answer": await _side(question, body, SIDE_WAIT_S)}


@router.post("/pair-desktop")
async def post_pair_desktop(request: Request, response: Response) -> Dict[str, Any]:
    """{key_hash, code, state}: the phone, signed in, leaves the PC's one-time code under its pairing secret's hash."""
    _no_store(response)
    _limited("pair")
    if not PAIRING_READY:
        raise HTTPException(status_code=501, detail="This Hermes cannot pair a computer this way")
    body = await _body(request)
    try:
        lib.pairing.deposit(body.get("key_hash"), body.get("code"), body.get("state"))
    except lib.pairing.PairingError as error:
        raise HTTPException(status_code=error.status, detail=str(error)) from None
    return {"ok": True}


@router.get("/pair-desktop/claim")
async def get_pair_desktop_claim(request: Request, response: Response) -> Dict[str, Any]:
    """The PC, with its pairing secret as bearer token (the only credential this route takes), takes its code once."""
    _no_store(response)
    _limited("read")
    found = lib.pairing.claim(lib.pairing.bearer(request))
    if found is None:
        raise HTTPException(status_code=404, detail="Nothing waits for this pairing")
    return {"ok": True, **found}


@router.get("/calls")
async def get_calls(response: Response, after: float = 0.0) -> Dict[str, Any]:
    """The calls Hermes made after `after` (seconds since 1970): the Island rings for each, as the phone does."""
    _no_store(response)
    _limited("read")
    rows = await asyncio.to_thread(lambda: lib.store.Store().calls_after(after))
    return {"ok": True, "calls": rows, "now": time.time()}


@router.get("/mutes")
async def get_mutes(response: Response) -> Dict[str, Any]:
    _no_store(response)
    _limited("read")
    rows = await asyncio.to_thread(lambda: lib.store.Store().mutes())
    out = []
    for key, until in rows.items():
        profile, _, session_id = key.partition("\u0000")
        out.append({"profile": profile, "session_id": session_id, "until": until})
    return {"ok": True, "mutes": out}


# How long a mute lasts, as the apps offer it (minutes; 0 is until unmuted).
MUTE_SPANS = {15, 60, 24 * 60, 0}


@router.post("/mute")
async def post_mute(request: Request, response: Response) -> Dict[str, Any]:
    """{profile, session_id, minutes: 15 | 60 | 1440 | 0 (until unmuted)} mutes a chat; {..., minutes: null} unmutes it."""
    _no_store(response)
    _limited("answer")
    body = await _body(request)
    profile = body.get("profile") if isinstance(body.get("profile"), str) else "default"
    session_id = body.get("session_id")
    minutes = body.get("minutes")
    if minutes is not None and (isinstance(minutes, bool) or minutes not in MUTE_SPANS):
        raise HTTPException(status_code=400, detail="`minutes` is 15, 60, 1440, 0 (until unmuted) or null (unmute)")
    until = None if minutes is None else (0.0 if minutes == 0 else time.time() + minutes * 60)
    try:
        await asyncio.to_thread(lambda: lib.store.Store().set_mute(profile, session_id, until))
    except lib.store.StoreError as error:
        raise HTTPException(status_code=error.status, detail=str(error)) from None
    return {"ok": True, "until": until}


@router.get("/telegram")
async def get_telegram(response: Response) -> Dict[str, Any]:
    """The user's Telegram application keys, for a device of theirs that has none yet. They are the user's own, kept on
    their own server, and only a signed-in Dashboard session reads them."""
    _no_store(response)
    _limited("read")
    settings = await asyncio.to_thread(lambda: lib.store.Store().settings())
    return {"ok": True, "telegram": settings["telegram"]}


@router.post("/telegram")
async def post_telegram(request: Request, response: Response) -> Dict[str, Any]:
    _no_store(response)
    _limited("pair")
    body = await _body(request)
    try:
        await asyncio.to_thread(lambda: lib.store.Store().set_telegram(body))
    except lib.store.StoreError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return {"ok": True}


def deny_here(entry: Dict[str, Any], reason: str) -> bool:
    """Denies the approval [entry] names if it waits in THIS process (a session the Dashboard itself runs). One that
    waits in another process (a profile's own gateway serving Telegram) is not reachable from here: False, and the
    app then says to answer it where its conversation runs. Uses Hermes' own approval queue, the way its
    `approval.respond` does; when several approvals wait on the session, only the one with the same command."""
    session_key = entry.get("session_key")
    if not isinstance(session_key, str) or not session_key:
        return False
    try:
        from tools import approval
    except Exception:  # noqa: BLE001 - not a Hermes process (tests), or an older Hermes
        return False
    try:
        pending = approval.list_gateway_approvals(session_key)
    except Exception:  # noqa: BLE001
        return False
    if not pending:
        return False
    command = entry.get("command") or ""
    same = [p for p in pending if isinstance(p, dict) and str(p.get("command") or "")[:4000] == command]
    if len(same) != 1 and len(pending) != 1:
        return False  # cannot tell which one is meant: never guess on a decision
    target = same[0] if len(same) == 1 else pending[0]
    request_id = target.get("request_id") if isinstance(target.get("request_id"), str) else None
    try:
        return int(approval.resolve_gateway_approval(session_key, "deny", reason=reason or None, request_id=request_id) or 0) > 0
    except Exception:  # noqa: BLE001
        return False


@router.post("/answer")
async def post_answer(request: Request, response: Response) -> Dict[str, Any]:
    _no_store(response)
    _limited("answer")
    body = await _body(request)
    if body.get("choice") != "deny":
        raise HTTPException(status_code=400, detail="Only a denial can be sent here; allowing is done on the card, in the app")
    entry = await asyncio.to_thread(lambda: lib.store.Store().get_waiting(body.get("request_id")))
    if entry is None or entry.get("kind") != "approval":
        return {"ok": True, "handled": False, "why": "gone"}
    reason = lib.events.clean(body.get("reason"), 500)
    handled = await asyncio.to_thread(deny_here, entry, reason)
    return {"ok": True, "handled": bool(handled), "why": "" if handled else "elsewhere"}
