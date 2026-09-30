"""Sales blueprint — trade dashboard, sales/purchases views and doc detail,
and the payment-status redirect stubs.

Extracted verbatim from app.py (behavior-preserving split) — see app.py's
module docstring for the overall file-split rationale. No URL changes;
route rules are unchanged, only their endpoint names gain a `sales.`
prefix.
"""
import sqlite3
from datetime import date

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, session)

import book_registry
import database
import line_unit_correction as luc
import models
import vat_math
from blueprints.bsn import import_running
from paging import paging

bp_sales = Blueprint('sales', __name__)


# ── Sales View ────────────────────────────────────────────────────────────────

@bp_sales.route('/trade-dashboard')
def trade_dashboard():
    date_from = request.args.get('date_from') or None
    date_to   = request.args.get('date_to')   or None
    stats = models.get_trade_dashboard(date_from, date_to,
                                       conn=book_registry.get_book_connection())
    return render_template('trade_dashboard.html', stats=stats)


@bp_sales.route('/sales')
def sales_view():
    today = date.today()
    default_from = today.replace(day=1).isoformat()
    default_to   = today.isoformat()
    pid_raw   = request.args.get('product_id', '').strip()
    product_id = int(pid_raw) if pid_raw.isdigit() else None
    if product_id:
        default_from = '2020-01-01'
        default_to   = today.isoformat()
    date_from = request.args.get('date_from', '').strip() or default_from
    date_to   = request.args.get('date_to',   '').strip() or default_to
    vat_raw   = request.args.get('vat_type',  '').strip()
    vat_type  = int(vat_raw) if vat_raw.isdigit() else None
    doc_no    = request.args.get('doc_no', '').strip() or None
    page, per_page = paging(request.args)

    conn = book_registry.get_book_connection()
    filter_product = models.get_product(product_id, conn=conn) if product_id else None

    rows, total = models.get_sales(
        product_id=product_id, date_from=date_from, date_to=date_to,
        vat_type=vat_type, page=page, per_page=per_page, doc_no=doc_no,
        conn=conn
    )
    summary = models.get_sales_summary(date_from=date_from, date_to=date_to,
                                        doc_no=doc_no, conn=conn)
    pages   = (total + per_page - 1) // per_page

    # Build summary dict keyed by vat_type (convert Row → plain dict)
    vat_summary = {r['vat_type']: dict(r) for r in summary}

    return render_template('sales.html',
                           rows=rows, total=total, pages=pages, page=page,
                           date_from=date_from, date_to=date_to,
                           vat_type=vat_type, vat_summary=vat_summary,
                           doc_no=doc_no,
                           product_id=product_id, filter_product=filter_product,
                           pending_map=len(models.get_pending_mappings(conn=conn)))


# ── Sales Doc Detail ─────────────────────────────────────────────────────────

@bp_sales.route('/sales/doc/<doc_base>')
def sales_doc(doc_base):
    conn = book_registry.get_book_connection()
    rows = models.get_sales_by_doc(doc_base, conn=conn)
    if not rows:
        # Inside the layout (#501): its banner carries the book switch, and a
        # bare string is a dead end in the standalone PWA.
        return render_template('book_link.html', mode='not_found',
                               entity=f'เอกสาร {doc_base}',
                               back_url=url_for('sales.sales_view')), 404
    total_net = sum(r['net'] or 0 for r in rows)
    # None on the VAT book: corrections exist in the main book only, and a
    # document number can exist in both.
    unit_badges = (luc.badges_for_doc(conn, doc_base)
                   if book_registry.active_book() == book_registry.DEFAULT_BOOK else None)
    return render_template('sales_doc.html', rows=rows, doc_base=doc_base,
                           total_net=total_net, vat_rate=vat_math.VAT_RATE,
                           unit_badges=unit_badges,
                           non_stock_codes=sorted(models.NON_STOCK_BSN_CODES),
                           audit_history=models.get_source_doc_audit_history(
                               doc_base, 'sales_transactions', conn=conn),
                           pending_map=len(models.get_pending_mappings(conn=conn)))


