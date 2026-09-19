"""Accounting-summary reader — extracted verbatim from models.py
(behavior-preserving split, Phase 12) — see models/__init__.py's module
docstring for the overall file-split rationale.

V2 (2026-07-20, design.md "V2 — /accounting P&L honesty"): wired expenses to
the real cashbook, replacing the dead `expense_log`/`expense_categories`
(always 0 rows). Revenue nets out SR (return) rows at the REPORTING layer
only — SR rows stay stored POSITIVE in sales_transactions (they sync to the
stock ledger as an IN; do NOT touch storage, see
.claude/rules/erp-engineering-discipline.md). Commission is no longer
subtracted a second time — cashbook opex already includes the
จ่ายค่าคอมมิชชั่น category (design.md Q5); `commission_total` is dropped.
"""

import calendar as _cal
from datetime import date

import sales_filters
from database import get_connection

# Cashbook opex exclusions — mirrors models/financial_health.py's
# _NON_OPEX_CATEGORIES / design.md's "Expense formula (opex, closed-month)".
# Unlike the pace panel (financial_health.py), this P&L wants salary IN opex
# (no separate deterministic-salary calc here), so เงินเดือน is NOT excluded.
_NON_OPEX_CATEGORIES = ('เงินทุน/เงินโอน', 'ซื้อสินค้า')

# Incomplete-month reading (CONTEXT.md "Internal P&L" ->
# เดือนที่ข้อมูลยังไม่ครบ): an account is "expected" for a month when it is
# active, non-transfer, and carried a qualifying opex row in >= _MIN of the
# _LOOKBACK calendar months before it.
_EXPECTED_LOOKBACK_MONTHS = 6
_EXPECTED_MIN_MONTHS = 3

# 2026-03 giveaway (วรสวัสดิ์) distorts that month's revenue/COGS — flagged
# as a note, not hard-coded out of the numbers (design.md step 5: no
# doc_base tuple; self-corrects once the accountant lands the ใบลดหนี้
# reversal). Just the calendar window it can overlap.
_MARCH_2026_START = '2026-03-01'
_MARCH_2026_END = '2026-03-31'


# Revenue convention (Put 2026-07-21; corrected #514, 2026-09-16): match
# revenue.py's canonical sales universe = GL 41-01 (ยอดขาย). EXCLUDE SR
# (returns — not a sale, live in the GL 41-02 contra account). COUNT HS
# (cash sales, ขายสด) — GL-verified (#514): 260/260 non-zero HS docs credit
# 41-01 for exactly their net, same account IV uses, and their COGS posts to
# the same 51-01 account too. The old "SUM(net) − Σ SR == GL 41-01" identity
# was measured on a population that already included HS inside SUM(net); the
# 2026-07-21 comment excluding HS was the misreading, not the identity.
# Every sales_transactions query in this P&L carries
# `AND doc_no NOT LIKE 'SR%'`.
# (SR/HS rows stay stored as-is — SR syncs to stock as an IN; NEVER flip that.)


def _overlaps(date_from, date_to, lo, hi):
    """True if the [date_from, date_to] period overlaps [lo, hi] (ISO dates)."""
    return date_from <= hi and date_to >= lo


def _shift_ym(ym, delta_months):
    """'YYYY-MM' shifted by a signed number of calendar months."""
    y, m = (int(x) for x in ym.split('-'))
    idx = y * 12 + (m - 1) + delta_months
    y2, m2 = divmod(idx, 12)
    return f'{y2:04d}-{m2 + 1:02d}'


