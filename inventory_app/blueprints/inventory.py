"""Inventory blueprint — stock alerts, manual stock adjustment, transaction
history, and product conversions (สูตรแปลงสินค้า).

Extracted verbatim from app.py (behavior-preserving split) — see app.py's
module docstring for the overall file-split rationale. No URL changes;
route rules are unchanged, only their endpoint names gain an `inventory.`
prefix.
"""
from datetime import date, datetime

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, session, abort, current_app)

import models
from database import get_connection

bp_inventory = Blueprint('inventory', __name__)


# ── Alerts ────────────────────────────────────────────────────────────────────

@bp_inventory.route('/alerts')
def alerts_view():
    alerts = models.get_stock_alerts()
    return render_template('alerts.html', alerts=alerts)


@bp_inventory.route('/products/<int:product_id>/adjust', methods=['GET', 'POST'])
def stock_adjust(product_id):
    product = models.get_product(product_id)
    if not product:
        flash('ไม่พบสินค้า', 'danger')
        return redirect(url_for('products.product_list'))

    # Pages that send the user here can pass ?next=/some/path (or hidden form
    # field) to return them to where they came from (e.g. /alerts inline
    # adjust). url_for guards prevent open-redirect; we only honour an
    # internal path (starts with '/').
    def _safe_next(default_endpoint, **kw):
        nxt = request.values.get('next') or ''
        if nxt.startswith('/') and not nxt.startswith('//'):
            return nxt
        return url_for(default_endpoint, **kw)

    REASON_LABELS = {
        'count': 'นับสต๊อก',
        'damaged': 'ชำรุด / แตกหัก',
        'lost': 'สูญหาย',
        'sample': 'ของแถม / เบิกใช้เอง',
        'correction': 'แก้ยอดผิด',
    }

    if request.method == 'POST':
        f = request.form
        # quantity
        try:
            new_qty = int(f['new_quantity'])
            if new_qty < 0:
                raise ValueError('จำนวนต้องไม่ติดลบ')
        except (KeyError, ValueError) as e:
            flash(str(e) or 'จำนวนไม่ถูกต้อง', 'danger')
            return redirect(_safe_next('products.product_detail', product_id=product_id))

        # reason -> note
        reason = f.get('reason', '')
        if reason == 'other':
            note = f.get('note_other', '').strip()
            if not note:
                flash('กรุณาระบุเหตุผล', 'danger')
                return redirect(_safe_next('products.product_detail', product_id=product_id))
        elif reason in REASON_LABELS:
            note = REASON_LABELS[reason]
        else:
            flash('กรุณาเลือกเหตุผลในการปรับยอด', 'danger')
            return redirect(_safe_next('products.product_detail', product_id=product_id))

        # date / created_at
        today = date.today().isoformat()
        if reason == 'count':
            created_at = None  # DB default = datetime('now','localtime')
        else:
            adj = f.get('adjust_date', '').strip()
            try:
                d = datetime.strptime(adj, '%Y-%m-%d').date()
            except ValueError:
                flash('วันที่ไม่ถูกต้อง', 'danger')
                return redirect(_safe_next('products.product_detail', product_id=product_id))
            if d > date.today():
                flash('วันที่ปรับต้องไม่เกินวันนี้', 'danger')
                return redirect(_safe_next('products.product_detail', product_id=product_id))
            created_at = None if adj == today else f'{adj} 00:00:00'

        # diff
        current = models.get_current_stock(product_id)
        diff = new_qty - current
        if diff == 0:
            flash('จำนวนเท่าเดิม ไม่มีการเปลี่ยนแปลง', 'info')
            return redirect(_safe_next('products.product_detail', product_id=product_id))

        models.add_transaction(product_id, 'ADJUST', diff, 'unit', note=note, created_at=created_at)
        flash(f'ปรับยอดสต็อกเป็น {new_qty} {product["unit_type"]} เรียบร้อย', 'success')
        return redirect(_safe_next('products.product_detail', product_id=product_id))

    return render_template('transactions/adjust_form.html', product=product,
                           today=date.today().isoformat())


# ── Transaction History ───────────────────────────────────────────────────────

