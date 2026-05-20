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

# Terminal outreach results — once any of these is the latest log for a
# customer the account is considered closed and is not reported as overdue
# even if next_action_date is in the past.
_TERMINAL_RESULTS = ('paid_full', 'closed')


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
        # Aggregate per customer using customer_code as the stable key (falling
        # back to customer name when no code exists, e.g. walk-in / หน้าร้าน).
        # Keying by mutable name would split one debtor across spellings and
        # orphan follow-up history; see Codex review 2026-05-20.
        agg: dict = {}
        canonical_invoice_date: dict = {}  # group_key -> ISO date of row that set 'customer'
        for r in rows:
            if r['outstanding'] <= 0.005:
                continue
            code = (r.get('customer_code') or '').strip()
            name = r['customer'] or ''
            key = code or name
            entry = agg.setdefault(key, {
                'customer': name,
                'customer_code': code or None,
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
                # Canonical display name = name from the most recent invoice.
                prev = canonical_invoice_date.get(key, '')
                if r['invoice_date'] > prev:
                    canonical_invoice_date[key] = r['invoice_date']
                    entry['customer'] = name

        # Attach last outreach (newest per group) if the log table exists.
        # Group key matches the ranking aggregation so name-spelling variants
        # roll up together.
        if _has_log_table(c):
            log_rows = c.execute("""
                SELECT
                  COALESCE(NULLIF(TRIM(customer_code), ''), customer) AS group_key,
                  MAX(log_date) AS last_log_date
                FROM ar_followup_log
                GROUP BY group_key
            """).fetchall()
            for lr in log_rows:
                key = lr['group_key']
                if key not in agg:
                    continue
                detail = c.execute("""
                    SELECT result, next_action_date
                    FROM ar_followup_log
                    WHERE COALESCE(NULLIF(TRIM(customer_code), ''), customer) = ?
                      AND log_date = ?
                    ORDER BY id DESC LIMIT 1
                """, (key, lr['last_log_date'])).fetchone()
                agg[key]['last_log_date'] = lr['last_log_date']
                agg[key]['last_log_result'] = detail['result'] if detail else None
                agg[key]['next_action_date'] = detail['next_action_date'] if detail else None
        for entry in agg.values():
            entry.setdefault('last_log_date', None)
            entry.setdefault('last_log_result', None)
            entry.setdefault('next_action_date', None)

        out = [e for e in agg.values() if e['outstanding'] >= min_outstanding]
        out.sort(key=lambda e: -e['outstanding'])
        return out


def _customer_group(conn, customer: str) -> tuple:
    """Resolve a customer NAME to (customer_code, [all names sharing that code]).

    If the name has no associated customer_code in sales or logs, returns
    (None, [customer]) so behavior degrades to a single-name lookup.

    Used internally by _resolve_target as the name-based fallback path.
    """
    row = conn.execute("""
        SELECT customer_code FROM sales_transactions
        WHERE customer = ? AND customer_code IS NOT NULL
          AND TRIM(customer_code) != ''
        LIMIT 1
    """, (customer,)).fetchone()
    code = (row['customer_code'].strip() if row and row['customer_code'] else None)
    if not code:
        row = conn.execute("""
            SELECT customer_code FROM ar_followup_log
            WHERE customer = ? AND customer_code IS NOT NULL
              AND TRIM(customer_code) != ''
            LIMIT 1
        """, (customer,)).fetchone()
        code = (row['customer_code'].strip() if row and row['customer_code'] else None)
    if not code:
        return (None, [customer])
    name_rows = conn.execute("""
        SELECT DISTINCT customer FROM sales_transactions
        WHERE TRIM(customer_code) = ? AND customer IS NOT NULL AND customer != ''
        UNION
        SELECT DISTINCT customer FROM ar_followup_log
        WHERE TRIM(customer_code) = ? AND customer IS NOT NULL AND customer != ''
    """, (code, code)).fetchall()
    names = [r[0] for r in name_rows]
    if customer not in names:
        names.append(customer)
    return (code, names)


def _resolve_target(conn, target: str) -> tuple:
    """Resolve a URL/lookup target to (customer_code, [names_list]).

    `target` can be EITHER a customer_code (stable, preferred URL key) OR a
    customer name (legacy bookmark / orphan customer fallback). Code lookup
    wins when both interpretations are possible. Returns (None, [target])
    for unresolvable orphan strings so callers degrade to single-string
    lookup instead of erroring.

    Why: routing by customer_code keeps URLs stable across upstream name
    typo-fixes and disambiguates same-name / different-customer collisions
    (scrutinize findings 2 & 3, 2026-05-20).
    """
    if not target:
        return (None, [])
    target = target.strip()
    # Try as customer_code first.
    name_rows = conn.execute("""
        SELECT DISTINCT customer FROM sales_transactions
        WHERE TRIM(customer_code) = ? AND customer IS NOT NULL AND customer != ''
        UNION
        SELECT DISTINCT customer FROM ar_followup_log
        WHERE TRIM(customer_code) = ? AND customer IS NOT NULL AND customer != ''
    """, (target, target)).fetchall()
    if name_rows:
        return (target, [r[0] for r in name_rows])
    # Fall back to name-based resolution (legacy bookmarks / orphan customers).
    return _customer_group(conn, target)


def get_customer_ar_detail(customer: str,
                            conn: Optional[sqlite3.Connection] = None,
                            db_path: Optional[str] = None) -> List[dict]:
    """Outstanding invoices for one customer with age_days, sorted by age DESC.

    Each row carries everything `invoice_settlement` returns + age_days + vat_type.
    `customer` may be a customer_code OR a name — `_resolve_target` figures
    it out and pulls invoices across every name in the customer group.
    """
    with _ConnCtx(conn, db_path) as c:
        today = date.today()
        _, names = _resolve_target(c, customer)
        all_rows = []
        seen_docs = set()
        for n in names:
            for r in pa.invoice_settlement(customer=n, conn=c):
                if r['doc_base'] in seen_docs:
                    continue
                seen_docs.add(r['doc_base'])
                all_rows.append(r)
        out = []
        for r in all_rows:
            if r['outstanding'] <= 0.005:
                continue
            age = None
            if r['invoice_date']:
                age = (today - date.fromisoformat(r['invoice_date'])).days
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
    """All outreach rows for one customer, newest log_date first (id tiebreak).

    `customer` may be a customer_code OR a name — `_resolve_target` figures
    it out and pulls history across every name in the customer group.
    """
    with _ConnCtx(conn, db_path) as c:
        code, names = _resolve_target(c, customer)
        if code:
            placeholders = ','.join('?' * len(names))
            rows = c.execute(f"""
                SELECT * FROM ar_followup_log
                WHERE TRIM(customer_code) = ? OR customer IN ({placeholders})
                ORDER BY log_date DESC, id DESC
            """, (code, *names)).fetchall()
        else:
            rows = c.execute("""
                SELECT * FROM ar_followup_log
                WHERE customer = ?
                ORDER BY log_date DESC, id DESC
            """, (customer,)).fetchall()
        return [dict(r) for r in rows]


def list_overdue_followups(as_of: Optional[str] = None,
                            conn: Optional[sqlite3.Connection] = None,
                            db_path: Optional[str] = None) -> List[dict]:
    """Past-due outreach obligations per customer group.

    Two-CTE design:
      latest_with_action — the newest log per group that has a non-NULL
        next_action_date (this is the "current plan" the customer is on)
      latest_overall — the newest log per group, regardless of next_action_date
        (used to detect terminal state from a NULL-next-action follow-up)

    A customer appears in overdue when:
      - their `latest_with_action.next_action_date` <= `as_of`, AND
      - their `latest_overall.result` is NOT terminal (paid_full / closed)

    Why both CTEs: if staff logs "no_answer" with no follow-up date set, the
    prior past-due plan must stay visible (the debt is not resolved), but if
    the latest log is `paid_full` (terminal) with NULL next_action, the
    customer is closed and must not re-surface. The single-CTE / "rn=1 from
    all rows" approach silently dropped the first case (scrutinize finding 1,
    2026-05-20).
    """
    as_of = as_of or date.today().isoformat()
    with _ConnCtx(conn, db_path) as c:
        rows = c.execute("""
            WITH
            latest_overall AS (
                SELECT *,
                       ROW_NUMBER() OVER (
                         PARTITION BY COALESCE(NULLIF(TRIM(customer_code), ''), customer)
                         ORDER BY log_date DESC, id DESC
                       ) AS rn
                FROM ar_followup_log
            ),
            latest_with_action AS (
                SELECT *,
                       ROW_NUMBER() OVER (
                         PARTITION BY COALESCE(NULLIF(TRIM(customer_code), ''), customer)
                         ORDER BY log_date DESC, id DESC
                       ) AS rn_action
                FROM ar_followup_log
                WHERE next_action_date IS NOT NULL
            )
            SELECT la.*
            FROM latest_with_action la
            JOIN latest_overall lo
              ON COALESCE(NULLIF(TRIM(la.customer_code), ''), la.customer)
                 = COALESCE(NULLIF(TRIM(lo.customer_code), ''), lo.customer)
             AND lo.rn = 1
            WHERE la.rn_action = 1
              AND la.next_action_date <= ?
              AND lo.result NOT IN ('paid_full', 'closed')
            ORDER BY la.next_action_date ASC, la.id ASC
        """, (as_of,)).fetchall()
        return [{k: r[k] for k in r.keys() if k != 'rn_action'} for r in rows]
