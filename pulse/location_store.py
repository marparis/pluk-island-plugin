"""PLUK stand-in for Hermes Conduit's location store: no phone shares where the user is with this plugin.

Conduit's Pulse reads the location its iPhone app shares (pulse_places, pulse_spots). PLUK Island never reads the
phone's location in the background (the battery comes first), so this store is always empty and sharing always
paused: "Where you are" and "walking past" stay off, as on a Conduit host whose phone shares nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

FILE = "pluk-no-location.json"


class LocationStore:
    def __init__(self, home: Any) -> None:
        self.path = Path(home) / FILE

    def exists(self) -> bool:
        return False

    def snapshot(self, upgrade: bool = False) -> dict[str, Any]:
        return {"places": {}, "sharing": {}}


def sharing_paused(sharing: Any, now: float) -> bool:
    return True
