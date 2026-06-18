"""Detect which Shopee/Lazada export a file is, so one upload box can route it.

Returns (kind, platform):
  kind ∈ {'balance','income','order', None}; platform ∈ {'shopee','lazada', None}.
Detection order: sheet-name signatures first (Balance/Income are unambiguous),
then sheet-0 column signatures for the flat Order export.
"""
import pandas as pd


def detect_file(source):
    # Lazada exports are ';'-delimited CSV (not Excel) — sniff the header first.
    try:
        head = source.read(4096)
        source.seek(0)
        if isinstance(head, bytes):
            head = head.decode('utf-8-sig', errors='ignore')
        first = head.splitlines()[0] if head.strip() else ''
        cols = {c.strip() for c in first.split(';')}
        if {'Statement Number', 'Fee Name', 'Amount(Include Tax)'} <= cols:
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
