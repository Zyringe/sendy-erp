"""Commission blueprint — dashboard, payouts, per-salesperson drilldown,
CSV export, and admin-only override CRUD.

Extracted verbatim from app.py (behavior-preserving split) — see app.py's
module docstring for the overall file-split rationale. No URL changes;
route rules are unchanged, only their endpoint names gain a `commission.`
prefix.

Named `commission_bp.py` (not `commission.py`) because a top-level
`commission.py` engine module already exists — a same-named blueprint
module would shadow it on `import commission`.
"""
import csv
import io
from datetime import date

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, session, abort, send_file)

import models
from database import get_connection
import commission as commission_mod
import hr_queries as hrq
from blueprints.cashbook import _default_account_id_for_user

bp_commission = Blueprint('commission', __name__)


def _commission_pay_accounts_ctx(conn):
    """(pay_accounts, default_account_id) for the commission payout account
    picker (plan.md decision C4) — active non-transfer cashbook accounts +
    the logged-in user's data-entry default (mirrors the salary pay-event
    account picker on /hr/payroll/<run_id>)."""
    accounts = hrq.get_active_cashbook_accounts(conn, non_transfer_only=True)
    default_account_id = _default_account_id_for_user(conn, session.get('user_id'))
    return accounts, default_account_id


def _months_with_payment_activity():
    """Distinct YYYY-MM strings present in received_payments (non-cancelled).

    Reads the canonical receipts table (not the frozen express_payments_in
    mirror) so the /commission month dropdown surfaces May-2026 onward."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT DISTINCT substr(date_iso, 1, 7) AS ym "
        "FROM received_payments WHERE cancelled=0 ORDER BY ym DESC"
    ).fetchall()
    conn.close()
    return [r['ym'] for r in rows]


def _payable_split(year_month, sp_code):
    """(payable_this_month, excluded_because_settled) for one salesperson.

    ONE definition, imported — the dashboard and the CSV export must not answer
    "how much commission this month" differently. They already did once: #335
    changed the screen to the payable figure and left `/commission/export`
    writing the raw `total_commission`, so June 2026 rep 31 read ฿130.00 on
    screen and ฿2,036.88 in the CSV. Same class of bug as the two screens the
    reassignment work was built to keep in step.
    """
    invoices = commission_mod.get_invoice_commission_for_sp(year_month, sp_code)
    payable = round(sum(i['commission_due'] for i in invoices
                        if i['paid_status'] != 'settled'), 2)
    settled = round(sum(i['commission_due'] for i in invoices
                        if i['paid_status'] == 'settled'), 2)
    return payable, settled


@bp_commission.route('/commission')
def commission_dashboard():
    months = _months_with_payment_activity()
    if not months:
        return render_template('commission.html', rows=[], months=[], year_month='',
                               summary={}, salespersons={})

    year_month = request.args.get('month') or months[0]
    rows = commission_mod.get_commission_for_month(year_month)

    # Show all 12 salespersons even if no activity, so dashboard is stable.
    conn = get_connection()
    sp_rows = conn.execute(
        "SELECT s.code, s.name, t.code AS tier_code "
        "FROM salespersons s "
        "LEFT JOIN commission_assignments a ON a.salesperson_code = s.code "
        "LEFT JOIN commission_tiers t ON t.id = a.tier_id "
        "ORDER BY s.code"
    ).fetchall()
    pay_accounts, default_account_id = _commission_pay_accounts_ctx(conn)
    conn.close()
    sp_meta = {r['code']: dict(r) for r in sp_rows}

    activity = {r['salesperson_code']: r for r in rows}
    full_rows = []
    for code, meta in sp_meta.items():
        if code in activity:
            r = activity[code]
            r['salesperson_name'] = meta['name']
            full_rows.append(r)
        else:
            full_rows.append({
                'salesperson_code': code, 'salesperson_name': meta['name'],
                'tier_code': meta['tier_code'] or '?', 'tier_name': '',
                'own_net': 0.0, 'third_net': 0.0, 'total_net': 0.0,
                'threshold_amount': None,
                'commission_below': 0.0, 'commission_above_own': 0.0,
                'commission_above_third': 0.0, 'total_commission': 0.0,
                'receipts_count': 0, 'invoices_seen': 0, 'lines_attributed': 0,
            })
    full_rows.sort(key=lambda r: -r['total_net'])

    # Layer in paid-amount per salesperson for the month + cumulative
    # remaining (matches the drilldown's "ค้างจ่าย ถึง..." view, per
    # Put 2026-05-02). paid_amount stays month-only (= what we paid in
    # this cycle), remaining becomes cumulative through this month.
    paid_map = commission_mod.get_payouts_for_month(year_month)
    for r in full_rows:
        paid = paid_map.get(r['salesperson_code'], 0.0)
        r['paid_amount'] = paid
        # Cumulative unpaid through end of this month (mirrors drilldown)
        unpaid = commission_mod.get_invoice_commission_for_sp(
            year_month, r['salesperson_code'],
            through_month=True, only_unpaid=True)
        r['remaining'] = round(sum(i['remaining'] for i in unpaid), 2)

        # THIS MONTH's payable commission, excluding invoices closed by business
        # rule (SOLD_SETTLED_BEFORE / SETTLED_THROUGH). `total_commission` is the
        # raw tier formula over everything collected this month, so it counts
        # commission on old invoices that were settled in the employee era and
        # will never be paid again: June 2026 read ฿2,036.88 of which ฿1,906.88
        # was two 2025 invoices. Put, 2026-07-30: that number "มีค่าทางธุรกิจน้อย
        # มาก" and the page should lead with what is actually owed.
        #
        # ⚠ Per-invoice by construction, so it excludes the Tier-B
        # above-threshold bonus, which lives only at month level and has never
        # been payable through this page (the จะจ่าย box defaults to
        # `remaining`, also per-invoice). Feb 2026 rep 31: total_commission
        # ฿6,362.40 vs ฿4,927.13 across its invoices — a ฿1,435.27 bonus with no
        # row to sit on. That gap is pre-existing and NOT decided here; the
        # column is named for what it is rather than pretending to be the total.
        (r['commission_month_payable'],
         r['commission_settled_excluded']) = _payable_split(
            year_month, r['salesperson_code'])

        if r['remaining'] <= 0.05:
            # "จ่ายครบ" must mean money moved or was genuinely owed and cleared.
            # Keying this on `total_commission` claimed จ่ายครบ for 4 rows in
            # 2026 where nothing was owed AND nothing was paid — it is the raw
            # aggregate, which counts settled invoices (and leaks per-product
            # overrides to zero-rate tiers).
            if paid > 0 or r['commission_month_payable'] > 0:
                r['payout_status'] = 'paid'
            else:
                r['payout_status'] = 'none'
        elif paid > 0:
            r['payout_status'] = 'partial'
        else:
            r['payout_status'] = 'pending'

    summary = {
        'total_collected_net': sum(r['total_net'] for r in full_rows),
        'total_commission':    sum(r['total_commission'] for r in full_rows),
        # Headline card mirrors the table's "คอมเดือนนี้" so the two cannot
        # disagree — the whole point of the 2026-07-30 redesign.
        'total_commission_payable': round(
            sum(r['commission_month_payable'] for r in full_rows), 2),
        'total_paid':          sum(r['paid_amount'] for r in full_rows),
        'total_remaining':     sum(r['remaining'] for r in full_rows),
        'breached_threshold':  sum(1 for r in full_rows
                                   if r['threshold_amount']
                                   and r['total_net'] > r['threshold_amount']),
    }
    today = date.today().isoformat()
    return render_template('commission.html',
                           rows=full_rows, months=months, year_month=year_month,
                           summary=summary, today=today,
                           pay_accounts=pay_accounts, default_account_id=default_account_id)


@bp_commission.route('/commission/payout', methods=['POST'])
def commission_record_payout():
    """Record commission payouts.

    Two modes:
    1. Bulk per-invoice — form has invoice_no[] checkbox values, plus a
       hidden sp_code (one salesperson at a time). Used by the drill-down
       "tick invoices to mark paid" form. amount per invoice = remaining
       commission_due (computed by engine, sent as amount_<invoice>).
    2. Bulk per-salesperson — form has sp_code[] checkbox values, plus
       per-sp amount field amount_<sp>. Used by the /commission month
       overview (legacy form, still supported for whole-month payouts
       without per-invoice tracking).
    """
    year_month  = request.form.get('month', '').strip()
    paid_date   = request.form.get('paid_date') or date.today().isoformat()
    paid_method = request.form.get('paid_method', '').strip()
    note        = request.form.get('note', '').strip()
    paid_by     = session.get('username', '')
    redirect_to = request.form.get('redirect_to') or url_for('commission.commission_dashboard',
                                                              month=year_month)

    # Pay-from account (plan.md decision C4): form override → else the logged-in
    # user's data-entry default. Validated ONCE up front — an invalid/missing
    # account inserts nothing (no partial batch, no auto-post to a bad account).
    conn = get_connection()
    account_id_raw = request.form.get('account_id', '').strip()
    if account_id_raw.isdigit():
        account_id = int(account_id_raw)
    else:
        account_id = _default_account_id_for_user(conn, session.get('user_id'))
    account = conn.execute(
        "SELECT id FROM cashbook_accounts WHERE id=? AND is_active=1 AND is_transfer=0",
        (account_id,),
    ).fetchone() if account_id else None
    conn.close()
    if account is None:
        flash('กรุณาเลือกบัญชีจ่ายเงินที่ถูกต้องและยังใช้งานอยู่', 'danger')
        return redirect(redirect_to)

    # Mode 1: per-invoice tick-list
    inv_list = request.form.getlist('invoice_no')
    if inv_list:
        sp_code = request.form.get('sp_code', '').strip()
        if not sp_code:
            flash('ขาด sp_code', 'danger')
            return redirect(redirect_to)
        inserted = 0
        errors = []
        for inv in inv_list:
            amt_raw = request.form.get(f'amount_{inv}', '').strip()
            if not amt_raw:
                continue
            try:
                amt = float(amt_raw.replace(',', ''))
            except ValueError:
                continue
            if amt <= 0:
                continue
            # Stamp the payout with the invoice's commission cycle (= month of
            # the receipt that earned it), NOT the page the user ticked from.
            # Paying May commission from the June drill-down must still land in
            # May, or the May page shows phantom รอจ่าย (the per-invoice paid
            # lookup only counts year_month <= the selected month).
            cycle_ym = commission_mod.get_invoice_cycle_month(sp_code, inv) \
                or year_month
            try:
                commission_mod.record_payout(
                    year_month=cycle_ym, salesperson_code=sp_code,
                    amount_paid=amt, paid_date=paid_date,
                    paid_method=paid_method, note=note, paid_by=paid_by,
                    invoice_no=inv, account_id=account_id,
                )
                inserted += 1
            except ValueError as e:
                errors.append(str(e))
        for e in errors:
            flash(e, 'danger')
        if inserted:
            flash(f'บันทึกการจ่าย commission แล้ว {inserted} ใบ', 'success')
        elif not errors:
            flash('ไม่ได้บันทึก (ยอดเป็น 0 หรือว่างเปล่า)', 'warning')
        return redirect(redirect_to)

    # Mode 2: per-salesperson (legacy month overview form)
    sp_codes = request.form.getlist('sp_code')
    if not sp_codes:
        single = request.form.get('sp_code')
        if single:
            sp_codes = [single]
    inserted = 0
    errors = []
    for sp in sp_codes:
        amt_raw = request.form.get(f'amount_{sp}', '').strip() \
                  or request.form.get('amount', '').strip()
        if not amt_raw:
            continue
        try:
            amt = float(amt_raw.replace(',', ''))
        except ValueError:
            continue
        if amt <= 0:
            continue
        try:
            commission_mod.record_payout(
                year_month=year_month, salesperson_code=sp,
                amount_paid=amt, paid_date=paid_date,
                paid_method=paid_method, note=note, paid_by=paid_by,
                account_id=account_id,
            )
            inserted += 1
        except ValueError as e:
            errors.append(str(e))
    for e in errors:
        flash(e, 'danger')
    if inserted:
        flash(f'บันทึกการจ่าย commission แล้ว {inserted} รายการ', 'success')
    elif not errors:
        flash('ไม่ได้บันทึก (เลือกจำนวน + ยอดให้ถูก)', 'warning')
    return redirect(redirect_to)


@bp_commission.route('/commission/payout/<int:payout_id>/delete', methods=['POST'])
def commission_delete_payout(payout_id):
    conn = get_connection()
    row = conn.execute(
        'SELECT year_month FROM commission_payouts WHERE id = ?',
        (payout_id,)
    ).fetchone()
    conn.close()
    actor = session.get('display_name') or session.get('username') or ''
    commission_mod.delete_payout(payout_id, actor=actor)
    flash('ลบรายการจ่ายแล้ว', 'success')
    if row:
        return redirect(url_for('commission.commission_payouts_list', month=row['year_month']))
    return redirect(url_for('commission.commission_payouts_list'))


@bp_commission.route('/commission/payouts')
def commission_payouts_list():
    year_month = request.args.get('month', '').strip()
    sp_code = request.args.get('sp', '').strip()
    payouts = commission_mod.get_payout_history(
        year_month=year_month or None, salesperson_code=sp_code or None
    )
    months = _months_with_payment_activity()
    conn = get_connection()
    sp_rows = conn.execute('SELECT code, name FROM salespersons ORDER BY code').fetchall()
    conn.close()
    return render_template('commission_payouts.html',
                           payouts=payouts,
                           year_month=year_month, sp_code=sp_code,
                           months=months,
                           salespersons=[dict(r) for r in sp_rows],
                           total=sum(p['amount_paid'] for p in payouts))


@bp_commission.route('/commission/sp/<sp_code>/invoice/<invoice_no>')
def commission_invoice_detail(sp_code, invoice_no):
    year_month = request.args.get('month', '').strip()
    if not year_month:
        months = _months_with_payment_activity()
        year_month = months[0] if months else ''
    header, lines = commission_mod.get_invoice_line_breakdown(
        year_month, sp_code, invoice_no)
    conn = get_connection()
    sp_row = conn.execute('SELECT name FROM salespersons WHERE code = ?',
                          (sp_code,)).fetchone()
    conn.close()
    sp_name = sp_row['name'] if sp_row else sp_code
    return render_template('commission_invoice_detail.html',
                           sp_code=sp_code, sp_name=sp_name,
                           year_month=year_month,
                           header=header, lines=lines)


@bp_commission.route('/commission/sp/<sp_code>')
def commission_drilldown(sp_code):
    months = _months_with_payment_activity()
    year_month = request.args.get('month') or (months[0] if months else '')
    if not year_month:
        return render_template('commission_drilldown.html',
                               sp_code=sp_code, sp_name=sp_code, year_month='',
                               lines=[], invoices=[], months=months, summary=None)
    lines = commission_mod.get_lines_for_salesperson(year_month, sp_code)
    summary_rows = commission_mod.get_commission_for_month(year_month, sp_code)
    summary = summary_rows[0] if summary_rows else None
    # Group lines by invoice for nicer display
    inv_map = {}
    for ln in lines:
        inv = inv_map.setdefault(ln['invoice_no'], {
            'invoice_no': ln['invoice_no'],
            'receipt_no': ln['receipt_no'],
            'receipt_date': ln['receipt_date'],
            'customer_name': ln['customer_name'],
            'lines': [],
            'own_net': 0.0,
            'third_net': 0.0,
        })
        inv['lines'].append(ln)
        if ln['brand_kind'] == 'own':
            inv['own_net'] += ln['line_net'] or 0
        else:
            inv['third_net'] += ln['line_net'] or 0
    invoices = sorted(inv_map.values(),
                      key=lambda i: (i['receipt_date'] or '', i['invoice_no']),
                      reverse=True)
    conn = get_connection()
    sp_row = conn.execute('SELECT name FROM salespersons WHERE code = ?',
                          (sp_code,)).fetchone()
    pay_accounts, default_account_id = _commission_pay_accounts_ctx(conn)
    conn.close()
    sp_name = sp_row['name'] if sp_row else sp_code
    # Per-invoice commission for the "tick to mark paid" workflow.
    # Through-month + only-unpaid: on the drill-down, picking month X
    # shows EVERY unpaid invoice with receipt-date ≤ end of X (carryover
    # included). Invoices already fully paid are hidden — Put doesn't
    # need to see them when settling.
    invoice_commissions = commission_mod.get_invoice_commission_for_sp(
        year_month, sp_code, through_month=True, only_unpaid=True)
    # Cumulative-remaining for the "คงเหลือ" summary card so it matches
    # what Put would see in the tick-list (Put 2026-05-02). Per-month
    # remaining was confusing because old-cycle carry-over wasn't
    # reflected.
    cumulative_remaining = sum(i['remaining'] for i in invoice_commissions)

    # Sort the per-invoice list per ?sort= and ?order= (default: receipt_date desc).
    sort_col = request.args.get('sort', 'receipt_date')
    sort_order = request.args.get('order', 'desc')
    SORT_KEYS = {
        'invoice_date':   lambda r: (r.get('invoice_date') or '', r['invoice_no']),
        'receipt_date':   lambda r: (r.get('receipt_date') or '', r['invoice_no']),
        'invoice_no':     lambda r: r['invoice_no'],
        'commission_due': lambda r: r['commission_due'],
    }
    keyfn = SORT_KEYS.get(sort_col, SORT_KEYS['receipt_date'])
    invoice_commissions.sort(key=keyfn, reverse=(sort_order == 'desc'))

    # All invoices issued in target month for this salesperson (paid + unpaid)
    all_invoices = commission_mod.get_invoices_for_salesperson(year_month, sp_code)
    payouts = commission_mod.get_payout_history(year_month=year_month,
                                                salesperson_code=sp_code)
    paid_amount = sum(p['amount_paid'] for p in payouts)
    return render_template('commission_drilldown.html',
                           sp_code=sp_code, sp_name=sp_name,
                           year_month=year_month, months=months,
                           invoices=invoices, summary=summary,
                           invoice_commissions=invoice_commissions,
                           all_invoices=all_invoices,
                           payouts=payouts,
                           paid_amount=paid_amount,
                           cumulative_remaining=cumulative_remaining,
                           sort_col=sort_col, sort_order=sort_order,
                           today=date.today().isoformat(),
                           pay_accounts=pay_accounts, default_account_id=default_account_id)


@bp_commission.route('/commission/export')
def commission_export():
    year_month = request.args.get('month') or ''
    if not year_month:
        abort(400)
    rows = commission_mod.get_commission_for_month(year_month)
    buf = io.StringIO()
    w = csv.writer(buf)
    # `commission_month_payable` FIRST: it is the number the page shows and the
    # number that can actually be paid. `total_commission` is kept after it as
    # audit detail (raw tier formula, counts settled invoices) — dropping it
    # would lose the reconciliation trail, but leading with it is what made the
    # CSV disagree with the screen.
    w.writerow(['salesperson_code', 'tier', 'own_net', 'third_net', 'total_net',
                'threshold', 'commission_month_payable',
                'commission_settled_excluded',
                'commission_below', 'commission_above_own',
                'commission_above_third', 'total_commission_raw',
                'receipts', 'invoices', 'lines'])
    for r in rows:
        payable, settled = _payable_split(year_month, r['salesperson_code'])
        w.writerow([r['salesperson_code'], r['tier_code'],
                    f"{r['own_net']:.2f}", f"{r['third_net']:.2f}",
                    f"{r['total_net']:.2f}", r['threshold_amount'] or '',
                    f"{payable:.2f}", f"{settled:.2f}",
                    f"{r['commission_below']:.2f}",
                    f"{r['commission_above_own']:.2f}",
                    f"{r['commission_above_third']:.2f}",
                    f"{r['total_commission']:.2f}",
                    r['receipts_count'], r['invoices_seen'], r['lines_attributed']])
    out = buf.getvalue().encode('utf-8-sig')  # BOM for Excel-Thai
    return send_file(io.BytesIO(out), mimetype='text/csv',
                     as_attachment=True,
                     download_name=f'commission_{year_month}.csv')


# ── Commission Overrides (admin-only CRUD) ───────────────────────────────────
# Rules sit in commission_overrides; the engine reads them fresh per computation
# (commission._load_overrides has no cache), so writes here are picked up
# automatically (multi-worker safe). clear_override_cache() is a retained no-op.

def _require_admin():
    if session.get('role') != 'admin':
        abort(403)


def _safe_clear_override_cache():
    """Best-effort clear_override_cache() after a write. Now a no-op (the engine
    reads overrides fresh per computation), kept so the write paths stay
    unchanged. Must not raise — a 500 after a successful DB write isn't OK."""
    try:
        commission_mod.clear_override_cache()
    except Exception as e:
        flash(f'บันทึกแล้ว แต่ refresh cache ล้มเหลว: {e}. รีสตาร์ท Sendy ถ้าค่ายังไม่อัปเดต',
              'warning')


