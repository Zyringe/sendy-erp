"""TDD + integration tests for Phase 2 finance revamp R4 (freshness
standard) — models.get_customer_unpaid_bills_by_code() returns
(rows, snapshot_date) instead of a bare row list, so customer_summary.html
can disclose "ณ {snapshot_date}" like the other AR widgets already do.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')


CODE = 'ZZSNAPDATE'
NAME = 'ลูกค้าทดสอบวันที่ snapshot'
DOC = 'ZZSNAP-IV'


def _seed_unpaid(db_path):
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        snapshot_date = conn.execute(
            "SELECT MAX(snapshot_date_iso) FROM express_ar_outstanding WHERE entity='BSN'"
        ).fetchone()[0]
        batch_id = conn.execute(
            "SELECT id FROM express_import_log ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        assert snapshot_date and batch_id

        conn.execute("DELETE FROM express_ar_outstanding WHERE customer_code = ? OR doc_no = ?",
                     (CODE, DOC))
        conn.execute("DELETE FROM ar_writeoffs WHERE doc_no = ?", (DOC,))
        conn.execute("DELETE FROM customers WHERE code = ? OR name = ?", (CODE, NAME))
        conn.execute("INSERT INTO customers (code, name) VALUES (?, ?)", (CODE, NAME))
        conn.execute(
            """INSERT INTO express_ar_outstanding
                 (batch_id, snapshot_date_iso, customer_code, customer_name,
                  doc_date_iso, doc_no, is_anomalous, bill_amount, paid_amount,
                  outstanding_amount, entity)
               VALUES (?, ?, ?, ?, '2026-01-01', ?, 0, 500, 0, 500, 'BSN')""",
            (batch_id, snapshot_date, CODE, NAME, DOC))
        conn.commit()
        return snapshot_date
    finally:
        conn.close()


def _admin(tmp_db):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1; s['username'] = 'admin'; s['role'] = 'admin'
    return c


def test_returns_rows_and_snapshot_date_tuple(tmp_db):
    import models

    expected_snapshot = _seed_unpaid(tmp_db)

    result = models.get_customer_unpaid_bills_by_code(CODE)
    assert isinstance(result, tuple) and len(result) == 2
    rows, snapshot_date = result
    assert snapshot_date == expected_snapshot
    assert len(rows) == 1
    assert rows[0]['doc_base'] == DOC


def test_snapshot_date_is_none_when_no_snapshot_rows(empty_db_conn):
    import models
    rows, snapshot_date = models.get_customer_unpaid_bills_by_code('ไม่มีรหัสนี้')
    assert rows == []
    assert snapshot_date is None


def test_customer_summary_route_shows_snapshot_date(tmp_db):
    """Integration guard: the header must render the freshness date
    whenever there's at least one unpaid bill for that customer."""
    from urllib.parse import quote

    snap = _seed_unpaid(tmp_db)

    c = _admin(tmp_db)
    r = c.get(f'/customer/code/{quote(CODE)}')
    assert r.status_code == 200
    body = r.data.decode()
    assert f'ณ {snap}' in body
