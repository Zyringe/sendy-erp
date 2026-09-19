"""#596 acceptance criterion 1: "all 44 of today's entries translate exactly
as before (a table-driven test over every entry, BSN5657)."

Independent oracle: `tests/fixtures/bsn_unit_full_pre596.json` is a frozen,
byte-identical copy of the retired data/reference/bsn_unit_full.json (diffed
against git history at 945da82, the commit that last touched it, before
migration 185 seeded the DB from it) — never read by any runtime code, only
by this test, so it carries no risk of reviving the JSON as a second map.
"""
import json
import os
from pathlib import Path

import pytest

import bsn_units

_FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures',
                        'bsn_unit_full_pre596.json')
_FORWARD_MIG = (Path(__file__).resolve().parents[1] / 'data' / 'migrations'
                / '185_unit_map_table.sql')


def _pre596_map():
    with open(_FIXTURE, encoding='utf-8') as f:
        return json.load(f)['map']


@pytest.fixture
def seeded_conn(empty_db_conn):
    """Self-contained: applies migration 185 itself rather than relying on
    `tmp_db` having inherited it from a DB some earlier test happened to
    migrate first (erp-engineering-discipline.md's ordering trap — this file
    must pass run alone, e.g. `pytest tests/test_unit_map_table.py`)."""
    empty_db_conn.executescript(_FORWARD_MIG.read_text(encoding='utf-8'))
    empty_db_conn.commit()
    return empty_db_conn


def test_fixture_is_the_44_entries_migration_185_was_seeded_from():
    m = _pre596_map()
    assert len(m) == 44, 'the oracle itself drifted — re-derive from git history, do not hand-edit'


def test_every_entry_translates_exactly_as_before(seeded_conn):
    m = _pre596_map()
    for spelling, expected_word in m.items():
        got = bsn_units.translate(spelling, bsn_units.DEFAULT_BOOK, conn=seeded_conn)
        assert got == expected_word, (
            f'{spelling!r} -> {got!r}, expected {expected_word!r} (this ticket '
            f'makes NO meaning changes — #599/#601 do)')
        # normalize_unit must agree (it is translate() + identity fallback)
        assert bsn_units.normalize_unit(spelling, conn=seeded_conn) == expected_word
        assert bsn_units.is_known(spelling, conn=seeded_conn)


def test_unit_map_table_has_no_extra_or_missing_bsn5657_rows(seeded_conn):
    m = _pre596_map()
    rows = {r[0]: r[1] for r in seeded_conn.execute(
        "SELECT spelling, word FROM unit_map WHERE book = 'BSN5657'")}
    assert rows == m


def test_an_unknown_spelling_is_unknown(seeded_conn):
    assert bsn_units.translate('ไม่รู้จักแน่นอน', conn=seeded_conn) is None
    assert bsn_units.normalize_unit('ไม่รู้จักแน่นอน', conn=seeded_conn) == 'ไม่รู้จักแน่นอน'
    assert not bsn_units.is_known('ไม่รู้จักแน่นอน', conn=seeded_conn)


def test_book_any_is_a_fallback_never_a_shadow(seeded_conn):
    """A BOOK_ANY (book-independent) variant is reachable under every book,
    but a book-specific row for the SAME spelling always wins — the
    precedence #599/#601 will rely on to override a meaning per book."""
    seeded_conn.execute(
        "INSERT INTO unit_map (book, spelling, word) VALUES ('*', 'กก.', 'กิโลกรัม')")
    seeded_conn.commit()
    assert bsn_units.translate('กก.', 'BSN5657', conn=seeded_conn) == 'กิโลกรัม'
    assert bsn_units.translate('กก.', 'xp5', conn=seeded_conn) == 'กิโลกรัม'

    seeded_conn.execute(
        "INSERT INTO unit_map (book, spelling, word) VALUES ('xp5', 'กก.', 'กิโลใหม่')")
    seeded_conn.commit()
    assert bsn_units.translate('กก.', 'xp5', conn=seeded_conn) == 'กิโลใหม่', (
        'a book-specific row must win over BOOK_ANY for that same book')
    assert bsn_units.translate('กก.', 'BSN5657', conn=seeded_conn) == 'กิโลกรัม', (
        'a DIFFERENT book must be unaffected by an xp5-specific override'
    )


def test_a_caller_that_omits_book_gets_bsn5657(seeded_conn):
    """"a caller that doesn't pass a book yet gets today's behaviour
    (BSN5657)" — the literal acceptance-criterion wording."""
    assert bsn_units.translate('กร', conn=seeded_conn) == 'ตัว'      # no book arg
    assert bsn_units.translate('กร', bsn_units.BOOK_BSN5657, conn=seeded_conn) == 'ตัว'


# ── fail loud when unseeded, instead of silently importing raw codes ────────
#
# A fresh DB built from data/schema.sql (bare git clone, empty Railway
# volume, or vat_book_builder's DATA_DIR-isolated subprocess) has the
# unit_map TABLE (schema.sql is DDL) but ZERO rows (only migration 185's own
# INSERTs seed it, and a fresh-DB boot backfills every migration as already
# applied without re-running them). Team-lead review on #596: this must
# raise, not pass every Express code through untranslated.

def test_empty_table_raises_instead_of_treating_everything_as_unknown(empty_db_conn):
    # empty_db_conn: unit_map exists (schema-only clone) with 0 rows —
    # exactly the fresh-DB-from-schema.sql shape, without needing a second
    # throwaway db file.
    with pytest.raises(bsn_units.UnitMapNotSeeded):
        bsn_units.translate('กร', conn=empty_db_conn)
    with pytest.raises(bsn_units.UnitMapNotSeeded):
        bsn_units.normalize_unit('กร', conn=empty_db_conn)
    with pytest.raises(bsn_units.UnitMapNotSeeded):
        bsn_units.is_known('กร', conn=empty_db_conn)
    with pytest.raises(bsn_units.UnitMapNotSeeded):
        bsn_units.full_units(conn=empty_db_conn)
    with pytest.raises(bsn_units.UnitMapNotSeeded):
        bsn_units.load_unit_map(conn=empty_db_conn)


def test_missing_table_also_raises(tmp_path):
    import sqlite3
    conn = sqlite3.connect(tmp_path / 'no_unit_map.db')
    conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY)")  # unrelated table; no unit_map at all
    with pytest.raises(bsn_units.UnitMapNotSeeded):
        bsn_units.translate('กร', conn=conn)
    conn.close()


def test_seeding_one_row_is_enough_to_clear_the_guard(empty_db_conn):
    """CONTROL for the two tests above: the guard fires on EMPTY, not on
    every empty_db_conn call for some unrelated reason."""
    empty_db_conn.execute(
        "INSERT INTO unit_map (book, spelling, word) VALUES ('BSN5657', 'กร', 'ตัว')")
    empty_db_conn.commit()
    assert bsn_units.translate('กร', conn=empty_db_conn) == 'ตัว'


def test_break_it_once_removing_the_guard_lets_unknown_codes_through(empty_db_conn, monkeypatch):
    """Anti-vacuity: prove the guard is load-bearing. With `_assert_seeded`
    disabled, an empty table stops raising and silently returns None
    (normalize_unit then passes the raw code through unchanged) — the
    EXACT outcome this guard exists to prevent."""
    monkeypatch.setattr(bsn_units, '_assert_seeded', lambda conn: None)
    assert bsn_units.translate('กร', conn=empty_db_conn) is None
    assert bsn_units.normalize_unit('กร', conn=empty_db_conn) == 'กร'  # raw code, untranslated
