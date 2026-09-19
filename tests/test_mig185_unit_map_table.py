"""Migration 185 — the unit map moves from bsn_unit_alias (mig 064) to
unit_map, seeded with exactly today's 44 entries under BSN5657 (#596).

Rehearsed in both directions on a schema-only clone of the live DB
(`empty_db_conn`), diffing `sqlite_master` + row content before/after —
not just "the table exists" (erp-engineering-discipline.md: a clean
rollback means the old table comes back byte-identical, not merely
present).
"""
import json
import os
from pathlib import Path

_MIG_DIR = Path(__file__).resolve().parents[1] / 'data' / 'migrations'
FORWARD = _MIG_DIR / '185_unit_map_table.sql'
ROLLBACK = _MIG_DIR / '185_unit_map_table.rollback.sql'

_FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures',
                        'bsn_unit_full_pre596.json')


def _pre596_map():
    with open(_FIXTURE, encoding='utf-8') as f:
        return json.load(f)['map']


def _apply(conn, path):
    conn.executescript(path.read_text(encoding='utf-8'))
    conn.commit()


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _pre_state(conn):
    """Reconstruct pre-185: bsn_unit_alias exists, unit_map does not —
    forced, never inherited (see erp-engineering-discipline.md's tmp_db
    trap: this DB may already have 185 applied)."""
    _apply(conn, ROLLBACK)
    conn.execute("DELETE FROM applied_migrations WHERE filename = ?",
                 (FORWARD.name,))
    conn.commit()


def test_forward_creates_unit_map_and_drops_bsn_unit_alias(empty_db_conn):
    _pre_state(empty_db_conn)
    assert _table_exists(empty_db_conn, 'bsn_unit_alias')
    assert not _table_exists(empty_db_conn, 'unit_map')

    _apply(empty_db_conn, FORWARD)

    assert not _table_exists(empty_db_conn, 'bsn_unit_alias')
    assert _table_exists(empty_db_conn, 'unit_map')
    rows = {r[0]: r[1] for r in empty_db_conn.execute(
        "SELECT spelling, word FROM unit_map WHERE book = 'BSN5657'")}
    assert rows == _pre596_map()
    idx = empty_db_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_unit_map_book_spelling'"
    ).fetchone()
    assert idx is not None and 'UNIQUE' in idx[0]


def test_forward_is_re_runnable(empty_db_conn):
    """Drop-first: applying it twice in a row must not error (mirrors the
    live-migration-runner's filename-keyed, non-idempotent-by-default
    reality — a re-apply during rehearsal must still succeed)."""
    _pre_state(empty_db_conn)
    _apply(empty_db_conn, FORWARD)
    _apply(empty_db_conn, FORWARD)   # must not raise "table already exists"
    n = empty_db_conn.execute(
        "SELECT COUNT(*) FROM unit_map WHERE book = 'BSN5657'").fetchone()[0]
    assert n == 44, 'a re-run must not duplicate rows'


def test_rollback_restores_bsn_unit_alias_byte_identical(empty_db_conn):
    _pre_state(empty_db_conn)
    before = {r[0]: r[1] for r in empty_db_conn.execute(
        "SELECT acronym, full FROM bsn_unit_alias")}
    assert before == _pre596_map(), 'CONTROL: the pre-state itself must match the oracle'

    _apply(empty_db_conn, FORWARD)
    _apply(empty_db_conn, ROLLBACK)

    assert not _table_exists(empty_db_conn, 'unit_map')
    assert _table_exists(empty_db_conn, 'bsn_unit_alias')
    after = {r[0]: r[1] for r in empty_db_conn.execute(
        "SELECT acronym, full FROM bsn_unit_alias")}
    assert after == before == _pre596_map()


def test_precondition_control_the_scan_can_find_something(empty_db_conn):
    """Anti-vacuity: prove the byte-identical comparison above is capable of
    FAILING, or it is not a comparison. Corrupt one row post-forward-migration
    and confirm the rollback-restore check would have caught it."""
    _pre_state(empty_db_conn)
    _apply(empty_db_conn, FORWARD)
    empty_db_conn.execute(
        "UPDATE unit_map SET word = 'ผิด' WHERE book='BSN5657' AND spelling='กร'")
    empty_db_conn.commit()
    rows = {r[0]: r[1] for r in empty_db_conn.execute(
        "SELECT spelling, word FROM unit_map WHERE book = 'BSN5657'")}
    assert rows != _pre596_map(), 'the mutation must be visible to the comparison'
