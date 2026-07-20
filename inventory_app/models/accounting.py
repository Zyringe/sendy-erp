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

from datetime import date

from database import get_connection

# Cashbook opex exclusions — mirrors models/financial_health.py's
# _NON_OPEX_CATEGORIES / design.md's "Expense formula (opex, closed-month)".
# Unlike the pace panel (financial_health.py), this P&L wants salary IN opex
# (no separate deterministic-salary calc here), so เงินเดือน is NOT excluded.
_NON_OPEX_CATEGORIES = ('เงินทุน/เงินโอน', 'ซื้อสินค้า')

# 2026-03 giveaway (วรสวัสดิ์) distorts that month's revenue/COGS — flagged
# as a note, not hard-coded out of the numbers (design.md step 5: no
# doc_base tuple; self-corrects once the accountant lands the ใบลดหนี้
# reversal). Just the calendar window it can overlap.
_MARCH_2026_START = '2026-03-01'
_MARCH_2026_END = '2026-03-31'


def _signed_net_sql(alias=None):
    """SQL expression that nets SR (return) rows out of a `net` sum.

    SR rows are stored POSITIVE in sales_transactions on purpose (they sync
    to the stock ledger as an IN — returned goods back in stock). The fix
    belongs at the REPORTING layer only: net revenue = Σnet(non-SR) −
    Σnet(SR), matching Ranpo's GL-verified identity
    `Sendy SUM(net) − SR == GL 41-01`.
    """
    net = f'{alias}.net' if alias else 'net'
    doc = f'{alias}.doc_no' if alias else 'doc_no'
    return f"CASE WHEN {doc} LIKE 'SR%' THEN -{net} ELSE {net} END"


def _overlaps(date_from, date_to, lo, hi):
    """True if the [date_from, date_to] period overlaps [lo, hi] (ISO dates)."""
    return date_from <= hi and date_to >= lo


def get_accounting_summary(date_from=None, date_to=None):
    """
    Aggregate profit / cost / expenses for the /accounting page.

    date_from / date_to: 'YYYY-MM-DD' strings.
    Defaults to the most recent month that has sales data.

    Revenue  = Σnet from sales_transactions, SR (return) rows netted out —
               pre-VAT, post-doc-discount.
    COGS     = SUM(qty * cost_price) from products        — current cost_price (WACC basis)
               Lines where product has no cost_price are counted separately (no_cost_lines)
    Expenses = cashbook_transactions opex (direction='expense', non-transfer
               account, category not in COGS/transfer categories) — BSN+SD
               (cashbook is not company-scoped). None when the period has
               ZERO qualifying cashbook rows (pre-cashbook-era months, e.g.
               before 2026-03) so the page can't show a fake profit.
    Commission is NOT subtracted separately — cashbook opex already
    includes it (see module docstring).
    """
    import calendar as _cal

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

    # ── Revenue (sales net, SR return rows netted out — see _signed_net_sql) ──
    s = conn.execute(f"""
        SELECT COALESCE(SUM({_signed_net_sql()}), 0) AS total_net,
               COUNT(*)               AS line_count,
               COUNT(DISTINCT doc_no) AS doc_count
          FROM sales_transactions
         WHERE date_iso >= ? AND date_iso <= ?
    """, (date_from, date_to)).fetchone()
    sales_net = float(s['total_net'])

    # ── COGS (current cost_price × qty; unmapped lines counted separately) ────
    cogs_row = conn.execute("""
        SELECT COALESCE(SUM(st.qty * COALESCE(p.cost_price, 0)), 0) AS cogs,
               COUNT(CASE WHEN p.cost_price IS NULL THEN 1 END)     AS no_cost_lines,
               COUNT(CASE WHEN p.cost_price = 0    THEN 1 END)      AS zero_cost_lines
          FROM sales_transactions st
          LEFT JOIN products p ON p.id = st.product_id
         WHERE st.date_iso >= ? AND st.date_iso <= ?
    """, (date_from, date_to)).fetchone()
    cogs = float(cogs_row['cogs'])
    no_cost_lines = cogs_row['no_cost_lines'] or 0
    zero_cost_lines = cogs_row['zero_cost_lines'] or 0

    # ── Gross profit ──────────────────────────────────────────────────────────
    gross_profit = sales_net - cogs
    margin_pct = (gross_profit / sales_net * 100.0) if sales_net > 0 else 0.0

    # ── Expenses (cashbook opex — replaces the dead expense_log) ──────────────
    # Count ROWS (not just sum) so a period with zero cashbook coverage is
    # distinguishable from a real month that happens to net to zero.
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
         GROUP BY ct.category
         ORDER BY total DESC
    """, (date_from, date_to, *_NON_OPEX_CATEGORIES)).fetchall()

    has_expense_coverage = len(exp_rows) > 0
    if has_expense_coverage:
        expenses_by_category = [
            {'category_name': r['category_name'], 'total': float(r['total'] or 0)}
            for r in exp_rows
        ]
        expenses = float(sum(c['total'] for c in expenses_by_category))
        # ── Net profit — NO separate commission subtraction: cashbook opex
        # above already includes จ่ายค่าคอมมิชชั่น (design.md Q5, avoids
        # double-counting commission_payouts on top of it).
        net_profit = gross_profit - expenses
    else:
        expenses_by_category = []
        expenses = None
        net_profit = None

    # ── Brand breakdown (own-brands first per CLAUDE.md priority) ────────────
    # Own-brand order: Golden Lion (sort 10) → A-SPEC (sort 20) → Sendai (sort 30)
    # then 3rd-party by sort_order → finally NULL brand rows. SR rows netted
    # out per-brand too (same _signed_net_sql identity as total revenue).
    signed_net_st = _signed_net_sql('st')
    brand_rows = conn.execute(f"""
        SELECT
          COALESCE(b.name_th, b.name, '(ไม่ระบุแบรนด์)') AS brand_label,
          b.is_own_brand,
          COALESCE(b.sort_order, 9999)                    AS sort_ord,
          ROUND(SUM({signed_net_st}), 2)                  AS sales_net,
          ROUND(SUM(st.qty * COALESCE(p.cost_price, 0)), 2) AS cogs_approx,
          COUNT(st.id)                                    AS line_count,
          COUNT(CASE WHEN p.cost_price IS NULL OR p.cost_price = 0 THEN 1 END)
                                                          AS no_cost_lines
        FROM sales_transactions st
        LEFT JOIN products  p ON p.id = st.product_id
        LEFT JOIN brands    b ON b.id = p.brand_id
        WHERE st.date_iso >= ? AND st.date_iso <= ?
        GROUP BY b.id, b.name, b.name_th, b.is_own_brand, b.sort_order
        ORDER BY COALESCE(b.is_own_brand, 0) DESC,
                 COALESCE(b.sort_order, 9999),
                 SUM({signed_net_st}) DESC
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
        'gross_profit': gross_profit,
        'margin_pct': margin_pct,
        'expenses': expenses,
        'expenses_by_category': expenses_by_category,
        'has_expense_coverage': has_expense_coverage,
        'net_profit': net_profit,
        'note_march_anomaly': note_march_anomaly,
        'brand_breakdown': brand_breakdown,
        'available_months': available_months,
    }