@bp_commission.route('/commission/overrides')
def commission_overrides_list():
    _require_admin()
    rules = models.list_commission_overrides(active_only=False)
    return render_template('commission_overrides_list.html', rules=rules)


@bp_commission.route('/commission/overrides/new', methods=['GET', 'POST'])
def commission_overrides_new():
    _require_admin()
    if request.method == 'POST':
        result = models.create_commission_override(request.form)
        if result['ok']:
            _safe_clear_override_cache()
            flash(f'เพิ่ม rule #{result["id"]} เรียบร้อย', 'success')
            return redirect(url_for('commission.commission_overrides_list'))
        flash(f'ไม่สามารถบันทึก: {result["error"]}', 'danger')

    return render_template(
        'commission_overrides_form.html',
        rule=None,
        form=request.form if request.method == 'POST' else None,
        products=models.get_products(per_page=10000)[0],
        brands=models.get_brands(),
        salespersons=models.get_active_salespersons(),
    )


@bp_commission.route('/commission/overrides/<int:override_id>/edit', methods=['GET', 'POST'])
def commission_overrides_edit(override_id):
    _require_admin()
    rule = models.get_commission_override(override_id)
    if not rule:
        abort(404)

    if request.method == 'POST':
        result = models.update_commission_override(override_id, request.form)
        if result['ok']:
            _safe_clear_override_cache()
            flash(f'อัปเดต rule #{override_id} เรียบร้อย', 'success')
            return redirect(url_for('commission.commission_overrides_list'))
        flash(f'ไม่สามารถบันทึก: {result["error"]}', 'danger')

    return render_template(
        'commission_overrides_form.html',
        rule=rule,
        form=request.form if request.method == 'POST' else None,
        products=models.get_products(per_page=10000)[0],
        brands=models.get_brands(),
        salespersons=models.get_active_salespersons(),
    )


