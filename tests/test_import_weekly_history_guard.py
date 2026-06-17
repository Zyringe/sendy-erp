"""Unit tests for the history-export guard heuristic
parse_weekly.is_history_export().

A full-history Express export (ประวัติการขาย_แยกตามลูกค้า / ประวัติการซื้อ) must be
flagged True so callers can warn before re-importing it; normal weekly files must
be False. The route-level preview/confirm safety (no insert until confirm) is now
exercised on the unified box — see test_unified_import_routes.py.
"""
import os

import pytest

os.environ.setdefault('SKIP_DB_INIT', '1')

import parse_weekly

# ── Fixture content ──────────────────────────────────────────────────────────
#
# History export header: title + วันที่จาก spanning 2567→2569 (3-year range).
# Content is minimal — the guard only reads the first ~15 lines.
_HISTORY_SALES_LINES = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                                                                             หน้า   :        1"',
    '"  รายงานประวัติการขาย\xa0แยกตามลูกค้า"',
    '"รหัสลูกค้า  01ก01                ถึง  Zหน้าร้าน                                                                      วันที่ : 29/05/69"',
    '"วันที่จาก   1\xa0ม.ค.\xa02567          ถึง  31\xa0ธ.ค.\xa02569"',
    '"รหัสสินค้า  000ก4001             ถึง  แบบ"',
    '"พนักงานขาย                       ถึง  S02                เลือกแผนก  *"',
    '"--------------------------------------------------------------------------------------------------------------------------------------"',
    '"  สินค้า วันที่ เลขที่เอกสาร          จำนวน   คืน   ราคาต่อหน่วย\xa0VAT   ส่วนลด       รวมเงิน  ส่วนลดรวม  ยอดขายสุทธิ  อ้างอิง  หมายเหตุ"',
    '"--------------------------------------------------------------------------------------------------------------------------------------"',
    '"  เกียรติทวีฮาร์ดแวร์ /01ก11"',
    '"   ใบตัดเพชร 4" #GL-888(แดง) /031บ4120"',
    '"      04/07/68   IV6801757-  1        50.00 ใบ          149.54  2                  7477.00                  7477.00"',
]

# History export header — SINGLE Buddhist year (the real-world re-export shape
# that the year-crossing heuristic missed). Modeled byte-for-byte on the real
# file data/source/new_source/bsn_ประวัติขาย_1.3.69-19.4.69.csv:
#   report date  วันที่ : 20/04/69   (export run 20 เม.ย. 2569)
#   filter       วันที่จาก 1 มี.ค. 2569 ถึง 19 เม.ย. 2569  (same BE year)
# Reach-back = report(2026-04-20) − filter_start(2026-03-01) = 50 days → history.
_HISTORY_SALES_SINGLE_YEAR_LINES = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                                                                             หน้า   :        1"',
    '"  รายงานประวัติการขาย\xa0แยกตามลูกค้า"',
    '"รหัสลูกค้า                       ถึง  Zหน้าร้าน                                                                      วันที่ : 20/04/69"',
    '"วันที่จาก   1\xa0มี.ค.\xa02569         ถึง  19\xa0เม.ย.\xa02569"',
    '"รหัสสินค้า  000ก4001             ถึง  แบบ"',
    '"พนักงานขาย                       ถึง  S02                เลือกแผนก  *"',
    '"--------------------------------------------------------------------------------------------------------------------------------------"',
    '"  สินค้า วันที่ เลขที่เอกสาร          จำนวน   คืน   ราคาต่อหน่วย\xa0VAT   ส่วนลด       รวมเงิน  ส่วนลดรวม  ยอดขายสุทธิ  อ้างอิง  หมายเหตุ"',
    '"--------------------------------------------------------------------------------------------------------------------------------------"',
    '"  เกียรติทวีฮาร์ดแวร์ /01ก11"',
    '"   ใบตัดเพชร 4" #GL-888(แดง) /031บ4120"',
    '"      02/03/69   IV6900100-  1        10.00 ใบ          149.54  2                  1495.40                  1495.40"',
    '"      18/04/69   IV6900200-  1        20.00 ใบ          149.54  2                  2990.80                  2990.80"',
]

# History export header for purchases (ซื้อ variant, multi-year).
_HISTORY_PURCH_LINES = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                                                                            หน้า   :        1"',
    '"  รายงานประวัติการซื้อ\xa0แยกตามผู้จำหน่าย"',
    '"รหัสผู้จำหน่ายจาก  AA01             ถึง  ZZ99                                                                       วันที่ : 29/05/69"',
    '"วันที่จาก           1\xa0ม.ค.\xa02567   ถึง  31\xa0ธ.ค.\xa02569"',
    '"รหัสสินค้าจาก      000ก4001             ถึง  แบบ                    เลือกแผนก [*   ]"',
    '"-------------------------------------------------------------------------------------------------------------------------------------"',
    '"   สินค้า  วันที่  เลขที่เอกสาร       จำนวน   คืน  ราคาต่อหน่วย\xa0VAT\xa0\xa0 ส่วนลด       รวมเงิน  ส่วนลดรวม     ยอดซื้อสุทธิ อ้างถึง"',
    '"-------------------------------------------------------------------------------------------------------------------------------------"',
]

