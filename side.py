"""Side questions about a chat, answered only to the app that asked.

Two uses, after Hermes Conduit (its notifier plugin's dashboard/api/chats.py, MIT, (c) 2026 Hermes Conduit
contributors):

- suggested replies: what the user will most likely send next (predict.py, taken from Conduit, writes the
  question and checks the answer);
- step summaries: "what did these steps do?" about a finished run.

Both are Hermes' own /btw answer (agent.side_question.answer_side_question): a fork of the live chat when the
Dashboard holds it (every tool refused, nothing written to the transcript, state.db or memory), else Hermes'
one-shot answer from the chat's history, read here from the profile's state.db (read only). Nothing runs unless
the app asks: no loop, no timer.
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

MAX_QUESTION_CHARS = 40_000
MAX_SESSION_ID_CHARS = 200
# The history a one-shot answer reads, newest last.
HISTORY_ROWS = 60
MAX_ROW_CHARS = 8_000
# Answers running at once on the host, and per chat (a summary and a suggestion may run together).
MAX_RUNNING = 4
PER_CHAT = 2
_RUNTIME_KEYS = ("model", "provider", "base_url", "api_key", "api_mode", "session_id")

_lock = threading.Lock()
_running: dict[str, int] = {}


class SideError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _live_sessions() -> Any:
    """The Dashboard's TUI gateway sessions when this process serves its chats, else None. Only a module the
    Dashboard itself loaded counts: importing it here would make a second, empty registry."""
    server = sys.modules.get("tui_gateway.server")
    sessions = getattr(server, "_sessions", None) if server is not None else None
    return sessions if sessions is not None and callable(getattr(sessions, "items", None)) else None


def live_chat(session_id: str) -> Optional[dict]:
    """The open chat named by its runtime id or its stored id (session_key), or None."""
    sessions = _live_sessions()
    if sessions is None:
        return None
    try:
        rows = list(sessions.items())
    except Exception:  # noqa: BLE001 - changed while read
        return None
    for key, session in rows:
        if isinstance(session, dict) and (key == session_id or str(session.get("session_key") or "") == session_id):
            return session
    return None


def _scope(session: Optional[dict]) -> Any:
    """Hermes' scope for the chat's own profile (config, secrets), as its side agents use."""
    server = sys.modules.get("tui_gateway.server")
    scope = getattr(server, "_session_profile_runtime_scope", None)
    if session is not None and callable(scope):
        try:
            return scope(session)
        except Exception:  # noqa: BLE001 - an older Hermes: the ambient profile
            pass
    return contextlib.nullcontext()


def stored_history(home: Path, session_id: str, rows: int = HISTORY_ROWS) -> list[dict[str, str]]:
    """The chat's last user and assistant messages from the profile's state.db (read only), oldest first."""
    path = Path(home) / "state.db"
    if not path.is_file():
        return []
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
    try:
        found = conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? AND role IN ('user', 'assistant')"
            " ORDER BY id DESC LIMIT ?", (session_id, int(rows))).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out = []
    for role, content in reversed(found):
        if isinstance(content, str) and content.strip():
            out.append({"role": role, "content": content[:MAX_ROW_CHARS]})
    return out


def _hermes_answer() -> Callable[..., str]:
    try:
        from agent.side_question import answer_side_question
    except ImportError:
        raise SideError(501, "This Hermes can't answer side questions; update Hermes") from None
    return answer_side_question


def ask(question: str, session_id: str, home: Path, *, answer: Optional[Callable[..., str]] = None) -> str:
    """Hermes' /btw answer about the chat, without the broadcast to every client."""
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION_CHARS:
        raise SideError(400, f"question must be text of at most {MAX_QUESTION_CHARS} characters")
    if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > MAX_SESSION_ID_CHARS:
        raise SideError(400, "session_id must be a chat's id")
    session_id = session_id.strip()
    answer = answer or _hermes_answer()
    with _lock:
        if _running.get(session_id, 0) >= PER_CHAT:
            raise SideError(429, "This chat's side questions are still running")
        if sum(_running.values()) >= MAX_RUNNING:
            raise SideError(503, "The host is busy with other side questions; try again shortly")
        _running[session_id] = _running.get(session_id, 0) + 1
    try:
        session = live_chat(session_id)
        agent = session.get("agent") if session else None
        history = list(getattr(agent, "_session_messages", None) or (session or {}).get("history") or [])
        if not history:
            key = str((session or {}).get("session_key") or session_id)
            history = stored_history(home, key)
        runtime = {k: getattr(agent, k, None) for k in _RUNTIME_KEYS} if agent is not None else None
        with _scope(session):
            try:
                text = answer(question.strip(), history, parent_agent=agent, main_runtime=runtime)
            except TypeError:  # an older Hermes without main_runtime
                text = answer(question.strip(), history, parent_agent=agent)
        return (text or "").strip()
    finally:
        with _lock:
            left = _running.get(session_id, 1) - 1
            if left > 0:
                _running[session_id] = left
            else:
                _running.pop(session_id, None)
