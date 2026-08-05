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


# ── year-boundary regression (independent review, 2026-08-03) ───────────────
#
# Express defaults the "ถึง" end of the filter to 31 ธ.ค. of the CURRENT year, so
# a weekly exported in early January starts in the old BE year and ends in the
# new one. The year-span rule fired before the reach-back rule, so every
# January weekly was hard-blocked as "history" — deterministic, once a year.

_JAN_WEEKLY = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                  หน้า   :        1"',
    '"  รายงานประวัติการขาย\xa0แยกตามลูกค้า"',
    '"รหัสลูกค้า                       ถึง  Zหน้าร้าน            วันที่ : 03/01/70"',
    '"วันที่จาก   28\xa0ธ.ค.\xa02569        ถึง  31\xa0ธ.ค.\xa02570"',
    '"------------------------------------------------------------------"',
]

# A start date whose Thai month abbreviation is NOT in _THAI_MONTH_ABBR, so the
# reach-back cannot be computed and only the year span is available.
_UNPARSEABLE_MONTH_MULTIYEAR = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                  หน้า   :        1"',
    '"  รายงานประวัติการขาย\xa0แยกตามลูกค้า"',
    '"รหัสลูกค้า                       ถึง  Zหน้าร้าน            วันที่ : 29/05/69"',
    '"วันที่จาก   1\xa0XX.\xa02567        ถึง  31\xa0ธ.ค.\xa02569"',
    '"------------------------------------------------------------------"',
]


@pytest.fixture
def jan_weekly_file(tmp_path):
    p = tmp_path / "ขาย_3.1.70.csv"
    p.write_text("\n".join(_JAN_WEEKLY) + "\n", encoding="cp874")
    return str(p)


@pytest.fixture
def unparseable_month_multiyear_file(tmp_path):
    p = tmp_path / "ประวัติการขาย_badmonth.csv"
    p.write_text("\n".join(_UNPARSEABLE_MONTH_MULTIYEAR) + "\n", encoding="cp874")
    return str(p)


def test_january_weekly_is_not_history(jan_weekly_file):
    """Reach-back 6 days (28 ธ.ค. 2569 → 3 ม.ค. 2570) is a weekly by any
    threshold; crossing the BE year boundary must not by itself mean history."""
    assert parse_weekly.is_history_export(jan_weekly_file) is False


def test_multiyear_still_history_when_reach_back_is_uncomputable(
        unparseable_month_multiyear_file):
    """The year-span signal must survive as the FALLBACK it was written to be:
    when the Thai month can't be parsed there is no reach-back to test, and a
    multi-year span is history outright."""
    assert parse_weekly.is_history_export(unparseable_month_multiyear_file) is True


# ── Put's policy-A tightening (2026-08-03, after the independent review) ────
#
# Measured on every real Express export in inventory_app/imports/: the widest
# genuine weekly reaches back 36 days, the narrowest single-year history export
# reaches back 50. The threshold sits between them.

def _hdr(report_ddmmyy, start_day, start_month, start_be, end_be='2569',
         title='รายงานประวัติการขาย\xa0แยกตามลูกค้า'):
    return [
        '"(BSN)บจก.บุญสวัสดิ์นำชัย                          หน้า   :        1"',
        f'"  {title}"',
        f'"รหัสลูกค้า            ถึง  Zหน้าร้าน         วันที่ : {report_ddmmyy}"',
        f'"วันที่จาก   {start_day}\xa0{start_month}\xa0{start_be}   ถึง  31\xa0ธ.ค.\xa0{end_be}"',
        '"--------------------------------------------------------------"',
    ]


def _write_hdr(tmp_path, name, lines):
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="cp874")
    return str(p)


def test_reach_back_just_under_the_threshold_is_weekly(tmp_path):
    """41 days back from a 12 พ.ค. export = 1 เม.ย. — still a (wide) weekly."""
    p = _write_hdr(tmp_path, 'ขาย_wide.csv', _hdr('12/05/69', 1, 'เม.ย.', '2569'))
    assert parse_weekly.is_history_export(p) is False


def test_reach_back_just_over_the_threshold_is_history(tmp_path):
    """43 days back from a 12 พ.ค. export = 30 มี.ค. — over the line."""
    p = _write_hdr(tmp_path, 'ประวัติ_narrow.csv', _hdr('12/05/69', 30, 'มี.ค.', '2569'))
    assert parse_weekly.is_history_export(p) is True


def test_the_widest_real_weekly_shape_is_allowed(tmp_path):
    """ขาย_4.5.69.csv / ซื้อ_4.5.69.csv: exported 4 พ.ค., filter from 29 มี.ค.
    (36 days). Both are real weeklies whose rows are already live in the ERP."""
    p = _write_hdr(tmp_path, 'ขาย_4.5.69.csv', _hdr('04/05/69', 29, 'มี.ค.', '2569'))
    assert parse_weekly.is_history_export(p) is False


def test_the_narrowest_real_history_shape_is_still_blocked(tmp_path):
    """ประวัติการขาย_1.3.69-19.4.69.csv: exported 20 เม.ย., filter from 1 มี.ค.
    (50 days)."""
    p = _write_hdr(tmp_path, 'ประวัติการขาย.csv', _hdr('20/04/69', 1, 'มี.ค.', '2569'))
    assert parse_weekly.is_history_export(p) is True


# ── the title gate is gone: dates alone decide ─────────────────────────────

def test_history_dates_are_blocked_even_without_the_แยกตาม_title(tmp_path):
    """detect_express_report routes 'รายงานประวัติการขาย เรียงตามวันที่' to sales,
    but the old title gate needed BOTH 'ประวัติ' AND 'แยกตาม' — so this shape
    reached the stock ledger ungated no matter how far back it reached."""
    p = _write_hdr(tmp_path, 'x.csv', _hdr('29/05/69', 1, 'ม.ค.', '2567'),)
    assert parse_weekly.is_history_export(p) is True
    p2 = _write_hdr(tmp_path, 'y.csv',
                    _hdr('29/05/69', 1, 'ม.ค.', '2567',
                         title='รายงานประวัติการขาย\xa0เรียงตามวันที่'))
    assert parse_weekly.is_history_export(p2) is True


def test_history_dates_are_blocked_without_the_ประวัติ_word(tmp_path):
    p = _write_hdr(tmp_path, 'z.csv',
                   _hdr('29/05/69', 1, 'ม.ค.', '2567',
                        title='รายงานการขาย\xa0แยกตามลูกค้า'))
    assert parse_weekly.is_history_export(p) is True


# ── fail CLOSED on a header we cannot read ─────────────────────────────────

def test_absent_date_filter_is_not_readable(tmp_path):
    lines = _hdr('29/05/69', 1, 'ม.ค.', '2569')
    del lines[3]                                   # drop the วันที่จาก line
    p = _write_hdr(tmp_path, 'no_filter.csv', lines)
    assert parse_weekly.date_filter_is_readable(p) is False


def test_blank_date_filter_is_not_readable(tmp_path):
    lines = _hdr('29/05/69', 1, 'ม.ค.', '2569')
    lines[3] = '"วันที่จาก                        ถึง"'
    p = _write_hdr(tmp_path, 'blank_filter.csv', lines)
    assert parse_weekly.date_filter_is_readable(p) is False


def test_a_real_weekly_header_is_readable(tmp_path):
    p = _write_hdr(tmp_path, 'ขาย_ok.csv', _hdr('12/05/69', 8, 'พ.ค.', '2569'))
    assert parse_weekly.date_filter_is_readable(p) is True
