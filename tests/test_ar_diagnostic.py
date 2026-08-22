"""AR reconciliation diagnostic — Express snapshot vs Sendy's derived balance.

READ-ONLY and NOT authoritative. Express `ARTRN.REMAMT` stays the source of
truth for AR (Put, 2026-08-22, option A); this compares Sendy's amount-aware
derived balance (`payments_alloc.invoice_settlement`) against the snapshot and
CLASSIFIES every difference, so the ledger's blind spots are visible instead of
inferred.

Design rules this file pins:
  - Every compared document lands in EXACTLY ONE bucket, and the buckets'
    snapshot amounts sum to the snapshot total. A doc that is both an RE
    anomaly and pre-era is counted once, not twice — the same disjointness
    `cashflow.bsn_ar_excluded` already guarantees for the AR pages.
  - Exact satang equality decides agreement. There is NO tolerance band and NO
    catch-all "legacy" bucket: a difference nothing explains is reported as
    unexplained, per document.
  - An empty snapshot RAISES. Returning an all-clear for a comparison that
    never happened is the vacuity trap this whole diagnostic exists to avoid.
"""
import pytest

from ar_diagnostic import build_ar_reconciliation


ERA = '2024-01-01'


def _snap(doc_no, outstanding, *, date='2026-05-01', anomalous=0, customer='ลูกค้า ก'):
    return {'doc_no': doc_no, 'doc_date_iso': date, 'is_anomalous': anomalous,
            'outstanding_amount': outstanding, 'customer_name': customer}


def _der(doc_base, outstanding, *, date='2026-05-01', customer='ลูกค้า ก'):
    return {'doc_base': doc_base, 'outstanding': outstanding,
            'invoice_date': date, 'customer': customer}


def _bucket(rep, reason):
    for b in rep['buckets']:
        if b['reason'] == reason:
            return b
    return None


def _reasons(rep):
    return {b['reason']: b['docs'] for b in rep['buckets'] if b['docs']}


# ── agreement ────────────────────────────────────────────────────────────────

def test_equal_to_the_satang_agrees_and_leaves_nothing_unexplained():
    rep = build_ar_reconciliation([_snap('IV1', 1234.56)],
                                  [_der('IV1', 1234.56)], era_start=ERA)

    assert _reasons(rep) == {'agrees': 1}
    assert rep['unexplained'] == []


def test_one_satang_apart_is_unexplained_not_swallowed():
    """No tolerance band. A satang is a real disagreement or the reconciliation
    is decorative."""
    rep = build_ar_reconciliation([_snap('IV1', 1234.56)],
                                  [_der('IV1', 1234.55)], era_start=ERA)

    assert _reasons(rep) == {'unexplained': 1}
    assert rep['unexplained'][0]['difference'] == 0.01


def test_sendy_thinking_a_still_open_invoice_is_settled_is_unexplained():
    """Derived 0 against an open snapshot row is the dangerous direction —
    Sendy would stop chasing money Express still says is owed."""
    rep = build_ar_reconciliation([_snap('IV1', 5000.0)],
                                  [_der('IV1', 0.0)], era_start=ERA)

    assert _reasons(rep) == {'unexplained': 1}
    assert rep['unexplained'][0]['derived'] == 0.0


# ── explained gaps ───────────────────────────────────────────────────────────

def test_snapshot_doc_with_no_ledger_lines_at_all():
    rep = build_ar_reconciliation([_snap('IV_GHOST', 900.0)], [], era_start=ERA)

    assert _reasons(rep) == {'no_ledger_lines': 1}
    assert _bucket(rep, 'no_ledger_lines')['snapshot_amount'] == 900.0


def test_derived_only_outstanding_is_an_unallocated_receipt():
    """Express shows the document settled (absent from the open snapshot) while
    Sendy still carries a balance — a payment link Sendy never got."""
    rep = build_ar_reconciliation([_snap('IV1', 100.0)],
                                  [_der('IV1', 100.0), _der('IV2', 750.0)],
                                  era_start=ERA)

    assert _reasons(rep) == {'agrees': 1, 'unallocated': 1}
    assert _bucket(rep, 'unallocated')['derived_amount'] == 750.0


