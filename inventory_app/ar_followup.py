"""AR follow-up workspace logic for Sendy ERP.

Drives /accounting/ar-followup — a ranked workspace for chasing unpaid
invoices.

AR SOURCE (Express-authoritative, 2026-05-29):
  BSN AR outstanding numbers come from `express_ar_outstanding` filtered to
  entity='BSN' at the latest snapshot_date_iso. Express is the system of
  record; Sendy's derived engine (payments_alloc.invoice_settlement) is kept
  as a DIAGNOSTIC only — do not call it for ranking or detail reads.

  Aging = days from doc_date_iso to the snapshot date (point-in-time; not
  from today, so the numbers match what Express published on that date).

  JOIN to `customers` by customer_code for contact/zone/phone display. When
  a snapshot customer_code has no customers row, fall back to the snapshot's
  customer_name — the row is NOT dropped.

Outreach workspace:
  Outreach attempts persist to `ar_followup_log` (migration 065). Keyed by
  customer_code (stable) where available, else by name. Unchanged by the
  source switch.

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
import payments_alloc as pa   # kept as diagnostic — do not remove


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


def _bsn_snapshot_date(conn) -> Optional[str]:
    """Return the latest snapshot_date_iso for BSN, or None if table is empty."""
    row = conn.execute(
        "SELECT MAX(snapshot_date_iso) AS snap FROM express_ar_outstanding"
        " WHERE entity='BSN'"
    ).fetchone()
    return row['snap'] if row else None


# ── ranking ─────────────────────────────────────────────────────────────────

def customer_ranking(conn: Optional[sqlite3.Connection] = None,
                     db_path: Optional[str] = None,
                     min_outstanding: float = 0.0) -> List[dict]:
    """Per-customer outstanding roll-up sourced from Express BSN snapshot.

    Aging is computed from doc_date_iso to the snapshot date (point-in-time,
    matches Express's own published date — not from today).

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
        snap = _bsn_snapshot_date(c)
        if not snap:
            return []

        snap_date = date.fromisoformat(snap)

        rows = c.execute("""
            SELECT
                ao.customer_code,
                COALESCE(cust.name, ao.customer_name) AS customer_name,
                ao.doc_date_iso,
                ao.outstanding_amount
            FROM express_ar_outstanding ao
            LEFT JOIN customers cust ON cust.code = ao.customer_code
            WHERE ao.entity = 'BSN'
              AND ao.snapshot_date_iso = ?
        """, (snap,)).fetchall()
        # NB: do NOT filter `outstanding_amount > 0` at the row level. Express
        # lists un-applied credit notes / overpayments as separate NEGATIVE
        # rows; a customer's true chaseable balance is the NET across all their
        # rows (matches Express's own per-customer subtotal). Filtering rows
        # would overstate mixed customers — e.g. ทรงพลเทรดดิ้ง would show
        # ฿284,863 gross instead of ฿164,323 net (฿120,540 of credits ignored).

        agg = {}
        for r in rows:
            code = (r['customer_code'] or '').strip()
            name = r['customer_name'] or ''
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
            amt = round(float(r['outstanding_amount'] or 0), 2)
            entry['outstanding'] = round(entry['outstanding'] + amt, 2)

            if r['doc_date_iso']:
                try:
                    age = (snap_date - date.fromisoformat(r['doc_date_iso'])).days
                except (ValueError, TypeError):
                    age = 0
                age = max(age, 0)
                if age > entry['oldest_age_days']:
                    entry['oldest_age_days'] = age
                bucket = _bucket_of(age)
                entry['age_buckets'][bucket] = round(
                    entry['age_buckets'][bucket] + amt, 2
                )

        # Attach last outreach (newest per group) if the log table exists.
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

        # Only customers whose NET balance is positive owe us money (net-zero
        # or net-credit customers are not chased).
        out = [e for e in agg.values()
               if e['outstanding'] > 0.005 and e['outstanding'] >= min_outstanding]
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
        # Try to find code from express_ar_outstanding snapshot
        row = conn.execute("""
            SELECT customer_code FROM express_ar_outstanding
            WHERE customer_name = ? AND customer_code IS NOT NULL
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
        UNION
        SELECT DISTINCT customer_name FROM express_ar_outstanding
        WHERE TRIM(customer_code) = ? AND customer_name IS NOT NULL AND customer_name != ''
    """, (code, code, code)).fetchall()
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
        UNION
        SELECT DISTINCT customer_name FROM express_ar_outstanding
        WHERE TRIM(customer_code) = ? AND customer_name IS NOT NULL
          AND customer_name != ''
    """, (target, target, target)).fetchall()
    if name_rows:
        return (target, [r[0] for r in name_rows])
    # Fall back to name-based resolution (legacy bookmarks / orphan customers).
    return _customer_group(conn, target)


def get_customer_ar_detail(customer: str,
                            conn: Optional[sqlite3.Connection] = None,
                            db_path: Optional[str] = None) -> List[dict]:
    """Outstanding invoices for one customer from the Express BSN snapshot.

    Returns per-invoice rows sorted by age DESC (oldest first). Each row:
      doc_no, doc_date_iso (= invoice_date), customer, customer_code,
      outstanding, bill_amount, paid_amount, age_days, salesperson_code.

    `customer` may be a customer_code OR a name — `_resolve_target` figures
    it out and pulls invoices across every name in the customer group.
    """
    with _ConnCtx(conn, db_path) as c:
        snap = _bsn_snapshot_date(c)
        if not snap:
            return []
        snap_date = date.fromisoformat(snap)
        code, _names = _resolve_target(c, customer)

        if code:
            rows = c.execute("""
                SELECT
                    ao.doc_no,
                    ao.doc_date_iso,
                    COALESCE(cust.name, ao.customer_name) AS customer,
                    ao.customer_code,
                    ao.bill_amount,
                    ao.paid_amount,
                    ao.outstanding_amount AS outstanding,
                    ao.salesperson_code
                FROM express_ar_outstanding ao
                LEFT JOIN customers cust ON cust.code = ao.customer_code
                WHERE ao.entity = 'BSN'
                  AND ao.snapshot_date_iso = ?
                  AND TRIM(ao.customer_code) = ?
            """, (snap, code)).fetchall()
        else:
            # Orphan / walk-in: match by customer_name
            target_name = customer.strip()
            rows = c.execute("""
                SELECT
                    ao.doc_no,
                    ao.doc_date_iso,
                    ao.customer_name AS customer,
                    ao.customer_code,
                    ao.bill_amount,
                    ao.paid_amount,
                    ao.outstanding_amount AS outstanding,
                    ao.salesperson_code
                FROM express_ar_outstanding ao
                WHERE ao.entity = 'BSN'
                  AND ao.snapshot_date_iso = ?
                  AND ao.customer_name = ?
            """, (snap, target_name)).fetchall()

        out = []
        for r in rows:
            age = None
            if r['doc_date_iso']:
                try:
                    age = (snap_date - date.fromisoformat(r['doc_date_iso'])).days
                    age = max(age, 0)
                except (ValueError, TypeError):
                    age = None
            out.append({
                'doc_no': r['doc_no'],
                'doc_base': r['doc_no'],        # alias kept for template compat
                'invoice_date': r['doc_date_iso'],
                'customer': r['customer'],
                'customer_code': r['customer_code'],
                'bill_amount': round(float(r['bill_amount'] or 0), 2),
                'paid_amount': round(float(r['paid_amount'] or 0), 2),
                'outstanding': round(float(r['outstanding'] or 0), 2),
                'age_days': age,
                'salesperson_code': r['salesperson_code'],
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
