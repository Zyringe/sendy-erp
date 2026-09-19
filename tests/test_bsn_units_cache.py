"""The unit map has NO per-process cache (#596).

WHY THIS EXISTS — this file used to pin a stat-keyed cache over
data/reference/bsn_unit_full.json (removed with the file itself). The JSON
cache existed because re-parsing 3KB of JSON 74,905 times cost ~2.4s of a
~3.5s run (2026-08-25 profiling) — but it also had to REVALIDATE, because
under `gunicorn -w 2` the worker that did not handle a `learn()` write must
still see it (PR #103's per-worker-state bug, twice).

Moving the map into the DB removes the need for the cache entirely: a
44-row indexed SQLite lookup is microseconds, so `bsn_units.py` now reads
the DB fresh on every call. This file proves the replacement holds the same
property the cache used to buy — a write from one connection ("worker") is
immediately visible from a DIFFERENT, freshly-opened connection (the
closest thing a single-process pytest run can do to modelling a second
gunicorn worker) and after the module is re-imported ("app restart").
"""
import importlib
import sqlite3
from pathlib import Path

import bsn_units

_FORWARD_MIG = (Path(__file__).resolve().parents[1] / 'data' / 'migrations'
                / '185_unit_map_table.sql')


def _ensure_migrated(conn):
    """Guarantee the 44-row seed exists on `conn`'s DB, regardless of
    whether some earlier test in this session already migrated the file
    `tmp_db` copied from (erp-engineering-discipline.md's ordering trap —
    this file must pass run alone too). Resets the table first: 185 itself
    keeps existing rows, and the live DB may carry codes named on prod."""
    conn.executescript('DROP TABLE IF EXISTS unit_map;\n'
                       + _FORWARD_MIG.read_text(encoding='utf-8'))
    conn.commit()


def _fresh_conn(tmp_db):
    c = sqlite3.connect(tmp_db, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def test_translate_reads_the_seeded_44_entries(tmp_db_conn):
    _ensure_migrated(tmp_db_conn)
    n = tmp_db_conn.execute(
        "SELECT COUNT(*) FROM unit_map WHERE book = 'BSN5657'").fetchone()[0]
    assert n == 44, f'expected the 44 seeded entries, found {n}'
    assert bsn_units.translate('หล', conn=tmp_db_conn) == 'โหล'
    assert bsn_units.translate('ไม่รู้จัก', conn=tmp_db_conn) is None
    assert bsn_units.normalize_unit('ไม่รู้จัก', conn=tmp_db_conn) == 'ไม่รู้จัก'


def test_a_write_on_one_connection_is_visible_from_a_fresh_one(tmp_db):
    """Models the gunicorn -w 2 requirement directly: connection A learns a
    code, connection B (opened AFTER, sharing nothing but the file) must see
    it without any cache-invalidation step."""
    a = _fresh_conn(tmp_db)
    b = _fresh_conn(tmp_db)
    try:
        _ensure_migrated(a)
        assert bsn_units.translate('ลง', conn=b) == 'ลัง', (
            'CONTROL: ลง must already resolve via the seeded data, or the '
            'rest of this test proves nothing')
        assert bsn_units.is_known('ลง', conn=b)
        assert bsn_units.translate('Zx9', conn=b) is None

        bsn_units.learn('Zx9', bsn_units.DEFAULT_BOOK, 'หน่วยใหม่', conn=a)
        a.commit()

        # B never re-opened, never told to invalidate anything — read fresh.
        assert bsn_units.translate('Zx9', conn=b) == 'หน่วยใหม่'
        assert bsn_units.is_known('Zx9', conn=b)
        assert bsn_units.normalize_unit('ลง', conn=b) == 'ลัง'   # CONTROL: not clobbered
    finally:
        a.close()
        b.close()


def test_a_write_survives_a_reimport(tmp_db, monkeypatch):
    """Models an app restart: re-import the module (a fresh Python process
    would rebuild all module state) and confirm the write is still there —
    there is no module-level dict left to have gone stale OR reset."""
    import config
    import database
    monkeypatch.setattr(config, 'DATABASE_PATH', tmp_db)
    monkeypatch.setattr(database, 'DATABASE_PATH', tmp_db)
    seed_conn = _fresh_conn(tmp_db)
    _ensure_migrated(seed_conn)
    seed_conn.close()

    bsn_units.add_acronym('Qq9', 'กระป๋องใหม่')

    reloaded = importlib.reload(bsn_units)
    try:
        assert reloaded.normalize_unit('Qq9') == 'กระป๋องใหม่'
    finally:
        importlib.reload(bsn_units)   # restore the real module object identity


def test_full_units_and_is_known_agree(tmp_db_conn):
    _ensure_migrated(tmp_db_conn)
    words = bsn_units.full_units(conn=tmp_db_conn)
    assert {'ตัว', 'โหล', 'กิโลกรัม'} <= words
    assert bsn_units.is_known('ตว', conn=tmp_db_conn)    # a spelling -> ตัว
    assert bsn_units.is_known('โหล', conn=tmp_db_conn)   # a word, standing for itself
    assert not bsn_units.is_known('ไม่มี', conn=tmp_db_conn)


def test_learn_is_idempotent_upsert(tmp_db_conn):
    _ensure_migrated(tmp_db_conn)
    bsn_units.learn('Zab', 'BSN5657', 'หน่วยหนึ่ง', conn=tmp_db_conn)
    bsn_units.learn('Zab', 'BSN5657', 'หน่วยสอง', conn=tmp_db_conn)   # re-learn, corrected spelling
    tmp_db_conn.commit()
    rows = tmp_db_conn.execute(
        "SELECT word FROM unit_map WHERE book='BSN5657' AND spelling='Zab'").fetchall()
    assert len(rows) == 1, 'UNIQUE(book, spelling) must upsert, not duplicate'
    assert rows[0][0] == 'หน่วยสอง'


def test_load_unit_map_overlays_book_specific_on_book_any(tmp_db_conn):
    _ensure_migrated(tmp_db_conn)
    tmp_db_conn.execute(
        "INSERT INTO unit_map (book, spelling, word) VALUES ('*', 'กก.', 'กิโลกรัม')")
    tmp_db_conn.execute(
        "INSERT INTO unit_map (book, spelling, word) VALUES ('xp5', 'หอ', 'หลอด')")
    tmp_db_conn.commit()
    m = bsn_units.load_unit_map('xp5', conn=tmp_db_conn)
    assert m['กก.'] == 'กิโลกรัม'         # book-independent variant reachable under any book
    assert m['หอ'] == 'หลอด'              # xp5-specific row
    assert bsn_units.load_unit_map('BSN5657', conn=tmp_db_conn)['หอ'] == 'ห่อ', (
        'BOOK_ANY must never shadow a book-specific row on a DIFFERENT book')
