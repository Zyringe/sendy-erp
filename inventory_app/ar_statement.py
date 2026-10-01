"""Chaseable BSN AR: the one module that answers "what does this customer owe".

Every chase-facing page reads its money through here (ADR 0023): the customer
page, `/m/customer`, `/express/ar/customer`, the dunning detail, the call card,
the `/call` badges, the sales trip and the `/ar` overview. Each page sorts the
rows in its own view; none of them re-types the population.

Source: `express_ar_outstanding` at the latest BSN snapshot, filtered by
`BSN_AR_PREDICATE` (ADR 0012). Ages count from the snapshot date on every page.

Connection style mirrors `ar_followup`: `config.DATABASE_PATH` is read lazily,
so `tests/conftest.py::tmp_db` needs no patch entry for this module.
"""
from datetime import date
from typing import List, Optional, Sequence
import sqlite3

import config


# The chaseable population (ADR 0012). Bare column names: only
# express_ar_outstanding has them, so filter it FIRST and join second wherever
# another table carrying `doc_no` or `entity` joins in.
# LOAD-BEARING: ar_writeoffs.doc_no must stay NOT NULL (mig 095). One NULL makes
# `doc_no NOT IN (...)` NULL for every row and chaseable AR collapses to 0.
BSN_AR_PREDICATE = (
    "is_anomalous = 0 AND doc_date_iso >= '2024-01-01' "
    "AND doc_no NOT IN (SELECT doc_no FROM ar_writeoffs)"
)

# Express exports the AR snapshot about daily, so anything older than one day is
# not safe to chase with. Not the DBF badge's 26-hour rule: that import carries
# no AR snapshot.
AR_SNAPSHOT_STALE_AFTER_DAYS = 1

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


def _snapshot_date(conn) -> Optional[str]:
    return conn.execute(
        "SELECT MAX(snapshot_date_iso) AS d FROM express_ar_outstanding"
        " WHERE entity = 'BSN'").fetchone()['d']


def _age_days(snap: str, doc_date_iso: Optional[str]) -> Optional[int]:
    if not doc_date_iso:
        return None
    try:
        return max((date.fromisoformat(snap) - date.fromisoformat(doc_date_iso)).days, 0)
    except (ValueError, TypeError):
        return None


def _bucket_of(age: int) -> str:
    if age <= 30:
        return '0-30'
    if age <= 60:
        return '31-60'
    if age <= 90:
        return '61-90'
    return '90+'


def freshness(as_of: Optional[str] = None,
              conn: Optional[sqlite3.Connection] = None,
              db_path: Optional[str] = None) -> dict:
    """How old the snapshot is on the observation date `as_of` (default today).

    The keys `_ar_snapshot_banner.html` reads, plus `snapshot_date`. `as_of` in
    the result is the snapshot date, or the observation date when there is no
    snapshot. `age_days` is signed and never clamped: a future-dated snapshot is
    a data error, and clamping would show it as the freshest possible verdict.
    """
    with _ConnCtx(conn, db_path) as c:
        snap = _snapshot_date(c)
    observed = as_of or date.today().isoformat()
    age = (None if not snap
           else (date.fromisoformat(observed) - date.fromisoformat(snap)).days)
    return {
        'as_of': snap or observed,
        'snapshot_date': snap,
        'age_days': age,
        'is_stale': age is None or age < 0 or age > AR_SNAPSHOT_STALE_AFTER_DAYS,
        'stale_after_days': AR_SNAPSHOT_STALE_AFTER_DAYS,
    }


