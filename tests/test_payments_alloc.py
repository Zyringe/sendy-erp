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
    # iv_no kept as the local parameter name for legibility (test fixtures
    # speak in terms of "the invoice number being settled"); the DB column
    # was renamed to doc_no in mig 082. doc_kind derived from the prefix.
    doc_kind = 'SR' if iv_no.startswith('SR') else 'IV'
    conn.execute(
        "INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount) VALUES (?,?,?,?)",
        (re_id, iv_no, doc_kind, amount),
    )


def _ins_sr(conn, sr_no, ref_invoice, customer, customer_code, date_iso, net,
            line=1, vat_type=1):
    """One sales-return (credit-note) line.

    Stored exactly like parse_weekly emits SR rows: doc_base starts with
    'SR', net is the positive credit-note value, ref_invoice points at the
    original IV's doc_base (may be None / '' for unattributable returns).
    """
    conn.execute(
        """INSERT INTO sales_transactions
           (date_iso, doc_no, doc_base, ref_invoice, customer, customer_code,
            qty, unit, unit_price, vat_type, total, net)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (date_iso, f"{sr_no}-{line}", sr_no, ref_invoice, customer,
         customer_code, 1, 'ตัว', net, vat_type, net, net),
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


# ── credit notes (SR rows netting against the original invoice) ──────────────

def test_credit_note_reduces_net_owed_unpaid(empty_db_conn):
    """IV billed 1000, SR 300 against it, no payment.
    net_owed = 1000 - 300 = 700; outstanding 700; unpaid."""
    c = empty_db_conn
    _ins_sale(c, 'IV-CN1', 'ACME', 'C01', '2026-01-10', 1000)
    _ins_sr(c, 'SR-CN1', 'IV-CN1', 'ACME', 'C01', '2026-01-20', 300)
    c.commit()

    iv = _by_doc(pa.invoice_settlement(conn=c))['IV-CN1']
    assert iv['billed'] == 1000.0
    assert iv['credit_notes'] == 300.0
    assert iv['net_owed'] == 700.0
    assert iv['collected'] == 0.0
    assert iv['outstanding'] == 700.0
    assert iv['status'] == 'unpaid'


def test_credit_note_with_full_payment_of_net_owed_is_paid(empty_db_conn):
    """IV 1000, SR 300, payment 700 → net_owed 700, fully paid."""
    c = empty_db_conn
    _ins_sale(c, 'IV-CN2', 'ACME', 'C01', '2026-01-10', 1000)
    _ins_sr(c, 'SR-CN2', 'IV-CN2', 'ACME', 'C01', '2026-01-20', 300)
    r = _ins_receipt(c, 'RE-CN2', 'ACME', '2026-02-01', total=700)
    _ins_paid(c, r, 'IV-CN2', 700)
    c.commit()

    iv = _by_doc(pa.invoice_settlement(conn=c))['IV-CN2']
    assert iv['billed'] == 1000.0
    assert iv['credit_notes'] == 300.0
    assert iv['net_owed'] == 700.0
    assert iv['collected'] == 700.0
    assert iv['outstanding'] == 0.0
    assert iv['status'] == 'paid'


def test_credit_note_equals_billed_fully_credited(empty_db_conn):
    """IV 1000, SR 1000, no payment → net_owed 0, status fully_credited."""
    c = empty_db_conn
    _ins_sale(c, 'IV-CN3', 'ACME', 'C01', '2026-01-10', 1000)
    _ins_sr(c, 'SR-CN3', 'IV-CN3', 'ACME', 'C01', '2026-01-20', 1000)
    c.commit()

    iv = _by_doc(pa.invoice_settlement(conn=c))['IV-CN3']
    assert iv['billed'] == 1000.0
    assert iv['credit_notes'] == 1000.0
    assert iv['net_owed'] == 0.0
    assert iv['collected'] == 0.0
    assert iv['outstanding'] == 0.0
    assert iv['status'] == 'fully_credited'


def test_credit_note_then_overpaid_net_owed(empty_db_conn):
    """IV 1000, SR 200 → net_owed 800; payment 1000 → outstanding -200,
    overpaid."""
    c = empty_db_conn
    _ins_sale(c, 'IV-CN4', 'ACME', 'C01', '2026-01-10', 1000)
    _ins_sr(c, 'SR-CN4', 'IV-CN4', 'ACME', 'C01', '2026-01-20', 200)
    r = _ins_receipt(c, 'RE-CN4', 'ACME', '2026-02-01', total=1000)
    _ins_paid(c, r, 'IV-CN4', 1000)
    c.commit()

    iv = _by_doc(pa.invoice_settlement(conn=c))['IV-CN4']
    assert iv['billed'] == 1000.0
    assert iv['credit_notes'] == 200.0
    assert iv['net_owed'] == 800.0
    assert iv['collected'] == 1000.0
    assert iv['outstanding'] == -200.0
    assert iv['status'] == 'overpaid'


def test_multiple_credit_notes_summed(empty_db_conn):
    """Two SR rows (150 + 250) against one IV sum to 400 credit_notes."""
    c = empty_db_conn
    _ins_sale(c, 'IV-CN5', 'ACME', 'C01', '2026-01-10', 1000)
    _ins_sr(c, 'SR-CN5A', 'IV-CN5', 'ACME', 'C01', '2026-01-20', 150)
    _ins_sr(c, 'SR-CN5B', 'IV-CN5', 'ACME', 'C01', '2026-01-25', 250)
    c.commit()

    iv = _by_doc(pa.invoice_settlement(conn=c))['IV-CN5']
    assert iv['credit_notes'] == 400.0
    assert iv['net_owed'] == 600.0
    assert iv['outstanding'] == 600.0
    assert iv['status'] == 'unpaid'


def test_credit_note_does_not_bleed_to_other_invoice(empty_db_conn):
    """SR against IV-A must not reduce IV-B's net_owed."""
    c = empty_db_conn
    _ins_sale(c, 'IV-CNA', 'ACME', 'C01', '2026-01-10', 1000)
    _ins_sale(c, 'IV-CNB', 'ACME', 'C01', '2026-01-11', 500)
    _ins_sr(c, 'SR-CNA', 'IV-CNA', 'ACME', 'C01', '2026-01-20', 300)
    c.commit()

    rows = _by_doc(pa.invoice_settlement(conn=c))
    assert rows['IV-CNA']['credit_notes'] == 300.0
    assert rows['IV-CNA']['net_owed'] == 700.0
    assert rows['IV-CNB']['credit_notes'] == 0.0
    assert rows['IV-CNB']['net_owed'] == 500.0
    assert rows['IV-CNB']['outstanding'] == 500.0


def test_credit_note_null_ref_invoice_ignored(empty_db_conn):
    """SR with ref_invoice NULL/'' is unattributable — must not net against
    any invoice (and is excluded from settlement billing as an SR row)."""
    c = empty_db_conn
    _ins_sale(c, 'IV-CN6', 'ACME', 'C01', '2026-01-10', 1000)
    _ins_sr(c, 'SR-CN6N', None, 'ACME', 'C01', '2026-01-20', 300)
    _ins_sr(c, 'SR-CN6E', '', 'ACME', 'C01', '2026-01-21', 400)
    c.commit()

    rows = _by_doc(pa.invoice_settlement(conn=c))
    iv = rows['IV-CN6']
    assert iv['credit_notes'] == 0.0
    assert iv['net_owed'] == 1000.0
    assert iv['outstanding'] == 1000.0
    # SR rows themselves are not billable invoices
    assert 'SR-CN6N' not in rows
    assert 'SR-CN6E' not in rows
    assert pa.unattributable_sr_count(conn=c) == 2


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


# ═══════════════════════════════════════════════════════════════════════════════
# credit_note_amounts (migration 062) authoritative netting + SR(-) receipt link
# ═══════════════════════════════════════════════════════════════════════════════
#
# Two coupled changes the `cn` / `pay` CTEs must honour:
#
#  1. `cn` is now driven by credit_note_amounts (the authoritative master
#     "รวมทั้งสิ้น" value), joined ref_invoice = inv.doc_base. The old
#     sales_transactions SR.net sum is kept ONLY as a LEFT-JOIN fallback for
#     SR docs that are absent from credit_note_amounts (legacy / not yet
#     imported).
#
#  2. collected = Σ IV(+) receipt links − Σ SR(−) receipt links (the SR(−)
#     line that import_payments now persists into paid_invoices with a
#     negative amount and iv_no = SR doc_base).
#
# No double count: the SR reduces net_owed (via cn) AND reduces collected
# (via the SR(−) receipt link) by the SAME credited amount, so
# outstanding = net_owed − collected is unaffected by the credit's
# magnitude when the receipt actually applied it.

def _ensure_cna(conn):
    """Create credit_note_amounts in the schema-clone DB if absent."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='credit_note_amounts'"
    ).fetchone()
    if exists is None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS credit_note_amounts (
                id              INTEGER PRIMARY KEY,
                sr_doc_base     TEXT    NOT NULL,
                ref_invoice     TEXT,
                credited_amount REAL    NOT NULL DEFAULT 0.0,
                sr_date_iso     TEXT,
                customer        TEXT,
                source          TEXT,
                created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
                UNIQUE(sr_doc_base)
            );
            CREATE INDEX IF NOT EXISTS idx_cna_ref_invoice
                ON credit_note_amounts(ref_invoice);
        """)


