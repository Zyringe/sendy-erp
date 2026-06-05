"""Cash flow analytics for Sendy ERP — Phase 2 Cash Flow dashboard.

Read-only. NO routes, templates, schema changes, or DB writes.

Connection style mirrors commission.py / payments_alloc.py: own _connect()
reading config.DATABASE_PATH; every public function accepts an optional
caller-supplied conn (used, not closed) else opens/owns one.

AR SOURCE (ar_aging, 2026-05-30):
  ar_aging() now reads from express_ar_outstanding WHERE entity='BSN' at
  the latest snapshot_date_iso. Express is the authoritative BSN AR source;
  the old derived engine (invoice_settlement) is kept as a DIAGNOSTIC only.

REUSE RULE — do NOT re-implement reconciliation here:
  - payments_alloc.py owns invoice_settlement() and the legacy-NULL-amount
    rule. invoice_settlement() is kept importable for diagnostic/reconcile
    tooling but is no longer called by ar_aging().
  - cash_in_by_month() aggregates payments_alloc.cash_in_rows() by month
    so it shares the exact same per-invoice attribution rule. This keeps
    Σ cash_in == Σ invoice_settlement().collected (within float noise).

CREDIT NOTES ARE NOT CASH:
  Credit-note (SR) netting lives entirely in payments_alloc — it reduces
  net_owed / outstanding but a credit note was never cash received.
  cash_in_by_month() and payments_alloc.cash_in_rows() are therefore
  UNCHANGED by credit notes: they still report only actual receipts, and
  Σ cash_in == Σ collected continues to hold. Only ar_aging() surfaces
  credit notes (via total_credit_notes) since it reports AR balances.

LEGACY-NULL-AMOUNT RULE (same as payments_alloc.py — real wins over NULL):
  Rows imported before migration 058 have paid_invoices.amount IS NULL.
  Those rows mean "linked but amount unknown". Per invoice:
    • has_real → Σ real link amounts, each in its own receipt month;
      NULL legacy links on that invoice are ignored.
    • pure-legacy (no real link, ≥1 non-cancelled NULL link) → full billed
      (Σ net of doc_base) once, attributed to the MAX non-cancelled
      receipt month.

HOW cash_in_by_month RECONCILES WITH invoice_settlement
  invoice_settlement groups by doc_base and attributes collected to the
  invoice level (no monthly breakdown). cash_in_by_month groups by RE month.
  Both functions call the same underlying join idiom and apply the same
  legacy-NULL rule, so Σ cash_in == Σ collected (within ฿0.01).

Python 3.9 — Optional[...] not X | None syntax.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import List, Optional

from config import DATABASE_PATH
import payments_alloc as pa


# ── Canonical BSN AR filter ──────────────────────────────────────────────────
# Every page that totals BSN AR must apply this to the latest snapshot so the
# numbers agree (enforced by tests/test_ar_reconcile.py). Excludes:
#   - RE / is_anomalous receipts — Put: "ลูกหนี้จ่ายแล้ว" (already paid), and
#   - pre-2024 legacy debt — before the Sendy era (Put 2026-06-04).
# Bare column names (no table alias) — unambiguous since only express_ar_outstanding
# has these columns, even in queries that JOIN customers.
#
# The `doc_no NOT IN (SELECT ... ar_writeoffs)` clause makes accountant-decided
# write-offs / write-backs drop from the collectable figure PERMANENTLY, even
# after the next ลูกหนี้คงค้าง import replaces express_ar_outstanding (the snapshot
# is DELETE+INSERTed per import; ar_writeoffs is keyed on doc_no and survives).
# `doc_no` is unambiguous here for the same reason as the bare columns above.
# LOAD-BEARING: ar_writeoffs.doc_no must stay NOT NULL (mig 095). A NULL in the
# subquery makes `doc_no NOT IN (SELECT doc_no FROM ar_writeoffs)` evaluate to
# NULL (never true) for every row → collectable AR collapses to 0.
BSN_AR_PREDICATE = (
    "is_anomalous = 0 AND doc_date_iso >= '2024-01-01' "
    "AND doc_no NOT IN (SELECT doc_no FROM ar_writeoffs)"
)


# ── DB helpers (mirrors payments_alloc._ConnCtx) ──────────────────────────────

def _connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class _ConnCtx:
    """Use the caller's connection if given (no close); else open/own one."""

    def __init__(self, conn: Optional[sqlite3.Connection],
                 db_path: Optional[str]):
        self._given = conn
        self._db_path = db_path
        self._owned: Optional[sqlite3.Connection] = None

    def __enter__(self) -> sqlite3.Connection:
        if self._given is not None:
            return self._given
        self._owned = _connect(self._db_path)
        return self._owned

    def __exit__(self, *exc):
        if self._owned is not None:
            self._owned.close()
        return False


