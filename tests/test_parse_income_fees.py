import pandas as pd
from parse_income_transfer import parse_shopee_income_fees

def _df(**over):
    base = {
        'หมายเลขคำสั่งซื้อ': '260610Q41E2471', 'วันที่ทำการสั่งซื้อ': '2026-06-10',
        'ชื่อผู้ใช้ (ผู้ซื้อ)': 'mbz0nna9ii', 'ค่าธรรมเนียม (%)': '3.21%',
        'วันที่โอนชำระเงินสำเร็จ': '2026-06-15', 'สินค้าราคาปกติ': '55',
        'ส่วนลดสินค้าจากผู้ขาย': '-6', 'ค่าคอมมิชชั่น AMS': '-6', 'ค่าคอมมิชชั่น': '-4',
        'ค่าบริการ': '-1', 'ค่าธรรมเนียมโครงสร้างพื้นฐานแพลตฟอร์ม': '0',
        'ค่าธุรกรรมการชำระเงิน': '-1', 'ค่าธรรมเนียมเติมเงินโฆษณาจากเงิน Escrow': '0',
        'ภาษี': '0', 'ค่าจัดส่งที่ชำระโดยผู้ซื้อ': '29', 'ค่าจัดส่งสินค้าที่ออกโดย Shopee': '-29',
        'ค่าจัดส่งที่ Shopee ชำระโดยชื่อของคุณ': '0',
        'ค่าธรรมเนียม ของโปรแกรมประหยัดค่าจัดส่ง': '-2',
        'จำนวนเงินทั้งหมดที่โอนแล้ว (฿)': '35',
    }
    base.update(over)
    return pd.DataFrame([base])

def test_fee_buckets_and_net():
    rows = parse_shopee_income_fees(_df())
    assert len(rows) == 1
    r = rows[0]
    assert r['order_sn'] == '260610Q41E2471'
    assert r['net_payout'] == 35.0
    assert r['item_value'] == 49.0            # 55 + (-6)
    assert r['fee_commission'] == -10.0       # AMS -6 + comm -4
    assert r['fee_transaction'] == -1.0
    assert r['shipping_net'] == 0.0           # 29 - 29 + 0
    assert r['fee_saver'] == -2.0
    assert r['fee_pct'] == '3.21%'
    # fee_total = item_value - net_payout (the satang-true identity)
    assert r['fee_total'] == round(49.0 - 35.0, 2)
    assert 'fee_raw_json' in r and '260610Q41E2471' in r['fee_raw_json']

def test_blank_order_sn_skipped():
    assert parse_shopee_income_fees(_df(**{'หมายเลขคำสั่งซื้อ': ''})) == []
