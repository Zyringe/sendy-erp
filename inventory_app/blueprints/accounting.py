"""Accounting blueprint — Express AR/AP redirects, unified AP/AR dashboards,
accounting summary, cash flow + revenue dashboards, and the AR follow-up
(dunning) workspace.

Extracted verbatim from app.py (behavior-preserving split) — see app.py's
module docstring for the overall file-split rationale. No URL changes;
route rules are unchanged, only their endpoint names gain an `accounting.`
prefix.
"""
import sqlite3
from datetime import date

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, session, current_app)

import models
from database import get_connection
import cashflow as cf_mod
import revenue as rev_mod
import ar_followup as arf_mod
import payments_alloc as pa_mod

bp_accounting = Blueprint('accounting', __name__)


@bp_accounting.route('/express/import')
def express_import():
    # Legacy single-file Express uploader (AR/AP snapshot, payments-out, credit
    # notes). Superseded by the unified box (/import-data), which auto-detects
    # and routes every Express report type. Kept as a redirect for old links.
    return redirect(url_for('bsn.unified_import'))


@bp_accounting.route('/express/ar')
def express_ar_dashboard():
    """Redirect stub — content moved to /ar?tab=overview (AR consolidation)."""
    return redirect(url_for('accounting.ar_dashboard', tab='overview'))


@bp_accounting.route('/express/ar/customer/<customer_code>')
def express_ar_customer(customer_code):
    """Per-customer AR drill-down — all unpaid invoices in the latest snapshot."""
    conn = get_connection()
    snapshot = conn.execute(
        "SELECT MAX(snapshot_date_iso) AS d FROM express_ar_outstanding WHERE entity = 'BSN'"
    ).fetchone()
    snapshot_date = snapshot['d'] if snapshot else None

    rows = conn.execute("""
        SELECT customer_code, customer_name, customer_type, salesperson_code,
               doc_no, doc_date_iso, bill_amount, paid_amount, outstanding_amount,
               is_anomalous, has_warning,
               CAST(julianday('now') - julianday(doc_date_iso) AS INTEGER) AS age_days
          FROM express_ar_outstanding
         WHERE entity = 'BSN'
           AND snapshot_date_iso = ?
           AND customer_code = ?
           AND is_anomalous = 0
         ORDER BY doc_date_iso ASC
    """, (snapshot_date, customer_code)).fetchall()

    if not rows:
        flash(f'ไม่พบลูกหนี้รหัส {customer_code}', 'warning')
        return redirect(url_for('accounting.express_ar_dashboard'))

    customer_name = rows[0]['customer_name']
    customer_type = rows[0]['customer_type']
    salesperson_code = rows[0]['salesperson_code']
    total_outstanding = sum((r['outstanding_amount'] or 0) for r in rows)
    total_billed = sum((r['bill_amount'] or 0) for r in rows)
    oldest = min((r['doc_date_iso'] or '9999-12-31') for r in rows)

    # Pull recent payment history from the CANONICAL received_payments table
    # (the express_payments_in twin is frozen / being retired). received_payments
    # has no customer_code FK, so match by name; it carries a single `total`
    # rather than a cash/cheque/discount split.
    recent_payments = conn.execute("""
        SELECT rp.re_no       AS doc_no,
               rp.date_iso,
               rp.total,
               rp.salesperson AS salesperson_code
          FROM received_payments rp
         WHERE rp.cancelled = 0
           AND rp.customer = ?
         ORDER BY rp.date_iso DESC
         LIMIT 20
    """, (customer_name,)).fetchall()
    conn.close()

    return render_template('express_ar_customer.html',
                           customer_code=customer_code,
                           customer_name=customer_name,
                           customer_type=customer_type,
                           salesperson_code=salesperson_code,
                           snapshot_date=snapshot_date,
                           rows=[dict(r) for r in rows],
                           recent_payments=[dict(r) for r in recent_payments],
                           total_outstanding=total_outstanding,
                           total_billed=total_billed,
                           oldest_date=oldest)


@bp_accounting.route('/express/ap')
def express_ap_dashboard():
    """Redirect stub — keep bookmarks working."""
    return redirect(url_for('accounting.ap_dashboard', tab='overview'))


