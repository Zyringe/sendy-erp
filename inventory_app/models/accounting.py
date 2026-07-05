"""Accounting-summary reader — extracted verbatim from models.py
(behavior-preserving split, Phase 12) — see models/__init__.py's module
docstring for the overall file-split rationale. No behavior changes.
"""

from datetime import date

from database import get_connection


def get_accounting_summary(date_from=None, date_to=None):
    """
    Aggregate profit / cost / expenses / commission for the /accounting page.

    date_from / date_to: 'YYYY-MM-DD' strings.
    Defaults to the most recent month that has sales data.

    Revenue  = SUM(net) from sales_transactions          — pre-VAT, post-doc-discount
    COGS     = SUM(qty * cost_price) from products        — current cost_price (WACC basis)
               Lines where product has no cost_price are counted separately (no_cost_lines)
    Expenses = SUM(amount_pre_vat) from expense_log       — 0 rows currently, shown as 0
    Commission = SUM(amount_paid) from commission_payouts — actual paid, by year_month overlap

    company_id = 1 (BSN) is the only scope in this DB.
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

    # ── Revenue (sales net) ───────────────────────────────────────────────────
    s = conn.execute("""
        SELECT COALESCE(SUM(net), 0)  AS total_net,
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

    # ── Expenses (expense_log, BSN = company_id 1) ────────────────────────────
    exp_total = conn.execute("""
        SELECT COALESCE(SUM(amount_pre_vat), 0) AS total
          FROM expense_log
         WHERE company_id = 1
           AND date_iso >= ? AND date_iso <= ?
    """, (date_from, date_to)).fetchone()
    expenses = float(exp_total['total'])

    # Expenses by category
    exp_by_cat = conn.execute("""
        SELECT ec.name_th AS category_name,
               ec.code    AS category_code,
               COALESCE(SUM(el.amount_pre_vat), 0) AS total
          FROM expense_categories ec
          LEFT JOIN expense_log el ON el.category_id = ec.id
                AND el.company_id = 1
                AND el.date_iso >= ? AND el.date_iso <= ?
         WHERE ec.is_active = 1
         GROUP BY ec.id, ec.code, ec.name_th, ec.sort_order
         ORDER BY ec.sort_order
    """, (date_from, date_to)).fetchall()

    # ── Commission (actual paid, overlapping the period's months) ─────────────
    # Extract YYYY-MM range from the date filter, match commission_payouts.year_month
    ym_from = date_from[:7]
    ym_to = date_to[:7]
    comm_row = conn.execute("""
        SELECT COALESCE(SUM(amount_paid), 0) AS total
          FROM commission_payouts
         WHERE year_month >= ? AND year_month <= ?
    """, (ym_from, ym_to)).fetchone()
    commission_total = float(comm_row['total'])

    # ── Net profit (approximate) ──────────────────────────────────────────────
    net_profit = gross_profit - expenses - commission_total

    # ── Brand breakdown (own-brands first per CLAUDE.md priority) ────────────
    # Own-brand order: Golden Lion (sort 10) → A-SPEC (sort 20) → Sendai (sort 30)
    # then 3rd-party by sort_order → finally NULL brand rows
    brand_rows = conn.execute("""
        SELECT
          COALESCE(b.name_th, b.name, '(ไม่ระบุแบรนด์)') AS brand_label,
          b.is_own_brand,
          COALESCE(b.sort_order, 9999)                    AS sort_ord,
          ROUND(SUM(st.net), 2)                           AS sales_net,
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
        'expenses_by_category': [dict(r) for r in exp_by_cat],
        'commission_total': commission_total,
        'net_profit': net_profit,
        'brand_breakdown': brand_breakdown,
        'available_months': available_months,
    }
