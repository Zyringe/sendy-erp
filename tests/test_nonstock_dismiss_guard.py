"""⛔ /unit-conversions must never delete a non-stock billable revenue row.

dismiss_pending_unit_conversion deletes every (product_id, unit) source row
with synced_to_stock=0, documented as "they have never touched stock so
deletion is safe". Non-stock rows are permanently synced_to_stock=0 AND hold
real revenue, so that premise no longer holds.
"""
import sqlite3

import pytest

import models
import models.bsn_sync as bsn_sync


def _seed(conn, code, name, unit):
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES (?, 'ตัว')",
        (name,)).lastrowid
    conn.execute(
        "INSERT INTO sales_transactions (batch_id, date_iso, doc_no, doc_base,"
        " product_id, bsn_code, product_name_raw, customer, qty, unit, net,"
        " synced_to_stock) VALUES (1,'2026-06-15','IV9100-1','IV9100',?,?,?,"
        "'วรสวัสดิ์',1,?,30.0,0)", (pid, code, name, unit))
    conn.commit()
    return pid


def test_non_stock_rows_absent_from_pending_unit_conversions(tmp_db_conn):
    conn = tmp_db_conn
    conn.execute("DELETE FROM sales_transactions")
    conn.execute("DELETE FROM purchase_transactions")
    conn.commit()
    ship_pid = _seed(conn, '888ค8888', 'ค่าขนส่ง', 'ใบ')
    real_pid = _seed(conn, '036ผ7110', 'แผ่นตัด 14 นิ้ว', 'แผ่น')

    # A NULL bsn_code row is an unmapped legacy row, NOT a non-stock code, so
    # the predicate must KEEP it. Carried forward from Task 2's review: the
    # IS NULL leg is only tested there as a lone predicate, never composed into
    # a real multi-clause WHERE. Simplified to a bare `NOT IN`, SQL's NULL
    # semantics would silently drop every legacy row from this list.
    null_pid = conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES ('เศษเหล็ก','ตัว')"
    ).lastrowid
    conn.execute(
        "INSERT INTO sales_transactions (batch_id, date_iso, doc_no, doc_base,"
        " product_id, bsn_code, product_name_raw, customer, qty, unit, net,"
        " synced_to_stock) VALUES (1,'2026-06-15','IV9101-1','IV9101',?,NULL,"
        "'เศษเหล็ก','วรสวัสดิ์',1,'กอง',30.0,0)", (null_pid,))
    conn.commit()

    pending = models.get_pending_unit_conversions()
    pids = [p['product_id'] for p in pending]

    # CONTROL: the ordinary product DOES appear — proving the query can find
    # things, so the absence below is a real absence and not an empty query.
    assert real_pid in pids, pending
    assert null_pid in pids, pending      # NULL bsn_code must survive the predicate
    assert ship_pid not in pids, pending


def test_dismiss_refuses_to_delete_non_stock_rows(tmp_db_conn):
    conn = tmp_db_conn
    conn.execute("DELETE FROM sales_transactions")
    conn.commit()
    ship_pid = _seed(conn, '888ค8888', 'ค่าขนส่ง', 'ใบ')

    deleted = models.dismiss_pending_unit_conversion(ship_pid, 'ใบ')

    assert deleted == 0
    surviving = conn.execute(
        "SELECT COUNT(*) FROM sales_transactions WHERE product_id=?",
        (ship_pid,)).fetchone()[0]
    assert surviving == 1, "the revenue row must survive a dismiss"


