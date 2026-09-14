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


# ── payment_speed ────────────────────────────────────────────────────────────

# Three invoices, each settled in full by its own receipt 30 days later. Enough
# for a figure to exist, so a case that must be EXCLUDED has a non-empty sample
# to be absent from (an exclusion asserted over an empty sample pins nothing).
FILLERS = [('IVF1', '2025-06-02', '2025-07-02'),
           ('IVF2', '2025-06-03', '2025-07-03'),
           ('IVF3', '2025-06-04', '2025-07-04')]


def _fillers(c):
    for doc, inv_date, paid_date in FILLERS:
        _iv(c, doc, inv_date, 1000)
        _re(c, f'RE-{doc}', paid_date, [(doc, 1000)])


def _cn(c, sr, ref_invoice, amount, date_iso):
    """A credit note against `ref_invoice`: its SR line plus the authoritative
    credit_note_amounts row, the shape the ใบลดหนี้ import leaves behind."""
    c.execute(
        """INSERT INTO sales_transactions
           (date_iso, doc_no, doc_base, ref_invoice, customer, customer_code,
            qty, unit, unit_price, vat_type, total, net)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (date_iso, f'{sr}-1', sr, ref_invoice, NAME, CODE, 1, 'ตัว',
         amount, 1, amount, amount),
    )
    c.execute(
        """INSERT INTO credit_note_amounts
           (sr_doc_base, ref_invoice, credited_amount, sr_date_iso, customer, source)
           VALUES (?,?,?,?,?,?)""",
        (sr, ref_invoice, amount, date_iso, NAME, 'test'),
    )


def _docs(ps):
    return [s['doc_base'] for s in ps['sample']]


def _row(ps, doc):
    rows = [s for s in ps['sample'] if s['doc_base'] == doc]
    assert len(rows) == 1, f'{doc} is not in the sample exactly once: {_docs(ps)}'
    return rows[0]


def _status(c, doc):
    """CONTROL: what the settlement engine itself says about one invoice, so an
    exclusion is shown to happen for the stated reason and not a broken seed."""
    rows = [r for r in pa.invoice_settlement(customer_code=CODE, conn=c)
            if r['doc_base'] == doc]
    assert len(rows) == 1
    return rows[0]


def test_one_receipt_20_days_after_the_invoice_counts_20(empty_db_conn):
    c = empty_db_conn
    for doc, inv_date, paid_date in [('IV1', '2026-01-10', '2026-01-30'),
                                     ('IV2', '2026-02-01', '2026-02-21'),
                                     ('IV3', '2026-03-02', '2026-03-22')]:
        _iv(c, doc, inv_date, 1000)
        _re(c, f'RE-{doc}', paid_date, [(doc, 1000)])
    c.commit()

    ps = pa.payment_speed(CODE, conn=c)
    assert ps['invoices'] == 3
    assert [s['days'] for s in ps['sample']] == [20, 20, 20]
    assert ps['median_days'] == 20
    assert ps['receipts'] == 3
    assert ps['first_invoice_date'] == '2026-01-10'
    assert ps['last_invoice_date'] == '2026-03-02'


def test_one_collection_round_paying_three_bills_is_one_receipt(empty_db_conn):
    """The receipt count is DISTINCT receipts: an upcountry rep collecting three
    bills on one visit writes one RE. That is the context the figure needs —
    for these shops "days to pay" mostly measures how often someone collected."""
    c = empty_db_conn
    _iv(c, 'IVR1', '2026-01-05', 1000)
    _iv(c, 'IVR2', '2026-01-15', 2000)
    _iv(c, 'IVR3', '2026-01-25', 3000)
    _re(c, 'RE-ROUND', '2026-02-24', [('IVR1', 1000), ('IVR2', 2000), ('IVR3', 3000)])
    c.commit()

    ps = pa.payment_speed(CODE, conn=c)
    assert ps['invoices'] == 3
    assert ps['receipts'] == 1
    assert [s['days'] for s in ps['sample']] == [50, 40, 30]
    assert ps['median_days'] == 40


def test_instalments_count_the_receipt_that_completed_the_bill(empty_db_conn):
    c = empty_db_conn
    _fillers(c)
    _iv(c, 'IVI', '2026-01-01', 1000)
    _re(c, 'RE-I1', '2026-01-11', [('IVI', 400)])   # day 10
    _re(c, 'RE-I2', '2026-02-10', [('IVI', 600)])   # day 40 — completes it
    c.commit()

    ps = pa.payment_speed(CODE, conn=c)
    assert ps['invoices'] == 4
    row = _row(ps, 'IVI')
    assert row['settle_date'] == '2026-02-10'
    assert row['days'] == 40
    assert ps['receipts'] == 5            # 3 filler receipts + both instalments


def test_partial_payment_only_is_excluded(empty_db_conn):
    c = empty_db_conn
    _fillers(c)
    _iv(c, 'IVP', '2026-01-01', 1000)
    _re(c, 'RE-P', '2026-01-21', [('IVP', 600)])
    c.commit()
    assert _status(c, 'IVP')['status'] == 'partial'
    assert _status(c, 'IVP')['last_payment_date'] == '2026-01-21'

    ps = pa.payment_speed(CODE, conn=c)
    assert ps['invoices'] == 3
    assert 'IVP' not in _docs(ps)
    assert ps['receipts'] == 3            # RE-P paid no invoice in the sample


def test_only_a_cancelled_receipt_is_excluded(empty_db_conn):
    c = empty_db_conn
    _fillers(c)
    _iv(c, 'IVX', '2026-01-01', 1000)
    _re(c, 'RE-X', '2026-01-21', [('IVX', 1000)], cancelled=1)
    c.commit()
    assert _status(c, 'IVX')['status'] == 'unpaid'

    ps = pa.payment_speed(CODE, conn=c)
    assert ps['invoices'] == 3
    assert 'IVX' not in _docs(ps)
    assert ps['receipts'] == 3


def test_vat2_invoice_paid_at_net_x_1_07_is_in_and_at_net_only_is_out(empty_db_conn):
    """แยก VAT: the customer owes net × 1.07. Paid at that, it is settled;
    paid at net only, it still owes the VAT and is a partial."""
    c = empty_db_conn
    _fillers(c)
    _iv(c, 'IVV1', '2026-01-01', 1000, vat_type=2)
    _re(c, 'RE-V1', '2026-01-16', [('IVV1', 1070)])       # day 15
    _iv(c, 'IVV2', '2026-01-02', 1000, vat_type=2)
    _re(c, 'RE-V2', '2026-01-17', [('IVV2', 1000)])
    c.commit()
    assert _status(c, 'IVV2')['status'] == 'partial'

    ps = pa.payment_speed(CODE, conn=c)
    assert ps['invoices'] == 4
    assert sorted(_docs(ps)) == ['IVF1', 'IVF2', 'IVF3', 'IVV1']
    assert _row(ps, 'IVV1')['days'] == 15


def test_partly_credited_invoice_settles_on_its_receipt_date(empty_db_conn):
    """Real Express shape (SR6900009 on IV6802996, prod): one receipt carries the
    invoice at its full amount and the credit note as a negative SR link."""
    c = empty_db_conn
    _fillers(c)
    _iv(c, 'IVC', '2026-01-01', 1000)
    _cn(c, 'SRC', 'IVC', 200, '2026-01-05')
    _re(c, 'RE-C', '2026-01-26', [('IVC', 1000), ('SRC', -200)])   # day 25
    c.commit()
    assert _status(c, 'IVC')['net_owed'] == 800.0

    ps = pa.payment_speed(CODE, conn=c)
    assert ps['invoices'] == 4
    row = _row(ps, 'IVC')
    assert row['settle_date'] == '2026-01-26'
    assert row['days'] == 25


def test_fully_credited_invoice_is_excluded_even_with_a_receipt(empty_db_conn):
    """68 fully-credited invoices on prod carry a receipt that nets the invoice
    against its credit note to zero, so they HAVE a last_payment_date. Only the
    settled-status filter keeps them out."""
    c = empty_db_conn
    _fillers(c)
    _iv(c, 'IVFC', '2026-01-01', 500)
    _cn(c, 'SRFC', 'IVFC', 500, '2026-01-03')
    _re(c, 'RE-FC', '2026-01-20', [('IVFC', 500), ('SRFC', -500)])
    c.commit()
    ctl = _status(c, 'IVFC')
    assert ctl['status'] == 'fully_credited'
    assert ctl['last_payment_date'] == '2026-01-20'

    ps = pa.payment_speed(CODE, conn=c)
    assert ps['invoices'] == 3
    assert 'IVFC' not in _docs(ps)


def test_over_credited_invoice_with_no_receipt_is_excluded(empty_db_conn):
    """Credited beyond its bill with nothing collected, the engine reads the
    invoice as 'overpaid' — but no receipt ever settled it (0 such on prod
    2026-09-12, and it must not crash the page when one appears)."""
    c = empty_db_conn
    _fillers(c)
    _iv(c, 'IVO', '2026-01-01', 500)
    _cn(c, 'SRO', 'IVO', 600, '2026-01-03')
    c.commit()
    ctl = _status(c, 'IVO')
    assert ctl['status'] == 'overpaid'
    assert ctl['last_payment_date'] is None

    ps = pa.payment_speed(CODE, conn=c)
    assert ps['invoices'] == 3
    assert 'IVO' not in _docs(ps)


def test_legacy_null_amount_link_counts_on_its_receipt_date(empty_db_conn):
    c = empty_db_conn
    _fillers(c)
    _iv(c, 'IVL', '2024-03-01', 1500)
    _re(c, 'RE-L', '2024-04-15', [('IVL', None)])   # pre-mig-058 link, day 45
    c.commit()

    ps = pa.payment_speed(CODE, conn=c)
    assert ps['invoices'] == 4
    row = _row(ps, 'IVL')
    assert row['settle_date'] == '2024-04-15'
    assert row['days'] == 45


def test_receipt_dated_a_day_before_its_invoice_counts_minus_one(empty_db_conn):
    """IV6900848 (prod): invoice 2026-06-05, receipt 2026-06-04. Counted at its
    real difference — no special case."""
    c = empty_db_conn
    _fillers(c)
    _iv(c, 'IVE', '2026-06-05', 1000)
    _re(c, 'RE-E', '2026-06-04', [('IVE', 1000)])
    c.commit()

    ps = pa.payment_speed(CODE, conn=c)
    assert ps['invoices'] == 4
    assert _row(ps, 'IVE')['days'] == -1


def test_window_keeps_only_the_latest_20_by_invoice_date(empty_db_conn):
    """25 settled invoices. The 5 OLDEST (January) were paid 100 days late, so
    they are the latest by SETTLE date, and their doc_bases sort LAST — a window
    ordered by settle date or doc_base would keep all five. By invoice date they
    are the five that fall out."""
    c = empty_db_conn
    old = [f'IVW90{i}' for i in range(1, 6)]
    for i, doc in enumerate(old, start=1):                 # 2025-01-01..05
        _iv(c, doc, f'2025-01-0{i}', 1000)
        _re(c, f'RE-{doc}', f'2025-04-{10 + i}', [(doc, 1000)])   # day 100
    new = [f'IVW1{k:02d}' for k in range(1, 21)]
    for k, doc in enumerate(new, start=1):                 # 2025-02-01..20
        _iv(c, doc, f'2025-02-{k:02d}', 1000)
    # Invoice k is settled k days after it (February 2025 has 28 days).
    settle = ['2025-02-02', '2025-02-04', '2025-02-06', '2025-02-08', '2025-02-10',
              '2025-02-12', '2025-02-14', '2025-02-16', '2025-02-18', '2025-02-20',
              '2025-02-22', '2025-02-24', '2025-02-26', '2025-02-28', '2025-03-02',
              '2025-03-04', '2025-03-06', '2025-03-08', '2025-03-10', '2025-03-12']
    for doc, paid in zip(new, settle):
        _re(c, f'RE-{doc}', paid, [(doc, 1000)])
    c.commit()
    assert len(pa.invoice_settlement(customer_code=CODE, conn=c)) == 25   # CONTROL

    ps = pa.payment_speed(CODE, conn=c)
    assert ps['invoices'] == 20
    assert sorted(_docs(ps)) == sorted(new)
    assert [s['days'] for s in ps['sample']] == list(range(1, 21))
    assert ps['median_days'] == 10.5
    assert ps['receipts'] == 20
    assert ps['first_invoice_date'] == '2025-02-01'
    assert ps['last_invoice_date'] == '2025-02-20'


def test_two_settled_invoices_give_no_figure(empty_db_conn):
    c = empty_db_conn
    _iv(c, 'IVT1', '2026-01-01', 1000)
    _re(c, 'RE-T1', '2026-01-11', [('IVT1', 1000)])
    _iv(c, 'IVT2', '2026-01-02', 1000)
    _re(c, 'RE-T2', '2026-01-12', [('IVT2', 1000)])
    _iv(c, 'IVT9', '2026-01-03', 1000)                 # partial: not a 3rd settled
    _re(c, 'RE-T9', '2026-01-13', [('IVT9', 500)])
    c.commit()
    assert pa.payment_speed(CODE, conn=c) is None

    # CONTROL: the same customer with a third settled invoice does get a figure,
    # so the None above is the threshold and not a seed that never registered.
    _iv(c, 'IVT3', '2026-01-04', 1000)
    _re(c, 'RE-T3', '2026-01-14', [('IVT3', 1000)])
    c.commit()
    ps = pa.payment_speed(CODE, conn=c)
    assert ps['invoices'] == 3
    assert ps['median_days'] == 10


def test_another_code_with_the_same_bill_name_does_not_leak(empty_db_conn):
    c = empty_db_conn
    shared = 'ร้านชื่อซ้ำ'
    for doc, inv, paid in [('IVA1', '2026-01-01', '2026-01-11'),
                           ('IVA2', '2026-01-02', '2026-01-12'),
                           ('IVA3', '2026-01-03', '2026-01-13')]:      # day 10
        _iv(c, doc, inv, 1000, code=CODE, name=shared)
        _re(c, f'RE-{doc}', paid, [(doc, 1000)], name=shared)
    for doc, inv, paid in [('IVB1', '2026-02-01', '2026-05-02'),
                           ('IVB2', '2026-02-02', '2026-05-03'),
                           ('IVB3', '2026-02-03', '2026-05-04')]:      # day 90
        _iv(c, doc, inv, 1000, code='ZZPAY2', name=shared)
        _re(c, f'RE-{doc}', paid, [(doc, 1000)], name=shared)
    c.commit()

    ps = pa.payment_speed(CODE, conn=c)
    assert ps['invoices'] == 3
    assert _docs(ps) == ['IVA1', 'IVA2', 'IVA3']
    assert ps['median_days'] == 10
    assert ps['receipts'] == 3

    other = pa.payment_speed('ZZPAY2', conn=c)
    assert other['invoices'] == 3
    assert other['median_days'] == 90


def test_blank_code_gives_no_figure_rather_than_the_whole_book(empty_db_conn):
    """The code filter is skipped when the code is falsy, so a blank code must
    stop before reaching the engine — otherwise it reads every customer's bills."""
    c = empty_db_conn
    _fillers(c)
    c.commit()
    assert pa.payment_speed(CODE, conn=c)['invoices'] == 3        # CONTROL
    assert pa.payment_speed('', conn=c) is None
    assert pa.payment_speed(None, conn=c) is None


def test_payment_speed_never_writes(empty_db, empty_db_conn):
    """Both pages call this on render; rendering never writes. A read-only
    connection refuses any write, so the helper must work on one."""
    import sqlite3
    _fillers(empty_db_conn)
    empty_db_conn.commit()
    ro = sqlite3.connect(f'file:{empty_db}?mode=ro', uri=True)
    ro.row_factory = sqlite3.Row
    try:
        ps = pa.payment_speed(CODE, conn=ro)
    finally:
        ro.close()
    assert ps['invoices'] == 3
    assert ps['median_days'] == 30
