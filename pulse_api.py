"""Pulse's Dashboard routes, mounted with the plugin's others at /api/plugins/pluk_notify/pulse...

The same routes and answers as Hermes Conduit's (its notifier plugin's dashboard/api/pulse.py, MIT, (c) 2026 Hermes
Conduit contributors), so the Cockpit's and the app's Pulse screens read what Conduit's read. Pulse is one per host,
in the Dashboard's own Hermes home: these routes take no profile. What needs an iPhone (suggestions to add to the
phone's calendar) is refused, as Conduit refuses it on a host where no phone shares.

    GET  /pulse                 Pulse's state and settings
    PUT  /pulse                 changes settings (pulse_control.SETTINGS); answers with the new state
    POST /pulse/report          a report now
    POST /pulse/backfill        {"action": "estimate"|"start"|"stop"|"cancel", "days": 1-365}: read older chats
    GET  /pulse/memory          what Pulse remembers
    POST /pulse/memory/forget   {"id": n}
    GET  /pulse/followups       promises it checks on
    GET  /pulse/feed            the Pulse screen's cards
    POST /pulse/feed/seen       {"ids": [n...] | null}
    POST /pulse/rate            {"card": n, "rating": "up"|"down", "reason"?}
    POST /pulse/done            {"card": n, "done": bool}
    POST /pulse/dismiss         {"card": n, "dismissed": bool, "until"?}
    POST /pulse/acted           {"card": n, "chat"?: {"profile", "id"}}
    GET  /pulse/learned         what ratings taught it
    POST /pulse/learned/undo    {"id": n}
    POST /pulse/learned/reset   {"scope", "value"}
    PUT  /pulse/levels          {"kind", "level"}
    GET  /pulse/left_out        what reports considered and left out
    POST /pulse/left_out/bring_up  {"id": n}

Nothing here imports Pulse until a route is called, so the Dashboard loads the plugin's routes even where Hermes
lacks what Pulse needs.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict

from fastapi import APIRouter, HTTPException, Request, Response

router = APIRouter()

# Room for the note to Pulse beside the other settings (pulse_control.NOTE_LIMIT characters, each up to twelve bytes
# once JSON-escaped).
PULSE_MAX_BODY_BYTES = 32 * 1024
PULSE_LIMIT = 60
PULSE_WINDOW_S = 60.0
_hits: list[float] = []
_hits_lock = threading.Lock()
NO_STORE = {"Cache-Control": "no-store"}


def _limited() -> None:
    now = time.monotonic()
    with _hits_lock:
        _hits[:] = [t for t in _hits if now - t < PULSE_WINDOW_S]
        if len(_hits) >= PULSE_LIMIT:
            raise HTTPException(status_code=429, detail="Too many Pulse requests; try again shortly", headers=NO_STORE)
        _hits.append(now)


def _module(name: str) -> Any:
    """A module of the pulse subpackage, as the copy of the plugin these routes belong to has it."""
    return importlib.import_module(f"{__package__}.pulse.{name}")


def _home() -> Any:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _call(action: Callable[[Any, Any, Any], Any], reads_only: bool = False) -> Any:
    """``reads_only``: an action that writes nothing on a host that never turned Pulse on, answered there from a
    scratch store (so a host that never turned Pulse on keeps no file)."""
    control = _module("pulse_control")
    pulse = _module("pulse")
    home = _home()
    try:
        if reads_only and not (Path(home) / pulse.store.DB_NAME).exists():
            with tempfile.TemporaryDirectory() as scratch:
                return action(control, pulse.open_store(Path(scratch)), home)
        return action(control, pulse.open_store(home), home)
    except control.Refused as refused:
        # 409 with the reason's code, so the app words it; "invalid" is the caller's mistake.
        raise HTTPException(status_code=400 if refused.code == "invalid" else 409,
                            detail={"code": refused.code, **refused.data}, headers=NO_STORE) from None


async def _route(action: Callable[[Any, Any, Any], Any], reads_only: bool = False) -> Dict[str, Any]:
    _limited()
    try:
        return {"ok": True, **(await asyncio.to_thread(_call, action, reads_only))}
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **NO_STORE}
        raise
    except ImportError as exc:
        raise HTTPException(status_code=501, detail=f"This Hermes lacks what Pulse needs: {exc}", headers=NO_STORE) from None


async def _body(request: Request) -> Dict[str, Any]:
    raw = await request.body()
    if len(raw) > PULSE_MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Body too large", headers=NO_STORE)
    try:
        value = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Body is not JSON", headers=NO_STORE) from None
    return value if isinstance(value, dict) else {}


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.get("/pulse")
async def get_pulse(response: Response) -> Dict[str, Any]:
    _no_store(response)
    return await _route(lambda control, store, home: control.state(store, home), reads_only=True)


@router.put("/pulse")
async def put_pulse(request: Request, response: Response) -> Dict[str, Any]:
    _no_store(response)
    body = await _body(request)

    def action(control: Any, store: Any, home: Any) -> Dict[str, Any]:
        control.update(store, body, home)
        return control.state(store, home)

    return await _route(action)


@router.post("/pulse/report")
async def post_pulse_report(response: Response) -> Dict[str, Any]:
    _no_store(response)
    return await _route(lambda control, store, home: (control.request_report(store), {})[1], reads_only=True)


BACKFILL_ACTIONS = ("estimate", "start", "stop", "cancel")


@router.post("/pulse/backfill")
async def post_pulse_backfill(request: Request, response: Response) -> Dict[str, Any]:
    _no_store(response)
    body = await _body(request)
    name, days = body.get("action"), body.get("days")
    if name not in BACKFILL_ACTIONS or (days is not None and (isinstance(days, bool) or not isinstance(days, int)
                                                              or not 1 <= days <= 365)):
        raise HTTPException(status_code=400, detail={"code": "invalid", "field": "action" if name not in BACKFILL_ACTIONS
                                                     else "days"}, headers=NO_STORE)

    def action(control: Any, store: Any, home: Any) -> Dict[str, Any]:
        if name == "estimate":
            return {"estimate": control.backfill_estimate(store, home, days)}
        if name == "start":
            control.backfill_start(store, home, days)
        elif name == "stop":
            control.backfill_stop(store)
        else:
            control.backfill_cancel(store)
        return control.state(store, home)

    return await _route(action, reads_only=name != "start")


@router.get("/pulse/memory")
async def get_pulse_memory(response: Response, kind: str = "") -> Dict[str, Any]:
    _no_store(response)
    return await _route(lambda control, store, home: {"items": control.items(store, kind, home)}, reads_only=True)


@router.get("/pulse/followups")
async def get_pulse_followups(response: Response) -> Dict[str, Any]:
    _no_store(response)
    return await _route(lambda control, store, home: {"followups": control.followups(store)}, reads_only=True)


@router.post("/pulse/memory/forget")
async def post_pulse_forget(request: Request, response: Response) -> Dict[str, Any]:
    _no_store(response)
    item_id = (await _body(request)).get("id")
    return await _route(lambda control, store, home: {"forgotten": control.forget(store, item_id)}, reads_only=True)


@router.get("/pulse/feed")
async def get_pulse_feed(response: Response, looks: bool = False) -> Dict[str, Any]:
    """The Pulse screen: cards newest first, how many are unseen, each kind's loudness and the Pulse chat's id.
    Suggestion cards (adding to the phone's calendar) are never asked for: no phone shares here."""
    _no_store(response)
    return await _route(lambda control, store, home: control.feed(store, home, proposals=False, looks=looks),
                        reads_only=True)


@router.post("/pulse/feed/seen")
async def post_pulse_seen(request: Request, response: Response) -> Dict[str, Any]:
    _no_store(response)
    body = await _body(request)
    if "ids" not in body:
        raise HTTPException(status_code=400, detail={"code": "invalid", "field": "ids"}, headers=NO_STORE)
    ids = body["ids"]
    return await _route(lambda control, store, home: {"seen": control.see(store, ids, proposals=False)}, reads_only=True)




@router.post("/pulse/rate")
async def post_pulse_rate(request: Request, response: Response) -> Dict[str, Any]:
    _no_store(response)
    body = await _body(request)

    def action(control: Any, store: Any, home: Any) -> Dict[str, Any]:
        reason = body.get("reason") or ""
        learned = control.rate(store, body.get("card"), body.get("rating"), reason, home=home)
        card = store.card(body["card"]) if reason == "wrong_time" else None
        return {"learned": learned, "rest_until": card.get("rest_until") if card else None}

    return await _route(action, reads_only=True)


@router.post("/pulse/done")
async def post_pulse_done(request: Request, response: Response) -> Dict[str, Any]:
    _no_store(response)
    body = await _body(request)
    return await _route(lambda control, store, home: {"changed": control.done(store, body.get("card"), body.get("done"))},
                        reads_only=True)


@router.post("/pulse/dismiss")
async def post_pulse_dismiss(request: Request, response: Response) -> Dict[str, Any]:
    _no_store(response)
    body = await _body(request)

    def action(control: Any, store: Any, home: Any) -> Dict[str, Any]:
        changed = control.dismiss(store, body.get("card"), body.get("dismissed"), until=body.get("until"), home=home)
        card = store.card(body["card"])
        return {"changed": changed, "rest_until": card.get("rest_until") if card else None}

    return await _route(action, reads_only=True)


@router.post("/pulse/acted")
async def post_pulse_acted(request: Request, response: Response) -> Dict[str, Any]:
    _no_store(response)
    body = await _body(request)
    return await _route(lambda control, store, home: {"booked": control.acted(store, body.get("card"), chat=body.get("chat"))},
                        reads_only=True)


@router.get("/pulse/learned")
async def get_pulse_learned(response: Response) -> Dict[str, Any]:
    _no_store(response)
    return await _route(lambda control, store, home: control.learned(store, home), reads_only=True)


@router.post("/pulse/learned/undo")
async def post_pulse_undo(request: Request, response: Response) -> Dict[str, Any]:
    _no_store(response)
    lesson_id = (await _body(request)).get("id")
    return await _route(lambda control, store, home: {"undone": control.undo(store, lesson_id), **control.learned(store, home)},
                        reads_only=True)


@router.post("/pulse/learned/reset")
async def post_pulse_reset(request: Request, response: Response) -> Dict[str, Any]:
    _no_store(response)
    body = await _body(request)

    def action(control: Any, store: Any, home: Any) -> Dict[str, Any]:
        lesson = control.reset(store, body.get("scope"), body.get("value", ""), home=home)
        return {"reset": lesson, **control.learned(store, home)}

    return await _route(action, reads_only=True)


@router.put("/pulse/levels")
async def put_pulse_levels(request: Request, response: Response) -> Dict[str, Any]:
    _no_store(response)
    body = await _body(request)

    def action(control: Any, store: Any, home: Any) -> Dict[str, Any]:
        control.set_level(store, body.get("kind"), body.get("level"))
        return control.learned(store)

    return await _route(action, reads_only=True)


@router.get("/pulse/left_out")
async def get_pulse_left_out(response: Response) -> Dict[str, Any]:
    _no_store(response)
    return await _route(lambda control, store, home: {"left_out": control.left_out(store, home)}, reads_only=True)


@router.post("/pulse/left_out/bring_up")
async def post_pulse_bring_up(request: Request, response: Response) -> Dict[str, Any]:
    _no_store(response)
    candidate_id = (await _body(request)).get("id")
    return await _route(lambda control, store, home: {"card": control.bring_up(store, candidate_id, home)}, reads_only=True)
