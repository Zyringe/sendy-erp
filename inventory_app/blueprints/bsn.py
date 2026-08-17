"""BSN blueprint — Express weekly import (unified box + legacy redirect),
BSN code mapping, and unit-conversions.

Extracted verbatim from app.py (behavior-preserving split) — see app.py's
module docstring for the overall file-split rationale. No URL changes;
route rules are unchanged, only their endpoint names gain a `bsn.` prefix.
"""
import datetime
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, session, jsonify, abort, current_app)

import config
import db_backup
import models
import review_rules as rr
from database import get_connection

bp_bsn = Blueprint('bsn', __name__)


# ── Weekly Import (legacy) → consolidated into /import-data ──────────────────

@bp_bsn.route('/import-weekly')
def import_weekly():
    # Legacy per-file ขาย/ซื้อ + AR/AP importer. The unified box (/import-data)
    # is a superset (auto-detects every report type, preview/confirm, snapshots
    # before writing). Kept as a redirect so old bookmarks don't 404.
    return redirect(url_for('bsn.unified_import'))


# ── Unit Conversions ──────────────────────────────────────────────────────────

def _flash_unit_hazard(b):
    if b['kind'] == 'pair':
        flash(f'"{b["product_name"]}" หน่วย {b["bsn_unit"]}: ห้ามตั้ง ratio ข้ามหน่วย — '
              f'บิลหน่วยนี้ต้อง map ไปที่ "{b["partner_name"]}" '
              f'(ใช้ "แยก mapping ตามหน่วย" ในหน้า mapping)', 'danger')
    elif b['kind'] == 'configuration_error':
        flash(f'"{b["product_name"]}" หน่วย {b["bsn_unit"]}: '
              f'บันทึกไม่ได้ — สูตรแปลงสินค้า (id {b["formula_id"]}) ผิดรูปแบบ: '
              f'{b["message"]} — แก้สูตรที่หน้าสูตรแปลงสินค้าก่อน', 'danger')
    else:  # pack_piece
        flash(f'"{b["product_name"]}" หน่วย {b["bsn_unit"]}: สินค้าสต็อกเป็น{b["product_unit"]} '
              f'บันทึกได้เฉพาะ ratio 1 (ขายทั้ง{b["product_unit"]} แต่บิลเขียนหน่วยต่าง) — '
              f'ถ้าขายแกะเป็นชิ้นจริง ต้องแยก mapping/สร้าง SKU ชิ้น', 'danger')

@bp_bsn.route('/unit-conversions')
def unit_conversions():
    search = request.args.get('q', '').strip()
    page = int(request.args.get('page', 1))
    per_page = current_app.config['ITEMS_PER_PAGE']
    pending = models.get_pending_unit_conversions(search=search or None)
    existing, total = models.get_all_unit_conversions(
        search=search or None, page=page, per_page=per_page
    )
    pages = (total + per_page - 1) // per_page
    return render_template('unit_conversions.html',
                           pending=pending, existing=existing,
                           search=search, page=page, pages=pages, total=total)


@bp_bsn.route('/unit-conversions/save', methods=['POST'])
def unit_conversions_save():
    # Pass 1: full-unit names Put typed for unknown acronyms
    # key: "fullunit_<product_id>_<acronym>"
    learned = {}                       # acronym -> full
    acr_full = {}                      # (pid_str, acronym) -> full
    for key, val in request.form.items():
        if key.startswith('fullunit_'):
            parts = key[9:].split('_', 1)
            full = (val or '').strip()
            if len(parts) == 2 and full:
                learned[parts[1]] = full
                acr_full[(parts[0], parts[1])] = full
    if learned:
        # persist to bsn_unit_full.json + normalise the whole ledger
        models.learn_acronyms_normalize(learned)

    items = []
    for key, val in request.form.items():
        # key format: "ratio_<product_id>_<bsn_unit>"
        if key.startswith('ratio_'):
            parts = key[6:].split('_', 1)
            if len(parts) == 2:
                try:
                    ratio = float(val)
                    # isfinite: `inf > 0` is True, and a ratio multiplies
                    # straight into quantity_change, so inf would poison the
                    # ledger and stock_levels beyond repair.
                    if math.isfinite(ratio) and ratio > 0:
                        pid_s, bsn_unit = parts[0], parts[1]
                        # if Put named this acronym, store conv under the
                        # FULL unit (ledger was just normalised to match)
                        bsn_unit = acr_full.get((pid_s, bsn_unit), bsn_unit)
                        items.append({'product_id': int(pid_s),
                                      'bsn_unit': bsn_unit, 'ratio': ratio})
                except (ValueError, IndexError):
                    pass
    if items:
        result = models.save_unit_conversions(items)
        if result['saved']:
            msg = f'บันทึกการแปลงหน่วย {result["saved"]} รายการเรียบร้อย'
            if learned:
                msg += (f'  |  เรียนรู้หน่วยใหม่ {len(learned)} ตัว '
                        f'(จำไว้ใช้ครั้งต่อไป)')
            flash(msg, 'success')
        for b in result['blocked']:
            _flash_unit_hazard(b)
    return redirect(url_for('bsn.unit_conversions'))


