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
