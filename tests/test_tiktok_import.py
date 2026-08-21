"""TikTok Seller Center `all_information` export → parser contract.

One file feeds TWO grains (listing → platform_products, variation →
platform_skus), so `parse_tiktok` returns both plus the one piece of metadata
the importer cannot recover afterwards: whether the export carried a
`quantity` column at all.

Why that flag exists (Put, 2026-08-21): TikTok's export dialog can emit the
same template with or without `quantity`. `import_platform_skus` overwrites
`stock` with no COALESCE, so feeding it the quantity-less shape would blank
the stock of every TikTok row — and `ecommerce_overview` reads
`max(stock,0) * qty_per_sale`, so every mapped TikTok product would flip to
the RED "sold out on the platform but we still hold stock" alert at once.
The chosen behaviour is: accept the file, preserve the stock already held,
and say so — never silently zero it.

Column-name reading, not positional: the two shapes differ by one column and
TikTok is free to add more.
"""
import io
import os

import openpyxl
import pytest

os.environ.setdefault('SKIP_DB_INIT', '1')
os.environ.setdefault('SECRET_KEY', 'test-only-secret')
os.environ.setdefault('ADMIN_PASSWORD', 'test-only-admin')

import parse_platform as pp


# ── Fixture builder — the real 5-header-row layout, data from row 5 ──────────

COLS = ['product_id', 'category', 'product_name', 'product_status', 'sku_id',
        'variation_value', 'product_description', 'brand', 'price', 'quantity',
        'seller_sku', 'parcel_weight', 'parcel_length', 'parcel_width',
        'parcel_height', 'cod', 'main_image', 'image_2', 'image_3']

_IMG = 'https://p16-oec-sg.ibyteimg.com/tos-x/{h}~tplv-x-origin-jpeg.jpeg?idc=my2&width=2000'

# Two listings: one with two variations, one single-variation.
ROWS = [
    ['173655', 'อุปกรณ์เสริมเครื่องมือไฟฟ้า (882952)', 'แผ่นตัดเหล็ก 14 นิ้ว Golden Lion',
     'วางขายอยู่(1)', '17365501', 'ใย 1 ชั้น, 1 ใบ', '<p>แผ่นตัด</p>',
     'golden lion (7072630204522563329)', '115', '50', 'DSC-CUT-GL-S5048-14in-BLK',
     '900', '36', '36', '2', 'ใช่', _IMG.format(h='a'), _IMG.format(h='b'), None],
    ['173655', 'อุปกรณ์เสริมเครื่องมือไฟฟ้า (882952)', 'แผ่นตัดเหล็ก 14 นิ้ว Golden Lion',
     'วางขายอยู่(1)', '17365502', 'ใย 1 ชั้น, 5 ใบ', '<p>แผ่นตัด</p>',
     'golden lion (7072630204522563329)', '545', '10', None,
     '5000', '36', '36', '5', 'ใช่', _IMG.format(h='a'), _IMG.format(h='b'), None],
    ['173698', 'เลื่อย (883720)', 'ใบเลื่อยคันธนู Sendai 30 นิ้ว', 'วางขายอยู่(1)',
     '17369801', 'ค่าเริ่มต้น', '<p>ใบเลื่อย</p>', 'sendai (7232571872918095621)',
     '95', '50', None, '150', None, None, None, 'ใช่',
     _IMG.format(h='c'), _IMG.format(h='d'), _IMG.format(h='e')],
]


def _xlsx(cols, rows, kind='All_Information'):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Template'
    n = len(cols)
    ws.append(cols)
    ws.append(['V4', kind, 'metric'] + [None] * (n - 3))
    ws.append(['หัวไทย'] * n)
    ws.append(['บังคับ'] * n)
    ws.append(['ไม่สามารถแก้ไขได้'] * n)
    for r in rows:
        ws.append(list(r))
    wb.create_sheet('TemplateConfig').append(['x'])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _drop(col, cols=None, rows=None):
    """Same export with one column removed — the 36-column shape."""
    cols = list(cols or COLS)
    rows = [list(r) for r in (rows or ROWS)]
    i = cols.index(col)
    return [c for j, c in enumerate(cols) if j != i], \
           [[v for j, v in enumerate(r) if j != i] for r in rows]