@bp_bsn.route('/unit-conversions/edit', methods=['POST'])
def unit_conversions_edit():
    product_id = request.form.get('product_id', type=int)
    bsn_unit   = request.form.get('bsn_unit', '').strip()
    new_ratio  = request.form.get('ratio', type=float)
    if (product_id and bsn_unit and new_ratio
            and math.isfinite(new_ratio) and new_ratio > 0):
        result = models.update_unit_conversion_ratio(product_id, bsn_unit, new_ratio)
        if 'blocked' in result:
            _flash_unit_hazard(result['blocked'])
        elif 'error' in result:
            flash(result['error'], 'danger')
        else:
            flash(f'อัปเดต ratio สำหรับ {bsn_unit} เรียบร้อย (re-sync แล้ว)', 'success')
    return redirect(url_for('bsn.unit_conversions'))


@bp_bsn.route('/unit-conversions/dismiss', methods=['POST'])
def unit_conversions_dismiss():
    product_id = request.form.get('product_id', type=int)
    bsn_unit   = request.form.get('bsn_unit', '').strip()
    if product_id and bsn_unit:
        deleted = models.dismiss_pending_unit_conversion(product_id, bsn_unit)
        if deleted:
            flash(f'ยกเลิก {deleted} แถวที่ยังไม่ sync ออกแล้ว (หน่วย "{bsn_unit}")', 'success')
        else:
            flash('ไม่ได้ยกเลิกรายการใด — กลุ่มนี้อาจมีบรรทัดที่เป็นรายได้จริง '
                  '(ค่าบริการ/ส่วนลด) ซึ่งระบบป้องกันไว้ไม่ให้ลบ', 'warning')
    return redirect(url_for('bsn.unit_conversions'))


# ── Product Code Mapping ──────────────────────────────────────────────────────

@bp_bsn.route('/mapping')
def mapping():
    pending = models.get_pending_mappings()
    pending_suggestions = models.get_pending_suggestions()
    conn = get_connection()
    all_products = conn.execute("""
        SELECT p.id, p.product_name, p.unit_type,
               COALESCE(s.quantity, 0) AS stock
          FROM products p
          LEFT JOIN stock_levels s ON s.product_id = p.id
         WHERE p.is_active = 1
         ORDER BY p.id
    """).fetchall()
    brands = conn.execute(
        "SELECT id, name, name_th FROM brands ORDER BY is_own_brand DESC, sort_order, name"
    ).fetchall()
    color_codes = conn.execute(
        "SELECT code, name_th FROM color_finish_codes ORDER BY sort_order, code"
    ).fetchall()
    # Standardised category master for the type-to-search picker in both the
    # approve form and the Suggest modal (replaces the old free-text field).
    categories = conn.execute(
        "SELECT id, code, name_th FROM categories ORDER BY sort_order, name_th"
    ).fetchall()
    # Suggestion sources for the free-text combo fields (unit_type / condition):
    # these stay free-text (any value allowed) but the dropdown offers the
    # values already in use so they stay consistent.
    unit_suggestions = [r[0] for r in conn.execute(
        "SELECT unit_type FROM products WHERE unit_type IS NOT NULL AND unit_type <> '' "
        "GROUP BY unit_type ORDER BY COUNT(*) DESC"
    ).fetchall()]
    conn.close()
    from sku_code_utils import CONDITION_SHORT
    condition_suggestions = list(CONDITION_SHORT.keys())
    tab = request.args.get('tab', 'mapping')
    return render_template(
        'mapping.html',
        pending=pending,
        pending_suggestions=pending_suggestions,
        pending_splits=models.get_pending_split_mappings(),
        all_products=all_products,
        brands=brands,
        color_codes=color_codes,
        categories=categories,
        unit_suggestions=unit_suggestions,
        condition_suggestions=condition_suggestions,
        active_tab=tab,
        non_stock_codes=sorted(models.NON_STOCK_BSN_CODES),
    )


@bp_bsn.route('/mapping/suggest/<bsn_code>')
def mapping_suggest(bsn_code):
    """Return JSON: top fuzzy matches + parsed fields + cost/unit
    for the smart-suggest modal on /mapping."""
    if not session.get('role'):
        abort(403)
    conn = get_connection()
    row = conn.execute(
        "SELECT bsn_code, bsn_name FROM product_code_mapping "
        "WHERE bsn_code = ? LIMIT 1",
        (bsn_code,),
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'unknown bsn_code'}), 404
    import bsn_suggest
    out = bsn_suggest.suggest_for_bsn(conn, bsn_code, row['bsn_name'])
    conn.close()
    return jsonify(out)


