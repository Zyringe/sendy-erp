"""Content-based sniffing for the unified /ecommerce import box.

Why this exists (2026-07-30 incident): the stock-import form took a
`platform` from a dropdown and never checked what the file actually was.
A Shopee **basic-info** export (item grain, no ตัวเลือก / no คลัง) was
uploaded into it; `parse_shopee` only requires a numeric `รหัสสินค้า`
column so it parsed happily, yielding 288 records with `variation_id=None`.
SQLite treats each NULL in `UNIQUE(platform, variation_id)` as distinct, so
all 288 INSERTed as junk stub rows instead of conflicting — 219 of them
unmappable, and `MAX(imported_at)` (the snapshot date the whole
`/ecommerce` estimate hangs off) jumped a day forward on rows that carried
no stock at all.

`detect_platform_file` closes that hole by routing on CONTENT, and — the
load-bearing half — by REFUSING anything it cannot positively identify.

Discriminators (verified against the real exports on record, 2026-07-30):
  Shopee — row 0 is a machine-readable `et_title_*` code row, so detection
    is immune to Shopee localizing the Thai header row underneath it.
  Lazada — sheet 'template', header row 0. Both Thai and English header
    variants are accepted (Lazada has localized these at least once).
"""
import io
import os

import openpyxl
import pandas as pd
import pytest

os.environ.setdefault('SKIP_DB_INIT', '1')
os.environ.setdefault('SECRET_KEY', 'test-only-secret')
os.environ.setdefault('ADMIN_PASSWORD', 'test-only-admin')

import parse_platform as pp


# ── Fixture builders — real header shapes, one data row ──────────────────────

def _xlsx(sheets):
    """sheets: {name: [row, ...]} -> BytesIO. First name becomes the active sheet."""
    wb = openpyxl.Workbook()
    first = True
    for name, rows in sheets.items():
        ws = wb.active if first else wb.create_sheet()
        ws.title = name
        first = False
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _shopee_stock_xlsx():
    """mass_update_sales_info_*.xlsx — variation grain, has คลัง."""
    return _xlsx({'Sheet1': [
        ['et_title_product_id', 'et_title_product_name', 'et_title_variation_id',
         'et_title_variation_name', 'et_title_parent_sku', 'et_title_variation_sku',
         'et_title_variation_price', 'ps_gtin_code', 'et_title_variation_stock'],
        ['sales_info', 'hash', '0', '74562936', '{}', '', '', '', ''],
        ['รหัสสินค้า', 'ชื่อสินค้า', 'รหัสตัวเลือกสินค้า', 'ชื่อตัวเลือกสินค้า',
         'Parent SKU', 'เลข SKU', 'ราคา', 'GTIN', 'คลัง'],
        ['', '', '', '', '', '', 'จำเป็นต้องกรอก', '', 'จำเป็นต้องกรอก'],
        [], [],
        ['44105980850', 'เลื่อยโค้ง 13 นิ้ว', '208571591496', 'รุ่น GL7200',
         '', '', '280', '', '100'],
    ]})


def _shopee_basic_xlsx():
    """mass_update_basic_info_*.xlsx — item grain, has รายละเอียดสินค้า, NO คลัง."""
    return _xlsx({'Sheet1': [
        ['et_title_product_id', 'et_title_parent_sku', 'et_title_product_name',
         'et_title_product_description', 'et_title_reason'],
        ['basic_info', 'hash', '0', '74562936', '{}'],
        ['รหัสสินค้า', 'Parent SKU', 'ชื่อสินค้า', 'รายละเอียดสินค้า', 'เหตุผล'],
        [], [], [],
        ['44105980850', '', 'เลื่อยโค้ง 13 นิ้ว', 'คำอธิบายยาวๆ', ''],
    ]})


def _lazada_stock_xlsx(headers=None):
    """pricestock*.xlsx — sheet 'template', has ร้าน sku + SpecialPrice."""
    headers = headers or ['Product ID', 'catId', 'ชื่อสินค้า', 'currencyCode',
                          'sku.skuId', 'status', 'ร้าน sku', 'SpecialPrice',
                          'SpecialPrice Start', 'SpecialPrice End', 'ราคา',
                          'บุญสวัสดิ์นำชัย']
    return _xlsx({'template': [
        headers,
        ['ไม่บังคับ'] * len(headers),
        ['คำอธิบาย'] * len(headers),
        ['421038989', '62556202', 'ลูกบิดประตู', 'THB', '803226494', 'active',
         '421038989_TH-803226494', '599.00', '', '', '720.00', '2'],
    ]})