def test_derived_zero_outside_the_snapshot_is_not_reported_at_all():
    """Both sides say settled. Reporting it would bury the real findings."""
    rep = build_ar_reconciliation([_snap('IV1', 100.0)],
                                  [_der('IV1', 100.0), _der('IV_PAID', 0.0)],
                                  era_start=ERA)

    assert _reasons(rep) == {'agrees': 1}


def test_re_anomaly_is_its_own_bucket():
    rep = build_ar_reconciliation([_snap('RE1', 4000.0, anomalous=1)], [], era_start=ERA)

    assert _reasons(rep) == {'re_anomaly': 1}


def test_pre_era_debt_is_classified_not_counted_as_a_ledger_defect():
    rep = build_ar_reconciliation([_snap('IV_OLD', 2500.0, date='2019-06-30')],
                                  [], era_start=ERA)

    assert _reasons(rep) == {'pre_era': 1}


def test_written_off_doc_is_its_own_bucket():
    rep = build_ar_reconciliation([_snap('IV_WO', 1500.0)], [],
                                  writeoff_doc_nos={'IV_WO'}, era_start=ERA)

    assert _reasons(rep) == {'written_off': 1}


def test_sr_and_hs_docs_are_classified_because_derived_excludes_them():
    """`payments_alloc._settlement_rows` filters out `SR%` and `HS%`, so their
    absence from the derived side is by construction, not a finding."""
    rep = build_ar_reconciliation(
        [_snap('SR9', -300.0), _snap('HS9', 120.0)], [], era_start=ERA)

    assert _reasons(rep) == {'credit_note': 1, 'cash_sale': 1}


# ── exactly one bucket per document ──────────────────────────────────────────

def test_a_doc_matching_several_reasons_is_counted_once_in_the_first():
    """An RE that is ALSO pre-era and ALSO written off. Double-counting would
    make the buckets stop summing to the snapshot total."""
    rep = build_ar_reconciliation(
        [_snap('RE_OLD', 700.0, date='2019-01-01', anomalous=1)], [],
        writeoff_doc_nos={'RE_OLD'}, era_start=ERA)

    assert _reasons(rep) == {'re_anomaly': 1}


def test_buckets_sum_to_the_snapshot_total_exactly():
    """The load-bearing invariant: nothing is dropped and nothing is
    double-counted, so the reader can trust the classification adds up."""
    snap = [_snap('IV1', 1000.0), _snap('IV2', 250.25),
            _snap('RE1', 4000.0, anomalous=1),
            _snap('IV_OLD', 33.33, date='2019-06-30'),
            _snap('IV_WO', 1500.0), _snap('SR9', -300.0),
            _snap('IV_GHOST', 900.0)]
    der = [_der('IV1', 1000.0), _der('IV2', 250.00), _der('IV_LOOSE', 42.0)]
    rep = build_ar_reconciliation(snap, der, writeoff_doc_nos={'IV_WO'}, era_start=ERA)

    assert rep['snapshot_total'] == 7383.58
    assert round(sum(b['snapshot_amount'] for b in rep['buckets']), 2) == 7383.58
    assert sum(b['docs'] for b in rep['buckets']) == len(snap) + 1   # +IV_LOOSE


def test_every_snapshot_doc_appears_in_exactly_one_bucket():
    snap = [_snap('IV1', 10.0), _snap('RE1', 20.0, anomalous=1),
            _snap('IV_OLD', 30.0, date='2019-01-01')]
    rep = build_ar_reconciliation(snap, [_der('IV1', 10.0)], era_start=ERA)

    assert sum(b['docs'] for b in rep['buckets']) == 3


# ── refusals ─────────────────────────────────────────────────────────────────

def test_empty_snapshot_raises_rather_than_reporting_all_clear():
    with pytest.raises(ValueError):
        build_ar_reconciliation([], [_der('IV1', 100.0)], era_start=ERA)


def test_duplicate_open_doc_no_is_refused():
    """Two open rows for one document would post the balance twice — the same
    refusal `express_dbf_source._reject_duplicate` makes on the import side."""
    with pytest.raises(ValueError):
        build_ar_reconciliation([_snap('IV1', 100.0), _snap('IV1', 100.0)],
                                [], era_start=ERA)