@bp_bsn.route('/mapping/save', methods=['POST'])
def mapping_save():
    data = request.get_json()
    user_id = session.get('user_id')
    for item in data.get('mappings', []):
        bsn_code = item.get('bsn_code')
        action   = item.get('action')       # 'map', 'ignore', 'stage'
        if action == 'map':
            pid = int(item['product_id'])
            models.upsert_mapping(bsn_code, item['bsn_name'], product_id=pid)
            # Optional: capture unit_conversion at map time when BSN unit ≠ product unit
            bsn_unit = (item.get('bsn_unit') or '').strip()
            ratio = item.get('unit_conversion_ratio')
            if bsn_unit and ratio:
                try:
                    r = float(ratio)
                except (TypeError, ValueError):
                    r = 0
                if r > 0:
                    models.upsert_unit_conversion(pid, bsn_unit, r)
        elif action == 'stage':
            # Smart-suggest flow: stage new SKU for manager/admin review
            payload = {
                'bsn_code': bsn_code,
                'bsn_name': item['bsn_name'],
                'suggested_name': item.get('suggested_name'),
                'category': item.get('category'),
                'series': item.get('series'),
                'brand_id': item.get('brand_id') or None,
                'model': item.get('model'),
                'size': item.get('size'),
                'color_th': item.get('color_th'),
                'color_code': item.get('color_code') or None,
                'packaging': item.get('packaging') or None,
                'condition': item.get('condition'),
                'pack_variant': item.get('pack_variant'),
                'suggested_cost': float(item.get('suggested_cost') or 0),
                'suggested_unit_type': item.get('suggested_unit_type') or 'ตัว',
                'units_per_carton': item.get('units_per_carton'),
                'units_per_box': item.get('units_per_box'),
                # Round-2 extras (mig 037)
                'brand_other_name': item.get('brand_other_name') or None,
                'color_code_other': item.get('color_code_other') or None,
                'packaging_other': item.get('packaging_other') or None,
                'bsn_unit': item.get('bsn_unit') or None,
                'unit_conversion_ratio': (
                    float(item['unit_conversion_ratio'])
                    if item.get('unit_conversion_ratio') else None
                ),
            }
            models.save_pending_suggestion(payload, user_id)
        elif action == 'ignore':
            try:
                models.upsert_mapping(
                    bsn_code, item['bsn_name'],
                    is_ignored=1,
                    ignore_reason=item.get('ignore_reason') or None,
                )
            except models.NonStockCodeError as exc:
                return jsonify({'ok': False, 'error': str(exc)}), 400

    # Backfill product_id on existing unlinked rows
    conn = get_connection()
    models.resolve_pending_mappings(conn)
    conn.close()

    pending_left = len(models.get_pending_mappings())
    pending_sugg = models.count_pending_suggestions()
    return jsonify({'ok': True, 'pending_left': pending_left,
                    'pending_suggestions': pending_sugg})


@bp_bsn.route('/mapping/split-save', methods=['POST'])
def mapping_split_save():
    """Repoint one unit-slice of a BSN code (the /mapping split-section row)
    onto a different product — the fix for a cross_unit_hazard-blocked row
    that /unit-conversions can't resolve with a ratio."""
    if session.get('role') not in ('admin', 'manager'):
        abort(403)
    bsn_code = (request.form.get('bsn_code') or '').strip()
    bsn_unit = (request.form.get('bsn_unit') or '').strip()
    product_id = request.form.get('product_id', type=int)
    conn = get_connection()
    product = (conn.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone()
               if product_id else None)
    conn.close()
    if not bsn_code or not bsn_unit or not product:
        flash('ข้อมูลไม่ครบ — เลือกสินค้าปลายทางก่อนบันทึก', 'danger')
        return redirect(url_for('bsn.mapping') + '#split-section')
    report = models.repoint_bsn_code(None, bsn_code, product_id, bsn_unit=bsn_unit)
    moved = report['rows_moved']['sales'] + report['rows_moved']['purchase']
    flash(f'ย้ายบิลหน่วย "{bsn_unit}" ของรหัส {bsn_code} ไปสินค้าใหม่แล้ว ({moved} แถว)', 'success')
    if report['orphan_rows_after'] != 0:
        flash(f'⚠ พบ ledger orphan {report["orphan_rows_after"]} แถวหลังย้าย — ตรวจสอบด้วย', 'danger')
    return redirect(url_for('bsn.mapping') + '#split-section')


@bp_bsn.route('/mapping/suggestions/<int:sid>/approve', methods=['POST'])
def mapping_suggestion_approve(sid):
    """Manager/admin approves a staged SKU suggestion.
    Body may include edits to override staged fields before product creation."""
    if session.get('role') not in ('admin', 'manager', 'shareholder'):
        abort(403)
    edits = request.get_json() or {}
    # cast brand_id to int if present
    if edits.get('brand_id'):
        try:
            edits['brand_id'] = int(edits['brand_id'])
        except (TypeError, ValueError):
            edits['brand_id'] = None
    # cast category_id to int if present (picker resolves name → id client-side)
    if edits.get('category_id'):
        try:
            edits['category_id'] = int(edits['category_id'])
        except (TypeError, ValueError):
            edits['category_id'] = None
    if edits.get('suggested_cost') is not None:
        try:
            edits['suggested_cost'] = float(edits['suggested_cost'])
        except (TypeError, ValueError):
            edits['suggested_cost'] = 0.0
    try:
        new_pid = models.approve_pending_suggestion(
            sid, edits, session.get('user_id')
        )
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    return jsonify({'ok': True, 'product_id': new_pid})


# Make import_express's machinery available to the upload form. We inject
# our own DB connection so the import shares this app's transaction
# semantics (lights-on FK off etc).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'scripts'))
import import_express as express_importer  # noqa: E402
import import_router  # noqa: E402  (unified /import box: detect + preview + commit dispatch)


# ── Unified import box (/import) — one drop zone for all weekly Express files ──
_IMPORT_STAGE_DIR = 'import-stage'   # under UPLOAD_FOLDER


def _snapshot_before_import(reason):
    """Best-effort full-DB snapshot right before an import commits, so an admin
    can roll the whole DB back (see /admin/backups). Never blocks the import —
    a backup-infra failure (e.g. disk full) is flashed as a warning, not fatal."""
    info, err = db_backup.safe_create_backup(
        reason, db_path=config.DATABASE_PATH,
        backup_dir=db_backup.default_backup_dir(config.DATABASE_PATH))
    if err:
        flash(f'⚠️ สำรองข้อมูลก่อนนำเข้าไม่สำเร็จ ({err}) — นำเข้าต่อโดยไม่มีจุดกู้คืน', 'warning')
    return info

