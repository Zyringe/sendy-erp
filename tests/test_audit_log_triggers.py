"""Migration 070 — audit_log triggers for transactions + received_payments.

audit_log (mig 023) shipped with INSERT/UPDATE/DELETE triggers on products,
customers, suppliers, regions, salespersons, companies, expense_*, PO*,
commission_*, listing_bundles, product_families, product_images,
product_price_tiers. The two highest-value finance tables that were NOT
covered are:
  - transactions (stock-movement ledger — append-only by convention)
  - received_payments (RE customer-payment imports)

Mig 070 adds:
  - audit_transactions_insert (INSERT only — ledger is append-only)
  - audit_received_payments_insert / _update / _delete

Tests verify each trigger fires with action+payload that matches the
existing audit_log column shape (table_name, row_id, action, changed_fields).
"""
import json
import os
import sqlite3

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_068 = os.path.join(REPO, "data", "migrations",
                       "068_drop_express_sales_brand_kind.sql")
MIG_069 = os.path.join(REPO, "data", "migrations",
                       "069_products_units_not_null.sql")
MIG_070 = os.path.join(REPO, "data", "migrations",
                       "070_audit_log_triggers.sql")
ROLLBACK_070 = os.path.join(REPO, "data", "migrations",
                            "070_audit_log_triggers.rollback.sql")


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


def _apply_chain(conn):
    """Apply 068 → 069 → 070, skipping any already in applied_migrations."""
    applied = {r[0] for r in conn.execute(
        "SELECT filename FROM applied_migrations").fetchall()}
    if "068_drop_express_sales_brand_kind.sql" not in applied:
        _apply(conn, MIG_068)
    if "069_products_units_not_null.sql" not in applied:
        _apply(conn, MIG_069)
    if "070_audit_log_triggers.sql" not in applied:
        _apply(conn, MIG_070)


# ── transactions ─────────────────────────────────────────────────────────────

