"""Parser for Shopee Seller Balance transaction reports
(my_balance_transaction_report.*.xlsx). One sheet 'Transaction Report' with a
metadata banner above the real header row. Each row is a wallet event:
รายรับจากคำสั่งซื้อ (order income), การถอนเงิน (bank withdrawal = a real bank
deposit), or รายการปรับปรุง (adjustment). The withdrawal rows + the running
balance are the ground truth for which orders make up each bank deposit.
"""
import pandas as pd

SHEET = 'Transaction Report'
_C_TIME='วันที่'; _C_TYPE='ประเภทการทำธุรกรรม'; _C_DESC='คำอธิบาย'
_C_SN='รหัสคำสั่งซื้อ'; _C_AMT='จำนวนเงิน'; _C_BAL='ยอดเงินหลังทำธุรกรรมเสร็จสิ้น'

_TYPE_MAP = {'รายรับจากคำสั่งซื้อ':'income', 'การถอนเงิน':'withdrawal',
             'รายการปรับปรุง':'adjustment'}


class BalanceError(Exception):
    pass


def _to_float(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).replace(',', '').strip()
    if s == '' or s.lower() == 'nan':
        return None
    try:
        return float(s)
    except ValueError:
        return None


def find_balance_header_row(raw_df, max_scan=25):
    """0-based index of the header row (cells include _C_TIME and _C_TYPE)."""
    limit = min(max_scan, len(raw_df))
    for i in range(limit):
        cells = {str(v).strip() for v in raw_df.iloc[i].tolist() if not pd.isna(v)}
        if _C_TIME in cells and _C_TYPE in cells:
            return i
    raise BalanceError(
        f'ไม่พบแถวหัวตาราง ("{_C_TIME}"/"{_C_TYPE}") ใน {limit} แถวแรก '
        '— ต้องเป็นไฟล์ Seller Balance (my_balance_transaction_report) จาก Shopee ค่ะ')


def load_balance_sheet(source):
    """Read the 'Transaction Report' sheet header-aware → str DataFrame."""
    try:
        raw = pd.read_excel(source, sheet_name=SHEET, header=None, dtype=str)
    except Exception as e:
        raise BalanceError(f'อ่านชีต "{SHEET}" ไม่ได้: {e}')
    hdr = find_balance_header_row(raw)
    df = raw.iloc[hdr + 1:].reset_index(drop=True)
    df.columns = [str(c).strip() for c in raw.iloc[hdr].tolist()]
    return df


def parse_shopee_balance(df):
    """List of wallet rows: {txn_time, txn_type, order_sn, amount,
    running_balance, description}. Rows with no amount are skipped (blank
    spacer rows). Raises BalanceError on an unknown txn type or unparseable
    amount on a real (typed) row."""
    out = []
    for _, r in df.iterrows():
        typ_raw = r.get(_C_TYPE)
        if typ_raw is None or pd.isna(typ_raw) or str(typ_raw).strip() == '':
            continue
        typ_raw = str(typ_raw).strip()
        if typ_raw not in _TYPE_MAP:
            raise BalanceError(f'ประเภทธุรกรรมไม่รู้จัก: {typ_raw!r}')
        amt = _to_float(r.get(_C_AMT))
        if amt is None:
            raise BalanceError(f'จำนวนเงินอ่านไม่ได้ในแถว {typ_raw!r}: {r.get(_C_AMT)!r}')
        sn = r.get(_C_SN)
        sn = None if (sn is None or pd.isna(sn) or str(sn).strip() in ('', '-')) else str(sn).strip()
        out.append({
            'txn_time':        '' if pd.isna(r.get(_C_TIME)) else str(r.get(_C_TIME)).strip(),
            'txn_type':        _TYPE_MAP[typ_raw],
            'order_sn':        sn,
            'amount':          round(amt, 2),
            'running_balance': _to_float(r.get(_C_BAL)),
            'description':     '' if pd.isna(r.get(_C_DESC)) else str(r.get(_C_DESC)).strip(),
        })
    return out
