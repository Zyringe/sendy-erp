"""/ap follows the book toggle, so xp5's payables become viewable (plan F4).

Phase 0 (#393) started writing each book's AR/AP snapshot into that book's own
DB -- which fixed F3 (the main book's /ap had been showing the VAT book's
RR26-series payables while claiming to be BSN5657) but left the xp5 half
written and unreachable. Measured on prod 2026-08-19, vat_book.db holds 45 AR
rows (฿261,796.91) and 14 AP rows (฿95,708.77) that no page could display.

WHAT IS AND IS NOT IN SCOPE, from the plan's own table:

  * /ap  -- yes. Self-contained: get_ap_outstanding(conn) plus three
    express_payments_out queries, all on the one connection the route holds.
  * commission -- deliberately NOT. It is paid on collections in the
    operational book, so a rep's payout must never depend on which tab someone
    had open. Pinned by a test here rather than left to a comment.
  * /ar -- NOT YET, and deliberately so. Its overview tab calls
    get_customer_debt_summary() and get_payment_summary(), neither of which
    takes a connection, and its customers tab calls ar_followup.customer_
    ranking() -- and ar_followup is blocked on the plan's open decision #2
    (is chasing a customer one conversation about one total, or two books that
    must never be added together?). Converting half a page would silently mix
    two books' money on one screen, which is worse than not converting it.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import book_registry


def _client(role='admin', book=None):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = 'u'
        s['role'] = role
        if book:
            s['active_book'] = book
    return c


def _seed_ap(conn, entity, snapshot, rows):
    conn.execute("DELETE FROM express_ap_outstanding")     # force, never inherit
    for doc, supplier, amount in rows:
        conn.execute(
            "INSERT INTO express_ap_outstanding (entity, snapshot_date_iso,"
            " supplier_code, supplier_name, doc_no, doc_date_iso,"
            " bill_amount, paid_amount, outstanding_amount)"
            " VALUES (?, ?, 'S1', ?, ?, '2026-08-01', ?, 0, ?)",
            (entity, snapshot, supplier, doc, amount, amount))
    conn.commit()


def test_ap_endpoint_is_in_the_parity_set():
    """Without this the toggle refuses the page outright, so every other
    assertion here would be meaningless."""
    assert 'accounting.ap_dashboard' in book_registry.PARITY_ENDPOINTS


def test_ap_reads_the_active_book(tmp_db, tmp_path, monkeypatch):
    """The novat and vat books hold DIFFERENT payables; the page must show the
    one that is selected. Asserting both directions in one test is the point --
    a route that ignored the toggle would show the same number twice."""
    conn = sqlite3.connect(tmp_db)
    _seed_ap(conn, 'BSN', '2026-08-19', [('RR6900001', 'ผู้ขายสมุดหลัก', 1111.0)])
    conn.close()

    vat_db = tmp_path / 'vat_book.db'
    import shutil
    shutil.copy2(tmp_db, vat_db)
    vconn = sqlite3.connect(str(vat_db))
    _seed_ap(vconn, 'BSN', '2026-08-19', [('RR2600001', 'ผู้ขายสมุดแวต', 2222.0)])
    vconn.execute("CREATE TABLE IF NOT EXISTS book_meta (key TEXT PRIMARY KEY, value TEXT)")
    vconn.execute("INSERT OR REPLACE INTO book_meta VALUES ('built_at','2026-08-19T17:16:18')")
    vconn.commit()
    vconn.close()
    monkeypatch.setattr(book_registry, 'book_db_path', lambda book: str(vat_db))

    novat = _client().get('/ap').data.decode()
    assert 'ผู้ขายสมุดหลัก' in novat
    assert 'ผู้ขายสมุดแวต' not in novat

    vat = _client(book='vat').get('/ap').data.decode()
    assert 'ผู้ขายสมุดแวต' in vat, 'the VAT book payables are still unreachable'
    assert 'ผู้ขายสมุดหลัก' not in vat


def test_ap_stays_read_only_in_vat_mode(tmp_db, tmp_path, monkeypatch):
    """VAT mode denies every unsafe method app-wide; adding a parity endpoint
    must not open a write path through it."""
    vat_db = tmp_path / 'vat_book.db'
    import shutil
    shutil.copy2(tmp_db, vat_db)
    monkeypatch.setattr(book_registry, 'book_db_path', lambda book: str(vat_db))
    r = _client(book='vat').post('/ap')
    assert r.status_code in (302, 403, 405), r.status_code


def test_commission_does_not_follow_the_book_toggle(tmp_db, tmp_path,
                                                    monkeypatch):
    """A rep is paid on collections in the OPERATIONAL book. If this ever
    followed the toggle, a payout would depend on which tab was open.

    Asserted on the module's own connection source, not on a page: commission
    must reach database.get_connection, never book_registry.
    """
    import commission
    import inspect
    src = inspect.getsource(commission)
    assert 'get_book_connection' not in src, (
        'commission must stay pinned to the operational book')
    assert 'book_registry' not in src
