"""Smoke tests for parse_express_purchase_history.py.

Covers:
1. cp874 decoding + supplier/product context attach
2. BE-date → ISO conversion
3. Return row sign / return_flag propagation
4. Footer reconciliation (mismatch must raise ValueError)
5. line_seq counter for duplicate (doc_no, bsn_code) pairs
"""
import sys
import pytest
from pathlib import Path

# scripts/ is a sibling of tests/ — ensure it is importable
_SCRIPTS = Path(__file__).resolve().parent.parent / 'scripts'
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import parse_express_purchase_history as p


# ── fixture helpers ───────────────────────────────────────────────────────────

# Minimal but realistic cp874 purchase-history file content.
# Replicates the real column layout:
#   col 8-16   date DD/MM/YY
#   col 19-27  doc_no (HP/RR/GR)
#   col ~38-43 qty
#   col ~44-46 unit
#   col ~47    return_flag (Y/N, GR rows only)
#   col ~53-62 unit_price
#   col 63     vat_type
#   col ~70-76 discount (optional)
#   col ~83-90 total
#   col ~100-101 total_discount (optional)
#   col ~107-115 net
#
# Values chosen to allow easy arithmetic verification.
_SAMPLE_LINES = [
    # Page header (must be skipped)
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                                                                            หน้า   :     1"',
    '"  รายงานประวัติการซื้อ\xa0แยกตามผู้จำหน่าย"',
    '"รหัสผู้จำหน่ายจาก                       ถึง  ไพ                                                                     วันที่"',
    '"วันที่จาก          1\xa0ม.ค.\xa02567          ถึง  31\xa0ธ.ค.\xa02569"',
    '"-----------------------------------------------------------------------------------------------------------------------"',
    '"   สินค้า  วันที่  เลขที่เอกสาร       จำนวน   คืน  ราคาต่อหน่วย\xa0VAT\xa0\xa0 ส่วนลด       รวมเงิน  ส่วนลดรวม     ยอดซื้อสุทธิ อ้างถึง"',
    '"-----------------------------------------------------------------------------------------------------------------------"',
    # Supplier header (2-space indent)
    '"  กนกชัยโลหะกิจ\xa0/กนก"',
    # Product header (3-space indent)
    '"   ลูกบิดรมดำ#5794\xa0/442ล5798"',
    # Normal HP purchase row: qty=120, unit=ผง, price=90, vat=1, total=10800, net=10800
    '"        19/05/68   HP6800229         120.00 ผง           90.00  1                 10800.00                 10800.00"',
    # Normal RR credit-purchase row: qty=120, unit=ผง, price=90, vat=0, total=10800, net=10800
    '"        19/08/68   RR6800370         120.00 ผง           90.00  0                 10800.00                 10800.00"',
    # Product header (3-space indent, second product)
    '"   ตลับเมตร\xa05\xa0เมตร\xa0/561ต1060"',
    # GR return row (return_flag=Y): qty=25, unit=ลก, price=25, vat=1, total=625, net=625
    '"        12/02/67   GR6600017          25.00 ลก Y         25.00  1                   625.00                   625.00 RR6600386-  1"',
    # GR price-adjustment row (return_flag=N): qty=0, price=60, total=156, net=156
    '"        02/04/67   GR6700005           0.00 มน N         60.00  1        35%        156.00                   156.00 RR6700127-  2"',
    # Subtotals (must be skipped)
    '"          รวมตาม ซื้อเชื่อ             120.00 ผง                                    10800.00                 10800.00"',
    '"          รวมตาม ใบลดหนี้              -25.00 ลก                                     -625.00                  -625.00"',
    # Supplier total (must be skipped)
    '"   รวม กนก                            4031.00 อน                                    54470.00                 54470.00"',
    # Grand total footer
    # Correct grand total = HP + RR - GR(Y) - GR(N)
    #   = 10800 + 10800 - 625 - 156 = 20819.00
    '"                                    รวมทั้งสิ้น                                    20819.00                 20819.00"',
    '">>>> จบรายงาน <<<<"',
]


@pytest.fixture
def sample_file(tmp_path):
    p_ = tmp_path / 'purchase_history_sample.csv'
    p_.write_text('\n'.join(_SAMPLE_LINES) + '\n', encoding='cp874')
    return str(p_)


@pytest.fixture
def sample_records(sample_file):
    return list(p.parse_purchase_history(sample_file))


# ── 1. cp874 decoding + context attach ───────────────────────────────────────

def test_cp874_decoding(sample_file):
    """Thai characters decode correctly from cp874."""
    with open(sample_file, encoding='cp874') as f:
        text = f.read()
    assert 'รายงานประวัติการซื้อ' in text
    assert 'กนกชัยโลหะกิจ' in text


def test_supplier_context_attached(sample_records):
    """Every parsed row carries the supplier name and code from the header above it."""
    for r in sample_records:
        assert r.supplier_code == 'กนก'
        assert 'กนกชัยโลหะกิจ' in r.supplier_name


def test_product_context_attached(sample_records):
    """Product name and code from the nearest product header are attached."""
    hp_row = next(r for r in sample_records if r.doc_no == 'HP6800229')
    assert hp_row.product_code == '442ล5798'
    assert 'ลูกบิดรมดำ' in hp_row.product_name