def _ins_cna(conn, sr_doc_base, ref_invoice, credited_amount):
    conn.execute(
        """INSERT INTO credit_note_amounts
               (sr_doc_base, ref_invoice, credited_amount, sr_date_iso,
                customer, source)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(sr_doc_base) DO UPDATE SET
               ref_invoice=excluded.ref_invoice,
               credited_amount=excluded.credited_amount""",
        (sr_doc_base, ref_invoice, credited_amount, '2026-01-20',
         'ACME', 'test'),
    )


def test_iv6802996_oracle_shape(empty_db_conn):
    """The exact production shape that produced the false ฿105,604.

    IV6802996 billed 5242.02; SR6900009 credited 2293.20 (master total, NOT
    detail net 2340.00). RE6900208 applies IV6802996 +5242.02 and the SR(-)
    receipt link -2293.20.

    Expected: billed 5242.02 / credit_notes 2293.20 / collected 2948.82 /
    net_owed 2948.82 / outstanding 0.00 / status paid.
    """
    c = empty_db_conn
    _ensure_cna(c)
    # billed = 5242.02 across 6 lines (matches the live decomposition)
    for ln, net in enumerate(
            [1470.0, 882.0, 279.3, 317.52, 2293.2, 0.0], start=1):
        _ins_sale(c, 'IV6802996', 'เจริญทรัพย์การค้า', 'C06',
                  '2025-12-13', net, line=ln)
    # SR detail line net (2340.00) in sales_transactions — must be IGNORED
    # by `cn` because credit_note_amounts has the authoritative 2293.20.
    _ins_sr(c, 'SR6900009', 'IV6802996', 'เจริญทรัพย์การค้า', 'C06',
            '2026-03-27', 2340.0)
    _ins_cna(c, 'SR6900009', 'IV6802996', 2293.20)
    # Receipt: +IV link and -SR netting link
    r = _ins_receipt(c, 'RE6900208', 'เจริญทรัพย์การค้า', '2026-03-27',
                     total=5242.02)
    _ins_paid(c, r, 'IV6802996', 5242.02)
    _ins_paid(c, r, 'SR6900009', -2293.20)
    c.commit()

    iv = _by_doc(pa.invoice_settlement(conn=c))['IV6802996']
    assert iv['billed'] == 5242.02
    assert iv['credit_notes'] == 2293.20
    assert iv['collected'] == 2948.82
    assert iv['net_owed'] == 2948.82
    assert iv['outstanding'] == 0.0
    assert iv['status'] == 'paid'