# ── determinism ──────────────────────────────────────────────────────────────

def test_output_is_identical_regardless_of_input_order():
    snap = [_snap('IV1', 10.0), _snap('IV2', 20.0), _snap('IV3', 30.0)]
    der = [_der('IV1', 11.0), _der('IV3', 30.0)]

    assert (build_ar_reconciliation(snap, der, era_start=ERA)
            == build_ar_reconciliation(list(reversed(snap)), list(reversed(der)),
                                       era_start=ERA))


def test_unexplained_rows_are_ordered_by_size_of_difference():
    snap = [_snap('IV_SMALL', 100.0), _snap('IV_BIG', 9000.0)]
    der = [_der('IV_SMALL', 99.0), _der('IV_BIG', 1.0)]
    rep = build_ar_reconciliation(snap, der, era_start=ERA)

    assert [r['doc_no'] for r in rep['unexplained']] == ['IV_BIG', 'IV_SMALL']


# ── against the REAL derived engine ──────────────────────────────────────────

def _ins_sale(conn, doc_base, date_iso, net, vat_type=1, line=1,
              customer='ลูกค้า ก'):
    conn.execute(
        """INSERT INTO sales_transactions
           (date_iso, doc_no, doc_base, customer, customer_code,
            qty, unit, unit_price, vat_type, total, net)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (date_iso, f'{doc_base}-{line}', doc_base, customer, 'C01',
         1, 'ตัว', net, vat_type, net, net))


def _ins_receipt(conn, re_no, date_iso, cancelled=0, customer='ลูกค้า ก'):
    return conn.execute(
        """INSERT INTO received_payments (re_no, date_iso, customer, cancelled)
           VALUES (?,?,?,?)""", (re_no, date_iso, customer, cancelled)).lastrowid


def _ins_paid(conn, re_id, doc_no, amount, doc_kind='IV'):
    conn.execute(
        'INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount) VALUES (?,?,?,?)',
        (re_id, doc_no, doc_kind, amount))


def _derived(conn):
    import payments_alloc as pa
    return pa.invoice_settlement(conn=conn)


def test_partial_and_multiple_receipts_tie_to_the_snapshot(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV100', '2026-05-01', 1000.0)
    re1 = _ins_receipt(c, 'RE1', '2026-05-10')
    _ins_paid(c, re1, 'IV100', 400.0)
    re2 = _ins_receipt(c, 'RE2', '2026-05-20')
    _ins_paid(c, re2, 'IV100', 250.0)

    der = _derived(c)
    assert [(d['doc_base'], d['outstanding']) for d in der] == [('IV100', 350.0)]

    rep = build_ar_reconciliation([_snap('IV100', 350.0)], der, era_start=ERA)
    assert _reasons(rep) == {'agrees': 1}


def test_a_cancelled_receipt_does_not_settle_and_the_diagnostic_agrees(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV101', '2026-05-01', 800.0)
    dead = _ins_receipt(c, 'RE_DEAD', '2026-05-05', cancelled=1)
    _ins_paid(c, dead, 'IV101', 800.0)

    der = _derived(c)
    assert [(d['doc_base'], d['outstanding']) for d in der] == [('IV101', 800.0)]

    rep = build_ar_reconciliation([_snap('IV101', 800.0)], der, era_start=ERA)
    assert _reasons(rep) == {'agrees': 1}


def test_vat_exclusive_invoice_is_billed_gross_and_ties(empty_db_conn):
    """vat_type=2 lines are ex-VAT revenue; the customer owes net * 1.07."""
    c = empty_db_conn
    _ins_sale(c, 'IV102', '2026-05-01', 1000.0, vat_type=2)

    der = _derived(c)
    assert der[0]['billed'] == 1070.0

    rep = build_ar_reconciliation([_snap('IV102', 1070.0)], der, era_start=ERA)
    assert _reasons(rep) == {'agrees': 1}


def test_a_legacy_null_allocation_settles_the_whole_bill(empty_db_conn):
    """Pre-mig-058 links carry NULL amounts and mean "settled". Treating NULL
    as zero would invent an outstanding balance that Express does not show."""
    c = empty_db_conn
    _ins_sale(c, 'IV103', '2026-05-01', 640.0)
    re = _ins_receipt(c, 'RE_LEGACY', '2026-05-09')
    _ins_paid(c, re, 'IV103', None)

    der = _derived(c)
    assert [(d['doc_base'], d['outstanding'], d['status']) for d in der] \
        == [('IV103', 0.0, 'paid')]


def test_overpayment_surfaces_instead_of_clamping_to_zero(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV104', '2026-05-01', 500.0)
    re = _ins_receipt(c, 'RE_OVER', '2026-05-09')
    _ins_paid(c, re, 'IV104', 620.0)

    der = _derived(c)
    assert der[0]['outstanding'] == -120.0
    assert der[0]['status'] == 'overpaid'

    # Express shows the doc settled, so it is absent from the open snapshot —
    # a negative derived balance must still reach the report, not vanish.
    rep = build_ar_reconciliation([_snap('IV_OTHER', 1.0)], der, era_start=ERA)
    assert _reasons(rep) == {'no_ledger_lines': 1, 'unallocated': 1}
    assert _bucket(rep, 'unallocated')['derived_amount'] == -120.0


# ── point-in-time: the snapshot is AS-OF its date, not today ─────────────────

def test_invoice_settlement_default_is_unchanged_by_the_as_of_parameter(empty_db_conn):
    """The pages call it with no as_of and must keep today's behaviour."""
    import payments_alloc as pa
    c = empty_db_conn
    _ins_sale(c, 'IV200', '2026-05-01', 1000.0)
    re = _ins_receipt(c, 'RE_LATE', '2026-07-20')
    _ins_paid(c, re, 'IV200', 1000.0)

    assert pa.invoice_settlement(conn=c) == pa.invoice_settlement(conn=c, as_of=None)
    assert pa.invoice_settlement(conn=c)[0]['outstanding'] == 0.0


def test_as_of_ignores_a_receipt_that_had_not_happened_yet(empty_db_conn):
    """Comparing a 2026-06-05 snapshot against payments up to today reports
    every later-settled invoice as a phantom disagreement. This is the guard
    that keeps the diagnostic honest."""
    import payments_alloc as pa
    c = empty_db_conn
    _ins_sale(c, 'IV200', '2026-05-01', 1000.0)
    re = _ins_receipt(c, 'RE_LATE', '2026-07-20')
    _ins_paid(c, re, 'IV200', 1000.0)

    rows = pa.invoice_settlement(conn=c, as_of='2026-06-05')
    assert [(r['doc_base'], r['outstanding']) for r in rows] == [('IV200', 1000.0)]


def test_as_of_excludes_an_invoice_raised_after_the_snapshot(empty_db_conn):
    import payments_alloc as pa
    c = empty_db_conn
    _ins_sale(c, 'IV_BEFORE', '2026-05-01', 100.0)
    _ins_sale(c, 'IV_AFTER', '2026-07-01', 200.0)

    rows = pa.invoice_settlement(conn=c, as_of='2026-06-05')
    assert [r['doc_base'] for r in rows] == ['IV_BEFORE']


def test_the_as_of_mismatch_is_what_turns_a_settled_invoice_into_a_finding(empty_db_conn):
    """Same data, two comparisons: without as_of the diagnostic invents an
    unexplained row; with it, the books agree."""
    import payments_alloc as pa
    c = empty_db_conn
    _ins_sale(c, 'IV200', '2026-05-01', 1000.0)
    re = _ins_receipt(c, 'RE_LATE', '2026-07-20')
    _ins_paid(c, re, 'IV200', 1000.0)
    snap = [_snap('IV200', 1000.0, date='2026-05-01')]

    wrong = build_ar_reconciliation(snap, pa.invoice_settlement(conn=c), era_start=ERA)
    right = build_ar_reconciliation(snap, pa.invoice_settlement(conn=c, as_of='2026-06-05'),
                                    era_start=ERA)

    assert _reasons(wrong) == {'unexplained': 1}
    assert _reasons(right) == {'agrees': 1}
