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
import types

_MAP_PATH = os.path.join(os.path.dirname(__file__), "..", "data",
                         "reference", "bsn_unit_full.json")
_lock = threading.Lock()

# (stat_key, mapping) or None. ONE tuple, read in a single load, because two
# separate globals can be read either side of a writer and pair a stale map
# with a fresh key.
_cache = None


def map_path() -> str:
    return os.path.abspath(_MAP_PATH)


def _load() -> dict:
    with open(map_path(), encoding="utf-8") as f:
        return json.load(f)


def _stat_key(path):
    """What makes a cached map still valid. `st_mtime_ns`, not `st_mtime`: a
    value swapped for one of equal length inside the same second changes neither
    the second nor the size — the same shape as the bytecode-cache trap already
    documented in .claude/rules. Measured on this filesystem: 200 tight rewrites
    produced 200 distinct mtime_ns."""
    try:
        st = os.stat(path)
    except OSError:
        return None                      # missing/unreadable → never cache it
    return (path, st.st_mtime_ns, st.st_size)


def invalidate_unit_map_cache() -> None:
    """Drop the cached map. Called on the write path; tests use it to isolate."""
    global _cache
    with _lock:
        _cache = None


def load_unit_map():
    """acronym → full Thai (identity entries kept; callers may filter).

    Cached, because this is called once per ledger row: profiling the Express
    drift comparison on the real dataset (2026-08-25) found 74,905 calls
    re-parsing the same 3KB of JSON for ~2.4s of a ~3.5s run, and the weekly BSN
    import calls it on the same hot path inside a request with a 60s ceiling.

    ⚠ It REVALIDATES on every call (one `os.stat`, measured at 0.10s per 75k).
    Under `gunicorn -w 2` the worker that did not handle an `add_acronym`
    request must still see the new acronym, so a cache that never re-checks
    would be the per-worker-state bug this repo shipped twice (PR #103). The
    result is read-only so no caller can edit the shared copy in place.
    """
    global _cache
    key = _stat_key(map_path())
    cached = _cache
    if key is not None and cached is not None and cached[0] == key:
        return cached[1]
    mapping = types.MappingProxyType(dict(_load().get("map", {})))
    if key is not None:
        with _lock:
            _cache = (key, mapping)
    return mapping


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
        # Invalidate INSIDE the lock, after the atomic replace.
        # ⚠ Measured: removing these two lines turns NO test red, because
        # os.replace moves st_mtime_ns and the stat key catches it on its own.
        # They are kept for the case the stat key cannot cover — a filesystem
        # whose timestamp resolution is coarser than two writes (a network
        # mount, a volume with 1s granularity), where an in-process write would
        # otherwise be invisible to this process until something else touched
        # the file. Do not read them as the reason add_acronym works.
        global _cache
        _cache = None
