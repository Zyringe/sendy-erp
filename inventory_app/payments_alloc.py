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

CREDIT-NOTE (SR) NETTING — AUTHORITATIVE AMOUNT (migration 062):
  Sales-return / credit-note SR docs reduce what the customer owes on the
  referenced invoice. The credited value is the ใบลดหนี้ master
  "รวมทั้งสิ้น" (post-doc-discount, post-VAT-policy), cached per SR
  doc_base in `credit_note_amounts` by import_credit_notes. The `cn` CTE:

    • cn_auth : credit_note_amounts.credited_amount, grouped by
                ref_invoice = inv.doc_base  (AUTHORITATIVE).
    • cn_fb   : LEGACY FALLBACK — SUM(sales_transactions.net) over SR rows
                whose doc_base is ABSENT from credit_note_amounts (SR not
                yet imported from the standalone ใบลดหนี้ file). A given SR
                is counted on exactly ONE side, never both.
    • credit_notes = cn_auth + cn_fb summed per invoice.

  The SR detail-line `net` in sales_transactions is PRE-doc-discount and
  over-credits the invoice (e.g. SR6900009 detail net 2340.00 vs master
  2293.20). Using the authoritative cached value is what removes the false
  "overpaid" customer-credit balance.

      net_owed = round(billed - credit_notes, 2)

  collected NETS the SR(-) receipt links: a receipt that applied a credit
  note carries an SR row in paid_invoices with a NEGATIVE amount and
  iv_no = SR doc_base. Those are re-attributed to the original invoice via
  credit_note_amounts.ref_invoice, so:

      collected = Σ IV(+) receipt links  −  Σ SR(-) receipt links

  NO DOUBLE COUNT: the SR reduces net_owed (via `cn`) AND reduces collected
  (via the SR(-) receipt link) by the same credited amount, so
  outstanding = net_owed − collected is unchanged by the credit's
  magnitude when the receipt actually applied it. Settlement is measured
  against net_owed, NOT billed:
    • real-amount invoices  → collected = Σ real (IV(+) − SR(-))
    • pure-legacy invoices  → collected = net_owed   (a pre-058 NULL link
      means "this invoice is settled"; with a credit note that means the
      *post-credit* balance is settled, so collected = net_owed, not billed)
    • outstanding = round(net_owed - collected, 2)

  SR rows with ref_invoice NULL or '' are unattributable and netted
  against nothing (see unattributable_sr_count()). A credit note whose
  ref_invoice matches no billable invoice simply has no invoice to reduce.

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


def _has_cna(conn) -> bool:
    """True when credit_note_amounts (migration 062) exists in this DB.

    The pytest schema-clone / pre-062 live snapshots may not have it yet;
    when absent we transparently fall back to the legacy SR.net-sum `cn`
    and skip SR(-) receipt re-attribution, so behaviour is byte-identical
    to pre-062 for those databases (existing fixtures stay green).
    """
    return conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='credit_note_amounts'"
    ).fetchone() is not None