def _lazada_basic_xlsx(headers=None):
    """basic*.xlsx — sheet 'template', has รูปภาพสินค้า1 / ชื่อสินค้าใน En."""
    headers = headers or ['Product ID', 'catId', 'ชื่อสินค้า', 'ชื่อสินค้าใน En',
                          'สถานะสินค้า', 'รูปภาพสินค้า1', 'รูปภาพสินค้า2']
    return _xlsx({'template': [
        headers,
        ['ไม่บังคับ'] * len(headers),
        ['คำอธิบาย'] * len(headers),
        ['421038989', '62556202', 'ลูกบิดประตู', 'Door knob', 'active', '', ''],
    ]})


def _lazada_freight_xlsx():
    """freight*.xlsx — sheet 'template' too, but a shipping-dimensions export."""
    headers = ['Product ID', 'catId', 'Product Name', 'currencyCode', 'sku.skuId',
               'Pre-Order(by Days) Enable', 'Package Weight (kg)', 'SellerSKU']
    return _xlsx({'template': [
        headers, ['optional'] * len(headers), ['help'] * len(headers),
        ['421038989', '62556202', 'Door knob', 'THB', '803226494', 'No', '0.5', 'X'],
    ]})


def _lazada_skuimg_xlsx():
    """skuimg*.xlsx — per-variation image URLs. Carries 'Shop SKU' exactly like
    pricestock does but NO price/stock columns, so a Shop-SKU-only rule would
    route it to parse_lazada and NULL out price+stock on every Lazada row."""
    headers = ['Product ID', 'catId', 'Product Name', 'sku.skuId', 'sku.auditStatus',
               'Shop SKU', 'sku.skuStatus', 'Images1', 'Images2', 'SellerSKU',
               'Variations Combo']
    return _xlsx({'template': [
        headers, ['optional'] * len(headers), ['help'] * len(headers),
        ['421038989', '62556202', 'Door knob', '803226494', 'PASS',
         '421038989_TH-803226494', 'active', 'http://img1', '', 'Brown', 'Brown'],
    ]})


def _shopee_order_xlsx():
    """Order.all.*.xlsx — order export, belongs to /marketplace not /ecommerce."""
    return _xlsx({'orders': [
        ['หมายเลขคำสั่งซื้อ', 'สถานะการสั่งซื้อ', 'ชื่อสินค้า', 'ชื่อตัวเลือก', 'จำนวน'],
        ['260701GSJ5NV3F', 'สำเร็จ', 'ลูกบิดประตู', 'สีทอง', '1'],
    ]})


# ── 1. The four supported types are identified by content ────────────────────

@pytest.mark.parametrize('builder, expected', [
    (_shopee_stock_xlsx, ('shopee', 'stock')),
    (_shopee_basic_xlsx, ('shopee', 'basic')),
    (_lazada_stock_xlsx, ('lazada', 'stock')),
    (_lazada_basic_xlsx, ('lazada', 'basic')),
])
def test_detects_each_supported_type(builder, expected):
    assert pp.detect_platform_file(builder()) == expected


def test_detection_ignores_filename():
    """The 07-30 incident's real defect: the old path trusted a dropdown /
    filename. A basic-info file must be seen as basic-info no matter what it
    is called — and must NOT be treatable as a stock file."""
    assert pp.detect_platform_file(_shopee_basic_xlsx()) == ('shopee', 'basic')
    assert pp.detect_platform_file(_shopee_stock_xlsx()) == ('shopee', 'stock')


@pytest.mark.parametrize('thai_header, expected', [
    (['รหัสสินค้า', 'ชื่อสินค้า', 'รหัสตัวเลือกสินค้า', 'ชื่อตัวเลือกสินค้า',
      'Parent SKU', 'เลข SKU', 'ราคา', 'GTIN', 'คลัง'], ('shopee', 'stock')),
    (['รหัสสินค้า', 'Parent SKU', 'ชื่อสินค้า', 'รายละเอียดสินค้า', 'เหตุผล'],
     ('shopee', 'basic')),
])
def test_shopee_detected_without_the_et_title_code_row(thai_header, expected):
    """Fallback path: match on the Thai header when row 0 carries no
    `et_title_*` codes (a re-saved file, or an exporter change). Without this
    the detector would refuse files the old importer accepted."""
    buf = _xlsx({'Sheet1': [
        ['metadata row 0'], ['metadata row 1'], thai_header,
        ['instruction'], ['instruction'], ['instruction'],
        ['44105980850'] + [''] * (len(thai_header) - 1),
    ]})
    assert pp.detect_platform_file(buf) == expected