# ── Unified AP page ───────────────────────────────────────────────────────────

@bp_accounting.route('/ap')
def ap_dashboard():
    """Unified payables page. Tabs: overview | suppliers | payments.
    VIEW open to any logged-in role (read-only; payments come from imports)."""
    tab = request.args.get('tab', 'overview')
    date_from = request.args.get('from') or '2024-01-01'
    date_to   = request.args.get('to')   or date.today().isoformat()
    conn = get_connection()
    ap = models.get_ap_outstanding(conn)
    summary = conn.execute("""
        SELECT COUNT(*) AS n_payments, COUNT(DISTINCT supplier_name) AS n_suppliers,
               ROUND(SUM(cash_amount + cheque_amount), 2) AS total_paid
          FROM express_payments_out
         WHERE is_void = 0 AND date_iso BETWEEN ? AND ?
    """, (date_from, date_to)).fetchone()
    ctx = {'tab': tab, 'ap': ap, 'summary': dict(summary) if summary else {},
           'date_from': date_from, 'date_to': date_to}

    if tab in ('suppliers', 'payments'):
        ctx['pay_rows'] = [dict(r) for r in conn.execute("""
            SELECT supplier_name, COUNT(*) AS payments,
                   ROUND(SUM(invoice_amount), 2) AS invoice_total,
                   ROUND(SUM(cash_amount + cheque_amount), 2) AS paid_total,
                   ROUND(SUM(discount_amount), 2) AS discount_total,
                   MAX(date_iso) AS last_paid
              FROM express_payments_out
             WHERE is_void = 0 AND date_iso BETWEEN ? AND ?
             GROUP BY supplier_name ORDER BY paid_total DESC
        """, (date_from, date_to)).fetchall()]

    if tab == 'suppliers':
        owed = {s['supplier_name']: s['subtotal'] for s in ap['suppliers']}
        paid = {p['supplier_name']: p for p in ctx['pay_rows']}
        names = list(owed) + [n for n in paid if n not in owed]
        ctx['supplier_rows'] = sorted(
            [{'supplier_name': n, 'owed': owed.get(n, 0.0),
              'paid': (paid.get(n) or {}).get('paid_total', 0.0),
              'last_paid': (paid.get(n) or {}).get('last_paid')} for n in names],
            key=lambda r: r['owed'], reverse=True)

    if tab == 'payments':
        ctx['recent'] = [dict(r) for r in conn.execute("""
            SELECT doc_no, date_iso, supplier_name, invoice_amount,
                   (cash_amount + cheque_amount) AS paid, note
              FROM express_payments_out
             WHERE is_void = 0 AND date_iso BETWEEN ? AND ?
             ORDER BY date_iso DESC, doc_no DESC LIMIT 50
        """, (date_from, date_to)).fetchall()]

    conn.close()
    return render_template('ap.html', **ctx)


# ── Accounting Summary ────────────────────────────────────────────────────────

