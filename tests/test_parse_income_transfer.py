"""Tests for Shopee Income Transfer file parser."""
import numpy as np
import pandas as pd
import pytest
from parse_income_transfer import (parse_shopee_income, IncomeTransferError,
                                   load_income_sheet, find_income_header_row)


def _make_df(rows):
    """Build a mock Income-sheet DataFrame with the exact Thai column headers.

    Unset/blank cells default to np.nan to mirror what production
    pd.read_excel(dtype=str) actually yields for empty cells (NOT '').
    """
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
        row = [np.nan] * len(cols)
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
    # A truly-blank order_sn cell is np.nan from read_excel(dtype=str), not ''.
    df = _make_df([
        {'order_sn': 'A001',   'settled_at': '2026-06-01', 'actual_payout': '100.00'},
        {'order_sn': np.nan,   'settled_at': '2026-06-01', 'actual_payout': '50.00'},
        {'order_sn': '',       'settled_at': '2026-06-01', 'actual_payout': '50.00'},
    ])
    result = parse_shopee_income(df)
    assert len(result) == 1
    assert result[0]['order_sn'] == 'A001'


def test_blank_payout_and_settled_at_do_not_poison_with_nan():
    """A valid order row with blank payout/settled_at cells (np.nan from
    read_excel(dtype=str)) must yield clean 0.0 / '' — never float('nan') or
    the literal string 'nan', which would silently corrupt SUM(payout) totals."""
    df = _make_df([
        {'order_sn': 'B001', 'settled_at': np.nan, 'actual_payout': np.nan},
    ])
    result = parse_shopee_income(df)
    assert len(result) == 1
    r = result[0]
    assert r['actual_payout'] == 0.0
    assert not pd.isna(r['actual_payout'])   # not float('nan')
    assert r['settled_at'] == ''             # not the literal string 'nan'

    # The poisoned value would survive SUM() — assert a batch total stays clean.
    df2 = _make_df([
        {'order_sn': 'B002', 'settled_at': '2026-06-01', 'actual_payout': '100.00'},
        {'order_sn': 'B003', 'settled_at': np.nan,        'actual_payout': np.nan},
    ])
    total = sum(x['actual_payout'] for x in parse_shopee_income(df2))
    assert total == pytest.approx(100.0)


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


# ── Header-banner handling ─────────────────────────────────────────────────
# Real Shopee Income Transfer files carry a multi-row metadata banner (seller
# name / date range / blank rows) ABOVE the real column-header row, so a plain
# header=0 read misses every column. load_income_sheet must auto-detect it.

def _write_income_xlsx(path, data_rows, banner_lines=5):
    """Write a tmp xlsx whose 'Income' sheet mirrors a REAL Shopee export:
    a `banner_lines`-row metadata banner, THEN the 37-col header row, THEN data.
    """
    header = list(_make_df([]).columns)          # the 37 real Thai headers
    width = len(header)
    banner = [
        ['รายงานรายรับ'] + [np.nan] * (width - 1),
        ['ชื่อร้าน: sendaibyboonsawat'] + [np.nan] * (width - 1),
        ['ช่วงเวลา: 2026-06-01 - 2026-06-09'] + [np.nan] * (width - 1),
        [np.nan] * width,
        [np.nan] * width,
    ][:banner_lines]
    rows = list(banner)
    rows.append(header)                          # real header at index == banner_lines
    for r in data_rows:
        row = [np.nan] * width
        row[1] = r['order_sn']
        row[10] = r['settled_at']
        row[36] = r['actual_payout']
        rows.append(row)
    pd.DataFrame(rows).to_excel(str(path), sheet_name='Income', header=False, index=False)


def test_find_income_header_row_locates_header_past_banner(tmp_path):
    p = tmp_path / 'inc.xlsx'
    _write_income_xlsx(p, [{'order_sn': 'X', 'settled_at': '2026-06-01', 'actual_payout': '1'}],
                       banner_lines=5)
    raw = pd.read_excel(str(p), sheet_name='Income', header=None, dtype=str)
    assert find_income_header_row(raw) == 5


def test_load_income_sheet_skips_metadata_banner(tmp_path):
    p = tmp_path / 'Income.test.xlsx'
    _write_income_xlsx(p, [
        {'order_sn': '26050192H4FY9X', 'settled_at': '2026-06-01', 'actual_payout': '211.00'},
        {'order_sn': 'A002',           'settled_at': '2026-06-02', 'actual_payout': '88.50'},
    ])
    df = load_income_sheet(str(p))
    result = parse_shopee_income(df)          # the real failure mode: header=0 finds 0 columns
    assert len(result) == 2
    assert result[0]['order_sn'] == '26050192H4FY9X'
    assert result[0]['actual_payout'] == pytest.approx(211.0)
    assert result[1]['settled_at'] == '2026-06-02'


def test_load_income_sheet_raises_when_header_absent(tmp_path):
    p = tmp_path / 'bad.xlsx'
    pd.DataFrame([['just', 'some'], ['random', 'rows']]).to_excel(
        str(p), sheet_name='Income', header=False, index=False)
    with pytest.raises(IncomeTransferError):
        load_income_sheet(str(p))
