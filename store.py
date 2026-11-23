"""What pluk-notify keeps on disk: the paired devices and what waits for the user.

One folder for the whole Hermes installation, `<hermes root>/pluk-notify/`,
whichever profile's process writes: every gateway (one per profile) and the
Dashboard read and write the same files, under one lock. The idea of a
waiting list kept by the hooks and read by a Dashboard route comes from
hermes-conduit-notifier (MIT, (c) 2026 Hermes Conduit contributors;
calls_store.py and dashboard/api/waiting.py).

The folder also holds the two files the user drops in:
  service-account.json   the Firebase service account key (secret; sends the pushes)
  google-services.json   the Firebase Android app's settings (public; handed to the app)
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from . import events, sealing

FOLDER = "pluk-notify"
SERVICE_ACCOUNT = "service-account.json"
GOOGLE_SERVICES = "google-services.json"
DEVICES = "devices.json"
WAITING = "waiting.json"
STATUS = "status.json"
SETTINGS = "settings.json"
MUTES = "mutes.json"
# The last calls Hermes made, for the PC to ring too (GET /calls): only these few, never the chats' words.
CALLS = "calls.json"
MAX_CALLS = 20
MAX_MUTES = 500
ANDROID_PACKAGE = "com.pluk.island"

MAX_DEVICES = 8
MAX_WAITING = 100
# A safety net only: a wait leaves the list when Hermes says it ended, or when the process that held it is gone.
WAITING_MAX_AGE_S = 24 * 3600.0
_TOKEN = re.compile(r"^[A-Za-z0-9_:\-.]{20,4096}\Z")


class StoreError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def hermes_root() -> Path:
    """The Hermes installation's root home (not a profile's), so every profile shares one pairing."""
    override = os.environ.get("PLUK_NOTIFY_HOME", "").strip()
    if override:
        return Path(override)
    try:
        from hermes_constants import get_default_hermes_root

        return Path(get_default_hermes_root())
    except Exception:  # noqa: BLE001 - an older Hermes: one home
        pass
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:  # noqa: BLE001 - no Hermes runtime (tests)
        return Path.home() / ".hermes"


def data_dir() -> Path:
    return hermes_root() / FOLDER


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def service_account() -> dict[str, Any] | None:
    """The Firebase service account key, or None when it is missing or not one."""
    value = _read_json(data_dir() / SERVICE_ACCOUNT)
    if not isinstance(value, dict):
        return None
    needed = ("project_id", "private_key", "client_email")
    if value.get("type") != "service_account" or not all(isinstance(value.get(k), str) and value[k] for k in needed):
        return None
    return value


def firebase_config() -> dict[str, str] | None:
    """The public settings the app starts Firebase with, read from google-services.json. None of them is a secret:
    they are in every APK built the usual way. Here they stay on the server and reach only the signed-in app."""
    value = _read_json(data_dir() / GOOGLE_SERVICES)
    if not isinstance(value, dict):
        return None
    info = value.get("project_info") if isinstance(value.get("project_info"), dict) else {}
    clients = [c for c in value.get("client", []) if isinstance(c, dict)] if isinstance(value.get("client"), list) else []

    def package(client: dict) -> str:
        client_info = client.get("client_info") if isinstance(client.get("client_info"), dict) else {}
        android = client_info.get("android_client_info") if isinstance(client_info.get("android_client_info"), dict) else {}
        return str(android.get("package_name") or "")

    client = next((c for c in clients if package(c) == ANDROID_PACKAGE), None)
    if client is None:
        return None
    keys = [k for k in client.get("api_key", []) if isinstance(k, dict)] if isinstance(client.get("api_key"), list) else []
    config = {
        "project_id": str(info.get("project_id") or ""),
        "sender_id": str(info.get("project_number") or ""),
        "application_id": str((client.get("client_info") or {}).get("mobilesdk_app_id") or ""),
        "api_key": str(keys[0].get("current_key") or "") if keys else "",
    }
    return config if all(config.values()) else None


@contextlib.contextmanager
def _exclusive(folder: Path) -> Iterator[None]:
    """One writer at a time across every Hermes process."""
    folder.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(folder, 0o700)
    handle = open(folder / ".lock", "a+b")
    try:
        if os.name == "nt":  # the development PC's tests; Hermes itself runs on Linux
            import msvcrt

            handle.seek(0)
            deadline = time.monotonic() + 10
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.02)
            try:
                yield
            finally:
                handle.seek(0)
                with contextlib.suppress(OSError):
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _write_json(path: Path, value: Any) -> None:
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"), ensure_ascii=False)
        with contextlib.suppress(OSError):
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _alive(pid: Any) -> bool:
    """Whether the process that noted a wait still runs. Unknown counts as alive."""
    if not isinstance(pid, int) or pid <= 0 or os.name == "nt":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def telegram_keys(value: Any) -> dict[str, Any] | None:
    """Telegram's application keys as kept ({"id": 12345, "hash": 32 hex characters}), or None when they are not that."""
    if not isinstance(value, dict):
        return None
    api_id, api_hash = value.get("id"), value.get("hash")
    if isinstance(api_id, bool) or not isinstance(api_id, int) or not 0 < api_id < 2**31:
        return None
    if not isinstance(api_hash, str) or len(api_hash) != 32 or any(c not in "0123456789abcdef" for c in api_hash.lower()):
        return None
    return {"id": api_id, "hash": api_hash.lower()}


