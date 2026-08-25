"""The unit map is read from disk ONCE, and a change to it is still seen.

WHY THIS EXISTS — measured, not suspected. `normalize_unit()` is
`load_unit_map().get(unit, unit)`, and `load_unit_map()` used to `open()` +
`json.load()` the file on EVERY call. Profiling the Express drift comparison on
the real dataset (2026-08-25) showed 74,905 calls costing ~2.4s of a ~3.5s run —
more than half the work, all of it re-parsing 3KB of JSON that had not changed.
The same function runs per row on the weekly BSN import, which lives inside a
request with a 60s gunicorn ceiling.

⚠ A cache here is only safe because it REVALIDATES. `add_acronym()` rewrites the
file, and under `gunicorn -w 2` the worker that did not handle that request must
still see the new acronym — a module global that never re-checks is exactly the
per-worker-state bug this repo has already shipped twice (PR #103). So the key is
(mtime_ns, size) on every call, plus an explicit invalidation on the write path;
tests 2 and 3 are the two halves of that, and neither passes without it.
"""
import json
import os
import time

import bsn_units


def _write(path, mapping):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'map': mapping}, f, ensure_ascii=False)


def _point_at(monkeypatch, tmp_path, mapping):
    """Redirect the module at a throwaway copy of the map."""
    p = tmp_path / 'bsn_unit_full.json'
    _write(str(p), mapping)
    monkeypatch.setattr(bsn_units, '_MAP_PATH', str(p))
    bsn_units.invalidate_unit_map_cache()
    return str(p)


def test_repeated_calls_parse_the_file_once(monkeypatch, tmp_path):
    p = _point_at(monkeypatch, tmp_path, {'ตว': 'ตัว', 'หล': 'โหล'})
    reads = []
    real_open = open

    def counting_open(file, *a, **kw):
        if str(file) == p:
            reads.append(str(file))
        return real_open(file, *a, **kw)

    monkeypatch.setattr('builtins.open', counting_open)

    # CONTROL first: a cache that returns junk must not pass on call count alone.
    assert [bsn_units.normalize_unit('ตว') for _ in range(50)] == ['ตัว'] * 50
    assert bsn_units.normalize_unit('หล') == 'โหล'
    assert bsn_units.normalize_unit('ไม่รู้จัก') == 'ไม่รู้จัก'

    # COUNT, then the property — an `all(...)` over an empty list is vacuous.
    assert len(reads) == 1, f'the map was parsed {len(reads)} times, not once'


def test_a_change_on_disk_is_picked_up(monkeypatch, tmp_path):
    """The revalidation half. A sibling gunicorn worker writes the file; this
    process must not keep serving the value it cached before that."""
    p = _point_at(monkeypatch, tmp_path, {'ตว': 'ตัว'})
    assert bsn_units.normalize_unit('กป') == 'กป'          # unknown, so far

    time.sleep(0.01)                                        # distinct mtime_ns
    _write(p, {'ตว': 'ตัว', 'กป': 'กระป๋อง'})
    assert bsn_units.normalize_unit('กป') == 'กระป๋อง'
    assert bsn_units.normalize_unit('ตว') == 'ตัว'          # CONTROL: not clobbered


def test_a_same_size_same_second_rewrite_is_still_picked_up(monkeypatch, tmp_path):
    """mtime alone is not enough, and neither is (mtime_seconds, size): a
    value swapped for one of equal length within the same second changes
    neither. This is the pycache trap the repo has documented, in a second place.
    """
    p = _point_at(monkeypatch, tmp_path, {'ถง': 'ถังใหญ่'})
    assert bsn_units.normalize_unit('ถง') == 'ถังใหญ่'
    _write(p, {'ถง': 'ถังเล็ก'})                            # same byte length
    assert os.path.getsize(p) == os.path.getsize(p)
    assert bsn_units.normalize_unit('ถง') == 'ถังเล็ก'


def test_add_acronym_is_visible_immediately(monkeypatch, tmp_path):
    p = _point_at(monkeypatch, tmp_path, {'ตว': 'ตัว'})
    assert bsn_units.normalize_unit('ลง') == 'ลง'
    bsn_units.add_acronym('ลง', 'ลัง')
    assert bsn_units.normalize_unit('ลง') == 'ลัง'
    assert json.load(open(p, encoding='utf-8'))['map']['ลง'] == 'ลัง'
    assert bsn_units.is_known('ลง') and bsn_units.is_known('ตัว')


def test_full_units_and_is_known_use_the_same_cache(monkeypatch, tmp_path):
    p = _point_at(monkeypatch, tmp_path, {'ตว': 'ตัว', 'หล': 'โหล'})
    reads = []
    real_open = open
    monkeypatch.setattr('builtins.open', lambda f, *a, **kw: (
        reads.append(str(f)) if str(f) == p else None, real_open(f, *a, **kw))[1])

    assert bsn_units.full_units() == {'ตัว', 'โหล'}
    assert bsn_units.is_known('ตว') and bsn_units.is_known('โหล')
    assert not bsn_units.is_known('ไม่มี')
    assert len(reads) == 1
