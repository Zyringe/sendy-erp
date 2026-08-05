"""bp_vat_sub — VAT-substitute lookup + curation + planning page.

URL prefix: /vat-sub

  GET  /vat-sub                          → search landing
  GET  /vat-sub/product/<int:id>         → lookup result for X (own-stock
                                            card §4.1, candidate pool §5,
                                            guess section §5)
  GET  /vat-sub/planning                 → all-groups view (§4.9)
  GET  /vat-sub/group/<int:id>           → one group's management surface
                                            (§4.5/§5: view members+links,
                                            add/remove/move member, link/
                                            unlink product, rename, delete)
  POST /vat-sub/promote
  POST /vat-sub/group/<int:id>/add-member
  POST /vat-sub/group/<int:id>/move-member
  POST /vat-sub/group/<int:id>/remove-member
  POST /vat-sub/group/<int:id>/link-product
  POST /vat-sub/group/<int:id>/unlink-product
  POST /vat-sub/group/<int:id>/rename
  POST /vat-sub/group/<int:id>/delete

View = admin/manager/shareholder (mirrors bp_reconcile._require_manager —
shareholder already sees cost/profit everywhere else in the app). All 8
POSTs are manager-whitelist only (access_control._MANAGER_POST_OK) — cost
data is sensitive and this is a curation surface, not a read (plan decision
12: manager+ only, no staff variant).

Lives in MAIN view (session's active_book toggle never applies here — the
VAT book is cross-read via models.open_vat_book(), independent of the
parity-page system, plan §4.10).
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, session

import book_registry
import models
from database import get_connection

bp_vat_sub = Blueprint('vat_sub', __name__, url_prefix='/vat-sub')


def _require_manager():
    if session.get('role') not in ('admin', 'manager', 'shareholder'):
        flash('ต้องเข้าสู่ระบบด้วยบัญชี Admin หรือ Manager', 'danger')
        return redirect(url_for('dashboard'))
    return None


def _flash_result(result, ok_message):
    if result.get('ok'):
        flash(result.get('message') or ok_message, 'success')
    else:
        flash(result.get('error') or 'ทำรายการไม่สำเร็จ', 'danger')


def _group_choices(conn, group_ids):
    if not group_ids:
        return []
    placeholders = ','.join('?' * len(group_ids))
    return conn.execute(
        f"SELECT id, label FROM vat_sub_groups WHERE id IN ({placeholders}) ORDER BY id",
        group_ids).fetchall()


# ── GET pages ────────────────────────────────────────────────────────────

@bp_vat_sub.route('')
def index():
    redirect_ = _require_manager()
    if redirect_:
        return redirect_
    return render_template('vat_sub/index.html',
                           vat_freshness=book_registry.vat_book_freshness())


@bp_vat_sub.route('/product/<int:product_id>')
def product_view(product_id):
    redirect_ = _require_manager()
    if redirect_:
        return redirect_
    conn = get_connection()
    book_conn = models.open_vat_book()
    try:
        product = models.get_product(product_id, conn=conn)
        if product is None:
            flash('ไม่พบสินค้านี้', 'danger')
            return redirect(url_for('vat_sub.index'))
        own_card = models.get_own_stock_card(product_id, conn, book_conn)
        candidates = models.get_candidates(product_id, conn, book_conn)
        exclude = {c['xp5_code'] for c in candidates}
        guesses = models.get_guesses(product_id, conn, book_conn, exclude_codes=exclude)
        unit_options = models.get_unit_options(product_id, conn)
        group_ids = [r['group_id'] for r in conn.execute(
            "SELECT group_id FROM vat_sub_product_links WHERE product_id=? ORDER BY group_id",
            (product_id,))]
        group_choices = _group_choices(conn, group_ids)
    finally:
        conn.close()
        if book_conn is not None:
            book_conn.close()
    return render_template('vat_sub/product_view.html', product=product, own_card=own_card,
                           candidates=candidates, guesses=guesses, unit_options=unit_options,
                           group_choices=group_choices,
                           vat_freshness=book_registry.vat_book_freshness())


@bp_vat_sub.route('/planning')
def planning():
    redirect_ = _require_manager()
    if redirect_:
        return redirect_
    conn = get_connection()
    book_conn = models.open_vat_book()
    try:
        groups = models.list_all_groups(conn, book_conn)
    finally:
        conn.close()
        if book_conn is not None:
            book_conn.close()
    return render_template('vat_sub/planning.html', groups=groups,
                           vat_freshness=book_registry.vat_book_freshness())


@bp_vat_sub.route('/group/<int:group_id>')
def group_detail(group_id):
    redirect_ = _require_manager()
    if redirect_:
        return redirect_
    conn = get_connection()
    book_conn = models.open_vat_book()
    try:
        group = models.get_group_detail(group_id, conn, book_conn)
        other_groups = conn.execute(
            "SELECT id, label FROM vat_sub_groups WHERE id != ? ORDER BY id",
            (group_id,)).fetchall()
    finally:
        conn.close()
        if book_conn is not None:
            book_conn.close()
    if group is None:
        flash('ไม่พบกลุ่มนี้', 'danger')
        return redirect(url_for('vat_sub.planning'))
    return render_template('vat_sub/group_detail.html', group=group, other_groups=other_groups,
                           vat_freshness=book_registry.vat_book_freshness())


# ── POSTs (manager-whitelist only, see access_control._MANAGER_POST_OK) ────

@bp_vat_sub.route('/promote', methods=['POST'])
def promote():
    product_id = request.form.get('product_id', type=int)
    xp5_code = (request.form.get('xp5_code') or '').strip()
    target_raw = (request.form.get('target_group_id') or '').strip()
    if target_raw == 'new':
        target_group_id = 'new'
    elif target_raw:
        try:
            target_group_id = int(target_raw)
        except ValueError:
            target_group_id = None
    else:
        target_group_id = None
    if not product_id or not xp5_code:
        flash('ข้อมูลไม่ครบ', 'danger')
        return redirect(url_for('vat_sub.index'))
    result = models.promote(product_id, xp5_code, target_group_id=target_group_id)
    if result.get('ok') and result.get('noop'):
        flash('มีอยู่ในกลุ่มนี้แล้ว', 'success')
    elif result.get('ok') and result.get('created_group'):
        flash('สร้างกลุ่มใหม่และเพิ่มสินค้าทดแทนแล้ว', 'success')
    else:
        _flash_result(result, 'เพิ่มเข้ากลุ่มแล้ว')
    return redirect(url_for('vat_sub.product_view', product_id=product_id))


@bp_vat_sub.route('/group/<int:group_id>/add-member', methods=['POST'])
def group_add_member(group_id):
    xp5_code = (request.form.get('xp5_code') or '').strip()
    result = models.add_member(group_id, xp5_code)
    _flash_result(result, 'เพิ่มสมาชิกแล้ว')
    return redirect(url_for('vat_sub.group_detail', group_id=group_id))


@bp_vat_sub.route('/group/<int:group_id>/move-member', methods=['POST'])
def group_move_member(group_id):
    xp5_code = (request.form.get('xp5_code') or '').strip()
    target_group_id = request.form.get('target_group_id', type=int)
    if target_group_id is None:
        flash('ต้องระบุกลุ่มปลายทาง', 'danger')
        return redirect(url_for('vat_sub.group_detail', group_id=group_id))
    result = models.move_member(group_id, target_group_id, xp5_code)
    _flash_result(result, 'ย้ายแล้ว')
    return redirect(url_for('vat_sub.group_detail', group_id=group_id))


@bp_vat_sub.route('/group/<int:group_id>/remove-member', methods=['POST'])
def group_remove_member(group_id):
    xp5_code = (request.form.get('xp5_code') or '').strip()
    result = models.remove_member(group_id, xp5_code)
    _flash_result(result, 'ลบสมาชิกแล้ว')
    return redirect(url_for('vat_sub.group_detail', group_id=group_id))


@bp_vat_sub.route('/group/<int:group_id>/link-product', methods=['POST'])
def group_link_product(group_id):
    product_id = request.form.get('product_id', type=int)
    if not product_id:
        flash('ต้องระบุสินค้า', 'danger')
        return redirect(url_for('vat_sub.group_detail', group_id=group_id))
    result = models.link_product(group_id, product_id)
    _flash_result(result, 'เชื่อมสินค้าแล้ว')
    return redirect(url_for('vat_sub.group_detail', group_id=group_id))


@bp_vat_sub.route('/group/<int:group_id>/unlink-product', methods=['POST'])
def group_unlink_product(group_id):
    product_id = request.form.get('product_id', type=int)
    result = models.unlink_product(group_id, product_id)
    _flash_result(result, 'ยกเลิกเชื่อมสินค้าแล้ว')
    return redirect(url_for('vat_sub.group_detail', group_id=group_id))


@bp_vat_sub.route('/group/<int:group_id>/rename', methods=['POST'])
def group_rename(group_id):
    label = request.form.get('label') or ''
    result = models.rename_group(group_id, label)
    _flash_result(result, 'เปลี่ยนชื่อกลุ่มแล้ว')
    return redirect(url_for('vat_sub.group_detail', group_id=group_id))


@bp_vat_sub.route('/group/<int:group_id>/delete', methods=['POST'])
def group_delete(group_id):
    result = models.delete_group(group_id)
    _flash_result(result, 'ลบกลุ่มแล้ว')
    return redirect(url_for('vat_sub.planning'))
