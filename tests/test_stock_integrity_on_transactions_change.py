"""Migration 080 — stock_levels integrity triggers on transactions UPDATE/DELETE.

Mig 070 (audit_log INSERT-only on transactions) implicitly relied on
"append-only ledger". When a typo cleanup on 2026-05-25 ran ad-hoc UPDATEs,
the audit gap was exposed (closed by mig 079) but stock_levels would also
have drifted silently if `quantity_change` or `product_id` had been touched.

Mig 080 adds:
  - after_transaction_update — fires on product_id/quantity_change diffs only
  - after_transaction_delete — reverses OLD.quantity_change on OLD.product_id

Tests verify stock_levels stays consistent with SUM(quantity_change) under
each mutation pattern.
"""
import os
import sqlite3

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_080 = os.path.join(REPO, "data", "migrations",
                       "080_stock_integrity_on_transactions_change.sql")


def _ensure_mig_080(conn):
    applied = {r[0] for r in conn.execute(
        "SELECT filename FROM applied_migrations").fetchall()}
    if "080_stock_integrity_on_transactions_change.sql" not in applied:
        with open(MIG_080, encoding="utf-8") as f:
            conn.executescript(f.read())


def _stock_of(conn, pid):
    row = conn.execute(
        "SELECT quantity FROM stock_levels WHERE product_id=?", (pid,),
    ).fetchone()
    return row[0] if row else 0


def _new_txn(conn, pid, qty, ref="MIG080_TEST"):
    cur = conn.execute(
        "INSERT INTO transactions"
        " (product_id, txn_type, quantity_change, unit_mode, reference_no, note)"
        " VALUES (?, 'ADJUST', ?, 'unit', ?, 'mig080 test')",
        (pid, qty, ref),
    )
    conn.commit()
    return cur.lastrowid


def test_update_quantity_change_delta_adjusts_stock(tmp_db):
    """UPDATE that changes quantity_change should delta-adjust stock_levels."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_mig_080(conn)

    pid = conn.execute("SELECT id FROM products LIMIT 1").fetchone()[0]
    before = _stock_of(conn, pid)
    txn_id = _new_txn(conn, pid, 10)  # stock += 10
    assert _stock_of(conn, pid) == before + 10

    # UPDATE quantity_change 10 → 25 (delta +15)
    conn.execute("UPDATE transactions SET quantity_change=25 WHERE id=?", (txn_id,))
    conn.commit()
    assert _stock_of(conn, pid) == before + 25, "stock should reflect new quantity_change"

    # Cleanup
    conn.execute("DELETE FROM transactions WHERE id=?", (txn_id,))
    conn.commit()
    assert _stock_of(conn, pid) == before, "DELETE should restore stock"


def test_update_product_id_moves_stock(tmp_db):
    """UPDATE that changes product_id should move stock from OLD to NEW product."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_mig_080(conn)

    pids = [r[0] for r in conn.execute(
        "SELECT id FROM products ORDER BY id LIMIT 2").fetchall()]
    pid_a, pid_b = pids[0], pids[1]
    a_before = _stock_of(conn, pid_a)
    b_before = _stock_of(conn, pid_b)

    txn_id = _new_txn(conn, pid_a, 7)  # +7 on A
    assert _stock_of(conn, pid_a) == a_before + 7
    assert _stock_of(conn, pid_b) == b_before

    # Move the row to product B
    conn.execute("UPDATE transactions SET product_id=? WHERE id=?", (pid_b, txn_id))
    conn.commit()
    assert _stock_of(conn, pid_a) == a_before, "A should revert"
    assert _stock_of(conn, pid_b) == b_before + 7, "B should gain"

    # Cleanup
    conn.execute("DELETE FROM transactions WHERE id=?", (txn_id,))
    conn.commit()
    assert _stock_of(conn, pid_a) == a_before
    assert _stock_of(conn, pid_b) == b_before


