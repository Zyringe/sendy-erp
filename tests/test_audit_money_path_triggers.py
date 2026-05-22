"""Audit-trigger coverage tests for mig 076 — paid_invoices, credit_note_amounts,
cashbook_transactions.

Verifies that mutations on each money-path table land in `audit_log` with the
right payload, and that the rollback round-trip cleanly removes all 9 triggers.
"""
import os
import sqlite3

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_076 = os.path.join(REPO, "data", "migrations",
                       "076_audit_money_path_tables.sql")
ROLLBACK_076 = os.path.join(REPO, "data", "migrations",
                            "076_audit_money_path_tables.rollback.sql")

MIG_076_TRIGGERS = {
    'audit_paid_invoices_insert', 'audit_paid_invoices_update',
    'audit_paid_invoices_delete',
    'audit_credit_note_amounts_insert', 'audit_credit_note_amounts_update',
    'audit_credit_note_amounts_delete',
    'audit_cashbook_transactions_insert', 'audit_cashbook_transactions_update',
    'audit_cashbook_transactions_delete',
}


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


def _trigger_names(conn, prefix='audit_'):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE ?",
        (prefix + '%',)
    ).fetchall()}


# ── paid_invoices ────────────────────────────────────────────────────────────

def test_paid_invoices_insert_logs_payload(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = OFF")  # avoid re_id FK requirement
    cur = conn.execute(
        "INSERT INTO paid_invoices (re_id, iv_no, amount) VALUES (99999, 'IV-T1', 1500.0)"
    )
    pid = cur.lastrowid
    conn.commit()
    payload = conn.execute(
        "SELECT changed_fields FROM audit_log "
        "WHERE table_name='paid_invoices' AND row_id=? AND action='INSERT'",
        (pid,),
    ).fetchone()[0]
    assert 'IV-T1' in payload and '1500' in payload
    conn.close()


def test_paid_invoices_update_logs_iv_change(tmp_db):
    """Re-routing iv_no must be audited (money path — credits a different
    invoice)."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = OFF")
    cur = conn.execute(
        "INSERT INTO paid_invoices (re_id, iv_no, amount) VALUES (99999, 'IV-A', 500.0)"
    )
    pid = cur.lastrowid
    conn.commit()
    conn.execute("UPDATE paid_invoices SET iv_no='IV-B' WHERE id=?", (pid,))
    conn.commit()
    payload = conn.execute(
        "SELECT changed_fields FROM audit_log "
        "WHERE table_name='paid_invoices' AND row_id=? AND action='UPDATE' "
        "ORDER BY id DESC LIMIT 1", (pid,),
    ).fetchone()[0]
    assert 'iv_no' in payload
    assert 'IV-A' in payload and 'IV-B' in payload
    conn.close()


def test_paid_invoices_delete_logs_snapshot(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = OFF")
    cur = conn.execute(
        "INSERT INTO paid_invoices (re_id, iv_no, amount) VALUES (99999, 'IV-DEL', 222.0)"
    )
    pid = cur.lastrowid
    conn.commit()
    conn.execute("DELETE FROM paid_invoices WHERE id=?", (pid,))
    conn.commit()
    payload = conn.execute(
        "SELECT changed_fields FROM audit_log "
        "WHERE table_name='paid_invoices' AND row_id=? AND action='DELETE'",
        (pid,),
    ).fetchone()[0]
    assert 'IV-DEL' in payload and '222' in payload
    conn.close()


# ── credit_note_amounts ──────────────────────────────────────────────────────

def test_credit_note_amounts_insert_logs_payload(tmp_db):
    conn = sqlite3.connect(tmp_db)
    cur = conn.execute(
        "INSERT INTO credit_note_amounts "
        "(sr_doc_base, ref_invoice, credited_amount, sr_date_iso, customer, source) "
        "VALUES ('SR-T1', 'IV-X', 100.0, '2026-01-15', 'C', 'csv')"
    )
    cnid = cur.lastrowid
    conn.commit()
    payload = conn.execute(
        "SELECT changed_fields FROM audit_log "
        "WHERE table_name='credit_note_amounts' AND row_id=? AND action='INSERT'",
        (cnid,),
    ).fetchone()[0]
    assert 'SR-T1' in payload and 'IV-X' in payload
    conn.close()


def test_credit_note_amounts_delete_logs_full_snapshot(tmp_db):
    """DELETE snapshot must include all 6 mutable fields — for legacy rows
    with no prior INSERT audit, this is the only forensic trail."""
    conn = sqlite3.connect(tmp_db)
    cur = conn.execute(
        "INSERT INTO credit_note_amounts "
        "(sr_doc_base, ref_invoice, credited_amount, sr_date_iso, customer, source) "
        "VALUES ('SR-DEL', 'IV-Z', 175.0, '2026-01-15', 'Acme Co', 'csv')"
    )
    cnid = cur.lastrowid
    conn.commit()
    conn.execute("DELETE FROM credit_note_amounts WHERE id=?", (cnid,))
    conn.commit()
    payload = conn.execute(
        "SELECT changed_fields FROM audit_log "
        "WHERE table_name='credit_note_amounts' AND row_id=? AND action='DELETE'",
        (cnid,),
    ).fetchone()[0]
    assert 'SR-DEL' in payload and 'IV-Z' in payload
    assert '175' in payload
    assert '2026-01-15' in payload
    assert 'Acme Co' in payload
    assert 'csv' in payload
    conn.close()


def test_credit_note_amounts_update_logs_amount_drift(tmp_db):
    """Drift in credited_amount caused the ฿105k phantom-overpay bug
    (fixed by mig 062). Trigger captures the diff for future audits."""
    conn = sqlite3.connect(tmp_db)
    cur = conn.execute(
        "INSERT INTO credit_note_amounts "
        "(sr_doc_base, ref_invoice, credited_amount, sr_date_iso, customer, source) "
        "VALUES ('SR-T2', 'IV-Y', 100.0, '2026-01-15', 'C', 'csv')"
    )
    cnid = cur.lastrowid
    conn.commit()
    conn.execute(
        "UPDATE credit_note_amounts SET credited_amount=250.0 WHERE id=?",
        (cnid,),
    )
    conn.commit()
    payload = conn.execute(
        "SELECT changed_fields FROM audit_log "
        "WHERE table_name='credit_note_amounts' AND row_id=? AND action='UPDATE'",
        (cnid,),
    ).fetchone()[0]
    assert '100' in payload and '250' in payload
    conn.close()


# ── cashbook_transactions ────────────────────────────────────────────────────

def test_cashbook_transactions_insert_logs_payload(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = OFF")
    cur = conn.execute(
        "INSERT INTO cashbook_transactions "
        "(account_id, txn_date, direction, category, amount, description) "
        "VALUES (1, '2026-01-15', 'income', 'sale', 5000.0, 'test deposit')"
    )
    cid = cur.lastrowid
    conn.commit()
    payload = conn.execute(
        "SELECT changed_fields FROM audit_log "
        "WHERE table_name='cashbook_transactions' AND row_id=? AND action='INSERT'",
        (cid,),
    ).fetchone()[0]
    assert 'test deposit' in payload and '5000' in payload
    conn.close()


def test_cashbook_transactions_update_logs_amount(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = OFF")
    cur = conn.execute(
        "INSERT INTO cashbook_transactions "
        "(account_id, txn_date, direction, category, amount, description) "
        "VALUES (1, '2026-01-15', 'income', 'sale', 100.0, 'd')"
    )
    cid = cur.lastrowid
    conn.commit()
    conn.execute(
        "UPDATE cashbook_transactions SET amount=200.0 WHERE id=?", (cid,)
    )
    conn.commit()
    payload = conn.execute(
        "SELECT changed_fields FROM audit_log "
        "WHERE table_name='cashbook_transactions' AND row_id=? AND action='UPDATE'",
        (cid,),
    ).fetchone()[0]
    assert '100' in payload and '200' in payload
    conn.close()


def test_cashbook_transactions_update_logs_provenance_fields(tmp_db):
    """source_file/sheet/row/import_batch_id changes must be audited too —
    re-routing a row to a different import source is suspicious and was
    silent before this fix (codex pass 1)."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = OFF")
    cur = conn.execute(
        "INSERT INTO cashbook_transactions "
        "(account_id, txn_date, direction, category, amount, description, "
        " source_file, source_sheet, source_row, import_batch_id) "
        "VALUES (1, '2026-01-15', 'income', 'sale', 100.0, 'd', "
        " 'orig.xlsx', 'Sheet1', 5, 'batch-A')"
    )
    cid = cur.lastrowid
    conn.commit()
    conn.execute(
        "UPDATE cashbook_transactions SET source_file='tampered.xlsx' WHERE id=?",
        (cid,),
    )
    conn.commit()
    payload = conn.execute(
        "SELECT changed_fields FROM audit_log "
        "WHERE table_name='cashbook_transactions' AND row_id=? AND action='UPDATE'",
        (cid,),
    ).fetchone()[0]
    assert 'source_file' in payload
    assert 'orig.xlsx' in payload and 'tampered.xlsx' in payload
    conn.close()