@bp_commission.route('/commission/overrides/<int:override_id>/toggle', methods=['POST'])
def commission_overrides_toggle(override_id):
    _require_admin()
    result = models.toggle_commission_override(override_id)
    if result['ok']:
        _safe_clear_override_cache()
        state = 'active' if result['is_active'] else 'inactive'
        flash(f'rule #{override_id} → {state}', 'success')
    else:
        flash(f'ไม่สามารถ toggle: {result["error"]}', 'danger')
    return redirect(url_for('commission.commission_overrides_list'))


@bp_commission.route('/commission/overrides/<int:override_id>/delete', methods=['POST'])
def commission_overrides_delete(override_id):
    _require_admin()
    result = models.delete_commission_override(override_id)
    if result['ok']:
        _safe_clear_override_cache()
        flash(f'ลบ rule #{override_id} เรียบร้อย', 'success')
    else:
        flash(f'ไม่สามารถลบ: {result["error"]}', 'danger')
    return redirect(url_for('commission.commission_overrides_list'))


# ── Customer reassignment (migration 143) ────────────────────────────────────
# Deliberately mounted UNDER /commission/ rather than as a new top-level page:
# a navigable top-level page needs its link in three surfaces (base.html,
# _mobile_drawer.html, and access_control._ENDPOINT_MODULE), and missing the
# third silently collapses the whole module sidebar on that page. Nested under
# an existing commission endpoint, it inherits the module mapping already in
# place.

