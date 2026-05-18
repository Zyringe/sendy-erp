"""Pure payment-reconciliation + FIFO-allocation logic for Sendy ERP.

Read-only. NO routes, templates, schema changes, or DB writes (allocate_fifo
is a *planner* only — persistence is a future task).

Connection style mirrors hr.py / commission.py: own `_connect()` reading
`config.DATABASE_PATH`, every public function accepts an optional caller-
supplied `conn` (used, not closed) else opens/owns one.

Established join idiom (do NOT diverge — see models.get_payment_status /
get_payment_summary ≈ line 2183-2245):
    paid_invoices.iv_no  = sales_transactions.doc_base
    received_payments.id = paid_invoices.re_id
    received_payments.cancelled = 0     (cancelled receipts ignored)

LEGACY-NULL-AMOUNT RULE (real amounts win over legacy NULLs):
  `received_payments.total` and `paid_invoices.amount` were added in
  migration 058. Rows imported before 058 have `paid_invoices.amount IS
  NULL` — "linked but amount unknown". The pre-058 logic
  (get_payment_summary) treated *any* non-cancelled link as "this invoice
  is fully paid".

  Per-invoice resolution (priority order):
    1. has_real  → at least one non-cancelled link with amount IS NOT NULL
                   ⇒ collected = Σ real amounts (NULL legacy links on the
                   SAME invoice are ignored — a known real number is more
                   trustworthy than "unknown").
    2. has_null  → no real link but at least one non-cancelled NULL link
                   ⇒ collected = billed (pre-058 binary behaviour, so a
                   pure-legacy invoice still reads as fully paid).
    3. neither   → collected = 0 (no usable link).

  This means a real 0 amount is distinct from NULL, and a partial real
  payment that happens to coexist with a legacy NULL link no longer
  silently inflates to fully-paid.

Python 3.9 — Optional[...] not `X | None`.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Optional

from config import DATABASE_PATH

# Float-noise tolerance for "is this settled / overpaid".
_EPS = 0.005


# ── DB helpers ───────────────────────────────────────────────────────────────
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


# ── core settlement query ────────────────────────────────────────────────────
def _settlement_rows(conn, customer=None, date_from=None, date_to=None,
                      as_of=None):
    """Per-invoice billed/collected with the legacy-NULL rule applied.

    Aggregations computed per doc_base, then reconciled in Python:
      - billed         = ROUND(SUM(net), 2) over the invoice's lines
      - real_collected = SUM(paid_invoices.amount) over non-cancelled links
                         that have a non-NULL amount
      - has_real       = any non-cancelled link with amount IS NOT NULL
      - has_legacy     = any non-cancelled link with amount IS NULL
      - last_pay       = MAX(non-cancelled receipt date_iso)

    `as_of` (ISO date) — point-in-time AR: only invoice lines with
    date_iso <= as_of, and only payments with received_payments.date_iso
    <= as_of are considered.
    """
    sale_conds = ["st.doc_base IS NOT NULL",
                  "st.doc_base NOT LIKE 'SR%'",
                  "st.doc_base NOT LIKE 'HS%'"]
    sale_params = []
    if customer:
        sale_conds.append("st.customer = ?")
        sale_params.append(customer)
    if date_from:
        sale_conds.append("st.date_iso >= ?")
        sale_params.append(date_from)
    if date_to:
        sale_conds.append("st.date_iso <= ?")
        sale_params.append(date_to)
    if as_of:
        sale_conds.append("st.date_iso <= ?")
        sale_params.append(as_of)

    # Payment-side date cap is applied inside the correlated payment join
    # so a late receipt simply doesn't count yet (point-in-time AR).
    pay_date_cap = "AND rp.date_iso <= ?" if as_of else ""

    sql = f"""
        WITH inv AS (
            SELECT st.doc_base                       AS doc_base,
                   MIN(st.customer)                  AS customer,
                   MIN(st.customer_code)             AS customer_code,
                   MIN(st.date_iso)                  AS invoice_date,
                   ROUND(SUM(st.net), 2)             AS billed
            FROM sales_transactions st
            WHERE {' AND '.join(sale_conds)}
            GROUP BY st.doc_base
        ),
        pay AS (
            SELECT pi.iv_no                                  AS iv_no,
                   SUM(CASE WHEN pi.amount IS NOT NULL
                            THEN pi.amount ELSE 0 END)        AS real_collected,
                   MAX(CASE WHEN pi.amount IS NOT NULL
                            THEN 1 ELSE 0 END)                AS has_real,
                   MAX(CASE WHEN pi.amount IS NULL
                            THEN 1 ELSE 0 END)                AS has_legacy,
                   MAX(rp.date_iso)                           AS last_pay
            FROM paid_invoices pi
            JOIN received_payments rp
              ON rp.id = pi.re_id AND rp.cancelled = 0 {pay_date_cap}
            GROUP BY pi.iv_no
        )
        SELECT inv.doc_base,
               inv.customer,
               inv.customer_code,
               inv.invoice_date,
               inv.billed,
               COALESCE(pay.real_collected, 0.0) AS real_collected,
               COALESCE(pay.has_real, 0)         AS has_real,
               COALESCE(pay.has_legacy, 0)       AS has_legacy,
               pay.last_pay                       AS last_pay
        FROM inv
        LEFT JOIN pay ON pay.iv_no = inv.doc_base
    """
    params = list(sale_params)
    if as_of:
        params.append(as_of)
    return conn.execute(sql, params).fetchall()


def _reconcile(row):
    """Apply legacy-NULL rule + status classification to one raw row."""
    billed = round(row['billed'] or 0.0, 2)
    if row['has_real']:
        # Real amount(s) present — trust them; ignore any NULL legacy
        # link on this same invoice.
        collected = round(row['real_collected'] or 0.0, 2)
    elif row['has_legacy']:
        # Pure-legacy invoice: pre-058 binary behaviour — a NULL-amount
        # link ⇒ fully paid.
        collected = billed
    else:
        collected = 0.0

    outstanding = round(billed - collected, 2)
    # Clamp float noise around zero, but never hide a genuine negative
    # (overpaid) — that must surface as a flag.
    if abs(outstanding) < _EPS:
        outstanding = 0.0

    if collected - billed > _EPS:
        status = 'overpaid'
    elif collected <= 0:
        status = 'unpaid'
    elif outstanding <= _EPS:
        status = 'paid'
    else:
        status = 'partial'

    return {
        'doc_base': row['doc_base'],
        'customer': row['customer'],
        'customer_code': row['customer_code'],
        'invoice_date': row['invoice_date'],
        'billed': billed,
        'collected': collected,
        'outstanding': outstanding,
        'status': status,
        'last_payment_date': row['last_pay'],
    }


# ── shared cash-in attribution ───────────────────────────────────────────────
def cash_in_rows(conn=None, db_path=None, date_from=None, date_to=None):
    """Per-receipt-link cash attribution, applying the SAME per-invoice rule
    as `_reconcile` (real amounts win over legacy NULLs).

    For every invoice (iv_no = doc_base):
      • has_real  → emit one row per NON-cancelled NON-NULL link, attributed
                    to that receipt's month (multi-receipt / partial payments
                    each land in their own month). NULL legacy links on the
                    same invoice are ignored.
      • pure-legacy (no real link, ≥1 non-cancelled NULL link) → emit ONE row
                    of the full `billed`, attributed to the MAX non-cancelled
                    receipt month (so it's counted exactly once even if the
                    invoice is linked to several receipts).
      • neither   → nothing emitted.

    SCOPE: only links whose `iv_no` is a *billable* invoice (it has ≥1
    sales_transactions line with doc_base NOT NULL / NOT 'SR%' / NOT 'HS%')
    are emitted — identical to invoice_settlement's `inv` CTE. Receipt
    links pointing at a non-billable / never-imported iv_no are dropped so
    they cannot inflate cash beyond what settlement can attribute.

    GUARANTEE: Σ amount == Σ invoice_settlement().collected (within float
    noise) unconditionally, because both sides cover exactly the same set
    of billable invoices and each invoice's emitted total equals its
    `collected`.

    date_from / date_to filter on received_payments.date_iso.

    Returns list[{'month':'YYYY-MM', 're_id':int, 'amount':float}].
    """
    params = []
    pay_conds = ["rp.cancelled = 0"]
    if date_from:
        pay_conds.append("rp.date_iso >= ?")
        params.append(date_from)
    if date_to:
        pay_conds.append("rp.date_iso <= ?")
        params.append(date_to)
    where = " AND ".join(pay_conds)

    sale_filter = ("st2.doc_base IS NOT NULL "
                   "AND st2.doc_base NOT LIKE 'SR%' "
                   "AND st2.doc_base NOT LIKE 'HS%'")

    # Billable-invoice gate: iv_no must have ≥1 billable sales line, exactly
    # like invoice_settlement's `inv` CTE. Keeps the reconciliation exact.
    billable = (f"EXISTS (SELECT 1 FROM sales_transactions st2 "
                f"WHERE st2.doc_base = pi.iv_no AND {sale_filter})")

    # has_real per iv_no over the (date-filtered) non-cancelled link set.
    has_real_sql = f"""
        SELECT pi.iv_no AS iv_no,
               MAX(CASE WHEN pi.amount IS NOT NULL THEN 1 ELSE 0 END)
                                                   AS has_real
        FROM received_payments rp
        JOIN paid_invoices pi ON pi.re_id = rp.id
        WHERE {where}
        GROUP BY pi.iv_no
    """

    # Real links: one emitted row each, in their own receipt month.
    real_sql = f"""
        SELECT SUBSTR(rp.date_iso, 1, 7)          AS month,
               rp.id                              AS re_id,
               ROUND(COALESCE(pi.amount, 0.0), 2) AS amount
        FROM received_payments rp
        JOIN paid_invoices pi ON pi.re_id = rp.id
             AND pi.amount IS NOT NULL
        WHERE {where} AND {billable}
    """

    # Pure-legacy candidates: invoices that DO have a non-cancelled NULL
    # link. Whether they're really pure-legacy is decided in Python using
    # has_real (a real link anywhere ⇒ NOT pure-legacy).
    legacy_sql = f"""
        SELECT pi.iv_no                            AS iv_no,
               SUBSTR(MAX(rp.date_iso), 1, 7)      AS month,
               MAX(rp.id)                          AS re_id,
               ROUND(COALESCE(
                   (SELECT SUM(st2.net)
                    FROM sales_transactions st2
                    WHERE st2.doc_base = pi.iv_no
                      AND {sale_filter}
                   ), 0.0), 2)                     AS billed
        FROM received_payments rp
        JOIN paid_invoices pi ON pi.re_id = rp.id
             AND pi.amount IS NULL
        WHERE {where} AND {billable}
        GROUP BY pi.iv_no
    """

    with _ConnCtx(conn, db_path) as c:
        has_real = {r['iv_no']: r['has_real']
                    for r in c.execute(has_real_sql, params).fetchall()}
        real_rows = c.execute(real_sql, params).fetchall()
        legacy_rows = c.execute(legacy_sql, params).fetchall()

    out = []
    for r in real_rows:
        out.append({'month': r['month'],
                    're_id': r['re_id'],
                    'amount': round(float(r['amount']), 2)})
    for r in legacy_rows:
        if has_real.get(r['iv_no']):
            continue  # real link wins — legacy NULL ignored
        out.append({'month': r['month'],
                    're_id': r['re_id'],
                    'amount': round(float(r['billed']), 2)})
    return out


# ── 1. invoice_settlement ────────────────────────────────────────────────────
def invoice_settlement(customer=None, date_from=None, date_to=None,
                       conn=None, db_path=None):
    """Per-invoice settlement (read-only).

    Returns list[dict]: doc_base, customer, customer_code, invoice_date,
    billed, collected, outstanding, status, last_payment_date.
    """
    with _ConnCtx(conn, db_path) as c:
        raw = _settlement_rows(c, customer=customer,
                               date_from=date_from, date_to=date_to)
    out = [_reconcile(r) for r in raw]
    out.sort(key=lambda d: (d['invoice_date'] or '', d['doc_base']))
    return out


# ── 2. customer_outstanding ──────────────────────────────────────────────────
def customer_outstanding(as_of=None, conn=None, db_path=None):
    """Point-in-time AR per customer, sorted by outstanding desc.

    `as_of` (ISO date, default today): invoices with invoice_date <= as_of
    and payments with received_payments.date_iso <= as_of only.
    Returns list[dict]: customer, customer_code, billed, collected,
    outstanding, oldest_unpaid_date, open_invoices.
    """
    as_of = as_of or _today_iso()
    with _ConnCtx(conn, db_path) as c:
        raw = _settlement_rows(c, as_of=as_of)

    agg = {}
    for r in raw:
        inv = _reconcile(r)
        key = inv['customer']
        a = agg.get(key)
        if a is None:
            a = agg[key] = {
                'customer': inv['customer'],
                'customer_code': inv['customer_code'],
                'billed': 0.0,
                'collected': 0.0,
                'outstanding': 0.0,
                'oldest_unpaid_date': None,
                'open_invoices': 0,
            }
        a['billed'] = round(a['billed'] + inv['billed'], 2)
        a['collected'] = round(a['collected'] + inv['collected'], 2)
        a['outstanding'] = round(a['outstanding'] + inv['outstanding'], 2)
        if inv['outstanding'] > _EPS:
            a['open_invoices'] += 1
            d = inv['invoice_date']
            if d and (a['oldest_unpaid_date'] is None
                      or d < a['oldest_unpaid_date']):
                a['oldest_unpaid_date'] = d

    rows = list(agg.values())
    rows.sort(key=lambda d: (-d['outstanding'], d['customer'] or ''))
    return rows


# ── 3. allocate_fifo ─────────────────────────────────────────────────────────
def allocate_fifo(customer, payment_amount, as_of=None,
                  conn=None, db_path=None):
    """Plan (no DB writes) how a lump-sum payment clears a customer's
    oldest-outstanding invoices first.

    Determinism / tie-break: invoices ordered by invoice_date ASC, then
    doc_base ASC. Each invoice's `outstanding` is filled before moving on;
    the last touched invoice may be partially filled. Already-settled and
    overpaid invoices (outstanding <= _EPS) are skipped.

    Returns dict:
      allocations: [{doc_base, invoice_date, outstanding_before,
                     applied, outstanding_after}]
      unapplied:   leftover if payment exceeds total outstanding (>=0)
      total_applied: sum of applied
    """
    try:
        remaining = float(payment_amount)
    except (TypeError, ValueError):
        remaining = 0.0

    if remaining <= 0:
        return {'allocations': [], 'unapplied': 0.0, 'total_applied': 0.0}

    with _ConnCtx(conn, db_path) as c:
        raw = _settlement_rows(c, customer=customer, as_of=as_of)

    invoices = [_reconcile(r) for r in raw]
    # Only invoices that still owe something. Overpaid (negative
    # outstanding) and paid (~0) are excluded from FIFO targets.
    open_invs = [iv for iv in invoices if iv['outstanding'] > _EPS]
    open_invs.sort(key=lambda d: (d['invoice_date'] or '', d['doc_base']))

    allocations = []
    total_applied = 0.0
    for iv in open_invs:
        if remaining <= _EPS:
            break
        owed = iv['outstanding']
        applied = round(min(owed, remaining), 2)
        if applied <= 0:
            continue
        after = round(owed - applied, 2)
        if abs(after) < _EPS:
            after = 0.0
        allocations.append({
            'doc_base': iv['doc_base'],
            'invoice_date': iv['invoice_date'],
            'outstanding_before': owed,
            'applied': applied,
            'outstanding_after': after,
        })
        total_applied = round(total_applied + applied, 2)
        remaining = round(remaining - applied, 2)

    unapplied = round(remaining, 2)
    if abs(unapplied) < _EPS:
        unapplied = 0.0
    return {
        'allocations': allocations,
        'unapplied': unapplied,
        'total_applied': total_applied,
    }