def test_cn_uses_authoritative_amount_over_sr_net(empty_db_conn):
    """When credit_note_amounts has the SR, its credited_amount wins over the
    sales_transactions SR.net sum."""
    c = empty_db_conn
    _ensure_cna(c)
    _ins_sale(c, 'IV-A1', 'ACME', 'C01', '2026-01-10', 1000)
    _ins_sr(c, 'SR-A1', 'IV-A1', 'ACME', 'C01', '2026-01-20', 350)  # net 350
    _ins_cna(c, 'SR-A1', 'IV-A1', 300)  # authoritative 300
    c.commit()

    iv = _by_doc(pa.invoice_settlement(conn=c))['IV-A1']
    assert iv['credit_notes'] == 300.0  # NOT 350
    assert iv['net_owed'] == 700.0
    assert iv['outstanding'] == 700.0
    assert iv['status'] == 'unpaid'


def test_cn_fallback_to_sr_net_when_absent_from_cna(empty_db_conn):
    """SR doc NOT in credit_note_amounts → fall back to the old SR.net sum
    (documented behaviour: legacy / not-yet-imported SR still nets)."""
    c = empty_db_conn
    _ensure_cna(c)  # table exists but empty for this SR
    _ins_sale(c, 'IV-B1', 'ACME', 'C01', '2026-01-10', 1000)
    _ins_sr(c, 'SR-B1', 'IV-B1', 'ACME', 'C01', '2026-01-20', 250)
    c.commit()

    iv = _by_doc(pa.invoice_settlement(conn=c))['IV-B1']
    assert iv['credit_notes'] == 250.0  # fallback SR.net
    assert iv['net_owed'] == 750.0


