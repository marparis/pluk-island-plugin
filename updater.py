"""The plugin updates itself: the app brings the new version, each Hermes service restarts itself when it is idle.

Nothing is fetched from the internet and no token is kept here. Every
release of the Android app carries the plugin it was built with; when the
app sees an older one on the server it hands it over through the Dashboard's
own signed-in connection (POST /api/plugins/pluk_notify/update).

`apply` checks the archive (a plain zip of this folder: safe names, a
higher version, Python that compiles) and swaps the plugin's folder. Every
profile links to that one folder, so they all get it at once.

Python keeps running the code it loaded, so each Hermes service must
restart to use the new one. `start_watch` runs a small thread in each
process: when the version on disk is no longer the one this process loaded,
and no turn runs here, it asks systemd to restart its own service. Only one
service restarts at a time, and only a process that is the service's main
process restarts anything: a `hermes` command run by hand or by cron never
does. Where there is no systemd, nothing restarts by itself and `status`
says so.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import io
import json
import logging
import os
import random
import shutil
import stat
import subprocess
import sys
import threading
import time
import types
import zipfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

NAME = "pluk_notify"
MAX_ARCHIVE = 2 * 1024 * 1024
MAX_UNPACKED = 4 * 1024 * 1024
MAX_FILES = 200
NEEDED = ("plugin.yaml", "__init__.py", "hooks.py", "store.py", "updater.py", "dashboard/plugin_api.py", "dashboard/manifest.json")
# No turn for this long in a process before it may restart.
IDLE_S = 120.0
# A turn that never said it ended (a crash inside Hermes) stops counting after this long.
TURN_FORGOTTEN_S = 2 * 3600.0
# Two services never restart closer than this: one at a time.
RESTART_GAP_S = 90.0
WATCH_EVERY_S = 30.0


class UpdateError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------ versions

def parse_version(text: Any) -> tuple[int, ...] | None:
    if not isinstance(text, str):
        return None
    parts = text.strip().split(".")
    if not 1 <= len(parts) <= 4 or not all(p.isdigit() and len(p) <= 6 for p in parts):
        return None
    return tuple(int(p) for p in parts)


def _yaml_value(text: str, wanted: str) -> str:
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == wanted:
            return value.strip().strip("\"'")
    return ""


def plugin_dir() -> Path:
    """The one real folder of the plugin (a profile reaches it through a link)."""
    return Path(os.path.realpath(os.path.dirname(os.path.abspath(__file__))))


def disk_version(folder: Path | None = None) -> str:
    try:
        return _yaml_value(((folder or plugin_dir()) / "plugin.yaml").read_text(encoding="utf-8"), "version") or "0"
    except OSError:
        return "0"


# ------------------------------------------------------------ what one process shares

def _shared() -> Any:
    """Hermes loads the plugin as a package and, in the Dashboard, its routes load these modules a second time under
    another name. Both copies must see the same turns and run one watch: what they share lives in one place."""
    module = sys.modules.get("_pluk_notify_shared")
    if module is None:
        module = types.ModuleType("_pluk_notify_shared")
        module.lock = threading.Lock()  # type: ignore[attr-defined]
        module.loaded = disk_version()  # type: ignore[attr-defined]
        module.last_activity = time.monotonic()  # type: ignore[attr-defined]
        module.turns = {}  # type: ignore[attr-defined]
        module.watching = False  # type: ignore[attr-defined]
        module = sys.modules.setdefault("_pluk_notify_shared", module)
    return module


_shared()  # at import: the version on disk now is the one this process runs


def loaded_version() -> str:
    """The version this process runs (the one on disk when it started)."""
    return _shared().loaded


def note_hook(name: str, session_id: Any = None) -> None:
    """Called for every hook Hermes runs: a turn is going on in this process. Never raises."""
    try:
        shared = _shared()
        now = time.monotonic()
        with shared.lock:
            shared.last_activity = now
            if isinstance(session_id, str) and session_id:
                if name == "on_session_end":
                    shared.turns.pop(session_id, None)
                else:
                    shared.turns[session_id] = now
                    while len(shared.turns) > 500:
                        del shared.turns[next(iter(shared.turns))]
    except Exception:  # noqa: BLE001
        pass


def idle(now: float | None = None) -> bool:
    shared = _shared()
    now = time.monotonic() if now is None else now
    with shared.lock:
        for session in [s for s, seen in shared.turns.items() if now - seen > TURN_FORGOTTEN_S]:
            del shared.turns[session]
        return not shared.turns and now - shared.last_activity >= IDLE_S


# ------------------------------------------------------------ applying an update

def _members(archive: bytes) -> dict[str, bytes]:
    if len(archive) > MAX_ARCHIVE:
        raise UpdateError("The update is too large", 413)
    try:
        bundle = zipfile.ZipFile(io.BytesIO(archive))
    except (zipfile.BadZipFile, ValueError):
        raise UpdateError("The update is not a zip archive") from None
    files: dict[str, bytes] = {}
    total = 0
    with bundle:
        infos = bundle.infolist()
        if len(infos) > MAX_FILES:
            raise UpdateError("The update has too many files")
        for info in infos:
            name = info.filename
            if name.endswith("/"):
                continue
            parts = name.split("/")
            if (not name or name.startswith("/") or "\\" in name or ":" in name or "\x00" in name
                    or any(p in ("", ".", "..") for p in parts) or "__pycache__" in parts):
                raise UpdateError("The update holds a file with an unsafe name")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise UpdateError("The update holds a link")
            total += info.file_size
            if total > MAX_UNPACKED:
                raise UpdateError("The update is too large", 413)
            data = bundle.read(info)
            if len(data) != info.file_size:
                raise UpdateError("The update is damaged")
            files[name] = data
    return files


def check(archive: bytes, current: str) -> tuple[str, dict[str, bytes]]:
    """The version an archive brings and its files, or UpdateError: it must be this plugin, whole, newer than
    [current], and every Python file in it must compile (a broken plugin could not repair itself)."""
    files = _members(archive)
    for needed in NEEDED:
        if needed not in files:
            raise UpdateError(f"The update is not a whole plugin ({needed} is missing)")
    try:
        manifest = files["plugin.yaml"].decode("utf-8")
    except UnicodeDecodeError:
        raise UpdateError("The update's plugin.yaml is not text") from None
    if _yaml_value(manifest, "name") != NAME:
        raise UpdateError("The update is not pluk_notify")
    version = _yaml_value(manifest, "version")
    new, old = parse_version(version), parse_version(current)
    if new is None:
        raise UpdateError("The update has no version")
    if old is not None and new <= old:
        raise UpdateError(f"The server already has {current}", 409)
    for name, data in files.items():
        if name.endswith(".py"):
            try:
                compile(data.decode("utf-8"), name, "exec", dont_inherit=True)
            except (SyntaxError, ValueError, UnicodeDecodeError):
                raise UpdateError(f"The update's {name} is not valid Python") from None
        elif name.endswith(".json"):
            try:
                json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise UpdateError(f"The update's {name} is not valid JSON") from None
    return version, files


def _move(source: Path, destination: Path) -> None:
    try:
        os.rename(source, destination)
    except OSError:  # another disk
        shutil.move(str(source), str(destination))


def apply(archive: bytes, *, target: Path | None = None, work: Path | None = None) -> str:
    """Replaces the plugin's folder with the archive's files and returns the new version. The folder it replaces is
    kept aside (in the data folder, never next to the plugin: Hermes would load it as a second plugin)."""
    from . import store

    target = target or plugin_dir()
    work = work or (store.data_dir() / "update")
    with store._exclusive(work):  # noqa: SLF001 - one update at a time, across every process
        version, files = check(archive, disk_version(target))
        fresh, previous = work / "new", work / "previous"
        shutil.rmtree(fresh, ignore_errors=True)
        for name, data in files.items():
            path = fresh / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            with contextlib.suppress(OSError):
                os.chmod(path, 0o644)
        shutil.rmtree(previous, ignore_errors=True)
        had = target.exists()
        if had:
            _move(target, previous)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _move(fresh, target)
        except OSError:
            if had and not target.exists():
                _move(previous, target)
            raise UpdateError("The server could not write the update", 500) from None
    logger.info("pluk_notify: updated on disk to %s; each service restarts when it is idle", version)
    return version


def decode(text: Any) -> bytes:
    if not isinstance(text, str) or not text:
        raise UpdateError("`archive` is missing")
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise UpdateError("`archive` is not base64") from None


# ------------------------------------------------------------ restarting, each service by itself

def _systemctl(*args: str) -> str | None:
    try:
        done = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def own_unit() -> str | None:
    """The systemd service this process is the main process of, or None: not under systemd, or a command somebody
    (or cron, or an agent) ran inside a service that is not its own."""
    if os.name != "posix" or os.environ.get("PLUK_NOTIFY_NO_RESTART") or not shutil.which("systemctl"):
        return None
    try:
        lines = Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    unit = next((part for line in lines for part in reversed(line.split("/")) if part.endswith(".service")), None)
    if not unit or unit.startswith("user@"):
        return None
    main = _systemctl("show", "-p", "MainPID", "--value", unit)
    if not main or not main.isdigit() or int(main) not in (os.getpid(), os.getppid()):
        return None
    return unit


def _my_turn(now: float) -> bool:
    """True, and noted, when no other service restarted in the last moments."""
    from . import store

    folder = store.data_dir()
    path = folder / "restart.json"
    with store._exclusive(folder):  # noqa: SLF001
        value = store._read_json(path)  # noqa: SLF001
        last = float(value.get("at") or 0) if isinstance(value, dict) else 0.0
        if 0 <= now - last < RESTART_GAP_S:
            return False
        store._write_json(path, {"at": now, "pid": os.getpid()})  # noqa: SLF001
    return True


def restart_if_due(unit: str) -> bool:
    if disk_version() == loaded_version() or not idle() or not _my_turn(time.time()):
        return False
    logger.info("pluk_notify: %s restarts to load version %s", unit, disk_version())
    return _systemctl("restart", "--no-block", unit) is not None


def start_watch() -> bool:
    """Starts this process's watch, once. False when this process restarts nothing by itself."""
    shared = _shared()
    with shared.lock:
        if shared.watching:
            return True
    unit = own_unit()
    if unit is None:
        return False
    with shared.lock:
        if shared.watching:
            return True
        shared.watching = True

    def watch() -> None:
        while True:
            time.sleep(WATCH_EVERY_S + random.uniform(0, 10))
            try:
                if restart_if_due(unit):
                    return
            except Exception:  # noqa: BLE001 - the watch must outlive a passing trouble
                logger.debug("pluk_notify: the update watch failed once", exc_info=True)

    threading.Thread(target=watch, name="pluk-notify-update", daemon=True).start()
    return True


def state() -> dict[str, Any]:
    """For `status`: the version on disk, the one this process runs, and whether services restart by themselves."""
    return {"version": disk_version(), "running": loaded_version(), "auto_restart": bool(_shared().watching) or own_unit() is not None}