# ── core settlement query ────────────────────────────────────────────────────
def _settlement_rows(conn, customer=None, date_from=None, date_to=None,
                      as_of=None):
    """Per-invoice billed/collected with the legacy-NULL rule applied.

    Aggregations computed per doc_base, then reconciled in Python:
      - billed         = ROUND(SUM(CASE WHEN vat_type=2 THEN net*1.07
                         ELSE net END), 2) — what the customer OWES/PAYS
                         ("แยก VAT" lines carry 7% output VAT; net itself
                         stays the ex-VAT revenue figure elsewhere)
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

    has_cna = _has_cna(conn)

    if has_cna:
        # AUTHORITATIVE credited amount = credit_note_amounts.credited_amount
        # (migration 062 — the ใบลดหนี้ master "รวมทั้งสิ้น",
        # post-doc-discount/VAT-policy). The sales_transactions SR.net sum is
        # kept ONLY as a fallback for SR docs ABSENT from
        # credit_note_amounts (legacy / SR not yet imported). A given SR is
        # counted on exactly ONE side, never both.
        cn_cte = """
        sr_fallback AS (
            SELECT st.ref_invoice              AS iv_no,
                   st.doc_base                 AS sr_doc_base,
                   ROUND(SUM(st.net), 2)       AS sr_net
            FROM sales_transactions st
            WHERE st.doc_base LIKE 'SR%'
              AND st.ref_invoice IS NOT NULL
              AND st.ref_invoice <> ''
              AND st.doc_base NOT IN (SELECT sr_doc_base FROM credit_note_amounts)
            GROUP BY st.ref_invoice, st.doc_base
        ),
        cn AS (
            SELECT iv_no,
                   ROUND(SUM(credit_notes), 2) AS credit_notes
            FROM (
                SELECT ref_invoice AS iv_no,
                       credited_amount AS credit_notes
                FROM credit_note_amounts
                WHERE ref_invoice IS NOT NULL AND ref_invoice <> ''
                UNION ALL
                SELECT iv_no, sr_net AS credit_notes FROM sr_fallback
            )
            GROUP BY iv_no
        ),"""
        pay_cte = f"""
        pay AS (
            -- collected nets the SR(-) receipt links: a receipt that applied
            -- a credit note carries an SR row in paid_invoices with a
            -- NEGATIVE amount and iv_no = SR doc_base. Re-attributed to the
            -- ORIGINAL invoice via credit_note_amounts.ref_invoice so it
            -- reduces that invoice's collected, symmetrically with how `cn`
            -- reduces its net_owed (no double count).
            SELECT iv_no,
                   ROUND(SUM(real_amt), 2)              AS real_collected,
                   MAX(has_real)                        AS has_real,
                   MAX(has_legacy)                      AS has_legacy,
                   MAX(last_pay)                        AS last_pay
            FROM (
                SELECT pi.iv_no                                  AS iv_no,
                       CASE WHEN pi.amount IS NOT NULL
                            THEN pi.amount ELSE 0 END             AS real_amt,
                       CASE WHEN pi.amount IS NOT NULL
                            THEN 1 ELSE 0 END                     AS has_real,
                       CASE WHEN pi.amount IS NULL
                            THEN 1 ELSE 0 END                     AS has_legacy,
                       rp.date_iso                                AS last_pay
                FROM paid_invoices pi
                JOIN received_payments rp
                  ON rp.id = pi.re_id AND rp.cancelled = 0 {pay_date_cap}
                WHERE pi.iv_no NOT LIKE 'SR%'

                UNION ALL

                SELECT srref.ref_invoice                         AS iv_no,
                       CASE WHEN pi.amount IS NOT NULL
                            THEN pi.amount ELSE 0 END             AS real_amt,
                       0                                          AS has_real,
                       0                                          AS has_legacy,
                       NULL                                       AS last_pay
                FROM paid_invoices pi
                JOIN received_payments rp
                  ON rp.id = pi.re_id AND rp.cancelled = 0 {pay_date_cap}
                JOIN (
                    -- Resolve the SR doc_base → original invoice ref.
                    -- AUTHORITATIVE: credit_note_amounts.ref_invoice.
                    -- FALLBACK: the SR's own sales_transactions.ref_invoice
                    -- when the SR is not yet cached (mirrors `cn`'s
                    -- auth-then-fallback so collected nets symmetrically
                    -- with net_owed and Σ cash_in stays == Σ collected).
                    SELECT sr_doc_base AS sr_doc_base,
                           ref_invoice AS ref_invoice
                    FROM credit_note_amounts
                    WHERE ref_invoice IS NOT NULL AND ref_invoice <> ''
                    UNION
                    SELECT st.doc_base AS sr_doc_base,
                           st.ref_invoice AS ref_invoice
                    FROM sales_transactions st
                    WHERE st.doc_base LIKE 'SR%'
                      AND st.ref_invoice IS NOT NULL
                      AND st.ref_invoice <> ''
                      AND st.doc_base NOT IN
                          (SELECT sr_doc_base FROM credit_note_amounts)
                ) srref
                  ON srref.sr_doc_base = pi.iv_no
                WHERE pi.iv_no LIKE 'SR%'
            )
            GROUP BY iv_no
        )"""
    else:
        # Pre-062 legacy path (schema-clone / old snapshots): byte-identical
        # to the original behaviour — cn = SR.net sum, no SR(-) netting.
        cn_cte = """
        cn AS (
            SELECT ref_invoice                AS iv_no,
                   ROUND(SUM(net), 2)         AS credit_notes
            FROM sales_transactions
            WHERE doc_base LIKE 'SR%'
              AND ref_invoice IS NOT NULL
              AND ref_invoice <> ''
            GROUP BY ref_invoice
        ),"""
        pay_cte = f"""
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
        )"""

    sql = f"""
        WITH inv AS (
            SELECT st.doc_base                       AS doc_base,
                   MIN(st.customer)                  AS customer,
                   MIN(st.customer_code)             AS customer_code,
                   MIN(st.date_iso)                  AS invoice_date,
                   -- VAT-aware: vat_type=2 ("แยก VAT") bills the customer
                   -- net + 7% output VAT, so what they OWE/PAY is net*1.07.
                   -- Same per-line idiom as models.py / test_vat_math.py.
                   -- (Revenue stays ex-VAT — see cashflow.revenue_by_month.)
                   ROUND(SUM(CASE WHEN st.vat_type = 2
                                  THEN st.net * 1.07 ELSE st.net END), 2)
                                                     AS billed
            FROM sales_transactions st
            WHERE {' AND '.join(sale_conds)}
            GROUP BY st.doc_base
        ),{cn_cte}{pay_cte}
        SELECT inv.doc_base,
               inv.customer,
               inv.customer_code,
               inv.invoice_date,
               inv.billed,
               COALESCE(cn.credit_notes, 0.0)    AS credit_notes,
               COALESCE(pay.real_collected, 0.0) AS real_collected,
               COALESCE(pay.has_real, 0)         AS has_real,
               COALESCE(pay.has_legacy, 0)       AS has_legacy,
               pay.last_pay                       AS last_pay
        FROM inv
        LEFT JOIN cn  ON cn.iv_no  = inv.doc_base
        LEFT JOIN pay ON pay.iv_no = inv.doc_base
    """
    params = list(sale_params)
    if as_of:
        # One bind per rendered `{pay_date_cap}` placeholder. The legacy
        # `pay` CTE renders it once; the CNA-aware `pay` CTE renders it
        # twice (IV branch + SR branch). Count, don't hardcode.
        params.extend([as_of] * pay_cte.count("rp.date_iso <= ?"))
    return conn.execute(sql, params).fetchall()


def _reconcile(row):
    """Apply legacy-NULL rule + credit-note netting + status to one row."""
    billed = round(row['billed'] or 0.0, 2)
    credit_notes = round(row['credit_notes'] or 0.0, 2)
    # What the customer actually owes after credit notes net down the bill.
    net_owed = round(billed - credit_notes, 2)

    if row['has_real']:
        # Real amount(s) present — trust them; ignore any NULL legacy
        # link on this same invoice.
        collected = round(row['real_collected'] or 0.0, 2)
    elif row['has_legacy']:
        # Pure-legacy invoice: pre-058 binary behaviour — a NULL-amount
        # link ⇒ the *post-credit* balance is settled, so it collects
        # net_owed (NOT billed; the credit note never was cash to collect).
        collected = net_owed
    else:
        collected = 0.0

    outstanding = round(net_owed - collected, 2)
    # Clamp float noise around zero, but never hide a genuine negative
    # (overpaid) — that must surface as a flag.
    if abs(outstanding) < _EPS:
        outstanding = 0.0

    if collected - net_owed > _EPS:
        status = 'overpaid'
    elif net_owed <= _EPS and credit_notes > _EPS and collected <= _EPS:
        # Bill fully wiped by credit notes, nothing collected.
        status = 'fully_credited'
    elif collected <= 0:
        status = 'unpaid'
    elif outstanding <= _EPS:
        status = 'paid'
    else:
        status = 'partial'

    # Data-quality flag: credited beyond what was billed (broken SR ref or
    # over-issued credit note). net_owed goes negative in that case.
    over_credited = credit_notes > billed + _EPS

    return {
        'doc_base': row['doc_base'],
        'customer': row['customer'],
        'customer_code': row['customer_code'],
        'invoice_date': row['invoice_date'],
        'billed': billed,
        'credit_notes': credit_notes,
        'net_owed': net_owed,
        'collected': collected,
        'outstanding': outstanding,
        'status': status,
        'over_credited': over_credited,
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
    # POSITIVE IV links only here; the SR(-) netting links are emitted by
    # sr_link_sql below so Σ cash_in stays == Σ collected.
    real_sql = f"""
        SELECT SUBSTR(rp.date_iso, 1, 7)          AS month,
               rp.id                              AS re_id,
               ROUND(COALESCE(pi.amount, 0.0), 2) AS amount
        FROM received_payments rp
        JOIN paid_invoices pi ON pi.re_id = rp.id
             AND pi.amount IS NOT NULL
        WHERE {where} AND {billable}
          AND pi.iv_no NOT LIKE 'SR%'
    """

    # SR(-) receipt links re-attributed to the ORIGINAL invoice via
    # credit_note_amounts.ref_invoice. amount is negative → subtracts cash in
    # the receipt's own month. Gated by `billable` on the ORIGINAL invoice
    # (cna.ref_invoice), exactly mirroring the `pay` CTE so the per-invoice
    # collected and the per-receipt cash attribution net identically.
    sr_billable = (f"EXISTS (SELECT 1 FROM sales_transactions st2 "
                   f"WHERE st2.doc_base = srref.ref_invoice AND {sale_filter})")
    sr_link_sql = f"""
        SELECT SUBSTR(rp.date_iso, 1, 7)          AS month,
               rp.id                              AS re_id,
               ROUND(COALESCE(pi.amount, 0.0), 2) AS amount
        FROM received_payments rp
        JOIN paid_invoices pi ON pi.re_id = rp.id
             AND pi.amount IS NOT NULL
             AND pi.iv_no LIKE 'SR%'
        JOIN (
            SELECT sr_doc_base AS sr_doc_base, ref_invoice AS ref_invoice
            FROM credit_note_amounts
            WHERE ref_invoice IS NOT NULL AND ref_invoice <> ''
            UNION
            SELECT st.doc_base AS sr_doc_base, st.ref_invoice AS ref_invoice
            FROM sales_transactions st
            WHERE st.doc_base LIKE 'SR%'
              AND st.ref_invoice IS NOT NULL
              AND st.ref_invoice <> ''
              AND st.doc_base NOT IN (SELECT sr_doc_base FROM credit_note_amounts)
        ) srref ON srref.sr_doc_base = pi.iv_no
        WHERE {where}
          AND {sr_billable}
    """

    # Pure-legacy candidates: invoices that DO have a non-cancelled NULL
    # link. Whether they're really pure-legacy is decided in Python using
    # has_real (a real link anywhere ⇒ NOT pure-legacy).
    legacy_sql = f"""
        SELECT pi.iv_no                            AS iv_no,
               SUBSTR(MAX(rp.date_iso), 1, 7)      AS month,
               MAX(rp.id)                          AS re_id,
               ROUND(COALESCE(
                   (SELECT SUM(CASE WHEN st2.vat_type = 2
                                    THEN st2.net * 1.07 ELSE st2.net END)
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
        cna_present = _has_cna(c)
        has_real = {r['iv_no']: r['has_real']
                    for r in c.execute(has_real_sql, params).fetchall()}
        real_rows = c.execute(real_sql, params).fetchall()
        sr_rows = (c.execute(sr_link_sql, params).fetchall()
                   if cna_present else [])
        legacy_rows = c.execute(legacy_sql, params).fetchall()

    out = []
    for r in real_rows:
        out.append({'month': r['month'],
                    're_id': r['re_id'],
                    'amount': round(float(r['amount']), 2)})
    for r in sr_rows:
        # negative amount → nets the receipt's attributed cash
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
    billed, credit_notes, net_owed, collected, outstanding, status,
    over_credited, last_payment_date.
    """
    with _ConnCtx(conn, db_path) as c:
        raw = _settlement_rows(c, customer=customer,
                               date_from=date_from, date_to=date_to)
    out = [_reconcile(r) for r in raw]
    out.sort(key=lambda d: (d['invoice_date'] or '', d['doc_base']))
    return out


# ── 1b. unattributable_sr_count ──────────────────────────────────────────────
def unattributable_sr_count(conn=None, db_path=None):
    """Count of credit-note (SR) rows that net against NOTHING because
    `ref_invoice` is NULL or '' — i.e. an SR that cannot be attributed to
    an original invoice. Read-only data-quality probe.

    Counts SR *rows* (one per sales_transactions line), matching the
    grain of the synthetic `_ins_sr` helper and of how SR rows are stored.
    """
    sql = """
        SELECT COUNT(*) AS n
        FROM sales_transactions
        WHERE doc_base LIKE 'SR%'
          AND (ref_invoice IS NULL OR ref_invoice = '')
    """
    with _ConnCtx(conn, db_path) as c:
        row = c.execute(sql).fetchone()
    return int(row['n'] if row is not None else 0)


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


# ── 2b. customer_credit_rows ─────────────────────────────────────────────────
def customer_credit_rows(threshold: float = 5.0,
                         as_of: Optional[str] = None,
                         conn: Optional[sqlite3.Connection] = None,
                         db_path: Optional[str] = None) -> list[dict]:
    """Per-invoice list where the customer overpaid (outstanding < 0).

    Filters `invoice_settlement()` for invoices whose reconciled
    `outstanding` is strictly negative AND |outstanding| >= threshold.
    Pass threshold=0 to surface everything down to the _EPS clamp.

    `as_of` (ISO date, default today): only invoices with
    invoice_date <= as_of appear. Prevents future-dated invoices (e.g. a
    typo'd 2027 date) from polluting a "point-in-time today" view and
    keeps `days_old` non-negative.

    Sort: credit DESC, then invoice_date DESC (newer first).

    Returns dicts with keys:
        doc_base, customer, customer_code, invoice_date,
        billed, credit_notes, collected,
        credit            # = -outstanding, always > 0 in this list
        days_old          # int, today - invoice_date (>= 0)
    """
    as_of_iso = as_of or _today_iso()
    today = date.today()
    settled = invoice_settlement(conn=conn, db_path=db_path)

    out: list[dict] = []
    for r in settled:
        outstanding = r['outstanding']
        if outstanding >= 0:
            continue  # not overpaid

        inv_date = r.get('invoice_date')
        if inv_date and inv_date > as_of_iso:
            continue  # future-dated; skip

        credit = round(-outstanding, 2)
        if credit < threshold:
            continue

        days_old: Optional[int] = None
        if inv_date:
            try:
                y, m, d = (int(x) for x in inv_date.split('-')[:3])
                days_old = (today - date(y, m, d)).days
            except (ValueError, TypeError):
                days_old = None

        out.append({
            'doc_base':      r['doc_base'],
            'customer':      r['customer'],
            'customer_code': r['customer_code'],
            'invoice_date':  inv_date,
            'billed':        r['billed'],
            'credit_notes':  r['credit_notes'],
            'collected':     r['collected'],
            'credit':        credit,
            'days_old':      days_old,
        })

    # Stable sort: sort by least-significant key first, then by primary.
    out.sort(key=lambda x: x['invoice_date'] or '', reverse=True)
    out.sort(key=lambda x: x['credit'], reverse=True)
    return out


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
