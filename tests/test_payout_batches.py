"""TDD tests for bank-deposit payout batch matcher.

Strategy:
- Use a MINIMAL in-memory SQLite DB with only the two tables the matcher needs
  (marketplace_orders with settlement columns + payout_batches from mig 105).
  This makes the tests fully self-contained and independent of the worktree's
  local DB schema state. The worktree DB can be stale/older; these tests don't
  care.
- Fixture `batch_db` builds a deterministic multi-order same-settled_at dataset
  so the straddle-boundary-day case is REAL (not masked by one-order-per-day).

Covered:
1. Clean week, no straddle → exact prefix match assigns all.
2. Straddle boundary day — prefix hits target exactly on a subset of the day.
3. Straddle boundary day — prefix does NOT hit target → no_exact_match, nothing assigned.
4. Negative payout (refund) included in accumulating sum.
5. Re-run idempotency — assigning twice doesn't move already-batched orders.
6. Manual assign path assigns exactly the given order_sns.
7. get_deposit_batch_report tie flag ✓ when Σ==deposit_amount, ✗ otherwise;
   unbatched bucket excludes batched orders.
8. create_baseline_batch absorbs only orders with settled_at <= cutoff.
9. After baseline, assign_orders_to_batch matches the next deposit's prefix over
   post-baseline orders only (mirrors the real June-2 deposit case).
10. Baseline sum_absorbed == Σ of absorbed orders' actual_payout.
11. get_deposit_batch_report labels baseline rows is_baseline=1, excludes their
    orders from the unbatched bucket.
12. Deleting the baseline frees its orders back to unbatched.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3
import pytest
import models


# ── Minimal schema for self-contained tests ───────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS marketplace_orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    platform         TEXT    NOT NULL,
    order_sn         TEXT    NOT NULL,
    status           TEXT,
    buyer_name       TEXT,
    buyer_phone      TEXT,
    ship_address     TEXT,
    order_date       TEXT,
    paid_date        TEXT,
    item_total       REAL,
    marketplace_fee  REAL,
    payout           REAL,
    currency         TEXT    NOT NULL DEFAULT 'THB',
    source_file      TEXT,
    raw_json         TEXT,
    first_synced_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    last_synced_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    actual_payout    REAL,
    settled_at       TEXT,
    settlement_source TEXT,
    payout_batch_id  INTEGER,
    UNIQUE(platform, order_sn)
);

CREATE TABLE IF NOT EXISTS payout_batches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    deposit_date   TEXT    NOT NULL,
    deposit_amount REAL    NOT NULL,
    bank_ref       TEXT,
    note           TEXT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    created_by     TEXT,
    is_baseline    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_marketplace_orders_payout_batch
    ON marketplace_orders(payout_batch_id);
"""


def _make_conn():
    """Return a fresh in-memory sqlite3 connection with the minimal schema."""
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


# ── Shared fixture ────────────────────────────────────────────────────────────

def _insert_order(conn, order_sn, actual_payout, settled_at, platform='shopee'):
    conn.execute(
        """INSERT INTO marketplace_orders
               (platform, order_sn, status, item_total, marketplace_fee,
                payout, currency, actual_payout, settled_at)
           VALUES (?, ?, 'สำเร็จแล้ว', ?, 0, ?, 'THB', ?, ?)""",
        (platform, order_sn, actual_payout, actual_payout, actual_payout, settled_at)
    )


@pytest.fixture
def batch_db():
    """In-memory DB with mig 105 schema and a deterministic order set.

    Layout (all Shopee, sorted settled_at ASC then order_sn ASC):
        ORDER_A1  2026-06-02  1000.00
        ORDER_A2  2026-06-02   500.00
        ORDER_A3  2026-06-02   285.00   ← straddle day: A1+A2 = deposit 1; A3 alone or with B1 straddled
        ORDER_B1  2026-06-09  2000.00
        ORDER_B2  2026-06-09  1500.00
        ORDER_REF 2026-06-09  -100.00   ← refund (negative payout)

    Deposit 1 target: 1785.00  (A1+A2+A3 exactly — clean prefix match)
    Deposit 2 target: 3400.00  (B1+B2+REF exactly = 2000+1500-100)
    Straddle scenario: deposit of 1500.00 matches [A1+A2] only; A3 stays unbatched.
    No-match scenario: deposit of 999.00 overshoots after A1 (1000 > 999) → no match.
    """
    conn = _make_conn()

    _insert_order(conn, 'ORDER_A1', 1000.00, '2026-06-02')
    _insert_order(conn, 'ORDER_A2',  500.00, '2026-06-02')
    _insert_order(conn, 'ORDER_A3',  285.00, '2026-06-02')
    _insert_order(conn, 'ORDER_B1', 2000.00, '2026-06-09')
    _insert_order(conn, 'ORDER_B2', 1500.00, '2026-06-09')
    _insert_order(conn, 'ORDER_REF', -100.00, '2026-06-09')
    conn.commit()
    return conn