def _incomplete_months(conn, date_from, date_to):
    """CONTEXT.md "Internal P&L" -> เดือนที่ข้อมูลยังไม่ครบ. Judged over the
    WHOLE calendar month(s) the period touches, never the [date_from, date_to]
    slice (a 10-day custom range must not flag every account as missing) —
    so month enumeration and the presence window both use calendar-month
    boundaries, ignoring the exact days in date_from/date_to.
    """
    first_ym = date_from[:7]
    last_ym = date_to[:7]

    window_start = _shift_ym(first_ym, -_EXPECTED_LOOKBACK_MONTHS) + '-01'
    last_y, last_m = (int(x) for x in last_ym.split('-'))
    window_end = f'{last_y:04d}-{last_m:02d}-{_cal.monthrange(last_y, last_m)[1]:02d}'

    excl_placeholders = ','.join('?' * len(_NON_OPEX_CATEGORIES))
    rows = conn.execute(f"""
        SELECT COALESCE(ca.display_name, ca.code) AS label,
               substr(ct.txn_date, 1, 7)           AS ym
          FROM cashbook_transactions ct
          JOIN cashbook_accounts ca ON ca.id = ct.account_id
         WHERE ct.direction = 'expense'
           AND ca.is_active = 1
           AND ca.is_transfer = 0
           AND COALESCE(ct.category, '') NOT IN ({excl_placeholders})
           AND ct.txn_date >= ? AND ct.txn_date <= ?
         GROUP BY label, ym
    """, (*_NON_OPEX_CATEGORIES, window_start, window_end)).fetchall()

    presence = {}
    for r in rows:
        presence.setdefault(r['label'], set()).add(r['ym'])

    months = []
    ym = first_ym
    while ym <= last_ym:
        months.append(ym)
        ym = _shift_ym(ym, 1)

    incomplete_months = []
    for ym in months:
        lookback = {_shift_ym(ym, -k) for k in range(1, _EXPECTED_LOOKBACK_MONTHS + 1)}
        expected = [label for label, yms in presence.items()
                    if len(yms & lookback) >= _EXPECTED_MIN_MONTHS]
        missing = sorted(label for label in expected if ym not in presence[label])
        if missing:
            incomplete_months.append({'ym': ym, 'missing': missing})

    return incomplete_months


