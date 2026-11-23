"""Pulse in Hermes' hooks, as Hermes Conduit's notifier plugin wires it (its __init__.py; MIT, (c) 2026 Hermes
Conduit contributors):

- pre_llm_call: a Pulse turn (the hidden turn Pulse sends into its own chat) gets Pulse's note as its context;
- pre_tool_call: a Pulse turn may only look things up (pulse_tools); fails closed, so it is not wrapped;
- post_tool_call: every tool call of the profile goes in Pulse's outcome ledger while Pulse is on;
- post_llm_call and on_session_end: a Pulse turn's reply is pushed only when it is news for the user (a follow-up,
  a report with something worth a push), with Pulse's own title and words (hooks.py asks ``gate``), and a Pulse turn
  that failed tells nobody (Pulse records its own failures).

Pulse's loop starts with the plugin (pulse.register) and acts only in the process that hosts the Dashboard's chats,
only while Pulse is on. Where Hermes lacks what Pulse needs, nothing here registers and the notifications go on.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_pulse: Any = None


def _load() -> Any:
    global _pulse
    if _pulse is None:
        from .pulse import pulse as module

        _pulse = module
    return _pulse


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


class Gate:
    """What hooks.py asks about a turn: whether it is Pulse's, whether its reply pushes, and what the push says."""

    def is_pulse_turn(self, session_id: str, turn_id: Any) -> bool:
        try:
            return bool(_pulse is not None and _pulse.is_pulse_turn(session_id, turn_id))
        except Exception:  # noqa: BLE001
            return False

    def pushes(self, session_id: str, turn_id: Any, response: Any) -> bool:
        try:
            return bool(_pulse.pushes(session_id, turn_id, response))
        except Exception:  # noqa: BLE001 - a Pulse turn whose push can't be decided stays quiet
            logger.debug("pluk_notify: Pulse could not tell whether its turn pushes", exc_info=True)
            return False

    def push_copy(self, session_id: str, turn_id: Any, response: Any) -> dict[str, Any]:
        try:
            copy = _pulse.push_copy(session_id, turn_id, response)
            return copy if isinstance(copy, dict) else {}
        except Exception:  # noqa: BLE001
            return {"title": "Pulse", "private": True, "body": ""}

    def turn_ended(self, session_id: str, turn_id: Any) -> None:
        try:
            if _pulse is not None:
                _pulse.turn_ended(session_id, turn_id)
        except Exception:  # noqa: BLE001
            logger.debug("pluk_notify: Pulse's turn_ended failed", exc_info=True)


gate = Gate()


def pre_llm_call(**kwargs: Any) -> Optional[dict[str, str]]:
    try:
        note = _pulse.pre_llm_call(_text(kwargs.get("session_id")), kwargs.get("turn_id"), kwargs.get("user_message"))
    except Exception:  # noqa: BLE001 - a turn Pulse can't read is an ordinary turn
        logger.debug("pluk_notify: Pulse's pre_llm_call failed", exc_info=True)
        return None
    return {"context": note} if note else None


def pre_tool_call(**kwargs: Any) -> Optional[dict[str, str]]:
    # Fails closed: Hermes blocks the call when this raises.
    return _pulse.pre_tool_call(_text(kwargs.get("session_id")), kwargs.get("turn_id"), kwargs.get("tool_name"),
                                kwargs.get("args"))


_ledger_warned = False


def post_tool_call(**kwargs: Any) -> None:
    global _ledger_warned
    try:
        _pulse.post_tool_call(**kwargs)
    except Exception:  # noqa: BLE001 - the ledger never breaks a tool call
        if not _ledger_warned:
            _ledger_warned = True
            logger.warning("pluk_notify: Pulse's outcome ledger failed", exc_info=True)


def register(ctx: Any) -> bool:
    """Starts Pulse for this load and registers its hooks; False when this Hermes lacks what Pulse needs."""
    try:
        module = _load()
    except Exception:  # noqa: BLE001
        logger.info("pluk_notify: Pulse is not available on this Hermes", exc_info=True)
        return False
    for name, function in (("pre_llm_call", pre_llm_call), ("pre_tool_call", pre_tool_call),
                           ("post_tool_call", post_tool_call)):
        try:
            ctx.register_hook(name, function)
        except Exception:  # noqa: BLE001 - an older Hermes without this hook
            logger.info("pluk_notify: this Hermes has no %s hook for Pulse", name)
    try:
        module.register(ctx)
    except Exception:  # noqa: BLE001
        logger.warning("pluk_notify: Pulse did not start", exc_info=True)
    return True


def unload() -> None:
    try:
        if _pulse is not None:
            _pulse.stop()
    except Exception:  # noqa: BLE001
        pass
