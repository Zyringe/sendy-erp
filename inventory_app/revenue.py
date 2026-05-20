"""Revenue analytics for Sendy ERP — Phase 3 Revenue dashboard.

Read-only. NO routes, templates, schema changes, or DB writes.

Connection style mirrors cashflow.py / payments_alloc.py: own _connect()
reading config.DATABASE_PATH; every public function accepts an optional
caller-supplied conn (used, not closed) else opens/owns one.

DATA SOURCE
  sales_transactions (BSN weekly import). Same filter as
  cashflow.revenue_by_month:
    doc_base IS NOT NULL
    doc_base NOT LIKE 'SR%'   -- sales returns (credit notes)
    doc_base NOT LIKE 'HS%'   -- historical opening balance
  Revenue = net (post-doc-discount, pre-VAT).

RECONCILIATION
  Σ top_brands_by_revenue(limit=large_enough).revenue
    == revenue_summary().total_revenue   (within ฿0.01)

Python 3.9 — Optional[...] not X | None syntax.
"""
from __future__ import annotations

import sqlite3
from typing import List, Optional

from config import DATABASE_PATH


# ── DB helpers (mirror cashflow._ConnCtx) ────────────────────────────────────

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


_SALES_FILTER = (
    "doc_base IS NOT NULL "
    "AND doc_base NOT LIKE 'SR%' "
    "AND doc_base NOT LIKE 'HS%'"
)


def _date_conds(date_from: Optional[str], date_to: Optional[str]):
    """Build (extra_where_clause, params) for date filters on date_iso."""
    parts = []
    params = []
    if date_from:
        parts.append("date_iso >= ?")
        params.append(date_from)
    if date_to:
        parts.append("date_iso <= ?")
        params.append(date_to)
    where = (" AND " + " AND ".join(parts)) if parts else ""
    return where, params


# ── 1. revenue_summary ───────────────────────────────────────────────────────

def revenue_summary(date_from: Optional[str] = None,
                    date_to: Optional[str] = None,
                    conn: Optional[sqlite3.Connection] = None,
                    db_path: Optional[str] = None) -> dict:
    """Period KPIs over the filtered sales-line universe.

    Returns {
        'total_revenue':   float,   # Σ net in period (post-doc-discount, pre-VAT)
        'total_invoices':  int,     # COUNT(DISTINCT doc_base)
        'total_customers': int,     # COUNT(DISTINCT customer_code) — falls back
                                    #   to customer name when customer_code is
                                    #   NULL/empty (legacy rows)
        'aov':             float,   # total_revenue / total_invoices (0 if none)
    }
    """
    date_where, params = _date_conds(date_from, date_to)
    sql = f"""
        SELECT ROUND(SUM(net), 2)                 AS total_revenue,
               COUNT(DISTINCT doc_base)           AS total_invoices,
               COUNT(DISTINCT COALESCE(
                   NULLIF(TRIM(customer_code), ''),
                   customer
               ))                                 AS total_customers
          FROM sales_transactions
         WHERE {_SALES_FILTER}{date_where}
    """
    with _ConnCtx(conn, db_path) as c:
        row = c.execute(sql, params).fetchone()

    total_revenue   = (row['total_revenue']   or 0.0) if row else 0.0
    total_invoices  = (row['total_invoices']  or 0)   if row else 0
    total_customers = (row['total_customers'] or 0)   if row else 0
    aov = (total_revenue / total_invoices) if total_invoices else 0.0
    return {
        'total_revenue':   round(total_revenue, 2),
        'total_invoices':  total_invoices,
        'total_customers': total_customers,
        'aov':             round(aov, 2),
    }


# ── 2. top_customers_by_revenue ──────────────────────────────────────────────

def top_customers_by_revenue(date_from: Optional[str] = None,
                             date_to: Optional[str] = None,
                             limit: int = 20,
                             conn: Optional[sqlite3.Connection] = None,
                             db_path: Optional[str] = None) -> List[dict]:
    """Top-N customers by net revenue in the period.

    Group key = COALESCE(NULLIF(TRIM(customer_code),''), customer) so the
    same customer is not split when occasional rows have a missing code.

    Returns list[{
        'customer':       str,             # display name (latest non-empty)
        'customer_code':  Optional[str],
        'revenue':        float,
        'invoice_count':  int,             # distinct doc_base
    }] sorted by revenue DESC, capped at `limit`.
    """
    date_where, params = _date_conds(date_from, date_to)
    sql = f"""
        SELECT COALESCE(NULLIF(TRIM(customer_code), ''), customer)
                                                 AS group_key,
               MAX(NULLIF(TRIM(customer_code), '')) AS customer_code,
               MAX(NULLIF(TRIM(customer), ''))      AS customer,
               ROUND(SUM(net), 2)                AS revenue,
               COUNT(DISTINCT doc_base)          AS invoice_count
          FROM sales_transactions
         WHERE {_SALES_FILTER}{date_where}
         GROUP BY group_key
         ORDER BY revenue DESC
         LIMIT ?
    """
    with _ConnCtx(conn, db_path) as c:
        rows = c.execute(sql, params + [limit]).fetchall()

    return [{
        'customer':      r['customer'] or r['customer_code'] or '(ไม่ระบุชื่อ)',
        'customer_code': r['customer_code'],
        'revenue':       round(r['revenue'] or 0.0, 2),
        'invoice_count': r['invoice_count'],
    } for r in rows]


# ── 3. top_brands_by_revenue ─────────────────────────────────────────────────

def top_brands_by_revenue(date_from: Optional[str] = None,
                          date_to: Optional[str] = None,
                          limit: int = 10,
                          conn: Optional[sqlite3.Connection] = None,
                          db_path: Optional[str] = None) -> List[dict]:
    """Top-N brands by net revenue in the period.

    LEFT JOIN products → brands. Rows where products.brand_id IS NULL (or
    sales_transactions.product_id IS NULL) collapse to a single
    'ไม่ระบุแบรนด์' bucket so it is visible in the dashboard.

    Returns list[{
        'brand_display': str,            # brands.name_th if set else brands.name
                                         # else 'ไม่ระบุแบรนด์'
        'brand_code':    Optional[str],  # brands.code (None for the unbranded bucket)
        'revenue':       float,
        'line_count':    int,            # sales_transactions row count
    }] sorted by revenue DESC, capped at `limit`.
    """
    date_where, params = _date_conds(date_from, date_to)
    sql = f"""
        SELECT b.id   AS brand_id,
               b.code AS brand_code,
               COALESCE(
                   NULLIF(TRIM(b.name_th), ''),
                   NULLIF(TRIM(b.name),    ''),
                   'ไม่ระบุแบรนด์'
               )                            AS brand_display,
               ROUND(SUM(st.net), 2)        AS revenue,
               COUNT(*)                     AS line_count
          FROM sales_transactions st
          LEFT JOIN products p ON p.id = st.product_id
          LEFT JOIN brands   b ON b.id = p.brand_id
         WHERE {_SALES_FILTER.replace('doc_base', 'st.doc_base')}
               {date_where.replace('date_iso', 'st.date_iso')}
         GROUP BY b.id
         ORDER BY revenue DESC
         LIMIT ?
    """
    with _ConnCtx(conn, db_path) as c:
        rows = c.execute(sql, params + [limit]).fetchall()

    return [{
        'brand_display': r['brand_display'],
        'brand_code':    r['brand_code'],
        'revenue':       round(r['revenue'] or 0.0, 2),
        'line_count':    r['line_count'],
    } for r in rows]
