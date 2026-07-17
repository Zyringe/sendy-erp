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
import marketplace_reconcile
from database import get_connection
from parse_balance import parse_shopee_balance, load_balance_sheet, BalanceError
from marketplace_files import detect_file
from parse_orders import parse_shopee_orders, parse_lazada_orders
from parse_income_transfer import (parse_shopee_income, IncomeTransferError,
                                   load_income_sheet, parse_shopee_income_fees)
from parse_lazada_statement import (parse_lazada_statement, load_lazada_statement_csv,
                                     LazadaStatementError)
from parse_lazada_wallet import (parse_lazada_wallet, load_lazada_wallet_csv,
                                  LazadaWalletError)

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


@bp_marketplace.route('/marketplace/returns')
def returns_cancelled():
    """Returns (net_payout<0) + cancelled orders — not counted as real sales."""
    return render_template('marketplace/returns.html',
                           data=models.get_marketplace_returns_cancelled())


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
    """Two tabs:
      deposits  — bank transfers (marketplace_payouts), each expandable to its
                  orders with the matched ใบกำกับ (IV); the weekly รับชำระหนี้
                  worksheet. Folds in what the old 'daily' tab did.
      reconcile — per-month กระทบยอด: payout ↔ billed IV ↔ รับชำระ (Express).
                  Was the standalone /marketplace/reconciliation page.
    """
    platform = request.args.get('platform', 'shopee')
    tab = request.args.get('tab', 'deposits')
    if tab not in ('deposits', 'reconcile'):
        tab = 'deposits'
    conn = get_connection()
    try:
        if tab == 'reconcile':
            report = models.get_marketplace_reconciliation(conn, platform=platform)
            payout_report = payout_years = selected_year = extras = None
        else:
            report = None
            payout_years = models.get_payout_years(conn, platform=platform)
            # Default to the latest year (lightest page); 'all' shows every year.
            selected_year = request.args.get('year') or (payout_years[0] if payout_years else None)
            report_year = None if selected_year == 'all' else selected_year
            # Light summaries only; each card lazy-loads its order rows from
            # /marketplace/api/payout/<id>/orders on expand.
            payout_report = models.get_payout_summaries(conn, platform=platform, year=report_year)
            extras = models.get_deposit_tab_extras(conn, platform=platform)
        # Read-only worklist counts for the "ต้องตรวจการจับคู่ใบกำกับ" badge —
        # see /marketplace/review (build-phase 1 of marketplace-iv-matching).
        worklist_badge = models.get_iv_match_worklist(conn, platform=platform)
    finally:
        conn.close()
    return render_template('marketplace/settlement.html',
                           report=report, platform=platform, tab=tab,
                           payout_report=payout_report,
                           payout_years=payout_years,
                           worklist_badge=worklist_badge,
                           selected_year=selected_year,
                           extras=extras)


@bp_marketplace.route('/marketplace/review')
def review():
    """ต้องตรวจการจับคู่ใบกำกับ — read-only worklist (build-phase 1 of
    marketplace-iv-matching). Groups every order that isn't cleanly connected
    to an Express IV by reason (A/B/C/D, see models.get_iv_match_worklist),
    with a suggested action per group. Writes NOTHING; B/C/D rows reuse the
    existing IV picker (iv-candidates + link-iv) for the pick action."""
    platform = request.args.get('platform', 'shopee')
    if platform not in ('shopee', 'lazada'):
        platform = 'shopee'
    conn = get_connection()
    try:
        worklist = models.get_iv_match_worklist(conn, platform=platform)
    finally:
        conn.close()
    return render_template('marketplace/review.html', platform=platform, worklist=worklist)


@bp_marketplace.route('/marketplace/reconciliation')
def reconciliation():
    """Reconciliation now lives as a tab inside Settlement; keep this URL as a
    redirect so old bookmarks / links still land in the right place."""
    return redirect(url_for('marketplace.settlement', tab='reconcile',
                            platform=request.args.get('platform', 'shopee')))


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