def test_product_context_switches_on_new_header(sample_records):
    """GR rows under the second product header carry that product's code."""
    gr_row = next(r for r in sample_records if r.doc_no == 'GR6600017')
    assert gr_row.product_code == '561ต1060'


# ── 2. BE-date conversion ─────────────────────────────────────────────────────

def test_be_date_2568(sample_records):
    """19/05/68 (พ.ศ. 2568) → 2025-05-19."""
    hp = next(r for r in sample_records if r.doc_no == 'HP6800229')
    assert hp.date_iso == '2025-05-19'


def test_be_date_2567(sample_records):
    """12/02/67 (พ.ศ. 2567) → 2024-02-12."""
    gr = next(r for r in sample_records if r.doc_no == 'GR6600017')
    assert gr.date_iso == '2024-02-12'


def test_thai_date_to_iso_direct():
    assert p.thai_date_to_iso('01/01/67') == '2024-01-01'
    assert p.thai_date_to_iso('31/12/69') == '2026-12-31'


# ── 3. Return row flag and sign ───────────────────────────────────────────────

def test_return_flag_y_on_gr_row(sample_records):
    """GR rows with Y in the คืน column have return_flag='Y'."""
    gr = next(r for r in sample_records if r.doc_no == 'GR6600017')
    assert gr.return_flag == 'Y'
    assert gr.qty == 25.0
    assert gr.total == 625.0


def test_return_flag_n_on_price_adjustment(sample_records):
    """GR rows with N in the คืน column have return_flag='N' (price adjustment)."""
    gr = next(r for r in sample_records if r.doc_no == 'GR6700005')
    assert gr.return_flag == 'N'
    assert gr.qty == 0.0
    assert gr.total == 156.0


def test_normal_hp_row_has_no_return_flag(sample_records):
    """HP purchase rows have return_flag=''."""
    hp = next(r for r in sample_records if r.doc_no == 'HP6800229')
    assert hp.return_flag == ''


def test_doc_no_prefix_set_correctly(sample_records):
    """All three doc prefixes (HP, RR, GR) appear in parsed records."""
    prefixes = {r.doc_no[:2] for r in sample_records}
    assert 'HP' in prefixes
    assert 'RR' in prefixes
    assert 'GR' in prefixes


# ── 4. Footer reconciliation ─────────────────────────────────────────────────

def test_validate_passes_when_balanced(sample_file, sample_records):
    """validate() completes without raising when Σ matches the footer.

    Grand total in fixture = HP(10800) + RR(10800) - GR_Y(625) - GR_N(156)
    = 20819.00, matching the footer row in _SAMPLE_LINES.
    """
    p.validate(sample_records, sample_file)  # must not raise


def test_validate_raises_on_mismatch(tmp_path, sample_records):
    """validate() raises ValueError when the footer total disagrees with parsed sum."""
    # Write a file whose footer is deliberately wrong (1.00 instead of correct value)
    wrong_lines = _SAMPLE_LINES[:-2] + [
        '"                                    รวมทั้งสิ้น                                        1.00                     1.00"',
        '">>>> จบรายงาน <<<<"',
    ]
    bad_file = tmp_path / 'bad.csv'
    bad_file.write_text('\n'.join(wrong_lines) + '\n', encoding='cp874')

    with pytest.raises(ValueError, match='Footer reconciliation FAIL'):
        p.validate(sample_records, str(bad_file))


def test_validate_skips_when_no_footer(tmp_path, sample_records):
    """validate() is a no-op when the file has no รวมทั้งสิ้น line."""
    no_footer = [l for l in _SAMPLE_LINES if 'รวมทั้งสิ้น' not in l]
    f = tmp_path / 'no_footer.csv'
    f.write_text('\n'.join(no_footer) + '\n', encoding='cp874')
    p.validate(sample_records, str(f))  # must not raise


# ── 5. line_seq for duplicate (doc_no, bsn_code) ─────────────────────────────

_DUP_LINES = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                              หน้า   :     1"',
    '"  รายงานประวัติการซื้อ\xa0แยกตามผู้จำหน่าย"',
    '"วันที่จาก          1\xa0ม.ค.\xa02567          ถึง  31\xa0ธ.ค.\xa02569"',
    '"  ซัพพลายเออร์ A /SUPA"',
    '"   สินค้า ทดสอบ /528ช1800"',
    # Two lines for same doc_no + product_code (different price)
    '"        17/01/67   RR6700029          10.00 ชด          570.00  1      10+5%       4873.50                  4873.50"',
    '"        17/01/67   RR6700029           1.00 ชด            0.00  1                     0.00                     0.00"',
    # No footer needed for this fixture (validate will skip)
]


@pytest.fixture
def dup_file(tmp_path):
    f = tmp_path / 'dup_test.csv'
    f.write_text('\n'.join(_DUP_LINES) + '\n', encoding='cp874')
    return str(f)


def test_line_seq_increments_for_duplicate_pair(dup_file):
    """When the same (doc_no, bsn_code) appears twice, line_seq is 1 then 2."""
    records = list(p.parse_purchase_history(dup_file))
    assert len(records) == 2
    seqs = sorted(r.line_seq for r in records)
    assert seqs == [1, 2]


def test_line_seq_is_1_for_unique_rows(sample_records):
    """Rows with unique (doc_no, product_code) all get line_seq=1."""
    for r in sample_records:
        assert r.line_seq == 1