# Normal weekly sales file header (same year: 2569→2569).
# Taken from the conftest SALES_SAMPLE_LINES (which is already a valid weekly
# fixture), so we re-use that fixture for the route test.

# Normal weekly purchase with same-year date range.
_WEEKLY_PURCH_SAME_YEAR_LINES = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                                                                            หน้า   :        1"',
    '"  รายงานประวัติการซื้อ\xa0แยกตามผู้จำหน่าย"',
    '"รหัสผู้จำหน่ายจาก                       ถึง  ไพ                                                                     วันที่ : 24/04/69"',
    '"วันที่จาก          23\xa0เม.ย.\xa02569        ถึง  31\xa0ธ.ค.\xa02569"',
    '"รหัสสินค้าจาก      000ก4001             ถึง  แบบ                    เลือกแผนก [*   ]"',
    '"-------------------------------------------------------------------------------------------------------------------------------------"',
    '"   สินค้า  วันที่  เลขที่เอกสาร       จำนวน   คืน  ราคาต่อหน่วย\xa0VAT\xa0\xa0 ส่วนลด       รวมเงิน  ส่วนลดรวม     ยอดซื้อสุทธิ อ้างถึง"',
    '"-------------------------------------------------------------------------------------------------------------------------------------"',
    '"  ย้งเจริญการพิมพ์\xa0/ย้ง"',
    '"   กล่องในปุ๊ก#7\xa0/Pกล่อง3"',
    '"        24/04/69   HP6900023       22965.00 กล            0.69  0                 15845.85                 15845.85 PO0000227-  1"',
]


@pytest.fixture
def history_sales_file(tmp_path):
    p = tmp_path / "ประวัติการขาย_แยกตามลูกค้า_full_29.5.69.csv"
    p.write_text("\n".join(_HISTORY_SALES_LINES) + "\n", encoding="cp874")
    return str(p)


@pytest.fixture
def history_sales_single_year_file(tmp_path):
    p = tmp_path / "ประวัติการขาย_แยกตามลูกค้า_1.3.69-19.4.69.csv"
    p.write_text("\n".join(_HISTORY_SALES_SINGLE_YEAR_LINES) + "\n", encoding="cp874")
    return str(p)


@pytest.fixture
def history_purch_file(tmp_path):
    p = tmp_path / "ประวัติการซื้อ_full.csv"
    p.write_text("\n".join(_HISTORY_PURCH_LINES) + "\n", encoding="cp874")
    return str(p)


@pytest.fixture
def weekly_purch_file(tmp_path):
    p = tmp_path / "ซื้อ_sample_weekly.csv"
    p.write_text("\n".join(_WEEKLY_PURCH_SAME_YEAR_LINES) + "\n", encoding="cp874")
    return str(p)


# ── is_history_export() unit tests ──────────────────────────────────────────

def test_history_sales_detected(history_sales_file):
    """History sales export (start 2567 < end 2569) must return True."""
    assert parse_weekly.is_history_export(history_sales_file) is True


def test_history_purch_detected(history_purch_file):
    """History purchase export (start 2567 < end 2569) must return True."""
    assert parse_weekly.is_history_export(history_purch_file) is True


def test_history_sales_single_year_detected(history_sales_single_year_file):
    """A full-history export confined to ONE Buddhist year (filter start far
    before the report date) must still be detected as history.

    Regression for the blocker: the old year-crossing heuristic returned False
    for single-year history dumps, letting them through /import-weekly and
    re-corrupting stock. Reach-back = 50 days (2026-03-01 → 2026-04-20).
    """
    assert parse_weekly.is_history_export(history_sales_single_year_file) is True


def test_weekly_sales_not_history(sample_sales_file):
    """Normal weekly sales (same-year date range) must return False."""
    assert parse_weekly.is_history_export(sample_sales_file) is False


def test_weekly_purch_not_history(sample_purchase_file):
    """Normal weekly purchase (same-year date range) must return False."""
    assert parse_weekly.is_history_export(sample_purchase_file) is False


def test_weekly_purch_same_year_not_history(weekly_purch_file):
    """Weekly purchase with same-year วันที่จาก must return False."""
    assert parse_weekly.is_history_export(weekly_purch_file) is False
