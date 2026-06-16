"""Marketplace orders blueprint — Shopee/Lazada order-export import + dashboard.

v1 ingests orders by FILE UPLOAD (Shopee API needs a Key Account Manager; this
path needs neither API nor approval). Replaces the manual Google tracking sheet.
Kept separate from the Express accounting ledger (sales_transactions) so
marketplace revenue is not double-counted. See parse_orders.py + migration 093.
"""
import io
import math

import pandas as pd
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, jsonify, session, abort)

import config
import db_backup
import models
import marketplace_match
from database import get_connection
from parse_orders import parse_shopee_orders, parse_lazada_orders
from parse_income_transfer import (parse_shopee_income, IncomeTransferError,
                                   load_income_sheet, parse_shopee_income_fees)

bp_marketplace = Blueprint('marketplace', __name__)


def _detect_platform(columns):
    cols = set(columns)
    if 'orderItemId' in cols and 'orderNumber' in cols:
        return 'lazada'
    if 'หมายเลขคำสั่งซื้อ' in cols:
        return 'shopee'
    return None


ROW_LIMIT = 500  # dashboard shows the newest N orders; see caption in the template


@bp_marketplace.route('/marketplace')
def dashboard():
    platform = request.args.get('platform') or None
    if platform not in ('shopee', 'lazada'):
        platform = None
    return render_template(
        'marketplace/index.html',
        summary=models.get_marketplace_summary(),
        orders=models.get_marketplace_orders(platform=platform, limit=ROW_LIMIT),
        platform=platform,
        row_limit=ROW_LIMIT,
    )


@bp_marketplace.route('/marketplace/import', methods=['POST'])
def import_orders():
    f = request.files.get('order_file')
    if not f or f.filename == '':
        flash('กรุณาเลือกไฟล์ order export (.xlsx)', 'warning')
        return redirect(url_for('marketplace.dashboard'))
    try:
        df = pd.read_excel(io.BytesIO(f.read()), sheet_name=0, header=0, dtype=str)
    except Exception as e:
        flash(f'อ่านไฟล์ไม่ได้: {e}', 'danger')
        return redirect(url_for('marketplace.dashboard'))

    platform = _detect_platform(df.columns)
    if platform is None:
        flash('ไม่รู้จักรูปแบบไฟล์ — ต้องเป็น order export จาก Shopee หรือ Lazada', 'danger')
        return redirect(url_for('marketplace.dashboard'))

    # Rollback point before the upsert overwrites existing orders' status etc.
    _info, _err = db_backup.safe_create_backup(
        'marketplace', db_path=config.DATABASE_PATH,
        backup_dir=db_backup.default_backup_dir(config.DATABASE_PATH))
    if _err:
        flash(f'⚠️ สำรองข้อมูลก่อนนำเข้าไม่สำเร็จ ({_err}) — นำเข้าต่อโดยไม่มีจุดกู้คืน', 'warning')

    try:
        orders = (parse_shopee_orders(df) if platform == 'shopee'
                  else parse_lazada_orders(df))
        conn = get_connection()
        try:
            stats = models.import_marketplace_orders(conn, orders, f.filename)
        finally:
            conn.close()
    except Exception as e:
        flash(f'นำเข้าไม่สำเร็จ: {e}', 'danger')
        return redirect(url_for('marketplace.dashboard'))

    msg = (f"นำเข้า {platform.capitalize()} สำเร็จ: {stats['orders']} ออเดอร์, "
           f"{stats['items']} รายการ (จับคู่สินค้าได้ {stats['lines_resolved']}, "
           f"ยังไม่จับคู่ {stats['unmapped']})")
    flash(msg, 'success' if stats['unmapped'] == 0 else 'warning')
    return redirect(url_for('marketplace.dashboard', platform=platform))


@bp_marketplace.route('/marketplace/unmapped')
def unmapped():
    return render_template('marketplace/unmapped.html',
                           rows=models.get_marketplace_unmapped())


