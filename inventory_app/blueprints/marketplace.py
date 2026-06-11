"""Marketplace orders blueprint — Shopee/Lazada order-export import + dashboard.

v1 ingests orders by FILE UPLOAD (Shopee API needs a Key Account Manager; this
path needs neither API nor approval). Replaces the manual Google tracking sheet.
Kept separate from the Express accounting ledger (sales_transactions) so
marketplace revenue is not double-counted. See parse_orders.py + migration 093.
"""
import io

import pandas as pd
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash)

import config
import db_backup
import models
from database import get_connection
from parse_orders import parse_shopee_orders, parse_lazada_orders
from parse_income_transfer import (parse_shopee_income, IncomeTransferError,
                                   load_income_sheet)

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
        df = load_income_sheet(io.BytesIO(f.read()))
        settlements = parse_shopee_income(df)
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
        finally:
            conn.close()
    except Exception as e:
        flash(f'นำเข้าไม่สำเร็จ: {e}', 'danger')
        return redirect(url_for('marketplace.settlement'))

    flash(
        f'นำเข้าสำเร็จ: อัปเดต {stats["updated"]} ออเดอร์'
        + (f', ไม่พบใน ERP {stats["not_found"]} รายการ' if stats['not_found'] else '')
        + (f', ข้ามรายการที่ยังไม่โอน (ไม่มีวันที่โอน) {stats["skipped_no_date"]} รายการ'
           if stats['skipped_no_date'] else ''),
        'success' if (stats['not_found'] == 0 and stats['skipped_no_date'] == 0) else 'warning'
    )
    return redirect(url_for('marketplace.settlement'))


@bp_marketplace.route('/marketplace/settlement')
def settlement():
    platform = request.args.get('platform', 'shopee')
    conn = get_connection()
    try:
        report = models.get_settlement_report(conn, platform=platform)
    finally:
        conn.close()
    return render_template('marketplace/settlement.html',
                           report=report, platform=platform)
