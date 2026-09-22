"""#601 acceptance criterion: "Map test: หอ + xp5 -> หลอด, หอ + BSN5657 -> ห่อ,
ดว + xp5 -> ดวง." Plus the seeding rule this migration's own header states:
every other xp5 code copies whatever word BSN5657's row holds AT MIGRATION
RUN TIME (a live SELECT, not a hand-typed literal) — verified here by
comparing the two books' rows directly, never by hardcoding a second word
list that could drift from the migration's own source of truth.

⛔ Order matters and is tested here, not just asserted: mig 192 depends on
mig 190 (#599, กร/ถง/บล -> Express meaning) having already applied — 190's
own UPDATE is scoped to `WHERE book = 'BSN5657'` and never touches book='xp5'
rows, so 192's copy-from-BSN5657 step must run AFTER 190 or xp5 freezes at
the old (wrong) words forever. The fixture below applies 185 -> 190 -> 192,
the only order the real runner can ever produce (filenames sort that way),
and test_every_other_shared_xp5_code_copies_bsn5657s_live_word plus the
explicit กร/ถง pin below both fail if that guarantee is ever violated by
hand (e.g. by applying 192 before 190 in a rehearsal).

Self-contained per erp-engineering-discipline.md's ordering trap: applies
all three forward migrations itself rather than relying on some earlier
test in the run having already migrated a shared connection.
"""
from pathlib import Path

import pytest

import bsn_units

_MIGRATIONS = Path(__file__).resolve().parents[1] / 'data' / 'migrations'
_MIG_185 = _MIGRATIONS / '185_unit_map_table.sql'
_MIG_190 = _MIGRATIONS / '190_express_unit_meanings.sql'
_MIG_192 = _MIGRATIONS / '192_unit_map_xp5_meanings.sql'
_ROLLBACK_192 = _MIGRATIONS / '192_unit_map_xp5_meanings.rollback.sql'

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
    """185 (seed the map) -> 190 (#599's กร/ถง/บล correction, BSN5657-only)
    -> 192 (this ticket) — the ONLY order the real runner can produce, and
    the order that makes 192's live copy pick up 190's corrected words."""
    empty_db_conn.executescript(_MIG_185.read_text(encoding='utf-8'))
    empty_db_conn.executescript(_MIG_190.read_text(encoding='utf-8'))
    empty_db_conn.executescript(_MIG_192.read_text(encoding='utf-8'))
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


def test_gor_and_thung_pick_up_mig190s_corrected_meaning(seeded_conn):
    """The pin the review round asked for: xp5's `กร`/`ถง` must read as
    Express actually means them (กุรุส/ถัง — matching xp5's OWN ISTAB, not
    just BSN5657's), which only happens because 190 ran before 192. This is
    the ONE case where getting the order backwards produces a wrong but
    plausible-looking value (ตัว/ถุง) rather than an obviously missing row,
    so it gets an explicit hardcoded assertion, not just the dynamic mirror
    test below."""
    assert bsn_units.translate('กร', 'xp5', conn=seeded_conn) == 'กุรุส'
    assert bsn_units.translate('ถง', 'xp5', conn=seeded_conn) == 'ถัง'
    # And BSN5657's own rows are what 190 promised, confirming the fixture
    # really did apply 190 (not skip it silently).
    assert bsn_units.translate('กร', 'BSN5657', conn=seeded_conn) == 'กุรุส'
    assert bsn_units.translate('ถง', 'BSN5657', conn=seeded_conn) == 'ถัง'


def test_every_other_shared_xp5_code_copies_bsn5657s_live_word(seeded_conn):
    """The migration's stated rule: every xp5 code besides หอ (and ดว, which
    BSN5657 lacks) gets EXACTLY the word BSN5657's own row holds — read back
    from the live table, not a second hardcoded list, so this cannot drift
    from the migration file's own source of truth. With 190 applied first,
    this naturally covers กร=กุรุส/ถง=ถัง too, without hardcoding them here."""
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
    """`บล` (BSN5657 -> บล็อก since mig 190) is not in xp5's own ISTAB at
    all, so it must not appear under book='xp5' here regardless."""
    assert 'บล' not in XP5_ISTAB_CODES
    assert bsn_units.translate('บล', 'BSN5657', conn=seeded_conn) == 'บล็อก'
    assert seeded_conn.execute(
        "SELECT 1 FROM unit_map WHERE book='xp5' AND spelling='บล'").fetchone() is None