def test_transactions_insert_logged(tmp_db):
    """INSERT into transactions creates an audit_log row, action=INSERT."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_chain(conn)

    pid = conn.execute("SELECT id FROM products LIMIT 1").fetchone()[0]
    before = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE table_name='transactions'"
    ).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO transactions "
        "(product_id, txn_type, quantity_change, unit_mode, "
        " reference_no, note) "
        "VALUES (?, 'ADJUST', 0, 'unit', 'AUDIT_TEST_MIG070', 'mig070 probe')",
        (pid,),
    )
    new_id = cur.lastrowid
    after = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE table_name='transactions'"
    ).fetchone()[0]
    assert after == before + 1

    row = conn.execute(
        "SELECT row_id, action, changed_fields FROM audit_log "
        "WHERE table_name='transactions' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] == new_id
    assert row[1] == "INSERT"
    payload = json.loads(row[2])
    assert payload["product_id"] == pid
    assert payload["txn_type"] == "ADJUST"
    assert payload["unit_mode"] == "unit"
    assert payload["reference_no"] == "AUDIT_TEST_MIG070"

    conn.execute("DELETE FROM transactions WHERE id = ?", (new_id,))
    conn.commit()
    conn.close()


# ── received_payments ────────────────────────────────────────────────────────

def _insert_rp(conn, re_no="RE_AUDIT_MIG070_TEST_1"):
    """Minimal valid received_payments INSERT. Returns (id, re_no)."""
    conn.execute("DELETE FROM received_payments WHERE re_no = ?", (re_no,))
    cur = conn.execute(
        "INSERT INTO received_payments "
        "(re_no, date_iso, customer, salesperson, cancelled, total) "
        "VALUES (?, '2026-05-21', 'test_customer_mig070', 'sp_test', 0, 1234.56)",
        (re_no,),
    )
    return cur.lastrowid, re_no


def test_received_payments_insert_logged(tmp_db):
    """INSERT into received_payments creates an audit_log row, action=INSERT."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_chain(conn)

    before = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE table_name='received_payments'"
    ).fetchone()[0]
    new_id, _ = _insert_rp(conn)
    after = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE table_name='received_payments'"
    ).fetchone()[0]
    assert after == before + 1

    row = conn.execute(
        "SELECT row_id, action, changed_fields FROM audit_log "
        "WHERE table_name='received_payments' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] == new_id
    assert row[1] == "INSERT"
    payload = json.loads(row[2])
    assert payload["re_no"] == "RE_AUDIT_MIG070_TEST_1"
    assert payload["customer"] == "test_customer_mig070"
    assert payload["total"] == 1234.56
    assert payload["cancelled"] == 0

    conn.execute("DELETE FROM received_payments WHERE id = ?", (new_id,))
    conn.commit()
    conn.close()


def test_received_payments_update_logged(tmp_db):
    """UPDATE on received_payments creates an audit_log row, action=UPDATE."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_chain(conn)

    new_id, _ = _insert_rp(conn, re_no="RE_AUDIT_MIG070_TEST_2")
    before = conn.execute(
        "SELECT COUNT(*) FROM audit_log "
        "WHERE table_name='received_payments' AND action='UPDATE'"
    ).fetchone()[0]

    conn.execute(
        "UPDATE received_payments SET cancelled=1, total=9999.99 WHERE id=?",
        (new_id,),
    )
    after = conn.execute(
        "SELECT COUNT(*) FROM audit_log "
        "WHERE table_name='received_payments' AND action='UPDATE'"
    ).fetchone()[0]
    assert after == before + 1

    row = conn.execute(
        "SELECT row_id, action, changed_fields FROM audit_log "
        "WHERE table_name='received_payments' AND action='UPDATE' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] == new_id
    payload = json.loads(row[2])
    # Same diff-style payload as audit_products_update: {field: [old, new]}.
    assert "cancelled" in payload
    assert payload["cancelled"] == [0, 1]
    assert "total" in payload
    assert payload["total"] == [1234.56, 9999.99]

    conn.execute("DELETE FROM received_payments WHERE id = ?", (new_id,))
    conn.commit()
    conn.close()


def test_received_payments_delete_logged(tmp_db):
    """DELETE on received_payments creates an audit_log row, action=DELETE."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_chain(conn)

    new_id, _ = _insert_rp(conn, re_no="RE_AUDIT_MIG070_TEST_3")
    before = conn.execute(
        "SELECT COUNT(*) FROM audit_log "
        "WHERE table_name='received_payments' AND action='DELETE'"
    ).fetchone()[0]
    conn.execute("DELETE FROM received_payments WHERE id = ?", (new_id,))
    after = conn.execute(
        "SELECT COUNT(*) FROM audit_log "
        "WHERE table_name='received_payments' AND action='DELETE'"
    ).fetchone()[0]
    assert after == before + 1

    row = conn.execute(
        "SELECT row_id, action, changed_fields FROM audit_log "
        "WHERE table_name='received_payments' AND action='DELETE' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] == new_id
    payload = json.loads(row[2])
    assert payload["re_no"] == "RE_AUDIT_MIG070_TEST_3"
    assert payload["customer"] == "test_customer_mig070"
    conn.commit()
    conn.close()


# ── rollback ─────────────────────────────────────────────────────────────────

def test_rollback_removes_only_mig070_triggers(tmp_db):
    """Rollback drops the 4 new triggers + the audit_log index, removes the
    bookkeeping row from applied_migrations so the runner re-applies on next
    boot, and leaves the pre-existing audit_* triggers (e.g.
    audit_products_insert) intact."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_chain(conn)

    # Record the applied-migrations row mig 070 inserts (only present after
    # the runner's bookkeeping INSERT; the migration script itself doesn't
    # self-insert, so simulate the runner's INSERT here).
    conn.execute(
        "INSERT OR IGNORE INTO applied_migrations (filename, applied_by) "
        "VALUES ('070_audit_log_triggers.sql', 'test-fixture')"
    )
    conn.commit()

    new_triggers = (
        "audit_transactions_insert",
        "audit_received_payments_insert",
        "audit_received_payments_update",
        "audit_received_payments_delete",
    )
    for name in new_triggers:
        got = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name=?",
            (name,),
        ).fetchone()
        assert got is not None, f"{name} should exist after mig 070"

    # New audit_log index created by forward mig 070.
    got = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name='idx_audit_log_table_time'"
    ).fetchone()
    assert got is not None, "idx_audit_log_table_time should exist after mig 070"

    _apply(conn, ROLLBACK_070)

    for name in new_triggers:
        got = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name=?",
            (name,),
        ).fetchone()
        assert got is None, f"{name} should be dropped after rollback"

    # Index dropped by rollback.
    got = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name='idx_audit_log_table_time'"
    ).fetchone()
    assert got is None, "idx_audit_log_table_time should be dropped after rollback"

    # applied_migrations bookkeeping row removed so runner re-applies on boot.
    row = conn.execute(
        "SELECT filename FROM applied_migrations "
        "WHERE filename='070_audit_log_triggers.sql'"
    ).fetchone()
    assert row is None, (
        "rollback must DELETE the applied_migrations row so the runner "
        "re-applies 070 on next boot"
    )

    # Pre-existing audit trigger must still be present.
    got = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='trigger' AND name='audit_products_insert'"
    ).fetchone()
    assert got is not None, \
        "rollback must not touch pre-existing audit_products_insert"
    conn.close()