class Store:
    def __init__(self, folder: Path | None = None):
        self.folder = Path(folder) if folder is not None else data_dir()

    def _update(self, name: str, key: str, change: Callable[[dict[str, Any]], Any]) -> Any:
        with _exclusive(self.folder):
            path = self.folder / name
            value = _read_json(path)
            table = value.get(key) if isinstance(value, dict) and isinstance(value.get(key), dict) else {}
            before = json.dumps(table, sort_keys=True)
            result = change(table)
            if json.dumps(table, sort_keys=True) != before:
                _write_json(path, {key: table})
            return result

    def _read(self, name: str, key: str) -> dict[str, Any]:
        value = _read_json(self.folder / name)
        table = value.get(key) if isinstance(value, dict) else None
        return table if isinstance(table, dict) else {}

    # ------------------------------------------------------------ devices

    def devices(self) -> dict[str, dict[str, Any]]:
        return {k: v for k, v in self._read(DEVICES, "devices").items() if isinstance(v, dict)}

    def pair(self, token: Any, name: Any = "", kinds: Any = None, device_id: Any = None) -> tuple[str, str | None]:
        """Adds a device, or brings one up to date (its FCM token changes now and then). Returns its id, and its
        secret when it is new: a device that already has one never gets it sent again."""
        if not isinstance(token, str) or not _TOKEN.match(token):
            raise StoreError("bad_token", "That is not a Firebase registration token")
        chosen = [k for k in kinds if k in events.OPTIONAL_KINDS] if isinstance(kinds, list) else list(events.OPTIONAL_KINDS)
        label = events.clean(name, 60) or "Android"

        def change(devices: dict[str, Any]) -> tuple[str, str | None]:
            known = devices.get(device_id) if isinstance(device_id, str) else None
            if isinstance(known, dict):
                known.update({"token": token, "name": label, "kinds": chosen, "seen_at": int(time.time())})
                for other in [k for k, v in devices.items() if k != device_id and isinstance(v, dict) and v.get("token") == token]:
                    del devices[other]
                return device_id, None
            # The same phone pairing again (its app data was cleared): its old entry goes.
            for other in [k for k, v in devices.items() if isinstance(v, dict) and v.get("token") == token]:
                del devices[other]
            if len(devices) >= MAX_DEVICES:
                raise StoreError("too_many", "Too many devices are paired; remove one with `hermes pluk-notify forget`", 409)
            new_id, secret = sealing.new_device_id(), sealing.new_secret()
            now = int(time.time())
            devices[new_id] = {"token": token, "secret": secret, "name": label, "kinds": chosen, "paired_at": now, "seen_at": now}
            return new_id, secret

        return self._update(DEVICES, "devices", change)

    def unpair(self, device_id: Any) -> bool:
        return bool(self._update(DEVICES, "devices", lambda d: d.pop(device_id, None) is not None if isinstance(device_id, str) else False))

    def forget_token(self, token: str) -> None:
        """Firebase said this token is gone (the app was uninstalled, or its data cleared)."""
        def change(devices: dict[str, Any]) -> None:
            for key in [k for k, v in devices.items() if isinstance(v, dict) and v.get("token") == token]:
                del devices[key]

        self._update(DEVICES, "devices", change)

    # ------------------------------------------------------------ what waits

    def note_waiting(self, entry: dict[str, Any]) -> None:
        def change(waiting: dict[str, Any]) -> None:
            self._prune(waiting)
            waiting[entry["id"]] = {**entry, "created": time.time(), "pid": os.getpid()}
            while len(waiting) > MAX_WAITING:
                del waiting[min(waiting, key=lambda k: waiting[k].get("created", 0))]

        self._update(WAITING, "waiting", change)

    def link_waiting(self, entry_id: str, human_id: str) -> bool:
        """Notes Hermes' own id for a wait, so its end (which names only that id) finds it."""
        def change(waiting: dict[str, Any]) -> bool:
            entry = waiting.get(entry_id)
            if not isinstance(entry, dict):
                return False
            entry["human_id"] = human_id
            return True

        return bool(self._update(WAITING, "waiting", change))

    def find_waiting(self, *, session_key: str, kind: str, unlinked: bool = False, within: float | None = None) -> str | None:
        """The newest wait of a session, for the two hooks that describe one approval to find each other."""
        now = time.time()
        best: tuple[float, str] | None = None
        for key, entry in self._read(WAITING, "waiting").items():
            if not isinstance(entry, dict) or entry.get("kind") != kind or entry.get("session_key") != session_key:
                continue
            if unlinked and entry.get("human_id"):
                continue
            created = float(entry.get("created") or 0)
            if within is not None and now - created > within:
                continue
            if best is None or created > best[0]:
                best = (created, key)
        return best[1] if best else None

    def resolve_waiting(self, *, entry_id: str = "", human_id: str = "", session_key: str = "", kind: str = "",
                        unlinked: bool = False) -> dict[str, Any] | None:
        """Takes one wait off the list: by its id, by Hermes' id, else the oldest of the session ([unlinked]: only
        one Hermes gave no id of its own for). Returns it."""
        def change(waiting: dict[str, Any]) -> dict[str, Any] | None:
            key = entry_id if entry_id in waiting else ""
            if not key and human_id:
                key = next((k for k, v in waiting.items() if isinstance(v, dict) and v.get("human_id") == human_id), "")
            if not key and session_key and not human_id and not entry_id:
                matches = [k for k, v in waiting.items()
                           if isinstance(v, dict) and v.get("session_key") == session_key and (not kind or v.get("kind") == kind)
                           and not (unlinked and v.get("human_id"))]
                key = min(matches, key=lambda k: waiting[k].get("created", 0)) if matches else ""
            return waiting.pop(key, None) if key else None

        return self._update(WAITING, "waiting", change)

    def get_waiting(self, entry_id: Any) -> dict[str, Any] | None:
        entry = self._read(WAITING, "waiting").get(entry_id) if isinstance(entry_id, str) else None
        return dict(entry) if isinstance(entry, dict) else None

    @staticmethod
    def _prune(waiting: dict[str, Any]) -> None:
        now = time.time()
        for key in [k for k, v in waiting.items()
                    if not isinstance(v, dict) or now - float(v.get("created") or 0) > WAITING_MAX_AGE_S or not _alive(v.get("pid"))]:
            del waiting[key]

    def waiting_list(self) -> list[dict[str, Any]]:
        def change(waiting: dict[str, Any]) -> list[dict[str, Any]]:
            self._prune(waiting)
            rows = sorted(waiting.values(), key=lambda v: v.get("created", 0))
            return [{k: v for k, v in row.items() if k not in ("pid", "human_id")} for row in rows]

        return self._update(WAITING, "waiting", change)

    # ------------------------------------------------------------ settings the user changes from the app

    def settings(self) -> dict[str, Any]:
        value = _read_json(self.folder / SETTINGS)
        value = value if isinstance(value, dict) else {}
        return {"calls": value.get("calls") is True, "last_call_at": float(value.get("last_call_at") or 0),
                "telegram": telegram_keys(value.get("telegram"))}

    def set_telegram(self, keys: Any) -> dict[str, Any]:
        """Keeps the user's Telegram application keys, so each of their devices gets them without typing or scanning."""
        clean = telegram_keys(keys)
        if clean is None:
            raise StoreError("bad_keys", "Telegram's keys are a number (api_id) and 32 hexadecimal characters (api_hash).")
        self._write_settings(telegram=clean)
        return clean

    def set_calls(self, enabled: bool) -> None:
        self._write_settings(calls=bool(enabled))

    def note_call(self, when: float, call: dict[str, Any] | None = None) -> None:
        """A call rang: when (for the gap between two), and what it was (for the PC, which rings it too)."""
        self._write_settings(last_call_at=float(when))
        if call is None:
            return
        with _exclusive(self.folder):
            value = _read_json(self.folder / CALLS)
            rows = [r for r in (value if isinstance(value, list) else []) if isinstance(r, dict)]
            rows.append({"at": float(when), **{k: str(call.get(k) or "")[:300] for k in ("id", "profile", "session_id", "title", "reason")}})
            _write_json(self.folder / CALLS, rows[-MAX_CALLS:])

    def calls_after(self, after: float) -> list[dict[str, Any]]:
        """The calls made after [after] (seconds since 1970), oldest first."""
        value = _read_json(self.folder / CALLS)
        rows = [r for r in (value if isinstance(value, list) else []) if isinstance(r, dict)]
        return [r for r in rows if isinstance(r.get("at"), (int, float)) and r["at"] > after]

    def _write_settings(self, **changes: Any) -> None:
        with _exclusive(self.folder):
            _write_json(self.folder / SETTINGS, {**self.settings(), **changes})

    # ------------------------------------------------------------ muted chats
    # A chat muted for a while, or until unmuted (Conduit's muted chats, hermes-conduit-notifier
    # /notifications/muted-chats; MIT, (c) 2026 Hermes Conduit contributors): its replies, progress and failures are
    # not pushed. What waits for the user (an approval, a question) still is: an agent is blocked on it.

    @staticmethod
    def _mute_key(profile: str, session_id: str) -> str:
        return f"{events.profile_name(profile)}\u0000{str(session_id)[:512]}"

    def mutes(self, now: float | None = None) -> dict[str, float]:
        """Every muted chat, "profile\\0session" -> until (0: until unmuted); the ended ones left out."""
        value = _read_json(self.folder / MUTES)
        moment = time.time() if now is None else now
        out: dict[str, float] = {}
        for key, until in (value.items() if isinstance(value, dict) else []):
            if isinstance(key, str) and isinstance(until, (int, float)) and not isinstance(until, bool) and (until == 0 or until > moment):
                out[key] = float(until)
        return out

    def set_mute(self, profile: str, session_id: str, until: float | None) -> None:
        """Mutes a chat until [until] (0: until unmuted), or unmutes it (None)."""
        if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 512:
            raise StoreError("bad_session", "A chat is named by its session id.")
        with _exclusive(self.folder):
            current = self.mutes()
            key = self._mute_key(profile, session_id)
            if until is None:
                current.pop(key, None)
            else:
                current[key] = float(until)
                while len(current) > MAX_MUTES:
                    del current[next(iter(current))]
            _write_json(self.folder / MUTES, current)

    def is_muted(self, profile: str, session_id: str, now: float | None = None) -> bool:
        return bool(session_id) and self._mute_key(profile, session_id) in self.mutes(now)

    # ------------------------------------------------------------ last delivery

    def note_delivery(self, ok: bool, error: str = "") -> None:
        """How the last push went, for `hermes pluk-notify status` and the app's settings. Never a token or a text."""
        with contextlib.suppress(OSError):
            with _exclusive(self.folder):
                _write_json(self.folder / STATUS, {"ok": bool(ok), "error": events.clean(error, 200), "at": int(time.time())})

    def last_delivery(self) -> dict[str, Any] | None:
        value = _read_json(self.folder / STATUS)
        return value if isinstance(value, dict) else None