@bp_inventory.route('/transactions')
def transaction_history():
    product_id = request.args.get('product_id', type=int)
    txn_type = request.args.get('type', '').strip() or None
    date_from = request.args.get('date_from', '').strip() or None
    date_to = request.args.get('date_to', '').strip() or None
    page = int(request.args.get('page', 1))

    txns, total = models.get_transactions(
        product_id=product_id, txn_type=txn_type,
        date_from=date_from, date_to=date_to,
        page=page, per_page=current_app.config['ITEMS_PER_PAGE']
    )
    pages = (total + current_app.config['ITEMS_PER_PAGE'] - 1) // current_app.config['ITEMS_PER_PAGE']
    return render_template('transactions/history.html',
                           txns=txns, total=total, page=page, pages=pages,
                           product_id=product_id, txn_type=txn_type,
                           date_from=date_from, date_to=date_to)


# ── Product Conversions (สูตรแปลงสินค้า) ─────────────────────────────────────

@bp_inventory.route('/conversions')
def conversion_list():
    formulas = models.get_conversion_formulas()
    recent_runs = models.get_recent_conversion_runs(limit=5)
    # "buildable now" per formula: units each formula could produce from current input stock
    _b = models.get_buildable()
    buildable = {src['formula_id']: src['qty'] for e in _b.values() for src in e['sources']}
    # Pair awareness: which formulas have a reciprocal partner (for the delete
    # confirm "delete both"), and which are pair-shaped-but-partnerless (flagged
    # "ทิศเดียว" for review). One shared connection — no per-formula reconnect.
    partners, oneway_ids = {}, set()
    conn = get_connection()
    try:
        for f in formulas:
            p = models.find_pair_partner(f['id'], conn=conn)
            if p is not None:
                partners[f['id']] = {'id': p['id'], 'name': p['name']}
            elif (f['is_active'] and f['input_count'] == 1
                  and (f['name'].startswith('[แพ็ค]') or f['name'].startswith('[แกะ]'))):
                oneway_ids.add(f['id'])
    finally:
        conn.close()
    return render_template('conversions/list.html',
                           formulas=formulas, recent_runs=recent_runs, buildable=buildable,
                           partners=partners, oneway_ids=oneway_ids)


@bp_inventory.route('/conversions/history')
def conversion_history():
    runs = models.get_recent_conversion_runs(limit=200)
    return render_template('conversions/history.html', runs=runs)


def _pair_prefill(pack_id, loose_id, ratio, direction, note):
    """Build a pair_form prefill dict from raw submitted strings, so a validation
    error re-shows the form without losing the user's input."""
    def _name(pid):
        if not pid:
            return ''
        conn = get_connection()
        try:
            r = conn.execute("SELECT product_name FROM products WHERE id=?", (pid,)).fetchone()
        finally:
            conn.close()
        return r['product_name'] if r else ''
    return {'pack_id': pack_id, 'loose_id': loose_id,
            'pack_name': _name(pack_id), 'loose_name': _name(loose_id),
            'ratio': ratio, 'direction': direction or 'both', 'note': note, 'editing': False}


@bp_inventory.route('/conversions/pair', methods=['GET', 'POST'])
def conversion_pair():
    """Pack↔loose pairing — the single place to create OR edit a conversion
    formula (the advanced builder was removed; see docs/adr/0001). Idempotent via
    models.upsert_pack_unpack_pair, so re-saving an existing pair edits it.
    GET ?from_formula=<id> reopens that pair prefilled (the list's แก้ไข button)."""
    if not session.get('role'):
        abort(403)
    if request.method == 'POST':
        pack_id   = request.form.get('pack_id', '').strip()
        loose_id  = request.form.get('loose_id', '').strip()
        ratio     = request.form.get('ratio', '').strip()
        direction = request.form.get('direction', 'both').strip()
        note      = request.form.get('note', '').strip()

        def _reshow():
            return render_template('conversions/pair_form.html',
                                   prefill=_pair_prefill(pack_id, loose_id, ratio, direction, note))
        if not pack_id or not loose_id or not ratio:
            flash('กรุณาเลือกสินค้าแพ็ค สินค้าตัวหลวม และจำนวนตัวต่อแพ็ค', 'danger')
            return _reshow()
        if pack_id == loose_id:
            flash('สินค้าแพ็คและตัวหลวมต้องไม่ใช่ตัวเดียวกัน', 'danger')
            return _reshow()
        try:
            ratio_i = int(ratio)
            if ratio_i < 1:
                raise ValueError
        except ValueError:
            flash('จำนวนตัวต่อแพ็คต้องเป็นจำนวนเต็มตั้งแต่ 1 ขึ้นไป', 'danger')
            return _reshow()
        if direction not in ('both', 'pack', 'unpack'):
            direction = 'both'
        res = models.upsert_pack_unpack_pair(int(pack_id), int(loose_id), ratio_i, direction, note)
        flash(f'บันทึกคู่แพ็ค-ตัวหลวมแล้ว (สร้าง {res["created"]} · อัปเดต {res["updated"]} สูตร)', 'success')
        return redirect(url_for('inventory.conversion_list'))

    # GET — blank (create) or prefilled from an existing formula (edit)
    prefill = None
    ff = request.args.get('from_formula', type=int)
    if ff:
        prefill = models.derive_pair_from_formula(ff)
        if prefill is None:
            flash('สูตรนี้แก้ไขผ่านหน้าจับคู่ไม่ได้ (ไม่ใช่คู่แพ็ค-ตัวหลวม)', 'warning')
            return redirect(url_for('inventory.conversion_list'))
        prefill['editing'] = True
    return render_template('conversions/pair_form.html', prefill=prefill)


