import pandas as pd
import pytest
from parse_balance import parse_shopee_balance, find_balance_header_row, BalanceError

HEADER = ['วันที่','ประเภทการทำธุรกรรม','คำอธิบาย','รหัสคำสั่งซื้อ',
          'รูปแบบธุรกรรม','จำนวนเงิน','สถานะ','ยอดเงินหลังทำธุรกรรมเสร็จสิ้น']

def _df(rows):
    return pd.DataFrame(rows, columns=HEADER)

def test_classifies_types_and_sign():
    df = _df([
        ['2026-06-15 13:55:39','รายรับจากคำสั่งซื้อ','#A','260610Q41E2471','เงินเข้า','35','สำเร็จ','7689'],
        ['2026-06-16 01:17:03','การถอนเงิน','อัตโนมัติ','-','เงินออก','-7689','สำเร็จ','0'],
        ['2026-02-05 15:42:13','รายการปรับปรุง','ชดเชย','-','เงินเข้า','55','สำเร็จ','856'],
    ])
    rows = parse_shopee_balance(df)
    assert [r['txn_type'] for r in rows] == ['income','withdrawal','adjustment']
    assert rows[0]['order_sn'] == '260610Q41E2471' and rows[0]['amount'] == 35.0
    assert rows[1]['order_sn'] is None and rows[1]['amount'] == -7689.0
    assert rows[1]['running_balance'] == 0.0

def test_find_header_row_past_banner():
    raw = pd.DataFrame([['รายงาน',None,None,None,None,None,None,None],
                        [None]*8, HEADER, ['2026-06-16','การถอนเงิน','x','-','เงินออก','-1','ok','0']])
    assert find_balance_header_row(raw) == 2

def test_bad_amount_raises():
    with pytest.raises(BalanceError):
        parse_shopee_balance(_df([['t','การถอนเงิน','x','-','เงินออก','notnum','ok','0']]))