def test_cash_only_invoice_unaffected_by_cn_changes(empty_db_conn):
    """No SR anywhere: a plain paid invoice is unchanged (regression guard)."""
    c = empty_db_conn
    _ensure_cna(c)
    _ins_sale(c, 'IV-C1', 'ACME', 'C01', '2026-01-10', 1000)
    r = _ins_receipt(c, 'RE-C1', 'ACME', '2026-02-01', total=1000)
    _ins_paid(c, r, 'IV-C1', 1000)
    c.commit()

    iv = _by_doc(pa.invoice_settlement(conn=c))['IV-C1']
    assert iv['billed'] == 1000.0
    assert iv['credit_notes'] == 0.0
    assert iv['collected'] == 1000.0
    assert iv['outstanding'] == 0.0
    assert iv['status'] == 'paid'


def test_partial_with_cn_and_sr_receipt_link(empty_db_conn):
    """IV 1000, CN 200 (authoritative) applied via SR(-) receipt link, plus a
    partial +IV payment of 500. net_owed 800, collected = 500 - 200 = 300,
    outstanding 500, partial."""
    c = empty_db_conn
    _ensure_cna(c)
    _ins_sale(c, 'IV-D1', 'ACME', 'C01', '2026-01-10', 1000)
    _ins_sr(c, 'SR-D1', 'IV-D1', 'ACME', 'C01', '2026-01-20', 200)
    _ins_cna(c, 'SR-D1', 'IV-D1', 200)
    r = _ins_receipt(c, 'RE-D1', 'ACME', '2026-02-01', total=500)
    _ins_paid(c, r, 'IV-D1', 500)
    _ins_paid(c, r, 'SR-D1', -200)
    c.commit()

    iv = _by_doc(pa.invoice_settlement(conn=c))['IV-D1']
    assert iv['billed'] == 1000.0
    assert iv['credit_notes'] == 200.0
    assert iv['net_owed'] == 800.0
    assert iv['collected'] == 300.0
    assert iv['outstanding'] == 500.0
    assert iv['status'] == 'partial'


