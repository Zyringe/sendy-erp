"""Migration 185: the unit map moves from bsn_unit_alias (mig 064) to
unit_map, seeded with exactly the 44 entries of the retired JSON under
BSN5657 (#596).

The pre-185 state is built from the frozen fixture and mig 064's own DDL,
never from 185's rollback, so the rollback is checked against something it
did not produce. Plain throwaway DB: runs without the live dev DB.
"""
import json
import os
import sqlite3
from pathlib import Path

import pytest

import bsn_units

_MIG_DIR = Path(__file__).resolve().parents[1] / 'data' / 'migrations'
FORWARD = _MIG_DIR / '185_unit_map_table.sql'
ROLLBACK = _MIG_DIR / '185_unit_map_table.rollback.sql'

_FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures',
                        'bsn_unit_full_pre596.json')

# bsn_unit_alias exactly as mig 064 created it (and as prod's sqlite_master
# holds it, read 2026-09-19).
_ALIAS_DDL = """CREATE TABLE bsn_unit_alias (
    acronym TEXT PRIMARY KEY,
    full    TEXT NOT NULL
)"""


def _pre596_map():
    with open(_FIXTURE, encoding='utf-8') as f:
        return json.load(f)['map']


def _apply(conn, path):
    conn.executescript(path.read_text(encoding='utf-8'))
    conn.commit()


def _ddl(conn, name):
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()
    return row[0] if row else None


def _unit_map(conn):
    return {r[0]: r[1] for r in conn.execute(
        "SELECT spelling, word FROM unit_map WHERE book = 'BSN5657'")}


@pytest.fixture
def pre185(tmp_path):
    c = sqlite3.connect(tmp_path / 'pre185.db')
    c.executescript("CREATE TABLE applied_migrations (filename TEXT PRIMARY KEY);\n"
                    + _ALIAS_DDL + ";")
    c.executemany("INSERT INTO bsn_unit_alias (acronym, full) VALUES (?, ?)",
                  _pre596_map().items())
    c.execute("INSERT INTO applied_migrations VALUES (?)", (FORWARD.name,))
    c.commit()
    yield c
    c.close()


def test_forward_creates_unit_map_and_drops_bsn_unit_alias(pre185):
    _apply(pre185, FORWARD)

    assert _ddl(pre185, 'bsn_unit_alias') is None
    assert _unit_map(pre185) == _pre596_map()
    assert pre185.execute("SELECT COUNT(*) FROM unit_map").fetchone()[0] == 44
    assert 'UNIQUE' in _ddl(pre185, 'ux_unit_map_book_spelling')


def test_a_rerun_keeps_what_put_named(pre185):
    """A second run (a DB whose applied_migrations lost 185's row) must not
    touch the map: a code named on /unit-conversions and a changed meaning
    both survive, and nothing is duplicated."""
    _apply(pre185, FORWARD)
    bsn_units.learn('ZZ596', 'BSN5657', 'ทดสอบ', conn=pre185)
    bsn_units.learn('กร', 'BSN5657', 'กุรุส', conn=pre185)
    pre185.commit()
    expected = {**_pre596_map(), 'ZZ596': 'ทดสอบ', 'กร': 'กุรุส'}
    assert _unit_map(pre185) == expected          # control: the edits landed

    _apply(pre185, FORWARD)

    assert _unit_map(pre185) == expected
    assert pre185.execute("SELECT COUNT(*) FROM unit_map").fetchone()[0] == 45


def test_book_is_constrained(pre185):
    _apply(pre185, FORWARD)
    for book in ('xp5', '*'):                      # control: the real books insert
        pre185.execute("INSERT INTO unit_map (book, spelling, word) VALUES (?, 'ทด', 'ทดสอบ')",
                       (book,))
    with pytest.raises(sqlite3.IntegrityError, match='CHECK'):
        pre185.execute("INSERT INTO unit_map (book, spelling, word) VALUES ('BSN', 'ทด', 'ทดสอบ')")


def test_rollback_restores_mig_064s_table_and_ignores_learned_rows(pre185):
    alias_ddl_before = _ddl(pre185, 'bsn_unit_alias')
    _apply(pre185, FORWARD)
    bsn_units.learn('ZZ596', 'BSN5657', 'ทดสอบ', conn=pre185)
    pre185.commit()
    assert 'ZZ596' in _unit_map(pre185)            # control: a learned row exists

    _apply(pre185, ROLLBACK)

    assert _ddl(pre185, 'unit_map') is None
    assert _ddl(pre185, 'bsn_unit_alias') == alias_ddl_before == _ALIAS_DDL
    restored = dict(pre185.execute("SELECT acronym, full FROM bsn_unit_alias"))
    assert restored == _pre596_map()
    assert pre185.execute("SELECT COUNT(*) FROM applied_migrations WHERE filename = ?",
                          (FORWARD.name,)).fetchone()[0] == 0
