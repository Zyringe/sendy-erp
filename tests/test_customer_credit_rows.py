"""TDD tests for payments_alloc.customer_credit_rows().

Synthetic-data only, on empty_db_conn schema clone. Mirrors the
sales_transactions/received_payments/paid_invoices idiom already
established in tests/test_payments_alloc.py.
"""
from datetime import date, timedelta

import pytest

import payments_alloc as pa


# ── synthetic data builders ──────────────────────────────────────────────────

def _ins_sale(conn, doc_base, customer, customer_code, date_iso, net,
              line=1, vat_type=1):
    conn.execute(
        """INSERT INTO sales_transactions
           (date_iso, doc_no, doc_base, customer, customer_code,
            qty, unit, unit_price, vat_type, total, net)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (date_iso, f"{doc_base}-{line}", doc_base, customer, customer_code,
         1, 'ตัว', net, vat_type, net, net),
    )


def _ins_receipt(conn, re_no, customer, date_iso, cancelled=0, total=None):
    cur = conn.execute(
        """INSERT INTO received_payments
           (re_no, date_iso, customer, salesperson, cancelled, total)
           VALUES (?,?,?,?,?,?)""",
        (re_no, date_iso, customer, 'S1', cancelled, total),
    )
    return cur.lastrowid


def _ins_paid(conn, re_id, iv_no, amount):
    doc_kind = 'SR' if iv_no.startswith('SR') else 'IV'
    conn.execute(
        "INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount) VALUES (?,?,?,?)",
        (re_id, iv_no, doc_kind, amount),
    )


def _ins_sr(conn, sr_no, ref_invoice, customer, customer_code, date_iso, net,
            line=1, vat_type=1):
    """Sales-return (credit-note) line — same shape as parse_weekly emits."""
    conn.execute(
        """INSERT INTO sales_transactions
           (date_iso, doc_no, doc_base, ref_invoice, customer, customer_code,
            qty, unit, unit_price, vat_type, total, net)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (date_iso, f"{sr_no}-{line}", sr_no, ref_invoice, customer,
         customer_code, 1, 'ตัว', net, vat_type, net, net),
    )


def _overpay(conn, doc_base, customer, billed, paid, date_iso='2026-05-10',
             customer_code='C1'):
    """Convenience: one billed invoice + one receipt that pays MORE than
    billed, leaving credit = paid - billed."""
    _ins_sale(conn, doc_base, customer, customer_code, date_iso, billed,
              vat_type=1)
    re_id = _ins_receipt(conn, f"RE-{doc_base}", customer, date_iso,
                         total=paid)
    _ins_paid(conn, re_id, doc_base, paid)


# ── tests ────────────────────────────────────────────────────────────────────

def test_basic_overpaid_invoice_appears(empty_db_conn):
    _overpay(empty_db_conn, 'IV001', 'Acme', billed=100.0, paid=150.0)
    rows = pa.customer_credit_rows(threshold=0.0, conn=empty_db_conn)
    assert len(rows) == 1
    r = rows[0]
    assert r['doc_base'] == 'IV001'
    assert r['customer'] == 'Acme'
    assert r['credit'] == 50.0
    assert r['credit'] > 0   # invariant: never expose a signed/negative credit


def test_eps_clamp_excludes_near_zero_credit(empty_db_conn):
    # _EPS = 0.005; an over-collection of 0.003 (sub-cent) must be clamped
    # away by _reconcile and therefore must not appear at any threshold.
    _overpay(empty_db_conn, 'IV002', 'Rounding', billed=100.0, paid=100.003)
    rows = pa.customer_credit_rows(threshold=0.0, conn=empty_db_conn)
    assert rows == []


def test_filters_below_threshold(empty_db_conn):
    # Two overpaid invoices: ฿2 (noise) and ฿10 (material).
    _overpay(empty_db_conn, 'IV003', 'A', billed=100.0, paid=102.0)
    _overpay(empty_db_conn, 'IV004', 'B', billed=100.0, paid=110.0)
    rows = pa.customer_credit_rows(threshold=5.0, conn=empty_db_conn)
    assert [r['doc_base'] for r in rows] == ['IV004']


def test_show_all_threshold_zero_includes_everything_above_eps(empty_db_conn):
    _overpay(empty_db_conn, 'IV005', 'A', billed=100.0, paid=102.0)
    _overpay(empty_db_conn, 'IV006', 'B', billed=100.0, paid=110.0)
    rows = pa.customer_credit_rows(threshold=0.0, conn=empty_db_conn)
    assert sorted(r['doc_base'] for r in rows) == ['IV005', 'IV006']


def test_sort_credit_desc_then_invoice_date_desc(empty_db_conn):
    # Same credit (฿20), different dates — newer first.
    _overpay(empty_db_conn, 'IV007', 'A', billed=100.0, paid=120.0,
             date_iso='2026-01-15')
    _overpay(empty_db_conn, 'IV008', 'B', billed=100.0, paid=120.0,
             date_iso='2026-04-15')
    # Bigger credit always wins regardless of date.
    _overpay(empty_db_conn, 'IV009', 'C', billed=100.0, paid=200.0,
             date_iso='2025-12-01')
    rows = pa.customer_credit_rows(threshold=0.0, conn=empty_db_conn)
    assert [r['doc_base'] for r in rows] == ['IV009', 'IV008', 'IV007']


