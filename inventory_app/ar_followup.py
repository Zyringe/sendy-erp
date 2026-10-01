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
- customer_ranking(...)           — ar_statement.customer_totals + last outreach
- resolve_customer_target(...)    — a customer CODE to the identity a log row stores
- get_customer_followups(...)     — outreach history for one code (newest first)
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

import ar_statement
import config
import payments_alloc as pa   # kept as diagnostic — do not remove

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


# ── ranking ─────────────────────────────────────────────────────────────────

def customer_ranking(conn: Optional[sqlite3.Connection] = None,
                     db_path: Optional[str] = None,
                     min_outstanding: float = 0.0) -> List[dict]:
    """`ar_statement.customer_totals()` (positive chaseable balances, largest
    first) with each customer's latest outreach joined on.

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
        agg = {(e['customer_code'] or e['customer']): e
               for e in ar_statement.customer_totals(conn=c)}

        # Attach last outreach (newest per group) if the log table exists.
        if _has_log_table(c):
            log_rows = c.execute("""
                SELECT
                  COALESCE(NULLIF(TRIM(customer_code), ''), customer) AS group_key,
                  MAX(log_date) AS last_log_date
                FROM ar_followup_log
                WHERE deleted_at IS NULL
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
                      AND deleted_at IS NULL
                    ORDER BY id DESC LIMIT 1
                """, (key, lr['last_log_date'])).fetchone()
                agg[key]['last_log_date'] = lr['last_log_date']
                agg[key]['last_log_result'] = detail['result'] if detail else None
                agg[key]['next_action_date'] = detail['next_action_date'] if detail else None

        for entry in agg.values():
            entry.setdefault('last_log_date', None)
            entry.setdefault('last_log_result', None)
            entry.setdefault('next_action_date', None)

        return [e for e in agg.values() if e['outstanding'] >= min_outstanding]


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
              AND TRIM(customer_code) != '' AND deleted_at IS NULL
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
          AND deleted_at IS NULL
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
          AND deleted_at IS NULL
        UNION
        SELECT DISTINCT customer_name FROM express_ar_outstanding
        WHERE TRIM(customer_code) = ? AND customer_name IS NOT NULL
          AND customer_name != ''
    """, (target, target, target)).fetchall()
    if name_rows:
        return (target, [r[0] for r in name_rows])
    # Fall back to name-based resolution (legacy bookmarks / orphan customers).
    return _customer_group(conn, target)


def resolve_customer_target(target: str,
                            conn: Optional[sqlite3.Connection] = None,
                            db_path: Optional[str] = None) -> Optional[dict]:
    """Resolve a customer CODE to the canonical identity to store on a log row.

    Returns ``{'customer_code': str, 'customer': str}``, or ``None`` when the
    key is not a code: no row in the snapshot, the sales ledger, the log or the
    customer master carries it as a code. A bare bill name is refused, so no
    log row is ever written without a code (ADR 0023). A code like 038ก01,
    with real AR rows and no name anywhere, is still a code (Put, 2026-08-15).

    Name preference: the CURRENT master name, else the newest snapshot name,
    else the newest surviving log name, else the code itself, so a
    since-corrected typo does not get re-frozen onto every new follow-up row.

    Callers resolve identity here and ignore any posted `customer` /
    `customer_code` fields.
    """
    code = (target or '').strip()
    if not code:
        return None
    with _ConnCtx(conn, db_path) as c:
        if not any(c.execute(sql, (code,)).fetchone() for sql in (
            "SELECT 1 FROM express_ar_outstanding WHERE TRIM(customer_code) = ? LIMIT 1",
            "SELECT 1 FROM sales_transactions WHERE TRIM(customer_code) = ? LIMIT 1",
            "SELECT 1 FROM ar_followup_log"
            " WHERE TRIM(customer_code) = ? AND deleted_at IS NULL LIMIT 1",
            "SELECT 1 FROM customers WHERE code = ? LIMIT 1",
        )):
            return None
        for sql in (
            "SELECT name FROM customers"
            " WHERE code = ? AND name IS NOT NULL AND TRIM(name) != ''",
            "SELECT customer_name FROM express_ar_outstanding"
            " WHERE TRIM(customer_code) = ? AND customer_name IS NOT NULL"
            "   AND TRIM(customer_name) != ''"
            " ORDER BY snapshot_date_iso DESC, id DESC LIMIT 1",
            "SELECT customer FROM ar_followup_log"
            " WHERE TRIM(customer_code) = ? AND deleted_at IS NULL"
            "   AND customer IS NOT NULL AND TRIM(customer) != ''"
            " ORDER BY log_date DESC, id DESC LIMIT 1",
            "SELECT customer FROM sales_transactions"
            " WHERE TRIM(customer_code) = ? AND customer IS NOT NULL"
            "   AND TRIM(customer) != '' LIMIT 1",
        ):
            row = c.execute(sql, (code,)).fetchone()
            if row:
                return {'customer_code': code, 'customer': row[0]}
        return {'customer_code': code, 'customer': code}


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
        # `deleted_at IS NULL` here too: editing a soft-deleted row would
        # silently revive collection history someone removed.
        c.execute(f"UPDATE ar_followup_log SET {', '.join(fields)}"
                  f" WHERE id = ? AND deleted_at IS NULL", params)
        if conn is None:
            c.commit()


def delete_outreach(log_id: int,
                    deleted_by: str,
                    conn: Optional[sqlite3.Connection] = None,
                    db_path: Optional[str] = None) -> bool:
    """SOFT-delete one outreach row; returns True only if a row changed.

    A promise-to-pay is collection evidence — a hard DELETE left no trace of
    what was removed or by whom (mig 160). Same pattern as
    call_card.soft_delete_log. Every reader filters `deleted_at IS NULL`, so
    the row disappears from the UI while staying auditable in SQL.

    Returns False for an already-deleted or nonexistent id so the caller can
    avoid flashing success over a no-op.
    """
    with _ConnCtx(conn, db_path) as c:
        cur = c.execute(
            """UPDATE ar_followup_log
                  SET deleted_at = datetime('now','localtime'), deleted_by = ?
                WHERE id = ? AND deleted_at IS NULL""",
            (deleted_by, log_id),
        )
        if conn is None:
            c.commit()
        return cur.rowcount == 1


def get_customer_followups(customer_code: str,
                           conn: Optional[sqlite3.Connection] = None,
                           db_path: Optional[str] = None) -> List[dict]:
    """All outreach rows for one customer code, newest log_date first (id tiebreak)."""
    with _ConnCtx(conn, db_path) as c:
        rows = c.execute("""
            SELECT * FROM ar_followup_log
            WHERE TRIM(customer_code) = ? AND deleted_at IS NULL
            ORDER BY log_date DESC, id DESC
        """, ((customer_code or '').strip(),)).fetchall()
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
                WHERE deleted_at IS NULL
            ),
            latest_with_action AS (
                SELECT *,
                       ROW_NUMBER() OVER (
                         PARTITION BY COALESCE(NULLIF(TRIM(customer_code), ''), customer)
                         ORDER BY log_date DESC, id DESC
                       ) AS rn_action
                FROM ar_followup_log
                WHERE next_action_date IS NOT NULL
                  AND deleted_at IS NULL
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
