"""
Smoke tests for parse_weekly.py — the BSN cp874 parser.

Targets the real public API:
- parse_purchases(filepath) -> list of dicts
- parse_sales(filepath)     -> list of dicts
- _be_to_iso(d)             -> 'YYYY-MM-DD'  (พ.ศ. → ค.ศ.)
- detect_file_type(filepath)
"""
import parse_weekly


# ── _be_to_iso ───────────────────────────────────────────────────────────────

def test_be_to_iso_basic():
    # 04/04/69 (พ.ศ. 2569) → 2026-04-04
    assert parse_weekly._be_to_iso("04/04/69") == "2026-04-04"


def test_be_to_iso_2568():
    # 23/04/68 (พ.ศ. 2568) → 2025-04-23
    assert parse_weekly._be_to_iso("23/04/68") == "2025-04-23"


def test_be_to_iso_zero_padded():
    # Day/month already zero-padded in source — verify output is too
    assert parse_weekly._be_to_iso("01/01/69") == "2026-01-01"


# ── cp874 decoding ───────────────────────────────────────────────────────────

def test_cp874_decoding_thai_chars(sample_purchase_file):
    """Reading via cp874 should produce real Thai characters (not mojibake)."""
    with open(sample_purchase_file, encoding="cp874") as f:
        text = f.read()
    # If decoding were wrong, we'd see latin-1-style garbage instead of these.
    assert "รายงานประวัติการซื้อ" in text
    assert "บจก.บุญสวัสดิ์นำชัย" in text


# ── parse_purchases ──────────────────────────────────────────────────────────

def test_parse_purchases_extracts_required_columns(sample_purchase_file):
    entries = parse_weekly.parse_purchases(sample_purchase_file)
    assert len(entries) >= 1
    e = entries[0]

    # Every entry must carry these fields with the right types.
    required = {
        'date_iso', 'doc_no', 'qty', 'unit', 'unit_price',
        'vat_type', 'discount', 'total', 'net',
        'product_name_raw', 'product_code_raw', 'party', 'party_code',
    }
    assert required.issubset(e.keys())

    # First row of synthesized sample is HP6900023 / Pกล่อง3 / 22965 กล @ 0.69, vat=0
    assert e['date_iso']         == "2026-04-24"
    assert e['doc_no']           == "HP6900023"
    assert e['qty']              == 22965.0
    assert e['unit']             == "กล"
    assert e['unit_price']       == 0.69
    assert e['vat_type']         == 0
    assert e['net']              == 15845.85
    assert e['product_code_raw'] == "Pกล่อง3"
    assert e['party_code']       == "ย้ง"


def test_parse_purchases_be_year_2568(sample_purchase_file):
    """Second purchase row uses 23/04/68 (พ.ศ. 2568) → must yield 2025."""
    entries = parse_weekly.parse_purchases(sample_purchase_file)
    dates = [e['date_iso'] for e in entries]
    assert "2025-04-23" in dates


# ── parse_sales ──────────────────────────────────────────────────────────────

def test_parse_sales_doc_no_normalised(sample_sales_file):
    """Sales doc_no like 'IV6900503-  1' must collapse internal whitespace."""
    entries = parse_weekly.parse_sales(sample_sales_file)
    assert len(entries) >= 1
    doc_nos = [e['doc_no'] for e in entries]
    # Embedded spaces stripped: "IV6900503-  1" → "IV6900503-1"
    assert "IV6900503-1" in doc_nos
    assert "IV6900501-1" in doc_nos


def test_parse_sales_vat_type_parsed(sample_sales_file):
    entries = parse_weekly.parse_sales(sample_sales_file)
    by_doc = {e['doc_no']: e for e in entries}
    # IV6900503-1 has vat_type=1, IV6900501-1 has vat_type=2 in the sample
    assert by_doc["IV6900503-1"]['vat_type'] == 1
    assert by_doc["IV6900501-1"]['vat_type'] == 2


