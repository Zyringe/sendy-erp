import os
import shutil
import sqlite3

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, session, jsonify, abort, current_app, send_file)

import config
import models
from database import get_connection
from sku_code_utils import PACKAGING_SHORT

bp_products = Blueprint('products', __name__)


# ── Category suggester ────────────────────────────────────────────────────────
# Keyword → category code map. Order doesn't matter; scoring counts hits per
# category and picks the highest. Tweak this dict as you find new patterns.
_CATEGORY_KEYWORDS = {
    'door_bolt':   ['กลอน', 'สลักประตู'],
    'door_knob':   ['ลูกบิด', 'ก๊อกประตู'],
    'hinge':       ['บานพับ'],
    'handle':      ['มือจับ', 'หูเหล็ก', 'หูจับ'],
    'lock_key':    ['กุญแจ', 'แม่กุญแจ', 'มาสเตอร์คีย์', 'padlock'],
    'hammer':      ['ค้อน'],
    'screwdriver': ['ไขควง'],
    'cutter':      ['กรรไกร', 'มีดตัด', 'มีดคัตเตอร์'],
    'plier':       ['คีม'],
    'drill_bit':   ['ดอกสว่าน', 'ดจ.', 'สว่าน'],
    'saw':         ['เลื่อย', 'ใบเลื่อย'],
    'fastener':    ['ตะปู', 'น๊อต', 'สกรู', 'นัต'],
    'anchor':      ['ปุ๊ก', 'สมอเหล็ก', 'พุก'],
    'glue':        ['กาว', 'ซิลิโคน', 'แชลค', 'ลาเท็กซ์'],
    'paint_brush': ['แปรง', 'สีรองพื้น', 'ทาสี', 'ทินเนอร์', 'ลูกกลิ้ง'],
    'sandpaper':   ['กระดาษทราย', 'ผ้าทราย'],
    'tape_gypsum': ['เทป', 'ยิปซั่ม', 'ยิบซั่ม'],
    'faucet':      ['ก๊อกน้ำ', 'ก๊อกบอล', 'สายชำระ', 'ฝักบัว', 'ประตูน้ำ', 'วาล์ว'],
    'trowel':      ['เกียง', 'ฉาก'],
    'wire_cable':  ['ลวด', 'สลิง', 'สายไฟ'],
    'disc':        ['แผ่นตัด', 'แผ่นขัด', 'ใบตัด', 'ใบขัด', 'แผ่นเจียร'],
    'chemical':    ['น้ำยา', 'โซดาไฟ', 'สเปรย์', 'จารบี'],
    'measuring':   ['ตลับเมตร', 'เกจ์', 'มิเตอร์', 'วัดระดับ'],
    'safety':      ['ถุงมือ', 'แว่นตา', 'หน้ากาก'],
}


def _suggest_category_id(product_name, cat_id_by_code):
    """Return best-matching category id for the given product_name, or None.
    Scores by keyword-hit count; ties broken by sort order in dict."""
    if not product_name:
        return None
    name_lower = product_name.lower()
    best_code, best_score = None, 0
    for code, keywords in _CATEGORY_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw.lower() in name_lower)
        if score > best_score:
            best_code, best_score = code, score
    return cat_id_by_code.get(best_code) if best_code else None