def test_lazada_english_headers_still_detected():
    """Lazada has localized these templates Thai <-> English at least once."""
    en_stock = ['Product ID', 'catId', 'Product Name', 'currencyCode', 'sku.skuId',
                'status', 'Shop SKU', 'SpecialPrice', 'SpecialPrice Start',
                'SpecialPrice End', 'Price', 'บุญสวัสดิ์นำชัย']
    assert pp.detect_platform_file(_lazada_stock_xlsx(en_stock)) == ('lazada', 'stock')

    en_basic = ['Product ID', 'catId', 'Product Name', 'Product Images1']
    assert pp.detect_platform_file(_lazada_basic_xlsx(en_basic)) == ('lazada', 'basic')


# ── 2. Everything else is REFUSED (the load-bearing half) ────────────────────

@pytest.mark.parametrize('builder, must_mention', [
    (_lazada_freight_xlsx, 'freight'),
    (_lazada_skuimg_xlsx, 'skuimg'),
    (_shopee_order_xlsx, 'คำสั่งซื้อ'),
])
def test_rejects_known_but_unsupported_exports(builder, must_mention):
    with pytest.raises(ValueError) as e:
        pp.detect_platform_file(builder())
    assert must_mention in str(e.value)


def test_skuimg_must_not_be_taken_for_a_stock_file():
    """Regression pin, /scrutinize 2026-07-30. skuimg is part of Lazada's
    standard 5-file product export, so it lands in the same download folder as
    pricestock and WILL get dropped into the one box. It shares 'Shop SKU' with
    pricestock but has no price/stock columns, and import_platform_skus assigns
    `price = excluded.price` / `stock = excluded.stock` WITHOUT a COALESCE — so
    misrouting it would blank price+stock on every Lazada listing and bump
    imported_at, collapsing every Lazada estimate on /ecommerce to zero. Worse
    than the incident this whole change exists to prevent."""
    with pytest.raises(ValueError):
        pp.detect_platform_file(_lazada_skuimg_xlsx())


def test_lazada_stock_requires_a_price_column():
    """What separates pricestock from its siblings is SpecialPrice — verified
    present in all five real pricestock exports on record (Thai and English
    header variants) and absent from skuimg/freight/basic."""
    no_price = ['Product ID', 'catId', 'ชื่อสินค้า', 'sku.skuId', 'ร้าน sku']
    with pytest.raises(ValueError):
        pp.detect_platform_file(_lazada_stock_xlsx(no_price))


def test_rejects_unrelated_spreadsheet():
    junk = _xlsx({'Sheet1': [['a', 'b', 'c'], [1, 2, 3]]})
    with pytest.raises(ValueError):
        pp.detect_platform_file(junk)


def test_shopee_family_but_unknown_variant_is_refused():
    """An et_title_* file we have no importer for must not fall through to a
    parser that would silently mangle it — the exact 07-30 failure shape."""
    odd = _xlsx({'Sheet1': [
        ['et_title_product_id', 'et_title_shipping_weight'],
        ['shipping_info', 'hash'],
        ['รหัสสินค้า', 'น้ำหนัก'],
        [], [], [],
        ['44105980850', '0.5'],
    ]})
    with pytest.raises(ValueError):
        pp.detect_platform_file(odd)


# ── 3. Detection must not consume the stream ─────────────────────────────────

def test_file_still_parseable_after_detection():
    """The route detects then parses the SAME BytesIO — a detector that left
    the cursor at EOF would break every import."""
    buf = _shopee_stock_xlsx()
    assert pp.detect_platform_file(buf) == ('shopee', 'stock')
    records = pp.parse_shopee(buf)
    assert len(records) == 1
    assert records[0]['variation_id'] == '208571591496'
    assert records[0]['stock'] == 100


# ── 4. Real exports on record (skipped when the files aren't present) ────────

