"""Cash flow analytics for Sendy ERP — Phase 2 Cash Flow dashboard.

Read-only. NO routes, templates, schema changes, or DB writes.

Connection style mirrors commission.py / payments_alloc.py: own _connect()
reading config.DATABASE_PATH; every public function accepts an optional
caller-supplied conn (used, not closed) else opens/owns one.

REUSE RULE — do NOT re-implement reconciliation here:
  - payments_alloc.py owns invoice_settlement() and the legacy-NULL-amount
    rule. ar_aging() calls invoice_settlement() directly so it always stays
    in sync with the rule.
  - cash_in_by_month() aggregates payments_alloc.cash_in_rows() by month
    so it shares the exact same per-invoice attribution rule. This keeps
    Σ cash_in == Σ invoice_settlement().collected (within float noise).

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

    Delegates settlement logic entirely to payments_alloc.invoice_settlement
    so the legacy-NULL-amount rule is applied identically.

    Returns:
      {
        'as_of': 'YYYY-MM-DD',
        'buckets': [
            {'label':'0-30',  'from':0,  'to':30,  'amount':float, 'count':int},
            {'label':'31-60', 'from':31, 'to':60,  'amount':float, 'count':int},
            {'label':'61-90', 'from':61, 'to':90,  'amount':float, 'count':int},
            {'label':'90+',   'from':91, 'to':None, 'amount':float, 'count':int},
        ],
        'total_outstanding': float,
        'total_billed':      float,
        'total_collected':   float,
      }

    RECONCILE IDENTITY:
      total_billed == total_collected + total_outstanding (within ฿0.01).
    """
    _EPS = 0.005
    as_of_str = as_of or _today_iso()
    as_of_date = date.fromisoformat(as_of_str)

    # Use caller conn if provided; pa.invoice_settlement accepts conn too
    rows = pa.invoice_settlement(conn=conn, db_path=db_path)

    # Point-in-time: only invoices with invoice_date <= as_of
    # (payments_alloc._settlement_rows uses as_of param for this, but here
    #  we call the simpler invoice_settlement; filter in Python)
    rows = [r for r in rows if r['invoice_date'] and r['invoice_date'] <= as_of_str]

    # Only keep outstanding invoices for aging
    outstanding_rows = [r for r in rows if r['outstanding'] > _EPS]

    buckets = [
        {'label': '0-30',  'from': 0,  'to': 30,  'amount': 0.0, 'count': 0},
        {'label': '31-60', 'from': 31, 'to': 60,  'amount': 0.0, 'count': 0},
        {'label': '61-90', 'from': 61, 'to': 90,  'amount': 0.0, 'count': 0},
        {'label': '90+',   'from': 91, 'to': None, 'amount': 0.0, 'count': 0},
    ]

    for inv in outstanding_rows:
        inv_date = date.fromisoformat(inv['invoice_date'])
        age = (as_of_date - inv_date).days
        if age <= 30:
            b = buckets[0]
        elif age <= 60:
            b = buckets[1]
        elif age <= 90:
            b = buckets[2]
        else:
            b = buckets[3]
        b['amount'] = round(b['amount'] + inv['outstanding'], 2)
        b['count'] += 1

    total_outstanding = round(sum(b['amount'] for b in buckets), 2)
    total_billed      = round(sum(r['billed']    for r in rows), 2)
    total_collected   = round(sum(r['collected'] for r in rows), 2)

    return {
        'as_of':             as_of_str,
        'buckets':           buckets,
        'total_outstanding': total_outstanding,
        'total_billed':      total_billed,
        'total_collected':   total_collected,
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