def _edit(row_idx, col, value):
    """Copy of ROWS with one cell changed."""
    rows = [list(r) for r in ROWS]
    rows[row_idx][COLS.index(col)] = value
    return rows


# ── 1. Both grains come out of one file ─────────────────────────────────────

def test_parses_both_grains_with_counts_first():
    out = pp.parse_tiktok(_xlsx(COLS, ROWS))
    assert len(out['skus']) == 3, 'one record per variation row'
    assert len(out['products']) == 2, 'listings de-duplicated to one record each'
    assert out['stock_present'] is True


def test_sku_record_carries_the_variation_grain_fields():
    out = pp.parse_tiktok(_xlsx(COLS, ROWS))
    by_id = {s['variation_id']: s for s in out['skus']}
    assert set(by_id) == {'17365501', '17365502', '17369801'}
    s = by_id['17365501']
    assert s['product_id_str'] == '173655'
    assert s['variation_name'] == 'ใย 1 ชั้น, 1 ใบ'
    assert s['price'] == 115.0
    assert s['stock'] == 50
    assert s['seller_sku'] == 'DSC-CUT-GL-S5048-14in-BLK'
    assert s['weight_kg'] == 0.9, 'parcel_weight is grams in the file, kg in the DB'
    assert (s['length_cm'], s['width_cm'], s['height_cm']) == (36.0, 36.0, 2.0)


def test_missing_parcel_dimensions_stay_none_not_zero():
    """38/47 real rows ship no L/W/H. Zero would read as a measured 0 cm."""
    out = pp.parse_tiktok(_xlsx(COLS, ROWS))
    s = next(s for s in out['skus'] if s['variation_id'] == '17369801')
    assert (s['length_cm'], s['width_cm'], s['height_cm']) == (None, None, None)
    assert s['seller_sku'] is None, 'blank seller_sku is absent, not empty string'


def test_product_record_carries_the_listing_grain_fields():
    out = pp.parse_tiktok(_xlsx(COLS, ROWS))
    by_id = {p['product_id_str']: p for p in out['products']}
    assert set(by_id) == {'173655', '173698'}
    p = by_id['173655']
    assert p['product_name'] == 'แผ่นตัดเหล็ก 14 นิ้ว Golden Lion'
    assert p['description'] == '<p>แผ่นตัด</p>'
    assert p['brand'] == 'golden lion'
    assert p['category_id_str'] == '882952'
    assert p['category_name'] == 'อุปกรณ์เสริมเครื่องมือไฟฟ้า'
    assert p['status'] == 'วางขายอยู่(1)'


# ── 2. Image URLs are identified by PATH, not by the whole URL ───────────────

def test_image_urls_are_normalised_to_the_stable_path():
    """TikTok rotates the CDN edge host and an `idc=` routing param between
    exports ten minutes apart, on every row. Keeping the raw URL makes every
    import look like all 47 images changed. Measured 2026-08-21."""
    out = pp.parse_tiktok(_xlsx(COLS, ROWS))
    p = next(p for p in out['products'] if p['product_id_str'] == '173698')
    assert p['cover_image_url'] == '/tos-x/c~tplv-x-origin-jpeg.jpeg'
    assert p['image_urls'] == ('/tos-x/c~tplv-x-origin-jpeg.jpeg,'
                               '/tos-x/d~tplv-x-origin-jpeg.jpeg,'
                               '/tos-x/e~tplv-x-origin-jpeg.jpeg')