_REAL = {
    '~/Downloads/mass_update_sales_info_74562936_20260729181929.xlsx': ('shopee', 'stock'),
    '~/Downloads/mass_update_basic_info_74562936_20260729170929.xlsx': ('shopee', 'basic'),
    '~/Downloads/pricestock100522265export1785320347654_0729-18-19-07.xlsx': ('lazada', 'stock'),
    '~/Downloads/basic100522265export1785315447528_0729-16-57-27.xlsx': ('lazada', 'basic'),
}


@pytest.mark.parametrize('path, expected', sorted(_REAL.items()))
def test_real_exports_detected(path, expected):
    full = os.path.expanduser(path)
    if not os.path.exists(full):
        pytest.skip(f'real export not on this machine: {path}')
    with open(full, 'rb') as fh:
        assert pp.detect_platform_file(io.BytesIO(fh.read())) == expected


# ── 5. TikTok Seller Center batch-edit exports ───────────────────────────────
#
# Discriminator: row 1 (0-indexed) carries a template version 'V4' in column 0
# and the template KIND in column 1 ('All_Information', 'Basic_Information',
# …). Both are machine-readable and survive TikTok localizing the Thai header
# row underneath, exactly like Shopee's et_title_* row.
#
# Why this cannot key on the Thai header: TikTok's row 2 contains
# 'รหัสสินค้า', which is _SHOPEE_ANY. Without a TikTok branch placed BEFORE
# the Shopee one, every TikTok export falls into the Shopee family and dies
# with a Shopee-flavoured error naming Shopee files (verified on all six real
# exports, 2026-08-21).
#
# Only All_Information is supported: measured against two separate real
# exports, it is a strict superset of the other five (the only differences
# were CDN host / `idc=` query-param noise on otherwise identical image URLs).

_TT_ALL_COLS = [
    'product_id', 'category', 'product_name', 'product_status', 'sku_id',
    'variation_value', 'product_description', 'brand', 'price', 'quantity',
    'seller_sku', 'parcel_weight', 'parcel_length', 'parcel_width',
    'parcel_height', 'cod', 'main_image', 'image_2', 'product_property/100107',
]
_TT_ALL_TH = [
    'รหัสสินค้า', 'หมวดหมู่', 'ชื่อสินค้า', 'สถานะสินค้า', 'SKU ID',
    'ตัวเลือกของตัวแปร', 'คำอธิบายสินค้า', 'แบรนด์', 'ราคาขายปลีก (สกุลเงินท้องถิ่น)',
    'ปริมาณ', 'SKU ของผู้ขาย', 'น้ำหนักพัสดุ(g)', 'ความยาวของพัสดุ(cm)',
    'ความกว้างของพัสดุ(cm)', 'ความสูงของพัสดุ(cm)',
    'เลือกว่าจะรองรับการเก็บเงินปลายทางหรือไม่', 'ภาพหลัก', 'ภาพที่ 2',
    'ประเภทของการรับประกัน',
]
_TT_ALL_ROW = [
    '1736550249302361784', 'อุปกรณ์เสริมเครื่องมือไฟฟ้า (882952)',
    'แผ่นตัดเหล็ก 14 นิ้ว Golden Lion', 'วางขายอยู่(1)', '1736551722754410168',
    'ใย 1 ชั้น, 1 ใบ', '<p>แผ่นตัดเหล็ก</p>', 'golden lion (7072630204522563329)',
    '115', '50', 'DSC-CUT-GL-S5048-14in-BLK', '900', '36', '36', '2', 'ใช่',
    'https://p16-oec-sg.ibyteimg.com/a~tplv-x-origin-jpeg.jpeg?idc=my2',
    'https://p16-oec-sg.ibyteimg.com/b~tplv-x-origin-jpeg.jpeg?idc=my2',
    'ไม่รับประกัน',
]


def _tiktok_xlsx(kind='All_Information', cols=None, thai=None, row=None,
                 sheets=('Instruction', 'HiddenStyle', 'TemplateConfig')):
    """Tiktoksellercenter_batchedit_*_template.xlsx — 5 header rows, data at 5."""
    cols = list(cols or _TT_ALL_COLS)
    thai = list(thai or _TT_ALL_TH)
    row = list(row or _TT_ALL_ROW)
    n = len(cols)
    book = {'Template': [
        cols,
        ['V4', kind, 'metric'] + [None] * (n - 3),
        thai,
        ['บังคับ'] * n,
        ['ไม่สามารถแก้ไขได้'] * n,
        row,
    ]}
    for s in sheets:
        book[s] = [['x']]
    return _xlsx(book)


