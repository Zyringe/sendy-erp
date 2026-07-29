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
    (_shopee_order_xlsx, 'คำสั่งซื้อ'),
])
def test_rejects_known_but_unsupported_exports(builder, must_mention):
    with pytest.raises(ValueError) as e:
        pp.detect_platform_file(builder())
    assert must_mention in str(e.value)


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