def test_parse_sales_carries_party_and_product_context(sample_sales_file):
    entries = parse_weekly.parse_sales(sample_sales_file)
    by_doc = {e['doc_no']: e for e in entries}

    e1 = by_doc["IV6900503-1"]
    assert e1['party_code'] == "01พ02"
    assert e1['product_code_raw'] == "031บ4120"

    e2 = by_doc["IV6900501-1"]
    assert e2['party_code'] == "01อ35"
    assert e2['product_code_raw'] == "001ก3435"


def test_parse_sales_decimal_baht_discount(sample_sales_file):
    """BSN sometimes emits line discount as decimal baht (e.g. '32.00') instead of percent.
    Old regex misaligned columns: total absorbed the discount, leaving the real total in
    the ignored column. This test guards against that regression."""
    entries = parse_weekly.parse_sales(sample_sales_file)
    by_doc = {e['doc_no']: e for e in entries}

    e = by_doc["IV6900498-2"]
    assert e['unit_price'] == 50.00
    assert e['discount']   == "32.00"
    assert e['total']      == 18.00
    assert e['net']        == 18.00


def test_parse_sales_doc_level_discount_percent(sample_sales_file):
    """Doc-level discount column ('ส่วนลดรวม') uses percent format like '2%'.
    Old regex captured only '2' (digit before %) as net, dropping the real net entirely.
    For IV6900370-2 the real net is 1728.72 (= 1764 × 0.98), not 2."""
    entries = parse_weekly.parse_sales(sample_sales_file)
    by_doc = {e['doc_no']: e for e in entries}

    e = by_doc["IV6900370-2"]
    assert e['discount'] == "10%"
    assert e['total']    == 1764.00
    assert e['net']      == 1728.72


def test_parse_sales_qty_unit_bang_separator(sample_sales_file):
    """BSN occasionally glues qty and unit with '!' instead of whitespace (e.g. '2.00!หล').
    Old regex used \\s+ between qty and unit groups, so the whole row failed to match and
    was silently dropped. ~137 such rows existed in the 2024-2026 sales export. The fix
    extends the qty/unit separator to allow '!' and strips it from captured groups."""
    entries = parse_weekly.parse_sales(sample_sales_file)
    by_doc = {e['doc_no']: e for e in entries}

    e = by_doc["IV6801044-4"]
    assert e['qty']        == 2.00
    assert e['unit']       == "หล"
    assert e['unit_price'] == 1317.79
    assert e['vat_type']   == 2
    assert e['discount']   == "10%"
    assert e['total']      == 2372.02
    assert e['net']        == 2372.02


# ── #525: fixed-width money columns (ส่วนลด/รวมเงิน/ส่วนลดรวม/ยอดขายสุทธิ) ──
#
# The four money columns after the VAT-type digit used to be extracted by an
# unbounded, unanchored regex character class. When ส่วนลด (line discount)
# was genuinely blank and ส่วนลดรวม (the doc-level discount, reprinted on
# every line of the bill) held a BAHT amount, the regex could not tell
# "nothing here" from "a number starts here" and grabbed รวมเงิน (the true
# line total) into `discount`, then the small doc-level discount into
# `total`. `net` was always correct — only `discount` and `total` shifted.
# 176 lines in production were mis-parsed this way, 0 keyed that way in
# Express (GH #525). The fix reads the four columns at their FIXED CHARACTER
# POSITIONS relative to the VAT-type digit instead of searching for the next
# available number — a blank column is then unambiguous by construction.
#
# These build their OWN tiny sales file rather than extending the shared
# SALES_SAMPLE_LINES/sample_sales_file fixture, which several other test
# files (test_bsn_weekly_import_hardening.py etc.) hard-code an exact row
# count against — appending rows there breaks tests unrelated to #525.

def _write_sales_csv(tmp_path, name, party_product_txn_lines):
    """party_product_txn_lines: one (party_line, product_line, txn_line)
    triple per case, in real-report format (already quote-wrapped)."""
    lines = [
        '"(BSN)บจก.บุญสวัสดิ์นำชัย                                                                                             หน้า   :        1"',
        '"  รายงานประวัติการขาย\xa0แยกตามลูกค้า"',
        '"รหัสลูกค้า                       ถึง  Zหน้าร้าน                                                                      วันที่ : 15/04/69"',
        '"วันที่จาก   12\xa0เม.ย.\xa02569         ถึง  31\xa0ธ.ค.\xa02569"',
    ]
    for party, product, txn in party_product_txn_lines:
        lines += [party, product, txn]
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="cp874")
    return str(p)