def get_accounting_summary(date_from=None, date_to=None):
    """
    Aggregate profit / cost / expenses for the /accounting page.

    date_from / date_to: 'YYYY-MM-DD' strings.
    Defaults to the most recent month that has sales data.

    Revenue  = Σnet from sales_transactions, SR (return) rows netted out —
               pre-VAT, post-doc-discount.
    COGS     = SUM(qty-in-BASE-units * cost_price) — current cost_price (WACC
               basis), with the bill's unit converted by sales_filters
               .base_qty_sql() first (a โหล line costs 12 pieces).
               Lines where product has no cost_price are counted separately
               (no_cost_lines); lines whose bill unit has no ratio, and so
               fell back to 1, are counted in unknown_ratio_lines.
    Expenses = cashbook_transactions opex (direction='expense', non-transfer
               account, category not in COGS/transfer categories) — BSN+SD
               (cashbook is not company-scoped). None when the period has
               ZERO qualifying cashbook rows (pre-cashbook-era months, e.g.
               before 2026-03) so the page can't show a fake profit.
               That population is SPLIT on `belongs_to_period` (mig 187):
               `expenses` is ค่าใช้จ่ายดำเนินงาน (the column NULL — the cost
               belongs to the month it was paid) and
               `prior_period_expenses` is ค่าใช้จ่ายของงวดก่อน (a period was
               identified, so the cost belongs to an earlier one). Both are
               None together or neither. กำไรสุทธิ subtracts both, so the
               split does not move it. See ADR 0014 and CONTEXT.md
               "Internal P&L".
    Commission is NOT subtracted separately — cashbook opex already
    includes it (see module docstring).
    """
    conn = get_connection()

    # ── Resolve default period ────────────────────────────────────────────────
    if not date_from and not date_to:
        # Latest month with sales data
        row = conn.execute(
            "SELECT MAX(date_iso) AS mx FROM sales_transactions"
        ).fetchone()
        if row and row['mx']:
            from datetime import datetime as _dt
            latest = _dt.strptime(row['mx'][:7], '%Y-%m')
            date_from = latest.strftime('%Y-%m-01')
            date_to = latest.strftime(
                f'%Y-%m-{_cal.monthrange(latest.year, latest.month)[1]:02d}'
            )
        else:
            today = date.today()
            date_from = today.strftime('%Y-%m-01')
            date_to = today.strftime(
                f'%Y-%m-{_cal.monthrange(today.year, today.month)[1]:02d}'
            )
    elif date_from and not date_to:
        date_to = date.today().isoformat()
    elif date_to and not date_from:
        date_from = '2000-01-01'

    # ── Revenue (SR return rows EXCLUDED, HS cash sales COUNTED — = GL 41-01,
    # see convention above) ──
    # doc_count counts invoices (doc_base); doc_no is one line of one (#496).
    s = conn.execute("""
        SELECT COALESCE(SUM(net), 0) AS total_net,
               COUNT(*)               AS line_count,
               COUNT(DISTINCT doc_base) AS doc_count
          FROM sales_transactions
         WHERE date_iso >= ? AND date_iso <= ?
           AND doc_no NOT LIKE 'SR%'
           AND {not_a_sale}
    """.format(not_a_sale=sales_filters.not_a_sale_clause()), (date_from, date_to)).fetchone()
    sales_net = float(s['total_net'])

    # ── COGS (current cost_price × qty in BASE units; see sales_filters) ─────
    cogs_row = conn.execute("""
        SELECT COALESCE(SUM({base_qty} * COALESCE(p.cost_price, 0)), 0) AS cogs,
               COUNT(CASE WHEN p.cost_price IS NULL THEN 1 END)     AS no_cost_lines,
               COUNT(CASE WHEN p.cost_price = 0    THEN 1 END)      AS zero_cost_lines,
               COALESCE(SUM({unratioed}), 0)                        AS unknown_ratio_lines
          FROM sales_transactions st
          LEFT JOIN products p ON p.id = st.product_id
          {uc_join}
         WHERE st.date_iso >= ? AND st.date_iso <= ?
           AND st.doc_no NOT LIKE 'SR%'
           -- NO excludes_revenue filter here, on purpose: a giveaway's goods
           -- really left the warehouse, so their cost is a real expense.
           -- Dropping it too would hand the margin back. See sales_filters.
           -- HS cash sales COUNTED here too, same reason — the goods really
           -- left the warehouse, and HS posts its COGS to the same 51-01 GL
           -- account IV uses (#514).
    """.format(base_qty=sales_filters.base_qty_sql(),
               unratioed=sales_filters.unratioed_line_sql(),
               uc_join=sales_filters.unit_conversion_join()),
       (date_from, date_to)).fetchone()
    cogs = float(cogs_row['cogs'])
    no_cost_lines = cogs_row['no_cost_lines'] or 0
    zero_cost_lines = cogs_row['zero_cost_lines'] or 0
    unknown_ratio_lines = cogs_row['unknown_ratio_lines'] or 0

    # ── Gross profit ──────────────────────────────────────────────────────────
    gross_profit = sales_net - cogs
    margin_pct = (gross_profit / sales_net * 100.0) if sales_net > 0 else 0.0

    # ── Expenses (cashbook opex — replaces the dead expense_log) ──────────────
    # Count ROWS (not just sum) so a period with zero cashbook coverage is
    # distinguishable from a real month that happens to net to zero.
    # ONE population, split in two by belongs_to_period (mig 187): NULL is
    # ค่าใช้จ่ายดำเนินงาน, a set value is ค่าใช้จ่ายของงวดก่อน. Both queries
    # therefore carry the SAME three filters — direction, non-transfer
    # account, non-COGS/transfer category — and the split is deliberately NOT
    # a reason to widen any of them (ADR 0014 decision 3 / spec #593).
    excl_placeholders = ','.join('?' * len(_NON_OPEX_CATEGORIES))
    exp_rows = conn.execute(f"""
        SELECT COALESCE(ct.category, '(ไม่ระบุหมวด)') AS category_name,
               SUM(ct.amount)                          AS total
          FROM cashbook_transactions ct
          JOIN cashbook_accounts ca ON ca.id = ct.account_id
         WHERE ct.direction = 'expense'
           AND ca.is_transfer = 0
           AND ct.txn_date >= ? AND ct.txn_date <= ?
           AND COALESCE(ct.category, '') NOT IN ({excl_placeholders})
           AND ct.belongs_to_period IS NULL
         GROUP BY ct.category
         ORDER BY total DESC
    """, (date_from, date_to, *_NON_OPEX_CATEGORIES)).fetchall()

    # Individual rows, not a category roll-up: the line is bounded by the fact
    # that only a deliberate DB write can put a row on it (no cashbook UI —
    # spec #593 calls that a YAGNI call, not an oversight), and Put reads it
    # to recognise the specific cost.
    prior_rows = conn.execute(f"""
        SELECT ct.txn_date, ct.belongs_to_period, ct.category, ct.description,
               ct.amount
          FROM cashbook_transactions ct
          JOIN cashbook_accounts ca ON ca.id = ct.account_id
         WHERE ct.direction = 'expense'
           AND ca.is_transfer = 0
           AND ct.txn_date >= ? AND ct.txn_date <= ?
           AND COALESCE(ct.category, '') NOT IN ({excl_placeholders})
           AND ct.belongs_to_period IS NOT NULL
         ORDER BY ct.txn_date, ct.amount DESC
    """, (date_from, date_to, *_NON_OPEX_CATEGORIES)).fetchall()

    # Coverage is judged on the COMBINED population: a month whose only
    # qualifying rows are prior-period ones HAS been keyed, so it is covered
    # (expenses 0.00), never 'no_coverage' — that label is reserved for the
    # genuine pre-cashbook era. `expenses` and `prior_period_expenses` are
    # therefore None together or neither; the template branches on that.
    has_expense_coverage = len(exp_rows) > 0 or len(prior_rows) > 0
    prior_period_lines = [
        {'txn_date': r['txn_date'],
         'belongs_to_period': r['belongs_to_period'],
         'category': r['category'],
         'description': r['description'],
         'amount': float(r['amount'] or 0)}
        for r in prior_rows
    ]
    if has_expense_coverage:
        expenses_by_category = [
            {'category_name': r['category_name'], 'total': float(r['total'] or 0)}
            for r in exp_rows
        ]
        expenses = float(sum(c['total'] for c in expenses_by_category))
        prior_period_expenses = float(sum(line['amount'] for line in prior_period_lines))
    else:
        expenses_by_category = []
        expenses = None
        prior_period_expenses = None

    # incomplete_months must be computed UNCONDITIONALLY — a month with zero
    # opex rows of its own (nobody has keyed it yet, e.g. the first days of a
    # new month) is 'incomplete', not 'no_coverage': that label is reserved
    # for the genuine pre-cashbook era, and its banner text ("สมุดรับ-จ่าย
    # เริ่มมีข้อมูลตั้งแต่ มี.ค. 2569") would be false for a month that simply
    # has not been keyed yet.
    incomplete_months = _incomplete_months(conn, date_from, date_to)
    if incomplete_months:
        expense_status = 'incomplete'
        net_profit = None
    elif not has_expense_coverage:
        expense_status = 'no_coverage'
        net_profit = None
    else:
        expense_status = 'complete'
        # ── Net profit — NO separate commission subtraction: cashbook opex
        # above already includes จ่ายค่าคอมมิชชั่น (design.md Q5, avoids
        # double-counting commission_payouts on top of it).
        # Both expense lines are subtracted, so splitting them does NOT move
        # this number — every baht that left still lands in the bottom line
        # (ADR 0014 decision 3, spec #593 story 10).
        # ⚠ The parentheses are load-bearing and must not be "simplified" to
        # `gross_profit - expenses - prior_period_expenses`: subtracting twice
        # re-associates the float and can move the result by 1 ULP. Measured
        # on a prod-derived copy for 2026-03: -663652.6317031696 before the
        # split and with this form, -663652.6317031697 with two subtractions.
        # Invisible at two decimal places, but story 10's whole claim is that
        # this number does not move, and "it moves in the last bit" is a
        # weaker claim than the one being made.
        # Pinned by test_stamping_a_row_does_not_move_net_profit_by_one_bit.
        net_profit = gross_profit - (expenses + prior_period_expenses)

    # ── Brand breakdown (own-brands first per CLAUDE.md priority) ────────────
    # Own-brand order: Golden Lion (sort 10) → A-SPEC (sort 20) → Sendai (sort 30)
    # then 3rd-party by sort_order → finally NULL brand rows. SR rows EXCLUDED,
    # HS cash sales COUNTED, per-brand too (same GL-41-01 convention as total
    # revenue, see top of file).
    brand_rows = conn.execute("""
        SELECT
          COALESCE(b.name_th, b.name, '(ไม่ระบุแบรนด์)') AS brand_label,
          b.is_own_brand,
          COALESCE(b.sort_order, 9999)                    AS sort_ord,
          ROUND(SUM(st.net), 2)                           AS sales_net,
          ROUND(SUM(""" + sales_filters.base_qty_sql() + """
                    * COALESCE(p.cost_price, 0)), 2)      AS cogs_approx,
          COUNT(st.id)                                    AS line_count,
          COUNT(CASE WHEN p.cost_price IS NULL OR p.cost_price = 0 THEN 1 END)
                                                          AS no_cost_lines
        FROM sales_transactions st
        LEFT JOIN products  p ON p.id = st.product_id
        LEFT JOIN brands    b ON b.id = p.brand_id
        """ + sales_filters.unit_conversion_join() + """
        WHERE st.date_iso >= ? AND st.date_iso <= ?
          AND st.doc_no NOT LIKE 'SR%'
          AND """ + sales_filters.not_a_sale_clause('st') + """
        GROUP BY b.id, b.name, b.name_th, b.is_own_brand, b.sort_order
        ORDER BY COALESCE(b.is_own_brand, 0) DESC,
                 COALESCE(b.sort_order, 9999),
                 SUM(st.net) DESC
    """, (date_from, date_to)).fetchall()

    brand_breakdown = []
    for r in brand_rows:
        sn = float(r['sales_net'] or 0)
        cg = float(r['cogs_approx'] or 0)
        gp = sn - cg
        mp = (gp / sn * 100.0) if sn > 0 else 0.0
        brand_breakdown.append({
            'brand_label': r['brand_label'],
            'is_own_brand': bool(r['is_own_brand']),
            'sales_net': sn,
            'cogs_approx': cg,
            'gross_profit': gp,
            'margin_pct': mp,
            'line_count': r['line_count'],
            'no_cost_lines': r['no_cost_lines'] or 0,
        })

    # ── Available months (for period selector) ────────────────────────────────
    months_rows = conn.execute("""
        SELECT DISTINCT strftime('%Y-%m', date_iso) AS ym
          FROM sales_transactions
         ORDER BY ym DESC
         LIMIT 36
    """).fetchall()
    available_months = [r['ym'] for r in months_rows]

    # ── March 2026 giveaway anomaly note (date-overlap only) ──────────────────
    note_march_anomaly = _overlaps(date_from, date_to, _MARCH_2026_START, _MARCH_2026_END)

    conn.close()

    return {
        'date_from': date_from,
        'date_to': date_to,
        'sales_net': sales_net,
        'doc_count': s['doc_count'],
        'line_count': s['line_count'],
        'cogs': cogs,
        'no_cost_lines': no_cost_lines,
        'zero_cost_lines': zero_cost_lines,
        'unknown_ratio_lines': unknown_ratio_lines,
        'gross_profit': gross_profit,
        'margin_pct': margin_pct,
        'expenses': expenses,
        'expenses_by_category': expenses_by_category,
        'prior_period_expenses': prior_period_expenses,
        'prior_period_lines': prior_period_lines,
        'expense_status': expense_status,
        'incomplete_months': incomplete_months,
        'net_profit': net_profit,
        'note_march_anomaly': note_march_anomaly,
        'brand_breakdown': brand_breakdown,
        'available_months': available_months,
    }