@bp_marketplace.route('/marketplace/settlement-import', methods=['POST'])
def settlement_import():
    f = request.files.get('settlement_file')
    if not f or f.filename == '':
        flash('กรุณาเลือกไฟล์ Income Transfer (.xlsx)', 'warning')
        return redirect(url_for('marketplace.settlement'))

    try:
        # Real Income Transfer files prepend a multi-row metadata banner above
        # the header row; load_income_sheet auto-detects it (header=0 would miss
        # every column). See parse_income_transfer.load_income_sheet.
        raw = f.read()
        df = load_income_sheet(io.BytesIO(raw))
        settlements = parse_shopee_income(df)
        fee_rows = parse_shopee_income_fees(df)
    except IncomeTransferError as e:
        flash(str(e), 'danger')
        return redirect(url_for('marketplace.settlement'))
    except Exception as e:
        flash(f'อ่านไฟล์ไม่ได้: {e} — ต้องเป็นไฟล์ Income Transfer จาก Shopee ค่ะ', 'danger')
        return redirect(url_for('marketplace.settlement'))

    _info, _err = db_backup.safe_create_backup(
        'marketplace_settlement', db_path=config.DATABASE_PATH,
        backup_dir=db_backup.default_backup_dir(config.DATABASE_PATH))
    if _err:
        flash(f'⚠️ สำรองข้อมูลไม่สำเร็จ ({_err}) — นำเข้าต่อโดยไม่มีจุดกู้คืน', 'warning')

    try:
        conn = get_connection()
        try:
            stats = models.upsert_marketplace_settlements(conn, settlements, f.filename)
            fee_n = models.upsert_marketplace_fees(conn, fee_rows, f.filename)
            # Settling an order sets its actual_payout — the key the IV matcher
            # needs — so auto-link orders↔Express IVs right after (idempotent,
            # never clobbers a manual confirm).
            match = marketplace_match.run_automatch(conn, 'shopee')
        finally:
            conn.close()
    except Exception as e:
        flash(f'นำเข้าไม่สำเร็จ: {e}', 'danger')
        return redirect(url_for('marketplace.settlement'))

    flash(
        f'นำเข้าสำเร็จ: อัปเดต {stats["updated"]} ออเดอร์'
        + (f', ไม่พบใน ERP {stats["not_found"]} รายการ' if stats['not_found'] else '')
        + (f', ข้ามรายการที่ยังไม่โอน (ไม่มีวันที่โอน) {stats["skipped_no_date"]} รายการ'
           if stats['skipped_no_date'] else '')
        + f' · ค่าธรรมเนียม {fee_n} ออเดอร์'
        + f' · จับคู่ใบกำกับ {match["matched"]} ใบ'
        + (f', เดายอดใกล้เคียง {match["review"]} ใบ' if match.get('review') else '')
        + (f', ยังไม่จับคู่ {match["unmatched"]} ใบ' if match['unmatched'] else ''),
        'success' if (stats['not_found'] == 0 and stats['skipped_no_date'] == 0) else 'warning'
    )
    return redirect(url_for('marketplace.settlement'))


@bp_marketplace.route('/marketplace/settlement')
def settlement():
    platform = request.args.get('platform', 'shopee')
    tab = request.args.get('tab', 'daily')  # 'daily' | 'batch'
    conn = get_connection()
    try:
        report = models.get_settlement_report(conn, platform=platform)
        batch_report = models.get_deposit_batch_report(conn=conn)
    finally:
        conn.close()
    return render_template('marketplace/settlement.html',
                           report=report, platform=platform,
                           tab=tab, batch_report=batch_report,
                           no_match_batch_id=None,
                           no_match_candidates=None,
                           no_match_deposit_amount=None)


@bp_marketplace.route('/marketplace/reconciliation')
def reconciliation():
    """Per-month reconciliation: Shopee payout ↔ matched IV billed ↔ รับชำระหนี้."""
    platform = request.args.get('platform', 'shopee')
    conn = get_connection()
    try:
        report = models.get_marketplace_reconciliation(conn, platform=platform)
    finally:
        conn.close()
    return render_template('marketplace/reconciliation.html',
                           report=report, platform=platform)


@bp_marketplace.route('/marketplace/api/order/<int:order_id>')
def api_order_detail(order_id):
    """JSON: order header + line items + matched IV, for the drill-down modal."""
    conn = get_connection()
    try:
        detail = models.get_marketplace_order_detail(conn, order_id)
    finally:
        conn.close()
    if detail is None:
        abort(404)
    return jsonify(detail)


@bp_marketplace.route('/marketplace/api/order/<int:order_id>/iv-candidates')
def api_iv_candidates(order_id):
    """JSON: candidate Express IVs (same payout, near settlement date) for the picker."""
    conn = get_connection()
    try:
        order = models.get_marketplace_order(conn, order_id)
        if order is None:
            abort(404)
        cands = marketplace_match.iv_candidates(conn, order)
    finally:
        conn.close()
    return jsonify(order_sn=order['order_sn'], candidates=cands)


@bp_marketplace.route('/marketplace/order/<int:order_id>/review-amount', methods=['POST'])
def review_amount(order_id):
    """Manager+ acknowledges (or clears) an order's billed≠payout discrepancy so it
    stops showing yellow. Gated to manager/admin via _MANAGER_POST_OK."""
    accept = request.form.get('action', 'accept') != 'clear'
    conn = get_connection()
    try:
        res = models.set_amount_review(conn, order_id, accept,
                                       reviewed_by=session.get('username'))
    finally:
        conn.close()
    if res is None:
        flash('ออเดอร์นี้ยังไม่ได้ผูกใบกำกับ', 'warning')
    elif res.get('cleared'):
        flash('ยกเลิกการตรวจแล้ว', 'success')
    else:
        flash('บันทึกว่าตรวจแล้ว (ยอดต่างนี้โอเค)', 'success')
    return redirect(url_for('marketplace.reconciliation',
                            platform=request.args.get('platform', 'shopee')))