# ── แก้หน่วยบรรทัด (#692, ADR 0021) ───────────────────────────────────────────
# Every rule and every number is line_unit_correction's. These routes read the
# form, call it and show what it said. The VAT book never reaches them: the
# book guard refuses a non-parity page and any POST before the route runs.

_IMPORT_BUSY = 'กำลังนำเข้าข้อมูลจาก Express อยู่ ลองใหม่อีกครั้งในอีกสักครู่'
_WACC_FAILED = 'คำนวณต้นทุนไม่สำเร็จ ระบบจึงไม่บันทึกการแก้หน่วย (ดูหน้าแจ้งเตือน)'


def _field(source, key):
    return source[key].strip() if key in source else None


def _acting_admin():
    """Who is really writing: an admin simulating another role (ADR 0003)
    still reaches these routes, and the record must name the admin."""
    return session.get('_real_username') or session.get('username')


def _unit_correction_page(conn, doc_no, bsn_code, *, chosen_unit=None,
                          effect=None, refused=None):
    line = luc.line_view(conn, doc_no, bsn_code)
    if line is None:
        return render_template('book_link.html', mode='not_found',
                               entity=f'บรรทัด {doc_no or ""}',
                               back_url=url_for('sales.sales_view')), 404
    corrections = luc.corrections_for_line(conn, doc_no, bsn_code)
    return render_template(
        'sales_unit_correction.html', line=line,
        active=next((c for c in corrections if c['status'] == 'active'), None),
        history=[c for c in corrections if c['status'] != 'active'],
        chosen_unit=chosen_unit, effect=effect, refused=refused,
        min_reason=luc.MIN_REASON)


@bp_sales.route('/sales/unit-correction')
def unit_correction():
    conn = database.get_connection()
    try:
        return _unit_correction_page(conn, _field(request.args, 'doc_no'),
                                     _field(request.args, 'bsn_code'))
    finally:
        conn.close()


@bp_sales.route('/sales/unit-correction/preview', methods=['POST'])
def unit_correction_preview():
    doc_no, bsn_code = _field(request.form, 'doc_no'), _field(request.form, 'bsn_code')
    unit = _field(request.form, 'corrected_unit')
    conn = database.get_connection()
    try:
        effect = refused = None
        try:
            effect = luc.preview(conn, doc_no, bsn_code, unit)
        except luc.Refused as exc:
            refused = str(exc)
        return _unit_correction_page(conn, doc_no, bsn_code, chosen_unit=unit,
                                     effect=effect, refused=refused)
    finally:
        conn.close()


def _attempt(write):
    """Run `write()`: (its result, None) when it was written, else
    (None, the message to flash)."""
    if import_running():
        return None, _IMPORT_BUSY
    try:
        return write(), None
    except luc.Refused as exc:
        return None, str(exc)
    except models.WaccIdentityError:
        return None, _WACC_FAILED
    except sqlite3.OperationalError as exc:
        if 'locked' not in str(exc):
            raise
        return None, _IMPORT_BUSY


@bp_sales.route('/sales/unit-correction/apply', methods=['POST'])
def unit_correction_apply():
    doc_no, bsn_code = _field(request.form, 'doc_no'), _field(request.form, 'bsn_code')
    unit = _field(request.form, 'corrected_unit')
    conn = database.get_connection()
    try:
        new_id, error = _attempt(lambda: luc.apply(
            conn, doc_no, bsn_code, unit, _field(request.form, 'stock_mode'),
            _field(request.form, 'reason'), _acting_admin()))
        written = luc.correction(conn, new_id) if error is None else None
    finally:
        conn.close()
    if error:
        flash(error, 'danger')
        if not doc_no or not bsn_code:
            return redirect(url_for('sales.sales_view'))
        return redirect(url_for('sales.unit_correction', doc_no=doc_no, bsn_code=bsn_code))
    flash(f'แก้หน่วยบรรทัด {doc_no} เป็น "{unit}" แล้ว', 'success')
    return redirect(url_for('sales.sales_doc', doc_base=written['doc_base']))