def test_same_image_through_a_different_edge_parses_identically():
    """CONTROL for the above: change only host + idc, expect no diff."""
    a = pp.parse_tiktok(_xlsx(COLS, ROWS))
    swapped = [[(v.replace('p16-', 'p19-').replace('idc=my2', 'idc=my3')
                 if isinstance(v, str) and v.startswith('https://') else v)
                for v in r] for r in ROWS]
    b = pp.parse_tiktok(_xlsx(COLS, swapped))
    assert [p['image_urls'] for p in a['products']] == \
           [p['image_urls'] for p in b['products']]


# ── 3. The quantity-less shape is accepted and FLAGGED ──────────────────────

def test_quantity_absent_is_flagged_and_leaves_stock_unset():
    cols, rows = _drop('quantity')
    out = pp.parse_tiktok(_xlsx(cols, rows))
    assert len(out['skus']) == 3
    assert out['stock_present'] is False
    assert all(s['stock'] is None for s in out['skus'])


def test_quantity_present_is_flagged_too():
    """CONTROL. Without this the flag could be hardwired False and nothing
    would notice — the quantity-less path is the one that looks 'safe'."""
    out = pp.parse_tiktok(_xlsx(COLS, ROWS))
    assert out['stock_present'] is True
    assert [s['stock'] for s in out['skus']] == [50, 10, 50]


# ── 4. Refusals — the load-bearing half ─────────────────────────────────────

def test_blank_sku_id_is_refused():
    """SQLite treats every NULL in UNIQUE(platform, variation_id) as distinct,
    so blank ids do not conflict — they accumulate as junk stub rows. That is
    exactly the 2026-07-30 Shopee incident, 288 rows deep."""
    with pytest.raises(ValueError) as e:
        pp.parse_tiktok(_xlsx(COLS, _edit(1, 'sku_id', None)))
    assert 'sku_id' in str(e.value).lower()


def test_duplicate_sku_id_within_one_file_is_refused():
    with pytest.raises(ValueError) as e:
        pp.parse_tiktok(_xlsx(COLS, _edit(1, 'sku_id', '17365501')))
    assert '17365501' in str(e.value)


def test_blank_product_id_is_refused():
    with pytest.raises(ValueError) as e:
        pp.parse_tiktok(_xlsx(COLS, _edit(2, 'product_id', None)))
    assert 'product_id' in str(e.value).lower()


@pytest.mark.parametrize('col, bad', [
    ('price', '-1'), ('price', 'ฟรี'), ('quantity', '-5'), ('quantity', 'เยอะ'),
])
def test_non_numeric_or_negative_money_and_stock_are_refused(col, bad):
    with pytest.raises(ValueError) as e:
        pp.parse_tiktok(_xlsx(COLS, _edit(0, col, bad)))
    assert col in str(e.value)


def test_missing_required_column_is_refused_by_name():
    cols, rows = _drop('price')
    with pytest.raises(ValueError) as e:
        pp.parse_tiktok(_xlsx(cols, rows))
    assert 'price' in str(e.value)


def test_file_with_no_data_rows_returns_empty_not_an_error():
    """The route flashes 'ไม่พบข้อมูลในไฟล์' for this — it is not a refusal."""
    assert pp.parse_tiktok(_xlsx(COLS, [])) == {}


# ── 5. One listing must not disagree with itself ────────────────────────────

def test_conflicting_listing_level_fields_across_variations_are_refused():
    """Both rows of listing 173655 must agree on name/description/brand —
    they are one listing. A disagreement means the file was hand-edited."""
    with pytest.raises(ValueError) as e:
        pp.parse_tiktok(_xlsx(COLS, _edit(1, 'product_name', 'ชื่ออื่น')))
    assert '173655' in str(e.value)


# ── 6. Importer — one transaction, two grains ───────────────────────────────
#
# `import_platform_skus` is NOT reused here on purpose. Its contract overwrites
# `stock` and always bumps `imported_at`, and Put's instruction (2026-08-21) was
# to leave the Shopee/Lazada path alone. TikTok gets its own writer so the two
# contracts never have to be reconciled in one function.

import sqlite3                                                     # noqa: E402


