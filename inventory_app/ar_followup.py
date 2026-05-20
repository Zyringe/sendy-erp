"""AR follow-up workspace logic for Sendy ERP.

Drives /accounting/ar-followup — a ranked workspace for chasing unpaid
invoices. Settlement truth comes from `payments_alloc.invoice_settlement`
(the authoritative engine — VAT-aware billed, credit-note-netted,
legacy-NULL-rule-applied). Outreach attempts persist to `ar_followup_log`
(migration 065).

Public surface
──────────────
- customer_ranking(...)           — per-customer roll-up sorted by outstanding DESC
- get_customer_ar_detail(...)     — outstanding invoices for one customer + age
- get_customer_followups(...)     — outreach history for one customer (newest first)
- list_overdue_followups(...)     — followups whose next_action_date has passed
- log_outreach(...)               — insert an outreach attempt
- update_outreach(...)            — edit one
- delete_outreach(...)            — delete one

Connection style mirrors hr.py / payments_alloc.py: every function accepts
an optional caller `conn`; else opens its own from config.DATABASE_PATH.
"""
from datetime import date
from typing import Optional, List
import sqlite3

import config
import payments_alloc as pa


_AGE_BUCKETS = ('0-30', '31-60', '61-90', '90+')


def _connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or config.DATABASE_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class _ConnCtx:
    def __init__(self, conn, db_path):
        self._given, self._db_path, self._owned = conn, db_path, None

    def __enter__(self):
        if self._given is not None:
            return self._given
        self._owned = _connect(self._db_path)
        return self._owned

    def __exit__(self, *exc):
        if self._owned is not None:
            self._owned.close()
        return False


def _bucket_of(age: int) -> str:
    if age <= 30:
        return '0-30'
    if age <= 60:
        return '31-60'
    if age <= 90:
        return '61-90'
    return '90+'


# ── ranking ─────────────────────────────────────────────────────────────────

def customer_ranking(conn: Optional[sqlite3.Connection] = None,
                     db_path: Optional[str] = None,
                     min_outstanding: float = 0.0) -> List[dict]:
    """Per-customer outstanding roll-up sorted by outstanding DESC.

    Each row:
      {
        'customer', 'customer_code',
        'invoice_count': int,
        'outstanding':   float,
        'oldest_age_days': int,
        'age_buckets':   {'0-30': float, '31-60': float, '61-90': float, '90+': float},
        'last_log_date': Optional[str],
        'last_log_result': Optional[str],
        'next_action_date': Optional[str],
      }
    """
    with _ConnCtx(conn, db_path) as c:
        today = date.today()
        rows = pa.invoice_settlement(conn=c)
        # Aggregate per customer (use the string as the grouping key; this
        # matches sales_transactions.customer and how the rest of the AR
        # surface keys customers).
        agg: dict = {}
        for r in rows:
            if r['outstanding'] <= 0.005:
                continue
            cust = r['customer'] or ''
            entry = agg.setdefault(cust, {
                'customer': cust,
                'customer_code': r.get('customer_code'),
                'invoice_count': 0,
                'outstanding': 0.0,
                'oldest_age_days': 0,
                'age_buckets': {b: 0.0 for b in _AGE_BUCKETS},
            })
            entry['invoice_count'] += 1
            entry['outstanding'] = round(entry['outstanding'] + r['outstanding'], 2)
            if r['invoice_date']:
                age = (today - date.fromisoformat(r['invoice_date'])).days
                if age > entry['oldest_age_days']:
                    entry['oldest_age_days'] = age
                entry['age_buckets'][_bucket_of(age)] = round(
                    entry['age_buckets'][_bucket_of(age)] + r['outstanding'], 2
                )
            # Backfill customer_code if a later invoice has one and earlier didn't.
            if not entry['customer_code'] and r.get('customer_code'):
                entry['customer_code'] = r['customer_code']

        # Attach last outreach (newest per customer) if table exists.
        if _has_log_table(c):
            log_rows = c.execute("""
                SELECT customer, MAX(log_date) AS last_log_date
                FROM ar_followup_log
                GROUP BY customer
            """).fetchall()
            last_map = {row['customer']: row['last_log_date'] for row in log_rows}
            # Pull result + next_action_date for that date row (newest id wins ties).
            for cust, last_date in last_map.items():
                detail = c.execute("""
                    SELECT result, next_action_date
                    FROM ar_followup_log
                    WHERE customer = ? AND log_date = ?
                    ORDER BY id DESC LIMIT 1
                """, (cust, last_date)).fetchone()
                if cust in agg:
                    agg[cust]['last_log_date'] = last_date
                    agg[cust]['last_log_result'] = detail['result'] if detail else None
                    agg[cust]['next_action_date'] = detail['next_action_date'] if detail else None
        for entry in agg.values():
            entry.setdefault('last_log_date', None)
            entry.setdefault('last_log_result', None)
            entry.setdefault('next_action_date', None)

        out = [e for e in agg.values() if e['outstanding'] >= min_outstanding]
        out.sort(key=lambda e: -e['outstanding'])
        return out


