"""Call-card blueprint — /call worklist + /call/<customer_code> card.

P3+P4: read-only UI.  P5: note/flag/tags/next-call/contact/log-delete POSTs.
"""
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, session)

from database import get_connection
import call_card as cc
import models
import ar_followup as arf_mod
from customer_geo import REGION_ORDER

bp_call = Blueprint('call', __name__)


def _ar_badge_map(conn):
    """Return {customer_key: outstanding_amount} for customers with outstanding > 0.

    Uses arf_mod.customer_ranking() — one query for all customers.
    Keys: customer_code when present, else customer name (mirrors call_card canonical key).
    We show the badge whenever outstanding > 0.
    """
    try:
        ranking = arf_mod.customer_ranking(conn=conn, min_outstanding=0)
    except Exception:
        return {}
    result = {}
    for row in ranking:
        outstanding = row.get('outstanding') or 0
        if outstanding and float(outstanding) > 0:
            # Prefer customer_code as key (canonical); fall back to name for orphans
            key = row.get('customer_code') or row.get('customer') or ''
            if key:
                result[key] = float(outstanding)
            # Also index by name so we can match either way
            name = row.get('customer') or ''
            if name and name != key:
                result[name] = float(outstanding)
    return result


@bp_call.route('/call')
def call_list():
    conn = get_connection()

    rows = cc.get_call_list(
        conn,
        q=request.args.get('q', '').strip() or None,
        region=request.args.get('region', '').strip() or None,
        call=request.args.get('call', '').strip() or None,
        spend_window=request.args.get('spend_window', '1y'),
        sort=request.args.get('sort', 'spend'),
        sp=request.args.get('sp', '').strip() or None,
    )

    # AR badge — ONE call for all rows, then map by customer name
    ar_map = _ar_badge_map(conn)
    for r in rows:
        # ar_aging keys on customer name; try both name and code
        overdue = ar_map.get(r['name']) or ar_map.get(r['customer_code']) or 0
        r['badges']['ar'] = round(overdue, 2) if overdue else 0

    salespersons = models.get_active_salespersons()
    conn.close()

    return render_template(
        'call/list.html',
        rows=rows,
        regions=REGION_ORDER,
        salespersons=salespersons,
        args=request.args,
        elapsed_th=cc.elapsed_th,
        status_label=cc.STATUS_LABEL,
    )


# P5: use <path:customer_code> so orphan canonical keys containing '/' don't 404
@bp_call.route('/call/<path:customer_code>')
def call_card(customer_code):
    conn = get_connection()
    data = cc.get_card(conn, customer_code)
    conn.close()
    if not data:
        flash('ไม่พบลูกค้า', 'warning')
        return redirect(url_for('call.call_list'))
    return render_template(
        'call/card.html',
        d=data,
        status_label=cc.STATUS_LABEL,
        elapsed_th=cc.elapsed_th,
        customer_code=customer_code,
    )


@bp_call.route('/call/<path:customer_code>/mark-called', methods=['POST'])
def call_mark_called(customer_code):
    conn = get_connection()
    cc.mark_called(conn, customer_code, session.get('username'))
    conn.close()
    flash('บันทึกว่าโทรแล้ววันนี้', 'success')
    return redirect(request.referrer or url_for('call.call_list'))


# ── P5 write routes ────────────────────────────────────────────────────────────

@bp_call.route('/call/<path:customer_code>/note', methods=['POST'])
def call_note(customer_code):
    conn = get_connection()
    kind = 'data_flag' if request.form.get('flag') else 'note'
    body = request.form.get('body', '').strip()
    cc.add_log(conn, customer_code, kind, body, session.get('username'))
    conn.close()
    flash('บันทึกแล้ว', 'success')
    return redirect(url_for('call.call_card', customer_code=customer_code))


@bp_call.route('/call/<path:customer_code>/crm', methods=['POST'])
def call_crm(customer_code):
    conn = get_connection()
    f = request.form
    fields = {}
    if 'tags' in f:
        fields['tags'] = f.get('tags', '').strip()
    if 'next_call_date' in f:
        fields['next_call_date'] = f.get('next_call_date') or None
    if 'call_target_days' in f:
        raw = f.get('call_target_days', '').strip()
        fields['call_target_days'] = int(raw) if raw else None
    if fields:
        cc.upsert_crm(conn, customer_code, session.get('username'), **fields)
    conn.close()
    flash('บันทึก CRM แล้ว', 'success')
    return redirect(url_for('call.call_card', customer_code=customer_code))


@bp_call.route('/call/<path:customer_code>/contact', methods=['POST'])
def call_contact(customer_code):
    conn = get_connection()
    f = request.form
    cur = conn.execute(
        'UPDATE customers SET phone=?, contact=?, address=?, fax=?, nickname=? WHERE code=?',
        (f.get('phone', '').strip() or None,
         f.get('contact', '').strip() or None,
         f.get('address', '').strip() or None,
         f.get('fax', '').strip() or None,
         f.get('nickname', '').strip() or None,
         customer_code),
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        flash('ลูกค้านี้ไม่มีระเบียนหลัก แก้ข้อมูลติดต่อไม่ได้', 'warning')
    else:
        flash('แก้ข้อมูลติดต่อแล้ว', 'success')
    return redirect(url_for('call.call_card', customer_code=customer_code))


@bp_call.route('/call/log/<int:log_id>/delete', methods=['POST'])
def call_log_delete(log_id):
    conn = get_connection()
    cc.soft_delete_log(conn, log_id, session.get('username'))
    code = request.form.get('customer_code', '')
    conn.close()
    return redirect(url_for('call.call_card', customer_code=code))