_REPORT_LABELS = {
    'sales': 'ขาย',
    'purchase': 'ซื้อ',
    'payments_in': 'การรับชำระหนี้ (ลูกหนี้)',
    'payments_out': 'การจ่ายชำระหนี้ (เจ้าหนี้)',
    'credit_notes_ar': 'ใบลดหนี้ — รับคืน (ลูกค้า)',
    'credit_notes_ap': 'ใบลดหนี้ — ส่งคืน (ผู้ขาย)',
    'ar_snapshot': 'ลูกหนี้คงค้าง',
    'ap_snapshot': 'เจ้าหนี้คงค้าง',
    'unknown': '— ไม่รู้จัก (เลือกเอง) —',
}


@bp_bsn.route('/import-data', methods=['GET', 'POST'])
def unified_import():
    # POST is gated by the _STAFF_POST_OK whitelist in before_request; GET is
    # open to any logged-in role (same as /import-weekly). Staff can run imports.
    if request.method == 'POST':
        import time
        import uuid
        files = [f for f in request.files.getlist('files') if f and f.filename]
        if not files:
            flash('ยังไม่ได้เลือกไฟล์', 'danger')
            return redirect(url_for('bsn.unified_import'))
        # Prune abandoned staged dirs (a GET-cancel or re-upload never reaches
        # /confirm's cleanup, so they would otherwise leak on disk).
        stage_root = os.path.join(current_app.config['UPLOAD_FOLDER'], _IMPORT_STAGE_DIR)
        if os.path.isdir(stage_root):
            cutoff = time.time() - 3600
            for d in os.listdir(stage_root):
                old = os.path.join(stage_root, d)
                try:
                    if os.path.isdir(old) and os.path.getmtime(old) < cutoff:
                        shutil.rmtree(old, ignore_errors=True)
                except OSError:
                    pass
        token = uuid.uuid4().hex
        stage = os.path.join(stage_root, token)
        os.makedirs(stage, exist_ok=True)
        rows = []
        for i, f in enumerate(files):
            saved = f'{i}_{os.path.basename(f.filename)}'
            path = os.path.join(stage, saved)
            f.save(path)
            rtype = import_router.detect_express_report(path)
            row = {'idx': i, 'filename': f.filename, 'saved': saved,
                   'detected': rtype, 'label': _REPORT_LABELS.get(rtype, rtype),
                   'count': None, 'detail': {}, 'error': None,
                   # Set only by a SUCCESSFUL sales/purchase preview. Source-line
                   # removal is offered and honoured only for such a row: the
                   # operator must have seen the parsed count and the deletion
                   # count before they can ask for deletions.
                   'removals_ok': False}
            if rtype != 'unknown':
                try:
                    prev = import_router.preview_file(path, rtype)
                    row['count'] = prev.get('count')
                    row['detail'] = prev.get('detail') or {}
                    # payments_in joins the list once its preview also reports a
                    # removal count — without that the operator would be opting
                    # into a deletion they cannot see (Task 2 shipped inert
                    # until this line included it).
                    row['removals_ok'] = rtype in ('sales', 'purchase', 'payments_in')
                except import_router.HistoryExportBlocked as exc:
                    # Policy A: full history goes through the Express ZIP module
                    # only. Flagged (not just errored) so the template can point
                    # the operator at that page.
                    row['error'] = str(exc)
                    row['blocked'] = 'history'
                except Exception as exc:   # preview failure isolates to this file
                    row['error'] = str(exc)
            rows.append(row)
        # The signed-cookie session is ~4KB. A credit-note preview's `detail`
        # carries per-row diff lists that can blow past that → the cookie is
        # silently dropped and /confirm 'เซสชันหมดอายุ'. The staged preview is
        # rendered from the in-memory `rows` (full detail) in THIS request;
        # /confirm only needs idx/filename/saved/detected, so store slim rows.
        # `blocked` and `removals_ok` are the PREVIEW'S VERDICT and must survive
        # into /confirm: the type dropdown is operator-supplied, so a decision
        # keyed on the submitted type alone can be re-routed around (a blocked
        # history file submitted as payments_in reached models.import_payments).
        # Both are small scalars — the ~4KB cookie limit that forced this list to
        # be slimmed was about per-row diff LISTS, not flags.
        slim = [{'idx': r['idx'], 'filename': r['filename'], 'saved': r['saved'],
                 'detected': r['detected'], 'blocked': r.get('blocked'),
                 'removals_ok': r['removals_ok']} for r in rows]
        session['import_stage'] = {'token': token, 'rows': slim}
        return render_template('import_box.html', staged=True, rows=rows, token=token,
                               report_labels=_REPORT_LABELS, results=None)
    return render_template('import_box.html', staged=False, rows=None,
                           report_labels=_REPORT_LABELS, results=None)