def _today_iso() -> str:
    return date.today().isoformat()


# ── 1. cash_in_by_month ───────────────────────────────────────────────────────

def cash_in_by_month(date_from: Optional[str] = None,
                     date_to: Optional[str] = None,
                     conn: Optional[sqlite3.Connection] = None,
                     db_path: Optional[str] = None) -> List[dict]:
    """Cash actually received, grouped by received_payments.date_iso month.

    Returns list[{
        'month':    'YYYY-MM',
        'cash_in':  float,   # sum of cash received in this month
        'receipts': int,     # count of distinct non-cancelled receipt rows
    }] sorted by month ASC.

    LEGACY-NULL-AMOUNT RULE applied per invoice (real wins over NULL):
      has_real → Σ real link amounts, each in its own receipt month.
      pure-legacy (no real link, ≥1 non-cancelled NULL link) → full billed
      (Σ net of doc_base) once, in the MAX non-cancelled receipt month.

    date_from / date_to filter on received_payments.date_iso.

    RECONCILIATION GUARANTEE:
      Σ cash_in == Σ invoice_settlement().collected (within ฿0.01).
      Both aggregate payments_alloc.cash_in_rows(), so the per-invoice
      attribution rule is shared verbatim.
    """
    from collections import defaultdict

    rows = pa.cash_in_rows(conn=conn, db_path=db_path,
                           date_from=date_from, date_to=date_to)

    month_cash = defaultdict(lambda: {'cash_in': 0.0, 're_ids': set()})
    for r in rows:
        m = r['month']
        month_cash[m]['cash_in'] = round(
            month_cash[m]['cash_in'] + float(r['amount']), 2)
        month_cash[m]['re_ids'].add(r['re_id'])

    result = []
    for month in sorted(month_cash):
        entry = month_cash[month]
        result.append({
            'month':    month,
            'cash_in':  round(entry['cash_in'], 2),
            'receipts': len(entry['re_ids']),
        })
    return result


# ── 2. ar_aging ───────────────────────────────────────────────────────────────

def ar_aging(as_of: Optional[str] = None,
             conn: Optional[sqlite3.Connection] = None,
             db_path: Optional[str] = None) -> dict:
    """Point-in-time AR aging bucketed by days outstanding.

    Source: Express BSN snapshot (express_ar_outstanding WHERE entity='BSN'
    at the latest snapshot_date_iso). This is the authoritative AR source —
    it captures RE-doc receivables and legacy IV4* rows that the old derived
    engine (invoice_settlement) missed.

    `as_of` is accepted for API compatibility but ignored: the snapshot date
    is always used as the reference point so bucket ages match what Express
    published on that date (same convention as ar_followup.customer_ranking).

    Returns:
      {
        'as_of': 'YYYY-MM-DD',          # snapshot_date_iso
        'buckets': [
            {'label':'0-30',  'from':0,  'to':30,  'amount':float, 'count':int},
            {'label':'31-60', 'from':31, 'to':60,  'amount':float, 'count':int},
            {'label':'61-90', 'from':61, 'to':90,  'amount':float, 'count':int},
            {'label':'90+',   'from':91, 'to':None, 'amount':float, 'count':int},
        ],
        'total_outstanding':   float,   # SUM(outstanding_amount) — includes negatives
        'total_billed':        float,   # SUM(bill_amount)
        'total_credit_notes':  float,   # always 0 (Express pre-nets CNs)
        'total_collected':     float,   # SUM(paid_amount)
      }

    RECONCILE IDENTITY:
      total_billed - total_collected == total_outstanding  (within ฿0.01).
      (total_credit_notes is always 0 here — Express already netted CNs into
      outstanding_amount and paid_amount, so the old three-term identity
      collapses to the two-term form.)

    NOTE: invoice_settlement() is preserved as a diagnostic. It is no longer
    called by ar_aging() but remains importable for reconciliation tooling.
    """
    buckets = [
        {'label': '0-30',  'from': 0,  'to': 30,  'amount': 0.0, 'count': 0},
        {'label': '31-60', 'from': 31, 'to': 60,  'amount': 0.0, 'count': 0},
        {'label': '61-90', 'from': 61, 'to': 90,  'amount': 0.0, 'count': 0},
        {'label': '90+',   'from': 91, 'to': None, 'amount': 0.0, 'count': 0},
    ]

    with _ConnCtx(conn, db_path) as c:
        snap = c.execute(
            "SELECT MAX(snapshot_date_iso) AS snap"
            " FROM express_ar_outstanding WHERE entity='BSN'"
        ).fetchone()['snap']

        if not snap:
            # No snapshot yet — return empty structure so the dashboard
            # renders gracefully (AR Aging section shows zeros).
            return {
                'as_of':              as_of or _today_iso(),
                'buckets':            buckets,
                'total_outstanding':  0.0,
                'total_billed':       0.0,
                'total_credit_notes': 0.0,
                'total_collected':    0.0,
            }

        snap_date = date.fromisoformat(snap)

        rows = c.execute(
            f"""SELECT doc_date_iso, outstanding_amount, bill_amount, paid_amount
               FROM express_ar_outstanding
               WHERE entity = 'BSN' AND snapshot_date_iso = ?
                 AND {BSN_AR_PREDICATE}""",
            (snap,),
        ).fetchall()

    total_billed    = 0.0
    total_collected = 0.0
    total_outstanding = 0.0

    for r in rows:
        amt = float(r['outstanding_amount'] or 0)
        total_billed    = round(total_billed    + float(r['bill_amount'] or 0), 2)
        total_collected = round(total_collected + float(r['paid_amount'] or 0), 2)
        total_outstanding = round(total_outstanding + amt, 2)

        doc_d = r['doc_date_iso']
        if doc_d:
            try:
                age = (snap_date - date.fromisoformat(doc_d)).days
            except (ValueError, TypeError):
                age = 9999
        else:
            age = 9999
        age = max(age, 0)

        if age <= 30:
            b = buckets[0]
        elif age <= 60:
            b = buckets[1]
        elif age <= 90:
            b = buckets[2]
        else:
            b = buckets[3]
        b['amount'] = round(b['amount'] + amt, 2)
        b['count'] += 1

    return {
        'as_of':              snap,
        'buckets':            buckets,
        'total_outstanding':  round(total_outstanding, 2),
        'total_billed':       round(total_billed, 2),
        'total_credit_notes': 0.0,
        'total_collected':    round(total_collected, 2),
    }


