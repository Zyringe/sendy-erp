"""Tests for Shopee Income Transfer file parser."""
import pandas as pd
import pytest
from parse_income_transfer import parse_shopee_income, IncomeTransferError


def _make_df(rows):
    """Build a mock Income-sheet DataFrame with the exact Thai column headers."""
    cols = [
        'ลำดับที่',
        'หมายเลขคำสั่งซื้อ',       # index 1  — order_sn
        'รหัสคืนสินค้า',
        'ชื่อผู้ใช้ (ผู้ซื้อ)',
        'วันที่ทำการสั่งซื้อ',
        'ช่องทางการชำระเงินของผู้ซื้อ',
        'Hot Listing',
        'ช่องทางการชำระเงิน (รายละเอียด)',
        'แผนการผ่อนชำระ',
        'ค่าธรรมเนียม (%)',
        'วันที่โอนชำระเงินสำเร็จ',   # index 10 — settled_at
        'สินค้าราคาปกติ',
        'ส่วนลดสินค้าจากผู้ขาย',
        'จำนวนเงินที่ทำการคืนให้ผู้ซื้อ',
        'ส่วนลดสินค้าที่ออกโดย Shopee',
        'โค้ดส่วนลดที่ออกโดยผู้ขาย',
        'โค้ดส่วนลดร่วมที่ออกโดยผู้ขาย',
        'Coins Cashback ที่สนับสนุนโดยผู้ขาย',
        'Coins Cashback ร่วมที่สนับสนุนโดยผู้ขาย',
        'ค่าจัดส่งที่ชำระโดยผู้ซื้อ',
        'ค่าจัดส่งสินค้าที่ออกโดย Shopee',
        'ค่าจัดส่งที่ Shopee ชำระโดยชื่อของคุณ',
        'ค่าจัดส่งสินค้าคืน',
        'ค่าจัดส่งสินค้าคืนผู้ขาย',
        'โปรแกรมประหยัดค่าจัดส่งคืนสินค้า',
        'ค่าคอมมิชชั่น AMS',
        'ค่าคอมมิชชั่น',
        'ค่าบริการ',
        'ค่าธรรมเนียมโครงสร้างพื้นฐานแพลตฟอร์ม',
        'ค่าธรรมเนียม ของโปรแกรมประหยัดค่าจัดส่ง',
        'ค่าธุรกรรมการชำระเงิน',
        'ภาษี',
        'ค่าธรรมเนียมเติมเงินโฆษณาจากเงิน Escrow',
        'ค่าบริการติดตั้งที่ชำระโดยผู้ซื้อ',
        'ค่าบริการติดตั้งจริงจากผู้ให้บริการ',
        'โบนัสส่วนลดเครื่องเก่าแลกใหม่จากผู้ขาย',
        'จำนวนเงินทั้งหมดที่โอนแล้ว (฿)',  # index 36 — actual_payout
    ]
    data = []
    for r in rows:
        row = [''] * len(cols)
        row[1]  = r['order_sn']
        row[10] = r['settled_at']
        row[36] = r['actual_payout']
        data.append(row)
    return pd.DataFrame(data, columns=cols)


def test_parse_returns_correct_fields():
    df = _make_df([
        {'order_sn': '26050192H4FY9X', 'settled_at': '2026-05-10', 'actual_payout': '211.00'},
    ])
    result = parse_shopee_income(df)
    assert len(result) == 1
    r = result[0]
    assert r['order_sn'] == '26050192H4FY9X'
    assert r['settled_at'] == '2026-05-10'
    assert r['actual_payout'] == pytest.approx(211.0)


def test_parse_multiple_orders():
    df = _make_df([
        {'order_sn': 'A001', 'settled_at': '2026-06-01', 'actual_payout': '100.00'},
        {'order_sn': 'A002', 'settled_at': '2026-06-01', 'actual_payout': '250.50'},
        {'order_sn': 'A003', 'settled_at': '2026-06-02', 'actual_payout': '75.00'},
    ])
    result = parse_shopee_income(df)
    assert len(result) == 3
    assert result[1]['order_sn'] == 'A002'
    assert result[1]['actual_payout'] == pytest.approx(250.5)


def test_skips_rows_with_empty_order_sn():
    df = _make_df([
        {'order_sn': 'A001', 'settled_at': '2026-06-01', 'actual_payout': '100.00'},
        {'order_sn': '',     'settled_at': '2026-06-01', 'actual_payout': '50.00'},
    ])
    result = parse_shopee_income(df)
    assert len(result) == 1
    assert result[0]['order_sn'] == 'A001'


def test_raises_on_missing_required_column():
    df = pd.DataFrame({'some_column': ['value']})
    with pytest.raises(IncomeTransferError, match='หมายเลขคำสั่งซื้อ'):
        parse_shopee_income(df)


def test_handles_zero_payout():
    """Cancelled orders can have actual_payout = 0."""
    df = _make_df([
        {'order_sn': 'CANCEL001', 'settled_at': '2026-06-01', 'actual_payout': '0'},
    ])
    result = parse_shopee_income(df)
    assert result[0]['actual_payout'] == pytest.approx(0.0)
