"""Shared BSN-unit acronym → full-Thai helper.

Single source of truth = data/reference/bsn_unit_full.json. Used by
models.import_weekly (auto-normalise every imported ledger unit so it
matches the already-normalised unit_conversions → far fewer pending)
and by the /unit-conversions page (learn a new acronym Put types in).

Keep this dependency-free (no Flask / no DB) so scripts can import it too.
"""
from __future__ import annotations

import json
import os
import threading

_MAP_PATH = os.path.join(os.path.dirname(__file__), "..", "data",
                         "reference", "bsn_unit_full.json")
_lock = threading.Lock()


def map_path() -> str:
    return os.path.abspath(_MAP_PATH)


def _load() -> dict:
    with open(map_path(), encoding="utf-8") as f:
        return json.load(f)


def load_unit_map() -> dict:
    """acronym → full Thai (identity entries kept; callers may filter)."""
    return _load().get("map", {})


def full_units() -> set:
    """The set of canonical full-Thai unit names (map values)."""
    return set(load_unit_map().values())


def normalize_unit(unit):
    """Return the full-Thai form if `unit` is a known acronym, else `unit`
    unchanged (unknown acronyms are left as-is so they surface as pending
    with a suggestion)."""
    if not unit:
        return unit
    return load_unit_map().get(unit, unit)


def is_known(unit) -> bool:
    """True if `unit` is already a canonical full unit or a mapped acronym."""
    m = load_unit_map()
    return unit in m or unit in set(m.values())


def add_acronym(acronym: str, full: str) -> None:
    """Persist a newly-learned acronym→full mapping to the JSON
    (idempotent; thread-safe enough for the single-writer Flask app)."""
    acronym = (acronym or "").strip()
    full = (full or "").strip()
    if not acronym or not full or acronym == full:
        return
    with _lock:
        data = _load()
        data.setdefault("map", {})
        if data["map"].get(acronym) == full:
            return
        data["map"][acronym] = full
        note = data.get("_doc", "")
        if "learned via /unit-conversions" not in note:
            data["_doc"] = note + " | learned via /unit-conversions UI."
        tmp = map_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, map_path())