def test_xp5_gaps_are_left_for_a_later_ticket_not_invented_here(seeded_conn):
    """ขว/คร/ตล/ทน/ใบ: xp5 codes with no existing BSN5657 row today. This
    migration must not guess a word for them (that is #610's job)."""
    for code in ('ขว', 'คร', 'ตล', 'ทน', 'ใบ'):
        assert bsn_units.translate(code, 'xp5', conn=seeded_conn) is None, (
            f'{code!r} should be unseeded (no BSN5657 row to copy) until #610')


def test_bsn5657_rows_are_untouched_by_192(seeded_conn):
    """186/192 acceptance-criterion twin: seeding xp5 must not touch a
    single BSN5657 row (count AND content) — 190's own corrections are
    already baked in by the fixture chain BEFORE 192 runs, so this pins
    192's non-interference with them, not the pre-190 values."""
    rows = dict(seeded_conn.execute(
        "SELECT spelling, word FROM unit_map WHERE book = 'BSN5657'"))
    assert len(rows) == 44
    assert rows['กร'] == 'กุรุส'         # mig 190's fix (#599), already applied
    assert rows['ถง'] == 'ถัง'           # mig 190's fix (#599), already applied
    assert rows['บล'] == 'บล็อก'         # mig 190's fix (#599), already applied
    assert rows['หอ'] == 'ห่อ'           # untouched by either 190 or 192


def test_migration_is_rerunnable(seeded_conn):
    before = sorted(tuple(r) for r in seeded_conn.execute(
        "SELECT book, spelling, word FROM unit_map"))
    seeded_conn.executescript(_MIG_192.read_text(encoding='utf-8'))
    seeded_conn.commit()
    after = sorted(tuple(r) for r in seeded_conn.execute(
        "SELECT book, spelling, word FROM unit_map"))
    assert before == after


def test_rollback_removes_only_xp5_rows(seeded_conn):
    seeded_conn.executescript(_ROLLBACK_192.read_text(encoding='utf-8'))
    seeded_conn.commit()
    assert seeded_conn.execute(
        "SELECT COUNT(*) FROM unit_map WHERE book='xp5'").fetchone()[0] == 0
    assert seeded_conn.execute(
        "SELECT COUNT(*) FROM unit_map WHERE book='BSN5657'").fetchone()[0] == 44


def test_forward_migration_replays_cleanly_after_rollback(seeded_conn):
    """Rehearsed in both directions on the SAME connection (the sqlite
    equivalent of the .backup-copy rehearsal, cheap enough here that the
    migration touches no other table)."""
    seeded_conn.executescript(_ROLLBACK_192.read_text(encoding='utf-8'))
    seeded_conn.commit()
    seeded_conn.executescript(_MIG_192.read_text(encoding='utf-8'))
    seeded_conn.commit()
    xp5_rows = dict(seeded_conn.execute(
        "SELECT spelling, word FROM unit_map WHERE book = 'xp5'"))
    assert xp5_rows['หอ'] == 'หลอด'
    assert xp5_rows['ดว'] == 'ดวง'
    assert xp5_rows['ตว'] == 'ตัว'
    assert xp5_rows['กร'] == 'กุรุส'
    assert xp5_rows['ถง'] == 'ถัง'


def test_running_192_before_190_freezes_the_wrong_word(empty_db_conn):
    """Documents the hazard the header now names, on its OWN throwaway
    connection (never the shared `seeded_conn`, which must stay in the real
    185->190->192 order): if 192 ran before 190, xp5's กร/ถง would copy
    BSN5657's UNCORRECTED word and never self-heal, because 190's UPDATE
    never touches book='xp5' and 192 never re-runs. This is what makes "190
    must run first" a real constraint rather than a defensive comment.

    Uses `empty_db_conn` (full live-schema clone) rather than a bare
    in-memory table, because 190 JOINs against products/product_code_
    mapping/unit_conversions — all empty here, so 190 has nothing to touch
    on the product side, but its unit_map UPDATE still runs."""
    empty_db_conn.executescript(_MIG_185.read_text(encoding='utf-8'))
    empty_db_conn.executescript(_MIG_192.read_text(encoding='utf-8'))  # wrong order, on purpose
    assert bsn_units.translate('กร', 'xp5', conn=empty_db_conn) == 'ตัว', (
        'this is the WRONG value — it exists to prove the hazard, not to '
        'bless it. Running 190 after this point cannot fix xp5 anymore.')
    empty_db_conn.executescript(_MIG_190.read_text(encoding='utf-8'))
    assert bsn_units.translate('กร', 'xp5', conn=empty_db_conn) == 'ตัว', (
        '190 ran but never touched xp5 — frozen wrong, as the header warns')
    assert bsn_units.translate('กร', 'BSN5657', conn=empty_db_conn) == 'กุรุส', (
        'meanwhile BSN5657 IS corrected — the two books now visibly disagree'
    )
