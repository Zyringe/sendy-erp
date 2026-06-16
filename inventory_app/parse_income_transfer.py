"""Parser for Shopee Income Transfer (โอนเงินสำเร็จ) XLSX files.

The file has several sheets (Summary / Income / Service Fee Details / Adjustment);
we read the "Income" sheet. ⚠ A real export carries a multi-row metadata banner
(seller name / date range / blank rows) ABOVE the real column-header row, so a
plain header=0 read finds none of the columns — use load_income_sheet(), which
auto-detects the header row past the banner, then parse_shopee_income() on the
result. parse_shopee_income() returns a list of settlement dicts:

    {
        'order_sn':     '26050192H4FY9X',
        'actual_payout': 211.0,   # col จำนวนเงินทั้งหมดที่โอนแล้ว (฿)
        'settled_at':   '2026-05-10',  # col วันที่โอนชำระเงินสำเร็จ
    }

These are the confirmed transferred amounts per order.
"""
import json

import pandas as pd

from parse_platform import _to_float


class IncomeTransferError(ValueError):
    pass


# Column headers in the Income sheet (Thai)
_COL_ORDER_SN    = 'หมายเลขคำสั่งซื้อ'
_COL_SETTLED_AT  = 'วันที่โอนชำระเงินสำเร็จ'
_COL_PAYOUT      = 'จำนวนเงินทั้งหมดที่โอนแล้ว (฿)'


def find_income_header_row(raw_df, max_scan=15):
    """Return the 0-based row index whose cells hold the Income-sheet headers.

    Real Shopee Income Transfer exports prepend a metadata banner (seller name /
    date range / blank rows), so the real header row is not row 0. Scan the first
    rows for the order-id header cell. Scanning (rather than hard-coding header=5)
    keeps this robust if Shopee changes the banner height.

    Args:
        raw_df: DataFrame read with header=None (positional columns).

    Raises:
        IncomeTransferError: if no header row is found within max_scan rows.
    """
    limit = min(max_scan, len(raw_df))
    for i in range(limit):
        for v in raw_df.iloc[i].tolist():
            if not pd.isna(v) and str(v).strip() == _COL_ORDER_SN:
                return i
    raise IncomeTransferError(
        f'ไม่พบแถวหัวตาราง (คอลัมน์ "{_COL_ORDER_SN}") ใน {limit} แถวแรก '
        '— ต้องเป็นไฟล์ Income Transfer (โอนเงินสำเร็จ) จาก Shopee ค่ะ')


def load_income_sheet(source):
    """Read the 'Income' sheet of a Shopee Income Transfer xlsx, header-aware.

    Auto-detects the real header row past the metadata banner and returns a
    str-typed DataFrame keyed by the Thai column names, ready for
    parse_shopee_income().

    Args:
        source: path or file-like (e.g. io.BytesIO) of the xlsx.

    Raises:
        IncomeTransferError: if the 'Income' sheet or its header is missing.
    """
    try:
        raw = pd.read_excel(source, sheet_name='Income', header=None, dtype=str)
    except ValueError as e:
        raise IncomeTransferError(
            "ไม่พบชีต 'Income' ในไฟล์ — ต้องเป็นไฟล์ Income Transfer "
            f"(โอนเงินสำเร็จ) จาก Shopee ค่ะ ({e})")
    hdr = find_income_header_row(raw)
    df = raw.iloc[hdr + 1:].copy()
    df.columns = [str(c).strip() for c in raw.iloc[hdr].tolist()]
    return df