def get_customer_ar_detail(customer: str,
                            conn: Optional[sqlite3.Connection] = None,
                            db_path: Optional[str] = None) -> List[dict]:
    """Outstanding invoices for one customer with age_days, sorted by age DESC.

    Each row carries everything `invoice_settlement` returns + age_days + vat_type.
    """
    with _ConnCtx(conn, db_path) as c:
        today = date.today()
        rows = pa.invoice_settlement(customer=customer, conn=c)
        out = []
        for r in rows:
            if r['outstanding'] <= 0.005:
                continue
            age = None
            if r['invoice_date']:
                age = (today - date.fromisoformat(r['invoice_date'])).days
            # Look up vat_type (one per doc_base).
            vt_row = c.execute(
                "SELECT vat_type FROM sales_transactions WHERE doc_base=? LIMIT 1",
                (r['doc_base'],)
            ).fetchone()
            out.append({
                **dict(r),
                'age_days': age,
                'vat_type': vt_row['vat_type'] if vt_row else None,
            })
        out.sort(key=lambda x: -(x['age_days'] or 0))
        return out


# ── outreach log CRUD ───────────────────────────────────────────────────────

def _has_log_table(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ar_followup_log'"
    ).fetchone() is not None


def log_outreach(customer: str, log_date: str, channel: str, result: str,
                 created_by: str,
                 customer_code: Optional[str] = None,
                 contact_person: Optional[str] = None,
                 promised_amount: Optional[float] = None,
                 promised_date: Optional[str] = None,
                 next_action_date: Optional[str] = None,
                 notes: Optional[str] = None,
                 conn: Optional[sqlite3.Connection] = None,
                 db_path: Optional[str] = None) -> int:
    """Insert one outreach attempt. Returns the new row id.

    Raises sqlite3.IntegrityError for bad channel/result enums (CHECK).
    """
    with _ConnCtx(conn, db_path) as c:
        cur = c.execute("""
            INSERT INTO ar_followup_log
              (customer, customer_code, log_date, channel, contact_person,
               result, promised_amount, promised_date, next_action_date,
               notes, created_by)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (customer, customer_code, log_date, channel, contact_person,
              result, promised_amount, promised_date, next_action_date,
              notes, created_by))
        # Only commit when we own the connection. Caller-supplied conn (tests,
        # multi-step routes) commits on its own boundary.
        if conn is None:
            c.commit()
        return cur.lastrowid


def update_outreach(log_id: int, *,
                    log_date: Optional[str] = None,
                    channel: Optional[str] = None,
                    contact_person: Optional[str] = None,
                    result: Optional[str] = None,
                    promised_amount: Optional[float] = None,
                    promised_date: Optional[str] = None,
                    next_action_date: Optional[str] = None,
                    notes: Optional[str] = None,
                    conn: Optional[sqlite3.Connection] = None,
                    db_path: Optional[str] = None) -> None:
    """Patch only fields the caller actually passes."""
    fields, params = [], []
    for k, v in [('log_date', log_date), ('channel', channel),
                 ('contact_person', contact_person), ('result', result),
                 ('promised_amount', promised_amount),
                 ('promised_date', promised_date),
                 ('next_action_date', next_action_date), ('notes', notes)]:
        if v is not None:
            fields.append(f"{k} = ?")
            params.append(v)
    if not fields:
        return
    fields.append("updated_at = datetime('now','localtime')")
    params.append(log_id)
    with _ConnCtx(conn, db_path) as c:
        c.execute(f"UPDATE ar_followup_log SET {', '.join(fields)} WHERE id = ?", params)
        if conn is None:
            c.commit()


def delete_outreach(log_id: int,
                    conn: Optional[sqlite3.Connection] = None,
                    db_path: Optional[str] = None) -> None:
    with _ConnCtx(conn, db_path) as c:
        c.execute("DELETE FROM ar_followup_log WHERE id = ?", (log_id,))
        if conn is None:
            c.commit()


def get_customer_followups(customer: str,
                           conn: Optional[sqlite3.Connection] = None,
                           db_path: Optional[str] = None) -> List[dict]:
    """All outreach rows for one customer, newest log_date first (id tiebreak)."""
    with _ConnCtx(conn, db_path) as c:
        rows = c.execute("""
            SELECT * FROM ar_followup_log
            WHERE customer = ?
            ORDER BY log_date DESC, id DESC
        """, (customer,)).fetchall()
        return [dict(r) for r in rows]


def list_overdue_followups(as_of: Optional[str] = None,
                            conn: Optional[sqlite3.Connection] = None,
                            db_path: Optional[str] = None) -> List[dict]:
    """Outreach rows whose `next_action_date` is on or before as_of (today)."""
    as_of = as_of or date.today().isoformat()
    with _ConnCtx(conn, db_path) as c:
        rows = c.execute("""
            SELECT * FROM ar_followup_log
            WHERE next_action_date IS NOT NULL AND next_action_date <= ?
            ORDER BY next_action_date ASC, id ASC
        """, (as_of,)).fetchall()
        return [dict(r) for r in rows]
