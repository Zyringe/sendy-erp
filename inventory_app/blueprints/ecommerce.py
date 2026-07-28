"""E-commerce blueprint — Shopee/Lazada platform SKU sync (import/export/edit)
and listing↔product mapping (both the platform-SKU mapping and the
order-derived listing mapping).

Extracted verbatim from app.py (behavior-preserving split) — see app.py's
module docstring for the overall file-split rationale. No URL changes;
route rules are unchanged, only their endpoint names gain an `ecommerce.`
prefix.

Named `blueprints/ecommerce.py` with blueprint name `'ecommerce'` — this
collides with the moved route function also named `ecommerce`, producing
endpoint `ecommerce.ecommerce`. That is intentional (Flask allows it).
"""
import io
import os

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort, current_app, send_file)

import config
import models
from parse_platform import (parse_shopee, parse_lazada, export_shopee, export_lazada,
                            export_mapping, parse_mapping,
                            parse_shopee_orders, parse_lazada_orders,
                            export_listing_mapping, parse_listing_mapping,
                            parse_shopee_basic_info, parse_lazada_basic_info)

bp_ecommerce = Blueprint('ecommerce', __name__)


# ── E-commerce ────────────────────────────────────────────────────────────────

@bp_ecommerce.route('/ecommerce')
def ecommerce():
    search   = request.args.get('q', '').strip()
    flt      = request.args.get('flt') or None
    page     = int(request.args.get('page', 1))
    per_page = current_app.config['ITEMS_PER_PAGE']

    rows, total, counts = models.get_marketplace_overview(
        search=search or None, flt=flt, page=page, per_page=per_page)
    pages = max(1, (total + per_page - 1) // per_page)
    freshness = models.get_marketplace_freshness()
    unmapped_counts = models.get_unmapped_counts()
    unmapped_rows = (models.get_unmapped_rows()
                     if unmapped_counts['platform_skus'] or unmapped_counts['ecommerce_listings']
                     else [])

    return render_template('ecommerce.html',
                           rows=rows, total=total, counts=counts,
                           search=search, flt=flt, page=page, pages=pages,
                           freshness=freshness, unmapped_counts=unmapped_counts,
                           unmapped_rows=unmapped_rows)


@bp_ecommerce.route('/ecommerce/product/<int:product_id>')
def ecommerce_product(product_id):
    detail = models.get_product_marketplace_detail(product_id)
    if detail is None or not any(pf['items'] for pf in detail['platforms'].values()):
        abort(404)

    # Reuse get_marketplace_overview verbatim for the alert/combined-stock math
    # (single source of truth, see plan) rather than re-deriving it here.
    # simplify: O(all listed products) per detail-page hit — fine at ~560 rows
    # / single-operator traffic; add a dedicated per-product query if the
    # catalog or traffic grows enough to make that cost matter.
    overview_rows, _, _ = models.get_marketplace_overview(per_page=100000)
    status_row = next((r for r in overview_rows if r['product_id'] == product_id), None)

    return render_template('ecommerce_product.html', detail=detail, status_row=status_row)


@bp_ecommerce.route('/ecommerce/import', methods=['POST'])
def ecommerce_import():
    platform = request.form.get('platform', '').lower()
    if platform not in ('shopee', 'lazada'):
        flash('ระบุ platform ไม่ถูกต้อง', 'danger')
        return redirect(url_for('ecommerce.ecommerce'))

    f = request.files.get('platform_file')
    if not f or not f.filename.endswith('.xlsx'):
        flash('กรุณาเลือกไฟล์ .xlsx', 'danger')
        return redirect(url_for('ecommerce.ecommerce'))

    try:
        file_bytes = io.BytesIO(f.read())
        if platform == 'shopee':
            records = parse_shopee(file_bytes)
        else:
            records = parse_lazada(file_bytes)

        if not records:
            flash('ไม่พบข้อมูลในไฟล์', 'warning')
            return redirect(url_for('ecommerce.ecommerce'))

        count, propagated = models.import_platform_skus(platform, records)
        flash(f'นำเข้าข้อมูล {platform.capitalize()} สำเร็จ {count} รายการ '
              f'(restore mapping {propagated} รายการ จาก ecommerce_listings)',
              'success')
    except Exception as e:
        flash(f'เกิดข้อผิดพลาด: {e}', 'danger')

    return redirect(url_for('ecommerce.ecommerce'))


@bp_ecommerce.route('/ecommerce/import-info', methods=['POST'])
def ecommerce_import_info():
    """Import item-grain 'basic info' exports (description/status/warranty)
    into platform_products. Platform is detected PER FILE by filename, not
    by a form field, so a batch of mixed Shopee+Lazada basic-info files can
    be dropped in one multi-file upload."""
    files = request.files.getlist('info_files')
    if not files or not any(f.filename for f in files):
        flash('กรุณาเลือกไฟล์ .xlsx', 'danger')
        return redirect(url_for('ecommerce.ecommerce'))

    for f in files:
        if not f.filename:
            continue
        fname = f.filename.lower()
        file_bytes = io.BytesIO(f.read())
        try:
            if 'mass_update_basic_info' in fname:
                platform = 'shopee'
                records = parse_shopee_basic_info(file_bytes)
            elif fname.startswith('basic') and fname.endswith('.xlsx'):
                platform = 'lazada'
                records = parse_lazada_basic_info(file_bytes)
            else:
                flash(f'ข้าม {f.filename} — ไม่ตรงรูปแบบไฟล์ basic info ที่รองรับ', 'warning')
                continue

            if not records:
                flash(f'{f.filename}: ไม่พบข้อมูลในไฟล์', 'warning')
                continue

            count = models.import_platform_products(platform, records)
            flash(f'{f.filename}: นำเข้า {count} รายการ ({platform})', 'success')
        except Exception as e:
            flash(f'{f.filename}: เกิดข้อผิดพลาด: {e}', 'danger')

    return redirect(url_for('ecommerce.ecommerce'))


@bp_ecommerce.route('/ecommerce/export/<platform>')
def ecommerce_export(platform):
    if platform not in ('shopee', 'lazada'):
        abort(404)

    rows = models.get_platform_skus_all(platform)
    if not rows:
        flash(f'ยังไม่มีข้อมูล {platform.capitalize()} ในระบบ', 'warning')
        return redirect(url_for('ecommerce.ecommerce'))

    from flask import send_file
    import datetime
    date_str = datetime.date.today().strftime('%Y%m%d')

    if platform == 'shopee':
        buf = export_shopee([dict(r) for r in rows])
        fname = f'Shopee_mass_update_{date_str}.xlsx'
        mimetype = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    else:
        buf = export_lazada([dict(r) for r in rows])
        fname = f'Lazada_pricestock_{date_str}.xlsx'
        mimetype = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

    return send_file(buf, mimetype=mimetype,
                     as_attachment=True, download_name=fname)


@bp_ecommerce.route('/ecommerce/mapping/export')
def ecommerce_mapping_export():
    rows = models.get_platform_mapping_data()
    if not rows:
        flash('ยังไม่มีข้อมูล platform ในระบบ', 'warning')
        return redirect(url_for('ecommerce.ecommerce'))

    from flask import send_file
    import datetime

    # Compute AI suggestions (~6s)
    suggestions = models.suggest_platform_mapping()

    buf = export_mapping(rows, suggestions=suggestions)
    fname = f'ecommerce_mapping_{datetime.date.today().strftime("%Y%m%d")}.xlsx'

    # บันทึกลง data/exports/ ด้วยทุกครั้ง
    exports_dir = os.path.join(os.path.dirname(config.BASE_DIR), 'data', 'exports')
    os.makedirs(exports_dir, exist_ok=True)
    save_path = os.path.join(exports_dir, fname)
    with open(save_path, 'wb') as f:
        f.write(buf.getvalue())
    buf.seek(0)

    return send_file(buf,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=fname)


@bp_ecommerce.route('/ecommerce/mapping/import', methods=['POST'])
def ecommerce_mapping_import():
    f = request.files.get('mapping_file')
    if not f or not f.filename.endswith('.xlsx'):
        flash('กรุณาเลือกไฟล์ .xlsx', 'danger')
        return redirect(url_for('ecommerce.ecommerce'))

    try:
        file_bytes = io.BytesIO(f.read())
        records = parse_mapping(file_bytes)
        updated, not_found = models.apply_platform_mapping(records)
        flash(f'Mapping สำเร็จ {updated} รายการ'
              + (f' | ไม่พบ SKU ในระบบ {not_found} รายการ' if not_found else ''),
              'success' if not_found == 0 else 'warning')
    except Exception as e:
        flash(f'เกิดข้อผิดพลาด: {e}', 'danger')

    return redirect(url_for('ecommerce.ecommerce'))


@bp_ecommerce.route('/ecommerce/sku/<int:sku_id>/edit', methods=['POST'])
def ecommerce_sku_edit(sku_id):
    try:
        models.update_platform_sku(
            sku_id,
            price       = float(request.form['price']) if request.form.get('price') else None,
            special_price = float(request.form['special_price']) if request.form.get('special_price') else None,
            stock       = int(request.form['stock']) if request.form.get('stock') else None,
            qty_per_sale = float(request.form.get('qty_per_sale') or 1),
        )
        flash('อัปเดตเรียบร้อย', 'success')
    except Exception as e:
        flash(f'เกิดข้อผิดพลาด: {e}', 'danger')
    # `next` lets the detail page's inline qty_per_sale edit redirect back to
    # itself instead of the overview; only a same-site relative path is
    # honored (never an attacker-supplied absolute/protocol-relative URL).
    next_url = request.form.get('next', '')
    if next_url.startswith('/') and not next_url.startswith('//'):
        return redirect(next_url)
    return redirect(url_for('ecommerce.ecommerce',
                            page=request.form.get('page', 1),
                            q=request.form.get('q', '')))



# ── Ecommerce Listing Mapping ─────────────────────────────────────────────────

@bp_ecommerce.route('/ecommerce/listings/import', methods=['POST'])
def ecommerce_listings_import():
    platform = request.form.get('platform', '').lower()
    if platform not in ('shopee', 'lazada'):
        flash('ระบุ platform ไม่ถูกต้อง', 'danger')
        return redirect(url_for('ecommerce.ecommerce'))

    files = request.files.getlist('order_files')
    if not files or all(not f.filename for f in files):
        flash('กรุณาเลือกไฟล์', 'danger')
        return redirect(url_for('ecommerce.ecommerce'))

    total_added = total_skipped = 0
    errors = []
    for f in files:
        if not f.filename.endswith('.xlsx'):
            errors.append(f'{f.filename}: ต้องเป็นไฟล์ .xlsx')
            continue
        try:
            file_bytes = io.BytesIO(f.read())
            if platform == 'shopee':
                records = parse_shopee_orders(file_bytes)
            else:
                records = parse_lazada_orders(file_bytes)
            added, skipped = models.import_ecommerce_listings(records)
            total_added   += added
            total_skipped += skipped
        except Exception as e:
            errors.append(f'{f.filename}: {e}')

    if errors:
        flash(' | '.join(errors), 'danger')
    if total_added or total_skipped:
        flash(f'นำเข้า {platform.capitalize()} สำเร็จ: เพิ่มใหม่ {total_added} รายการ, ซ้ำข้าม {total_skipped} รายการ', 'success')
    return redirect(url_for('ecommerce.ecommerce'))


@bp_ecommerce.route('/ecommerce/listings/mapping-export')
def ecommerce_listings_mapping_export():
    unmatched_only = request.args.get('unmatched') == '1'
    rows = models.get_listing_mapping_data(unmatched_only=unmatched_only)
    if not rows:
        flash('ยังไม่มีข้อมูล listing ในระบบ', 'warning')
        return redirect(url_for('ecommerce.ecommerce'))

    from flask import send_file
    import datetime
    suggestions = models.suggest_listing_mapping()
    buf = export_listing_mapping(rows, suggestions=suggestions, unmatched_only=False)
    suffix = '_unmatched' if unmatched_only else ''
    fname  = f'ecommerce_listing_mapping{suffix}_{datetime.date.today().strftime("%Y%m%d")}.xlsx'

    exports_dir = os.path.join(os.path.dirname(config.BASE_DIR), 'data', 'exports')
    os.makedirs(exports_dir, exist_ok=True)
    with open(os.path.join(exports_dir, fname), 'wb') as fh:
        fh.write(buf.getvalue())
    buf.seek(0)

    return send_file(buf,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=fname)


@bp_ecommerce.route('/ecommerce/listings/mapping-import', methods=['POST'])
def ecommerce_listings_mapping_import():
    f = request.files.get('listing_mapping_file')
    if not f or not f.filename.endswith('.xlsx'):
        flash('กรุณาเลือกไฟล์ .xlsx', 'danger')
        return redirect(url_for('ecommerce.ecommerce'))
    try:
        file_bytes = io.BytesIO(f.read())
        records = parse_listing_mapping(file_bytes)
        updated, not_found = models.apply_listing_mapping(records)
        flash(f'Mapping สำเร็จ {updated} รายการ'
              + (f' | ไม่พบ SKU ในระบบ {not_found} รายการ' if not_found else ''),
              'success' if not_found == 0 else 'warning')
    except Exception as e:
        flash(f'เกิดข้อผิดพลาด: {e}', 'danger')
    return redirect(url_for('ecommerce.ecommerce'))