# ── 3. revenue_by_month ───────────────────────────────────────────────────────

def revenue_by_month(date_from: Optional[str] = None,
                     date_to: Optional[str] = None,
                     conn: Optional[sqlite3.Connection] = None,
                     db_path: Optional[str] = None) -> List[dict]:
    """Accrual revenue grouped by sales_transactions.date_iso month (sale date).

    Returns list[{'month': 'YYYY-MM', 'revenue': float}] sorted by month ASC.

    This is the accrual counterpart to cash_in_by_month: revenue is
    recognised when the sale occurs, not when cash is received.
    Comparing both series shows the timing difference between earned revenue
    and collected cash.

    Phase-3 Revenue-dashboard hook — cheap to compute alongside cash flow.
    """
    conds = ["doc_base IS NOT NULL",
             "doc_base NOT LIKE 'SR%'",
             "doc_base NOT LIKE 'HS%'"]
    params = []
    if date_from:
        conds.append("date_iso >= ?")
        params.append(date_from)
    if date_to:
        conds.append("date_iso <= ?")
        params.append(date_to)

    where = " AND ".join(conds)
    sql = f"""
        SELECT SUBSTR(date_iso, 1, 7) AS month,
               ROUND(SUM(net), 2)     AS revenue
        FROM sales_transactions
        WHERE {where}
        GROUP BY month
        ORDER BY month
    """

    with _ConnCtx(conn, db_path) as c:
        rows = c.execute(sql, params).fetchall()

    return [{'month': r['month'], 'revenue': round(r['revenue'] or 0.0, 2)}
            for r in rows]


