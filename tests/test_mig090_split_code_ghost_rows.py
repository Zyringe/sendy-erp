"""Migration 090 — remove split-code ghost rows.

Three invoices (IV6900394-7, IV6900391-2, IV6900392-1) had batch-1 ghost
sales_transactions rows and linked OUT transactions that inflated revenue by
฿1,248 and overcounted stock decrements for products 128, 815, 436.

Mig 090 deletes the ghost rows using stable predicates (doc_no + bsn_code for
ST; reference_no + product_id + txn_type [+ quantity_change for product 436]
for transactions). The after_transaction_delete trigger (mig 080) auto-corrects
stock_levels on each DELETE — no manual stock recalc.

Tests verify:
  - Ghost ST rows absent after mig 090
  - Ghost transaction rows absent after mig 090
  - Canonical ST rows (batch 18) intact
  - Canonical transaction rows intact (especially product 436's canonical -30 row)
  - stock_levels for products 128, 815, 436 increase by the reversed amounts
  - Idempotency: re-run on already-cleaned DB is a no-op
"""
import os
import sqlite3

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_090      = os.path.join(REPO, "data", "migrations",
                            "090_remove_split_code_ghost_rows.sql")
ROLLBACK_090 = os.path.join(REPO, "data", "migrations",
                            "090_remove_split_code_ghost_rows.rollback.sql")

# Ghost rows (stable predicates — NOT raw autoincrement ids)
GHOST_ST = [
    ("IV6900394-7", "041ม2761"),
    ("IV6900391-2", "556ห7000"),
    ("IV6900392-1", "999อ1501"),
]

# Canonical ST rows (batch 18 — must survive mig 090)
CANONICAL_ST = [
    ("IV6900394-7", "041ม2760"),
    ("IV6900391-2", "556ห7002"),
    ("IV6900392-1", "999อ1500"),
]

# Ghost transactions: (reference_no, product_id, txn_type, quantity_change)
GHOST_TXNS = [
    ("IV6900394-7", 128, "OUT", -24),
    ("IV6900391-2", 815, "OUT", -1),
    ("IV6900392-1", 436, "OUT", -5),
]

# Canonical transactions that share doc_no but must NOT be deleted
CANONICAL_TXNS = [
    ("IV6900391-2", 825, "OUT", -1),   # product 825, not 815
    ("IV6900394-7", 148, "OUT", -24),  # product 148, not 128
    ("IV6900392-1", 436, "OUT", -30),  # same product 436 but qty=-30 (canonical)
]

# Products whose stock increases when ghost OUT rows are removed
# (product_id, qty_change_reversed)
STOCK_DELTAS = {
    128: 24,   # ghost was -24
    815: 1,    # ghost was -1
    436: 5,    # ghost was -5
}


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


def _stock(conn, pid):
    row = conn.execute(
        "SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)
    ).fetchone()
    return row[0] if row else 0


def _ghost_st_count(conn):
    """Number of ghost ST rows still present."""
    total = 0
    for doc_no, bsn_code in GHOST_ST:
        total += conn.execute(
            "SELECT COUNT(*) FROM sales_transactions WHERE doc_no=? AND bsn_code=?",
            (doc_no, bsn_code)
        ).fetchone()[0]
    return total


def _canonical_st_count(conn):
    """Number of canonical ST rows present (must remain 3 throughout)."""
    total = 0
    for doc_no, bsn_code in CANONICAL_ST:
        total += conn.execute(
            "SELECT COUNT(*) FROM sales_transactions WHERE doc_no=? AND bsn_code=?",
            (doc_no, bsn_code)
        ).fetchone()[0]
    return total


def _ghost_txn_count(conn):
    """Number of ghost transaction rows still present."""
    total = 0
    for ref_no, pid, txn_type, qty in GHOST_TXNS:
        total += conn.execute(
            """SELECT COUNT(*) FROM transactions
               WHERE reference_no=? AND product_id=? AND txn_type=? AND quantity_change=?""",
            (ref_no, pid, txn_type, qty)
        ).fetchone()[0]
    return total


def _canonical_txn_count(conn):
    """Number of canonical transaction rows present (must remain 3)."""
    total = 0
    for ref_no, pid, txn_type, qty in CANONICAL_TXNS:
        total += conn.execute(
            """SELECT COUNT(*) FROM transactions
               WHERE reference_no=? AND product_id=? AND txn_type=? AND quantity_change=?""",
            (ref_no, pid, txn_type, qty)
        ).fetchone()[0]
    return total


def _reset_to_pre_mig(conn):
    """If mig 090 is already applied on the cloned live DB, roll it back so
    tests start from the pre-mig state."""
    applied = conn.execute(
        "SELECT 1 FROM applied_migrations WHERE filename='090_remove_split_code_ghost_rows.sql'"
    ).fetchone()
    if applied is not None:
        _apply(conn, ROLLBACK_090)
        conn.execute(
            "DELETE FROM applied_migrations WHERE filename='090_remove_split_code_ghost_rows.sql'"
        )
        conn.commit()


