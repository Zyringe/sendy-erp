"""TikTok Shop income file (income_*.xlsx) -> per-order payout + fee breakdown.

Sheet รายละเอียดคำสั่งซื้อ holds one row per order or adjustment (68 columns);
sheet รายงาน holds the period summary, used here only as an oracle. The other
two sheets (บันทึกการถอน, คำอธิบายค่าธรรมเนียม) are not read: the money is still
in TikTok (Put, 2026-09-30), so no wallet or withdrawal rows are written.

Returns {'settlements', 'fee_rows', 'adjustments', 'total'}:
  settlements  [{order_sn, actual_payout, settled_at}]  -> upsert_marketplace_settlements
  fee_rows     [{order_sn, item_value, fee_*, shipping_net, fee_total, net_payout,
                 fee_raw_json}]                          -> upsert_marketplace_fees
  adjustments  [{id, type, amount}]: rows whose ประเภทธุรกรรม is not คำสั่งซื้อ. Not
               imported; the route names them so none is dropped silently.
  total        Σ ยอดการชำระเงินทั้งหมด over every row.

Three checks refuse the whole file (TikTokIncomeError), so nothing is written:
  (a) per row, Σ leaf fee buckets == ค่าธรรมเนียมทั้งหมด, to the satang;
  (b) per ORDER row, รายได้ทั้งหมด + ค่าธรรมเนียมทั้งหมด + จำนวนการปรับยอด ==
      ยอดการชำระเงินทั้งหมด (TikTok's own identity, true on every sample row).
      Adjustment rows are exempt: they are not imported, (c) already pins their
      money, and the sample has none, so their shape is unknown;
  (c) Σ ยอดการชำระเงินทั้งหมด over ALL rows == the รายงาน sheet's total.
A column this module does not know, holding any non-zero value, is refused by
name rather than guessed into a bucket.

Read with pandas, never openpyxl read_only=True: that stops after 1 data row on
this file.
"""
import json

import pandas as pd

from marketplace_fee_buckets import TIKTOK_BUCKET

DETAIL_SHEET = 'รายละเอียดคำสั่งซื้อ'
REPORT_SHEET = 'รายงาน'

_ID = 'หมายเลขคำสั่งซื้อ/การปรับ'
_TYPE = 'ประเภทธุรกรรม'
_ORDER_TYPE = 'คำสั่งซื้อ'
_SETTLED = 'เวลาที่ชำระคำสั่งซื้อ'
_PAYOUT = 'ยอดการชำระเงินทั้งหมด'
_REVENUE = 'รายได้ทั้งหมด'
_FEES = 'ค่าธรรมเนียมทั้งหมด'
_ADJUST = 'จำนวนการปรับยอด'

# Parts of the subtotal columns in TIKTOK_BUCKET. Known, never summed: the
# subtotal already carries them.
TIKTOK_SUBTOTAL_PARTS = {
    # under ยอดรวมค่าจัดส่งที่ร้านค้าจ่ายจริง
    'ค่าธรรมเนียมการจัดส่งจริง', 'ส่วนลดค่าธรรมเนียมการจัดส่งจากแพลตฟอร์ม',
    'ค่าธรรมเนียมการจัดส่งของลูกค้า', 'ค่าธรรมเนียมการจัดส่งสินค้าคืนตามจริง',
    'เงินคืนสำหรับค่าจัดส่ง', 'เงินสนับสนุนการจัดส่ง',
    'ค่าจัดส่งสินค้าที่แลกเปลี่ยน (ลูกค้าเป็นผู้จ่าย)', 'ค่าจัดส่งสินค้าทดแทน (ลูกค้าเป็นผู้จ่าย)',
    'Guarantee program reimbursement',
    # under ค่าคอมมิชชั่นแอฟฟิลิเอต
    'ค่าคอมมิชชั่นไม่ใช่แอฟฟิลิเอตก่อนหักภาษีเงินได้บุคคลธรรมดา',
    'ภาษีเงินได้บุคคลธรรมดาที่หักไว้จากคอมมิชชั่นของแอฟฟิลิเอต',
    # under ค่าคอมมิชชั่นแอฟฟิลิเอตสำหรับโฆษณาร้านค้า
    'ค่าคอมมิชชั่นโฆษณาร้านค้าแอฟฟิลิเอตก่อน PIT',
    'ภาษีเงินได้บุคคลธรรมดาหัก ณ ที่จ่ายจากค่าคอมมิชชั่นโฆษณาร้านค้าแอฟฟิลิเอต',
}