def test_cn_in_file_but_no_receipt_link(empty_db_conn):
    """Credit note exists in credit_note_amounts and reduces net_owed, but the
    customer never paid (no receipt at all). collected 0, outstanding =
    net_owed."""
    c = empty_db_conn
    _ensure_cna(c)
    _ins_sale(c, 'IV-E1', 'ACME', 'C01', '2026-01-10', 1000)
    _ins_sr(c, 'SR-E1', 'IV-E1', 'ACME', 'C01', '2026-01-20', 300)
    _ins_cna(c, 'SR-E1', 'IV-E1', 300)
    c.commit()

    iv = _by_doc(pa.invoice_settlement(conn=c))['IV-E1']
    assert iv['credit_notes'] == 300.0
    assert iv['net_owed'] == 700.0
    assert iv['collected'] == 0.0
    assert iv['outstanding'] == 700.0
    assert iv['status'] == 'unpaid'


def test_sr_receipt_link_present_but_sr_not_in_cna_fallback(empty_db_conn):
    """SR(-) receipt link exists and an SR.net fallback (no credit_note_amounts
    row). cn falls back to SR.net=300; collected = 1000 - 300 = 700;
    net_owed = 700; outstanding 0; paid."""
    c = empty_db_conn
    _ensure_cna(c)
    _ins_sale(c, 'IV-F1', 'ACME', 'C01', '2026-01-10', 1000)
    _ins_sr(c, 'SR-F1', 'IV-F1', 'ACME', 'C01', '2026-01-20', 300)
    r = _ins_receipt(c, 'RE-F1', 'ACME', '2026-02-01', total=1000)
    _ins_paid(c, r, 'IV-F1', 1000)
    _ins_paid(c, r, 'SR-F1', -300)
    c.commit()

    iv = _by_doc(pa.invoice_settlement(conn=c))['IV-F1']
    assert iv['credit_notes'] == 300.0
    assert iv['net_owed'] == 700.0
    assert iv['collected'] == 700.0
    assert iv['outstanding'] == 0.0
    assert iv['status'] == 'paid'


def test_cash_in_rows_nets_sr_receipt_links(empty_db_conn):
    """cash_in_rows must be symmetric with collected: Σ cash_in == Σ collected.
    The SR(-) link reduces the receipt's attributed cash."""
    c = empty_db_conn
    _ensure_cna(c)
    _ins_sale(c, 'IV-G1', 'ACME', 'C01', '2026-01-10', 5242.02, line=1)
    _ins_sr(c, 'SR-G1', 'IV-G1', 'ACME', 'C01', '2026-03-27', 2340.0)
    _ins_cna(c, 'SR-G1', 'IV-G1', 2293.20)
    r = _ins_receipt(c, 'RE-G1', 'ACME', '2026-03-27', total=5242.02)
    _ins_paid(c, r, 'IV-G1', 5242.02)
    _ins_paid(c, r, 'SR-G1', -2293.20)
    c.commit()

    settle = pa.invoice_settlement(conn=c)
    total_collected = round(sum(x['collected'] for x in settle), 2)
    cash = pa.cash_in_rows(conn=c)
    total_cash = round(sum(x['amount'] for x in cash), 2)
    assert total_collected == pytest.approx(2948.82)
    assert total_cash == pytest.approx(total_collected), (
        f"cash_in {total_cash} != collected {total_collected}"
    )


# ── VAT-aware billed (vat_type=2 = แยก VAT → customer pays net*1.07) ──────────
#
# Convention shared with models.py / test_vat_math.py:
#   billed per line = CASE WHEN vat_type=2 THEN net*1.07 ELSE net END
# `net` is pre-VAT post-doc-discount; the customer of a "แยก VAT" invoice
# actually remits net + 7% output VAT. payments_alloc previously summed bare
# `net`, so every paid vat_type=2 invoice read as overpaid by ~7% (the
# spurious ฿105k / ฿442k customer-credit balance). Revenue stays ex-VAT
# (cashflow.revenue_by_month) — only what the customer OWES/PAYS is grossed.