@bp_marketplace.route('/marketplace/api/payout/<int:payout_id>/orders')
def api_payout_orders(payout_id):
    """JSON: the order rows for one bank deposit, lazy-loaded when its card is
    expanded on the deposits tab (keeps the initial page light)."""
    platform = request.args.get('platform', 'shopee')
    conn = get_connection()
    try:
        orders = models.get_payout_orders(conn, platform, payout_id)
    finally:
        conn.close()
    return jsonify({'orders': orders})


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
    return redirect(url_for('marketplace.settlement', tab='reconcile',
                            platform=request.args.get('platform', 'shopee')))


@bp_marketplace.route('/marketplace/order/<int:order_id>/review-dismiss', methods=['POST'])
def review_dismiss(order_id):
    """Manager+ acknowledges a bucket-D order that has NO Express IV (sale was
    never keyed) so it stops nagging on /marketplace/review — or undoes that
    (action=undo). Auditable + reversible; gated via _MANAGER_POST_OK."""
    undo = request.form.get('action', 'dismiss') == 'undo'
    conn = get_connection()
    try:
        if undo:
            n = models.undismiss_review_order(conn, order_id)
            flash('นำกลับเข้ารายการตรวจแล้ว' if n else 'ไม่พบรายการที่รับทราบไว้',
                  'success' if n else 'warning')
        else:
            sn = models.dismiss_review_order(
                conn, order_id,
                reason=(request.form.get('reason') or '').strip() or 'ไม่มีใบกำกับ (ทีมไม่ได้คีย์)',
                by=session.get('username'))
            if sn is None:
                abort(404)
            flash(f'รับทราบออเดอร์ {sn} แล้ว (ไม่มีใบกำกับ) — กดคืนได้จากส่วน "รับทราบแล้ว"', 'success')
    finally:
        conn.close()
    return redirect(url_for('marketplace.review',
                            platform=request.form.get('platform', 'shopee')))


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


@bp_marketplace.route('/marketplace/balance-import', methods=['POST'])
def balance_import():
    f = request.files.get('balance_file')
    if not f or f.filename == '':
        flash('กรุณาเลือกไฟล์ Seller Balance (.xlsx)', 'warning')
        return redirect(url_for('marketplace.settlement'))
    try:
        df = load_balance_sheet(io.BytesIO(f.read()))
        wallet = parse_shopee_balance(df)
    except BalanceError as e:
        flash(str(e), 'danger')
        return redirect(url_for('marketplace.settlement'))
    except Exception as e:
        flash(f'อ่านไฟล์ไม่ได้: {e}', 'danger')
        return redirect(url_for('marketplace.settlement'))
    _info, _err = db_backup.safe_create_backup(
        'marketplace_balance', db_path=config.DATABASE_PATH,
        backup_dir=db_backup.default_backup_dir(config.DATABASE_PATH))
    conn = get_connection()
    try:
        ins = models.import_wallet_txns(conn, wallet, f.filename)
        try:
            rec = marketplace_reconcile.reconcile_payouts(conn, 'shopee')
        except marketplace_reconcile.ReconcileError as e:
            flash(f'นำเข้าแล้ว {ins} รายการ แต่กระทบยอดไม่ลงตัว: {e} '
                  '(ไฟล์ Balance อาจไม่ครบช่วง) — ตรวจดูยอดโอนค่ะ', 'warning')
            return redirect(url_for('marketplace.settlement'))
    finally:
        conn.close()
    flash(f'นำเข้า Balance สำเร็จ: เพิ่ม {ins} รายการ · ยอดโอนเข้าบัญชี '
          f'{rec["payouts"]} ก้อน ({rec["orders_linked"]} ออเดอร์)'
          + (f' · ⚠ {rec["unbalanced"]} ก้อนยอดไม่ตรง รอตรวจ' if rec.get('unbalanced') else ''),
          'success')
    return redirect(url_for('marketplace.settlement'))


# Processing order within one batch. Order files MUST land before the files that
# stamp them: settlements are applied with `UPDATE marketplace_orders ... WHERE
# order_sn = ?`, so an income/statement file parsed before its order file matches
# nothing and every payout in it is dropped with no row kept to replay from.
# Arrival order can't be trusted — the browser hands files over in the OS file
# dialog's display order, which is alphabetical, and Shopee's own export names
# put `Income.*` ahead of `Order.*`.
_KIND_ORDER = {'order': 0, 'income': 1, 'laz_statement': 1,
               'balance': 2, 'laz_wallet': 2}


