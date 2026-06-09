import csv
import io
import sqlite3

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, session, jsonify, abort, current_app)

import config
import models
from database import get_connection

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
    return render_template('products/list.html',
                           products=products, total=total,
                           page=page, pages=pages,
                           search=search, low_stock=low_stock,
                           hard_to_sell=hard_to_sell,
                           location=location, in_stock=in_stock,
                           restock=restock, show_alt=show_alt, buildable=buildable)


@bp_products.route('/products/new', methods=['GET', 'POST'])
def product_new():
    if request.method == 'POST':
        f = request.form
        try:
            data = {
                'product_name': f['product_name'].strip(),
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
            return render_template('products/form.html', product=f, action='new')

        pid = models.create_product(data)
        locations = request.form.getlist('floor_no')
        models.save_product_locations(pid, locations)
        flash('เพิ่มสินค้าเรียบร้อย', 'success')
        return redirect(url_for('products.product_detail', product_id=pid))

    return render_template('products/form.html', product={}, action='new', locations=[])


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
    return render_template('products/detail.html',
                           product=product,
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
    if session.get('role') not in ('admin', 'manager'):
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
                'product_name': f['product_name'].strip(),
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
            return render_template('products/form.html', product=f, action='edit', product_id=product_id)

        models.update_product(product_id, data)
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
    if session.get('role') not in ('admin', 'manager'):
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
    if session.get('role') not in ('admin', 'manager'):
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
    if session.get('role') not in ('admin', 'manager'):
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
        return redirect(url_for('trade_dashboard'))
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


# ── CSV Import (product master) ───────────────────────────────────────────────

def _parse_csv_content(text):
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for r in reader:
        def parse_int(v):
            v = str(v).strip()
            return int(v) if v else None

        try:
            product_id = parse_int(r.get('product_id', ''))
        except ValueError:
            continue
        name = r.get('Product_Name', '').strip()
        if not name:
            continue

        rows.append({
            'product_id': product_id,
            'product_name': name,
            'units_per_carton': parse_int(r.get('บรรจุ/ลัง', '')),
            'units_per_box': parse_int(r.get('บรรจุ/กล่อง', '')),
            'unit_type': r.get('หน่วย', 'ตัว').strip() or 'ตัว',
            'hard_to_sell': 1 if str(r.get('ขายยาก', '')).strip().upper() == 'TRUE' else 0,
        })
    return rows


@bp_products.route('/import', methods=['GET', 'POST'])
def csv_import():
    if request.method == 'POST':
        if 'csv_file' not in request.files:
            flash('กรุณาเลือกไฟล์', 'danger')
            return redirect(url_for('products.csv_import'))

        f = request.files['csv_file']
        if not f.filename.endswith('.csv'):
            flash('รองรับเฉพาะไฟล์ .csv', 'danger')
            return redirect(url_for('products.csv_import'))

        content = f.read().decode('utf-8-sig')
        rows = _parse_csv_content(content)
        if not rows:
            flash('ไม่พบข้อมูลในไฟล์', 'warning')
            return redirect(url_for('products.csv_import'))

        session['import_rows'] = rows
        session['import_filename'] = f.filename
        return render_template('import.html', preview=rows[:20],
                               total=len(rows), step='confirm',
                               filename=f.filename)

    return render_template('import.html', step='upload')


@bp_products.route('/import/confirm', methods=['POST'])
def csv_import_confirm():
    rows = session.pop('import_rows', None)
    filename = session.pop('import_filename', 'unknown.csv')
    if not rows:
        flash('หมดเวลา กรุณาอัปโหลดใหม่', 'warning')
        return redirect(url_for('products.csv_import'))

    overwrite = request.form.get('overwrite') == '1'
    imported, skipped = models.bulk_import_products(rows, overwrite=overwrite)

    conn = get_connection()
    conn.execute("""
        INSERT INTO import_log (filename, rows_imported, rows_skipped, notes)
        VALUES (?, ?, ?, ?)
    """, (filename, imported, skipped, f'overwrite={overwrite}'))
    conn.commit()
    conn.close()

    flash(f'นำเข้าสำเร็จ {imported} รายการ, ข้าม {skipped} รายการ', 'success')
    return redirect(url_for('products.product_list'))