def test_parse_sales_blank_discount_baht_doc_discount_no_comma(tmp_path):
    """Case 1 — the real corruption shape (IV6701161-1), value NOT
    comma-grouped. Before the anchor fix this returned
    discount='3480.00' total=170.00 (shifted). Must return blank discount,
    total=3480.00 (รวมเงิน), net=3446.26 (ยอดขายสุทธิ, always correct)."""
    p = _write_sales_csv(tmp_path, "ขาย_525case1.csv", [(
        '"  ทดสอบ525เคส1\xa0/99ท525001"',
        '"   ลูกบิด\xa0#130\xa0AC\xa0\'S/D\'\xa0/031บ4525"',
        '"      09/05/67   IV6900910-  1        24.00 ผง          145.00  1                  3480.00     170.00       3446.26"',
    )])
    entries = parse_weekly.parse_sales(p)
    assert len(entries) == 1
    e = entries[0]
    assert e['discount'] == ""
    assert e['total']    == 3480.00
    assert e['net']      == 3446.26


def test_parse_sales_blank_discount_baht_doc_discount_comma_grouped(tmp_path):
    """Case 2 — same shape as case 1, but the stolen รวมเงิน value is
    comma-grouped ('3,480.00'). Proves the fix is column-position based,
    not a character-class tweak: commit b998736 (2026-05-31, adding ','
    to the class to fix a DIFFERENT bug) made this exact shape WORSE, not
    better, because it let the greedy discount slot consume the comma too."""
    p = _write_sales_csv(tmp_path, "ขาย_525case2.csv", [(
        '"  ทดสอบ525เคส2\xa0/99ท525002"',
        '"   ลูกบิด\xa0#130\xa0AC\xa0\'S/D\'\xa0/031บ4526"',
        '"      09/05/67   IV6900911-  1        24.00 ผง          145.00  1                 3,480.00     170.00       3446.26"',
    )])
    entries = parse_weekly.parse_sales(p)
    assert len(entries) == 1
    e = entries[0]
    assert e['discount'] == ""
    assert e['total']    == 3480.00
    assert e['net']      == 3446.26


def test_parse_sales_blank_discount_percent_doc_discount_control(tmp_path):
    """Case 3 (control) — blank line discount + a doc-level discount printed
    as a PERCENT, not baht. Must stay correct: a percent sign is unambiguous
    on its own, and this shape never triggered the bug."""
    p = _write_sales_csv(tmp_path, "ขาย_525case3.csv", [(
        '"  ทดสอบ525เคส3\xa0/99ท525003"',
        '"   ทดสอบสินค้า525เคส3\xa0/99ท525013"',
        '"      04/03/69   IV6900912-  1         1.00 ลง         1960.00  1                  1960.00         3%       1901.20"',
    )])
    entries = parse_weekly.parse_sales(p)
    assert len(entries) == 1
    e = entries[0]
    assert e['discount'] == ""
    assert e['total']    == 1960.00
    assert e['net']      == 1901.20


def test_parse_sales_both_discounts_populated_control(tmp_path):
    """Case 4 (control) — both discount columns genuinely populated with
    baht amounts (all four numbers present on the line). Must stay correct
    before and after the fix."""
    p = _write_sales_csv(tmp_path, "ขาย_525case4.csv", [(
        '"  ทดสอบ525เคส4\xa0/99ท525004"',
        '"   ทดสอบสินค้า525เคส4\xa0/99ท525014"',
        '"      10/04/69   IV6900913-  1         1.00 อน          165.00  1      38.00        127.00      10.00        117.00"',
    )])
    entries = parse_weekly.parse_sales(p)
    assert len(entries) == 1
    e = entries[0]
    assert e['discount'] == "38.00"
    assert e['total']    == 127.00
    assert e['net']      == 117.00