# Fee buckets → list of source column names summed into each bucket.
_FEE_BUCKETS = {
    'fee_commission':  ['ค่าคอมมิชชั่น AMS', 'ค่าคอมมิชชั่น'],
    'fee_service':     ['ค่าบริการ'],
    'fee_transaction': ['ค่าธุรกรรมการชำระเงิน'],
    'fee_platform':    ['ค่าธรรมเนียมโครงสร้างพื้นฐานแพลตฟอร์ม'],
    'fee_ads_escrow':  ['ค่าธรรมเนียมเติมเงินโฆษณาจากเงิน Escrow'],
    'fee_tax':         ['ภาษี'],
    'shipping_net':    ['ค่าจัดส่งที่ชำระโดยผู้ซื้อ', 'ค่าจัดส่งสินค้าที่ออกโดย Shopee',
                        'ค่าจัดส่งที่ Shopee ชำระโดยชื่อของคุณ'],
    'fee_saver':       ['ค่าธรรมเนียม ของโปรแกรมประหยัดค่าจัดส่ง'],
}
_ITEM_COLS = ['สินค้าราคาปกติ', 'ส่วนลดสินค้าจากผู้ขาย']
_COL_FEE_PCT = 'ค่าธรรมเนียม (%)'


def _sum_cols(row, cols):
    total = 0.0
    for c in cols:
        v = _to_float(row.get(c)) if c in row else None
        if v is not None:
            total += v
    return round(total, 2)


def parse_shopee_income_fees(df):
    """Per-order fee breakdown from the Income sheet (full columns).

    Returns a list of dicts: order_sn, order_date, buyer, item_value, the
    fee_* buckets, shipping_net, fee_total, net_payout, fee_pct, fee_raw_json.
    Rows with a blank order id are skipped. Amounts keep Shopee's sign
    (fees are negative). fee_total = item_value - net_payout (the satang-true
    identity), independent of bucket completeness.
    """
    out = []
    for _, row in df.iterrows():
        sn = '' if pd.isna(row.get(_COL_ORDER_SN)) else str(row.get(_COL_ORDER_SN)).strip()
        if not sn or sn.lower() == 'nan':
            continue
        item_value = _sum_cols(row, _ITEM_COLS)
        net = _to_float(row.get(_COL_PAYOUT)) or 0.0
        rec = {
            'order_sn':   sn,
            'order_date': ('' if pd.isna(row.get('วันที่ทำการสั่งซื้อ'))
                           else str(row.get('วันที่ทำการสั่งซื้อ')).strip()[:10]),
            'buyer':      ('' if pd.isna(row.get('ชื่อผู้ใช้ (ผู้ซื้อ)'))
                           else str(row.get('ชื่อผู้ใช้ (ผู้ซื้อ)')).strip()),
            'item_value': item_value,
            'net_payout': round(net, 2),
            'fee_pct':    ('' if pd.isna(row.get(_COL_FEE_PCT))
                           else str(row.get(_COL_FEE_PCT)).strip()),
            'fee_total':  round(item_value - net, 2),
            'fee_raw_json': json.dumps(
                {k: (None if pd.isna(v) else str(v)) for k, v in row.items()},
                ensure_ascii=False),
        }
        for bucket, cols in _FEE_BUCKETS.items():
            rec[bucket] = _sum_cols(row, cols)
        out.append(rec)
    return out


def parse_shopee_income(df):
    """Parse the Income sheet DataFrame into settlement dicts.

    Args:
        df: pandas DataFrame of the "Income" sheet (all columns as str).

    Returns:
        List of dicts with keys: order_sn, actual_payout, settled_at.

    Raises:
        IncomeTransferError: if required columns are missing.
    """
    for col in (_COL_ORDER_SN, _COL_SETTLED_AT, _COL_PAYOUT):
        if col not in df.columns:
            raise IncomeTransferError(
                f'ไม่พบคอลัมน์ "{col}" — ต้องเป็นไฟล์ Income Transfer จาก Shopee ค่ะ')

    result = []
    for _, row in df.iterrows():
        order_sn_raw = row[_COL_ORDER_SN]
        if pd.isna(order_sn_raw):
            continue
        order_sn = str(order_sn_raw).strip()
        if not order_sn:
            continue
        payout_raw = row[_COL_PAYOUT]
        actual_payout = 0.0 if pd.isna(payout_raw) else (_to_float(payout_raw) or 0.0)
        settled_at = '' if pd.isna(row[_COL_SETTLED_AT]) else str(row[_COL_SETTLED_AT]).strip()
        result.append({
            'order_sn':      order_sn,
            'actual_payout': actual_payout,
            'settled_at':    settled_at,
        })
    return result