def _seed_ghost_rows(conn):
    """Insert the 3 ghost ST rows + 3 ghost transaction rows + the 2 canonical
    transaction rows that the rebuilt DB no longer holds (product 148/-24 and
    436/-30).  Idempotent: uses INSERT OR IGNORE on the ST ids and inserts
    transactions only when absent.

    This makes tests self-contained against the 2026-05-30 ledger rebuild which
    regenerated transactions from the already-cleaned sales_transactions, so the
    ghost rows no longer exist and the canonical txn state differs from pre-rebuild.
    """
    conn.execute("PRAGMA foreign_keys = OFF")

    # ── Ghost sales_transactions rows ────────────────────────────────────────
    # Use hard-coded ids (73, 295, 313) matching what the rollback SQL would
    # re-insert, so the test is consistent with the rollback script.
    conn.executescript("""
        INSERT OR IGNORE INTO sales_transactions
            (id, batch_id, date_iso, doc_no, doc_base, product_id, bsn_code,
             qty, unit, unit_price, vat_type, total, net, synced_to_stock)
        VALUES
            (73,  1, '2026-03-10', 'IV6900394-7', 'IV6900394', 128, '041ม2761',
             24, 'ตัว', 46.75, 0, 1122.0, 1122.0, 1),
            (295, 1, '2026-03-09', 'IV6900391-2', 'IV6900391', 815, '556ห7000',
              1, 'ตัว', 25.0,  0,   25.0,   25.0, 1),
            (313, 1, '2026-03-10', 'IV6900392-1', 'IV6900392', 436, '999อ1501',
              5, 'ตัว', 20.2,  0,  101.0,  101.0, 1);
    """)

    # ── Ghost transaction rows ─────────────────────────────────────────────────
    # Delete then re-insert to ensure exactly one copy of each ghost txn row.
    # Without this, _reset_to_pre_mig's rollback SQL (which uses bare INSERT, no
    # ON CONFLICT) may have already inserted a second copy if the row existed in
    # the rebuilt live DB, resulting in count > 3.
    for ref_no, pid, qty in [
        ("IV6900394-7", 128, -24),
        ("IV6900391-2", 815, -1),
        ("IV6900392-1", 436, -5),
    ]:
        conn.execute(
            "DELETE FROM transactions WHERE reference_no=? AND product_id=? "
            "AND txn_type='OUT' AND quantity_change=?",
            (ref_no, pid, qty)
        )
        conn.execute(
            "INSERT INTO transactions "
            "(product_id, txn_type, quantity_change, unit_mode, reference_no, note) "
            "VALUES (?, 'OUT', ?, 'unit', ?, 'BSN ขาย (ghost)')",
            (pid, qty, ref_no)
        )

    # ── Canonical transaction rows missing from rebuilt DB ────────────────────
    # product 148 / IV6900394-7 / qty=-24 (canonical; absent because the ST row
    # has product_id=128 in the rebuilt DB due to stale BSN mapping)
    # product 436 / IV6900392-1 / qty=-30 (canonical; absent post-rebuild)
    for ref_no, pid, qty in [
        ("IV6900394-7", 148, -24),
        ("IV6900392-1", 436, -30),
    ]:
        exists = conn.execute(
            "SELECT 1 FROM transactions WHERE reference_no=? AND product_id=? "
            "AND txn_type='OUT' AND quantity_change=?",
            (ref_no, pid, qty)
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO transactions "
                "(product_id, txn_type, quantity_change, unit_mode, reference_no, note) "
                "VALUES (?, 'OUT', ?, 'unit', ?, 'BSN ขาย (canonical)')",
                (pid, qty, ref_no)
            )

    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()


@pytest.fixture
def conn(tmp_db):
    c = sqlite3.connect(tmp_db)
    c.execute("PRAGMA foreign_keys = ON")
    _reset_to_pre_mig(c)
    yield c
    c.close()


@pytest.fixture
def conn_seeded(tmp_db):
    """Like `conn` but also seeds the ghost rows and missing canonical rows so
    tests are self-contained against the 2026-05-30 full ledger rebuild."""
    c = sqlite3.connect(tmp_db)
    c.execute("PRAGMA foreign_keys = ON")
    _reset_to_pre_mig(c)
    _seed_ghost_rows(c)
    yield c
    c.close()


# ── Pre-state guards ──────────────────────────────────────────────────────────

def test_pre_state_ghost_rows_exist(conn_seeded):
    """All 3 ghost ST rows and 3 ghost transaction rows must exist pre-mig."""
    assert _ghost_st_count(conn_seeded) == 3, "Expected 3 ghost ST rows pre-mig"
    assert _ghost_txn_count(conn_seeded) == 3, "Expected 3 ghost transaction rows pre-mig"


def test_pre_state_canonical_rows_exist(conn_seeded):
    """All 3 canonical ST rows and 3 canonical transaction rows exist pre-mig."""
    assert _canonical_st_count(conn_seeded) == 3, "Expected 3 canonical ST rows pre-mig"
    assert _canonical_txn_count(conn_seeded) == 3, "Expected 3 canonical transaction rows pre-mig"


