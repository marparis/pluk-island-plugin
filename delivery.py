"""From an event to the paired devices: sealed for each one, sent through Firebase.

A hook never waits for the network: it puts the event in a small queue that
one daemon thread empties (the pattern of hermes-conduit-notifier's client.py;
MIT, (c) 2026 Hermes Conduit contributors). The thread sleeps on the queue, so
an idle Hermes does no work for this plugin.
"""

from __future__ import annotations

import atexit
import logging
import os
import queue
import threading
import time
from typing import Any, Callable

from . import events, fcm, sealing
from .store import Store, service_account

logger = logging.getLogger(__name__)

QUEUE_SIZE = 200
RETRY_PAUSES_S = (2.0, 8.0)
# How long Firebase keeps a message for a phone that is off. A request is worth nothing once Hermes gave up on it.
TTL_S = {events.APPROVAL: 3600, events.QUESTION: 3600, events.RESOLVED: 3600, events.TEST: 300, events.PROGRESS: 120, events.ENDED: 600, events.CALL: 120}
DEFAULT_TTL_S = 24 * 3600

_queue: "queue.Queue[dict[str, Any] | None]" = queue.Queue(maxsize=QUEUE_SIZE)
_lock = threading.Lock()
_worker: threading.Thread | None = None
_worker_pid = 0
_seen: dict[str, None] = {}
_sender = fcm.Sender()


# What a muted chat no longer pushes; an approval, a question, their end and a call still go.
MUTED_KINDS = frozenset({events.REPLY, events.PROGRESS, events.ENDED, events.FAILED})


def deliver(event: dict[str, Any], *, store: Store | None = None, sender: fcm.Sender | None = None,
            only_device: str | None = None, pause: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Sends [event] to every paired device that wants it, now. Returns counts and the last error's words."""
    store = store or Store()
    sender = sender or _sender
    result: dict[str, Any] = {"sent": 0, "gone": 0, "failed": 0, "skipped": 0, "error": ""}
    devices = store.devices()
    if only_device is not None:
        devices = {k: v for k, v in devices.items() if k == only_device}
    if not devices:
        return result
    # a muted chat: no reply, progress or failure on the phone (what waits for the user still goes)
    if event.get("type") in MUTED_KINDS and event.get("session_id") and store.is_muted(str(event.get("profile") or ""), str(event.get("session_id"))):
        result["skipped"] = len(devices)
        return result
    if not sealing.available():
        result.update(failed=len(devices), error="the cryptography library is missing")
        return result
    account = service_account()
    if account is None:
        result.update(failed=len(devices), error="service-account.json is missing or is not a service account key")
        store.note_delivery(False, result["error"])
        return result
    high = event.get("type") not in events.QUIET_KINDS
    ttl = TTL_S.get(str(event.get("type")), DEFAULT_TTL_S)
    for device_id, device in devices.items():
        if not events.wants(device.get("kinds"), event):
            result["skipped"] += 1
            continue
        try:
            data = sealing.seal(str(device.get("secret") or ""), device_id, event)
        except Exception:  # noqa: BLE001 - a damaged entry must not stop the others
            result["failed"] += 1
            result["error"] = "a device's pairing is damaged; pair it again"
            continue
        outcome, error = _send_with_retries(sender, account, str(device.get("token") or ""), data, high, ttl, pause)
        if outcome == fcm.OK:
            result["sent"] += 1
        elif outcome == fcm.GONE:
            result["gone"] += 1
            store.forget_token(str(device.get("token") or ""))
        else:
            result["failed"] += 1
            result["error"] = error
    if result["sent"] or result["failed"]:
        store.note_delivery(result["failed"] == 0, result["error"])
    return result


def _send_with_retries(sender: fcm.Sender, account: dict[str, Any], token: str, data: dict[str, str], high: bool, ttl: int,
                       pause: Callable[[float], None]) -> tuple[str, str]:
    for wait in (*RETRY_PAUSES_S, None):
        try:
            return sender.send(account, token, data, high=high, ttl_s=ttl), ""
        except fcm.FcmError as error:
            if not error.retry or wait is None:
                return "failed", str(error)
            pause(wait)
        except Exception as error:  # noqa: BLE001
            return "failed", type(error).__name__
    return "failed", "gave up"


def enqueue(event: dict[str, Any]) -> bool:
    """Queues [event] for the background thread. False when it was already queued, or the queue is full."""
    identifier = str(event.get("id") or "")
    with _lock:
        if identifier and identifier in _seen:
            return False
        if identifier:
            _seen[identifier] = None
            while len(_seen) > 500:
                del _seen[next(iter(_seen))]
        _start_locked()
    try:
        _queue.put_nowait(event)
        return True
    except queue.Full:
        logger.warning("pluk_notify: the delivery queue is full; a notification was dropped")
        return False


def _start_locked() -> None:
    global _worker, _worker_pid
    if _worker is not None and _worker.is_alive() and _worker_pid == os.getpid():
        return
    _worker_pid = os.getpid()
    _worker = threading.Thread(target=_run, name="pluk-notify-delivery", daemon=True)
    _worker.start()


def _run() -> None:
    while True:
        event = _queue.get()
        if event is None:
            return
        try:
            result = deliver(event)
            if result["failed"]:
                logger.warning("pluk_notify: a notification was not delivered: %s", result["error"])
        except Exception:  # noqa: BLE001 - the thread must outlive any one event
            logger.warning("pluk_notify: delivery failed", exc_info=True)


def stop(timeout: float = 2.0) -> None:
    """Lets the thread finish what is queued, briefly, when the plugin unloads or Hermes exits."""
    global _worker
    with _lock:
        worker, _worker = _worker, None
    if worker is None or not worker.is_alive():
        return
    try:
        _queue.put_nowait(None)
    except queue.Full:
        return
    worker.join(timeout)


atexit.register(stop)