def _parsed(rows=None, cols=None, stock=True):
    cols = cols or COLS
    rows = ROWS if rows is None else rows
    if not stock:
        cols, rows = _drop('quantity', cols, rows)
    return pp.parse_tiktok(_xlsx(cols, rows))


def _tt(conn, table='platform_skus'):
    return conn.execute(
        f"SELECT * FROM {table} WHERE platform='tiktok' ORDER BY id").fetchall()


@pytest.fixture
def db(tmp_db):
    """Force the tiktok state — never inherit it. tmp_db clones the LIVE dev DB
    with its rows, so a future dev-DB import of real TikTok data would silently
    change what these tests mean."""
    import models
    from database import get_connection
    conn = get_connection()
    conn.execute("DELETE FROM platform_skus WHERE platform='tiktok'")
    conn.execute("DELETE FROM platform_products WHERE platform='tiktok'")
    conn.execute("DELETE FROM platform_price_history WHERE platform='tiktok'")
    conn.commit()
    yield models
    conn.close()


def test_import_writes_both_grains(db):
    from database import get_connection
    n_prod, n_sku, absent = db.import_tiktok_snapshot(_parsed())
    assert (n_prod, n_sku) == (2, 3)
    assert absent == []
    conn = get_connection()
    assert len(_tt(conn, 'platform_products')) == 2
    rows = _tt(conn)
    assert len(rows) == 3
    assert {r['variation_id'] for r in rows} == {'17365501', '17365502', '17369801'}
    assert {r['stock'] for r in rows} == {50, 10}
    conn.close()


def test_reimport_preserves_mapping_and_ratio(db):
    """The two columns `import_platform_skus` refuses to touch must stay
    untouched here too — they are the operator's work, not the file's."""
    from database import get_connection
    db.import_tiktok_snapshot(_parsed())
    conn = get_connection()
    pid = conn.execute(
        "SELECT id FROM products WHERE is_active=1 ORDER BY id LIMIT 1").fetchone()[0]
    conn.execute("UPDATE platform_skus SET internal_product_id=?, qty_per_sale=5 "
                 "WHERE platform='tiktok' AND variation_id='17365502'", (pid,))
    conn.commit()
    conn.close()

    db.import_tiktok_snapshot(_parsed(_edit(1, 'price', '600')))

    conn = get_connection()
    r = conn.execute("SELECT * FROM platform_skus WHERE platform='tiktok' "
                     "AND variation_id='17365502'").fetchone()
    assert r['price'] == 600.0, 'CONTROL: the import must actually have run'
    assert r['internal_product_id'] == pid
    assert r['qty_per_sale'] == 5
    conn.close()


def test_quantity_less_import_preserves_stock_and_snapshot_date(db):
    """Both halves matter. `stock` feeds listing_units; `imported_at` is what
    ecommerce_overview._snapshot_dates() reads as the platform's snapshot date,
    so bumping it would make a stale stock number look like today's truth and
    collapse the sold_since window to zero."""
    from database import get_connection
    db.import_tiktok_snapshot(_parsed())
    conn = get_connection()
    before = {r['variation_id']: (r['stock'], r['imported_at']) for r in _tt(conn)}
    conn.execute("UPDATE platform_skus SET imported_at='2026-01-01 00:00:00' "
                 "WHERE platform='tiktok'")
    conn.commit()
    conn.close()

    n_prod, n_sku, _ = db.import_tiktok_snapshot(
        _parsed(_edit(0, 'price', '119'), stock=False))
    assert (n_prod, n_sku) == (2, 3)

    conn = get_connection()
    rows = {r['variation_id']: r for r in _tt(conn)}
    assert len(rows) == 3
    assert rows['17365501']['price'] == 119.0, 'CONTROL: the import ran'
    for vid, (stock, _) in before.items():
        assert rows[vid]['stock'] == stock, f'{vid} stock was blanked'
        assert rows[vid]['imported_at'] == '2026-01-01 00:00:00', \
            f'{vid} snapshot date moved on a file that carried no stock'
    conn.close()