def bsn_ar_excluded(conn: Optional[sqlite3.Connection] = None,
                    db_path: Optional[str] = None) -> dict:
    """Amounts EXCLUDED from canonical BSN AR (see BSN_AR_PREDICATE), for
    disclosure on the AR pages so the amount removed from the gross snapshot
    isn't hidden: pre-2024 legacy debt + RE/anomalous receipts + write-offs.

    The three buckets are DISJOINT and together are the exact complement of
    BSN_AR_PREDICATE, so collectable + legacy + re + writeoff == gross snapshot:
      - re      = is_anomalous=1                                  (any date)
      - legacy  = is_anomalous=0 AND doc < 2024                   (non-anomalous old)
      - writeoff= is_anomalous=0 AND doc >= 2024 AND in ar_writeoffs
                  (would-be-collectable, removed by an accountant decision)
    A write-off that is itself RE/legacy stays in its re/legacy bucket (the
    writeoff bucket is scoped to the collectable date/anomaly window), so a
    written-off doc is counted in exactly one bucket — no double-count."""
    empty = {'legacy_amount': 0.0, 'legacy_count': 0, 're_amount': 0.0, 're_count': 0,
             'writeoff_amount': 0.0, 'writeoff_count': 0}
    with _ConnCtx(conn, db_path) as c:
        snap = c.execute("SELECT MAX(snapshot_date_iso) AS d FROM "
                         "express_ar_outstanding WHERE entity='BSN'").fetchone()['d']
        if not snap:
            return empty
        legacy = c.execute(
            "SELECT ROUND(SUM(outstanding_amount),2) a, COUNT(*) n "
            "FROM express_ar_outstanding WHERE entity='BSN' AND snapshot_date_iso=? "
            "AND is_anomalous=0 AND doc_date_iso < '2024-01-01'", (snap,)).fetchone()
        re = c.execute(
            "SELECT ROUND(SUM(outstanding_amount),2) a, COUNT(*) n "
            "FROM express_ar_outstanding WHERE entity='BSN' AND snapshot_date_iso=? "
            "AND is_anomalous=1", (snap,)).fetchone()
        writeoff = c.execute(
            "SELECT ROUND(SUM(outstanding_amount),2) a, COUNT(*) n "
            "FROM express_ar_outstanding WHERE entity='BSN' AND snapshot_date_iso=? "
            "AND is_anomalous=0 AND doc_date_iso >= '2024-01-01' "
            "AND doc_no IN (SELECT doc_no FROM ar_writeoffs)", (snap,)).fetchone()
    return {'legacy_amount': legacy['a'] or 0.0, 'legacy_count': legacy['n'] or 0,
            're_amount': re['a'] or 0.0, 're_count': re['n'] or 0,
            'writeoff_amount': writeoff['a'] or 0.0, 'writeoff_count': writeoff['n'] or 0}


def bsn_ar_excluded_by_customer(conn: Optional[sqlite3.Connection] = None,
                                db_path: Optional[str] = None) -> List[dict]:
    """Per-customer debt EXCLUDED from collectable AR (the complement of
    BSN_AR_PREDICATE): RE/anomalous receipts (e.g. จึงเจริญ, a write-off review)
    and pre-2024 legacy debt (e.g. ทรงพลเทรดดิ้ง 2014). Shown as a separate
    'not collectable' section on the dunning page so it stays trackable without
    inflating the collection target. Net per customer, largest first."""
    with _ConnCtx(conn, db_path) as c:
        snap = c.execute("SELECT MAX(snapshot_date_iso) AS d FROM "
                         "express_ar_outstanding WHERE entity='BSN'").fetchone()['d']
        if not snap:
            return []
        # WHERE = exact complement of BSN_AR_PREDICATE so every excluded doc
        # (RE, pre-2024 legacy, OR a written-off would-be-collectable) appears
        # exactly once. The collectable set is
        #   (is_anomalous=0 AND doc>=2024 AND doc_no NOT IN ar_writeoffs);
        # its NOT(...) pulls the written-off recents into this excluded section.
        rows = c.execute("""
            SELECT ao.customer_code,
                   COALESCE(cust.name, ao.customer_name) AS customer_name,
                   ROUND(SUM(ao.outstanding_amount), 2)  AS outstanding,
                   COUNT(*)                              AS doc_count,
                   MAX(CASE WHEN ao.is_anomalous = 1 THEN 1 ELSE 0 END) AS has_re,
                   MAX(CASE WHEN ao.is_anomalous = 0 AND ao.doc_date_iso < '2024-01-01'
                            THEN 1 ELSE 0 END) AS has_legacy,
                   MAX(CASE WHEN ao.is_anomalous = 0 AND ao.doc_date_iso >= '2024-01-01'
                             AND ao.doc_no IN (SELECT doc_no FROM ar_writeoffs)
                            THEN 1 ELSE 0 END) AS has_writeoff
            FROM express_ar_outstanding ao
            LEFT JOIN customers cust ON cust.code = ao.customer_code
            WHERE ao.entity = 'BSN' AND ao.snapshot_date_iso = ?
              AND NOT (ao.is_anomalous = 0 AND ao.doc_date_iso >= '2024-01-01'
                       AND ao.doc_no NOT IN (SELECT doc_no FROM ar_writeoffs))
            GROUP BY ao.customer_code
            HAVING ROUND(SUM(ao.outstanding_amount), 2) > 0
            ORDER BY outstanding DESC
        """, (snap,)).fetchall()
    return [dict(r) for r in rows]