@bp_products.route('/products/categorize')
def product_categorize():
    """Bulk-classify products: show unclassified ones + suggested category
    pre-selected in dropdown. User confirms / changes / skips → POST."""
    show = request.args.get('show', 'unclassified')   # unclassified | all
    limit = int(request.args.get('limit', 50))

    conn = get_connection()
    cats = conn.execute(
        "SELECT id, code, name_th FROM categories ORDER BY sort_order"
    ).fetchall()
    cat_id_by_code = {c['code']: c['id'] for c in cats}

    where = "WHERE p.is_active = 1"
    if show == 'unclassified':
        where += " AND p.category_id IS NULL"

    rows = conn.execute(
        f"""
        SELECT p.id, p.product_name, p.category_id,
               (SELECT name_th FROM categories WHERE id = p.category_id) AS current_cat
        FROM products p
        {where}
        ORDER BY p.id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    n_unclassified = conn.execute(
        "SELECT COUNT(*) FROM products WHERE is_active=1 AND category_id IS NULL"
    ).fetchone()[0]

    suggestions = [
        {
            'id': r['id'],
            'name': r['product_name'],
            'current_id': r['category_id'],
            'current_name': r['current_cat'],
            'suggested_id': _suggest_category_id(r['product_name'], cat_id_by_code),
        }
        for r in rows
    ]
    conn.close()
    return render_template(
        'products/categorize.html',
        suggestions=suggestions, categories=cats,
        n_unclassified=n_unclassified, show=show, limit=limit,
    )


@bp_products.route('/products/categorize/save', methods=['POST'])
def product_categorize_save():
    """Apply user's confirmed category selections. Form fields named
    `cat_<product_id>`; empty string = leave unchanged."""
    saved = 0
    conn = get_connection()
    for key, val in request.form.items():
        if not key.startswith('cat_'):
            continue
        try:
            pid = int(key[4:])
            cat_id = int(val) if val else None
        except ValueError:
            continue
        if cat_id:
            conn.execute(
                "UPDATE products SET category_id=? WHERE id=? AND (category_id IS NULL OR category_id != ?)",
                (cat_id, pid, cat_id),
            )
            saved += conn.total_changes if False else 1   # rough — counts intent, not rows
    conn.commit()
    conn.close()
    flash(f'จัดหมวด {saved} รายการแล้ว', 'success')
    return redirect(url_for('products.product_categorize',
                            show=request.form.get('back_show', 'unclassified')))


# ── Products ──────────────────────────────────────────────────────────────────

@bp_products.route('/products')
def product_list():
    search = request.args.get('q', '').strip()
    location = request.args.get('location', '').strip()
    low_stock = request.args.get('low_stock') == '1'
    hard_to_sell = request.args.get('hard_to_sell') == '1'
    in_stock = request.args.get('in_stock') == '1'
    restock = request.args.get('restock') == '1'
    show_alt = request.args.get('show_alt') == '1'
    show_inactive = request.args.get('show_inactive') == '1'
    page = int(request.args.get('page', 1))
    per_page = current_app.config['ITEMS_PER_PAGE']

    products, total = models.get_products(
        search=search or None,
        low_stock=low_stock,
        hard_to_sell=hard_to_sell,
        location=location or None,
        in_stock=in_stock,
        restock=restock,
        page=page,
        per_page=per_page,
        include_inactive=show_inactive,
    )
    pages = (total + per_page - 1) // per_page
    # Pack/unpack "true availability": only when the tick is on, compute how many
    # extra units each product on this page could be obtained by running a
    # conversion (unpack a แผง / pack ตัว). Display-only (see get_buildable).
    buildable = {}
    if show_alt and products:
        buildable = {pid: info['buildable']
                     for pid, info in models.get_buildable([p['id'] for p in products]).items()
                     if info['buildable'] > 0}
    # Remember this filtered view so a product's back button can return here even
    # after a detail-page action redirects without the ?back= param. Stored per
    # user in the signed-cookie session (multi-worker safe). Only the detail back
    # button reads it — /products itself never auto-restores it, so the สินค้า nav
    # tab still shows the full list (not sticky).
    return_url = url_for('products.product_list',
                         q=search or None, location=location or None,
                         low_stock='1' if low_stock else None,
                         in_stock='1' if in_stock else None,
                         show_alt='1' if show_alt else None,
                         show_inactive='1' if show_inactive else None,
                         hard_to_sell='1' if hard_to_sell else None,
                         restock='1' if restock else None,
                         page=page if page and page > 1 else None)
    session['products_return'] = return_url
    return render_template('products/list.html',
                           products=products, total=total,
                           page=page, pages=pages,
                           search=search, low_stock=low_stock,
                           hard_to_sell=hard_to_sell,
                           location=location, in_stock=in_stock,
                           restock=restock, show_alt=show_alt, buildable=buildable,
                           show_inactive=show_inactive, return_url=return_url)


def _new_form_context():
    """Pick-list data shared by the GET render and any re-render on a POST
    validation error, for the /products/new structured form (category/brand/
    color selects)."""
    conn = get_connection()
    categories = conn.execute(
        "SELECT id, code, name_th FROM categories ORDER BY sort_order, name_th"
    ).fetchall()
    color_codes = conn.execute(
        "SELECT code, name_th FROM color_finish_codes ORDER BY sort_order, code"
    ).fetchall()
    conn.close()
    return {
        'categories': categories,
        'color_codes': color_codes,
        'brands': models.get_brands(),
        'packaging_options': list(PACKAGING_SHORT.keys()),
    }


@bp_products.route('/products/parse-name')
def product_parse_name():
    """Parse a raw/typed product name into structured spec fields, for the
    /products/new 'type name -> parse -> review/edit' flow (P4). Reuses the
    same name-only parser bsn_suggest.py wraps for Smart Suggest (no bsn_code
    involved here). Read-only GET — the global login gate in app.py already
    covers it, no extra role check needed."""
    name = (request.args.get('name') or '').strip()
    if not name:
        return jsonify({'parsed': {}, 'proposed_name': '', 'brand_id': None, 'color_code': None})

    import bsn_suggest
    conn = get_connection()
    ctx = bsn_suggest._load_parser_context(conn)
    parsed = bsn_suggest._parse_bsn_name(name, ctx)
    proposed_name = bsn_suggest._build_proposed_name(parsed, ctx)

    brand_id = None
    if parsed.get('brand'):
        for bid, brec in ctx['brands_by_id'].items():
            if brec['name'] == parsed['brand']:
                brand_id = bid
                break
    conn.close()

    return jsonify({
        'parsed': parsed,
        'proposed_name': proposed_name,
        'brand_id': brand_id,
        'color_code': parsed.get('color_code') or None,
    })


@bp_products.route('/products/new', methods=['GET', 'POST'])
def product_new():
    if request.method == 'POST':
        f = request.form
        # brand_id / color_code selects carry a '__other__' sentinel when the
        # user picked "อื่นๆ (ระบุ)" — in that case the real value lives in
        # the paired *_other_name/*_other free-text field instead.
        brand_raw = (f.get('brand_id') or '').strip()
        color_raw = (f.get('color_code') or '').strip()
        try:
            data = {
                'product_name': f['product_name'].strip(),
                'category_id': int(f['category_id']) if f.get('category_id') else None,
                'sub_category': f.get('sub_category', '').strip() or None,
                'brand_id': int(brand_raw) if brand_raw and brand_raw != '__other__' else None,
                'brand_other_name': f.get('brand_other_name', '').strip() or None,
                'color_code': color_raw if color_raw and color_raw != '__other__' else None,
                'color_code_other': f.get('color_code_other', '').strip() or None,
                'packaging_th': f.get('packaging_th', '').strip() or None,
                'series': f.get('series', '').strip() or None,
                'model': f.get('model', '').strip() or None,
                'size': f.get('size', '').strip() or None,
                'condition': f.get('condition', '').strip() or None,
                'pack_variant': f.get('pack_variant', '').strip() or None,
                'units_per_carton': int(f['units_per_carton']) if f.get('units_per_carton') else None,
                'units_per_box': int(f['units_per_box']) if f.get('units_per_box') else None,
                'unit_type': f.get('unit_type', 'ตัว').strip() or 'ตัว',
                'hard_to_sell': 1 if f.get('hard_to_sell') else 0,
                'cost_price': float(f.get('cost_price') or 0),
                'base_sell_price': float(f.get('base_sell_price') or 0),
                'low_stock_threshold': int(f.get('low_stock_threshold') or config.LOW_STOCK_DEFAULT_THRESHOLD),
                'shopee_stock': int(f.get('shopee_stock') or 0),
                'lazada_stock': int(f.get('lazada_stock') or 0),
            }
        except ValueError as e:
            flash(f'ข้อมูลไม่ถูกต้อง: {e}', 'danger')
            return render_template('products/form.html', product=f, action='new',
                                   locations=[], **_new_form_context())

        try:
            pid = models.create_structured_product(data, 'manual')
        except sqlite3.DatabaseError as e:
            flash(f'บันทึกไม่สำเร็จ: {e}', 'danger')
            return render_template('products/form.html', product=f, action='new',
                                   locations=[], **_new_form_context())

        locations = request.form.getlist('floor_no')
        models.save_product_locations(pid, locations)
        flash('เพิ่มสินค้าเรียบร้อย', 'success')
        return redirect(url_for('products.product_detail', product_id=pid))

    return render_template('products/form.html', product={}, action='new', locations=[],
                           **_new_form_context())


@bp_products.route('/products/<int:product_id>')
def product_detail(product_id):
    product = models.get_product(product_id)
    if not product:
        flash('ไม่พบสินค้า', 'danger')
        return redirect(url_for('products.product_list'))
    promotions = models.get_promotions(product_id)
    active_promo = models.get_active_promotion(product_id)
    price_tiers = models.get_product_price_tiers(product_id)
    sell_price = models.effective_price(product)
    txn_page = int(request.args.get('txn_page', 1))
    per_page = 20
    txns, txn_total = models.get_transactions(product_id=product_id, page=txn_page, per_page=per_page)
    txn_pages = (txn_total + per_page - 1) // per_page
    locations = models.get_product_locations(product_id)
    bsn_pricing = models.get_product_pricing_summary(product_id)
    brands = models.get_brands()
    current_brand = models.get_brand(product['brand_id']) if product['brand_id'] else None
    # pack/unpack true-availability: extra units obtainable by running a conversion
    buildable = models.get_buildable([product_id]).get(product_id)
    # "Back to filtered list": prefer the ?back= the list link carried; fall back
    # to the last list view remembered in session so the back button still returns
    # to the filter even after a detail-page action redirects without ?back=.
    # Validated to an internal product-list path (prefix-safe; open-redirect guard).
    list_root = url_for('products.product_list')
    back = request.args.get('back') or session.get('products_return', '')
    back_url = back if (back == list_root or back.startswith(list_root + '?')) else list_root
    return render_template('products/detail.html',
                           product=product,
                           back_url=back_url,
                           buildable=buildable,
                           promotions=promotions,
                           active_promo=active_promo,
                           price_tiers=price_tiers,
                           sell_price=sell_price,
                           txns=txns,
                           txn_page=txn_page,
                           txn_pages=txn_pages,
                           txn_total=txn_total,
                           locations=locations,
                           bsn_pricing=bsn_pricing,
                           brands=brands,
                           current_brand=current_brand)


@bp_products.route('/products/<int:product_id>/brand', methods=['POST'])
def product_set_brand(product_id):
    if not models.get_product(product_id):
        abort(404)
    raw = request.form.get('brand_id', '').strip()
    new_brand_name = request.form.get('new_brand_name', '').strip()
    new_brand_name_th = request.form.get('new_brand_name_th', '').strip()
    new_brand_is_own = bool(request.form.get('new_brand_is_own'))

    brand_id = None
    if raw == '__new__' and new_brand_name:
        try:
            brand_id = models.create_brand(new_brand_name,
                                            name_th=new_brand_name_th,
                                            is_own=new_brand_is_own)
            flash(f'เพิ่มแบรนด์ "{new_brand_name}" แล้ว', 'success')
        except ValueError as e:
            flash(f'เพิ่มแบรนด์ไม่สำเร็จ: {e}', 'danger')
            return redirect(url_for('products.product_detail', product_id=product_id))
    elif raw == '__none__' or raw == '':
        brand_id = None
    else:
        try:
            brand_id = int(raw)
        except ValueError:
            flash('brand_id ผิดรูป', 'danger')
            return redirect(url_for('products.product_detail', product_id=product_id))

    models.set_product_brand(product_id, brand_id)
    flash('อัปเดตแบรนด์เรียบร้อย', 'success')
    return redirect(url_for('products.product_detail', product_id=product_id))


@bp_products.route('/products/<int:product_id>/cost-history')
def product_cost_history(product_id):
    if session.get('role') not in ('admin', 'manager', 'shareholder'):
        abort(403)
    history = models.get_cost_history(product_id)
    current_wacc = history[-1]['wacc_after'] if history else 0.0
    return jsonify({'wacc': current_wacc, 'history': history})


@bp_products.route('/products/<int:product_id>/pricing')
def product_pricing(product_id):
    product = models.get_product(product_id)
    if not product:
        abort(404)
    pricing = models.get_product_pricing(product_id)
    return render_template('products/pricing.html', product=product, pricing=pricing)


@bp_products.route('/products/<int:product_id>/edit', methods=['GET', 'POST'])
def product_edit(product_id):
    product = models.get_product(product_id)
    if not product:
        flash('ไม่พบสินค้า', 'danger')
        return redirect(url_for('products.product_list'))

    if request.method == 'POST':
        f = request.form
        try:
            data = {
                # Renaming is owned by the Master Naming page (/naming) — keep
                # the existing name and ignore whatever the read-only field
                # posts, so name + sku_code stay in sync there. (Phase 2)
                'product_name': product['product_name'],
                'units_per_carton': int(f['units_per_carton']) if f.get('units_per_carton') else None,
                'units_per_box': int(f['units_per_box']) if f.get('units_per_box') else None,
                'unit_type': f.get('unit_type', 'ตัว').strip() or 'ตัว',
                # hard_to_sell / shopee_stock / lazada_stock are intentionally
                # NOT included here: the edit form (templates/products/form.html,
                # `{% if action == 'edit' %}` block) doesn't render inputs for
                # them, so they'd always be absent from `f` and would clobber
                # the real values to 0/False. models.update_product only writes
                # columns present in this dict, so omitting them preserves
                # whatever is already in the DB.
                'cost_price': float(f.get('cost_price') or 0),
                'base_sell_price': float(f.get('base_sell_price') or 0),
                'low_stock_threshold': int(f.get('low_stock_threshold') or config.LOW_STOCK_DEFAULT_THRESHOLD),
            }
        except ValueError as e:
            flash(f'ข้อมูลไม่ถูกต้อง: {e}', 'danger')
            return render_template('products/form.html', product=f, action='edit', product_id=product_id)

        _who = session.get('username') or session.get('display_name') or '?'
        models.update_product(product_id, data, source=f'manual:{_who}')
        locations = request.form.getlist('floor_no')
        models.save_product_locations(product_id, locations)
        flash('แก้ไขสินค้าเรียบร้อย', 'success')
        return redirect(url_for('products.product_detail', product_id=product_id))

    locations = models.get_product_locations(product_id)
    return render_template('products/form.html', product=product, action='edit', product_id=product_id, locations=locations)


@bp_products.route('/products/<int:product_id>/location', methods=['POST'])
def product_location_save(product_id):
    if not models.get_product(product_id):
        abort(404)
    locations = request.form.getlist('floor_no')
    models.save_product_locations(product_id, locations)
    flash('อัปเดตสถานที่เก็บสินค้าเรียบร้อย', 'success')
    return redirect(url_for('products.product_detail', product_id=product_id))


@bp_products.route('/products/<int:product_id>/packaging', methods=['POST'])
def product_packaging_save(product_id):
    if session.get('role') not in ('admin', 'manager', 'shareholder'):
        abort(403)
    if not models.get_product(product_id):
        abort(404)

    def _parse(field):
        raw = (request.form.get(field) or '').strip()
        if not raw:
            return 1
        try:
            val = int(raw)
        except ValueError:
            return None
        return val if val >= 1 else None

    carton = _parse('units_per_carton')
    box = _parse('units_per_box')
    if carton is None or box is None:
        flash('ค่าบรรจุต้องเป็นจำนวนเต็มตั้งแต่ 1 ขึ้นไป', 'danger')
        return redirect(url_for('products.product_detail', product_id=product_id))

    conn = get_connection()
    conn.execute(
        'UPDATE products SET units_per_carton=?, units_per_box=? WHERE id=?',
        (carton, box, product_id),
    )
    conn.commit()
    conn.close()
    flash(f'บันทึกบรรจุ: ลัง={carton} · กล่อง={box}', 'success')
    return redirect(url_for('products.product_detail', product_id=product_id))


@bp_products.route('/products/<int:product_id>/deactivate', methods=['POST'])
def product_deactivate(product_id):
    models.deactivate_product(product_id)
    flash('ปิดใช้งานสินค้าเรียบร้อย', 'success')
    return redirect(url_for('products.product_list'))


@bp_products.route('/products/<int:product_id>/sku-code', methods=['POST'])
def product_sku_code_save(product_id):
    """Manual edit of sku_code. Saving manually sets sku_code_locked=1
    so future bulk regen doesn't overwrite. Empty value clears + unlocks."""
    if session.get('role') not in ('admin', 'manager', 'shareholder'):
        abort(403)
    new_code = (request.form.get('sku_code') or '').strip() or None
    conn = get_connection()
    if new_code:
        # Collision check (excluding self)
        clash = conn.execute(
            "SELECT id FROM products WHERE sku_code = ? AND id != ?",
            (new_code, product_id)
        ).fetchone()
        if clash:
            conn.close()
            flash(f'sku_code "{new_code}" ถูกใช้แล้วโดยรหัส (ID) #{clash[0]}', 'danger')
            return redirect(url_for('products.product_detail', product_id=product_id))
        conn.execute(
            "UPDATE products SET sku_code = ?, sku_code_locked = 1 WHERE id = ?",
            (new_code, product_id)
        )
        msg = f'บันทึก sku_code = "{new_code}" (ล็อก ป้องกันการ regen)'
    else:
        conn.execute(
            "UPDATE products SET sku_code = NULL, sku_code_locked = 0 WHERE id = ?",
            (product_id,)
        )
        msg = 'ลบ sku_code (unlocked — regen ภายหลังได้)'
    conn.commit()
    conn.close()
    flash(msg, 'success')
    return redirect(url_for('products.product_detail', product_id=product_id))


@bp_products.route('/products/<int:product_id>/regen-sku-code', methods=['POST'])
def product_regen_sku_code(product_id):
    """Regenerate sku_code from current structured cols.
    Admin/manager only. Forces unlock if user explicitly regenerates."""
    if session.get('role') not in ('admin', 'manager', 'shareholder'):
        abort(403)
    import sku_code_utils
    conn = get_connection()
    # Explicit existence check — don't conflate "product missing" with a
    # legitimately-NULL existing sku_code (regenerate_for_product returns
    # old=None in BOTH cases; NULL is the feature's primary use case).
    if conn.execute(
        "SELECT 1 FROM products WHERE id = ?", (product_id,)
    ).fetchone() is None:
        conn.close()
        abort(404)
    old, new = sku_code_utils.regenerate_for_product(conn, product_id)
    # Reset lock flag — regen explicitly requested overrides any prior lock
    conn.execute(
        "UPDATE products SET sku_code_locked = 0 WHERE id = ?", (product_id,)
    )
    conn.commit()
    conn.close()
    if old == new:
        flash(f'sku_code = "{new}" (ไม่มีการเปลี่ยนแปลง)', 'info')
    else:
        flash(f'Regen sku_code: "{old or "(NULL)"}" → "{new}"', 'success')
    return redirect(url_for('products.product_detail', product_id=product_id))


@bp_products.route('/products/<int:product_id>/trade')
def product_trade_summary(product_id):
    date_from = request.args.get('date_from') or None
    date_to   = request.args.get('date_to')   or None
    data = models.get_product_trade_summary(product_id, date_from, date_to)
    if not data['product']:
        flash('ไม่พบสินค้า', 'danger')
        return redirect(url_for('sales.trade_dashboard'))
    return render_template('products/trade_summary.html', data=data)


# ── Promotions ────────────────────────────────────────────────────────────────

@bp_products.route('/products/<int:product_id>/promotions/new', methods=['GET', 'POST'])
def promotion_new(product_id):
    product = models.get_product(product_id)
    if not product:
        flash('ไม่พบสินค้า', 'danger')
        return redirect(url_for('products.product_list'))

    if request.method == 'POST':
        f = request.form

        def _opt_float(v):
            v = (v or '').strip()
            return float(v) if v else None

        def _opt_int(v):
            v = (v or '').strip()
            return int(v) if v else None

        def _opt_str(v):
            v = (v or '').strip()
            return v or None

        try:
            data = {
                'product_id':       product_id,
                'promo_name':       f['promo_name'].strip(),
                'promo_type':       f['promo_type'],
                'discount_value':   _opt_float(f.get('discount_value')),
                'date_start':       f.get('date_start') or None,
                'date_end':         f.get('date_end') or None,
                'bundle_buy':       _opt_int(f.get('bundle_buy')),
                'bundle_free':      _opt_int(f.get('bundle_free')),
                'bundle_unit':      _opt_str(f.get('bundle_unit')),
                'bundle_condition': _opt_str(f.get('bundle_condition')),
                'gift_desc':        _opt_str(f.get('gift_desc')),
                'gift_qty':         _opt_str(f.get('gift_qty')),
            }
        except ValueError as e:
            flash(f'ข้อมูลไม่ถูกต้อง: {e}', 'danger')
            return render_template('promotions/form.html', product=product)

        # Validate per-type required fields (DB CHECK is the final gate; this
        # gives a friendlier error before hitting it)
        t = data['promo_type']
        if t == 'percent':
            if data['discount_value'] is None or not (0 < data['discount_value'] <= 100):
                flash('ส่วนลด % ต้องอยู่ระหว่าง 1–100', 'danger')
                return render_template('promotions/form.html', product=product)
        elif t == 'fixed':
            if not data['discount_value'] or data['discount_value'] <= 0:
                flash('ราคาตายตัวต้องมากกว่า 0', 'danger')
                return render_template('promotions/form.html', product=product)
        elif t == 'bundle':
            if data['bundle_buy'] is None or data['bundle_free'] is None:
                flash('โปรโมชันแถมของต้องระบุทั้ง "ซื้อ" และ "แถม"', 'danger')
                return render_template('promotions/form.html', product=product)
        elif t == 'gift':
            if not data['gift_desc'] or not data['gift_qty']:
                flash('โปรโมชันของแถมต้องระบุชื่อและจำนวน', 'danger')
                return render_template('promotions/form.html', product=product)
        elif t == 'mixed':
            if (data['discount_value'] is None
                and data['bundle_buy'] is None
                and not data['gift_desc']):
                flash('โปรโมชันแบบผสมต้องระบุอย่างน้อย 1 อย่าง (ส่วนลด / แถม / ของแถม)', 'danger')
                return render_template('promotions/form.html', product=product)
        else:
            flash(f'ไม่รองรับ promo_type {t!r}', 'danger')
            return render_template('promotions/form.html', product=product)

        try:
            models.create_promotion(data)
        except sqlite3.IntegrityError as e:
            flash(f'บันทึกไม่สำเร็จ (CHECK constraint): {e}', 'danger')
            return render_template('promotions/form.html', product=product)
        flash('เพิ่มโปรโมชันเรียบร้อย', 'success')
        return redirect(url_for('products.product_detail', product_id=product_id))

    return render_template('promotions/form.html', product=product)


@bp_products.route('/promotions/<int:promo_id>/deactivate', methods=['POST'])
def promotion_deactivate(promo_id):
    conn = get_connection()
    row = conn.execute("SELECT product_id FROM promotions WHERE id = ?", (promo_id,)).fetchone()
    conn.close()
    product_id = row['product_id'] if row else None
    models.deactivate_promotion(promo_id)
    flash('ยกเลิกโปรโมชันเรียบร้อย', 'success')
    return redirect(url_for('products.product_detail', product_id=product_id) if product_id else url_for('products.product_list'))


# ── Photo serving / review / walkthrough / labels (moved from app.py, Phase 4) ──

@bp_products.route('/photos/<path:filepath>')
def serve_catalog_photo(filepath):
    """Serve product photos from Design/photos/ (new layout 2026-05-25).

    Old layout was Design/Catalog/photos/products/<category>/<bucket>/...; rebuilt
    by Design/Catalog/scripts/rebuild_photo_index.py into Design/photos/<family_code>/.
    """
    if not session.get('role'):
        abort(403)
    photos_root = os.path.abspath(os.path.join(
        os.path.dirname(__file__), '..', '..', '..', 'Design', 'photos'
    ))
    full = os.path.normpath(os.path.join(photos_root, filepath))
    # Separator-aware containment: a bare startswith would also admit sibling
    # dirs sharing the prefix (e.g. ../photos_backup). commonpath compares
    # whole path components.
    try:
        if os.path.commonpath((full, photos_root)) != photos_root:
            abort(403)  # path traversal attempt
    except ValueError:
        abort(403)  # different drive / mixed abs-rel → reject
    if not os.path.isfile(full):
        abort(404)
    return send_file(full)


# ── Photo reviewer (clears Design/photos/_review/ queue) ─────────────────────
# One photo at a time + keyboard shortcuts. Atomic: move file + INSERT product_images.
_REVIEW_ROOT_REL = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..', '..', 'Design', 'photos', '_review'))
_PHOTOS_ROOT_REL = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..', '..', 'Design', 'photos'))
_IMG_EXTS_REV = ('.png', '.jpg', '.jpeg', '.webp')
_SINGLETON_NOTE = 'auto-singleton-from-photo-import-2026-05-25'


def _walk_review_files():
    """Return sorted list of (rel_path_from__review, abs_path) for unprocessed photos."""
    out = []
    if not os.path.isdir(_REVIEW_ROOT_REL):
        return out
    for root, _dirs, files in os.walk(_REVIEW_ROOT_REL):
        for fn in files:
            if fn.lower().endswith(_IMG_EXTS_REV):
                ab = os.path.join(root, fn)
                rel = os.path.relpath(ab, _REVIEW_ROOT_REL)
                out.append((rel, ab))
    out.sort(key=lambda x: x[0])
    return out


@bp_products.route('/photos/review')
def photos_review():
    if session.get('role') not in ('admin', 'manager', 'shareholder'):
        abort(403)
    files = _walk_review_files()
    after = request.args.get('after') or ''
    current = None
    if after:
        for rel, ab in files:
            if rel > after:
                current = (rel, ab)
                break
    else:
        current = files[0] if files else None
    remaining = len(files)
    return render_template('photos_review.html',
                           current=current, remaining=remaining, after=after)


def _safe_under(root, candidate):
    """Reject path traversal: candidate must resolve inside root."""
    full = os.path.normpath(os.path.join(root, candidate))
    try:
        if os.path.commonpath((full, root)) != root:
            return None
    except ValueError:
        return None
    return full


@bp_products.route('/photos/review/assign', methods=['POST'])
def photos_review_assign():
    if session.get('role') not in ('admin', 'manager', 'shareholder'):
        abort(403)
    src_rel = (request.form.get('src') or '').strip()
    sku_id = request.form.get('sku_id', type=int)
    role = (request.form.get('role') or 'extra').strip()
    if role not in ('single', 'pack', 'family', 'extra'):
        role = 'extra'
    if not src_rel or not sku_id:
        return jsonify({'ok': False, 'error': 'missing src or sku_id'}), 400
    src_abs = _safe_under(_REVIEW_ROOT_REL, src_rel)
    if not src_abs or not os.path.isfile(src_abs):
        return jsonify({'ok': False, 'error': 'source file not found'}), 404

    conn = get_connection()
    try:
        cur = conn.execute(
            "SELECT id, sku_code, product_name, brand_id, family_id "
            "FROM products WHERE id=?", (sku_id,)
        )
        prod = cur.fetchone()
        if not prod:
            return jsonify({'ok': False, 'error': 'product not in db'}), 404

        # If product has no family, auto-create singleton (mirror rebuild_photo_index logic)
        family_id = prod['family_id']
        if not family_id:
            fam_code = prod['sku_code']
            existing = conn.execute(
                "SELECT id FROM product_families WHERE family_code=?", (fam_code,)
            ).fetchone()
            if existing:
                family_id = existing['id']
            else:
                ins = conn.execute(
                    "INSERT INTO product_families "
                    "(family_code, display_name, brand_id, note) VALUES (?,?,?,?)",
                    (fam_code, prod['product_name'], prod['brand_id'], _SINGLETON_NOTE)
                )
                family_id = ins.lastrowid
            conn.execute(
                "UPDATE products SET family_id=? WHERE id=? AND family_id IS NULL",
                (family_id, prod['id'])
            )

        fam = conn.execute(
            "SELECT family_code FROM product_families WHERE id=?", (family_id,)
        ).fetchone()
        family_code = fam['family_code']

        # Construct target filename: sku_<code>_<role>_<orig>  (preserves original for uniqueness)
        orig_name = os.path.basename(src_rel)
        target_name = f"sku_{prod['sku_code']}_{role}_{orig_name}"
        target_dir = os.path.join(_PHOTOS_ROOT_REL, family_code)
        os.makedirs(target_dir, exist_ok=True)
        target_abs = os.path.join(target_dir, target_name)
        # Avoid collision
        if os.path.exists(target_abs):
            base, ext = os.path.splitext(target_name)
            i = 2
            while os.path.exists(os.path.join(target_dir, f"{base}_{i}{ext}")):
                i += 1
            target_name = f"{base}_{i}{ext}"
            target_abs = os.path.join(target_dir, target_name)

        # image_path stored relative to Design/photos/ (matches Flask /photos/ route)
        image_path = f"{family_code}/{target_name}"
        sort_order = {'single': 10, 'pack': 20, 'family': 50, 'extra': 30}[role]
        store_sku_id = None if role == 'family' else prod['id']
        conn.execute(
            "INSERT INTO product_images "
            "(family_id, sku_id, image_path, presentation_tag, sort_order) "
            "VALUES (?,?,?,?,?)",
            (family_id, store_sku_id, image_path, role, sort_order)
        )
        # Move file last so DB write rolls back if file move fails
        shutil.move(src_abs, target_abs)
        try:
            conn.commit()
        except Exception:
            # File already left the review queue but the INSERT never landed —
            # move it back so it isn't orphaned, then surface the failure the
            # same way an earlier move failure would (uncaught → 500).
            try:
                shutil.move(target_abs, src_abs)
            except Exception:
                current_app.logger.error(
                    "photos_review_assign: commit failed AND compensating "
                    "move-back failed; file may be stranded at %s (expected %s)",
                    target_abs, src_abs
                )
            raise
    finally:
        conn.close()
    return jsonify({'ok': True, 'next_url': url_for('products.photos_review')})


@bp_products.route('/photos/review/delete', methods=['POST'])
def photos_review_delete():
    if session.get('role') not in ('admin', 'manager', 'shareholder'):
        abort(403)
    src_rel = (request.form.get('src') or '').strip()
    if not src_rel:
        return jsonify({'ok': False, 'error': 'missing src'}), 400
    src_abs = _safe_under(_REVIEW_ROOT_REL, src_rel)
    if not src_abs or not os.path.isfile(src_abs):
        return jsonify({'ok': False, 'error': 'source file not found'}), 404
    os.remove(src_abs)
    return jsonify({'ok': True, 'next_url': url_for('products.photos_review')})


@bp_products.route('/api/products/search')
def api_products_search():
    q = (request.args.get('q') or '').strip()
    limit = min(int(request.args.get('limit', 20)), 50)
    if not q:
        return jsonify({'items': []})
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT p.id, p.product_name, p.base_sell_price, p.unit_type,
               (SELECT barcode FROM product_barcodes pb
                  WHERE pb.product_id = p.id
                  ORDER BY pb.is_primary DESC, pb.id ASC LIMIT 1) AS barcode
          FROM products p
         WHERE p.is_active = 1
           AND (p.product_name LIKE :q
                OR CAST(p.id AS TEXT) LIKE :q
                OR EXISTS (SELECT 1 FROM product_barcodes pb
                            WHERE pb.product_id = p.id AND pb.barcode LIKE :q))
         ORDER BY
             CASE WHEN CAST(p.id AS TEXT) = :exact THEN 0
                  WHEN p.product_name LIKE :starts THEN 1
                  ELSE 2 END,
             p.product_name
         LIMIT :lim
        """,
        {'q': f'%{q}%', 'starts': f'{q}%', 'exact': q, 'lim': limit}
    ).fetchall()
    conn.close()
    items = [{
        'id':         r['id'],
        'name':       r['product_name'],
        'price':      r['base_sell_price'],
        'unit':       r['unit_type'],
        'barcode':    r['barcode'] or '',
    } for r in rows]
    return jsonify({'items': items})


@bp_products.route('/api/products/<int:product_id>/barcodes', methods=['GET', 'POST', 'DELETE'])
def api_product_barcodes(product_id):
    if not session.get('role'):
        abort(403)
    conn = get_connection()
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        barcode = (data.get('barcode') or '').strip()
        if not barcode:
            conn.close()
            return jsonify({'error': 'barcode required'}), 400
        try:
            conn.execute(
                "INSERT INTO product_barcodes (product_id, barcode, source) "
                "VALUES (?, ?, 'manual')",
                (product_id, barcode)
            )
            conn.commit()
        except Exception as e:
            conn.close()
            return jsonify({'error': str(e)}), 400
    elif request.method == 'DELETE':
        bc_id = request.args.get('id')
        if bc_id:
            conn.execute("DELETE FROM product_barcodes WHERE id=? AND product_id=?",
                         (bc_id, product_id))
            conn.commit()
    rows = conn.execute(
        "SELECT id, barcode, is_primary, source FROM product_barcodes "
        "WHERE product_id=? ORDER BY is_primary DESC, id ASC",
        (product_id,)
    ).fetchall()
    conn.close()
    return jsonify({'items': [dict(r) for r in rows]})