@bp_inventory.route('/conversions/<int:formula_id>/run', methods=['GET', 'POST'])
def conversion_run(formula_id):
    formula, inputs = models.get_conversion_formula(formula_id)
    if not formula or not formula['is_active']:
        abort(404)
    if request.method == 'POST':
        if not session.get('role'):
            abort(403)
        try:
            multiplier   = max(1, int(request.form.get('multiplier', 1)))
        except (ValueError, TypeError):
            multiplier   = 1
        reference_no = request.form.get('reference_no', '').strip()
        extra_note   = request.form.get('note', '').strip()
        try:
            writeoff_qty = max(0, int(request.form.get('writeoff_qty') or 0))
        except (ValueError, TypeError):
            writeoff_qty = 0
        # fold a write-off reason into the note for the audit trail
        wo_reason = request.form.get('writeoff_reason', '').strip()
        if writeoff_qty and wo_reason:
            extra_note = (extra_note + ' | ' if extra_note else '') + f'ของเสีย: {wo_reason}'

        success, message, _ = models.run_conversion(formula_id, multiplier, reference_no, extra_note, writeoff_qty)
        flash(message, 'success' if success else 'danger')
        if success:
            return redirect(url_for('inventory.conversion_list'))

    return render_template('conversions/run.html', formula=formula, inputs=inputs)


@bp_inventory.route('/conversions/<int:formula_id>/delete', methods=['POST'])
def conversion_delete(formula_id):
    if not session.get('role'):
        abort(403)
    # Re-derive the partner server-side (don't trust a client id) so deleting one
    # half of a pack/unpack pair can take its reciprocal with it instead of
    # silently orphaning it. No partner → behaves exactly as before.
    also = None
    if request.form.get('delete_partner') == '1':
        partner = models.find_pair_partner(formula_id)
        if partner is not None:
            also = partner['id']
    models.delete_conversion_formula(formula_id, also_delete_id=also)
    flash('ลบสูตรทั้งคู่ (แพ็คและแกะ) เรียบร้อยแล้ว' if also is not None
          else 'ลบสูตรเรียบร้อยแล้ว', 'success')
    return redirect(url_for('inventory.conversion_list'))


@bp_inventory.route('/conversions/<int:formula_id>/deactivate', methods=['POST'])
def conversion_deactivate(formula_id):
    if session.get('role') != 'admin':
        abort(403)
    conn = get_connection()
    conn.execute("UPDATE conversion_formulas SET is_active=0 WHERE id=?", (formula_id,))
    conn.commit()
    conn.close()
    flash('ปิดใช้งานสูตรแล้ว', 'success')
    return redirect(url_for('inventory.conversion_list'))


@bp_inventory.route('/conversions/<int:formula_id>/activate', methods=['POST'])
def conversion_activate(formula_id):
    if session.get('role') != 'admin':
        abort(403)
    conn = get_connection()
    conn.execute("UPDATE conversion_formulas SET is_active=1 WHERE id=?", (formula_id,))
    conn.commit()
    conn.close()
    flash('เปิดใช้งานสูตรแล้ว', 'success')
    return redirect(url_for('inventory.conversion_list'))