def _log_import(conn, filename, rows=0, skipped=0, notes=None):
    """Record one import_log row. Never let bookkeeping break an import."""
    try:
        conn.execute(
            "INSERT INTO import_log (filename, rows_imported, rows_skipped, notes)"
            " VALUES (?,?,?,?)", (filename, rows, skipped, notes))
        conn.commit()
    except Exception:
        pass


def _log_upload_batch(files):
    """Durable trace of a batch, written on entry BEFORE any parsing.

    Railway's container logs are lost on restart and every per-file handler
    below turns its error into a flash rather than a traceback, so this row is
    the only evidence a failed upload leaves behind. Written on its own
    connection so it survives whatever the batch does next.
    """
    try:
        conn = get_connection()
        try:
            _log_import(conn, 'marketplace:upload', notes=(
                f'{len(files)} ไฟล์: ' + ' | '.join(f.filename for f in files)))
        finally:
            conn.close()
    except Exception:
        pass


@bp_marketplace.route('/marketplace/upload', methods=['POST'])
def upload():
    """One box for all marketplace files; detects kind+platform and routes.

    Each file is imported independently: one bad file reports itself and the
    rest of the batch still lands. Only files that actually imported something
    are reported as success — anything skipped or failed gets its own warning.
    """
    files = request.files.getlist('files')
    files = [f for f in files if f and f.filename]
    _log_upload_batch(files)
    if not files:
        flash('กรุณาเลือกไฟล์ค่ะ', 'warning')
        return redirect(url_for('marketplace.settlement'))
    _info, _err = db_backup.safe_create_backup(
        'marketplace_upload', db_path=config.DATABASE_PATH,
        backup_dir=db_backup.default_backup_dir(config.DATABASE_PATH))

    # Detect every file up front so the batch can be ordered by dependency
    # rather than by however the browser happened to send it.
    staged = []
    for f in files:
        data = f.read()
        kind, platform = detect_file(io.BytesIO(data))
        staged.append((f.filename, data, kind, platform))
    staged.sort(key=lambda s: _KIND_ORDER.get(s[2], 9))

    # `done` counts FILES imported; `tail` holds batch-level notes (reconcile)
    # that must not inflate that count.
    done, tail, problems = [], [], []
    reconcile_platforms, automatch_platforms = set(), set()
    conn = get_connection()
    try:
        for name, data, kind, platform in staged:
            if kind is None:
                problems.append(('warning', f'⚠️ {name}: ไม่รู้จักชนิดไฟล์ — ต้องเป็นไฟล์ '
                                            'Order / Income / Balance จาก Shopee หรือ Lazada ค่ะ'))
                _log_import(conn, name, notes='marketplace:UNKNOWN')
                continue
            try:
                if kind == 'order':
                    df = pd.read_excel(io.BytesIO(data), sheet_name=0, header=0, dtype=str)
                    orders = (parse_shopee_orders(df) if platform == 'shopee'
                              else parse_lazada_orders(df))
                    s = models.import_marketplace_orders(conn, orders, name)
                    done.append(f'📦 {name}: ออเดอร์ {s["orders"]} (ใหม่), จับคู่ {s["lines_resolved"]}')
                    _log_import(conn, name, rows=s['orders'], notes=f'marketplace:order:{platform}')
                elif kind == 'income':
                    df = load_income_sheet(io.BytesIO(data))
                    ss = models.upsert_marketplace_settlements(
                        conn, parse_shopee_income(df), name)
                    fn = models.upsert_marketplace_fees(
                        conn, parse_shopee_income_fees(df), name)
                    automatch_platforms.add('shopee')
                    done.append(f'💰 {name}: ยอดโอน {ss["updated"]} · ค่าธรรมเนียม {fn} ออเดอร์')
                    _log_import(conn, name, rows=ss['updated'], skipped=ss['not_found'],
                                notes='marketplace:income:shopee')
                    if ss['not_found']:
                        problems.append(('warning',
                            f'⚠️ {name}: ไม่พบออเดอร์ {ss["not_found"]} รายการ — ยอดโอนของออเดอร์'
                            ' เหล่านี้ยังไม่ถูกบันทึก กรุณาอัปโหลดไฟล์ Order ของช่วงเดียวกัน'
                            ' แล้วอัปโหลดไฟล์ Income นี้ซ้ำอีกครั้งค่ะ'))
                elif kind == 'balance':
                    ins = models.import_wallet_txns(
                        conn, parse_shopee_balance(load_balance_sheet(io.BytesIO(data))), name)
                    reconcile_platforms.add('shopee')
                    done.append(f'🏦 {name}: รายการกระเป๋าเงิน +{ins}')
                    _log_import(conn, name, rows=ins, notes='marketplace:balance:shopee')
                elif kind == 'laz_statement':
                    df = load_lazada_statement_csv(io.BytesIO(data))
                    parsed = parse_lazada_statement(df)
                    ss = models.upsert_marketplace_settlements(
                        conn, parsed['settlements'], name, platform='lazada')
                    fn = models.upsert_marketplace_fees(
                        conn, parsed['fee_rows'], name, platform='lazada')
                    models.import_wallet_txns(
                        conn, parsed['income_rows'], name, platform='lazada')
                    automatch_platforms.add('lazada')
                    reconcile_platforms.add('lazada')
                    done.append(f'💰 {name}: Lazada ยอดโอน {ss["updated"]} · ค่าธรรมเนียม {fn} ออเดอร์')
                    _log_import(conn, name, rows=ss['updated'], skipped=ss['not_found'],
                                notes='marketplace:laz_statement')
                    if ss['not_found']:
                        problems.append(('warning',
                            f'⚠️ {name}: ไม่พบออเดอร์ {ss["not_found"]} รายการ — กรุณาอัปโหลด'
                            ' ไฟล์ Order ของช่วงเดียวกัน แล้วอัปโหลดไฟล์นี้ซ้ำอีกครั้งค่ะ'))
                    if parsed['unmapped_fee_names']:
                        problems.append(('warning',
                            f'⚠️ {name}: ค่าธรรมเนียมชื่อใหม่ที่ยังไม่รู้จัก: '
                            + ', '.join(parsed['unmapped_fee_names'])))
                elif kind == 'laz_wallet':
                    parsed = parse_lazada_wallet(load_lazada_wallet_csv(io.BytesIO(data)))
                    ins = models.import_wallet_txns(
                        conn, parsed['withdrawals'], name, platform='lazada')
                    # Per-statement settlement times re-anchor income for accurate reconcile.
                    models.upsert_lazada_settlements(conn, parsed['settlements'])
                    reconcile_platforms.add('lazada')
                    done.append(f'🏦 {name}: Lazada เงินเข้าบัญชี +{ins} รายการ '
                                f'(settlement {len(parsed["settlements"])} รอบบิล)')
                    _log_import(conn, name, rows=ins, notes='marketplace:laz_wallet')
            except Exception as e:
                problems.append(('danger', f'❌ {name}: นำเข้าไม่สำเร็จ — {e}'))
                _log_import(conn, name, notes=f'marketplace:{kind}:ERROR {e}')

        # Deferred to once per platform: run_automatch rebuilds every 'auto' row
        # on each call, so running it per-file re-did the same work N times.
        for plat in sorted(automatch_platforms):
            try:
                marketplace_match.run_automatch(conn, plat)
            except Exception as e:
                problems.append(('warning', f'⚠️ {plat}: จับคู่ใบกำกับอัตโนมัติไม่สำเร็จ: {e}'))
        for plat in sorted(reconcile_platforms):
            try:
                rec = marketplace_reconcile.reconcile_payouts(conn, plat)
                tail.append(f'↔ {plat}: กระทบยอดโอน {rec["payouts"]} ก้อน '
                            f'({rec["orders_linked"]} ออเดอร์)')
                if rec.get('unbalanced'):
                    problems.append(('warning',
                        f'⚠️ {plat}: {rec["unbalanced"]} ก้อนยอดไม่ตรง รอตรวจค่ะ'))
            except marketplace_reconcile.ReconcileError as e:
                problems.append(('warning', f'⚠️ {plat}: กระทบยอดไม่ลงตัว: {e}'))
    finally:
        conn.close()

    if done:
        flash(f'✓ นำเข้า {len(done)} ไฟล์สำเร็จ · ' + ' · '.join(done + tail), 'success')
    for category, message in problems:
        flash(message, category)
    return redirect(url_for('marketplace.settlement'))