def test_quantity_present_import_does_move_both(db):
    """CONTROL for the test above — without it, an importer that never writes
    stock or imported_at at all would pass, and nothing else would notice."""
    from database import get_connection
    db.import_tiktok_snapshot(_parsed())
    conn = get_connection()
    conn.execute("UPDATE platform_skus SET imported_at='2026-01-01 00:00:00' "
                 "WHERE platform='tiktok'")
    conn.commit()
    conn.close()

    db.import_tiktok_snapshot(_parsed(_edit(0, 'quantity', '7')))

    conn = get_connection()
    r = conn.execute("SELECT * FROM platform_skus WHERE platform='tiktok' "
                     "AND variation_id='17365501'").fetchone()
    assert r['stock'] == 7
    assert r['imported_at'] != '2026-01-01 00:00:00'
    conn.close()


def test_import_is_atomic_across_both_grains(db):
    """A half-written snapshot is worse than none: platform_products would
    advertise listings whose variations, prices and stock never landed."""
    from database import get_connection
    parsed = _parsed()
    parsed['skus'][2]['product_name'] = None      # platform_skus.product_name NOT NULL

    with pytest.raises(sqlite3.IntegrityError):
        db.import_tiktok_snapshot(parsed)

    conn = get_connection()
    assert _tt(conn, 'platform_products') == [], 'product grain must roll back too'
    assert _tt(conn) == []
    conn.close()


def test_rows_absent_from_the_file_are_reported_but_not_flagged(db):
    """Decision, Put 2026-08-21: report, never auto-ignore. An `all_information`
    export cannot be told apart from a partial one, and auto-flagging a partial
    export would hide listings that are still selling."""
    from database import get_connection
    db.import_tiktok_snapshot(_parsed())
    _, _, absent = db.import_tiktok_snapshot(_parsed(rows=ROWS[:2]))

    assert len(absent) == 1
    assert absent[0]['variation_id'] == '17369801'
    conn = get_connection()
    gone = conn.execute("SELECT * FROM platform_skus WHERE platform='tiktok' "
                        "AND variation_id='17369801'").fetchone()
    assert gone is not None, 'the row is kept — nothing is ever deleted'
    assert gone['is_ignored'] == 0, 'and it is NOT auto-flagged'
    conn.close()


def test_price_change_lands_in_platform_price_history(db):
    """mig 140 opened the history trigger to tiktok; pin that it actually fires,
    so a future rewrite of the upsert cannot silently drop price history."""
    from database import get_connection
    db.import_tiktok_snapshot(_parsed())
    db.import_tiktok_snapshot(_parsed(_edit(0, 'price', '125')))

    conn = get_connection()
    hist = conn.execute(
        "SELECT * FROM platform_price_history WHERE platform='tiktok' "
        "AND variation_id='17365501' AND field_name='price'").fetchall()
    assert len(hist) == 1
    assert float(hist[0]['old_value']) == 115.0
    assert float(hist[0]['new_value']) == 125.0
    conn.close()


# ── 7. TikTok must survive the trip to the product page ─────────────────────

def test_tiktok_listings_reach_the_product_detail_view(db):
    """`get_marketplace_listings_with_history` seeded its return dict with
    shopee+lazada only and then `continue`d on anything else, so a mapped
    TikTok listing was dropped with no error and no empty state — the product
    page simply had no TikTok section. Fixing the label map downstream
    (blueprints/products.py) is inert until this one is fixed."""
    from database import get_connection
    db.import_tiktok_snapshot(_parsed())
    conn = get_connection()
    pid = conn.execute(
        "SELECT id FROM products WHERE is_active=1 ORDER BY id LIMIT 1").fetchone()[0]
    # FORCE the state — tmp_db clones the live dev DB WITH its rows, so this
    # product may already carry real Shopee/Lazada listings and the control
    # below would count them. (Caught by the control itself, 2026-08-22.)
    conn.execute("DELETE FROM platform_skus WHERE internal_product_id=?", (pid,))
    conn.execute("UPDATE platform_skus SET internal_product_id=? "
                 "WHERE platform='tiktok' AND variation_id='17365501'", (pid,))
    # CONTROL: a Shopee row on the same product must keep showing up.
    conn.execute(
        "INSERT INTO platform_skus (platform, variation_id, product_name, "
        "  variation_name, price, stock, internal_product_id) "
        "VALUES ('shopee','ctrl-1','ctrl','ตัวควบคุม',9.0,1,?)", (pid,))
    conn.commit()
    conn.close()

    out = db.get_marketplace_listings_with_history(pid)

    assert 'tiktok' in out, 'TikTok block missing entirely'
    assert len(out['tiktok']['listings']) == 1
    assert out['tiktok']['listings'][0]['variation_id'] == '17365501'
    assert len(out['shopee']['listings']) == 1, 'CONTROL: shopee still routed'