@bp_accounting.route('/accounting')
def accounting_summary():
    """
    Accounting summary landing page for the 'การค้า & บัญชี' module.
    Admin + manager: full view including cost/margin.
    Staff: redirected — same gating as cost-visible pages (e.g. partners.customer_summary).
    """
    if session.get('role') not in ('admin', 'manager', 'shareholder'):
        flash('ต้องเข้าสู่ระบบด้วยบัญชี Admin หรือ Manager', 'danger')
        return redirect(url_for('dashboard'))

    date_from = request.args.get('date_from') or None
    date_to = request.args.get('date_to') or None
    year_month = request.args.get('month') or None  # YYYY-MM shortcut

    # If a YYYY-MM shortcut is given, derive date_from/date_to from it
    if year_month and not date_from and not date_to:
        import calendar as _cal
        try:
            y, m = int(year_month[:4]), int(year_month[5:7])
            date_from = f'{y:04d}-{m:02d}-01'
            date_to = f'{y:04d}-{m:02d}-{_cal.monthrange(y, m)[1]:02d}'
        except (ValueError, IndexError):
            year_month = None

    summary = models.get_accounting_summary(date_from, date_to)

    # ── Financial-health pace panel (v1) — "เดือนนี้รอดไหม?" ──────────────────
    # Break-even pace check for the CURRENT month, moved here from the former
    # standalone /financial-health route (design.md, locked S1/S2): no P&L,
    # no projection, no cash/runway. Money math lives in
    # models/financial_health.py, kept separate from get_accounting_summary().
    break_even = models.get_break_even()
    pace = models.get_current_month_pace()

    # Pace status (display-only derivation, NOT a projection — see design.md
    # S1: this compares mtd_revenue to the FIXED floor target prorated by
    # elapsed days, it never extrapolates future performance).
    pace_pct = None
    pace_status = None
    floor_be = break_even.get('break_even_floor')
    if floor_be and pace.get('days_in_month'):
        expected_by_now = floor_be * pace['day_of_month'] / pace['days_in_month']
        if expected_by_now > 0:
            ratio = pace['mtd_revenue'] / expected_by_now
            pace_pct = min(ratio, 1.0) * 100
            if ratio < 0.6:
                pace_status = 'red'
            elif ratio < 1.0:
                pace_status = 'yellow'
            else:
                pace_status = 'green'

    trailing_months = break_even.get('trailing_months') or []
    all_below_floor = bool(trailing_months) and floor_be is not None and all(
        m['revenue'] < floor_be for m in trailing_months)

    return render_template('accounting.html', s=summary, be=break_even, pace=pace,
                            pace_pct=pace_pct, pace_status=pace_status,
                            all_below_floor=all_below_floor)


# ── Unified AR page ───────────────────────────────────────────────────────────