def _chaseable_rows(c, snap: str, match_sql: str, params: Sequence) -> List[dict]:
    rows = c.execute(f"""
        SELECT ao.doc_no,
               ao.doc_date_iso,
               COALESCE(cust.name, ao.customer_name) AS customer,
               ao.customer_name,
               ao.customer_code,
               ao.customer_type,
               ao.salesperson_code,
               ao.bill_amount,
               ao.paid_amount,
               ao.outstanding_amount AS outstanding,
               ao.is_anomalous,
               ao.has_warning
          FROM (SELECT * FROM express_ar_outstanding
                 WHERE entity = 'BSN' AND snapshot_date_iso = ?
                   AND {BSN_AR_PREDICATE}) ao
          LEFT JOIN customers cust ON cust.code = ao.customer_code
         WHERE {match_sql}
         ORDER BY ao.id
    """, [snap, *params]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d['bill_amount'] = round(float(d['bill_amount'] or 0), 2)
        d['paid_amount'] = round(float(d['paid_amount'] or 0), 2)
        d['outstanding'] = round(float(d['outstanding'] or 0), 2)
        d['age_days'] = _age_days(snap, d['doc_date_iso'])
        out.append(d)
    return out


def _excluded_docs(c, snap: str, match_sql: str, params: Sequence) -> List[dict]:
    """Snapshot rows for one customer that are NOT chaseable, and why.

    `excluded_by` is one of 're' (is_anomalous = 1, any date), 'legacy'
    (pre-2024) or 'writeoff' (in ar_writeoffs), the same buckets as
    `cashflow.bsn_ar_excluded()`, so a doc both written off and pre-2024 counts
    once, as legacy. The write-off metadata rides along whenever a write-off row
    exists. The WHERE is `NOT (BSN_AR_PREDICATE)`: a hand-typed complement could
    drift and let a document fall out of both lists. Not filtered on
    `outstanding_amount > 0`, so the partition with the chaseable rows holds.
    The ar_writeoffs join cannot fan out: that table is UNIQUE(doc_no).
    """
    rows = c.execute(f"""
        SELECT ao.doc_no,
               ao.doc_date_iso,
               COALESCE(cust.name, ao.customer_name) AS customer,
               ao.customer_name,
               ao.customer_code,
               ao.customer_type,
               ao.salesperson_code,
               ao.bill_amount,
               ao.paid_amount,
               ao.outstanding_amount                AS outstanding,
               CASE WHEN ao.is_anomalous = 1            THEN 're'
                    WHEN ao.doc_date_iso < '2024-01-01' THEN 'legacy'
                    ELSE 'writeoff' END              AS excluded_by,
               w.type                               AS writeoff_type,
               w.writeoff_date                      AS writeoff_date,
               w.reason                             AS writeoff_reason
          FROM (SELECT * FROM express_ar_outstanding
                 WHERE entity = 'BSN' AND snapshot_date_iso = ?
                   AND NOT ({BSN_AR_PREDICATE})) ao
          LEFT JOIN customers cust ON cust.code = ao.customer_code
          LEFT JOIN ar_writeoffs w ON w.doc_no = ao.doc_no
         WHERE {match_sql}
         ORDER BY ao.doc_date_iso DESC
    """, [snap, *params]).fetchall()
    return [dict(r) for r in rows]


def customer_statement(code: str,
                       conn: Optional[sqlite3.Connection] = None,
                       db_path: Optional[str] = None) -> dict:
    """One customer's chaseable AR, keyed by customer code only.

    Returns:
      customer_code  the code asked for, stripped
      snapshot_date  latest BSN snapshot date, or None
      chaseable      every chaseable row, in snapshot order: doc_no,
                     doc_date_iso, customer, customer_name, customer_code,
                     customer_type, salesperson_code, bill_amount, paid_amount,
                     outstanding, is_anomalous, has_warning, age_days
      bills          the chaseable rows with outstanding > 0. A bill LIST drops
                     credit rows on purpose (ADR 0012); the total does not.
      excluded       the rest of the customer's snapshot rows, with excluded_by
      total          sum of chaseable outstanding, credits included
      freshness      freshness() today

    The code matches `TRIM(customer_code)`, so a padded code in the snapshot
    lands on both lists or neither.
    """
    code = (code or '').strip()
    with _ConnCtx(conn, db_path) as c:
        fresh = freshness(conn=c)
        snap = fresh['snapshot_date']
        if not snap or not code:
            chaseable, excluded = [], []
        else:
            match = "TRIM(ao.customer_code) = ?"
            chaseable = _chaseable_rows(c, snap, match, [code])
            excluded = _excluded_docs(c, snap, match, [code])
    return {
        'customer_code': code,
        'snapshot_date': snap,
        'chaseable': chaseable,
        'bills': [r for r in chaseable if r['outstanding'] > 0],
        'excluded': excluded,
        'total': round(sum(r['outstanding'] for r in chaseable), 2),
        'freshness': fresh,
    }


def customer_totals(conn: Optional[sqlite3.Connection] = None,
                    db_path: Optional[str] = None) -> List[dict]:
    """Chaseable AR per customer, largest first, positive balances only.

    Keyed by the trimmed code. A row with a blank code groups under its name
    and carries `customer_code` None, so it still shows on /ar without a link.
    A customer's balance is the NET of its rows: Express lists unapplied credits
    as negative rows, and dropping them would overstate mixed customers.

    Each row: customer, customer_code, invoice_count, outstanding,
    oldest_age_days, age_buckets {'0-30', '31-60', '61-90', '90+'}.
    """
    with _ConnCtx(conn, db_path) as c:
        snap = _snapshot_date(c)
        if not snap:
            return []
        rows = _chaseable_rows(c, snap, "1 = 1", [])

    agg = {}
    for r in rows:
        code = (r['customer_code'] or '').strip()
        name = r['customer'] or ''
        entry = agg.setdefault(code or name, {
            'customer': name,
            'customer_code': code or None,
            'invoice_count': 0,
            'outstanding': 0.0,
            'oldest_age_days': 0,
            'age_buckets': {b: 0.0 for b in _AGE_BUCKETS},
        })
        entry['invoice_count'] += 1
        amt = r['outstanding']
        entry['outstanding'] = round(entry['outstanding'] + amt, 2)
        age = r['age_days']
        if age is not None:
            entry['oldest_age_days'] = max(entry['oldest_age_days'], age)
            b = _bucket_of(age)
            entry['age_buckets'][b] = round(entry['age_buckets'][b] + amt, 2)

    out = [e for e in agg.values() if e['outstanding'] > 0.005]
    out.sort(key=lambda e: -e['outstanding'])
    return out