@bp_bsn.route('/import-data/confirm', methods=['POST'])
def unified_import_confirm():
    # Gated by the _STAFF_POST_OK whitelist in before_request (staff-allowed).
    stage = session.get('import_stage') or {}
    token = stage.get('token')
    rows = stage.get('rows') or []
    if not token or request.form.get('token') != token:
        flash('เซสชันหมดอายุ กรุณาอัปโหลดใหม่', 'warning')
        return redirect(url_for('bsn.unified_import'))
    _snapshot_before_import('unified')   # rollback point before the ledger writes
    base = os.path.join(current_app.config['UPLOAD_FOLDER'], _IMPORT_STAGE_DIR, token)
    results = []
    for row in rows:
        i = row['idx']
        # Put can override a detected/unknown type via the per-row dropdown.
        rtype = request.form.get(f'type_{i}', row['detected'])
        path = os.path.join(base, row['saved'])
        # A row the PREVIEW refused stays refused, whatever type is submitted
        # now. The guard inside commit_file only runs for sales/purchase, so
        # without this the dropdown could re-route a blocked history file to
        # another importer entirely (verified: submitted as payments_in it
        # reached models.import_payments). The guard cannot simply run for every
        # type — a legitimate การรับชำระหนี้ export carries a wide วันที่จาก range
        # and would be blocked as history — so the verdict is remembered instead.
        if row.get('blocked'):
            results.append({
                'filename': row['filename'], 'ok': False,
                'msg': import_router.history_block_reason(path) or
                       'ไฟล์นี้ถูกปฏิเสธตั้งแต่ตอนตรวจสอบ — นำเข้าไม่ได้',
                'blocked': row['blocked']})
            continue
        if rtype == 'unknown' or not os.path.isfile(path):
            results.append({'filename': row['filename'], 'ok': False,
                            'msg': 'ข้าม — ไม่ได้เลือกประเภท'})
            continue
        # Source-line removal is OFF unless the operator ticked this file's
        # "complete weekly export" box on the preview page. The choice rides the
        # confirm FORM, not the signed session.
        #
        # It is honoured ONLY for a row whose sales/purchase preview succeeded
        # AND whose type has not changed since. An unpreviewed row has no parsed
        # count and no deletion count, so enabling deletions there would reverse
        # lines the operator never saw; and a sales preview's removal plan is
        # meaningless once the row is switched to purchase. The checkbox is not
        # rendered in those cases, so a `removals_N` arriving anyway is a stale
        # tab or a hand-built POST — ignored, not trusted.
        apply_removals = (bool(request.form.get(f'removals_{i}'))
                          and bool(row.get('removals_ok'))
                          and rtype == row.get('detected'))
        try:
            out = import_router.commit_file(path, rtype, filename=row['filename'],
                                            apply_removals=apply_removals)
            result_row = {'filename': row['filename'], 'ok': True,
                          'label': _REPORT_LABELS.get(rtype, rtype),
                          'summary': out.get('summary')}
            if rtype == 'sales':
                bid = (out.get('summary') or {}).get('batch_id')
                if bid:
                    try:
                        scan = rr.scan_after_import(bid)
                        result_row['review_flagged'] = scan.get('docs_flagged', 0)
                    except Exception as _scan_exc:
                        flash(f'สแกนตรวจบิลไม่สำเร็จ: {_scan_exc}', 'warning')
            results.append(result_row)
        except import_router.HistoryExportBlocked as exc:
            results.append({'filename': row['filename'], 'ok': False,
                            'msg': str(exc), 'blocked': 'history'})
        except Exception as exc:   # per-file isolation — one bad file doesn't sink the batch
            results.append({'filename': row['filename'], 'ok': False, 'msg': str(exc)})
    session.pop('import_stage', None)
    shutil.rmtree(base, ignore_errors=True)
    # Self-limit audit_log once per import flow (not per file). A big import
    # churns audit rows; the TTL prune keeps the table from bloating the volume.
    # Best-effort — a prune failure must never sink a successful import.
    try:
        models.prune_audit_log()
    except Exception as _prune_exc:
        flash(f'ตัด audit log เก่าไม่สำเร็จ: {_prune_exc}', 'warning')
    return render_template('import_box.html', staged=False, rows=None,
                           report_labels=_REPORT_LABELS, results=results)


# ── Express DBF-direct import (projects/express-integration/plan.md Phase 2) ──
# The team's end-of-day ritual: a Windows script zips ~11 Express DBF tables
# (script 1). A logged-in team member then uploads that zip through this page
# (script 2 — the old non-interactive curl upload — is retired), so the
# upload below is a normal login+CSRF Sendy POST, not a token-gated endpoint.

# A little above the observed ~30-40MB zip (plan §"Sizes") — no global
# MAX_CONTENT_LENGTH is set anywhere in this app (the existing DB-upload
# routes accept an ~80MB file uncapped), so this is a scoped safety cap
# for this endpoint rather than a raise of an existing limit.
_EXPRESS_DBF_MAX_UPLOAD_BYTES = 100 * 1024 * 1024


_EXPRESS_DBF_MAX_MEMBERS = 800
_EXPRESS_DBF_MAX_MEMBER_BYTES = 300 * 1024 * 1024        # BSN5657 STCRD ≈ 119MB
_EXPRESS_DBF_MAX_TOTAL_BYTES = 1536 * 1024 * 1024

# ISINFO identity → which import path a dataset dir feeds. Values verified
# against the real company files 2026-08-02 (BSN5657's TAXID is literally
# thirteen zeros — never classify by TAXID alone, always the pair).
_BSN5657_SIGNATURE = {'THINAM': '(BSN)บจก.บุญสวัสดิ์นำชัย', 'TAXID': '0000000000000'}


def _zip_preflight(zf):
    """Reject a hostile/oversized zip BEFORE extraction: path traversal,
    absolute members, member count, per-file and total uncompressed size.
    Returns an error string or None."""
    infos = zf.infolist()
    if len(infos) > _EXPRESS_DBF_MAX_MEMBERS:
        return f'zip มีไฟล์เกิน {_EXPRESS_DBF_MAX_MEMBERS} รายการ'
    total = 0
    for zi in infos:
        name = zi.filename.replace('\\', '/')
        if name.startswith('/') or '..' in name.split('/'):
            return f'zip มี path ไม่ปลอดภัย: {zi.filename[:80]}'
        total += zi.file_size
        if zi.file_size > _EXPRESS_DBF_MAX_MEMBER_BYTES:
            return f'ไฟล์ใน zip ใหญ่เกินไป: {zi.filename[:80]}'
        if total > _EXPRESS_DBF_MAX_TOTAL_BYTES:
            return 'ขนาดรวมหลังแตก zip ใหญ่เกินไป'
    return None


