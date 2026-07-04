"""Tests for scripts/import_product_labels.py.

Covers the pure cleaning functions (no Excel file / DB needed):
  - prefix strip (label-cell quirk, e.g. "ตราสินค้า : GOLDEN LION")
  - barcode normalize (Excel float artifact) + length flagging
  - duplicate-barcode flagging
  - blank product-name row skip
  - clean_rows() end-to-end on a small synthetic sheet
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import import_product_labels as imp


# ── strip_field_prefix ──────────────────────────────────────────────────────

def test_strip_field_prefix_removes_known_prefix():
    assert imp.strip_field_prefix("ตราสินค้า : GOLDEN LION", "ตราสินค้า") == "GOLDEN LION"


def test_strip_field_prefix_no_prefix_passthrough():
    assert imp.strip_field_prefix("SENDAI", "ตราสินค้า") == "SENDAI"


def test_strip_field_prefix_blank_and_nan():
    assert imp.strip_field_prefix(None, "ขนาด") == ""
    assert imp.strip_field_prefix(float("nan"), "ขนาด") == ""
    assert imp.strip_field_prefix("  ", "ขนาด") == ""


def test_strip_field_prefix_no_space_colon_variant():
    assert imp.strip_field_prefix("บรรจุ:1 กล่อง", "บรรจุ") == "1 กล่อง"


# ── normalize_barcode ────────────────────────────────────────────────────────

def test_normalize_barcode_strips_excel_float_artifact():
    assert imp.normalize_barcode(8850124001234.0) == "8850124001234"


def test_normalize_barcode_string_with_trailing_dot_zero():
    assert imp.normalize_barcode("8850124001234.0") == "8850124001234"


def test_normalize_barcode_blank_and_nan():
    assert imp.normalize_barcode(None) == ""
    assert imp.normalize_barcode(float("nan")) == ""
    assert imp.normalize_barcode("") == ""


def test_normalize_barcode_plain_string_passthrough():
    assert imp.normalize_barcode("8850124001234") == "8850124001234"


# ── flag_barcode_length ──────────────────────────────────────────────────────

def test_flag_barcode_length_13_digits_ok():
    assert imp.flag_barcode_length("8850124001234") is None


def test_flag_barcode_length_12_digits_flagged():
    reason = imp.flag_barcode_length("885012400123")
    assert reason is not None and "12" in reason


def test_flag_barcode_length_15_digits_flagged():
    reason = imp.flag_barcode_length("885012400123456")
    assert reason is not None and "15" in reason


def test_flag_barcode_length_blank_not_flagged():
    assert imp.flag_barcode_length("") is None


# ── find_duplicate_barcodes ──────────────────────────────────────────────────

def test_find_duplicate_barcodes():
    barcodes = ["111", "222", "111", "", "", "333", "333"]
    assert imp.find_duplicate_barcodes(barcodes) == {"111", "333"}


def test_find_duplicate_barcodes_none_when_all_unique():
    assert imp.find_duplicate_barcodes(["111", "222", ""]) == set()


# ── is_blank_name ────────────────────────────────────────────────────────────

def test_is_blank_name():
    assert imp.is_blank_name(None) is True
    assert imp.is_blank_name("") is True
    assert imp.is_blank_name("   ") is True
    assert imp.is_blank_name(float("nan")) is True
    assert imp.is_blank_name("บานพับ") is False


# ── clean_rows (end-to-end on a synthetic sheet) ────────────────────────────

def _row(**overrides):
    base = {c: "" for c in imp.COLUMNS}
    base.update(overrides)
    return base


def test_clean_rows_skips_blank_name():
    rows = [
        _row(**{"No": "1", "ชื่อสินค้า": "บานพับ", "รหัสบาร์โค้ด": 8850124001234.0}),
        _row(**{"No": "2", "ชื่อสินค้า": "  ", "รหัสบาร์โค้ด": 8850124001235.0}),
        _row(**{"No": "3", "ชื่อสินค้า": None, "รหัสบาร์โค้ด": 8850124001236.0}),
    ]
    cleaned, skipped = imp.clean_rows(rows)
    assert skipped == 2
    assert len(cleaned) == 1
    assert cleaned[0]["product_name"] == "บานพับ"


def test_clean_rows_flags_bad_length_barcode():
    rows = [_row(**{"ชื่อสินค้า": "บานพับ", "รหัสบาร์โค้ด": 885012400123.0})]  # 12 digits
    cleaned, _ = imp.clean_rows(rows)
    assert cleaned[0]["needs_review"] == 1
    assert "12" in cleaned[0]["review_note"]


def test_clean_rows_flags_duplicate_barcode():
    rows = [
        _row(**{"ชื่อสินค้า": "บานพับ A", "รหัสบาร์โค้ด": 8850124001234.0}),
        _row(**{"ชื่อสินค้า": "บานพับ B", "รหัสบาร์โค้ด": 8850124001234.0}),
    ]
    cleaned, _ = imp.clean_rows(rows)
    assert all(r["needs_review"] == 1 for r in cleaned)
    assert all("duplicate barcode" in r["review_note"] for r in cleaned)


def test_clean_rows_valid_13_digit_barcode_not_flagged():
    rows = [_row(**{"ชื่อสินค้า": "บานพับ", "รหัสบาร์โค้ด": 8850124001234.0})]
    cleaned, _ = imp.clean_rows(rows)
    assert cleaned[0]["needs_review"] == 0
    assert cleaned[0]["review_note"] is None
    assert cleaned[0]["barcode"] == "8850124001234"


def test_clean_rows_strips_brand_and_field_prefixes():
    rows = [_row(**{
        "ชื่อสินค้า": "บานพับ",
        "รหัสบาร์โค้ด": 8850124001234.0,
        "ตราสินค้า": "ตราสินค้า : GOLDEN LION",
        "วิธีใช้": "วิธีใช้ : ใช้กับประตูไม้",
        "ขนาด": "ขนาด : 3 นิ้ว",
    })]
    cleaned, _ = imp.clean_rows(rows)
    row = cleaned[0]
    assert row["brand"] == "GOLDEN LION"
    assert row["usage_th"] == "ใช้กับประตูไม้"
    assert row["size_th"] == "3 นิ้ว"


def test_clean_rows_legacy_no_from_excel_float():
    rows = [_row(**{"ชื่อสินค้า": "บานพับ", "No": 12.0})]
    cleaned, _ = imp.clean_rows(rows)
    assert cleaned[0]["legacy_no"] == "12"


# ── build_company_block ──────────────────────────────────────────────────────

def test_build_company_block_takes_mode():
    rows = [
        _row(**{"ผู้จัดจำหน่าย": "ผู้จัดจำหน่าย : บุญสวัสดิ์นำชัย", "นำเข้าโดย": "เซ็นไดเทรดดิ้ง"}),
        _row(**{"ผู้จัดจำหน่าย": "ผู้จัดจำหน่าย : บุญสวัสดิ์นำชัย", "นำเข้าโดย": "เซ็นไดเทรดดิ้ง"}),
        _row(**{"ผู้จัดจำหน่าย": "", "นำเข้าโดย": ""}),
    ]
    block = imp.build_company_block(rows)
    assert block["distributor_th"] == "บุญสวัสดิ์นำชัย"
    assert block["importer_th"] == "เซ็นไดเทรดดิ้ง"


def test_build_company_block_all_blank_is_none():
    rows = [_row()]
    block = imp.build_company_block(rows)
    assert block["distributor_th"] is None
