"""#601 acceptance criterion: "Map test: หอ + xp5 -> หลอด, หอ + BSN5657 -> ห่อ,
ดว + xp5 -> ดวง." Plus the seeding rule this migration's own header states:
every other xp5 code copies whatever word BSN5657's row holds AT MIGRATION
RUN TIME (a live SELECT, not a hand-typed literal) — verified here by
comparing the two books' rows directly, never by hardcoding a second word
list that could drift from the migration's own source of truth.

Self-contained per erp-engineering-discipline.md's ordering trap: applies
both forward migrations itself (185 then 191) rather than relying on some
earlier test in the run having already migrated a shared connection.
"""
import sqlite3
from pathlib import Path

import pytest

import bsn_units

_MIGRATIONS = Path(__file__).resolve().parents[1] / 'data' / 'migrations'
_MIG_185 = _MIGRATIONS / '185_unit_map_table.sql'
_MIG_191 = _MIGRATIONS / '191_unit_map_xp5_meanings.sql'
_ROLLBACK_191 = _MIGRATIONS / '191_unit_map_xp5_meanings.rollback.sql'

# The 34 codes xp5's own ISTAB (TABTYP '20') holds, read live 2026-09-21 —
# independent of the migration file, so a bug in the migration's own IN(...)
# list would show up as a mismatch here rather than agreeing with itself.
XP5_ISTAB_CODES = {
    'กก', 'กน', 'กป', 'กร', 'กล', 'ขด', 'ขว', 'คน', 'คร', 'คู', 'ชด', 'ชน',
    'ซง', 'ดก', 'ดว', 'ตล', 'ตว', 'ถง', 'ทง', 'ทน', 'ปน', 'ผง', 'ผน', 'มน',
    'ลก', 'ลง', 'สน', 'หค', 'หล', 'หอ', 'อน', 'แก', 'แพ', 'ใบ',
}


@pytest.fixture
def seeded_conn(empty_db_conn):
    empty_db_conn.executescript(_MIG_185.read_text(encoding='utf-8'))
    empty_db_conn.executescript(_MIG_191.read_text(encoding='utf-8'))
    empty_db_conn.commit()
    return empty_db_conn


def test_istab_fixture_matches_the_migrations_own_code_count():
    """Anti-drift control for the fixture above: if xp5 ever grows/shrinks a
    code, this test — not a silently-wrong assumption baked into the
    assertions below — is what should go red first."""
    assert len(XP5_ISTAB_CODES) == 34


def test_hoo_and_duang_are_the_stated_exceptions(seeded_conn):
    assert bsn_units.translate('หอ', 'xp5', conn=seeded_conn) == 'หลอด'
    assert bsn_units.translate('หอ', 'BSN5657', conn=seeded_conn) == 'ห่อ'
    assert bsn_units.translate('ดว', 'xp5', conn=seeded_conn) == 'ดวง'
    # ดว has no BSN5657 counterpart at all — not even the wrong one.
    assert bsn_units.translate('ดว', 'BSN5657', conn=seeded_conn) is None


def test_hod_bsn5657_spelling_of_the_same_unit_does_not_leak_into_xp5(seeded_conn):
    """xp5 has no `หด` code (it spells the same unit `หอ`). A migration bug
    that seeded `หด` under xp5 too would be harmless UNLESS it also silently
    proved the copy loop is copying BSN5657 spellings wholesale rather than
    scoping to xp5's own code list — which the IN(...) list must do."""
    assert bsn_units.translate('หด', 'xp5', conn=seeded_conn) is None
    assert bsn_units.translate('หด', 'BSN5657', conn=seeded_conn) == 'หลอด'


