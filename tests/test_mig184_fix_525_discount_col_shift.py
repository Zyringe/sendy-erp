"""Migration 184 — GH #525 sales_transactions.discount/total column-shift repair.

The CSV parser's old, unanchored money-column regex grabbed รวมเงิน (the real
line total) into `discount` and the bill's own ส่วนลดรวม into `total` whenever
a line's ส่วนลด was blank and the bill discount was printed in baht. `net`
was always correct. This migration corrects `discount`/`total` on the 176
affected rows, driven by literal values taken from the Express DBF (see the
migration file's own header for the full derivation and #525).

`tmp_db_conn` clones this worktree's real `inventory_app/instance/inventory.db`
— which is a genuine prod-derived snapshot — so the 176 real row ids named in
the migration's VALUES block actually exist here. That is what lets this test
assert on REAL ids instead of synthetic fixtures: no other test file in this
repo has to seed rows for a one-off, DBF-driven data correction like this one.
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3
from pathlib import Path

import pytest

_MIG_DIR = Path(__file__).resolve().parents[1] / 'data/migrations'
MIG_184 = _MIG_DIR / '184_fix_525_discount_col_shift.sql'
ROLLBACK_184 = _MIG_DIR / '184_fix_525_discount_col_shift.rollback.sql'

# A handful of the 176 real affected rows, spanning both mechanisms, plus one
# untouched control row from the same historical era. Values verified against
# both the Express DBF and a live prod read in the PR — see the migration file.
MECH_A_ID = 28394       # IV6800557-1: blank discount was the correct answer
MECH_A_ID2 = 30481       # IV6701161-1: same mechanism, different bill
MECH_B_ID = 36661        # IV6900395-1: pre-comma-fix row, correct discount is '1,429.00'
CONTROL_ID = 19783       # IV6801757-1: already-faithful line, must never move

EXPECTED = {
    MECH_A_ID:  {'old_discount': '480.00',  'old_total': 804.0, 'correct_discount': '',         'correct_total': 480.0,  'net': 0.0,     'qty': 3.0},
    MECH_A_ID2: {'old_discount': '3480.00', 'old_total': 170.0, 'correct_discount': '',         'correct_total': 3480.0, 'net': 3446.26, 'qty': 24.0},
    MECH_B_ID:  {'old_discount': '',        'old_total': 1429.0, 'correct_discount': '1,429.00', 'correct_total': 5991.0, 'net': 5991.0,  'qty': 14.0},
}
CONTROL_EXPECTED = {'discount': '', 'total': 7477.0, 'net': 7477.0, 'qty': 50.0}


@pytest.fixture
def db(tmp_db_conn):
    """Pre-state: 184 NOT applied — undone AND unstamped if this clone had it
    (mirrors test_mig181's own fixture, for the same reason: once 184 ships,
    a dev DB pulled from prod after that carries both the corrected values
    and the applied_migrations row, and this fixture must not silently find
    nothing pending)."""
    tmp_db_conn.executescript(ROLLBACK_184.read_text())
    tmp_db_conn.execute("DELETE FROM applied_migrations WHERE filename = ?",
                        (MIG_184.name,))
    tmp_db_conn.commit()
    return tmp_db_conn


def _row(conn, row_id):
    r = conn.execute(
        "SELECT discount, total, net, qty, unit_price, product_id, doc_no, bsn_code "
        "FROM sales_transactions WHERE id = ?", (row_id,)
    ).fetchone()
    assert r is not None, f"CONTROL — row {row_id} exists in this DB"
    return dict(r)


def _row_count(conn):
    return conn.execute("SELECT COUNT(*) FROM sales_transactions").fetchone()[0]


def _apply(conn, path):
    conn.executescript(path.read_text())
    conn.commit()


# ── pre-state control: the fixture actually produced the OLD, wrong shape ──

def test_control_fixture_starts_in_the_pre_migration_wrong_state(db):
    """If this fails, the `db` fixture is not doing what its docstring
    claims and every other test in this file is vacuous."""
    for row_id, exp in EXPECTED.items():
        row = _row(db, row_id)
        assert row['discount'] == exp['old_discount']
        assert row['total'] == exp['old_total']
    control = _row(db, CONTROL_ID)
    assert control['discount'] == CONTROL_EXPECTED['discount']
    assert control['total'] == CONTROL_EXPECTED['total']


# ── forward ──────────────────────────────────────────────────────────────

def test_forward_corrects_mechanism_a_rows(db):
    _apply(db, MIG_184)
    for row_id in (MECH_A_ID, MECH_A_ID2):
        exp = EXPECTED[row_id]
        row = _row(db, row_id)
        assert row['discount'] == exp['correct_discount']
        assert row['total'] == exp['correct_total']
        # net/qty/unit_price/product_id never touched by this migration —
        # net and qty pinned explicitly (the postcondition asserts both
        # inside the migration itself; pin them here too, not just via the
        # whole-table sweep in test_forward_touches_no_row_count_no_other_column)
        assert row['net'] == exp['net']
        assert row['qty'] == exp['qty']


def test_forward_restores_mechanism_b_discount_not_blank(db):
    """id 36661 is the ONE row where the correct discount is a real baht
    value, not blank — the migration must not treat every row the same way."""
    _apply(db, MIG_184)
    row = _row(db, MECH_B_ID)
    exp = EXPECTED[MECH_B_ID]
    assert row['discount'] == exp['correct_discount']
    assert row['total'] == exp['correct_total']
    assert row['net'] == exp['net']
    assert row['qty'] == exp['qty']


def test_forward_leaves_an_already_faithful_row_untouched(db):
    """CONTROL — a line that was never mis-parsed must not move a single
    byte, proving the migration's WHERE clause is scoped, not blanket."""
    _apply(db, MIG_184)
    row = _row(db, CONTROL_ID)
    assert row['discount'] == CONTROL_EXPECTED['discount']
    assert row['total'] == CONTROL_EXPECTED['total']
    assert row['net'] == CONTROL_EXPECTED['net']
    assert row['qty'] == CONTROL_EXPECTED['qty']


