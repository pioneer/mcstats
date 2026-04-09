"""Per-ROI neighbour cache stored as JSON files.

Cache directory layout::

    .cache/
      Kyiv_SoldSlobidka_R1.json
      Kyiv_SomeOther_R2.json

Each file contains a list of neighbour dicts (as returned by MeshCore
get_contacts) that were confirmed reachable through the ROI.
"""
from __future__ import annotations

import json
import pathlib
from datetime import datetime, timezone
from typing import Any

DEFAULT_CACHE_DIR = ".cache"


def _cache_path(roi_name: str, cache_dir: str = DEFAULT_CACHE_DIR) -> pathlib.Path:
    safe_name = roi_name.replace("/", "_").replace("\\", "_")
    return pathlib.Path(cache_dir) / f"{safe_name}.json"


def load_neighbours(roi_name: str, cache_dir: str = DEFAULT_CACHE_DIR) -> list[dict[str, Any]] | None:
    """Load cached neighbours for *roi_name*.  Returns None if no cache."""
    p = _cache_path(roi_name, cache_dir)
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    return data.get("neighbours")


def save_neighbours(
    roi_name: str,
    neighbours: list[dict[str, Any]],
    cache_dir: str = DEFAULT_CACHE_DIR,
) -> pathlib.Path:
    """Persist discovered neighbours for *roi_name*.  Returns the file path."""
    p = _cache_path(roi_name, cache_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "roi": roi_name,
        "updated": datetime.now(timezone.utc).isoformat(),
        "neighbours": neighbours,
    }
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return p


def list_cached(cache_dir: str = DEFAULT_CACHE_DIR) -> list[str]:
    """Return ROI names that have a cache file."""
    d = pathlib.Path(cache_dir)
    if not d.is_dir():
        return []
    return [p.stem for p in sorted(d.glob("*.json"))]