def _find_express_dbf_dataset_dirs(root):
    """ALL directories that directly hold .DBF tables inside an extracted
    zip (one per company file — the daily zip may carry BSN5657 and xp5
    together). ARTRN.DBF is mandatory for every import type, so its location
    pins each dataset dir express_dbf_source.open_table() expects."""
    dirs = []
    for dirpath, _dirnames, filenames in os.walk(root):
        if any(fn.upper() == 'ARTRN.DBF' for fn in filenames):
            dirs.append(dirpath)
    return dirs


def _classify_dataset(dataset_dir):
    """'bsn' | 'vat' | 'missing' | None from the dataset's own ISINFO
    identity — registry-driven, never guessed from folder names.

    'missing' = no readable ISINFO.DBF. The team's existing daily zip has
    never been required to carry ISINFO, so a SINGLE missing dataset falls
    back to the legacy meaning ('bsn' — the route decides); an ISINFO that
    IS present but matches no registered book is a hard None (wrong company
    file — reject, never guess)."""
    import book_registry
    import express_dbf_source as eds
    try:
        rows = eds.open_table(dataset_dir, 'ISINFO')
    except Exception:
        return 'missing'
    if not rows:
        return 'missing'
    sig = {'THINAM': str(rows[0].get('THINAM') or '').strip(),
           'TAXID': str(rows[0].get('TAXID') or '').strip()}
    if sig == _BSN5657_SIGNATURE:
        return 'bsn'
    if sig == book_registry.BOOKS['vat']['isinfo_signature']:
        return 'vat'
    return None


