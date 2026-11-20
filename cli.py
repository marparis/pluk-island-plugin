"""`hermes pluk-notify`: what is set up, a test push, the paired devices. It never prints a key or a token."""

from __future__ import annotations

import argparse
import time
from typing import Any

from . import delivery, events, sealing, store


def register_cli(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="pluk_notify_action")
    commands.add_parser("status", help="Show what is set up, without revealing any secret")
    commands.add_parser("test", help="Send a test notification to every paired device")
    commands.add_parser("devices", help="List the paired devices")
    forget = commands.add_parser("forget", help="Remove a paired device")
    forget.add_argument("device", help="The device's id, or the start of it (see `devices`)")
    commands.add_parser("waiting", help="List what waits for the user right now")


def status() -> dict[str, Any]:
    """Shared with the Dashboard route: what the app's settings and the command both show."""
    current = store.Store()
    return {
        "version": _version(),
        "crypto": sealing.available(),
        "service_account": store.service_account() is not None,
        "firebase_config": store.firebase_config() is not None,
        "devices": len(current.devices()),
        "last_delivery": current.last_delivery(),
        "folder": str(store.data_dir()),
    }


def _version() -> str:
    import os

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugin.yaml")
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.partition(":")
                if key.strip() == "version":
                    return value.strip().strip("\"'")
    except OSError:
        pass
    return "0"


def test_event() -> dict[str, Any]:
    return events.build(events.TEST, identifier=events.event_id("test"), title="PLUK Island", body="Notifications work.")


def dispatch(args: argparse.Namespace) -> int:
    action = getattr(args, "pluk_notify_action", None)
    current = store.Store()
    if action == "test":
        result = delivery.deliver(test_event())
        print(f"sent: {result['sent']}  removed (no longer registered): {result['gone']}  failed: {result['failed']}")
        if result["error"]:
            print(f"last error: {result['error']}")
        if not (result["sent"] or result["gone"] or result["failed"]):
            print("No device is paired yet: open PLUK Island on the phone, Settings > Notifications.")
        return 0 if result["failed"] == 0 else 1
    if action == "devices":
        devices = current.devices()
        if not devices:
            print("No device is paired.")
        for device_id, device in devices.items():
            when = time.strftime("%Y-%m-%d", time.localtime(float(device.get("paired_at") or 0)))
            kinds = ", ".join(device.get("kinds") or []) or "requests only"
            print(f"{device_id[:10]}  {device.get('name', '')}  paired {when}  also receives: {kinds}")
        return 0
    if action == "forget":
        matches = [key for key in current.devices() if key.startswith(args.device)]
        if len(matches) != 1:
            print("No single device starts with that id." if matches else "No device has that id.")
            return 1
        current.unpair(matches[0])
        print("Removed.")
        return 0
    if action == "waiting":
        rows = current.waiting_list()
        if not rows:
            print("Nothing waits.")
        for row in rows:
            age = int(time.time() - float(row.get("created") or 0))
            print(f"{row.get('kind')}  {row.get('profile')}  {row.get('platform') or '?'}  {age}s  {row.get('description', '')[:80]}")
        return 0
    info = status()
    print(f"pluk_notify {info['version']}")
    print("updates: brought by the app; each service restarts itself when it is idle (see README)")
    print(f"folder: {info['folder']}")
    print(f"encryption library: {'ok' if info['crypto'] else 'MISSING (cryptography)'}")
    print(f"service-account.json: {'ok' if info['service_account'] else 'MISSING or not a service account key'}")
    print(f"google-services.json: {'ok' if info['firebase_config'] else 'MISSING, or it has no Android app com.pluk.island'}")
    print(f"paired devices: {info['devices']}")
    last = info["last_delivery"]
    if last:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(last.get("at") or 0)))
        print(f"last delivery: {'ok' if last.get('ok') else 'FAILED: ' + str(last.get('error', ''))} ({when})")
    return 0