def test_dismiss_refuses_a_MIXED_group_all_or_nothing(tmp_db_conn):
    """Pins the FUNCTION-LEVEL rejection specifically.

    Step 3 installs two guards (a rejection before the DELETE, and a narrowed
    DELETE). Removing either one alone leaves the other covering the
    single-code cases, so no single-code test can tell them apart — a guard
    no test pins is a guard that can be deleted silently.

    A MIXED (product_id, unit) group separates them: with only the narrowed
    DELETE, the ordinary row is deleted and the call returns 1. With the
    rejection, the whole group is refused and nothing is deleted.
    """
    conn = tmp_db_conn
    conn.execute("DELETE FROM sales_transactions")
    conn.commit()
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES ('รวมมิตร','ตัว')"
    ).lastrowid
    for code in ('888ค8888', '036ผ7110'):
        conn.execute(
            "INSERT INTO sales_transactions (batch_id, date_iso, doc_no, doc_base,"
            " product_id, bsn_code, customer, qty, unit, net, synced_to_stock)"
            " VALUES (1,'2026-06-15',?,?,?,?,'วรสวัสดิ์',1,'ใบ',30.0,0)",
            (f'IV9150-{code}', 'IV9150', pid, code))
    conn.commit()

    deleted = models.dismiss_pending_unit_conversion(pid, 'ใบ')

    assert deleted == 0, "a mixed group must be refused whole, never half-deleted"
    remaining = conn.execute(
        "SELECT COUNT(*) FROM sales_transactions WHERE product_id=?", (pid,)).fetchone()[0]
    assert remaining == 2, "neither row may be deleted"


def test_dismiss_still_works_for_ordinary_products(tmp_db_conn):
    """CONTROL — the guard must not break the feature it is protecting."""
    conn = tmp_db_conn
    conn.execute("DELETE FROM sales_transactions")
    conn.commit()
    real_pid = _seed(conn, '036ผ7110', 'แผ่นตัด 14 นิ้ว', 'แผ่น')

    deleted = models.dismiss_pending_unit_conversion(real_pid, 'แผ่น')

    assert deleted == 1
    remaining = conn.execute(
        "SELECT COUNT(*) FROM sales_transactions WHERE product_id=?",
        (real_pid,)).fetchone()[0]
    assert remaining == 0


def test_dismiss_holds_lock_before_first_read(tmp_db_conn, monkeypatch):
    """Concurrency seam BEFORE the first write (fix round 1 finding).

    Under SQLite's default deferred isolation a connection holds no lock
    until it writes, so a probe injected AFTER the first write would be
    excluded either way — with or without BEGIN IMMEDIATE — and prove
    nothing (erp-engineering-discipline.md, "A concurrency test whose seam
    sits AFTER the first write proves nothing"). `non_stock_clause` is
    called by dismiss_pending_unit_conversion's very first statement (the
    protected-row read), strictly before any DELETE, so patching it is the
    earliest available seam. Open a SECOND connection with a short timeout
    and assert the concurrent writer is excluded — this is the exact
    scenario the finding describes: a concurrent BSN import inserting a new
    protected row into the same (product_id, unit) between the read and the
    DELETE.
    """
    conn = tmp_db_conn
    conn.execute("DELETE FROM sales_transactions")
    conn.commit()
    real_pid = _seed(conn, '036ผ7110', 'แผ่นตัด 14 นิ้ว', 'แผ่น')

    import config
    seen = {}
    real_fn = bsn_sync.non_stock_clause

    def _spy(*args, **kwargs):
        if not seen:
            seen['fired'] = True
            probe = sqlite3.connect(config.DATABASE_PATH, timeout=0.1)
            try:
                with pytest.raises(sqlite3.OperationalError, match='database is locked'):
                    probe.execute(
                        "INSERT INTO sales_transactions (batch_id, date_iso, doc_no,"
                        " doc_base, product_id, bsn_code, customer, qty, unit, net,"
                        " synced_to_stock) VALUES"
                        " (1,'2026-06-15','INTRUDER-1','INTRUDER',?,'888ค8888',"
                        "'intruder',1,?,30.0,0)", (real_pid, 'แผ่น'))
                seen['locked'] = True
            finally:
                probe.close()
        return real_fn(*args, **kwargs)

    monkeypatch.setattr(bsn_sync, 'non_stock_clause', _spy)
    deleted = bsn_sync.dismiss_pending_unit_conversion(real_pid, 'แผ่น')

    assert seen.get('locked') is True
    # The intruder was locked out, so the group never became mixed — the
    # ordinary row still dismisses normally.
    assert deleted == 1
    remaining = conn.execute(
        "SELECT COUNT(*) FROM sales_transactions WHERE product_id=?",
        (real_pid,)).fetchone()[0]
    assert remaining == 0