# ── Test 1: clean week, exact prefix match assigns all ───────────────────────

def test_clean_week_exact_match_assigns_orders(batch_db):
    """A target that is the exact running sum of the first N orders assigns those orders."""
    conn = batch_db
    batch_id = models.create_payout_batch('2026-06-06', 1785.00, conn=conn)

    result = models.assign_orders_to_batch(batch_id, 1785.00, conn=conn)

    assert result['status'] == 'matched'
    assert set(result['order_ids']) == {
        conn.execute("SELECT id FROM marketplace_orders WHERE order_sn='ORDER_A1'").fetchone()[0],
        conn.execute("SELECT id FROM marketplace_orders WHERE order_sn='ORDER_A2'").fetchone()[0],
        conn.execute("SELECT id FROM marketplace_orders WHERE order_sn='ORDER_A3'").fetchone()[0],
    }
    assert result['n'] == 3
    assert abs(result['sum'] - 1785.00) < 0.01

    # DB rows updated
    rows = conn.execute(
        "SELECT order_sn FROM marketplace_orders WHERE payout_batch_id = ?", (batch_id,)
    ).fetchall()
    assert {r[0] for r in rows} == {'ORDER_A1', 'ORDER_A2', 'ORDER_A3'}


# ── Test 2: straddle boundary day — prefix hits exact subset ─────────────────

def test_straddle_boundary_exact_prefix_assigns_subset(batch_db):
    """Deposit of 1500.00 hits A1+A2 exactly even though A3 is on the same settled_at date."""
    conn = batch_db
    batch_id = models.create_payout_batch('2026-06-06', 1500.00, conn=conn)

    result = models.assign_orders_to_batch(batch_id, 1500.00, conn=conn)

    assert result['status'] == 'matched'
    a1_id = conn.execute("SELECT id FROM marketplace_orders WHERE order_sn='ORDER_A1'").fetchone()[0]
    a2_id = conn.execute("SELECT id FROM marketplace_orders WHERE order_sn='ORDER_A2'").fetchone()[0]
    assert set(result['order_ids']) == {a1_id, a2_id}
    assert result['n'] == 2
    # A3 remains unbatched
    a3_batch = conn.execute(
        "SELECT payout_batch_id FROM marketplace_orders WHERE order_sn='ORDER_A3'"
    ).fetchone()[0]
    assert a3_batch is None


# ── Test 3: no exact prefix → no_exact_match, nothing assigned ───────────────

def test_no_exact_prefix_returns_candidates_assigns_nothing(batch_db):
    """999.00 overshoots on the first order (1000 > 999); nothing is assigned."""
    conn = batch_db
    batch_id = models.create_payout_batch('2026-06-06', 999.00, conn=conn)

    result = models.assign_orders_to_batch(batch_id, 999.00, conn=conn)

    assert result['status'] == 'no_exact_match'
    # Candidates list is returned for UI
    assert 'candidates' in result
    assert len(result['candidates']) > 0
    # Nothing assigned
    assigned = conn.execute(
        "SELECT COUNT(*) FROM marketplace_orders WHERE payout_batch_id = ?", (batch_id,)
    ).fetchone()[0]
    assert assigned == 0
    # closest_n / closest_sum present
    assert 'closest_n' in result
    assert 'closest_sum' in result


# ── M1: match_orders_to_amount is a pure dry-run (no orphan batch) ────────────

