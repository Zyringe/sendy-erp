"""TDD tests for inventory_app/cashflow.py — Cash Flow dashboard logic.

Synthetic data only (built on empty_db_conn schema clone). All helpers mirror
the style used in test_payments_alloc.py.

Key semantic rules under test
──────────────────────────────
cash_in_by_month:
  - Groups cash by received_payments.date_iso (RE date), NOT sale date.
  - Cancelled receipts are excluded.
  - Legacy NULL-amount link (paid_invoices.amount IS NULL, non-cancelled)
    counts as the invoice's full billed amount (Σ net of its doc_base).
  - Must reconcile with payments_alloc.invoice_settlement Σ collected.

ar_aging (Express-sourced, 2026-05-30):
  - Sources from express_ar_outstanding WHERE entity='BSN' at latest snapshot.
  - Buckets each doc's outstanding_amount by (snapshot_date − doc_date_iso).
  - Negatives from credit notes net correctly per bucket.
  - ar_aging totals CANONICAL collectable AR (excl RE + pre-2024 legacy) =
    ฿732,357.86 / 145 docs (Put 2026-06-04). The raw snapshot is ฿1,299,335.94 /
    200 docs (see test_bsn_snapshot_totals in test_ar_followup).
  - จึงเจริญ (all RE) is EXCLUDED from the collectable total and tracked in the
    not-collectable list (cashflow.bsn_ar_excluded_by_customer).

revenue_by_month:
  - Groups by sale date (sales_transactions.date_iso), NOT RE date.
  - Differs from cash_in_by_month when payment is in a later month.
"""
import pytest
from datetime import date, timedelta

import cashflow as cf
import payments_alloc as pa


# ── synthetic data helpers (mirror test_payments_alloc.py style) ─────────────

