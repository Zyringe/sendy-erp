"""Every BSN AR surface must total the SAME canonical figure.

Canonical AR = latest Express BSN snapshot, EXCLUDING:
  - RE / is_anomalous receipts (Put: "ลูกหนี้จ่ายแล้ว", already paid), and
  - pre-2024 legacy debt (before the Sendy era).

Before this was reconciled the three helpers diverged badly:
  get_customer_debt_summary ฿732,157  ·  ar_aging ฿1,299,335  ·  customer_ranking ฿1,325,201
which is exactly the confusion this guards against.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import models
import cashflow
import ar_followup


def _canonical_total(db_path):
    # Mirror the FULL BSN_AR_PREDICATE — including the write-off exclusion
    # (ar_writeoffs). If this drifts from the real predicate the "all surfaces
    # agree" guard would compare against a wrong oracle and pass/fail falsely.
    conn = sqlite3.connect(db_path)
    try:
        n = conn.execute("""
            WITH latest AS (SELECT MAX(snapshot_date_iso) d
                            FROM express_ar_outstanding WHERE entity='BSN')
            SELECT ROUND(SUM(outstanding_amount), 2)
            FROM express_ar_outstanding
            WHERE entity='BSN'
              AND snapshot_date_iso=(SELECT d FROM latest)
              AND is_anomalous=0
              AND doc_date_iso >= '2024-01-01'
              AND doc_no NOT IN (SELECT doc_no FROM ar_writeoffs)
        """).fetchone()[0]
        return round(n or 0, 2)
    finally:
        conn.close()


def test_all_ar_surfaces_agree_on_canonical_total(tmp_db):
    canonical = _canonical_total(tmp_db)
    assert canonical > 0   # sanity: the live copy has BSN AR

    debt_summary = round(sum(r['outstanding_amount'] or 0
                             for r in models.get_customer_debt_summary()), 2)
    aging = round(cashflow.ar_aging()['total_outstanding'], 2)
    ranking = round(sum(r['outstanding'] or 0
                        for r in ar_followup.customer_ranking()), 2)

    assert abs(debt_summary - canonical) < 0.01, f"payment_customers {debt_summary} != {canonical}"
    assert abs(aging - canonical) < 0.01, f"ar_aging {aging} != {canonical}"
    assert abs(ranking - canonical) < 0.01, f"customer_ranking {ranking} != {canonical}"


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
