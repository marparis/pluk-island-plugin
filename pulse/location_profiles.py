"""PLUK stand-in for Hermes Conduit's location_profiles: no profile shares a location here (see location_store.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


def store_for(home: Any, make: Callable[[Any], Any]) -> tuple[Any, None]:
    return make(home), None


def profile_homes(home: Path) -> list[tuple[str, Path]]:
    return []
