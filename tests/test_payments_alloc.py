"""TDD tests for inventory_app/payments_alloc.py — pure payment
reconciliation + FIFO allocation.

Synthetic data only (built on empty_db_conn schema clone). Mirrors the
established join idiom from models.get_payment_summary:
  paid_invoices.iv_no = sales_transactions.doc_base
  received_payments.cancelled = 0
"""
import pytest

import payments_alloc as pa


# ── synthetic data builders ──────────────────────────────────────────────────

def _ins_sale(conn, doc_base, customer, customer_code, date_iso, net,
              line=1, vat_type=1):
    """One sales_transactions line. doc_base == doc_no for simplicity."""
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
    conn.execute(
        "INSERT INTO paid_invoices (re_id, iv_no, amount) VALUES (?,?,?)",
        (re_id, iv_no, amount),
    )


def _by_doc(rows):
    return {r['doc_base']: r for r in rows}


# ── invoice_settlement ───────────────────────────────────────────────────────

def test_billed_sums_multiline_and_excludes_cancelled(empty_db_conn):
    c = empty_db_conn
    # IV001: two lines 700 + 300 = 1000 billed
    _ins_sale(c, 'IV001', 'ACME', 'C01', '2026-01-10', 700, line=1)
    _ins_sale(c, 'IV001', 'ACME', 'C01', '2026-01-10', 300, line=2)
    # one good receipt 1000, one cancelled receipt that must be ignored
    r_good = _ins_receipt(c, 'RE001', 'ACME', '2026-02-01', total=1000)
    _ins_paid(c, r_good, 'IV001', 1000)
    r_bad = _ins_receipt(c, 'RE-CANCEL', 'ACME', '2026-02-05',
                         cancelled=1, total=1000)
    _ins_paid(c, r_bad, 'IV001', 9999)
    c.commit()

    rows = _by_doc(pa.invoice_settlement(conn=c))
    iv = rows['IV001']
    assert iv['billed'] == 1000.0
    assert iv['collected'] == 1000.0
    assert iv['outstanding'] == 0.0
    assert iv['status'] == 'paid'
    assert iv['last_payment_date'] == '2026-02-01'  # cancelled excluded
    assert iv['customer'] == 'ACME'
    assert iv['customer_code'] == 'C01'
    assert iv['invoice_date'] == '2026-01-10'