def test_match_orders_to_amount_writes_nothing_on_match(batch_db):
    """Dry-run matcher returns the matched ids but must NOT set payout_batch_id
    or create any payout_batches row — the batch is created only on commit."""
    conn = batch_db
    result = models.match_orders_to_amount(1785.00, conn=conn)
    assert result['status'] == 'matched'
    assert result['n'] == 3
    assert conn.execute(
        "SELECT COUNT(*) FROM marketplace_orders WHERE payout_batch_id IS NOT NULL"
    ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM payout_batches").fetchone()[0] == 0


def test_match_orders_to_amount_writes_nothing_on_no_match(batch_db):
    """M1 invariant: a no_exact_match dry-run leaves zero orphan state — no order
    assigned and no batch row. The create route calls this BEFORE creating a
    batch, so an abandoned no-match leaves nothing behind."""
    conn = batch_db
    result = models.match_orders_to_amount(999.00, conn=conn)
    assert result['status'] == 'no_exact_match'
    assert conn.execute(
        "SELECT COUNT(*) FROM marketplace_orders WHERE payout_batch_id IS NOT NULL"
    ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM payout_batches").fetchone()[0] == 0


# ── Test 4: negative payout (refund) included in sum ─────────────────────────

def test_negative_payout_refund_included_in_sum(batch_db):
    """B1+B2+REF = 2000+1500-100 = 3400 — the refund is part of the running sum."""
    conn = batch_db
    # First batch to consume A1+A2+A3 so B rows are truly the only candidates
    batch_id_a = models.create_payout_batch('2026-06-06', 1785.00, conn=conn)
    models.assign_orders_to_batch(batch_id_a, 1785.00, conn=conn)

    batch_id_b = models.create_payout_batch('2026-06-13', 3400.00, conn=conn)
    result = models.assign_orders_to_batch(batch_id_b, 3400.00, conn=conn)

    assert result['status'] == 'matched'
    assert result['n'] == 3
    assert abs(result['sum'] - 3400.00) < 0.01
    # REF (negative) is included
    sns = {
        conn.execute("SELECT order_sn FROM marketplace_orders WHERE id=?", (oid,)).fetchone()[0]
        for oid in result['order_ids']
    }
    assert 'ORDER_REF' in sns


# ── Test 5: idempotency — assigning twice doesn't double-count ───────────────

def test_assign_idempotent(batch_db):
    """Re-running assign on the same batch does not move already-assigned orders."""
    conn = batch_db
    batch_id = models.create_payout_batch('2026-06-06', 1785.00, conn=conn)

    models.assign_orders_to_batch(batch_id, 1785.00, conn=conn)

    # Second run — candidates are now empty (all assigned) or the same batch
    # Regardless: the already-assigned orders must not move
    result2 = models.assign_orders_to_batch(batch_id, 1785.00, conn=conn)
    # If all candidates are exhausted, result is matched with n=0 (empty prefix = 0 == 1785? no)
    # Spec: candidates = payout_batch_id IS NULL, so re-run sees no candidates → no_exact_match or matched n=0
    # Either way: the original 3 orders stay on batch_id
    rows = conn.execute(
        "SELECT order_sn FROM marketplace_orders WHERE payout_batch_id = ?", (batch_id,)
    ).fetchall()
    assert {r[0] for r in rows} == {'ORDER_A1', 'ORDER_A2', 'ORDER_A3'}


# ── Test 6: manual assign path ───────────────────────────────────────────────

def test_manual_assign_assigns_exactly_given_order_sns(batch_db):
    """assign_orders_manual assigns exactly the listed order_sns to the batch."""
    conn = batch_db
    batch_id = models.create_payout_batch('2026-06-06', 1785.00, conn=conn)

    models.assign_orders_manual(batch_id, ['ORDER_A1', 'ORDER_A3'], conn=conn)

    rows = conn.execute(
        "SELECT order_sn FROM marketplace_orders WHERE payout_batch_id = ?", (batch_id,)
    ).fetchall()
    assert {r[0] for r in rows} == {'ORDER_A1', 'ORDER_A3'}
    # A2 untouched
    a2_batch = conn.execute(
        "SELECT payout_batch_id FROM marketplace_orders WHERE order_sn='ORDER_A2'"
    ).fetchone()[0]
    assert a2_batch is None


def test_manual_assign_idempotent(batch_db):
    """Calling assign_orders_manual twice with the same sns is safe (no duplicate effect)."""
    conn = batch_db
    batch_id = models.create_payout_batch('2026-06-06', 1785.00, conn=conn)

    models.assign_orders_manual(batch_id, ['ORDER_A1', 'ORDER_A2'], conn=conn)
    models.assign_orders_manual(batch_id, ['ORDER_A1', 'ORDER_A2'], conn=conn)

    rows = conn.execute(
        "SELECT order_sn FROM marketplace_orders WHERE payout_batch_id = ?", (batch_id,)
    ).fetchall()
    assert len(rows) == 2  # not 4


# ── Test 7: get_deposit_batch_report ─────────────────────────────────────────

def test_deposit_batch_report_tie_flag_and_unbatched(batch_db):
    """Tie flag ✓ when Σ actual_payout == deposit_amount; unbatched excludes batched orders."""
    conn = batch_db

    # Create batch matching A1+A2+A3 exactly
    batch_id = models.create_payout_batch('2026-06-06', 1785.00, bank_ref='REF001', conn=conn)
    models.assign_orders_to_batch(batch_id, 1785.00, conn=conn)

    report = models.get_deposit_batch_report(conn=conn)

    matched_batch = next(b for b in report['batches'] if b['id'] == batch_id)
    assert matched_batch['order_count'] == 3
    assert abs(matched_batch['sum_payout'] - 1785.00) < 0.01
    assert matched_batch['tied'] is True  # Σ == deposit_amount

    # Unbatched: B1, B2, REF (not batched)
    unbatched_sns = {u['order_sn'] for u in report['unbatched']}
    assert 'ORDER_B1' in unbatched_sns
    assert 'ORDER_B2' in unbatched_sns
    assert 'ORDER_REF' in unbatched_sns
    # Batched orders NOT in unbatched
    assert 'ORDER_A1' not in unbatched_sns
    assert 'ORDER_A2' not in unbatched_sns
    assert 'ORDER_A3' not in unbatched_sns


def test_deposit_batch_report_tie_flag_false_when_mismatch(batch_db):
    """Tie flag ✗ when Σ actual_payout != deposit_amount (manual assign with wrong sum)."""
    conn = batch_db

    # Create batch for 1785 but manually assign only A1 (1000) → mismatch
    batch_id = models.create_payout_batch('2026-06-06', 1785.00, conn=conn)
    models.assign_orders_manual(batch_id, ['ORDER_A1'], conn=conn)

    report = models.get_deposit_batch_report(conn=conn)

    b = next(b for b in report['batches'] if b['id'] == batch_id)
    assert b['tied'] is False


def test_unassign_batch_clears_orders_and_deletes_batch(batch_db):
    """unassign_batch sets payout_batch_id=NULL for orders and deletes the batch row."""
    conn = batch_db
    batch_id = models.create_payout_batch('2026-06-06', 1785.00, conn=conn)
    models.assign_orders_to_batch(batch_id, 1785.00, conn=conn)

    models.unassign_batch(batch_id, conn=conn)

    # Batch row gone
    row = conn.execute("SELECT id FROM payout_batches WHERE id=?", (batch_id,)).fetchone()
    assert row is None
    # Orders back to NULL
    still_assigned = conn.execute(
        "SELECT COUNT(*) FROM marketplace_orders WHERE payout_batch_id=?", (batch_id,)
    ).fetchone()[0]
    assert still_assigned == 0


def test_unassign_batch_reruns_safely(batch_db):
    """Calling unassign_batch on an already-deleted batch id raises no error."""
    conn = batch_db
    batch_id = models.create_payout_batch('2026-06-06', 1785.00, conn=conn)
    models.unassign_batch(batch_id, conn=conn)
    # Second call — batch row already gone; should not raise
    models.unassign_batch(batch_id, conn=conn)


# ── Baseline / ยอดยกมา tests (TDD — written before implementation) ──────────

@pytest.fixture
def baseline_db():
    """In-memory DB mirroring the real historical-backlog situation.

    Two "old" orders (pre-tracking, settled before 2026-06-01) and two
    "new" orders (post-baseline, the June deposits to actually track).

        OLD_1  2026-01-15   800.00   ← pre-tracking backlog
        OLD_2  2026-05-20  1200.00   ← pre-tracking backlog
        NEW_1  2026-06-02  1000.00   ← first tracked deposit includes this
        NEW_2  2026-06-02   500.00   ← first tracked deposit includes this

    Baseline cutoff: 2026-05-25 → absorbs OLD_1 + OLD_2 (sum = 2000.00).
    First real deposit target: 1500.00 = NEW_1 + NEW_2 exactly.
    """
    conn = _make_conn()
    _insert_order(conn, 'OLD_1', 800.00,  '2026-01-15')
    _insert_order(conn, 'OLD_2', 1200.00, '2026-05-20')
    _insert_order(conn, 'NEW_1', 1000.00, '2026-06-02')
    _insert_order(conn, 'NEW_2', 500.00,  '2026-06-02')
    conn.commit()
    return conn


def test_baseline_absorbs_only_orders_on_or_before_cutoff(baseline_db):
    """create_baseline_batch assigns orders with settled_at <= cutoff; leaves later ones free."""
    conn = baseline_db
    result = models.create_baseline_batch('2026-05-25', conn=conn)

    assert result['n_absorbed'] == 2
    # OLD_1 and OLD_2 absorbed
    absorbed = conn.execute(
        "SELECT order_sn FROM marketplace_orders WHERE payout_batch_id = ?",
        (result['batch_id'],)
    ).fetchall()
    assert {r[0] for r in absorbed} == {'OLD_1', 'OLD_2'}
    # NEW_1 and NEW_2 still free
    free = conn.execute(
        "SELECT order_sn FROM marketplace_orders WHERE payout_batch_id IS NULL"
    ).fetchall()
    assert {r[0] for r in free} == {'NEW_1', 'NEW_2'}


def test_baseline_sum_absorbed_equals_sum_of_orders(baseline_db):
    """sum_absorbed == Σ actual_payout of absorbed orders (800 + 1200 = 2000)."""
    conn = baseline_db
    result = models.create_baseline_batch('2026-05-25', conn=conn)
    assert abs(result['sum_absorbed'] - 2000.00) < 0.01


def test_baseline_then_assign_matches_post_baseline_only(baseline_db):
    """After a baseline, assign_orders_to_batch sees only post-baseline orders.

    Mirrors the real case: 415 old orders consumed by baseline; the 2-June
    deposit (1500) matches exactly on NEW_1+NEW_2 without touching the backlog.
    """
    conn = baseline_db
    models.create_baseline_batch('2026-05-25', conn=conn)

    batch_id = models.create_payout_batch('2026-06-06', 1500.00, conn=conn)
    result = models.assign_orders_to_batch(batch_id, 1500.00, conn=conn)

    assert result['status'] == 'matched'
    assert result['n'] == 2
    assert abs(result['sum'] - 1500.00) < 0.01
    assigned_sns = {
        conn.execute("SELECT order_sn FROM marketplace_orders WHERE id=?", (oid,)).fetchone()[0]
        for oid in result['order_ids']
    }
    assert assigned_sns == {'NEW_1', 'NEW_2'}


def test_get_deposit_batch_report_labels_baseline_row(baseline_db):
    """Baseline batch appears in report with is_baseline=True; its orders not in unbatched."""
    conn = baseline_db
    result = models.create_baseline_batch('2026-05-25', conn=conn)
    bid = result['batch_id']

    report = models.get_deposit_batch_report(conn=conn)

    baseline_batch = next(b for b in report['batches'] if b['id'] == bid)
    assert baseline_batch['is_baseline'] is True
    # No bank-tie assertion on baseline rows (deposit_amount == sum absorbed, but
    # the point is the template doesn't show ✓/✗ for them)

    # Its orders not in unbatched
    unbatched_sns = {u['order_sn'] for u in report['unbatched']}
    assert 'OLD_1' not in unbatched_sns
    assert 'OLD_2' not in unbatched_sns
    # Post-baseline orders ARE in unbatched (not yet in a real deposit batch)
    assert 'NEW_1' in unbatched_sns
    assert 'NEW_2' in unbatched_sns


def test_normal_batches_have_is_baseline_false(baseline_db):
    """Regular payout batches report is_baseline=False."""
    conn = baseline_db
    batch_id = models.create_payout_batch('2026-06-06', 1500.00, conn=conn)
    models.assign_orders_to_batch(batch_id, 1500.00, conn=conn)

    report = models.get_deposit_batch_report(conn=conn)
    b = next(b for b in report['batches'] if b['id'] == batch_id)
    assert b['is_baseline'] is False


def test_deleting_baseline_frees_orders_to_unbatched(baseline_db):
    """unassign_batch on the baseline row frees OLD_1/OLD_2 back to unbatched."""
    conn = baseline_db
    result = models.create_baseline_batch('2026-05-25', conn=conn)
    bid = result['batch_id']

    models.unassign_batch(bid, conn=conn)

    # Batch row gone
    assert conn.execute("SELECT id FROM payout_batches WHERE id=?", (bid,)).fetchone() is None
    # Orders free again
    free = conn.execute(
        "SELECT order_sn FROM marketplace_orders WHERE payout_batch_id IS NULL"
    ).fetchall()
    assert {r[0] for r in free} == {'OLD_1', 'OLD_2', 'NEW_1', 'NEW_2'}