# ── 8. Route wiring — assert STATE, not the redirect ────────────────────────
#
# `/ecommerce/import` ends `try: … except Exception: flash(...)` then
# `redirect(...)`, so 302 is what a success, a caught exception AND a refusal
# all return. Asserting the status code proves nothing; these assert the rows.

@pytest.fixture
def admin_client(tmp_db):
    from app import app as flask_app
    from database import get_connection
    conn = get_connection()
    conn.execute("DELETE FROM platform_skus WHERE platform='tiktok'")
    conn.execute("DELETE FROM platform_products WHERE platform='tiktok'")
    conn.commit()
    conn.close()
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


def _post(client, buf, name='Tiktoksellercenter_batchedit_all_information_template.xlsx'):
    return client.post('/ecommerce/import',
                       data={'files': (buf, name)},
                       content_type='multipart/form-data',
                       follow_redirects=True)


def test_route_imports_a_real_shaped_tiktok_file(admin_client):
    from database import get_connection
    resp = _post(admin_client, _xlsx(COLS, ROWS))
    body = resp.data.decode('utf-8')

    conn = get_connection()
    skus = conn.execute("SELECT * FROM platform_skus WHERE platform='tiktok' "
                        "ORDER BY variation_id").fetchall()
    prods = conn.execute("SELECT * FROM platform_products "
                         "WHERE platform='tiktok'").fetchall()
    conn.close()
    assert len(skus) == 3, f'rows did not land; page said: {body[:400]}'
    assert len(prods) == 2
    assert [s['price'] for s in skus] == [115.0, 545.0, 95.0]
    assert [s['stock'] for s in skus] == [50, 10, 50]
    assert 'TikTok' in body


def test_route_warns_when_the_file_carried_no_stock(admin_client):
    """Put's decision (2026-08-21): accept it, keep the stock, SAY SO. A silent
    accept is the failure mode — the number on screen would be stale with
    nothing marking it."""
    from database import get_connection
    _post(admin_client, _xlsx(COLS, ROWS))
    cols, rows = _drop('quantity')
    resp = _post(admin_client, _xlsx(cols, rows))
    body = resp.data.decode('utf-8')

    conn = get_connection()
    stocks = [r['stock'] for r in conn.execute(
        "SELECT stock FROM platform_skus WHERE platform='tiktok' "
        "ORDER BY variation_id")]
    conn.close()
    assert stocks == [50, 10, 50], 'stock must survive a quantity-less file'
    assert 'สต็อก' in body and 'ไม่ได้อัปเดต' in body


def test_route_reports_rows_the_file_no_longer_contains(admin_client):
    _post(admin_client, _xlsx(COLS, ROWS))
    resp = _post(admin_client, _xlsx(COLS, ROWS[:2]))
    body = resp.data.decode('utf-8')
    # Assert the whole fragment, not a bare '1' — a page is full of digits, so
    # `'1' in body` would survive an off-by-one in the absent count.
    assert 'มี 1 ตัวเลือกใน Sendy ที่ไม่อยู่ในไฟล์นี้' in body
    assert 'ใบเลื่อยคันธนู Sendai 30 นิ้ว' in body