@bp_sales.route('/sales/unit-correction/cancel', methods=['POST'])
def unit_correction_cancel():
    raw_id = _field(request.form, 'correction_id') or ''
    # isdecimal, not isdigit ('²' is a digit int() refuses); 18 digits is
    # the most a SQLite integer bind always takes.
    correction_id = int(raw_id) if raw_id.isdecimal() and len(raw_id) <= 18 else None
    conn = database.get_connection()
    try:
        c = luc.correction(conn, correction_id)
        _none, error = _attempt(lambda: luc.cancel(
            conn, correction_id, _field(request.form, 'reason'), _acting_admin()))
    finally:
        conn.close()
    if error:
        flash(error, 'danger')
        if c is None:
            return redirect(url_for('sales.sales_view'))
        return redirect(url_for('sales.unit_correction',
                                doc_no=c['doc_no'], bsn_code=c['bsn_code']))
    flash(f'ยกเลิกการแก้หน่วยบรรทัด {c["doc_no"]} แล้ว', 'success')
    return redirect(url_for('sales.sales_doc', doc_base=c['doc_base']))


# ── Purchases View ────────────────────────────────────────────────────────────

@bp_sales.route('/purchases')
def purchases_view():
    today = date.today()
    default_from = today.replace(day=1).isoformat()
    default_to   = today.isoformat()
    date_from = request.args.get('date_from', '').strip() or default_from
    date_to   = request.args.get('date_to',   '').strip() or default_to
    vat_raw   = request.args.get('vat_type',  '').strip()
    vat_type  = int(vat_raw) if vat_raw.isdigit() else None
    doc_no    = request.args.get('doc_no', '').strip() or None
    page, per_page = paging(request.args)

    conn = book_registry.get_book_connection()
    rows, total = models.get_purchases(
        date_from=date_from, date_to=date_to,
        page=page, per_page=per_page, vat_type=vat_type, doc_no=doc_no,
        conn=conn
    )
    pages = (total + per_page - 1) // per_page
    summary = models.get_purchases_summary(date_from=date_from, date_to=date_to,
                                            doc_no=doc_no, conn=conn)
    vat_summary = {r['vat_type']: dict(r) for r in
                   models.get_purchases_summary_by_vat(date_from, date_to, doc_no,
                                                       conn=conn)}

    return render_template('purchases.html',
                           rows=rows, total=total, pages=pages, page=page,
                           date_from=date_from, date_to=date_to,
                           vat_type=vat_type, vat_summary=vat_summary,
                           doc_no=doc_no,
                           summary=summary,
                           pending_map=len(models.get_pending_mappings(conn=conn)))


# ── Purchases Doc Detail ─────────────────────────────────────────────────────

@bp_sales.route('/purchases/doc/<doc_base>')
def purchases_doc(doc_base):
    conn = book_registry.get_book_connection()
    rows = models.get_purchases_by_doc(doc_base, conn=conn)
    if not rows:
        return render_template('book_link.html', mode='not_found',
                               entity=f'เอกสาร {doc_base}',
                               back_url=url_for('sales.purchases_view')), 404
    total_net = sum(r['net'] or 0 for r in rows)
    return render_template('purchases_doc.html', rows=rows, doc_base=doc_base,
                           total_net=total_net,
                           non_stock_codes=sorted(models.NON_STOCK_BSN_CODES),
                           audit_history=models.get_source_doc_audit_history(
                               doc_base, 'purchase_transactions', conn=conn),
                           pending_map=len(models.get_pending_mappings(conn=conn)))


# ── Payment Status ────────────────────────────────────────────────────────────

@bp_sales.route('/payment-status')
def payment_status():
    """Redirect stub — content moved to /ar?tab=invoices (AR consolidation)."""
    return redirect(url_for('accounting.ar_dashboard', tab='invoices'))


@bp_sales.route('/payment-status/customers')
def payment_customers():
    """Redirect stub — content moved to /ar?tab=customers (AR consolidation)."""
    return redirect(url_for('accounting.ar_dashboard', tab='customers'))
