"""What a notification says: pure builders, no Hermes and no network here.

The text cleaning (`plain_text`, the MEDIA tag read as its file's name, the
[SILENT] sentinel, the clarify questions' shapes) is taken from
hermes-conduit-notifier's events.py (MIT, (c) 2026 Hermes Conduit
contributors) and reduced to what PLUK Island shows.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import Any

# The kinds of events, as the app's settings name them.
APPROVAL = "approval.needed"
QUESTION = "input.needed"
REPLY = "response.ready"
FAILED = "turn.failed"
RESOLVED = "request.resolved"
# An agent is at work (which tool it uses now), and its turn is over: what the phone's "live" notification follows.
PROGRESS = "turn.progress"
ENDED = "turn.ended"
# Hermes rings the user (the agent used the pluk_call_user tool, and the user allowed calls).
CALL = "call.requested"
TEST = "test"

# What a device may ask not to receive. Requests always go.
OPTIONAL_KINDS = ("reply", "reply_telegram", "failure", "progress")
# These never wake a sleeping phone: they are shown when it is awake anyway, and are worth nothing a moment later.
QUIET_KINDS = (RESOLVED, PROGRESS, ENDED)

MAX_QUESTIONS = 8
MAX_CHOICES = 8
# An FCM data message holds 4096 bytes in all. The sealed body is base64 of
# the JSON plus a 16-byte tag, beside the envelope's own fields.
MAX_EVENT_BYTES = 2600

_PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def event_id(prefix: str, *parts: Any) -> str:
    values = [str(part).strip() for part in parts if str(part or "").strip()]
    if values:
        digest = hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()[:24]
        return f"{prefix}:{digest}"
    return f"{prefix}:{uuid.uuid4().hex[:24]}"


def routing_id(value: Any, maximum: int = 512) -> str:
    """A session id or key kept whole, or "" (a cut id names another chat, or none)."""
    if not isinstance(value, str) or not value or _CONTROL.search(value):
        return ""
    return value if len(value.encode("utf-8", "replace")) <= maximum else ""


def profile_name(value: Any) -> str:
    return value if isinstance(value, str) and _PROFILE.match(value) else "default"


_CHAT = re.compile(r"^-?[0-9]{1,20}$")
_THREAD = re.compile(r"^[0-9]{1,10}$")


def build(
    kind: str,
    *,
    identifier: str,
    profile: str = "default",
    session_id: str = "",
    session_key: str = "",
    platform: str = "",
    title: str = "",
    body: str = "",
    command: str = "",
    request_id: str = "",
    questions: list[dict[str, Any]] | None = None,
    tool: str = "",
    chat: str = "",
    thread: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {"id": identifier, "type": kind, "ts": int(now if now is not None else time.time()),
                             "profile": profile_name(profile)}
    if routing_id(session_id):
        event["session_id"] = session_id
    if routing_id(session_key):
        event["session_key"] = session_key
    if platform:
        event["platform"] = clean(platform, 32)
    # Where in Telegram it was said (the chat's number, and the topic's in a forum), so a tap opens that conversation.
    # Numbers only: no name, no word of the conversation.
    if platform == "telegram" and isinstance(chat, str) and _CHAT.match(chat):
        event["chat"] = chat
        if isinstance(thread, str) and _THREAD.match(thread):
            event["thread"] = thread
    if title:
        event["title"] = shown_clean(title, 120)
    if body:
        event["body"] = shown_clean(body, 500)
    if command and kind == APPROVAL:
        # Whole or not at all on the lock screen's side: the app shows a cut command as cut and only lets it be denied.
        event["command"] = command[:600]
        if len(command) > 600:
            event["command_cut"] = True
    if request_id:
        event["request_id"] = clean(request_id, 80)
    if questions and kind == QUESTION:
        event["questions"] = questions[:MAX_QUESTIONS]
    if tool and kind == PROGRESS:
        event["tool"] = clean(tool, 64)
    return fit(event)


def fit(event: dict[str, Any], maximum: int = MAX_EVENT_BYTES) -> dict[str, Any]:
    """[event] cut down until it fits one FCM message: choices first, then the command, then the body."""
    def size() -> int:
        return len(json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))

    if size() <= maximum:
        return event
    event = dict(event)
    if "questions" in event:
        event["questions"] = [{k: v for k, v in q.items() if k != "choices"} for q in event["questions"]][:2]
        event["questions_cut"] = True
    if size() > maximum and "command" in event:
        event["command"] = event["command"][:200]
        event["command_cut"] = True
    while size() > maximum and len(event.get("body", "")) > 40:
        event["body"] = event["body"][: len(event["body"]) // 2]
    if size() > maximum:
        for key in ("questions", "command", "body", "title"):
            event.pop(key, None)
    return event


def wants(kinds: Any, event: dict[str, Any]) -> bool:
    """Whether a device whose settings are [kinds] receives [event]. Requests, their end and tests always go."""
    kind = event.get("type")
    if kind in (APPROVAL, QUESTION, RESOLVED, TEST, CALL):
        return True  # a call only exists when the user turned calls on: that was the choice
    chosen = set(kinds) if isinstance(kinds, (list, tuple, set)) else set(OPTIONAL_KINDS)
    if kind == REPLY:
        if event.get("platform") == "telegram":
            return "reply_telegram" in chosen
        return "reply" in chosen
    if kind == FAILED:
        return "failure" in chosen
    if kind in (PROGRESS, ENDED):
        return "progress" in chosen
    return False


# ---------------------------------------------------------------- clarify questions


def flatten_choice_labels(choices: Any) -> list[str]:
    if not isinstance(choices, list):
        return []
    labels: list[str] = []
    for choice in choices:
        label = ""
        if isinstance(choice, str):
            label = choice.strip()
        elif isinstance(choice, dict):
            for key in ("label", "description", "text", "title"):
                value = choice.get(key)
                if isinstance(value, str) and value.strip():
                    label = value.strip()
                    break
        if label:
            labels.append(shown_clean(label, 80))
    return labels[:MAX_CHOICES]


def normalize_questions(args: Any) -> list[dict[str, Any]]:
    """Every shape the clarify tool is called with, as one list. Question ids are minted the way Hermes' own
    normalizer mints them (`q<index>` from the raw position), so they name the same questions as the gateway's."""
    if not isinstance(args, dict):
        return []
    raw = args.get("questions")
    if isinstance(raw, list) and raw:
        out: list[dict[str, Any]] = []
        for index, item in enumerate(raw[:MAX_QUESTIONS]):
            if isinstance(item, str):
                item = {"question": item}
            if not isinstance(item, dict):
                return []
            text = str(item.get("question") or "").strip()
            if not text:
                return []
            entry: dict[str, Any] = {"qid": f"q{index}", "question": shown_clean(text, 300)}
            labels = flatten_choice_labels(item.get("choices"))
            if labels:
                entry["choices"] = labels
            out.append(entry)
        return out
    for key in ("question", "prompt", "message"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            entry = {"qid": "q0", "question": shown_clean(value, 300)}
            labels = flatten_choice_labels(args.get("choices"))
            if labels:
                entry["choices"] = labels
            return [entry]
    return []


# ---------------------------------------------------------------- text


def is_silent_response(value: Any) -> bool:
    """True for Hermes' no-notification sentinel: a reply that is "[SILENT]", or that starts with it, as a scheduled job
    answers when it has nothing to tell ("[SILENT] No changes"). Nobody is notified of such a reply."""
    return isinstance(value, str) and value.lstrip().casefold().startswith("[silent]")


_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_QUOTE = re.compile(r"^\s{0,3}>\s?")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(?:\[[ xX]\]\s+)?")
_RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*\Z")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*\Z")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_STRONG = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_EMPHASIS = re.compile(r"(?<![\w*])\*(?=\S)([^*]+?)(?<=\S)\*(?![\w*])")
_UNDERSCORE_EMPHASIS = re.compile(r"(?<![\w_])_(?=\S)([^_]+?)(?<=\S)_(?![\w_])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
# A file Hermes attached: `MEDIA:/path/to/file.png`, quoted or not. A push shows its name, never the host's path.
_MEDIA_TAG = re.compile(r"""[`"']?MEDIA:[^\S\n]*(?P<path>`[^`\n]+`|"[^"\n]+"|'[^'\n]+'|(?:~/|/|[A-Za-z]:[/\\])[^\s`"'<>]+)[`"']?""")
_MEDIA_DIRECTIVE = re.compile(r"\[\[(?:audio_as_voice|as_document)\]\]")
_CACHE_NAME = re.compile(r"^(?:img|audio|video|doc)_[0-9a-f]{12}(?:_(?P<name>.+))?$", re.IGNORECASE)
_HASH_NAME = re.compile(r"^[0-9a-f-]{16,}$", re.IGNORECASE)


def _attachment(match: re.Match) -> str:
    path = match.group("path").strip().strip("`\"'").rstrip(".,;:!?)]}*_~")
    name = re.split(r"[/\\]", path.rstrip("/\\"))[-1]
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    cached = _CACHE_NAME.match(name) or _CACHE_NAME.match(stem)
    if cached:
        name = cached.group("name") or ""
    if not name or (not cached and _HASH_NAME.match(stem)):
        name = ext.upper()
    return f"\U0001F4CE {name}".rstrip()


def media_free(value: str) -> str:
    return _MEDIA_TAG.sub(_attachment, _MEDIA_DIRECTIVE.sub("", value))


def plain_text(value: Any) -> str:
    """A Markdown reply as a notification shows it: the words without the markup, and an attached file as its
    name. Code inside a fence stays: it is the content."""
    if not isinstance(value, str):
        return ""
    lines: list[str] = []
    fenced = False
    for line in value.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            lines.append(line)
            continue
        line = media_free(_QUOTE.sub("", line))
        if _RULE.match(line) or _TABLE_RULE.match(line):
            continue
        line = _HEADING.sub("", line)
        line = _BULLET.sub(r"\1• ", line)
        if line.lstrip().startswith("|"):
            line = " · ".join(cell.strip() for cell in line.strip().strip("|").split("|") if cell.strip())
        line = _LINK.sub(r"\1", _IMAGE.sub(r"\1", line))
        line = _STRONG.sub(r"\2", line)
        line = _STRIKE.sub(r"\1", _UNDERSCORE_EMPHASIS.sub(r"\1", _EMPHASIS.sub(r"\1", line)))
        lines.append(line.replace("`", ""))
    return "\n".join(lines).strip()


def shown_clean(value: Any, maximum: int) -> str:
    """One line, bounded, with any MEDIA tag read as its attachment."""
    return clean(media_free(value) if isinstance(value, str) else value, maximum)


def clean(value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:maximum]