def test_partial_payment(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV002', 'ACME', 'C01', '2026-01-15', 1000)
    r = _ins_receipt(c, 'RE002', 'ACME', '2026-02-10', total=600)
    _ins_paid(c, r, 'IV002', 600)
    c.commit()

    iv = _by_doc(pa.invoice_settlement(conn=c))['IV002']
    assert iv['billed'] == 1000.0
    assert iv['collected'] == 600.0
    assert iv['outstanding'] == 400.0
    assert iv['status'] == 'partial'


def test_legacy_null_amount_link_treated_as_fully_paid(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV003', 'ACME', 'C01', '2026-01-20', 1500)
    # pre-058 legacy link: amount IS NULL → invoice considered fully settled
    r = _ins_receipt(c, 'RE003', 'ACME', '2026-02-12', total=None)
    _ins_paid(c, r, 'IV003', None)
    c.commit()

    iv = _by_doc(pa.invoice_settlement(conn=c))['IV003']
    assert iv['collected'] == 1500.0
    assert iv['outstanding'] == 0.0
    assert iv['status'] == 'paid'


def test_mixed_real_and_null_links_uses_real_only(empty_db_conn):
    """has_real wins over has_null: a real-amount link plus a NULL legacy
    link on the SAME invoice → collected = Σ real (NOT billed)."""
    c = empty_db_conn
    _ins_sale(c, 'IV-MIX', 'ACME', 'C01', '2026-01-20', 1000)
    r_a = _ins_receipt(c, 'RE-MIX-A', 'ACME', '2026-02-01', total=400)
    _ins_paid(c, r_a, 'IV-MIX', 400)                # real
    r_b = _ins_receipt(c, 'RE-MIX-B', 'ACME', '2026-02-10', total=None)
    _ins_paid(c, r_b, 'IV-MIX', None)               # NULL legacy — ignored
    c.commit()

    iv = _by_doc(pa.invoice_settlement(conn=c))['IV-MIX']
    assert iv['billed'] == 1000.0
    assert iv['collected'] == 400.0
    assert iv['outstanding'] == 600.0
    assert iv['status'] == 'partial'


def test_pure_legacy_single_null_link_is_paid(empty_db_conn):
    """No real link, one NULL link → collected = billed, paid."""
    c = empty_db_conn
    _ins_sale(c, 'IV-LEG', 'ACME', 'C01', '2026-01-20', 500)
    r = _ins_receipt(c, 'RE-LEG', 'ACME', '2026-02-12', total=None)
    _ins_paid(c, r, 'IV-LEG', None)
    c.commit()

    iv = _by_doc(pa.invoice_settlement(conn=c))['IV-LEG']
    assert iv['billed'] == 500.0
    assert iv['collected'] == 500.0
    assert iv['outstanding'] == 0.0
    assert iv['status'] == 'paid'


def test_all_real_multi_receipt_sums_to_paid(empty_db_conn):
    """All-real links across two receipts 300+700 → 1000 paid."""
    c = empty_db_conn
    _ins_sale(c, 'IV-AR', 'ACME', 'C01', '2026-01-27', 1000)
    r1 = _ins_receipt(c, 'RE-AR-A', 'ACME', '2026-02-01', total=300)
    _ins_paid(c, r1, 'IV-AR', 300)
    r2 = _ins_receipt(c, 'RE-AR-B', 'ACME', '2026-03-01', total=700)
    _ins_paid(c, r2, 'IV-AR', 700)
    c.commit()

    iv = _by_doc(pa.invoice_settlement(conn=c))['IV-AR']
    assert iv['collected'] == 1000.0
    assert iv['outstanding'] == 0.0
    assert iv['status'] == 'paid'


def test_real_overpaid_flagged_negative(empty_db_conn):
    """Real amount 1200 vs billed 1000 → outstanding -200, overpaid."""
    c = empty_db_conn
    _ins_sale(c, 'IV-OP', 'ACME', 'C01', '2026-01-26', 1000)
    r = _ins_receipt(c, 'RE-OP', 'ACME', '2026-02-15', total=1200)
    _ins_paid(c, r, 'IV-OP', 1200)
    c.commit()

    iv = _by_doc(pa.invoice_settlement(conn=c))['IV-OP']
    assert iv['collected'] == 1200.0
    assert iv['outstanding'] == -200.0
    assert iv['status'] == 'overpaid'


def test_unpaid_invoice(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV004', 'ACME', 'C01', '2026-01-25', 500)
    c.commit()

    iv = _by_doc(pa.invoice_settlement(conn=c))['IV004']
    assert iv['collected'] == 0.0
    assert iv['outstanding'] == 500.0
    assert iv['status'] == 'unpaid'
    assert iv['last_payment_date'] is None


def test_overpaid_flagged(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV005', 'ACME', 'C01', '2026-01-26', 1000)
    r = _ins_receipt(c, 'RE005', 'ACME', '2026-02-15', total=1200)
    _ins_paid(c, r, 'IV005', 1200)
    c.commit()

    iv = _by_doc(pa.invoice_settlement(conn=c))['IV005']
    assert iv['collected'] == 1200.0
    assert iv['outstanding'] == -200.0  # genuine negative NOT clamped
    assert iv['status'] == 'overpaid'


def test_multi_receipt_invoice_sums_to_paid(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV006', 'ACME', 'C01', '2026-01-27', 1000)
    r1 = _ins_receipt(c, 'RE006A', 'ACME', '2026-02-01', total=300)
    _ins_paid(c, r1, 'IV006', 300)
    r2 = _ins_receipt(c, 'RE006B', 'ACME', '2026-03-01', total=700)
    _ins_paid(c, r2, 'IV006', 700)
    c.commit()

    iv = _by_doc(pa.invoice_settlement(conn=c))['IV006']
    assert iv['collected'] == 1000.0
    assert iv['outstanding'] == 0.0
    assert iv['status'] == 'paid'
    assert iv['last_payment_date'] == '2026-03-01'


def test_filter_by_customer_and_dates(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IVA', 'ACME', 'C01', '2026-01-05', 100)
    _ins_sale(c, 'IVB', 'OTHER', 'C02', '2026-01-06', 200)
    _ins_sale(c, 'IVC', 'ACME', 'C01', '2026-03-30', 300)
    c.commit()

    only_acme = _by_doc(pa.invoice_settlement(customer='ACME', conn=c))
    assert set(only_acme) == {'IVA', 'IVC'}

    in_jan = _by_doc(pa.invoice_settlement(
        date_from='2026-01-01', date_to='2026-01-31', conn=c))
    assert set(in_jan) == {'IVA', 'IVB'}


# ── customer_outstanding ─────────────────────────────────────────────────────

def test_customer_outstanding_point_in_time(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IVX', 'ACME', 'C01', '2026-01-10', 1000)
    r = _ins_receipt(c, 'REX', 'ACME', '2026-03-15', total=1000)
    _ins_paid(c, r, 'IVX', 1000)
    c.commit()

    # Before the payment date: still fully outstanding
    before = {x['customer']: x
              for x in pa.customer_outstanding(as_of='2026-02-01', conn=c)}
    assert before['ACME']['outstanding'] == 1000.0
    assert before['ACME']['open_invoices'] == 1
    assert before['ACME']['oldest_unpaid_date'] == '2026-01-10'

    # After: paid, zero outstanding
    after = {x['customer']: x
             for x in pa.customer_outstanding(as_of='2026-04-01', conn=c)}
    assert after['ACME']['outstanding'] == 0.0
    assert after['ACME']['open_invoices'] == 0


def test_customer_outstanding_sorted_desc(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV-S', 'SMALL', 'C03', '2026-01-01', 50)
    _ins_sale(c, 'IV-B', 'BIG', 'C04', '2026-01-01', 9000)
    c.commit()

    rows = pa.customer_outstanding(as_of='2026-12-31', conn=c)
    assert [r['customer'] for r in rows] == ['BIG', 'SMALL']
    assert rows[0]['billed'] == 9000.0


# ── allocate_fifo ────────────────────────────────────────────────────────────

@pytest.fixture
def fifo_customer(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'F-JAN', 'FIFO', 'C09', '2026-01-01', 100)
    _ins_sale(c, 'F-FEB', 'FIFO', 'C09', '2026-02-01', 200)
    _ins_sale(c, 'F-MAR', 'FIFO', 'C09', '2026-03-01', 300)
    c.commit()
    return c


def test_fifo_partial_fill_oldest_first(fifo_customer):
    res = pa.allocate_fifo('FIFO', 250, conn=fifo_customer)
    al = res['allocations']
    assert [a['doc_base'] for a in al] == ['F-JAN', 'F-FEB']
    assert al[0]['applied'] == 100.0
    assert al[0]['outstanding_after'] == 0.0
    assert al[1]['applied'] == 150.0
    assert al[1]['outstanding_before'] == 200.0
    assert al[1]['outstanding_after'] == 50.0
    assert res['unapplied'] == 0.0
    assert res['total_applied'] == 250.0


def test_fifo_overpay_remainder_unapplied(fifo_customer):
    res = pa.allocate_fifo('FIFO', 700, conn=fifo_customer)
    al = res['allocations']
    assert [a['doc_base'] for a in al] == ['F-JAN', 'F-FEB', 'F-MAR']
    assert all(a['outstanding_after'] == 0.0 for a in al)
    assert res['total_applied'] == 600.0
    assert res['unapplied'] == 100.0


def test_fifo_zero_and_negative(fifo_customer):
    for amt in (0, -50):
        res = pa.allocate_fifo('FIFO', amt, conn=fifo_customer)
        assert res['allocations'] == []
        assert res['total_applied'] == 0.0
        # amt <= 0 → planner returns no leftover (nothing to apply)
        assert res['unapplied'] == 0.0


def test_fifo_customer_with_no_outstanding(empty_db_conn):
    res = pa.allocate_fifo('NOBODY', 500, conn=empty_db_conn)
    assert res['allocations'] == []
    assert res['total_applied'] == 0.0
    assert res['unapplied'] == 500.0


def test_fifo_exactly_clears_two(fifo_customer):
    res = pa.allocate_fifo('FIFO', 300, conn=fifo_customer)
    al = res['allocations']
    assert [a['doc_base'] for a in al] == ['F-JAN', 'F-FEB']
    assert all(a['outstanding_after'] == 0.0 for a in al)
    assert res['unapplied'] == 0.0
    assert res['total_applied'] == 300.0


def test_fifo_tie_break_by_doc_base(empty_db_conn):
    c = empty_db_conn
    # same date → deterministic tie-break by doc_base asc
    _ins_sale(c, 'F-B', 'TIE', 'C10', '2026-01-01', 100)
    _ins_sale(c, 'F-A', 'TIE', 'C10', '2026-01-01', 100)
    c.commit()
    res = pa.allocate_fifo('TIE', 100, conn=c)
    assert [a['doc_base'] for a in res['allocations']] == ['F-A']


def test_fifo_skips_already_paid_invoice(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'P-PAID', 'MIX', 'C11', '2026-01-01', 100)
    _ins_sale(c, 'P-OPEN', 'MIX', 'C11', '2026-02-01', 200)
    r = _ins_receipt(c, 'RE-MIX', 'MIX', '2026-01-15', total=100)
    _ins_paid(c, r, 'P-PAID', 100)
    c.commit()
    res = pa.allocate_fifo('MIX', 150, conn=c)
    al = res['allocations']
    assert [a['doc_base'] for a in al] == ['P-OPEN']
    assert al[0]['applied'] == 150.0
    assert al[0]['outstanding_after'] == 50.0