@bp_marketplace.route('/marketplace/settlement/batch', methods=['POST'])
def settlement_batch_create():
    """Dry-run greedy match then either create+assign (hit) or show tick UI (miss).

    M1 fix: no batch row is created until we have an exact prefix match.
    On no_exact_match the form fields are carried as hidden inputs into
    /settlement/batch/manual so the operator can tick orders and create exactly
    one batch row per submission.
    """
    deposit_date   = request.form.get('deposit_date', '').strip()
    deposit_amount = request.form.get('deposit_amount', '').strip()
    bank_ref       = request.form.get('bank_ref', '').strip() or None
    note           = request.form.get('note', '').strip() or None

    if not deposit_date or not deposit_amount:
        flash('กรุณาระบุวันที่และจำนวนเงินที่โอนค่ะ', 'warning')
        return redirect(url_for('marketplace.settlement', tab='batch'))

    try:
        amount = float(deposit_amount)
    except ValueError:
        flash('จำนวนเงินต้องเป็นตัวเลขค่ะ', 'warning')
        return redirect(url_for('marketplace.settlement', tab='batch'))

    # L3: reject non-positive / non-finite amounts
    if not math.isfinite(amount) or amount <= 0:
        flash('จำนวนเงินต้องเป็นตัวเลขบวกค่ะ', 'warning')
        return redirect(url_for('marketplace.settlement', tab='batch'))

    conn = get_connection()
    try:
        # Pure dry-run — no DB writes yet.
        result = models.match_orders_to_amount(amount, conn=conn)

        if result['status'] == 'matched':
            # Exact hit — create batch and assign in one transaction.
            batch_id = models.create_payout_batch(
                deposit_date, amount,
                bank_ref=bank_ref, note=note,
                created_by=session.get('username'),
                conn=conn,
            )
            models.assign_orders_to_batch(batch_id, amount, conn=conn)
            flash(
                f'จับคู่สำเร็จ: {result["n"]} ออเดอร์ รวม ฿{result["sum"]:,.2f} '
                f'ตรงกับยอดโอน ฿{amount:,.2f} ค่ะ',
                'success',
            )
            return redirect(url_for('marketplace.settlement', tab='batch'))

        # no_exact_match — fetch page data and re-render with tick UI.
        # NO batch row created; deposit fields passed as hidden inputs.
        report = models.get_settlement_report(conn, platform='shopee')
        batch_report = models.get_deposit_batch_report(conn=conn)
    finally:
        conn.close()

    flash(
        f'ไม่พบ prefix sum ที่ตรงพอดี (prefix แต่ละอันไม่ได้เรียงต่อเนื่องพอดีกับยอดโอน) '
        f'— ติ๊กเลือก order เองด้านล่างได้เลยค่ะ ข้อมูลไม่ได้ผิด แค่ไม่มี prefix ที่ตรงพอดี '
        f'(ใกล้สุด: {result["closest_n"]} ออเดอร์ รวม ฿{result["closest_sum"]:,.2f})',
        'warning',
    )
    return render_template(
        'marketplace/settlement.html',
        report=report,
        platform='shopee',
        tab='batch',
        batch_report=batch_report,
        no_match_deposit_date=deposit_date,
        no_match_deposit_amount=amount,
        no_match_bank_ref=bank_ref or '',
        no_match_note=note or '',
        no_match_candidates=result['candidates'],
    )


@bp_marketplace.route('/marketplace/settlement/batch/manual', methods=['POST'])
def settlement_batch_manual():
    """Create-and-assign in one step from the manual-tick form.

    Hidden fields carry the original deposit metadata (deposit_date,
    deposit_amount, bank_ref, note) from the no_exact_match re-render.
    The batch row is created HERE, after the operator has chosen order_sns,
    so no orphan empty batch is ever committed.
    """
    deposit_date   = request.form.get('deposit_date', '').strip()
    deposit_amount = request.form.get('deposit_amount', '').strip()
    bank_ref       = request.form.get('bank_ref', '').strip() or None
    note           = request.form.get('note', '').strip() or None
    order_sns      = request.form.getlist('order_sns')

    if not deposit_date or not deposit_amount:
        flash('ข้อมูลก้อนเงินไม่ครบ กรุณากรอกฟอร์มใหม่ค่ะ', 'warning')
        return redirect(url_for('marketplace.settlement', tab='batch'))
    if not order_sns:
        flash('กรุณาเลือก order อย่างน้อย 1 รายการค่ะ', 'warning')
        return redirect(url_for('marketplace.settlement', tab='batch'))

    try:
        amount = float(deposit_amount)
    except ValueError:
        flash('จำนวนเงินไม่ถูกต้องค่ะ', 'warning')
        return redirect(url_for('marketplace.settlement', tab='batch'))

    conn = get_connection()
    try:
        batch_id = models.create_payout_batch(
            deposit_date, amount,
            bank_ref=bank_ref, note=note,
            created_by=session.get('username'),
            conn=conn,
        )
        models.assign_orders_manual(batch_id, order_sns, conn=conn)
    finally:
        conn.close()

    flash(
        f'บันทึก {len(order_sns)} ออเดอร์ เข้าก้อนเงินโอน #{batch_id} '
        f'(฿{amount:,.2f} วันที่ {deposit_date}) แล้วค่ะ',
        'success',
    )
    return redirect(url_for('marketplace.settlement', tab='batch'))


