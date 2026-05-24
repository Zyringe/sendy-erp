"""Migration 079 — audit_log UPDATE/DELETE triggers for `transactions`.

Background: mig 070 added only `audit_transactions_insert` ("append-only ledger"
rationale). A typo cleanup on 2026-05-25 exposed the gap — UPDATEs left no
audit trail. Mig 079 adds:
  - audit_transactions_update  (AFTER UPDATE, fires only on real diffs)
  - audit_transactions_delete  (BEFORE DELETE, captures all OLD values)

Tests verify both new triggers fire with the expected payload shape,
and that the WHEN-clause correctly skips no-op UPDATEs.
"""
import json
import os
import sqlite3

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_079 = os.path.join(REPO, "data", "migrations",
                       "079_audit_transactions_update_delete.sql")


def _ensure_mig_079(conn):
    """Apply mig 079 if not already in applied_migrations."""
    applied = {r[0] for r in conn.execute(
        "SELECT filename FROM applied_migrations").fetchall()}
    if "079_audit_transactions_update_delete.sql" not in applied:
        with open(MIG_079, encoding="utf-8") as f:
            conn.executescript(f.read())


def _new_txn(conn, ref_no="MIG079_TEST", note="probe"):
    """Insert a throwaway transaction (quantity_change=0 to avoid stock desync) → row id."""
    pid = conn.execute("SELECT id FROM products LIMIT 1").fetchone()[0]
    cur = conn.execute(
        "INSERT INTO transactions"
        " (product_id, txn_type, quantity_change, unit_mode, reference_no, note)"
        " VALUES (?, 'ADJUST', 0, 'unit', ?, ?)",
        (pid, ref_no, note),
    )
    conn.commit()
    return cur.lastrowid


def test_update_fires_audit_with_diff(tmp_db):
    """UPDATE on transactions creates audit_log row, action=UPDATE, with field diff."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_mig_079(conn)

    txn_id = _new_txn(conn, ref_no="MIG079_UPD", note="original")
    before = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE table_name='transactions' "
        "AND row_id=? AND action='UPDATE'", (txn_id,),
    ).fetchone()[0]

    conn.execute("UPDATE transactions SET note=? WHERE id=?", ("changed", txn_id))
    conn.commit()

    rows = conn.execute(
        "SELECT changed_fields FROM audit_log "
        "WHERE table_name='transactions' AND row_id=? AND action='UPDATE' "
        "ORDER BY id DESC", (txn_id,),
    ).fetchall()
    assert len(rows) == before + 1, "audit_log should have one new UPDATE row"
    diff = json.loads(rows[0][0])
    assert "note" in diff
    assert diff["note"] == ["original", "changed"]


def test_noop_update_does_not_fire(tmp_db):
    """UPDATE that doesn't change any tracked column should be filtered by WHEN clause."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_mig_079(conn)

    txn_id = _new_txn(conn, ref_no="MIG079_NOOP", note="same-same")
    before = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE table_name='transactions' "
        "AND row_id=? AND action='UPDATE'", (txn_id,),
    ).fetchone()[0]

    # UPDATE with identical values for all mutable columns
    conn.execute("UPDATE transactions SET note='same-same' WHERE id=?", (txn_id,))
    conn.commit()

    after = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE table_name='transactions' "
        "AND row_id=? AND action='UPDATE'", (txn_id,),
    ).fetchone()[0]
    assert after == before, "no-op UPDATE must not fire trigger (WHEN clause)"


def test_update_multi_field_diff(tmp_db):
    """UPDATE touching multiple tracked columns should capture all diffs in one payload."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_mig_079(conn)

    txn_id = _new_txn(conn, ref_no="MIG079_MULTI", note="n1")

    conn.execute(
        "UPDATE transactions SET note=?, reference_no=? WHERE id=?",
        ("n2", "MIG079_MULTI_v2", txn_id),
    )
    conn.commit()

    row = conn.execute(
        "SELECT changed_fields FROM audit_log "
        "WHERE table_name='transactions' AND row_id=? AND action='UPDATE' "
        "ORDER BY id DESC LIMIT 1", (txn_id,),
    ).fetchone()
    assert row is not None
    diff = json.loads(row[0])
    assert diff["note"] == ["n1", "n2"]
    assert diff["reference_no"] == ["MIG079_MULTI", "MIG079_MULTI_v2"]


def test_delete_fires_audit_with_old_values(tmp_db):
    """DELETE creates audit_log row, action=DELETE, with full OLD payload."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_mig_079(conn)

    txn_id = _new_txn(conn, ref_no="MIG079_DEL", note="delete-me")
    pid = conn.execute(
        "SELECT product_id FROM transactions WHERE id=?", (txn_id,),
    ).fetchone()[0]

    conn.execute("DELETE FROM transactions WHERE id=?", (txn_id,))
    conn.commit()

    row = conn.execute(
        "SELECT changed_fields FROM audit_log "
        "WHERE table_name='transactions' AND row_id=? AND action='DELETE'",
        (txn_id,),
    ).fetchone()
    assert row is not None, "BEFORE DELETE trigger must log row before removal"
    payload = json.loads(row[0])
    assert payload["product_id"] == pid
    assert payload["txn_type"] == "ADJUST"
    assert payload["quantity_change"] == 0
    assert payload["reference_no"] == "MIG079_DEL"
    assert payload["note"] == "delete-me"
    assert "created_at" in payload


def test_insert_still_logged(tmp_db):
    """Sanity: mig 070's INSERT trigger still fires (we didn't disturb it)."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_mig_079(conn)

    txn_id = _new_txn(conn, ref_no="MIG079_INS", note="insert-probe")

    row = conn.execute(
        "SELECT changed_fields FROM audit_log "
        "WHERE table_name='transactions' AND row_id=? AND action='INSERT'",
        (txn_id,),
    ).fetchone()
    assert row is not None
    payload = json.loads(row[0])
    assert payload["reference_no"] == "MIG079_INS"