# ── Forward migration ─────────────────────────────────────────────────────────

def test_ghost_st_rows_deleted(conn):
    """All 3 ghost sales_transactions rows are deleted by mig 090."""
    _apply(conn, MIG_090)
    assert _ghost_st_count(conn) == 0, "Ghost ST rows must be absent after mig 090"


def test_ghost_txn_rows_deleted(conn):
    """All 3 ghost transaction rows are deleted by mig 090."""
    _apply(conn, MIG_090)
    assert _ghost_txn_count(conn) == 0, "Ghost txn rows must be absent after mig 090"


def test_canonical_st_rows_intact(conn):
    """Canonical ST rows (batch 18) survive mig 090 untouched."""
    _apply(conn, MIG_090)
    assert _canonical_st_count(conn) == 3, "Canonical ST rows must all survive mig 090"


def test_canonical_txn_rows_intact(conn_seeded):
    """Canonical transaction rows survive mig 090 — especially product 436's -30 row."""
    _apply(conn_seeded, MIG_090)
    assert _canonical_txn_count(conn_seeded) == 3, "Canonical txn rows must all survive mig 090"


def test_product_436_canonical_txn_qty_unchanged(conn_seeded):
    """product 436 / IV6900392-1: canonical row with quantity_change=-30 must survive."""
    _apply(conn_seeded, MIG_090)
    row = conn_seeded.execute(
        """SELECT quantity_change FROM transactions
           WHERE reference_no='IV6900392-1' AND product_id=436 AND quantity_change=-30""",
    ).fetchone()
    assert row is not None, "Canonical txn for product 436 (qty=-30) was incorrectly deleted"
    assert row[0] == -30


def test_stock_increases_after_ghost_delete(conn_seeded):
    """stock_levels for products 128, 815, 436 each increase by the reversed
    ghost OUT quantity (trigger mig 080 fires on each DELETE).
    """
    before = {pid: _stock(conn_seeded, pid) for pid in STOCK_DELTAS}
    _apply(conn_seeded, MIG_090)
    after = {pid: _stock(conn_seeded, pid) for pid in STOCK_DELTAS}

    for pid, delta in STOCK_DELTAS.items():
        expected = before[pid] + delta
        assert after[pid] == expected, (
            f"product {pid}: stock before={before[pid]}, expected after={expected}, "
            f"got {after[pid]} (delta should be +{delta})"
        )


def test_other_products_stock_unchanged(conn):
    """A sample of other products must not have their stock touched by mig 090."""
    sample_pids = [1, 50, 100, 200, 500, 1000]
    before = {}
    for pid in sample_pids:
        row = conn.execute(
            "SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)
        ).fetchone()
        if row is not None:
            before[pid] = row[0]

    _apply(conn, MIG_090)

    for pid, prior_stock in before.items():
        cur_stock = conn.execute(
            "SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)
        ).fetchone()[0]
        assert cur_stock == prior_stock, (
            f"product {pid}: stock changed unexpectedly: {prior_stock} -> {cur_stock}"
        )


# ── Idempotency ───────────────────────────────────────────────────────────────

def test_rerun_is_noop(conn_seeded):
    """Applying mig 090 twice is a no-op on the second run.

    The EXISTS guard in each DELETE prevents any double-effect: once the ghost
    ST row is gone, the EXISTS sub-query returns false and nothing is deleted.
    """
    _apply(conn_seeded, MIG_090)

    # Snapshot stock after first run
    stock_after_first = {pid: _stock(conn_seeded, pid) for pid in STOCK_DELTAS}

    _apply(conn_seeded, MIG_090)  # second run

    # Ghost rows still absent
    assert _ghost_st_count(conn_seeded) == 0
    assert _ghost_txn_count(conn_seeded) == 0

    # Canonical rows still present
    assert _canonical_st_count(conn_seeded) == 3
    assert _canonical_txn_count(conn_seeded) == 3

    # Stock unchanged from after-first-run state (no double-add)
    for pid in STOCK_DELTAS:
        assert _stock(conn_seeded, pid) == stock_after_first[pid], (
            f"product {pid}: stock drifted on second run of mig 090"
        )


# ── Ledger self-consistency ───────────────────────────────────────────────────

def test_ledger_sum_equals_stock_after_mig(conn):
    """Post-mig: SUM(quantity_change) GROUP BY product_id = stock_levels.quantity
    for each of the 3 affected products.
    """
    _apply(conn, MIG_090)

    for pid in STOCK_DELTAS:
        ledger_sum = conn.execute(
            "SELECT COALESCE(SUM(quantity_change), 0) FROM transactions WHERE product_id=?",
            (pid,)
        ).fetchone()[0]
        sl_qty = _stock(conn, pid)
        assert ledger_sum == sl_qty, (
            f"product {pid}: ledger_sum={ledger_sum} != stock_levels={sl_qty} (drift)"
        )