def test_update_product_id_and_quantity_simultaneously(tmp_db):
    """UPDATE that changes both product_id and quantity_change — applies correctly to each."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_mig_080(conn)

    pids = [r[0] for r in conn.execute(
        "SELECT id FROM products ORDER BY id LIMIT 2").fetchall()]
    pid_a, pid_b = pids[0], pids[1]
    a_before = _stock_of(conn, pid_a)
    b_before = _stock_of(conn, pid_b)

    txn_id = _new_txn(conn, pid_a, 5)
    conn.execute(
        "UPDATE transactions SET product_id=?, quantity_change=? WHERE id=?",
        (pid_b, 12, txn_id),
    )
    conn.commit()

    assert _stock_of(conn, pid_a) == a_before, "A reverted to pre-insert state"
    assert _stock_of(conn, pid_b) == b_before + 12, "B gained the NEW qty (not OLD)"

    # Cleanup
    conn.execute("DELETE FROM transactions WHERE id=?", (txn_id,))
    conn.commit()


def test_update_note_only_does_not_touch_stock(tmp_db):
    """UPDATE that only changes `note` should not modify stock_levels (WHEN filter)."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_mig_080(conn)

    pid = conn.execute("SELECT id FROM products LIMIT 1").fetchone()[0]
    before = _stock_of(conn, pid)
    txn_id = _new_txn(conn, pid, 4)
    after_insert = _stock_of(conn, pid)
    assert after_insert == before + 4

    conn.execute("UPDATE transactions SET note='new note' WHERE id=?", (txn_id,))
    conn.commit()
    assert _stock_of(conn, pid) == after_insert, "note UPDATE must not change stock"

    # Cleanup
    conn.execute("DELETE FROM transactions WHERE id=?", (txn_id,))
    conn.commit()


def test_delete_reverses_stock(tmp_db):
    """DELETE on a transactions row should reverse its quantity_change effect."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_mig_080(conn)

    pid = conn.execute("SELECT id FROM products LIMIT 1").fetchone()[0]
    before = _stock_of(conn, pid)
    txn_id = _new_txn(conn, pid, -8)  # negative qty (OUT-style ADJUST)
    assert _stock_of(conn, pid) == before - 8

    conn.execute("DELETE FROM transactions WHERE id=?", (txn_id,))
    conn.commit()
    assert _stock_of(conn, pid) == before, "DELETE should fully reverse the effect"


def test_insert_still_updates_stock(tmp_db):
    """Regression: mig 080 must not disturb the canonical after_transaction_insert trigger."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_mig_080(conn)

    pid = conn.execute("SELECT id FROM products LIMIT 1").fetchone()[0]
    before = _stock_of(conn, pid)
    txn_id = _new_txn(conn, pid, 3)
    assert _stock_of(conn, pid) == before + 3

    # Cleanup
    conn.execute("DELETE FROM transactions WHERE id=?", (txn_id,))
    conn.commit()


def test_stock_levels_invariant_under_combined_mutations(tmp_db):
    """End-to-end: SUM(quantity_change) GROUP BY product_id stays equal to stock_levels.quantity
    across INSERT → UPDATE → DELETE sequence on the same row."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_mig_080(conn)

    pid = conn.execute("SELECT id FROM products LIMIT 1").fetchone()[0]

    def assert_invariant():
        actual = _stock_of(conn, pid)
        expected = conn.execute(
            "SELECT COALESCE(SUM(quantity_change),0) FROM transactions WHERE product_id=?",
            (pid,),
        ).fetchone()[0]
        assert actual == expected, f"drift: stock_levels={actual} vs ledger_sum={expected}"

    assert_invariant()
    txn_id = _new_txn(conn, pid, 50)
    assert_invariant()

    conn.execute("UPDATE transactions SET quantity_change=20 WHERE id=?", (txn_id,))
    conn.commit()
    assert_invariant()

    conn.execute("UPDATE transactions SET quantity_change=-5 WHERE id=?", (txn_id,))
    conn.commit()
    assert_invariant()

    conn.execute("DELETE FROM transactions WHERE id=?", (txn_id,))
    conn.commit()
    assert_invariant()