def _tiktok_all_no_quantity_xlsx():
    """The 36-column variant — same export, `quantity` unticked at export time."""
    keep = [i for i, c in enumerate(_TT_ALL_COLS) if c != 'quantity']
    return _tiktok_xlsx(
        cols=[_TT_ALL_COLS[i] for i in keep],
        thai=[_TT_ALL_TH[i] for i in keep],
        row=[_TT_ALL_ROW[i] for i in keep],
    )


def _tiktok_sibling_xlsx(kind, cols):
    idx = [_TT_ALL_COLS.index(c) for c in cols]
    return _tiktok_xlsx(kind=kind, cols=cols,
                        thai=[_TT_ALL_TH[i] for i in idx],
                        row=[_TT_ALL_ROW[i] for i in idx])


_TT_SIBLINGS = {
    'Basic_Information': ['product_id', 'category', 'brand', 'product_name',
                          'product_status', 'product_description'],
    'Sales_Information': ['product_id', 'category', 'product_name', 'sku_id',
                          'variation_value', 'price', 'seller_sku'],
    'Shipping_Information': ['product_id', 'category', 'product_name', 'sku_id',
                             'variation_value', 'parcel_weight', 'parcel_length',
                             'parcel_width', 'parcel_height', 'cod'],
    'Media_Information': ['product_id', 'category', 'product_name', 'main_image',
                          'image_2'],
    'ProductProperty_Information': ['product_id', 'category', 'product_name',
                                    'product_description',
                                    'product_property/100107'],
}


@pytest.mark.parametrize('builder', [
    _tiktok_xlsx,
    _tiktok_all_no_quantity_xlsx,
])
def test_tiktok_all_information_detected(builder):
    """Both shapes route the same. The quantity-less one is accepted on
    purpose (Put, 2026-08-21) — the importer preserves the stock it already
    holds rather than the file blanking it; see test_tiktok_import."""
    assert pp.detect_platform_file(builder()) == ('tiktok', 'all')


@pytest.mark.parametrize('kind, cols', sorted(_TT_SIBLINGS.items()))
def test_tiktok_siblings_refused_by_name(kind, cols):
    """Each sibling is refused individually and the message names both the
    file it IS and the one to use instead — same stance as Lazada's
    freight/skuimg rejections, so a wrong drag is self-correcting."""
    with pytest.raises(ValueError) as e:
        pp.detect_platform_file(_tiktok_sibling_xlsx(kind, cols))
    msg = str(e.value)
    assert kind in msg, f'refusal must name the file it got: {msg}'
    assert 'all_information' in msg.lower(), f'refusal must name the fix: {msg}'


def test_tiktok_is_not_mistaken_for_shopee():
    """CONTROL for the branch-order bug. TikTok's Thai header row contains
    'รหัสสินค้า' (= _SHOPEE_ANY), so a TikTok branch placed AFTER Shopee's
    never runs. Pin the shape of the failure, not just the success: assert
    the verdict is tiktok AND that no Shopee wording leaked into it."""
    assert pp.detect_platform_file(_tiktok_xlsx()) == ('tiktok', 'all')
    with pytest.raises(ValueError) as e:
        pp.detect_platform_file(_tiktok_sibling_xlsx(
            'Media_Information', _TT_SIBLINGS['Media_Information']))
    assert 'Shopee' not in str(e.value)


def test_shopee_and_lazada_still_route_after_tiktok_branch():
    """CONTROL. Inserting a branch ahead of the others must not shadow them."""
    assert pp.detect_platform_file(_shopee_stock_xlsx()) == ('shopee', 'stock')
    assert pp.detect_platform_file(_shopee_basic_xlsx()) == ('shopee', 'basic')
    assert pp.detect_platform_file(_lazada_stock_xlsx()) == ('lazada', 'stock')
    assert pp.detect_platform_file(_lazada_basic_xlsx()) == ('lazada', 'basic')


def test_tiktok_detection_does_not_consume_the_stream():
    buf = _tiktok_xlsx()
    assert pp.detect_platform_file(buf) == ('tiktok', 'all')
    df = pd.read_excel(buf, sheet_name='Template', header=None, dtype=str)
    assert df.iloc[5, 0] == '1736550249302361784'