@bp_marketplace.route('/marketplace/settlement/batch/<int:batch_id>/assign', methods=['POST'])
def settlement_batch_assign(batch_id):
    """Manual assign: the operator checks specific order_sns from the candidate list."""
    order_sns = request.form.getlist('order_sns')
    if not order_sns:
        flash('กรุณาเลือก order อย่างน้อย 1 รายการค่ะ', 'warning')
        return redirect(url_for('marketplace.settlement', tab='batch'))

    conn = get_connection()
    try:
        models.assign_orders_manual(batch_id, order_sns, conn=conn)
    finally:
        conn.close()

    flash(f'บันทึก {len(order_sns)} ออเดอร์ เข้าก้อนเงินโอน #{batch_id} แล้วค่ะ', 'success')
    return redirect(url_for('marketplace.settlement', tab='batch'))


@bp_marketplace.route('/marketplace/settlement/batch/<int:batch_id>/delete', methods=['POST'])
def settlement_batch_delete(batch_id):
    """Unassign all orders and delete the batch."""
    conn = get_connection()
    try:
        models.unassign_batch(batch_id, conn=conn)
    finally:
        conn.close()
    flash(f'ลบก้อนเงินโอน #{batch_id} แล้วค่ะ', 'success')
    return redirect(url_for('marketplace.settlement', tab='batch'))


@bp_marketplace.route('/marketplace/settlement/baseline', methods=['POST'])
def settlement_baseline():
    """Create a one-time "ยอดยกมา" baseline batch to absorb the historical backlog.

    Field: cutoff_date (ISO date). All Shopee settled orders with
    settled_at <= cutoff_date that are not yet in any batch are absorbed.
    """
    cutoff_date = request.form.get('cutoff_date', '').strip()
    if not cutoff_date:
        flash('กรุณาระบุวันที่ตัดยอดค่ะ', 'warning')
        return redirect(url_for('marketplace.settlement', tab='batch'))

    conn = get_connection()
    try:
        result = models.create_baseline_batch(
            cutoff_date,
            created_by=session.get('username'),
            conn=conn,
        )
    finally:
        conn.close()

    flash(
        f'ตั้งยอดยกมาสำเร็จ: ดูดซับ {result["n_absorbed"]} ออเดอร์ '
        f'รวม ฿{result["sum_absorbed"]:,.2f} (ก่อน/ตรงวันที่ {cutoff_date}) ค่ะ',
        'success',
    )
    return redirect(url_for('marketplace.settlement', tab='batch'))


@bp_marketplace.route('/marketplace/order/<int:order_id>/link-iv', methods=['POST'])
def link_iv(order_id):
    """Human confirms (or overrides) the IV for one order. doc_base from the picker."""
    # The picker radios POST `doc_base`; the free-text fallback POSTs `doc_base_manual`.
    doc_base = (request.form.get('doc_base_manual') or request.form.get('doc_base') or '').strip()
    conn = get_connection()
    try:
        order = models.get_marketplace_order(conn, order_id)
        if order is None:
            abort(404)
        if not doc_base:
            flash('กรุณาเลือกหรือพิมพ์เลขใบกำกับ (IV) ค่ะ', 'warning')
        else:
            stolen = marketplace_match.link_manual(
                conn, order['platform'], order['order_sn'], doc_base,
                confirmed_by=session.get('username'))
            msg = f'ผูกออเดอร์ {order["order_sn"]} กับ {doc_base} แล้วค่ะ'
            if stolen:
                msg += f' (ปลด {doc_base} ออกจากออเดอร์ {", ".join(stolen)} — ต้องเลือกใบกำกับใหม่ให้ออเดอร์นั้น)'
            flash(msg, 'success')
    finally:
        conn.close()
    return redirect(url_for('marketplace.settlement',
                            platform=request.args.get('platform', 'shopee')))