def _flash_master_sync(result):
    """Tell Put when the customer master moved with the rule. Silent when it
    was already correct — a no-op message every save is noise."""
    to = result.get('master_synced_to')
    if to:
        flash(f'อัปเดตทะเบียนลูกค้าให้เป็นเซลส์ {to} ตามกฎนี้ด้วยแล้ว', 'info')


def _flash_paid_cycle_warnings(result):
    """A reassignment never rewrites commission_payouts, so a rule dated back
    into an already-paid cycle silently turns it into an overpayment. Put may
    want that, so warn loudly rather than block."""
    for w in result.get('paid_cycle_warnings') or []:
        flash(
            f'⚠️ กฎนี้ดึงยอดออกจากรอบ {w["year_month"]} ของเซลส์ {w["losing_rep"]} '
            f'ซึ่งจ่ายคอมไปแล้ว ฿{w["amount_paid"]:,.2f} — รอบนั้นจะกลายเป็น "จ่ายเกิน". '
            f'ถ้าไม่ได้ตั้งใจ ให้เลื่อนวันเริ่มมีผลให้หลังรอบนั้น',
            'warning')


def _reassign_customer_choices():
    """Every customer, for the form's <datalist>. ~1,500 rows is small enough to
    inline (it renders as plain <option> text) and avoids adding a search
    endpoint for one admin-only form."""
    rows, _ = models.get_customers_master(per_page=100000)
    return rows


