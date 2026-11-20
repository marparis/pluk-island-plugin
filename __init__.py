"""pluk_notify: push notifications from Hermes to the PLUK Island Android app.

It listens to Hermes through its plugin hooks and sends, through the user's
own Firebase project, a sealed message the phone alone can read. It opens no
port and has no relay: the only traffic is outgoing HTTPS to Google.

Built from the parts of hermes-conduit-notifier that fit (MIT, (c) 2026
Hermes Conduit contributors; see LICENSE-CONDUIT): how the hooks are read,
the cleaned text, the sealing, the waiting list. Each module says what it
took.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_hooks: Any = None
_registered_profile = "default"
_titles: dict[tuple[str, str], tuple[float, str]] = {}
_titles_lock = threading.Lock()
TITLE_CACHE_S = 30.0

HOOKS = ("pre_tool_call", "post_llm_call", "on_session_end", "pre_approval_request", "post_approval_response",
         "on_human_input_request", "on_human_input_resolved", "subagent_start")
# Heard only to know that a turn runs in this process, so the plugin never restarts a service in the middle of one
# (updater.py). A Hermes without them is fine: the hooks above say it too.
ACTIVITY_HOOKS = ("on_session_start", "pre_llm_call")


def _profile() -> str:
    """The profile whose turn runs now: one process can serve several."""
    try:
        from hermes_cli.profiles import get_active_profile_name

        name = get_active_profile_name()
        if isinstance(name, str) and name:
            return name
    except Exception:  # noqa: BLE001
        pass
    return _registered_profile


def _session_title(key: str) -> str:
    """The session's title, or "". Read as hermes-conduit-notifier's session_titles.py reads it: the path is
    pinned to the profile's own state.db, since a hook can run off the turn's thread. Never raises."""
    if not key:
        return ""
    try:
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
        entry = (str(home), key)
        now = time.monotonic()
        with _titles_lock:
            hit = _titles.get(entry)
            if hit and now - hit[0] < TITLE_CACHE_S:
                return hit[1]
        from hermes_state import SessionDB

        db = SessionDB(db_path=home / "state.db")
        try:
            getter = getattr(db, "get_session_title", None)
            value = getter(key) if getter else None
        finally:
            closer = getattr(db, "close", None)
            if closer:
                closer()
        value = value.strip() if isinstance(value, str) else ""
        if value:
            with _titles_lock:
                _titles[entry] = (time.monotonic(), value)
                while len(_titles) > 256:
                    del _titles[next(iter(_titles))]
        return value
    except Exception:  # noqa: BLE001 - a notification must never break the turn
        logger.debug("pluk_notify: no title for a session", exc_info=True)
        return ""


def _quiet(name: str, function: Any) -> Any:
    """A hook must never break a turn, whatever happens here."""
    def call(**kwargs: Any) -> None:
        try:
            from . import updater

            updater.note_hook(name, kwargs.get("session_id"))
            if function is not None:
                function(**kwargs)
        except Exception:  # noqa: BLE001
            logger.warning("pluk_notify: the %s hook failed", name, exc_info=True)
        return None

    return call


def register(ctx: Any) -> None:
    global _hooks, _registered_profile
    from . import delivery
    from .hooks import Hooks

    try:
        _registered_profile = ctx.profile_name or "default"
    except Exception:  # noqa: BLE001
        _registered_profile = "default"
    _hooks = Hooks(enqueue=delivery.enqueue, profile=_profile, title=_session_title)
    for name in HOOKS:
        try:
            ctx.register_hook(name, _quiet(name, getattr(_hooks, name)))
        except Exception:  # noqa: BLE001 - an older Hermes without this hook: the others still work
            logger.info("pluk_notify: this Hermes has no %s hook", name)
    for name in ACTIVITY_HOOKS:
        try:
            ctx.register_hook(name, _quiet(name, None))
        except Exception:  # noqa: BLE001
            pass
    try:
        from . import updater

        updater.start_watch()
    except Exception:  # noqa: BLE001 - the notifications do not depend on the plugin updating itself
        logger.info("pluk_notify: the update watch did not start", exc_info=True)
    _register_call_tool(ctx)
    if hasattr(ctx, "on_unload"):
        try:
            ctx.on_unload(_unload)
        except Exception:  # noqa: BLE001
            pass
    try:
        from .cli import dispatch, register_cli

        ctx.register_cli_command(
            name="pluk-notify",
            help="PLUK Island notifications: status, test, paired devices",
            setup_fn=register_cli,
            handler_fn=dispatch,
            description="Shows whether PLUK Island's notifications are set up, sends a test, lists and removes paired devices.",
        )
    except Exception:  # noqa: BLE001 - the notifications do not depend on the command
        logger.info("pluk_notify: the CLI command was not registered", exc_info=True)


CALL_TOOL = "pluk_call_user"
CALL_SCHEMA: dict[str, Any] = {
    "name": CALL_TOOL,
    "description": (
        "Ring the user's phone through PLUK Island: it rings when this turn ends, and opens this conversation. "
        "Use it only when the user asked to be called or rung about what this turn does (\"call me when it is done\"), "
        "or for news they would want at once and that cannot wait for them to look. Never for a routine update. "
        "Finish the work first and end the turn with the result as your reply: the call opens on it. "
        "Refused when the user has not allowed calls, and when you rang them less than two minutes ago."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "One short sentence: why you call, and the news (for example \"The deploy finished, and two tests failed.\"). Up to 200 characters.",
            },
        },
        "required": ["reason"],
    },
}


def _call_handler(args: Any, session_id: Any = None, **_: Any) -> str:
    import json

    try:
        if _hooks is None:
            raise RuntimeError("not registered")
        answer = _hooks.ask_call(session_id, (args or {}).get("reason") if isinstance(args, dict) else None)
    except Exception:  # noqa: BLE001 - a tool must never raise
        logger.warning("pluk_notify: the call tool failed", exc_info=True)
        answer = {"ok": False, "error": "unavailable", "note": "The call could not be set up. Tell the user in your reply instead."}
    return json.dumps(answer, ensure_ascii=False)


def _calls_allowed() -> bool:
    """Whether the tool is offered to the model at all: only once the user turned calls on in the app. Hermes reads
    this when a session's tools are built, so a change shows in new sessions (or after the gateway restarts)."""
    try:
        from .store import Store

        return Store().settings()["calls"]
    except Exception:  # noqa: BLE001
        return False


def _register_call_tool(ctx: Any) -> None:
    if not hasattr(ctx, "register_tool"):
        return
    # Always offered, calls on or off: "call me" then reaches the tool, which answers that calls are off and where to turn
    # them on. Hidden while calls were off, an agent that was asked to call had nothing to call with and said nothing
    # (hermes-conduit-notifier met the same and changed the same way, plugin 0.134.9).
    tool = {"name": CALL_TOOL, "toolset": "pluk", "schema": CALL_SCHEMA, "handler": _call_handler}
    try:
        try:
            ctx.register_tool(**tool, description=CALL_SCHEMA["description"], emoji="\U0001F4DE")
        except TypeError:  # a Hermes without the display fields
            ctx.register_tool(**tool)
    except Exception:  # noqa: BLE001 - the notifications do not depend on the tool
        logger.info("pluk_notify: the call tool was not registered", exc_info=True)


def _unload() -> None:
    from . import delivery

    if _hooks is not None:
        _hooks.unload()
    delivery.stop()
