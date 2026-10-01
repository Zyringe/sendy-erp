"""Every BSN AR surface must total the SAME canonical figure.

Canonical AR = latest Express BSN snapshot, EXCLUDING:
  - RE / is_anomalous receipts (Put: "ลูกหนี้จ่ายแล้ว", already paid), and
  - pre-2024 legacy debt (before the Sendy era).

Before this was reconciled the three helpers of the day diverged badly:
  get_customer_debt_summary ฿732,157  ·  ar_aging ฿1,299,335  ·  customer_ranking ฿1,325,201
which is exactly the confusion this guards against.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import ar_followup
import ar_statement
import cashflow
from ar_statement import BSN_AR_PREDICATE

_LATEST = ("entity='BSN' AND snapshot_date_iso=(SELECT MAX(snapshot_date_iso)"
           " FROM express_ar_outstanding WHERE entity='BSN')")


def _canonical_total(db_path):
    # The predicate is IMPORTED: a re-typed oracle can drift from production
    # and keep passing (ADR 0012).
    conn = sqlite3.connect(db_path)
    try:
        n = conn.execute(f"""
            SELECT ROUND(SUM(outstanding_amount), 2) FROM express_ar_outstanding
             WHERE {_LATEST} AND {BSN_AR_PREDICATE}
        """).fetchone()[0]
        return round(n or 0, 2)
    finally:
        conn.close()


def _canonical_by_code(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {code: (round(total, 2), n) for code, total, n in conn.execute(f"""
            SELECT TRIM(customer_code), SUM(outstanding_amount), COUNT(*)
              FROM express_ar_outstanding
             WHERE {_LATEST} AND {BSN_AR_PREDICATE}
             GROUP BY TRIM(customer_code)
        """)}
    finally:
        conn.close()


def test_all_ar_surfaces_agree_on_canonical_total(tmp_db):
    canonical = _canonical_total(tmp_db)
    assert canonical > 0   # sanity: the live copy has BSN AR

    totals = round(sum(t['outstanding'] for t in ar_statement.customer_totals()), 2)
    aging = round(cashflow.ar_aging()['total_outstanding'], 2)
    ranking = round(sum(r['outstanding'] or 0
                        for r in ar_followup.customer_ranking()), 2)

    assert abs(totals - canonical) < 0.01, f"customer_totals {totals} != {canonical}"
    assert abs(aging - canonical) < 0.01, f"ar_aging {aging} != {canonical}"
    assert abs(ranking - canonical) < 0.01, f"customer_ranking {ranking} != {canonical}"


def test_totals_and_statements_agree_per_trimmed_code(tmp_db):
    oracle = _canonical_by_code(tmp_db)
    totals = {t['customer_code']: t for t in ar_statement.customer_totals()}
    owing = {code: v for code, v in oracle.items() if v[0] > 0.005}
    assert owing, 'the dev DB copy needs chaseable AR for this test to mean anything'
    assert set(totals) == set(owing)
    for code, (total, n) in owing.items():
        st = ar_statement.customer_statement(code)
        assert (totals[code]['outstanding'], totals[code]['invoice_count']) == (total, n), code
        assert (st['total'], len(st['chaseable'])) == (total, n), code


def test_canonical_excludes_re_and_pre2024(tmp_db):
    """Guard the two exclusions explicitly so a future filter change is caught."""
    conn = sqlite3.connect(tmp_db)
    try:
        unfiltered = conn.execute("""
            WITH latest AS (SELECT MAX(snapshot_date_iso) d
                            FROM express_ar_outstanding WHERE entity='BSN')
            SELECT ROUND(SUM(outstanding_amount),2) FROM express_ar_outstanding
            WHERE entity='BSN' AND snapshot_date_iso=(SELECT d FROM latest)
        """).fetchone()[0] or 0
    finally:
        conn.close()
    # the canonical total must be strictly less (RE + legacy are excluded)
    assert _canonical_total(tmp_db) < round(unfiltered, 2)


def test_excluded_disclosure_reconciles_to_gross(tmp_db):
    """canonical + legacy + RE + writeoff must equal the gross snapshot — so the
    disclosure note on the AR pages is accurate (the four buckets are the exact
    disjoint complement of the collectable predicate)."""
    import cashflow
    conn = sqlite3.connect(tmp_db)
    try:
        gross = conn.execute("""
            WITH latest AS (SELECT MAX(snapshot_date_iso) d
                            FROM express_ar_outstanding WHERE entity='BSN')
            SELECT ROUND(SUM(outstanding_amount),2) FROM express_ar_outstanding
            WHERE entity='BSN' AND snapshot_date_iso=(SELECT d FROM latest)
        """).fetchone()[0] or 0
    finally:
        conn.close()
    exc = cashflow.bsn_ar_excluded(db_path=tmp_db)
    recombined = round(_canonical_total(tmp_db) + exc['legacy_amount']
                       + exc['re_amount'] + exc['writeoff_amount'], 2)
    assert abs(recombined - round(gross, 2)) < 0.01