@bp_commission.route('/commission/reassign')
def commission_reassign_list():
    _require_admin()
    return render_template(
        'commission_reassign_list.html',
        rules=models.list_customer_reassignments(active_only=False),
    )


@bp_commission.route('/commission/reassign/new', methods=['GET', 'POST'])
def commission_reassign_new():
    _require_admin()
    if request.method == 'POST':
        result = models.create_customer_reassignment(request.form)
        if result['ok']:
            flash(f'เพิ่มกฎย้ายลูกค้า #{result["id"]} เรียบร้อย', 'success')
            _flash_paid_cycle_warnings(result)
            _flash_master_sync(result)
            return redirect(url_for('commission.commission_reassign_list'))
        flash(f'ไม่สามารถบันทึก: {result["error"]}', 'danger')

    return render_template(
        'commission_reassign_form.html',
        rule=None,
        form=request.form if request.method == 'POST' else None,
        salespersons=models.get_active_salespersons(),
        customers=_reassign_customer_choices(),
    )


@bp_commission.route('/commission/reassign/<int:reassign_id>/edit', methods=['GET', 'POST'])
def commission_reassign_edit(reassign_id):
    _require_admin()
    rule = models.get_customer_reassignment(reassign_id)
    if not rule:
        abort(404)

    if request.method == 'POST':
        result = models.update_customer_reassignment(reassign_id, request.form)
        if result['ok']:
            flash(f'อัปเดตกฎ #{reassign_id} เรียบร้อย', 'success')
            _flash_paid_cycle_warnings(result)
            _flash_master_sync(result)
            return redirect(url_for('commission.commission_reassign_list'))
        flash(f'ไม่สามารถบันทึก: {result["error"]}', 'danger')

    return render_template(
        'commission_reassign_form.html',
        rule=rule,
        form=request.form if request.method == 'POST' else None,
        salespersons=models.get_active_salespersons(),
        customers=_reassign_customer_choices(),
    )


@bp_commission.route('/commission/reassign/<int:reassign_id>/toggle', methods=['POST'])
def commission_reassign_toggle(reassign_id):
    _require_admin()
    result = models.toggle_customer_reassignment(reassign_id)
    if result['ok']:
        flash(f'กฎ #{reassign_id} → {"active" if result["is_active"] else "inactive"}', 'success')
        _flash_master_sync(result)
    else:
        flash(f'ไม่สามารถ toggle: {result["error"]}', 'danger')
    return redirect(url_for('commission.commission_reassign_list'))


@bp_commission.route('/commission/reassign/<int:reassign_id>/delete', methods=['POST'])
def commission_reassign_delete(reassign_id):
    _require_admin()
    result = models.delete_customer_reassignment(reassign_id)
    if result['ok']:
        flash(f'ลบกฎ #{reassign_id} เรียบร้อย', 'success')
        _flash_master_sync(result)
    else:
        flash(f'ไม่สามารถลบ: {result["error"]}', 'danger')
    return redirect(url_for('commission.commission_reassign_list'))