def test_forward_touches_no_row_count_no_other_column(db):
    before_count = _row_count(db)
    before_products = {r['id']: dict(r) for r in db.execute(
        "SELECT id, product_id, qty, unit_price, vat_type, date_iso, doc_no, bsn_code, net "
        "FROM sales_transactions"
    )}
    _apply(db, MIG_184)
    after_count = _row_count(db)
    assert after_count == before_count
    for row_id, before in before_products.items():
        after = dict(db.execute(
            "SELECT id, product_id, qty, unit_price, vat_type, date_iso, doc_no, bsn_code, net "
            "FROM sales_transactions WHERE id = ?", (row_id,)
        ).fetchone())
        assert after == before, f"row {row_id} moved a column this migration must never touch"


_AUDIT_MARK = "GH #525:%"


def _audit_counts(conn):
    total = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE table_name='sales_transactions'"
    ).fetchone()[0]
    marked = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE table_name='sales_transactions' "
        "AND change_reason LIKE ?", (_AUDIT_MARK,)
    ).fetchone()[0]
    return total, marked


def test_forward_writes_176_more_audit_rows_than_before(db):
    """DELTA, not an absolute count — a DB whose audit_log ALREADY carries
    184's own marked rows (e.g. a dev DB refreshed from a prod snapshot
    taken after 184 shipped: this test's own `db` fixture rolls back the
    DATA but audit_log is append-only, so those old rows survive) must not
    make this guard go red the moment the thing it guards is finally true.
    See test_forward_audit_delta_survives_pre_existing_184_history for the
    world where before_marked is already 176, not 0."""
    before_total, before_marked = _audit_counts(db)
    _apply(db, MIG_184)
    after_total, after_marked = _audit_counts(db)
    assert after_total == before_total + 176
    assert after_marked == before_marked + 176


def test_forward_audit_delta_survives_pre_existing_184_history(db):
    """Same guard as above, run in the world it must also hold in: seed 176
    marked audit rows BEFORE applying (standing in for a real prior run
    whose audit trail outlived a later rollback), confirm the seed landed
    (control), then assert the delta is still +176 — an absolute
    `after_marked == 176` would read 352 here and fail for no real reason."""
    for i in range(176):
        db.execute(
            "INSERT INTO audit_log (table_name, row_id, action, changed_fields, "
            "change_source, change_reason) VALUES "
            "('sales_transactions', ?, 'UPDATE', '{}', 'manual', "
            "'GH #525: pre-existing history from a prior real run')",
            (900000 + i,)
        )
    db.commit()
    before_total, before_marked = _audit_counts(db)
    assert before_marked == 176   # CONTROL — the seed actually landed

    _apply(db, MIG_184)

    after_total, after_marked = _audit_counts(db)
    assert after_total == before_total + 176
    assert after_marked == before_marked + 176   # 352, not a hardcoded 176


