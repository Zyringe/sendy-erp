"""Commission payout year_month is stamped from the RECEIPT cycle, not the
page the user ticked from.

Background (2026-06-15): commission is binned by receipt-collection month
(the engine groups receipts by date). When Put pays last month's commission
from the *current* month's drill-down, the old route stamped the payout with
the currently-viewed page month, so a May-collected invoice paid in June got
year_month='2026-06'. The May page then never saw the payment (its paid
lookup only counts year_month <= the selected month) and showed phantom
รอจ่าย. The fix: derive each invoice's year_month from the month of the
earliest receipt the salesperson collected against it.
"""
from __future__ import annotations

import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))


@pytest.fixture
def migrated_db(tmp_db):
    """tmp_db with pending migrations applied (Phase 3 needs mig 129's
    cashbook_transactions.commission_payout_id — the route now auto-posts a
    linked cashbook row on every payout, see commission.record_payout)."""
    import database
    database.init_db()
    return tmp_db


def _seed_receipt(db_path, *, re_no, date_iso, salesperson, doc_no,
                  amount=100.0, customer="TEST CUST", cancelled=0,
                  doc_kind="IV"):
    """Insert one received_payments + paid_invoices pair into the temp DB."""
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        cur = conn.execute(
            "INSERT INTO received_payments (re_no, date_iso, customer, "
            "salesperson, cancelled, total) VALUES (?, ?, ?, ?, ?, ?)",
            (re_no, date_iso, customer, salesperson, cancelled, amount),
        )
        re_id = cur.lastrowid
        conn.execute(
            "INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount) "
            "VALUES (?, ?, ?, ?)",
            (re_id, doc_no, doc_kind, amount),
        )
        conn.commit()
    finally:
        conn.close()


# ── helper: get_invoice_cycle_month ──────────────────────────────────────────

def test_cycle_month_is_receipt_month(tmp_db):
    import commission

    _seed_receipt(tmp_db, re_no="RE_CYC_1", date_iso="2026-05-08",
                  salesperson="ZZ", doc_no="IV_CYC_1")
    assert commission.get_invoice_cycle_month("ZZ", "IV_CYC_1",
                                              db_path=tmp_db) == "2026-05"


def test_cycle_month_none_when_no_receipt(tmp_db):
    import commission

    assert commission.get_invoice_cycle_month("ZZ", "IV_DOES_NOT_EXIST",
                                              db_path=tmp_db) is None


def test_cycle_month_uses_earliest_receipt(tmp_db):
    """Split payment across months → cycle = the earliest receipt's month
    (matches the engine, which surfaces the invoice from its first receipt)."""
    import commission

    _seed_receipt(tmp_db, re_no="RE_CYC_2A", date_iso="2026-06-03",
                  salesperson="ZZ", doc_no="IV_CYC_2")
    _seed_receipt(tmp_db, re_no="RE_CYC_2B", date_iso="2026-05-20",
                  salesperson="ZZ", doc_no="IV_CYC_2")
    assert commission.get_invoice_cycle_month("ZZ", "IV_CYC_2",
                                              db_path=tmp_db) == "2026-05"


def test_cycle_month_ignores_cancelled_and_sr(tmp_db):
    """Cancelled receipts and SR (credit-note) links don't define the cycle."""
    import commission

    _seed_receipt(tmp_db, re_no="RE_CYC_3X", date_iso="2026-04-01",
                  salesperson="ZZ", doc_no="IV_CYC_3", cancelled=1)
    _seed_receipt(tmp_db, re_no="RE_CYC_3Y", date_iso="2026-03-01",
                  salesperson="ZZ", doc_no="IV_CYC_3", doc_kind="SR")
    _seed_receipt(tmp_db, re_no="RE_CYC_3Z", date_iso="2026-05-15",
                  salesperson="ZZ", doc_no="IV_CYC_3")
    assert commission.get_invoice_cycle_month("ZZ", "IV_CYC_3",
                                              db_path=tmp_db) == "2026-05"


# ── route: /commission/payout stamps the receipt cycle ───────────────────────

def _admin_client(tmp_db):
    from app import app as flask_app
    flask_app.config["TESTING"] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = 1
        sess["username"] = "admin"
        sess["role"] = "admin"
    return c


def _payout_rows(db_path, doc_no):
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT year_month, amount_paid, invoice_no FROM commission_payouts "
            "WHERE invoice_no = ?", (doc_no,)).fetchall()]
    finally:
        conn.close()


def test_route_stamps_receipt_cycle_not_page_month(migrated_db):
    """The bug repro: tick a May-collected invoice from the June page →
    payout must land in year_month=2026-05, not 2026-06."""
    tmp_db = migrated_db
    _seed_receipt(tmp_db, re_no="RE_RT_1", date_iso="2026-05-08",
                  salesperson="ZZ", doc_no="IV_RT_1")
    client = _admin_client(tmp_db)
    resp = client.post("/commission/payout", data={
        "month": "2026-06",            # user is on the JUNE page
        "sp_code": "ZZ",
        "invoice_no": "IV_RT_1",
        "amount_IV_RT_1": "185.25",
        "paid_date": "2026-06-08",
        "paid_method": "transfer",
    }, follow_redirects=False)
    assert resp.status_code in (302, 303), resp.data[:300]

    rows = _payout_rows(tmp_db, "IV_RT_1")
    assert len(rows) == 1, rows
    assert rows[0]["year_month"] == "2026-05", (
        f"expected receipt-cycle month 2026-05, got {rows[0]['year_month']}")


def test_route_falls_back_to_form_month_when_no_receipt(migrated_db):
    """Defensive: an invoice with no qualifying receipt keeps the form month."""
    tmp_db = migrated_db
    client = _admin_client(tmp_db)
    resp = client.post("/commission/payout", data={
        "month": "2026-06",
        "sp_code": "ZZ",
        "invoice_no": "IV_NO_RECEIPT",
        "amount_IV_NO_RECEIPT": "10.00",
        "paid_date": "2026-06-08",
    }, follow_redirects=False)
    assert resp.status_code in (302, 303), resp.data[:300]
    rows = _payout_rows(tmp_db, "IV_NO_RECEIPT")
    assert len(rows) == 1, rows
    assert rows[0]["year_month"] == "2026-06"


def test_mode2_month_level_still_uses_form_month(migrated_db):
    """Mode 2 (per-salesperson, no invoice_no) is an explicit month-level
    payout — it must keep using the form's month."""
    tmp_db = migrated_db
    client = _admin_client(tmp_db)
    resp = client.post("/commission/payout", data={
        "month": "2026-06",
        "sp_code": "ZZ",
        "amount_ZZ": "500.00",
        "paid_date": "2026-06-08",
    }, follow_redirects=False)
    assert resp.status_code in (302, 303), resp.data[:300]
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT year_month, amount_paid, invoice_no FROM commission_payouts "
            "WHERE salesperson_code='ZZ' AND invoice_no IS NULL "
            "AND amount_paid=500.0").fetchall()]
    finally:
        conn.close()
    assert len(rows) == 1, rows
    assert rows[0]["year_month"] == "2026-06"