def test_route_refuses_a_sibling_export_and_writes_nothing(admin_client):
    from database import get_connection
    cols = ['product_id', 'category', 'product_name', 'sku_id',
            'variation_value', 'price', 'seller_sku']
    idx = [COLS.index(c) for c in cols]
    buf = _xlsx(cols, [[r[i] for i in idx] for r in ROWS], kind='Sales_Information')
    resp = _post(admin_client, buf, 'Tiktoksellercenter_batchedit_sales_information.xlsx')
    body = resp.data.decode('utf-8')

    conn = get_connection()
    n = conn.execute("SELECT COUNT(*) FROM platform_skus "
                     "WHERE platform='tiktok'").fetchone()[0]
    conn.close()
    assert n == 0, 'a refused file must write nothing'
    assert 'all_information' in body


# ── 9. The INSERT path of a quantity-less file ──────────────────────────────
#
# Section 3 only ever re-imports over variations that already exist, so the
# CASE expressions in the UPSERT are exercised on their DO UPDATE side alone.
# On INSERT those CASEs do not apply: `stock` goes in as NULL and `imported_at`
# takes the table default = now. Measured on the real 36-column export against
# an empty TikTok state (2026-08-22): 47 rows, all stock NULL, snapshot date
# stamped today, and the first row mapped to a product came out RED with
# true_available 97. That is the exact false alarm the flag exists to prevent.
#
# Rule: a file with no `quantity` column may UPDATE what we already hold, but
# it cannot introduce a variation — it has nothing to say about the stock of
# something we have never seen.

def test_quantity_less_file_refuses_to_introduce_new_variations(db):
    from database import get_connection
    with pytest.raises(ValueError) as e:
        db.import_tiktok_snapshot(_parsed(stock=False))
    assert '17365501' in str(e.value) or 'ปริมาณ' in str(e.value)

    conn = get_connection()
    assert _tt(conn) == [], 'nothing may land'
    assert _tt(conn, 'platform_products') == [], 'the product grain must roll back too'
    conn.close()


def test_quantity_less_file_still_updates_variations_we_already_hold(db):
    """CONTROL — the refusal must be scoped to NEW rows only, or Put's decision
    (accept the file, keep the stock, warn) is quietly cancelled."""
    from database import get_connection
    db.import_tiktok_snapshot(_parsed())
    n_prod, n_sku, _ = db.import_tiktok_snapshot(
        _parsed(_edit(0, 'price', '117'), stock=False))
    assert (n_prod, n_sku) == (2, 3)

    conn = get_connection()
    rows = {r['variation_id']: r for r in _tt(conn)}
    assert rows['17365501']['price'] == 117.0, 'the update must have run'
    assert [rows[v]['stock'] for v in ('17365501', '17365502', '17369801')] == [50, 10, 50]
    conn.close()


def test_partly_new_quantity_less_file_lands_nothing_at_all(db):
    """A file that is half known, half new is refused whole — a partial write
    is what atomicity exists to prevent."""
    from database import get_connection
    db.import_tiktok_snapshot(_parsed(rows=ROWS[:2]))          # 17369801 unknown
    conn = get_connection()
    before = {r['variation_id']: r['price'] for r in _tt(conn)}
    conn.close()

    with pytest.raises(ValueError):
        db.import_tiktok_snapshot(_parsed(_edit(0, 'price', '999'), stock=False))

    conn = get_connection()
    after = {r['variation_id']: r['price'] for r in _tt(conn)}
    assert after == before, 'the known half must not have been updated either'
    conn.close()


def test_fractional_quantity_is_refused_not_truncated(db):
    """int(3.9) == 3 understates stock by a whole unit with nothing to show for
    it, and stock feeds the RED/AMBER verdicts directly."""
    with pytest.raises(ValueError) as e:
        pp.parse_tiktok(_xlsx(COLS, _edit(0, 'quantity', '3.9')))
    assert 'quantity' in str(e.value)