def test_forward_is_idempotent_second_run_changes_nothing(db):
    _apply(db, MIG_184)
    snapshot_after_first = {r['id']: dict(r) for r in db.execute(
        "SELECT id, discount, total FROM sales_transactions WHERE id IN ({})".format(
            ','.join('?' * len(EXPECTED))), tuple(EXPECTED))}
    audit_before_second = db.execute(
        "SELECT COUNT(*) FROM audit_log WHERE table_name='sales_transactions'"
    ).fetchone()[0]

    _apply(db, MIG_184)  # re-run against an already-corrected DB

    audit_after_second = db.execute(
        "SELECT COUNT(*) FROM audit_log WHERE table_name='sales_transactions'"
    ).fetchone()[0]
    assert audit_after_second == audit_before_second, "a re-run must change zero rows"
    for row_id, before in snapshot_after_first.items():
        after = dict(db.execute(
            "SELECT id, discount, total FROM sales_transactions WHERE id = ?", (row_id,)
        ).fetchone())
        assert after == before


# ── precondition (break-it-once, kept as a permanent guard) ────────────────

def test_precondition_aborts_on_unexpected_drift(db):
    """A row hand-edited to a THIRD value (neither old-wrong nor corrected)
    between this file being written and being applied must abort the WHOLE
    migration, not silently skip that one row or silently overwrite it."""
    db.execute(
        "UPDATE sales_transactions SET discount='999.99', total=1.0, "
        "change_token='pre-test-drift', change_source='manual', change_actor='test', "
        "change_reason='simulating unexpected third-state drift for this test' "
        "WHERE id=?", (MECH_A_ID,)
    )
    db.commit()

    with pytest.raises(sqlite3.IntegrityError, match='precondition FAILED'):
        db.executescript(MIG_184.read_text())
    db.rollback()

    # The drifted row is exactly as the drift left it — the aborted migration
    # touched nothing, on this row or any other.
    row = _row(db, MECH_A_ID)
    assert row['discount'] == '999.99'
    assert row['total'] == 1.0
    other = _row(db, MECH_A_ID2)
    assert other['discount'] == EXPECTED[MECH_A_ID2]['old_discount']
    assert other['total'] == EXPECTED[MECH_A_ID2]['old_total']
    with pytest.raises(sqlite3.OperationalError, match='no such table'):
        db.execute("SELECT COUNT(*) FROM migration_184_snapshot").fetchone()


def test_precondition_aborts_on_a_missing_row_does_not_silently_skip_it(db):
    """A DELETE'd row is a DIFFERENT drift shape from a hand-edited value —
    the migration header used to claim this was a no-op ("no-op for any id
    that does not exist"); an outside review proved that wrong by deleting a
    row and getting an ABORT. This pins the corrected, actually-shipped
    behaviour: missing is drift too, and the whole migration stops."""
    db.execute("DELETE FROM sales_transactions WHERE id=?", (MECH_B_ID,))
    db.commit()

    with pytest.raises(sqlite3.IntegrityError, match='precondition FAILED'):
        db.executescript(MIG_184.read_text())
    db.rollback()

    # Nothing else committed either — the aborted migration touched no row.
    assert db.execute(
        "SELECT COUNT(*) FROM sales_transactions WHERE id=?", (MECH_B_ID,)
    ).fetchone()[0] == 0   # still deleted, not resurrected
    other = _row(db, MECH_A_ID)
    assert other['discount'] == EXPECTED[MECH_A_ID]['old_discount']
    assert other['total'] == EXPECTED[MECH_A_ID]['old_total']
    with pytest.raises(sqlite3.OperationalError, match='no such table'):
        db.execute("SELECT COUNT(*) FROM migration_184_snapshot").fetchone()


# ── rollback ────────────────────────────────────────────────────────────────

def test_rollback_restores_the_original_wrong_values(db):
    _apply(db, MIG_184)
    _apply(db, ROLLBACK_184)
    for row_id, exp in EXPECTED.items():
        row = _row(db, row_id)
        assert row['discount'] == exp['old_discount']
        assert row['total'] == exp['old_total']
        assert row['net'] == exp['net']
        assert row['qty'] == exp['qty']
    with pytest.raises(sqlite3.OperationalError, match='no such table'):
        db.execute("SELECT COUNT(*) FROM migration_184_snapshot").fetchone()


def test_rollback_does_not_move_an_already_faithful_row(db):
    _apply(db, MIG_184)
    _apply(db, ROLLBACK_184)
    row = _row(db, CONTROL_ID)
    assert row['discount'] == CONTROL_EXPECTED['discount']
    assert row['total'] == CONTROL_EXPECTED['total']
    assert row['qty'] == CONTROL_EXPECTED['qty']