# Identity, totals, the revenue breakdown and the buyer-side detail: known, not fees.
_OTHER_COLUMNS = {
    _ID, _TYPE, 'เวลาที่สร้างคำสั่งซื้อ', _SETTLED, 'สกุลเงิน', _PAYOUT, _REVENUE,
    'ยอดรวมค่าสินค้าหลังหักส่วนลดจากผู้ขาย', 'ยอดรวมค่าสินค้าก่อนหักส่วนลด', 'ส่วนลดจากร้านค้า',
    'ยอดรวมเงินคืนหลังหักส่วนลดจากผู้ขาย', 'ยอดรวมเงินคืนก่อนหักส่วนลดจากร้านค้า',
    'เงินคืนจากส่วนลดร้านค้า', _FEES, _ADJUST, 'หมายเลขคำสั่งซื้อที่เกี่ยวข้อง',
    'การชำระเงินของลูกค้า', 'การคืนเงินจากลูกค้า', 'คูปองส่วนลดร่วมของผู้ขาย',
    'การคืนเงินคูปองส่วนลดร่วมของผู้ขาย', 'ส่วนลดจากแพลตฟอร์ม', 'การคืนเงินส่วนลดจากแพลตฟอร์ม',
    'คูปองส่วนลดร่วมของแพลตฟอร์ม', 'การคืนเงินคูปองส่วนลดร่วมของแพลตฟอร์ม',
    'ส่วนลดค่าจัดส่งจากร้านค้า', 'น้ำหนักพัสดุโดยประมาณ', 'น้ำหนักที่เรียกเก็บเงิน',
    'รายละเอียดสินค้าที่ขายได้', 'ธนาคารของลูกค้าสำหรับการชำระเงิน',
}

_FEE_COLUMNS = set(TIKTOK_BUCKET) | TIKTOK_SUBTOTAL_PARTS
_KNOWN = _FEE_COLUMNS | _OTHER_COLUMNS
_BUCKETS = ('fee_commission', 'fee_service', 'fee_transaction', 'fee_platform',
            'fee_ads_escrow', 'shipping_net')


class TikTokIncomeError(ValueError):
    """The income file is refused as a whole; nothing may be written from it."""


def load_tiktok_income(source):
    """xlsx (path or file-like) -> (detail DataFrame, report DataFrame), dtype=str."""
    try:
        detail = pd.read_excel(source, sheet_name=DETAIL_SHEET, dtype=str)
        if hasattr(source, 'seek'):
            source.seek(0)
        report = pd.read_excel(source, sheet_name=REPORT_SHEET, header=None, dtype=str)
    except ValueError as e:                      # pandas: "Worksheet named ... not found"
        raise TikTokIncomeError(f'ไฟล์ Income ของ TikTok ไม่ครบ: {e}')
    return detail, report