def _ins_sale(conn, doc_base, customer, customer_code, date_iso, net,
              line=1, vat_type=1):
    """One sales_transactions line."""
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
    """Sales-return (credit-note) line, mirrors test_payments_alloc._ins_sr."""
    conn.execute(
        """INSERT INTO sales_transactions
           (date_iso, doc_no, doc_base, ref_invoice, customer, customer_code,
            qty, unit, unit_price, vat_type, total, net)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (date_iso, f"{sr_no}-{line}", sr_no, ref_invoice, customer,
         customer_code, 1, 'ตัว', net, vat_type, net, net),
    )


def _by_month(rows):
    return {r['month']: r for r in rows}


# ── cash_in_by_month: groups by RE date not sale date ────────────────────────

def test_cash_in_groups_by_re_date_not_sale_date(empty_db_conn):
    """A sale in Jan paid in Feb must appear in Feb's cash_in, not Jan."""
    c = empty_db_conn
    # Sale on 2026-01-10, cash receipt on 2026-02-05
    _ins_sale(c, 'IV001', 'ACME', 'C01', '2026-01-10', 1000)
    r = _ins_receipt(c, 'RE001', 'ACME', '2026-02-05', total=1000)
    _ins_paid(c, r, 'IV001', 1000)
    c.commit()

    rows = _by_month(cf.cash_in_by_month(conn=c))
    # Jan should have no cash_in (sale date only)
    assert '2026-01' not in rows or rows['2026-01']['cash_in'] == 0.0
    # Feb should have 1000 cash
    assert '2026-02' in rows
    assert rows['2026-02']['cash_in'] == 1000.0
    assert rows['2026-02']['receipts'] == 1


def test_cancelled_receipts_excluded_from_cash_in(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV002', 'ACME', 'C01', '2026-01-15', 500)
    r_good = _ins_receipt(c, 'RE002', 'ACME', '2026-02-01', total=500)
    _ins_paid(c, r_good, 'IV002', 500)
    r_bad = _ins_receipt(c, 'RE-CANCEL', 'ACME', '2026-02-01',
                          cancelled=1, total=9999)
    _ins_paid(c, r_bad, 'IV002', 9999)
    c.commit()

    rows = _by_month(cf.cash_in_by_month(conn=c))
    # Only the non-cancelled receipt (500) counts
    assert rows['2026-02']['cash_in'] == 500.0
    assert rows['2026-02']['receipts'] == 1


def test_legacy_null_amount_falls_back_to_billed(empty_db_conn):
    """Legacy NULL-amount link: cash_in should equal the invoice's billed amount."""
    c = empty_db_conn
    _ins_sale(c, 'IV003', 'ACME', 'C01', '2026-01-20', 1500)
    # pre-058 link: both receipt.total and paid_invoices.amount are NULL
    r = _ins_receipt(c, 'RE003', 'ACME', '2026-03-01', total=None)
    _ins_paid(c, r, 'IV003', None)
    c.commit()

    rows = _by_month(cf.cash_in_by_month(conn=c))
    # Cash must be recognized at the full billed amount (1500) in the RE month
    assert rows['2026-03']['cash_in'] == 1500.0
    assert rows['2026-03']['receipts'] == 1


def test_no_receipts_returns_empty_list(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV004', 'ACME', 'C01', '2026-01-10', 1000)
    c.commit()

    rows = cf.cash_in_by_month(conn=c)
    assert rows == []


def test_multiple_receipts_same_month_summed(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV005', 'ACME', 'C01', '2026-01-10', 400)
    _ins_sale(c, 'IV006', 'ACME', 'C01', '2026-01-15', 600)
    r1 = _ins_receipt(c, 'RE005', 'ACME', '2026-02-10', total=400)
    _ins_paid(c, r1, 'IV005', 400)
    r2 = _ins_receipt(c, 'RE006', 'ACME', '2026-02-20', total=600)
    _ins_paid(c, r2, 'IV006', 600)
    c.commit()

    rows = _by_month(cf.cash_in_by_month(conn=c))
    assert rows['2026-02']['cash_in'] == 1000.0
    assert rows['2026-02']['receipts'] == 2


def test_date_filter_respected(empty_db_conn):
    """date_from / date_to filter on RE date (not sale date)."""
    c = empty_db_conn
    _ins_sale(c, 'IV007', 'ACME', 'C01', '2025-11-01', 200)
    _ins_sale(c, 'IV008', 'ACME', 'C01', '2026-01-01', 300)
    r1 = _ins_receipt(c, 'RE007', 'ACME', '2025-12-01', total=200)
    _ins_paid(c, r1, 'IV007', 200)
    r2 = _ins_receipt(c, 'RE008', 'ACME', '2026-02-01', total=300)
    _ins_paid(c, r2, 'IV008', 300)
    c.commit()

    # Filter only 2026 receipts
    rows = _by_month(cf.cash_in_by_month(date_from='2026-01-01', date_to='2026-12-31', conn=c))
    assert '2025-12' not in rows
    assert rows['2026-02']['cash_in'] == 300.0


# ── ar_aging — Express-snapshot-based tests ───────────────────────────────────

def _ensure_express_batch(conn, snap_date):
    """Return a batch_id for snap_date, inserting an express_import_log row if needed."""
    row = conn.execute(
        "SELECT id FROM express_import_log"
        " WHERE file_type='ar_snapshot' AND snapshot_date_iso=? LIMIT 1",
        (snap_date,),
    ).fetchone()
    if row:
        return row['id']
    cur = conn.execute(
        """INSERT INTO express_import_log
           (file_type, snapshot_date_iso, record_count, line_count, status)
           VALUES ('ar_snapshot', ?, 0, 0, 'imported')""",
        (snap_date,),
    )
    return cur.lastrowid


def _ins_express_snap(conn, snap_date, customer_code, customer_name,
                      doc_no, doc_date_iso, bill, paid, outstanding,
                      entity='BSN'):
    """Insert one express_ar_outstanding row for ar_aging synthetic tests."""
    batch_id = _ensure_express_batch(conn, snap_date)
    conn.execute(
        """INSERT INTO express_ar_outstanding
           (batch_id, snapshot_date_iso, customer_code, customer_name, doc_no,
            doc_date_iso, bill_amount, paid_amount, outstanding_amount, entity)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (batch_id, snap_date, customer_code, customer_name, doc_no,
         doc_date_iso, bill, paid, outstanding, entity),
    )


def test_ar_aging_buckets(empty_db_conn):
    """Four docs aged 10, 45, 75, 200 days from snapshot land in right buckets."""
    c = empty_db_conn
    snap = '2026-05-18'

    def inv_date(days_ago):
        return (date(2026, 5, 18) - timedelta(days=days_ago)).isoformat()

    _ins_express_snap(c, snap, 'C10', 'SHOP', 'IV-A01', inv_date(10),  1000, 0, 1000)  # 0-30
    _ins_express_snap(c, snap, 'C10', 'SHOP', 'IV-A02', inv_date(45),  2000, 0, 2000)  # 31-60
    _ins_express_snap(c, snap, 'C10', 'SHOP', 'IV-A03', inv_date(75),  3000, 0, 3000)  # 61-90
    _ins_express_snap(c, snap, 'C10', 'SHOP', 'IV-A04', inv_date(200), 4000, 0, 4000)  # 90+
    c.commit()

    result = cf.ar_aging(conn=c)
    assert result['as_of'] == snap
    by_label = {b['label']: b for b in result['buckets']}

    assert by_label['0-30']['amount']  == pytest.approx(1000.0)
    assert by_label['0-30']['count']   == 1
    assert by_label['31-60']['amount'] == pytest.approx(2000.0)
    assert by_label['31-60']['count']  == 1
    assert by_label['61-90']['amount'] == pytest.approx(3000.0)
    assert by_label['61-90']['count']  == 1
    assert by_label['90+']['amount']   == pytest.approx(4000.0)
    assert by_label['90+']['count']    == 1


def test_ar_aging_negative_row_nets_in_bucket(empty_db_conn):
    """Negative outstanding (credit note) should reduce its bucket amount."""
    c = empty_db_conn
    snap = '2026-05-18'
    # Positive invoice: 1000 outstanding, 200 days old → 90+
    _ins_express_snap(c, snap, 'C10', 'SHOP', 'IV-B01', '2025-11-01', 1000, 0, 1000)
    # Negative CN: -300 outstanding, 200 days old → 90+
    _ins_express_snap(c, snap, 'C10', 'SHOP', 'SR-B01', '2025-11-01', 0, 0, -300)
    c.commit()

    result = cf.ar_aging(conn=c)
    by_label = {b['label']: b for b in result['buckets']}
    # Net in 90+ = 1000 - 300 = 700
    assert by_label['90+']['amount'] == pytest.approx(700.0)
    assert by_label['90+']['count']  == 2
    assert result['total_outstanding'] == pytest.approx(700.0)


def test_ar_aging_reconcile_identity(empty_db_conn):
    """total_billed - total_collected == total_outstanding (accounting identity)."""
    c = empty_db_conn
    snap = '2026-05-18'
    _ins_express_snap(c, snap, 'C10', 'SHOP', 'IV-C01', '2026-03-01', 1000, 1000, 0)
    _ins_express_snap(c, snap, 'C10', 'SHOP', 'IV-C02', '2026-04-01', 800,  0,    800)
    c.commit()

    result = cf.ar_aging(conn=c)
    assert result['total_billed'] == pytest.approx(
        result['total_collected'] + result['total_outstanding']
    )
    assert result['total_outstanding'] == pytest.approx(800.0)
    assert result['total_credit_notes'] == pytest.approx(0.0)


def test_ar_aging_no_snapshot_returns_zeros(empty_db_conn):
    """No Express snapshot rows → graceful zero return, valid structure."""
    c = empty_db_conn
    # No rows inserted — table is empty
    result = cf.ar_aging(conn=c)
    assert 'as_of' in result
    assert 'buckets' in result
    assert len(result['buckets']) == 4
    labels = [b['label'] for b in result['buckets']]
    assert labels == ['0-30', '31-60', '61-90', '90+']
    assert result['total_outstanding'] == pytest.approx(0.0)


def test_ar_aging_non_bsn_entity_excluded(empty_db_conn):
    """Only BSN rows are included; SD or other-entity rows are ignored."""
    c = empty_db_conn
    snap = '2026-05-18'
    _ins_express_snap(c, snap, 'C10', 'SHOP', 'IV-D01', '2026-04-01',
                      500, 0, 500, entity='BSN')
    _ins_express_snap(c, snap, 'C10', 'SHOP', 'IV-D02', '2026-04-01',
                      9000, 0, 9000, entity='SD')
    c.commit()

    result = cf.ar_aging(conn=c)
    # Only the BSN row (500) should appear
    assert result['total_outstanding'] == pytest.approx(500.0)


# ── live-DB ar_aging tests (against real Express snapshot) ───────────────────

def test_ar_aging_live_total_is_canonical_collectable(tmp_db_conn):
    """ar_aging now totals the CANONICAL collectable BSN AR — the latest Express
    snapshot EXCLUDING RE/anomalous receipts and pre-2024 legacy debt
    (Put 2026-06-04) = ฿732,357.86 across 145 docs."""
    result = cf.ar_aging(conn=tmp_db_conn)
    assert result['total_outstanding'] == pytest.approx(732_357.86, abs=0.01), (
        f"Expected canonical ฿732,357.86; got {result['total_outstanding']:.2f}"
    )
    total_in_buckets = round(sum(b['amount'] for b in result['buckets']), 2)
    assert total_in_buckets == pytest.approx(732_357.86, abs=0.01)
    total_count = sum(b['count'] for b in result['buckets'])
    assert total_count == 145, f"Expected 145 docs; got {total_count}"


def test_re_jungjaroen_excluded_from_collectable_but_shown_separately(tmp_db_conn):
    """จึงเจริญ is entirely RE (2005-2017, ฿349,596.08 net) → EXCLUDED from the
    collectable aging total (locked by the test above), but must still surface
    in the not-collectable (RE / pre-2024) list so it stays trackable for the
    write-off process."""
    exc = cf.bsn_ar_excluded_by_customer(conn=tmp_db_conn)
    jung = [r for r in exc if 'จึงเจริญ' in (r['customer_name'] or '')]
    assert jung, "จึงเจริญ must appear in the not-collectable (RE) list"
    assert jung[0]['has_re'] == 1
    assert jung[0]['outstanding'] == pytest.approx(349_596.08, abs=0.01)


# ── revenue_by_month ──────────────────────────────────────────────────────────

def test_revenue_groups_by_sale_date(empty_db_conn):
    """Revenue appears in the month of the sale, not the payment month."""
    c = empty_db_conn
    _ins_sale(c, 'IVF001', 'ACME', 'C01', '2026-01-10', 1000)
    r = _ins_receipt(c, 'REF001', 'ACME', '2026-03-01', total=1000)
    _ins_paid(c, r, 'IVF001', 1000)
    c.commit()

    rows = {r['month']: r for r in cf.revenue_by_month(conn=c)}
    # Revenue in Jan (sale date), not Mar (cash date)
    assert '2026-01' in rows
    assert rows['2026-01']['revenue'] == pytest.approx(1000.0)
    # The cash month should NOT have revenue from this invoice
    assert '2026-03' not in rows or rows['2026-03']['revenue'] == pytest.approx(0.0)


def test_revenue_differs_from_cash_in_across_months(empty_db_conn):
    """
    Invoice sold Jan, paid Mar:
      revenue Jan = 1000,  cash_in Jan = 0
      cash_in Mar = 1000,  revenue Mar = 0 (or only from other invoices)
    This proves accrual vs cash split works.
    """
    c = empty_db_conn
    _ins_sale(c, 'IVG001', 'ACME', 'C01', '2026-01-15', 1000)
    r = _ins_receipt(c, 'REG001', 'ACME', '2026-03-20', total=1000)
    _ins_paid(c, r, 'IVG001', 1000)
    c.commit()

    rev_by_m  = {r['month']: r for r in cf.revenue_by_month(conn=c)}
    cash_by_m = {r['month']: r for r in cf.cash_in_by_month(conn=c)}

    # Accrual: revenue shows in Jan
    assert rev_by_m['2026-01']['revenue'] == pytest.approx(1000.0)
    # Cash: payment shows in Mar
    assert cash_by_m['2026-03']['cash_in'] == pytest.approx(1000.0)
    # No cash in Jan
    assert '2026-01' not in cash_by_m or cash_by_m['2026-01']['cash_in'] == 0.0
    # No revenue in Mar from this invoice
    assert '2026-03' not in rev_by_m or rev_by_m['2026-03']['revenue'] == 0.0


def test_revenue_by_month_date_filter(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IVH001', 'ACME', 'C01', '2025-12-01', 500)
    _ins_sale(c, 'IVH002', 'ACME', 'C01', '2026-02-01', 700)
    c.commit()

    rows = {r['month']: r for r in
            cf.revenue_by_month(date_from='2026-01-01', date_to='2026-12-31', conn=c)}
    assert '2025-12' not in rows
    assert rows['2026-02']['revenue'] == pytest.approx(700.0)


# ── reconciliation: cash_in_by_month must tie to payments_alloc.invoice_settlement ──

def test_cash_in_reconciles_with_payments_alloc(empty_db_conn):
    """
    Σ cash_in_by_month == Σ invoice_settlement.collected within ฿0.01.

    Both use the same legacy-NULL rule, so they must produce the same total
    no matter how many legacy vs real-amount rows are in the data.
    """
    c = empty_db_conn

    # Mix of real-amount links, NULL legacy links, partial payments,
    # cancelled receipts, multi-line invoices.
    _ins_sale(c, 'IVR001', 'ACME', 'C01', '2026-01-10', 800, line=1)
    _ins_sale(c, 'IVR001', 'ACME', 'C01', '2026-01-10', 200, line=2)  # total billed 1000
    r1 = _ins_receipt(c, 'RER001', 'ACME', '2026-02-01', total=1000)
    _ins_paid(c, r1, 'IVR001', 1000)  # real amount

    _ins_sale(c, 'IVR002', 'SHOP', 'C02', '2026-02-01', 1500)
    r2 = _ins_receipt(c, 'RER002', 'SHOP', '2026-03-01', total=None)  # legacy
    _ins_paid(c, r2, 'IVR002', None)   # legacy → counts as 1500

    _ins_sale(c, 'IVR003', 'SHOP', 'C02', '2026-03-01', 2000)
    r3 = _ins_receipt(c, 'RER003', 'SHOP', '2026-04-01', total=800)
    _ins_paid(c, r3, 'IVR003', 800)   # partial

    _ins_sale(c, 'IVR004', 'SHOP', 'C02', '2026-03-05', 600)
    r4_bad = _ins_receipt(c, 'RER004-CANCEL', 'SHOP', '2026-04-01',
                           cancelled=1, total=600)
    _ins_paid(c, r4_bad, 'IVR004', 600)  # cancelled — must be ignored

    _ins_sale(c, 'IVR005', 'ACME', 'C01', '2026-04-01', 300)  # unpaid

    c.commit()

    total_cash_in = sum(r['cash_in'] for r in cf.cash_in_by_month(conn=c))
    total_alloc   = sum(r['collected']
                        for r in pa.invoice_settlement(conn=c))

    assert abs(total_cash_in - total_alloc) < 0.01, (
        f"cash_in={total_cash_in:.2f} != alloc_collected={total_alloc:.2f}"
    )
    # Sanity: 1000 (real) + 1500 (legacy) + 800 (partial) = 3300
    assert total_cash_in == pytest.approx(3300.0)


def test_combined_dataset_full_reconciliation(empty_db_conn):
    """Combined pure-legacy + mixed(real+NULL) + all-real dataset.

    Asserts (credit-note-aware identity):
      - Σ cash_in_by_month == Σ invoice_settlement collected (฿0.01)
      - Σ billed - Σ credit_notes - Σ collected == Σ outstanding (to the
        cent; outstanding includes genuine negatives from overpaid)
      - Σ overpaid_excess == Σ max(0, collected - net_owed)
      - this fixture has no SR rows so Σ credit_notes == 0 and the identity
        collapses back to the legacy billed==collected+outstanding form
    """
    c = empty_db_conn

    # 1) Pure-legacy: single NULL link → collected = billed (700)
    _ins_sale(c, 'CMB-LEG', 'ACME', 'C01', '2026-01-05', 700)
    r_leg = _ins_receipt(c, 'RE-CMB-LEG', 'ACME', '2026-02-05', total=None)
    _ins_paid(c, r_leg, 'CMB-LEG', None)

    # 2) Mixed real+NULL on same invoice: real 400 wins → collected 400
    _ins_sale(c, 'CMB-MIX', 'ACME', 'C01', '2026-01-10', 1000)
    r_mix_a = _ins_receipt(c, 'RE-CMB-MIX-A', 'ACME', '2026-02-01',
                           total=400)
    _ins_paid(c, r_mix_a, 'CMB-MIX', 400)
    r_mix_b = _ins_receipt(c, 'RE-CMB-MIX-B', 'ACME', '2026-02-15',
                           total=None)
    _ins_paid(c, r_mix_b, 'CMB-MIX', None)

    # 3) All-real multi-receipt: 300 + 700 → collected 1000 (paid)
    _ins_sale(c, 'CMB-AR', 'SHOP', 'C02', '2026-01-20', 1000)
    r_ar_1 = _ins_receipt(c, 'RE-CMB-AR-1', 'SHOP', '2026-02-10',
                          total=300)
    _ins_paid(c, r_ar_1, 'CMB-AR', 300)
    r_ar_2 = _ins_receipt(c, 'RE-CMB-AR-2', 'SHOP', '2026-03-10',
                          total=700)
    _ins_paid(c, r_ar_2, 'CMB-AR', 700)

    # 4) All-real overpaid: 1200 vs 1000 → outstanding -200
    _ins_sale(c, 'CMB-OP', 'SHOP', 'C02', '2026-01-25', 1000)
    r_op = _ins_receipt(c, 'RE-CMB-OP', 'SHOP', '2026-02-20', total=1200)
    _ins_paid(c, r_op, 'CMB-OP', 1200)

    c.commit()

    settle = pa.invoice_settlement(conn=c)
    total_billed       = round(sum(r['billed']       for r in settle), 2)
    total_credit_notes = round(sum(r['credit_notes'] for r in settle), 2)
    total_collected    = round(sum(r['collected']    for r in settle), 2)
    total_outstanding  = round(sum(r['outstanding']  for r in settle), 2)
    total_overpaid     = round(sum(max(0.0, r['collected'] - r['net_owed'])
                                   for r in settle), 2)

    total_cash_in = round(
        sum(r['cash_in'] for r in cf.cash_in_by_month(conn=c)), 2)

    # cash_in ties to collected (credit notes are NOT cash)
    assert abs(total_cash_in - total_collected) < 0.01, (
        f"cash_in={total_cash_in:.2f} "
        f"!= collected={total_collected:.2f}")

    # credit-note-aware accounting identity to the cent
    # (outstanding includes negatives from overpaid)
    assert abs(total_billed - total_credit_notes
               - total_collected - total_outstanding) < 0.01

    # this fixture has no SR rows
    assert total_credit_notes == pytest.approx(0.0)
    # with cn==0 the identity collapses to the legacy form
    assert abs(total_billed
               - (total_collected + total_outstanding)) < 0.01

    # expected: billed 700+1000+1000+1000 = 3700
    assert total_billed == pytest.approx(3700.0)
    # collected 700(leg) + 400(mix) + 1000(ar) + 1200(op) = 3300
    assert total_collected == pytest.approx(3300.0)
    # outstanding 0 + 600 + 0 + (-200) = 400
    assert total_outstanding == pytest.approx(400.0)
    # overpaid excess only from CMB-OP = 200
    assert total_overpaid == pytest.approx(200.0)


def test_credit_note_aware_identity_and_cash_unaffected_by_sr(empty_db_conn):
    """Combined identity with real SR rows:
      Σ billed - Σ credit_notes - Σ collected == Σ outstanding
    and cash_in is UNAFFECTED by credit notes (a credit note is not cash).
    """
    c = empty_db_conn

    # IV-A: 1000 billed, SR 300, payment 700 → fully paid
    _ins_sale(c, 'CID-A', 'ACME', 'C01', '2026-01-05', 1000)
    _ins_sr(c, 'SR-CID-A', 'CID-A', 'ACME', 'C01', '2026-01-15', 300)
    r_a = _ins_receipt(c, 'RE-CID-A', 'ACME', '2026-02-01', total=700)
    _ins_paid(c, r_a, 'CID-A', 700)

    # IV-B: 800 billed, SR 800 → fully credited, no cash
    _ins_sale(c, 'CID-B', 'SHOP', 'C02', '2026-01-10', 800)
    _ins_sr(c, 'SR-CID-B', 'CID-B', 'SHOP', 'C02', '2026-01-20', 800)

    # IV-C: 500 billed, no SR, no payment → outstanding 500
    _ins_sale(c, 'CID-C', 'SHOP', 'C02', '2026-01-25', 500)

    # SR with NULL ref — unattributable, must not net anywhere
    _ins_sr(c, 'SR-CID-X', None, 'SHOP', 'C02', '2026-01-28', 999)

    c.commit()

    settle = pa.invoice_settlement(conn=c)
    total_billed       = round(sum(r['billed']       for r in settle), 2)
    total_credit_notes = round(sum(r['credit_notes'] for r in settle), 2)
    total_collected    = round(sum(r['collected']    for r in settle), 2)
    total_outstanding  = round(sum(r['outstanding']  for r in settle), 2)

    total_cash_in = round(
        sum(r['cash_in'] for r in cf.cash_in_by_month(conn=c)), 2)

    # billed only from real IVs (SR excluded from inv CTE)
    assert total_billed == pytest.approx(2300.0)        # 1000+800+500
    assert total_credit_notes == pytest.approx(1100.0)  # 300+800 (NULL ref dropped)
    assert total_collected == pytest.approx(700.0)
    # outstanding: A=0, B=0, C=500 → 500
    assert total_outstanding == pytest.approx(500.0)

    # credit-note-aware identity holds exactly
    assert abs(total_billed - total_credit_notes
               - total_collected - total_outstanding) < 0.01

    # cash_in is unaffected by SR — only the real 700 receipt is cash
    assert total_cash_in == pytest.approx(700.0)
    assert abs(total_cash_in - total_collected) < 0.01

    # ar_aging now sources from Express snapshot, not invoice_settlement.
    # With no Express rows in this synthetic fixture, ar_aging returns zeros —
    # that is correct behaviour (Express is the authoritative source).
    # The credit-note accounting identity is covered by the Express-snapshot
    # tests above (test_ar_aging_negative_row_nets_in_bucket,
    # test_ar_aging_reconcile_identity).
    ag = cf.ar_aging(conn=c)
    assert ag['total_outstanding'] == pytest.approx(0.0)
    assert ag['total_billed'] == pytest.approx(0.0)
    assert ag['total_collected'] == pytest.approx(0.0)
