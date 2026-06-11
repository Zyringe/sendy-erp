"""Parser for Shopee Income Transfer (โอนเงินสำเร็จ) XLSX files.

The file has 3 sheets: Summary / Income / Service Fee Details.
This module parses the "Income" sheet and returns a list of settlement dicts:

    {
        'order_sn':     '26050192H4FY9X',
        'actual_payout': 211.0,   # col จำนวนเงินทั้งหมดที่โอนแล้ว (฿)
        'settled_at':   '2026-05-10',  # col วันที่โอนชำระเงินสำเร็จ
    }

These are the confirmed transferred amounts per order.
"""


class IncomeTransferError(ValueError):
    pass


# Column headers in the Income sheet (Thai)
_COL_ORDER_SN    = 'หมายเลขคำสั่งซื้อ'
_COL_SETTLED_AT  = 'วันที่โอนชำระเงินสำเร็จ'
_COL_PAYOUT      = 'จำนวนเงินทั้งหมดที่โอนแล้ว (฿)'


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
        order_sn = str(row[_COL_ORDER_SN]).strip()
        if not order_sn or order_sn == 'nan':
            continue
        try:
            actual_payout = float(str(row[_COL_PAYOUT]).replace(',', '') or 0)
        except (ValueError, TypeError):
            actual_payout = 0.0
        settled_at = str(row[_COL_SETTLED_AT]).strip() if row[_COL_SETTLED_AT] else ''
        result.append({
            'order_sn':      order_sn,
            'actual_payout': actual_payout,
            'settled_at':    settled_at,
        })
    return result
