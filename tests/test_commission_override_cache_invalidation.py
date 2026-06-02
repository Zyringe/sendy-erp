"""commission._load_overrides must auto-reload when commission_overrides changes.

Regression for the cross-worker stale-cache money bug. The override list was
cached in a module global (_OVERRIDES_CACHE) cleared only by
clear_override_cache(), which runs ONLY in the gunicorn worker that handled the
edit. Under `-w 2` the sibling worker kept the stale rules, so ~50% of Railway
commission calculations used the OLD override rate/fixed-per-unit/price-gate
after any override edit — silently wrong money that feeds payroll.

Fix: _load_overrides reads the table fresh on every call (no process-global
cache), so every worker always sees the current rules — no cross-process
invalidation needed. These tests mutate the DB *directly* (simulating "another
worker wrote it") and assert the next _load_overrides() reflects the change with
NO clear_override_cache() call.
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import commission


def _insert_override(db_path, product_id=1, fixed_per_unit=5.0):
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO commission_overrides (product_id, fixed_per_unit, is_active) "
        "VALUES (?, ?, 1)",
        (product_id, fixed_per_unit),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def _bump(db_path, sql, params):
    conn = sqlite3.connect(db_path)
    conn.execute(sql, params)
    conn.commit()
    conn.close()


def test_insert_seen_without_clear_cache(tmp_db):
    n0 = len(commission._load_overrides(tmp_db))
    _insert_override(tmp_db, product_id=1, fixed_per_unit=7.0)
    # deliberately NO clear_override_cache() — simulate a write by another worker
    after = commission._load_overrides(tmp_db)
    assert len(after) == n0 + 1


def test_edit_fixed_per_unit_seen_without_clear_cache(tmp_db):
    rid = _insert_override(tmp_db, product_id=1, fixed_per_unit=5.0)
    loaded = commission._load_overrides(tmp_db)
    assert any(o['id'] == rid and o['fixed_per_unit'] == 5.0 for o in loaded)

    # another worker edits the rate (mirror the real UPDATE: bumps updated_at)
    _bump(
        tmp_db,
        "UPDATE commission_overrides SET fixed_per_unit = 9.0, "
        "updated_at = datetime('now','localtime','+1 second') WHERE id = ?",
        (rid,),
    )
    reloaded = commission._load_overrides(tmp_db)
    assert any(o['id'] == rid and o['fixed_per_unit'] == 9.0 for o in reloaded)


def test_toggle_inactive_seen_without_clear_cache(tmp_db):
    rid = _insert_override(tmp_db, product_id=1, fixed_per_unit=5.0)
    assert any(o['id'] == rid for o in commission._load_overrides(tmp_db))

    _bump(
        tmp_db,
        "UPDATE commission_overrides SET is_active = 0, "
        "updated_at = datetime('now','localtime','+1 second') WHERE id = ?",
        (rid,),
    )
    reloaded = commission._load_overrides(tmp_db)
    assert not any(o['id'] == rid for o in reloaded)  # only active rows returned


def test_delete_seen_without_clear_cache(tmp_db):
    rid = _insert_override(tmp_db, product_id=1, fixed_per_unit=5.0)
    n_with = len(commission._load_overrides(tmp_db))
    _bump(tmp_db, "DELETE FROM commission_overrides WHERE id = ?", (rid,))
    reloaded = commission._load_overrides(tmp_db)
    assert len(reloaded) == n_with - 1


def test_repeated_reads_are_consistent(tmp_db):
    """With no table change, repeated fresh reads return equal content (no stale
    cache, and no spurious drift between reads)."""
    a = commission._load_overrides(tmp_db)
    b = commission._load_overrides(tmp_db)
    assert a == b