def test_purchase_net_with_comma_doc_discount():
    """Regression (RR6700192): when the doc-level discount column carries a
    comma-thousands value (e.g. '1,800.00'), the pre-b998736 _DISCOUNT_COL
    class `[\\d+%.]*` could not match the comma, so `net` (the last field)
    grabbed the doc-discount column instead of the true final column. Real
    net is 4358.93, NOT 1,800.00. Now driven through the public parse_purchases()
    API (the fixed-width columns are no longer regex capture groups)."""
    from parse_weekly import parse_purchases, _clean
    lines = [
        '"(BSN)บจก.บุญสวัสดิ์นำชัย                                                                                            หน้า   :        1"',
        '"  รายงานประวัติการซื้อ\xa0แยกตามผู้จำหน่าย"',
        '"รหัสผู้จำหน่ายจาก                       ถึง  ไพ                                                                     วันที่ : 24/04/69"',
        '"วันที่จาก          23\xa0เม.ย.\xa02569        ถึง  31\xa0ธ.ค.\xa02569"',
        '"  ทดสอบ525comma\xa0/99ค525"',
        '"   ทดสอบสินค้าcomma\xa0/99ค525001"',
        '"        16/05/67   RR6700192        1000.00 มน            9.50  1      50+5%       4512.50   1,800.00       4358.93"',
    ]
    import tempfile, os
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "ซื้อ_sample_comma.csv")
        with open(p, "w", encoding="cp874") as f:
            f.write("\n".join(lines) + "\n")
        entries = parse_purchases(p)
    assert len(entries) == 1
    e = entries[0]
    assert e['total'] == 4512.50
    assert e['net']   == 4358.93


def test_purchase_net_with_plain_doc_discount_still_ok():
    """Control: a no-comma doc-discount middle column (RR6700256) already
    parsed correctly and must keep doing so."""
    from parse_weekly import parse_purchases
    lines = [
        '"(BSN)บจก.บุญสวัสดิ์นำชัย                                                                                            หน้า   :        1"',
        '"  รายงานประวัติการซื้อ\xa0แยกตามผู้จำหน่าย"',
        '"รหัสผู้จำหน่ายจาก                       ถึง  ไพ                                                                     วันที่ : 24/04/69"',
        '"วันที่จาก          23\xa0เม.ย.\xa02569        ถึง  31\xa0ธ.ค.\xa02569"',
        '"  ทดสอบ525plain\xa0/99พ525"',
        '"   ทดสอบสินค้าplain\xa0/99พ525001"',
        '"        21/06/67   RR6700256         300.00 มน           14.00  1      50+5%       1995.00     540.00       1917.86"',
    ]
    import tempfile, os
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "ซื้อ_sample_plain.csv")
        with open(p, "w", encoding="cp874") as f:
            f.write("\n".join(lines) + "\n")
        entries = parse_purchases(p)
    assert len(entries) == 1
    e = entries[0]
    assert e['total'] == 1995.00
    assert e['net']   == 1917.86


# ── detect_file_type ─────────────────────────────────────────────────────────

def test_detect_file_type_purchase(sample_purchase_file):
    assert parse_weekly.detect_file_type(sample_purchase_file) == "purchase"


def test_detect_file_type_sales(sample_sales_file):
    assert parse_weekly.detect_file_type(sample_sales_file) == "sales"


# ── VAT-entity gate ───────────────────────────────────────────────────────────

def test_parse_sales_rejects_vat_entity_file(tmp_path):
    """Files from the VAT entity (บริษัท บุญสวัสดิ์ นำชัย จำกัด) must be
    rejected with a clear ValueError. Root cause: 2026-06-22 incident where
    the VAT company export was accidentally imported, adding IV26* doc_nos."""
    import pytest
    vat_lines = [
        '"บริษัท บุญสวัสดิ์ นำชัย จำกัด                                       หน้า   :        1"',
        '"  รายงานประวัติการขาย แยกตามลูกค้า"',
        '"      22/06/69   IV2600172-  1        25.00 ผน            5.61  2                   140.25                   140.25"',
    ]
    p = tmp_path / "ประวัติการขาย_22.6.69.csv"
    p.write_text("\n".join(vat_lines) + "\n", encoding="cp874")
    with pytest.raises(ValueError, match="VAT entity"):
        parse_weekly.parse_sales(str(p))
