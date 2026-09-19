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


# ── never empty by construction, instead of a per-call guard ────────────────
#
# A fresh DB built from data/schema.sql (bare git clone, empty Railway
# volume, or vat_book_builder's DATA_DIR-isolated subprocess) has the
# unit_map TABLE (schema.sql is DDL) but ZERO rows (only migration 185's own
# INSERTs seed it, and a fresh-DB boot backfills every migration as already
# applied without re-running them). Team-lead review on #596: this must not
# silently pass every Express code through untranslated.
#
# Two shapes were tried. A per-call raise in bsn_units.py (option b) was
# implemented first and reverted: measured against the full suite it broke
# ~100 unrelated tests that build a schema-only `empty_db`/`empty_db_conn`
# clone for OTHER features and only incidentally pass through a bsn_units
# call (cross_unit_hazard, import_weekly, reconcile, ...) — the guard's
# blast radius was disproportionate to the deployment-only risk it existed
# to catch. `database.py::init_db()` instead re-seeds `unit_map` from
# migration 185's own SQL whenever the table exists but is empty (option a)
# — see tests/test_fresh_db_build.py::test_init_db_from_empty_seeds_unit_map
# for the break-it-once-verified regression test. This file's tests are
# unaffected either way (they seed through the migration directly, not
# through init_db()).
