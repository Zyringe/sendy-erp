"""Detect which marketplace export a file is, so one upload box can route it,
and read an order export into parsed orders (`load_order_export`).

detect_file returns (kind, platform):
  kind ∈ {'balance','income','order','laz_statement','laz_wallet', None};
  platform ∈ models._shared.PLATFORMS or None.
Detection order: CSV header signatures (Lazada ';', TikTok ','), then sheet-name
signatures (Balance/Income are unambiguous), then sheet-0 column signatures for
the flat Excel Order export.
"""
import csv
import io

import pandas as pd

from parse_orders import parse_shopee_orders, parse_lazada_orders, parse_tiktok_orders

_TIKTOK_ORDER_COLS = {'Order ID', 'Order Substatus', 'SKU ID',
                      'SKU Subtotal After Discount', 'Created Time'}

_ORDER_PARSERS = {'shopee': parse_shopee_orders, 'lazada': parse_lazada_orders,
                  'tiktok': parse_tiktok_orders}


def detect_file(source):
    # Lazada exports are ';'-delimited CSV and TikTok's order export is a ','
    # CSV (not Excel) — sniff the header first.
    try:
        head = source.read(4096)
        source.seek(0)
        if isinstance(head, bytes):
            head = head.decode('utf-8-sig', errors='ignore')
        first = head.splitlines()[0] if head.strip() else ''
        if _TIKTOK_ORDER_COLS <= {c.strip() for c in next(csv.reader([first]), [])}:
            return ('order', 'tiktok')
        cols = {c.strip() for c in first.split(';')}
        if {'Statement Number', 'Fee Name', 'Amount(Include Tax)'} <= cols:
            return ('laz_statement', 'lazada')
        # Thai-language Account Statement export (same file, Thai headers).
        if {'รหัสรอบบิล', 'ชื่อรายการธุรกรรม', 'จำนวนเงิน(รวมภาษี)',
            'หมายเลขคำสั่งซื้อ'} <= cols:
            return ('laz_statement', 'lazada')
        if {'Transaction Number', 'Transaction Time', 'Type', 'Sub Type',
            'Amount', 'Remarks'} <= cols:
            return ('laz_wallet', 'lazada')
    except Exception:
        source.seek(0)
    try:
        xl = pd.ExcelFile(source)
    except Exception:
        return (None, None)
    sheets = set(xl.sheet_names)
    if 'Transaction Report' in sheets:
        return ('balance', 'shopee')
    if 'Income' in sheets and 'Service Fee Details' in sheets:
        return ('income', 'shopee')
    # Order export: read sheet 0 header (no banner) and sniff columns.
    try:
        cols = set(pd.read_excel(xl, sheet_name=0, header=0, nrows=0, dtype=str).columns)
    except Exception:
        cols = set()
    if 'orderItemId' in cols and 'orderNumber' in cols:
        return ('order', 'lazada')
    if 'หมายเลขคำสั่งซื้อ' in cols:
        return ('order', 'shopee')
    return (None, None)


def load_order_export(data):
    """bytes of an order export -> (platform, orders). The one reader for every
    platform's order file: detect, read (Excel for Shopee/Lazada, CSV for TikTok),
    dispatch to that platform's parser. Raises ValueError for anything else."""
    kind, platform = detect_file(io.BytesIO(data))
    if kind != 'order':
        raise ValueError('ไม่ใช่ไฟล์ order export ของ Shopee / Lazada / TikTok')
    if platform == 'tiktok':
        df = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False, encoding='utf-8-sig')
    else:
        df = pd.read_excel(io.BytesIO(data), sheet_name=0, header=0, dtype=str)
    return platform, _ORDER_PARSERS[platform](df)