def test_empty_when_no_overpaid(empty_db_conn):
    # Underpaid invoice — must NOT surface here.
    _ins_sale(empty_db_conn, 'IV010', 'A', 'C1', '2026-05-01', 100.0)
    re_id = _ins_receipt(empty_db_conn, 'RE-IV010', 'A', '2026-05-01',
                         total=80.0)
    _ins_paid(empty_db_conn, re_id, 'IV010', 80.0)
    assert pa.customer_credit_rows(threshold=0.0, conn=empty_db_conn) == []


def test_days_old_computed_from_today(empty_db_conn):
    # Invoice 30 days old → days_old == 30.
    invoice_date = (date.today() - timedelta(days=30)).isoformat()
    _overpay(empty_db_conn, 'IV011', 'A', billed=100.0, paid=120.0,
             date_iso=invoice_date)
    rows = pa.customer_credit_rows(threshold=0.0, conn=empty_db_conn)
    assert rows[0]['days_old'] == 30


# ── adversarial paths (Codex review WARN 3) ─────────────────────────────────

def test_sr_fully_credited_does_not_appear(empty_db_conn):
    """IV billed, then a credit note for the full amount, nothing collected.
    `_reconcile` flags status='fully_credited' with outstanding=0, NOT
    overpaid — the helper must keep it out of the list."""
    _ins_sale(empty_db_conn, 'IV020', 'A', 'C1', '2026-05-01', 100.0)
    _ins_sr(empty_db_conn, 'SR020', 'IV020', 'A', 'C1', '2026-05-02', 100.0)
    # No receipt at all.
    assert pa.customer_credit_rows(threshold=0.0, conn=empty_db_conn) == []


def test_cancelled_receipt_does_not_register_overpay(empty_db_conn):
    """A cancelled receipt MUST NOT contribute to `collected`. Otherwise a
    bill paid via a later non-cancelled receipt could look overpaid here."""
    _ins_sale(empty_db_conn, 'IV021', 'A', 'C1', '2026-05-01', 100.0)
    # Cancelled link points to an apparent overpay — must be ignored.
    cancelled = _ins_receipt(empty_db_conn, 'RE-021c', 'A', '2026-05-02',
                             cancelled=1, total=200.0)
    _ins_paid(empty_db_conn, cancelled, 'IV021', 200.0)
    # Real receipt that fully settles at face value.
    re_id = _ins_receipt(empty_db_conn, 'RE-021', 'A', '2026-05-03',
                         total=100.0)
    _ins_paid(empty_db_conn, re_id, 'IV021', 100.0)
    rows = pa.customer_credit_rows(threshold=0.0, conn=empty_db_conn)
    assert rows == []


def test_multiline_invoice_overpaid_emits_one_row(empty_db_conn):
    """One doc_base with multiple sales_transactions lines that together
    bill ฿1,000; customer pays ฿1,200 → ONE row in the helper output
    (not one per line)."""
    _ins_sale(empty_db_conn, 'IV022', 'A', 'C1', '2026-05-01', 700.0, line=1)
    _ins_sale(empty_db_conn, 'IV022', 'A', 'C1', '2026-05-01', 300.0, line=2)
    re_id = _ins_receipt(empty_db_conn, 'RE-022', 'A', '2026-05-01',
                         total=1200.0)
    _ins_paid(empty_db_conn, re_id, 'IV022', 1200.0)
    rows = pa.customer_credit_rows(threshold=0.0, conn=empty_db_conn)
    assert len(rows) == 1
    assert rows[0]['doc_base'] == 'IV022'
    assert rows[0]['billed'] == 1000.0
    assert rows[0]['credit'] == 200.0


def test_vat2_billed_uses_inclusive_amount(empty_db_conn):
    """vat_type=2 means 'แยก VAT' → customer pays net*1.07. A net=100 bill
    actually charges ฿107; paying ฿110 leaves a ฿3 credit. The helper
    reads the VAT-aware `billed` field from `_reconcile`."""
    _ins_sale(empty_db_conn, 'IV023', 'A', 'C1', '2026-05-01', 100.0,
              vat_type=2)
    re_id = _ins_receipt(empty_db_conn, 'RE-023', 'A', '2026-05-01',
                         total=110.0)
    _ins_paid(empty_db_conn, re_id, 'IV023', 110.0)
    rows = pa.customer_credit_rows(threshold=0.0, conn=empty_db_conn)
    assert len(rows) == 1
    r = rows[0]
    assert r['billed'] == 107.0   # 100 * 1.07
    assert r['credit'] == 3.0     # 110 - 107


def test_as_of_excludes_future_dated_invoice(empty_db_conn):
    """Point-in-time today: a 2027-dated overpay (data-entry typo) must
    not appear when `as_of` defaults to today, and `days_old` must never
    be negative."""
    future = (date.today() + timedelta(days=400)).isoformat()
    _overpay(empty_db_conn, 'IV024', 'A', billed=100.0, paid=200.0,
             date_iso=future)
    rows = pa.customer_credit_rows(threshold=0.0, conn=empty_db_conn)
    assert rows == []
    # Explicit as_of of the future date opens the cap.
    rows2 = pa.customer_credit_rows(threshold=0.0, as_of=future,
                                    conn=empty_db_conn)
    assert len(rows2) == 1