def test_cashbook_transactions_delete_logs_snapshot(tmp_db):
    """DELETE snapshot must include business + provenance context — for rows
    with no prior INSERT audit (legacy), this is the only forensic trail."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = OFF")
    cur = conn.execute(
        "INSERT INTO cashbook_transactions "
        "(account_id, txn_date, direction, category, user_category, amount, "
        " description, note, source_file, source_sheet, source_row, "
        " import_batch_id) "
        "VALUES (1, '2026-01-15', 'income', 'sale', 'wholesale', 333.0, "
        " 'to-delete', 'extra note', 'src.xlsx', 'Sh1', 9, 'batch-Z')"
    )
    cid = cur.lastrowid
    conn.commit()
    conn.execute("DELETE FROM cashbook_transactions WHERE id=?", (cid,))
    conn.commit()
    payload = conn.execute(
        "SELECT changed_fields FROM audit_log "
        "WHERE table_name='cashbook_transactions' AND row_id=? AND action='DELETE'",
        (cid,),
    ).fetchone()[0]
    # Business context
    assert '333' in payload and 'income' in payload
    assert 'sale' in payload and 'wholesale' in payload
    assert 'to-delete' in payload and 'extra note' in payload
    # Provenance
    assert 'src.xlsx' in payload and 'Sh1' in payload
    assert 'batch-Z' in payload
    conn.close()


# ── rollback round-trip ──────────────────────────────────────────────────────

def test_mig_076_rollback_round_trip(tmp_db):
    """Rollback drops all 9 triggers; re-apply brings them back."""
    conn = sqlite3.connect(tmp_db)
    triggers = _trigger_names(conn)
    assert MIG_076_TRIGGERS.issubset(triggers), \
        f"baseline: missing {MIG_076_TRIGGERS - triggers}"

    _apply(conn, ROLLBACK_076)
    triggers = _trigger_names(conn)
    leftover = MIG_076_TRIGGERS & triggers
    assert not leftover, f"after rollback: leftover {leftover}"

    _apply(conn, MIG_076)
    triggers = _trigger_names(conn)
    assert MIG_076_TRIGGERS.issubset(triggers)
    conn.close()


def test_mig_076_applied_row_cleaned_on_rollback(tmp_db):
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT filename FROM applied_migrations WHERE filename=?",
        ('076_audit_money_path_tables.sql',),
    ).fetchone()
    assert row is not None
    _apply(conn, ROLLBACK_076)
    row = conn.execute(
        "SELECT filename FROM applied_migrations WHERE filename=?",
        ('076_audit_money_path_tables.sql',),
    ).fetchone()
    assert row is None
    conn.close()