def test_vat2_invoice_billed_is_grossed_and_settles_at_107(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV-V2', 'ACME', 'C01', '2026-01-10', 1000.0, vat_type=2)
    r = _ins_receipt(c, 'RE-V2', 'ACME', '2026-02-01', total=1070.0)
    _ins_paid(c, r, 'IV-V2', 1070.0)
    c.commit()
    iv = _by_doc(pa.invoice_settlement(conn=c))['IV-V2']
    assert iv['billed'] == 1070.0
    assert iv['collected'] == 1070.0
    assert iv['outstanding'] == 0.0
    assert iv['status'] == 'paid'          # NOT 'overpaid' (the old bug)


def test_vat2_paid_at_net_is_underpaid_not_settled(empty_db_conn):
    """Paying only the pre-VAT net on a แยก-VAT bill leaves 7% owed."""
    c = empty_db_conn
    _ins_sale(c, 'IV-V2B', 'ACME', 'C01', '2026-01-10', 1000.0, vat_type=2)
    r = _ins_receipt(c, 'RE-V2B', 'ACME', '2026-02-01', total=1000.0)
    _ins_paid(c, r, 'IV-V2B', 1000.0)
    c.commit()
    iv = _by_doc(pa.invoice_settlement(conn=c))['IV-V2B']
    assert iv['billed'] == 1070.0
    assert iv['outstanding'] == 70.0
    assert iv['status'] == 'partial'


def test_vat1_and_vat0_unaffected_by_gross(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV-V1', 'ACME', 'C01', '2026-01-10', 1000.0, vat_type=1)
    _ins_sale(c, 'IV-V0', 'ACME', 'C01', '2026-01-10', 1000.0, vat_type=0)
    for d in ('IV-V1', 'IV-V0'):
        r = _ins_receipt(c, f'RE-{d}', 'ACME', '2026-02-01', total=1000.0)
        _ins_paid(c, r, d, 1000.0)
    c.commit()
    rows = _by_doc(pa.invoice_settlement(conn=c))
    assert rows['IV-V1']['billed'] == 1000.0
    assert rows['IV-V0']['billed'] == 1000.0
    assert rows['IV-V1']['status'] == 'paid'
    assert rows['IV-V0']['status'] == 'paid'


def test_vat_mixed_lines_billed_line_by_line(empty_db_conn):
    """A doc with both vat_type=1 and =2 lines grosses only the =2 line."""
    c = empty_db_conn
    _ins_sale(c, 'IV-VM', 'ACME', 'C01', '2026-01-10', 500.0, line=1,
              vat_type=1)
    _ins_sale(c, 'IV-VM', 'ACME', 'C01', '2026-01-10', 500.0, line=2,
              vat_type=2)
    c.commit()
    iv = _by_doc(pa.invoice_settlement(conn=c))['IV-VM']
    assert iv['billed'] == 1035.0          # 500 + 500*1.07


def test_vat2_pure_legacy_cash_in_is_grossed_and_reconciles(empty_db_conn):
    """Pure-legacy (NULL amount) vat_type=2 invoice: collected == net_owed ==
    grossed billed, and Σ cash_in == Σ collected (cash_in legacy path must
    gross identically or the reconciliation identity breaks)."""
    c = empty_db_conn
    _ins_sale(c, 'IV-VL', 'ACME', 'C01', '2026-01-10', 1000.0, vat_type=2)
    r = _ins_receipt(c, 'RE-VL', 'ACME', '2026-02-01', total=None)
    _ins_paid(c, r, 'IV-VL', None)         # legacy NULL-amount link
    c.commit()
    iv = _by_doc(pa.invoice_settlement(conn=c))['IV-VL']
    assert iv['billed'] == 1070.0
    assert iv['collected'] == 1070.0
    assert iv['status'] == 'paid'
    total_collected = round(
        sum(x['collected'] for x in pa.invoice_settlement(conn=c)), 2)
    total_cash = round(sum(x['amount'] for x in pa.cash_in_rows(conn=c)), 2)
    assert total_cash == pytest.approx(total_collected)
