"""What Hermes tells the plugin, and what the phone hears of it.

The rules come from hermes-conduit-notifier's __init__.py (MIT, (c) 2026
Hermes Conduit contributors): one notification per turn, held until Hermes
says how the turn ended, so a failed turn sends its failure and not a reply;
nothing for a delegated child's own turns; nothing for the [SILENT] sentinel
or for a script's one-shot run; a wait kept in a list until Hermes says it
ended.

Hooks run on Hermes' threads, sometimes on a worker thread of the hook
dispatcher, so nothing here relies on the calling thread. A hook never
raises and never waits for the network.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Callable

from . import events
from .store import Store

logger = logging.getLogger(__name__)

# How long a reply waits for Hermes to say how its turn ended before it goes anyway.
READY_GRACE_S = 60.0
# "The agent is at work" is told at most this often for one session: enough to follow, never a stream.
PROGRESS_EVERY_S = 25.0
# Two calls are never closer than this (Conduit's rule against being pestered).
CALL_GAP_S = 120.0
# The two hooks that describe one approval fire back to back.
PAIRING_WINDOW_S = 5.0
CLARIFY_ARGS_S = 120.0
MAX_TRACKED = 2000
# A turn of a script, a one-shot or a Kanban worker is not a conversation (Conduit's AUTOMATION_SOURCES).
AUTOMATION_SOURCES = frozenset({"tool", "oneshot", "kanban"})
# The user is at that very terminal: a push would only repeat what the screen shows.
LOCAL_PLATFORMS = frozenset({"cli"})


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


class Hooks:
    def __init__(
        self,
        *,
        enqueue: Callable[[dict[str, Any]], Any],
        store: Callable[[], Store] = Store,
        profile: Callable[[], str] = lambda: "default",
        title: Callable[[str], str] = lambda key: "",
        later: Callable[[float, Callable[[], None]], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._enqueue = enqueue
        self._store = store
        self._profile = profile
        self._title = title
        self._later = later or self._timer
        self._clock = clock
        self._lock = threading.Lock()
        self._children: dict[str, None] = {}
        self._clarify_args: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._human: dict[str, tuple[float, str, str, str]] = {}  # session key -> (when, Hermes' id, session id, platform)
        self._progress: dict[str, float] = {}  # session id -> when "at work" was last told
        self._calls: dict[str, str] = {}  # session id -> why the agent wants to ring the user, until its turn ends
        self._ready: dict[tuple[str, str], tuple[dict[str, Any], Any]] = {}

    @staticmethod
    def _timer(delay: float, work: Callable[[], None]) -> Any:
        timer = threading.Timer(delay, work)
        timer.daemon = True
        timer.start()
        return timer

    def unload(self) -> None:
        with self._lock:
            pending, self._ready = list(self._ready.values()), {}
        for _, timer in pending:
            _cancel(timer)

    # ------------------------------------------------------------ who is asking

    def _is_child(self, session_id: str) -> bool:
        try:
            from agent.delegation_context import is_delegated_child_context

            if is_delegated_child_context():
                return True
        except Exception:  # noqa: BLE001 - an older Hermes: the ids below answer
            pass
        with self._lock:
            return bool(session_id) and session_id in self._children

    def subagent_start(self, **kwargs: Any) -> None:
        child = _text(kwargs.get("child_session_id"))
        if child:
            with self._lock:
                self._children[child] = None
                while len(self._children) > MAX_TRACKED:
                    del self._children[next(iter(self._children))]

    @staticmethod
    def _automation(platform: Any) -> bool:
        try:
            from gateway.session_finite import finite_turn_required

            if finite_turn_required() is True:
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            from agent.session_source import session_source_for

            source = session_source_for(platform if isinstance(platform, str) and platform else None)
        except Exception:  # noqa: BLE001
            import os

            source = os.environ.get("HERMES_SESSION_SOURCE", "")
        return isinstance(source, str) and source.strip().lower() in AUTOMATION_SOURCES

    @staticmethod
    def _scheduled(platform: Any) -> bool:
        """A scheduled job's own turn: nobody watches it work, so it gets no "at work" line (its reply still goes,
        unless the job says [SILENT])."""
        if isinstance(platform, str) and platform.strip().lower() == "cron":
            return True
        try:
            from agent.session_source import session_source_for

            source = session_source_for(platform if isinstance(platform, str) and platform else None)
        except Exception:  # noqa: BLE001
            import os

            source = os.environ.get("HERMES_SESSION_SOURCE", "")
        return isinstance(source, str) and source.strip().lower() == "cron"

    @staticmethod
    def _ambient_platform() -> str:
        """The platform of the session running now, read the way Hermes' own human-input hook reads it."""
        try:
            import os

            from gateway.session_context import get_session_env

            return _text(os.getenv("HERMES_PLATFORM") or get_session_env("HERMES_SESSION_PLATFORM")
                         or get_session_env("HERMES_SESSION_SOURCE") or "")
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------ approvals and questions

    def pre_tool_call(self, **kwargs: Any) -> None:
        """Tells that the agent is at work (now and then, never a stream), and reads a question's choices: Hermes'
        own "waiting on a human" hook carries the words alone."""
        session_id = _text(kwargs.get("session_id"))
        tool = _text(kwargs.get("tool_name"))
        if tool != "clarify":
            self._at_work(session_id, tool)
            return None
        questions = events.normalize_questions(kwargs.get("args"))
        if session_id and questions:
            with self._lock:
                self._clarify_args[session_id] = (self._clock(), questions)
                while len(self._clarify_args) > 200:
                    del self._clarify_args[next(iter(self._clarify_args))]
        return None

    # ------------------------------------------------------------ Hermes rings the user

    def ask_call(self, session_id: Any, reason: Any) -> dict[str, Any]:
        """What the pluk_call_user tool does: it notes the call, which rings when this turn ends, so it opens on the
        finished result (Conduit's rule). Refused when the user did not turn calls on, for a sub-agent, and when
        the last call is too recent."""
        session = _text(session_id)
        why = events.shown_clean(reason, 200)
        store = self._store()
        settings = store.settings()
        if not settings["calls"]:
            return {"ok": False, "error": "calls_off", "note": "The user has not allowed calls. Say what you have to say in your reply instead; "
                                                               "they can allow calls in PLUK Island, Settings > Notifications."}
        if not session or not why:
            return {"ok": False, "error": "bad_request", "note": "Give one short sentence in `reason`: why you call, and the news."}
        if self._is_child(session):
            return {"ok": False, "error": "not_here", "note": "Only the main conversation can ring the user."}
        wait = CALL_GAP_S - (self._clock() - settings["last_call_at"])
        if wait > 0:
            return {"ok": False, "error": "too_soon", "note": f"You rang the user moments ago. Put the news in your reply; you may call again in {int(wait) + 1} seconds."}
        with self._lock:
            self._calls[session] = why
            while len(self._calls) > 100:
                del self._calls[next(iter(self._calls))]
        return {"ok": True, "note": "The user's phone will ring when this turn ends. Finish the work and end with the result as your reply."}

    def _ring(self, session_id: str, platform: str) -> bool:
        """Rings for a call the turn asked for, now that it ended with an answer. True when it did."""
        with self._lock:
            why = self._calls.pop(session_id, None)
        if not why:
            return False
        store = self._store()
        store.note_call(self._clock())
        self._enqueue(events.build(
            events.CALL, identifier=events.event_id("call", session_id, int(self._clock())), profile=self._profile(),
            session_id=session_id, platform=platform, title=self._title(session_id), body=why,
        ))
        return True

    def _at_work(self, session_id: str, tool: str) -> None:
        if not session_id or not tool or self._is_child(session_id):
            return
        now = self._clock()
        with self._lock:
            last = self._progress.get(session_id)
            if last is not None and now - last < PROGRESS_EVERY_S:
                return
            self._progress.pop(session_id, None)
            self._progress[session_id] = now
            while len(self._progress) > 500:
                del self._progress[next(iter(self._progress))]
        platform = self._ambient_platform()
        if platform in LOCAL_PLATFORMS or self._automation(platform) or self._scheduled(platform):
            return
        self._enqueue(events.build(
            events.PROGRESS, identifier=events.event_id("progress", session_id, int(now)), profile=self._profile(),
            session_id=session_id, platform=platform, title=self._title(session_id), tool=tool,
        ))

    def _work_over(self, session_id: str, told_otherwise: bool) -> None:
        """The turn ended. A reply or a failure that goes out says so by itself; a turn that ends without one (stopped,
        or with nothing to say) gets its own short word, so the phone's "at work" notification never outlives it."""
        with self._lock:
            was_told = self._progress.pop(session_id, None) is not None
        if was_told and not told_otherwise:
            self._enqueue(events.build(
                events.ENDED, identifier=events.event_id("ended", session_id, int(self._clock())), profile=self._profile(),
                session_id=session_id,
            ))

    def on_human_input_request(self, **kwargs: Any) -> None:
        kind = kwargs.get("kind")
        human_id = _text(kwargs.get("request_id"))
        session_id = _text(kwargs.get("session_id"))
        session_key = _text(kwargs.get("session_key"))
        platform = _text(kwargs.get("platform"))
        if not human_id:
            return
        if kind == "approval":
            # The approval hook describes it (its reason, its command) and fires just before or just after this one.
            store = self._store()
            entry = store.find_waiting(session_key=session_key, kind="approval", unlinked=True, within=PAIRING_WINDOW_S) if session_key else None
            if entry and store.link_waiting(entry, human_id):
                return
            with self._lock:
                self._human[session_key] = (self._clock(), human_id, session_id, platform)
                while len(self._human) > 200:
                    del self._human[next(iter(self._human))]
            return
        if kind != "clarify" or platform in LOCAL_PLATFORMS:
            return
        with self._lock:
            stashed = self._clarify_args.pop(session_id, None)
        questions = stashed[1] if stashed and self._clock() - stashed[0] <= CLARIFY_ARGS_S else []
        if not questions:
            prompt = events.shown_clean(kwargs.get("prompt"), 300)
            questions = [{"qid": "q0", "question": prompt}] if prompt else []
        profile = self._profile()
        title = self._title(session_id or session_key)
        body = questions[0]["question"] if questions else ""
        self._store().note_waiting({
            "id": human_id, "human_id": human_id, "kind": "question", "profile": profile, "session_id": session_id,
            "session_key": session_key, "platform": platform, "title": title, "description": body, "questions": questions,
        })
        self._enqueue(events.build(
            events.QUESTION, identifier=events.event_id("input", human_id), profile=profile, session_id=session_id,
            session_key=session_key, platform=platform, title=title, body=body, request_id=human_id, questions=questions,
        ))

    def pre_approval_request(self, **kwargs: Any) -> None:
        # "smart" is Hermes' own model deciding: nobody is asked. A coalesced request waits on one already shown.
        surface = _text(kwargs.get("surface"))
        if surface in ("smart", "cli") or kwargs.get("coalesced"):
            return
        session_key = _text(kwargs.get("session_key"))
        with self._lock:
            human = self._human.pop(session_key, None)
        if human and self._clock() - human[0] > PAIRING_WINDOW_S:
            human = None
        session_id = _text(kwargs.get("session_id")) or (human[2] if human else "")
        platform = (human[3] if human else "") or self._ambient_platform()
        if platform in LOCAL_PLATFORMS:
            return
        description = events.shown_clean(kwargs.get("description"), 500)
        command = kwargs.get("command") if isinstance(kwargs.get("command"), str) else ""
        entry_id = uuid.uuid4().hex
        profile = self._profile()
        title = self._title(session_id or session_key)
        entry = {
            "id": entry_id, "kind": "approval", "profile": profile, "session_id": session_id, "session_key": session_key,
            "platform": platform, "title": title, "description": description, "command": command[:4000], "surface": surface,
        }
        if human:
            entry["human_id"] = human[1]
        self._store().note_waiting(entry)
        self._enqueue(events.build(
            events.APPROVAL, identifier=events.event_id("approval", entry_id), profile=profile, session_id=session_id,
            session_key=session_key, platform=platform, title=title, body=description, command=command, request_id=entry_id,
        ))

    def on_human_input_resolved(self, **kwargs: Any) -> None:
        human_id = _text(kwargs.get("request_id"))
        if not human_id:
            return
        with self._lock:
            for key in [k for k, v in self._human.items() if v[1] == human_id]:
                del self._human[key]
        self._resolved(self._store().resolve_waiting(human_id=human_id))

    def post_approval_response(self, **kwargs: Any) -> None:
        """For an approval Hermes' "waiting on a human" hook did not name (a file-write guard, an older Hermes).
        One that hook named is ended by it, so two approvals of one session are never mixed up."""
        session_key = _text(kwargs.get("session_key"))
        if session_key:
            self._resolved(self._store().resolve_waiting(session_key=session_key, kind="approval", unlinked=True))

    def _resolved(self, entry: dict[str, Any] | None) -> None:
        if not entry:
            return
        # Takes the notification off the phone when the answer came from elsewhere (Telegram, the Cockpit) or the wait ended.
        self._enqueue(events.build(
            events.RESOLVED, identifier=events.event_id("resolved", entry.get("id")), profile=_text(entry.get("profile")),
            session_id=_text(entry.get("session_id")), request_id=_text(entry.get("id")),
        ))

    # ------------------------------------------------------------ a turn's reply, or its failure

    def post_llm_call(self, **kwargs: Any) -> None:
        session_id = _text(kwargs.get("session_id"))
        platform = _text(kwargs.get("platform"))
        response = kwargs.get("assistant_response")
        if self._is_child(session_id) or platform in LOCAL_PLATFORMS:
            return
        body = events.plain_text(response)
        if not body or events.is_silent_response(response) or self._automation(platform):
            return
        turn_id = _text(kwargs.get("turn_id"))
        ready = events.build(
            events.REPLY, identifier=events.event_id("response", turn_id or uuid.uuid4().hex, session_id), profile=self._profile(),
            session_id=session_id, platform=platform, title=self._title(session_id), body=body,
        )
        if not turn_id:
            self._enqueue(ready)
            return
        # Hermes also runs this hook for a turn that failed with text: on_session_end, right after, says which it was.
        key = (session_id, turn_id)
        timer = self._later(READY_GRACE_S, lambda: self._settle(key, send=True))
        with self._lock:
            earlier = self._ready.pop(key, None)
            self._ready[key] = (ready, timer)
            overflow = []
            while len(self._ready) > 200:
                overflow.append(self._ready.pop(next(iter(self._ready))))
        for _, old_timer in ([earlier] if earlier else []):
            _cancel(old_timer)
        for old_ready, old_timer in overflow:
            _cancel(old_timer)
            self._enqueue(old_ready)

    def _settle(self, key: tuple[str, str], *, send: bool) -> bool:
        with self._lock:
            held = self._ready.pop(key, None)
        if held is None:
            return False
        _cancel(held[1])
        if send:
            self._enqueue(held[0])
        return True

    def on_session_end(self, **kwargs: Any) -> None:
        session_id = _text(kwargs.get("session_id"))
        turn_id = _text(kwargs.get("turn_id"))
        platform = _text(kwargs.get("platform"))
        if self._is_child(session_id):
            return
        # A turn that hit its step limit still answered: its reply goes out, not a failure (Conduit's rule).
        answered = (not kwargs.get("interrupted") and kwargs.get("failed") is not True
                    and (bool(kwargs.get("completed")) or kwargs.get("failed") is False))
        if answered:
            # A call the turn asked for rings in place of the reply's notification: the reply is in the chat it opens.
            if self._ring(session_id, platform):
                self._settle((session_id, turn_id), send=False)
                self._work_over(session_id, told_otherwise=True)
                return
            self._work_over(session_id, told_otherwise=self._settle((session_id, turn_id), send=True))
            return
        with self._lock:
            self._calls.pop(session_id, None)  # a turn that failed or was stopped rings nobody
        self._settle((session_id, turn_id), send=False)
        if kwargs.get("interrupted") or not turn_id or platform in LOCAL_PLATFORMS or self._automation(platform):
            self._work_over(session_id, told_otherwise=False)
            return  # stopped by the user, a chat closing, or nobody to tell
        self._work_over(session_id, told_otherwise=True)
        self._enqueue(events.build(
            events.FAILED, identifier=events.event_id("failure", turn_id, session_id), profile=self._profile(),
            session_id=session_id, platform=platform, title=self._title(session_id),
        ))


def _cancel(timer: Any) -> None:
    try:
        timer.cancel()
    except Exception:  # noqa: BLE001
        pass