@bp_bsn.route('/import-express-dbf')
def express_dbf_import():
    import book_registry
    freshness = models.get_express_dbf_freshness()
    conn = get_connection()
    row = conn.execute(
        "SELECT imported_at, notes FROM import_log "
        "WHERE filename='express-dbf-upload' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    last_run = None
    if row:
        try:
            last_run = {'at': row['imported_at'],
                        'results': json.loads(row['notes'] or '{}')}
        except ValueError:
            last_run = {'at': row['imported_at'], 'results': {}}
        # A builder that died before reporting leaves 'building' forever —
        # after 30 minutes show it as stalled (retrying the upload is safe).
        vat_res = (last_run['results'].get('vat')
                   if isinstance(last_run['results'], dict) else None)
        if isinstance(vat_res, dict) and vat_res.get('status') == 'building':
            from datetime import datetime, timedelta
            try:
                started = datetime.strptime(last_run['at'], '%Y-%m-%d %H:%M:%S')
                if datetime.now() - started > timedelta(minutes=30):
                    vat_res['status'] = 'stalled'
            except (TypeError, ValueError):
                pass
    return render_template('import_express_dbf.html', freshness=freshness,
                           vat_freshness=book_registry.vat_book_freshness(),
                           last_run=last_run)


def _express_dbf_summary_message(per_type):
    """Thai one-liner for the post-upload flash — the per-type 'imported'
    (or 'upserted', for credit_notes_ar) count from commit_express_dbf()'s
    result dict."""
    sales = per_type['sales']['imported']
    purchase = per_type['purchase']['imported']
    pay_in = per_type['payments_in']['imported']
    pay_out = per_type['payments_out']['imported']
    cn_ar = per_type['credit_notes_ar']['upserted']
    cn_ap = per_type['credit_notes_ap']['imported']
    msg = (f'นำเข้าสำเร็จ — ขาย {sales}, ซื้อ {purchase}, '
           f'รับชำระ {pay_in}, จ่ายเงิน {pay_out}, '
           f'ลดหนี้ขาย {cn_ar}, ลดหนี้ซื้อ {cn_ap} รายการ')

    # Outstanding balances, appended only when this caller actually produced
    # them. Read with .get on purpose: scripts/import_express and older callers
    # build a per-type dict without these keys, and
    # test_summary_survives_a_result_dict_without_the_new_keys pins that
    # tolerance — a KeyError here turns a SUCCESSFUL import into a reported
    # failure, which is the opposite of what this message is for.
    ar_snap = (per_type.get('ar_snapshot') or {}).get('imported')
    ap_snap = (per_type.get('ap_snapshot') or {}).get('imported')
    if ar_snap is not None or ap_snap is not None:
        msg += (f' · ลูกหนี้คงค้าง {ar_snap or 0}, เจ้าหนี้คงค้าง {ap_snap or 0} ใบ '
                f'(ณ {per_type.get("snapshot_date") or "-"})')

    # This import DELETES receipt→invoice links whose receipt no longer lists
    # them (import_router.commit_express_dbf applies replacement), and it skips
    # any ARRCPIT line with an unsupported RECTYP. Both are money-path events
    # with no preview step in front of them, so neither may be silent. Appended
    # only when non-zero: the ordinary daily message must not grow permanent
    # noise, or the exceptional one stops standing out.
    removed = per_type['payments_in'].get('removed_links') or 0
    if removed:
        msg += f' · ลบลิงก์ที่ต้นทางไม่มีแล้ว {removed} รายการ'
    skipped = per_type['payments_in'].get('skipped_rectyp') or []
    if skipped:
        msg += f' · ข้ามบรรทัดที่อ่านไม่ได้ {len(skipped)} บรรทัด'
    return msg


@bp_bsn.route('/import-express-dbf/upload', methods=['POST'])
def express_dbf_upload():
    # A logged-in team member (require_login + staff POST whitelist, see
    # access_control.py) uploads the daily Express DBF zip through this
    # form — PRG pattern: flash the result and redirect back to the GET
    # page, same as every other Sendy import route.
    redirect_to = url_for('bsn.express_dbf_import')

    # Checked via the Content-Length header BEFORE touching request.files —
    # accessing request.files is what makes werkzeug parse/spool the whole
    # multipart body, so this bails before any of an oversized upload lands
    # on disk.
    if request.content_length and request.content_length > _EXPRESS_DBF_MAX_UPLOAD_BYTES:
        limit_mb = _EXPRESS_DBF_MAX_UPLOAD_BYTES // (1024 * 1024)
        flash(f'ไฟล์ใหญ่เกินไป (จำกัด {limit_mb}MB)', 'danger')
        return redirect(redirect_to)

    f = request.files.get('file')
    if not f or not f.filename:
        flash('กรุณาเลือกไฟล์ zip', 'danger')
        return redirect(redirect_to)
    if not f.filename.lower().endswith('.zip'):
        flash('ไฟล์ต้องเป็น .zip', 'danger')
        return redirect(redirect_to)

    tmpdir = tempfile.mkdtemp(prefix='express_dbf_')
    try:
        zip_path = os.path.join(tmpdir, 'upload.zip')
        f.save(zip_path)
        if not zipfile.is_zipfile(zip_path):
            flash('ไฟล์ไม่ใช่ zip ที่ถูกต้อง', 'danger')
            return redirect(redirect_to)
        with zipfile.ZipFile(zip_path) as zf:
            err = _zip_preflight(zf)
            if err:
                flash(f'ปฏิเสธไฟล์: {err}', 'danger')
                return redirect(redirect_to)
            extract_dir = os.path.join(tmpdir, 'extracted')
            zf.extractall(extract_dir)

        dataset_dirs = _find_express_dbf_dataset_dirs(extract_dir)
        if not dataset_dirs:
            flash('ไม่พบ ARTRN.DBF ใน zip ที่อัปโหลด', 'danger')
            return redirect(redirect_to)

        # Classify EVERY dataset before importing ANY (plan rev 3 P3):
        # an unknown or duplicated company file rejects the whole upload.
        # Legacy compatibility: the daily zip predates ISINFO shipping — a
        # SINGLE dataset without ISINFO keeps its historical meaning (the
        # BSN5657 book). Multiple datasets must all self-identify.
        classified = {}
        for d in dataset_dirs:
            kind = _classify_dataset(d)
            if kind == 'missing':
                if len(dataset_dirs) == 1:
                    kind = 'bsn'
                else:
                    flash('zip มีหลายชุดข้อมูลแต่บางชุดไม่มี ISINFO.DBF — '
                          'แนบ ISINFO.DBF ของทุกชุด แล้วอัปโหลดใหม่ (ยกเลิกทั้งไฟล์)',
                          'danger')
                    return redirect(redirect_to)
            if kind is None:
                flash('มีชุดข้อมูลที่ไม่รู้จักใน zip (ISINFO ไม่ตรงกับ BSN5657/xp5) '
                      '— ยกเลิกทั้งไฟล์ ไม่มีการนำเข้าใดๆ', 'danger')
                return redirect(redirect_to)
            if kind in classified:
                flash('zip มีชุดข้อมูลของสมุดเดียวกันซ้ำกัน — ยกเลิกทั้งไฟล์', 'danger')
                return redirect(redirect_to)
            classified[kind] = d

        # Per-dataset outcomes, reported separately (partial success is a
        # legitimate, honestly-reported state — the BSN path is idempotent,
        # so retrying the same zip after a VAT failure is safe).
        results = {}
        flashes = []
        # ONE as-of date for this upload, decided here and handed to both books.
        # The VAT half runs in a detached subprocess that starts minutes later and
        # can cross midnight, so letting each side call date.today() for itself is
        # how the two books end up stamped on different days.
        snapshot_date = datetime.date.today().isoformat()
        if 'bsn' in classified:
            try:
                # since_days defaults to 60 inside commit_express_dbf — a
                # daily upload only ever needs the recent window, and that window
                # is what keeps this import fast. It scopes the LEDGER only; the
                # outstanding snapshots deliberately ignore it (a balance is owed
                # regardless of the invoice's age).
                per_type = import_router.commit_express_dbf(
                    classified['bsn'], db_path=config.DATABASE_PATH,
                    snapshot_date=snapshot_date)
                results['bsn'] = {'ok': True,
                                  'summary': _express_dbf_summary_message(per_type),
                                  'reconcile': per_type.get('reconcile', {})}
                flashes.append(('success', f"BSN5657: {results['bsn']['summary']}"))
                # A snapshot that refused is isolated from the money import
                # (import_router._commit_snapshot) — the ledger above is fine,
                # but AR/AP silently keep showing YESTERDAY's balances, so say
                # so loudly rather than letting the green flash imply otherwise.
                for _key, _label in (('ar_snapshot', 'ลูกหนี้คงค้าง'),
                                     ('ap_snapshot', 'เจ้าหนี้คงค้าง')):
                    _err = (per_type.get(_key) or {}).get('error')
                    if _err:
                        results['bsn'][_key + '_error'] = _err
                        flashes.append(('warning',
                                        f'{_label}: สร้างยอดคงค้างรอบนี้ไม่สำเร็จ ({_err}) '
                                        f'— ยอดขาย/ซื้อเข้าปกติ แต่หน้ายอดคงค้างจะยังเป็นของรอบก่อน'))
                _reconcile = results['bsn']['reconcile']
                if _reconcile.get('error'):
                    # scan_reconcile failed but the money import above already
                    # committed fine — say so, don't call the whole upload
                    # failed (import_router.commit_express_dbf isolates this).
                    flashes.append(('danger',
                                    f'ตรวจสอบบิลหายจาก Express ล้มเหลว: {_reconcile["error"]} '
                                    f'— การนำเข้าปกติไม่กระทบ'))
                elif _reconcile.get('deleted', 0):
                    flashes.append(('warning',
                                    f'พบบิล {_reconcile["deleted"]} ใบที่หายไปจาก Express — '
                                    f'ตรวจที่หน้า "ตรวจสอบบิลหายจาก Express" (เมนูนำเข้าข้อมูล)'))
            except Exception as exc:
                results['bsn'] = {'ok': False, 'error': str(exc)[:400]}
                flashes.append(('danger', f'BSN5657 นำเข้าไม่สำเร็จ: {exc}'))
        if 'vat' in classified:
            results['vat'] = {'ok': None, 'status': 'building'}

        # Persist the run record FIRST so the async builder can update it.
        conn = get_connection()
        cur = conn.execute(
            "INSERT INTO import_log (filename, rows_imported, rows_skipped, notes) "
            "VALUES ('express-dbf-upload', 0, 0, ?)",
            (json.dumps(results, ensure_ascii=False),))
        run_id = cur.lastrowid
        conn.commit()
        conn.close()

        if 'vat' in classified:
            try:
                _spawn_vat_rebuild(classified['vat'], run_id, snapshot_date)
                flashes.append(('info',
                                'สมุด VAT (xp5): เริ่ม rebuild เบื้องหลังแล้ว (~2-5 นาที) '
                                '— ดูสถานะที่บรรทัด "ผลการนำเข้าล่าสุด" ด้านล่าง'))
            except Exception as exc:
                _update_run_result(run_id, 'vat',
                                   {'ok': False, 'error': str(exc)[:400]})
                flashes.append(('danger', f'สมุด VAT เริ่ม rebuild ไม่สำเร็จ: {exc}'))
    except Exception as exc:
        flash(f'นำเข้าไม่สำเร็จ: {exc}', 'danger')
        return redirect(redirect_to)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    for cat, msg in flashes:
        flash(msg, cat)
    return redirect(redirect_to)


def _update_run_result(run_id, key, value):
    conn = get_connection()
    try:
        row = conn.execute("SELECT notes FROM import_log WHERE id=?",
                           (run_id,)).fetchone()
        notes = json.loads(row['notes']) if row and row['notes'] else {}
        notes[key] = value
        conn.execute("UPDATE import_log SET notes=? WHERE id=?",
                     (json.dumps(notes, ensure_ascii=False), run_id))
        conn.commit()
    finally:
        conn.close()


# Not business state — bookkeeping so finished detached builders get reaped
# (poll() collects the zombie); bounded by in-flight builds per worker.
_VAT_BUILD_PROCS = []


def _spawn_vat_rebuild(dataset_dir, run_id, snapshot_date=None):
    """Detached full rebuild of vat_book.db (minutes — far beyond gunicorn's
    60s worker timeout, so NEVER in-request). The dataset is copied out of
    the request's tmpdir so it survives this request's cleanup; the builder
    acquires the publish lock (serializing concurrent rebuilds), publishes
    atomically, updates the run record, and removes the scratch."""
    import book_registry
    _VAT_BUILD_PROCS[:] = [p for p in _VAT_BUILD_PROCS if p.poll() is None]

    # Advisory busy-probe only — the BUILDER's flock is the real gate
    # (acquisition is atomic and the kernel releases it the instant the
    # owner dies, so there is NO stale-lock recovery and nothing ever
    # unlinks the lockfile — Codex R6: every check-then-act variant here
    # had a TOCTOU). If two spawns slip past this probe, the second
    # builder fails its own flock and reports that into its run record.
    import fcntl
    lock_path = book_registry.book_db_path('vat') + '.lock'
    if os.path.exists(lock_path):
        probe = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(probe, fcntl.LOCK_UN)
        except OSError:
            raise RuntimeError('มี rebuild สมุด VAT กำลังทำงานอยู่ '
                               '— รอให้เสร็จก่อนแล้วค่อยอัปโหลดใหม่')
        finally:
            os.close(probe)

    run_root = tempfile.mkdtemp(prefix='vat_book_run_')
    src = os.path.join(run_root, 'dataset')
    shutil.copytree(dataset_dir, src)
    data_dir = os.path.join(run_root, 'build')
    os.makedirs(data_dir)
    builder = os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', 'vat_book_builder.py'))
    env = {**os.environ, 'DATA_DIR': data_dir, 'VAT_BOOK_BUILD': '1'}
    proc = subprocess.Popen(
        [sys.executable, builder,
         '--source', src,
         '--publish-to', book_registry.book_db_path('vat'),
         '--result-db', config.DATABASE_PATH,
         '--result-row', str(run_id),
         '--cleanup-dir', run_root]
        + (['--snapshot-date', snapshot_date] if snapshot_date else []),
        cwd=os.path.dirname(builder), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    _VAT_BUILD_PROCS.append(proc)
