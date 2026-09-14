"""#499 — how many days a customer usually takes to pay.

Receipt history over SETTLED invoices: the median of (settling receipt date −
invoice date) over the customer's latest 20 settled invoices. It is neither
**outstanding** nor **chaseable** (ADR 0012) — those name two populations of
UNPAID bills from the Express snapshot; this figure is read off the receipts.

Seams (agreed in triage):
    payments_alloc.invoice_settlement(customer_code=...)   exact code filter
    payments_alloc.payment_speed(customer_code)            the figure, or None

Synthetic data on the `empty_db` schema clone (zero rows), so nothing is
inherited from the live dev DB. Every expected value is a literal worked out by
hand from the fixture's own dates, never recomputed the way the code does.
"""
import pytest

import payments_alloc as pa


CODE = 'ZZPAY1'
NAME = 'ร้านทดสอบจ่ายเงิน'


def _iv(c, doc, date_iso, net, code=CODE, name=NAME, vat_type=1):
    """One invoice line in the real shape: doc_no '<base>-1', doc_base '<base>'."""
    c.execute(
        """INSERT INTO sales_transactions
           (date_iso, doc_no, doc_base, customer, customer_code,
            qty, unit, unit_price, vat_type, total, net)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (date_iso, f'{doc}-1', doc, name, code, 1, 'ตัว', net, vat_type, net, net),
    )


def _re(c, re_no, date_iso, links, cancelled=0, name=NAME):
    """One Express receipt and its paid_invoices links [(doc_no, amount)].

    doc_kind follows the prefix: an SR link is the credit-note netting row a
    receipt carries (negative amount); amount None is a pre-mig-058 legacy link.
    """
    rid = c.execute(
        """INSERT INTO received_payments
           (re_no, date_iso, customer, salesperson, cancelled, total)
           VALUES (?,?,?,?,?,?)""",
        (re_no, date_iso, name, 'S1', cancelled, None),
    ).lastrowid
    for doc, amount in links:
        c.execute(
            "INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount) VALUES (?,?,?,?)",
            (rid, doc, 'SR' if doc.startswith('SR') else 'IV', amount),
        )
    return rid


# ── invoice_settlement: the exact customer-code filter ──────────────────────

def test_invoice_settlement_filters_by_exact_customer_code(empty_db_conn):
    """Two codes share one bill name. The code filter returns only its own
    invoices; the existing NAME filter is unchanged and still returns both."""
    c = empty_db_conn
    _iv(c, 'IVA1', '2026-01-05', 1000, code='ZZSAME1', name='ร้านชื่อซ้ำ')
    _iv(c, 'IVB1', '2026-01-06', 2000, code='ZZSAME2', name='ร้านชื่อซ้ำ')
    c.commit()

    by_code = pa.invoice_settlement(customer_code='ZZSAME1', conn=c)
    assert len(by_code) == 1
    assert by_code[0]['doc_base'] == 'IVA1'
    assert by_code[0]['customer_code'] == 'ZZSAME1'

    by_name = pa.invoice_settlement(customer='ร้านชื่อซ้ำ', conn=c)
    assert sorted(r['doc_base'] for r in by_name) == ['IVA1', 'IVB1']