def _cell(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ''
    return str(v).strip()


def _money(v, where):
    s = _cell(v).replace(',', '')
    if s == '':
        return 0.0
    try:
        return float(s)
    except ValueError:
        raise TikTokIncomeError(f'{where}: ไม่ใช่ตัวเลข: {s!r}')


def _is_zero(s):
    try:
        return float(s) == 0
    except ValueError:
        return False


def _date(v, where):
    """'YYYY/MM/DD' -> 'YYYY-MM-DD'. A row in the income file is settled, so a
    blank or unparseable date is refused, never stored raw."""
    s = _cell(v)
    try:
        return pd.to_datetime(s, format='%Y/%m/%d').strftime('%Y-%m-%d')
    except (ValueError, TypeError):
        raise TikTokIncomeError(f'{where}: วันที่ชำระไม่ใช่ YYYY/MM/DD: {s!r}')


def _report_total(report):
    for _, row in report.iterrows():
        cells = [_cell(v) for v in row]
        if _PAYOUT in cells:
            vals = [c for c in cells[cells.index(_PAYOUT) + 1:] if c]
            if vals:
                return _money(vals[-1], f'{REPORT_SHEET} {_PAYOUT}')
    raise TikTokIncomeError(f'ไม่พบ {_PAYOUT} ในชีต {REPORT_SHEET}')


def parse_tiktok_income(detail, report):
    missing = {_ID, _TYPE, _SETTLED, _PAYOUT, _REVENUE, _FEES, _ADJUST} - set(detail.columns)
    if missing:
        raise TikTokIncomeError(f'ไฟล์ Income ของ TikTok ขาดคอลัมน์: {sorted(missing)}')

    unknown = []
    for col in detail.columns:
        if col in _KNOWN:
            continue
        for v in detail[col]:
            s = _cell(v).replace(',', '')
            if s and not _is_zero(s):
                unknown.append(str(col))
                break
    if unknown:
        raise TikTokIncomeError('คอลัมน์ที่ยังไม่รู้จักมีค่า (ต้องเพิ่มใน TIKTOK_BUCKET ก่อน): '
                                + ', '.join(unknown))

    settlements, fee_rows, adjustments = [], [], []
    total = 0.0
    for _, row in detail.iterrows():
        rid = _cell(row[_ID])
        if not rid:
            continue
        payout = _money(row[_PAYOUT], f'{rid} {_PAYOUT}')
        revenue = _money(row[_REVENUE], f'{rid} {_REVENUE}')
        fees = _money(row[_FEES], f'{rid} {_FEES}')
        adjust = _money(row[_ADJUST], f'{rid} {_ADJUST}')
        buckets = dict.fromkeys(_BUCKETS, 0.0)
        for col, bucket in TIKTOK_BUCKET.items():
            if col in detail.columns:
                buckets[bucket] += _money(row[col], f'{rid} {col}')
        buckets = {k: round(v, 2) for k, v in buckets.items()}

        if round(sum(buckets.values()), 2) != round(fees, 2):                           # (a)
            raise TikTokIncomeError(f'{rid}: ค่าธรรมเนียมแยกรายการรวม {sum(buckets.values()):.2f} '
                                    f'≠ {_FEES} {fees:.2f}')
        kind = _cell(row[_TYPE])
        if kind == _ORDER_TYPE and round(revenue + fees + adjust, 2) != round(payout, 2):  # (b)
            raise TikTokIncomeError(f'{rid}: {_REVENUE} {revenue:.2f} + {_FEES} {fees:.2f} + '
                                    f'{_ADJUST} {adjust:.2f} ≠ {_PAYOUT} {payout:.2f}')
        total += payout

        if kind != _ORDER_TYPE:
            adjustments.append({'id': rid, 'type': kind, 'amount': round(payout, 2)})
            continue
        settlements.append({'order_sn': rid, 'actual_payout': round(payout, 2),
                            'settled_at': _date(row[_SETTLED], rid)})
        # Fee columns as numbers (the smart label reads them); the rest as text,
        # so an 18-digit order id is not rounded through a float.
        raw = {str(col): (_money(v, f'{rid} {col}') if col in _FEE_COLUMNS else (_cell(v) or None))
               for col, v in row.items()}
        fee_rows.append(dict(buckets, order_sn=rid, item_value=round(revenue, 2),
                             net_payout=round(payout, 2),
                             fee_total=round(revenue - payout, 2),
                             fee_raw_json=json.dumps(raw, ensure_ascii=False)))

    total = round(total, 2)
    expected = _report_total(report)
    if total != round(expected, 2):                                                        # (c)
        raise TikTokIncomeError(f'{_PAYOUT} รวมทุกแถว {total:.2f} ≠ ชีต {REPORT_SHEET} '
                                f'{expected:.2f}')
    return {'settlements': settlements, 'fee_rows': fee_rows,
            'adjustments': adjustments, 'total': total}