@bp_accounting.route('/ar')
def ar_dashboard():
    """Unified receivables page. Tabs: overview | customers | invoices | reconcile.
    VIEW open to any logged-in role (staff incl.); dunning WRITES stay manager+."""
    tab = request.args.get('tab', 'overview')
    is_ar_manager = session.get('role') in ('admin', 'manager')
    is_ar_admin = session.get('role') == 'admin'   # dunning log writes are admin-only
    ctx = {'tab': tab,
           'snapshot_date': cf_mod.ar_aging().get('as_of'),
           'is_ar_manager': is_ar_manager,
           'is_ar_admin': is_ar_admin}

    if tab == 'overview':
        debt = models.get_customer_debt_summary()
        summ = models.get_payment_summary()
        snapshot_total = sum(r['outstanding_amount'] or 0 for r in debt)
        snapshot_count = len(debt)
        ledger_unpaid = summ['unpaid_amount']
        diff_amount = ledger_unpaid - snapshot_total
        ctx.update(
            snapshot_total=snapshot_total,
            snapshot_count=snapshot_count,
            ledger_unpaid=ledger_unpaid,
            unpaid_count=summ['unpaid_count'],
            diff_amount=diff_amount,
            aging=cf_mod.ar_aging(),
            top_customers=debt[:8],
        )
    elif tab == 'customers':
        bucket = request.args.get('bucket', '').strip()
        min_str = request.args.get('min', '').strip()
        search = request.args.get('q', '').strip()
        sort = request.args.get('sort', 'outstanding')
        try:
            min_amt = float(min_str.replace(',', '')) if min_str else 0.0
        except ValueError:
            min_amt = 0.0
        # customer_ranking() has per-customer age_buckets + oldest_age_days for filters/display
        all_ranked = arf_mod.customer_ranking(min_outstanding=min_amt)
        if bucket in ('0-30', '31-60', '61-90', '90+'):
            all_ranked = [r for r in all_ranked if r['age_buckets'].get(bucket, 0) > 0]
        if search:
            s = search.lower()
            all_ranked = [r for r in all_ranked
                          if s in (r['customer'] or '').lower()
                          or s in (r.get('customer_code') or '').lower()]
        if sort == 'age':
            all_ranked.sort(key=lambda r: -r['oldest_age_days'])
        elif sort == 'count':
            all_ranked.sort(key=lambda r: -r['invoice_count'])
        # else already sorted by outstanding DESC from customer_ranking
        ctx.update(
            customer_rows=all_ranked,
            bucket=bucket,
            min_str=min_str,
            search=search,
            sort=sort,
            customer_total=sum(r['outstanding'] or 0 for r in all_ranked),
        )
    elif tab == 'invoices':
        inv_status = request.args.get('status', 'all')
        inv_search = request.args.get('q', '').strip()
        date_from = request.args.get('date_from', '').strip()
        date_to = request.args.get('date_to', '').strip()
        page = int(request.args.get('page', 1))
        per_page = current_app.config['ITEMS_PER_PAGE']
        rows, total = models.get_payment_status(
            status=inv_status, search=inv_search,
            date_from=date_from, date_to=date_to,
            page=page, per_page=per_page,
        )
        summ = models.get_payment_summary()
        total_pages = max(1, (total + per_page - 1) // per_page)
        ctx.update(
            inv_rows=rows, inv_total=total,
            summary=summ,
            inv_status=inv_status, inv_search=inv_search,
            date_from=date_from, date_to=date_to,
            page=page, total_pages=total_pages,
        )
    elif tab == 'reconcile':
        rec = models.get_ar_reconciliation()
        ctx['reconcile'] = rec

        # Customer-credit-balance section — moved here verbatim from
        # /cashflow (R1, Phase 2 finance revamp): "/ar owns all AR".
        # Point-in-time today, not period. Single snapshot then
        # Python-filter — avoids double-querying and the drift that two
        # separate calls could produce in a concurrent import.
        show_all_credit  = request.args.get('show_all') in ('1', 'true', 'on')
        credit_threshold = 0.0 if show_all_credit else 5.0
        all_credit_rows  = pa_mod.customer_credit_rows(threshold=0.0)
        credit_rows = (all_credit_rows if show_all_credit
                       else [r for r in all_credit_rows
                             if r['credit'] >= credit_threshold])
        credit_total = round(sum(r['credit'] for r in credit_rows), 2)
        credit_hidden_count = len(all_credit_rows) - len(credit_rows)
        ctx.update(
            credit_rows=credit_rows,
            credit_total=credit_total,
            credit_hidden_count=credit_hidden_count,
            show_all_credit=show_all_credit,
        )

    return render_template('ar.html', **ctx)


# ── Cash Flow Dashboard ────────────────────────────────────────────────────────

@bp_accounting.route('/cashflow')
def cashflow_dashboard():
    """Cash flow dashboard: pure "เงินเข้า" view — cash-in by RE month vs
    accrual revenue, plus a compact AR headline linking to /ar.

    AR detail (full aging breakdown + customer credit balances) lives on
    /ar now (R1, Phase 2 finance revamp) — /cashflow only teases the total.

    Admin + manager only (same gating as accounting_summary).
    Optional ?from=YYYY-MM&to=YYYY-MM period filter.
    Default: last 12 months ending the latest data month.
    """
    if session.get('role') not in ('admin', 'manager', 'shareholder'):
        flash('ต้องเข้าสู่ระบบด้วยบัญชี Admin หรือ Manager', 'danger')
        return redirect(url_for('dashboard'))

    from_month = request.args.get('from') or None
    to_month   = request.args.get('to')   or None

    # Derive date_from / date_to from YYYY-MM shortcuts
    def _month_start(ym):
        """'YYYY-MM' → 'YYYY-MM-01'"""
        return ym + '-01'

    def _month_end(ym):
        """'YYYY-MM' → last day of that month"""
        import calendar as _cal
        try:
            y, m = int(ym[:4]), int(ym[5:7])
            return f'{y:04d}-{m:02d}-{_cal.monthrange(y, m)[1]:02d}'
        except (ValueError, IndexError):
            return ym + '-31'

    # Default: last 12 calendar months ending today's month (inclusive).
    # Subtract 11 from (year*12 + month-1) to land on the same month one year ago + 1.
    if not from_month or not to_month:
        today = date.today()
        to_month = today.strftime('%Y-%m')
        total = today.year * 12 + (today.month - 1) - 11
        fm_year, fm_month = divmod(total, 12)
        from_month = f'{fm_year:04d}-{fm_month + 1:02d}'

    date_from = _month_start(from_month)
    date_to   = _month_end(to_month)

    cash_rows     = cf_mod.cash_in_by_month(date_from=date_from, date_to=date_to)
    aging         = cf_mod.ar_aging()          # always point-in-time today — AR headline only
    month_compare = cf_mod.cash_vs_revenue_by_month(date_from=date_from, date_to=date_to)

    total_cash_in     = round(sum(r['cash_in'] for r in cash_rows), 2)
    total_receipts    = sum(r['receipts'] for r in cash_rows)
    total_outstanding = aging['total_outstanding']
    total_open_count  = sum(b['count'] for b in aging['buckets'])
    total_revenue     = round(sum(r['revenue'] for r in month_compare), 2)

    return render_template(
        'cashflow.html',
        cash_rows=cash_rows,
        aging=aging,
        month_compare=month_compare,
        total_cash_in=total_cash_in,
        total_receipts=total_receipts,
        total_outstanding=total_outstanding,
        total_open_count=total_open_count,
        total_revenue=total_revenue,
        from_month=from_month,
        to_month=to_month,
        date_from=date_from,
        date_to=date_to,
    )


# ── Revenue Dashboard ─────────────────────────────────────────────────────────

@bp_accounting.route('/revenue')
def revenue_dashboard():
    """Revenue dashboard: monthly revenue (accrual) + accrual-vs-cash
    side-by-side + top customers + top brands + period KPIs.

    Admin + manager only (same gating as cashflow_dashboard).
    Optional ?from=YYYY-MM&to=YYYY-MM period filter.
    Default: last 12 months ending today's month.
    """
    if session.get('role') not in ('admin', 'manager', 'shareholder'):
        flash('ต้องเข้าสู่ระบบด้วยบัญชี Admin หรือ Manager', 'danger')
        return redirect(url_for('dashboard'))

    from_month = request.args.get('from') or None
    to_month   = request.args.get('to')   or None

    def _month_start(ym):
        return ym + '-01'

    def _month_end(ym):
        import calendar as _cal
        try:
            y, m = int(ym[:4]), int(ym[5:7])
            return f'{y:04d}-{m:02d}-{_cal.monthrange(y, m)[1]:02d}'
        except (ValueError, IndexError):
            return ym + '-31'

    # Default: last 12 calendar months ending today's month (inclusive).
    # Subtract 11 from (year*12 + month-1) to land on the same month one year ago + 1.
    if not from_month or not to_month:
        today = date.today()
        to_month = today.strftime('%Y-%m')
        total = today.year * 12 + (today.month - 1) - 11
        fm_year, fm_month = divmod(total, 12)
        from_month = f'{fm_year:04d}-{fm_month + 1:02d}'

    date_from = _month_start(from_month)
    date_to   = _month_end(to_month)

    summary       = rev_mod.revenue_summary(date_from=date_from, date_to=date_to)
    revenue_rows  = cf_mod.revenue_by_month(date_from=date_from, date_to=date_to)
    cash_rows     = cf_mod.cash_in_by_month(date_from=date_from, date_to=date_to)
    top_customers = rev_mod.top_customers_by_revenue(
                        date_from=date_from, date_to=date_to, limit=20)
    top_brands    = rev_mod.top_brands_by_revenue(
                        date_from=date_from, date_to=date_to, limit=10)

    # Accrual-vs-Cash by month: full outer join in Python so gaps show as 0.
    months = sorted({r['month'] for r in revenue_rows} |
                    {r['month'] for r in cash_rows})
    rev_by_m  = {r['month']: r['revenue'] for r in revenue_rows}
    cash_by_m = {r['month']: r['cash_in']  for r in cash_rows}
    month_compare = []
    for m in months:
        rev_v  = rev_by_m.get(m, 0.0)
        cash_v = cash_by_m.get(m, 0.0)
        month_compare.append({
            'month':   m,
            'revenue': round(rev_v, 2),
            'cash_in': round(cash_v, 2),
            'gap':     round(rev_v - cash_v, 2),
        })

    total_cash_in = round(sum(r['cash_in'] for r in cash_rows), 2)

    return render_template(
        'revenue.html',
        total_revenue=summary['total_revenue'],
        total_invoices=summary['total_invoices'],
        total_customers=summary['total_customers'],
        aov=summary['aov'],
        total_cash_in=total_cash_in,
        revenue_rows=revenue_rows,
        cash_rows=cash_rows,
        month_compare=month_compare,
        top_customers=top_customers,
        top_brands=top_brands,
        from_month=from_month,
        to_month=to_month,
        date_from=date_from,
        date_to=date_to,
    )


@bp_accounting.route('/revenue/unmapped')
def revenue_unmapped_drilldown():
    """Drill into the 'ไม่ระบุแบรนด์' bucket from /revenue.

    Shows ranked list of (unmapped BSN code) + (no-brand product) items
    so mapping work can target the biggest items first. Admin + manager
    only. Optional ?from=YYYY-MM&to=YYYY-MM filter (defaults to last 12
    months — mirrors /revenue).
    """
    if session.get('role') not in ('admin', 'manager', 'shareholder'):
        flash('ต้องเข้าสู่ระบบด้วยบัญชี Admin หรือ Manager', 'danger')
        return redirect(url_for('dashboard'))

    from_month = request.args.get('from') or None
    to_month   = request.args.get('to')   or None
    limit_raw  = request.args.get('limit', '100')
    try:
        limit = max(1, min(int(limit_raw), 500))
    except ValueError:
        limit = 100

    if not from_month or not to_month:
        today = date.today()
        to_month = today.strftime('%Y-%m')
        total = today.year * 12 + (today.month - 1) - 11
        fm_year, fm_month = divmod(total, 12)
        from_month = f'{fm_year:04d}-{fm_month + 1:02d}'

    import calendar as _cal
    def _month_end(ym):
        try:
            y, m = int(ym[:4]), int(ym[5:7])
            return f'{y:04d}-{m:02d}-{_cal.monthrange(y, m)[1]:02d}'
        except (ValueError, IndexError):
            return ym + '-31'

    date_from = from_month + '-01'
    date_to   = _month_end(to_month)

    rows = rev_mod.unmapped_revenue_drilldown(
        date_from=date_from, date_to=date_to, limit=limit,
    )
    bucket_total = round(sum(r['revenue'] for r in rows), 2)

    return render_template(
        'revenue_unmapped.html',
        rows=rows,
        bucket_total=bucket_total,
        from_month=from_month,
        to_month=to_month,
        limit=limit,
    )


# ── AR Follow-up workspace ───────────────────────────────────────────────────

def _arf_require_manager():
    if session.get('role') not in ('admin', 'manager', 'shareholder'):
        flash('ต้องเข้าสู่ระบบด้วยบัญชี Admin หรือ Manager', 'danger')
        return redirect(url_for('dashboard'))
    return None


def _arf_require_admin():
    if session.get('role') != 'admin':
        flash('ต้องใช้บัญชี Admin', 'danger')
        return redirect(url_for('accounting.ar_dashboard', tab='customers'))
    return None


@bp_accounting.route('/accounting/ar-followup')
def ar_followup():
    """Redirect stub — content moved to /ar?tab=customers (AR consolidation)."""
    return redirect(url_for('accounting.ar_dashboard', tab='customers'))


@bp_accounting.route('/accounting/ar-followup/customer/<path:customer_key>')
def ar_followup_customer(customer_key):
    """Per-customer detail page. `customer_key` is the URL slug — either a
    `customer_code` (preferred, stable) or a customer name (legacy bookmark
    or orphan customer). Resolved by `arf_mod._resolve_target` inside the
    detail/followup helpers."""
    redirect_ = _arf_require_manager()
    if redirect_:
        return redirect_

    invoices = arf_mod.get_customer_ar_detail(customer=customer_key)
    followups = arf_mod.get_customer_followups(customer=customer_key)
    total_outstanding = round(sum(i['outstanding'] for i in invoices), 2)

    # Display name = name on the most recent invoice; else newest log; else key.
    if invoices:
        latest_inv = max(invoices, key=lambda i: i.get('invoice_date') or '')
        customer_name = latest_inv['customer']
        customer_code = latest_inv.get('customer_code')
    elif followups:
        customer_name = followups[0]['customer']
        customer_code = followups[0].get('customer_code')
    else:
        customer_name = customer_key
        customer_code = None

    return render_template(
        'ar_followup_detail.html',
        customer_key=customer_key,
        customer_name=customer_name,
        customer_code=customer_code,
        invoices=invoices,
        followups=followups,
        total_outstanding=total_outstanding,
        today=date.today().isoformat(),
    )


@bp_accounting.route('/accounting/ar-followup/log/new', methods=['POST'])
def ar_followup_log_new():
    redirect_ = _arf_require_admin()
    if redirect_:
        return redirect_

    customer = request.form.get('customer', '').strip()
    customer_code = (request.form.get('customer_code') or '').strip() or None
    # Redirect target = URL slug of the detail page. Prefer customer_code
    # (stable) over name; fall back to customer_key form field for legacy
    # bookmarks; finally fall back to the name.
    customer_key = (request.form.get('customer_key') or '').strip() \
                   or customer_code or customer
    if not customer:
        flash('ระบุชื่อลูกค้าไม่ถูกต้อง', 'danger')
        return redirect(url_for('accounting.ar_dashboard', tab='customers'))

    def _f(name):
        v = request.form.get(name, '').strip()
        return v or None

    promised_amount = _f('promised_amount')
    try:
        promised_amount = float(promised_amount.replace(',', '')) if promised_amount else None
    except ValueError:
        promised_amount = None

    try:
        arf_mod.log_outreach(
            customer=customer,
            customer_code=customer_code,
            log_date=_f('log_date') or date.today().isoformat(),
            channel=request.form.get('channel', 'phone'),
            contact_person=_f('contact_person'),
            result=request.form.get('result', 'other'),
            promised_amount=promised_amount,
            promised_date=_f('promised_date'),
            next_action_date=_f('next_action_date'),
            notes=_f('notes'),
            created_by=session.get('display_name') or session.get('role') or 'admin',
        )
        flash('บันทึกการติดตามแล้ว', 'success')
    except sqlite3.IntegrityError as e:
        flash(f'ข้อมูลไม่ถูกต้อง: {e}', 'danger')

    return redirect(url_for('accounting.ar_followup_customer', customer_key=customer_key))


@bp_accounting.route('/accounting/ar-followup/log/<int:log_id>/delete', methods=['POST'])
def ar_followup_log_delete(log_id):
    redirect_ = _arf_require_admin()
    if redirect_:
        return redirect_

    customer_key = (request.form.get('customer_key') or '').strip()
    arf_mod.delete_outreach(log_id=log_id)
    flash('ลบรายการแล้ว', 'success')
    if customer_key:
        return redirect(url_for('accounting.ar_followup_customer', customer_key=customer_key))
    return redirect(url_for('accounting.ar_dashboard', tab='customers'))


@bp_accounting.route('/accounting/ar-followup/export.csv')
def ar_followup_export():
    redirect_ = _arf_require_manager()
    if redirect_:
        return redirect_

    rows = arf_mod.customer_ranking()
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    buf.write('﻿')  # BOM so Excel reads UTF-8 Thai correctly
    w = _csv.writer(buf)
    w.writerow(['ลูกค้า', 'รหัส', '#ใบ', 'ยอดค้างรวม',
                'อายุสูงสุด(วัน)', '0-30', '31-60', '61-90', '90+',
                'ติดตามล่าสุด', 'ผลล่าสุด', 'นัดหมายถัดไป'])
    for r in rows:
        b = r['age_buckets']
        w.writerow([r['customer'], r.get('customer_code') or '',
                    r['invoice_count'], f'{r["outstanding"]:.2f}',
                    r['oldest_age_days'],
                    f'{b["0-30"]:.2f}', f'{b["31-60"]:.2f}',
                    f'{b["61-90"]:.2f}', f'{b["90+"]:.2f}',
                    r.get('last_log_date') or '',
                    r.get('last_log_result') or '',
                    r.get('next_action_date') or ''])

    from flask import Response
    fname = f'ar_followup_{date.today().strftime("%Y%m%d")}.csv'
    return Response(buf.getvalue().encode('utf-8'), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename={fname}'})
