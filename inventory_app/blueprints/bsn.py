"""BSN blueprint — Express weekly import (unified box + legacy redirect),
BSN code mapping, and unit-conversions.

Extracted verbatim from app.py (behavior-preserving split) — see app.py's
module docstring for the overall file-split rationale. No URL changes;
route rules are unchanged, only their endpoint names gain a `bsn.` prefix.
"""
import os
import shutil
import sys
import tempfile
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
                    if ratio > 0:
                        pid_s, bsn_unit = parts[0], parts[1]
                        # if Put named this acronym, store conv under the
                        # FULL unit (ledger was just normalised to match)
                        bsn_unit = acr_full.get((pid_s, bsn_unit), bsn_unit)
                        items.append({'product_id': int(pid_s),
                                      'bsn_unit': bsn_unit, 'ratio': ratio})
                except (ValueError, IndexError):
                    pass
    if items:
        models.save_unit_conversions(items)
        msg = f'บันทึกการแปลงหน่วย {len(items)} รายการเรียบร้อย'
        if learned:
            msg += (f'  |  เรียนรู้หน่วยใหม่ {len(learned)} ตัว '
                    f'(จำไว้ใช้ครั้งต่อไป)')
        flash(msg, 'success')
    return redirect(url_for('bsn.unit_conversions'))


@bp_bsn.route('/unit-conversions/edit', methods=['POST'])
def unit_conversions_edit():
    product_id = request.form.get('product_id', type=int)
    bsn_unit   = request.form.get('bsn_unit', '').strip()
    new_ratio  = request.form.get('ratio', type=float)
    if product_id and bsn_unit and new_ratio and new_ratio > 0:
        models.update_unit_conversion_ratio(product_id, bsn_unit, new_ratio)
        flash(f'อัปเดต ratio สำหรับ {bsn_unit} เรียบร้อย (re-sync แล้ว)', 'success')
    return redirect(url_for('bsn.unit_conversions'))


@bp_bsn.route('/unit-conversions/dismiss', methods=['POST'])
def unit_conversions_dismiss():
    product_id = request.form.get('product_id', type=int)
    bsn_unit   = request.form.get('bsn_unit', '').strip()
    if product_id and bsn_unit:
        deleted = models.dismiss_pending_unit_conversion(product_id, bsn_unit)
        flash(f'ยกเลิก {deleted} แถวที่ยังไม่ sync ออกแล้ว (หน่วย "{bsn_unit}")', 'success')
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
        all_products=all_products,
        brands=brands,
        color_codes=color_codes,
        categories=categories,
        unit_suggestions=unit_suggestions,
        condition_suggestions=condition_suggestions,
        active_tab=tab,
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
            models.upsert_mapping(
                bsn_code, item['bsn_name'],
                is_ignored=1,
                ignore_reason=item.get('ignore_reason') or None,
            )

    # Backfill product_id on existing unlinked rows
    conn = get_connection()
    models.resolve_pending_mappings(conn)
    conn.close()

    pending_left = len(models.get_pending_mappings())
    pending_sugg = models.count_pending_suggestions()
    return jsonify({'ok': True, 'pending_left': pending_left,
                    'pending_suggestions': pending_sugg})


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
                   'count': None, 'detail': {}, 'error': None}
            if rtype != 'unknown':
                try:
                    prev = import_router.preview_file(path, rtype)
                    row['count'] = prev.get('count')
                    row['detail'] = prev.get('detail') or {}
                except Exception as exc:   # preview failure isolates to this file
                    row['error'] = str(exc)
            rows.append(row)
        # The signed-cookie session is ~4KB. A credit-note preview's `detail`
        # carries per-row diff lists that can blow past that → the cookie is
        # silently dropped and /confirm 'เซสชันหมดอายุ'. The staged preview is
        # rendered from the in-memory `rows` (full detail) in THIS request;
        # /confirm only needs idx/filename/saved/detected, so store slim rows.
        slim = [{'idx': r['idx'], 'filename': r['filename'], 'saved': r['saved'],
                 'detected': r['detected']} for r in rows]
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
        if rtype == 'unknown' or not os.path.isfile(path):
            results.append({'filename': row['filename'], 'ok': False,
                            'msg': 'ข้าม — ไม่ได้เลือกประเภท'})
            continue
        try:
            out = import_router.commit_file(path, rtype, filename=row['filename'])
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


def _find_express_dbf_dataset_dir(root):
    """Locate the directory that directly holds the .DBF tables inside an
    extracted zip — root itself, or however deep Compress-Archive happened
    to nest it. ARTRN.DBF is mandatory for every import type, so its
    location pins the dataset dir express_dbf_source.open_table() expects."""
    for dirpath, _dirnames, filenames in os.walk(root):
        if any(fn.upper() == 'ARTRN.DBF' for fn in filenames):
            return dirpath
    return None


@bp_bsn.route('/import-express-dbf')
def express_dbf_import():
    freshness = models.get_express_dbf_freshness()
    return render_template('import_express_dbf.html', freshness=freshness)


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
    return (f'นำเข้าสำเร็จ — ขาย {sales}, ซื้อ {purchase}, '
            f'รับชำระ {pay_in}, จ่ายเงิน {pay_out}, '
            f'ลดหนี้ขาย {cn_ar}, ลดหนี้ซื้อ {cn_ap} รายการ')


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
        extract_dir = os.path.join(tmpdir, 'extracted')
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)
        dataset_dir = _find_express_dbf_dataset_dir(extract_dir)
        if dataset_dir is None:
            flash('ไม่พบ ARTRN.DBF ใน zip ที่อัปโหลด', 'danger')
            return redirect(redirect_to)

        # since_days defaults to 60 inside commit_express_dbf — a daily
        # upload of the full Express history only ever needs the recent
        # window; leave the default rather than duplicating it here.
        per_type = import_router.commit_express_dbf(dataset_dir, db_path=config.DATABASE_PATH)
    except Exception as exc:
        flash(f'นำเข้าไม่สำเร็จ: {exc}', 'danger')
        return redirect(redirect_to)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    flash(_express_dbf_summary_message(per_type), 'success')
    return redirect(redirect_to)