def test_every_other_shared_xp5_code_copies_bsn5657s_live_word(seeded_conn):
    """The migration's stated rule: every xp5 code besides หอ (and ดว, which
    BSN5657 lacks) gets EXACTLY the word BSN5657's own row holds — read back
    from the live table, not a second hardcoded list, so this cannot drift
    from the migration file's own source of truth."""
    bsn_rows = dict(seeded_conn.execute(
        "SELECT spelling, word FROM unit_map WHERE book = 'BSN5657'"))
    xp5_rows = dict(seeded_conn.execute(
        "SELECT spelling, word FROM unit_map WHERE book = 'xp5'"))

    shared = XP5_ISTAB_CODES - {'หอ', 'ดว'}
    checked = 0
    for code in shared:
        if code not in bsn_rows:
            # No BSN5657 row to copy (five such gaps exist: ขว/คร/ตล/ทน/ใบ) —
            # #610's job, not seeded here. Assert the negative explicitly so a
            # future accidental seed of one of these is caught, not ignored.
            assert code not in xp5_rows, (
                f'{code!r} has no BSN5657 row to copy from, yet xp5 has one — '
                'where did this word come from?')
            continue
        assert xp5_rows.get(code) == bsn_rows[code], (
            f'{code!r}: xp5={xp5_rows.get(code)!r} != BSN5657={bsn_rows[code]!r}')
        checked += 1
    # Control: the loop actually compared something, not a vacuously-empty set.
    assert checked >= 20, f'only {checked} shared codes actually compared'


def test_bl_has_no_xp5_counterpart(seeded_conn):
    """`บล` (BSN5657 -> แผง, soon กุรุส's sibling fix under #599) is not in
    xp5's own ISTAB at all, so it must not appear under book='xp5' here."""
    assert 'บล' not in XP5_ISTAB_CODES
    assert seeded_conn.execute(
        "SELECT 1 FROM unit_map WHERE book='xp5' AND spelling='บล'").fetchone() is None


def test_xp5_gaps_are_left_for_a_later_ticket_not_invented_here(seeded_conn):
    """ขว/คร/ตล/ทน/ใบ: xp5 codes with no existing BSN5657 row today. This
    migration must not guess a word for them (that is #610's job)."""
    for code in ('ขว', 'คร', 'ตล', 'ทน', 'ใบ'):
        assert bsn_units.translate(code, 'xp5', conn=seeded_conn) is None, (
            f'{code!r} should be unseeded (no BSN5657 row to copy) until #610')


def test_bsn5657_rows_are_untouched(seeded_conn):
    """186 acceptance-criterion twin: seeding xp5 must not touch a single
    BSN5657 row (count AND content)."""
    rows = dict(seeded_conn.execute(
        "SELECT spelling, word FROM unit_map WHERE book = 'BSN5657'"))
    assert len(rows) == 44
    assert rows['กร'] == 'ตัว'          # #599's fix has not landed on this branch
    assert rows['หอ'] == 'ห่อ'


def test_migration_is_rerunnable(seeded_conn):
    before = sorted(tuple(r) for r in seeded_conn.execute(
        "SELECT book, spelling, word FROM unit_map"))
    seeded_conn.executescript(_MIG_191.read_text(encoding='utf-8'))
    seeded_conn.commit()
    after = sorted(tuple(r) for r in seeded_conn.execute(
        "SELECT book, spelling, word FROM unit_map"))
    assert before == after


def test_rollback_removes_only_xp5_rows(seeded_conn):
    seeded_conn.executescript(_ROLLBACK_191.read_text(encoding='utf-8'))
    seeded_conn.commit()
    assert seeded_conn.execute(
        "SELECT COUNT(*) FROM unit_map WHERE book='xp5'").fetchone()[0] == 0
    assert seeded_conn.execute(
        "SELECT COUNT(*) FROM unit_map WHERE book='BSN5657'").fetchone()[0] == 44


def test_forward_migration_replays_cleanly_after_rollback(seeded_conn):
    """Rehearsed in both directions on the SAME connection (the sqlite
    equivalent of the .backup-copy rehearsal, cheap enough here that the
    migration touches no other table)."""
    seeded_conn.executescript(_ROLLBACK_191.read_text(encoding='utf-8'))
    seeded_conn.commit()
    seeded_conn.executescript(_MIG_191.read_text(encoding='utf-8'))
    seeded_conn.commit()
    xp5_rows = dict(seeded_conn.execute(
        "SELECT spelling, word FROM unit_map WHERE book = 'xp5'"))
    assert xp5_rows['หอ'] == 'หลอด'
    assert xp5_rows['ดว'] == 'ดวง'
    assert xp5_rows['ตว'] == 'ตัว'
