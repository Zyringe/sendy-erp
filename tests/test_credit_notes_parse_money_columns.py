"""
GH #556 — `_parse_detail_line` assigned the SR money columns by token position.

The detail row's money region is `[unit_price] [discount] [amount] [ref]`, printed
fixed-width with blanks where Express holds 0.00. The old code split the tail on
runs of 2+ spaces and assigned the remaining tokens positionally, so any token
count other than the one it expected silently moved a column. Three real shapes
broke, all of them collapsing `amount` — which feeds BOTH `total` and `net` — to 0.00:

  C1  a two-digit reference LINE number.  Express right-aligns it in 3 columns
      after the '-', so "IV6602766-  1" (two spaces, splits into two tokens) becomes
      "IV6601858- 12" (ONE space, stays a single token). The old allowlist regex
      `^(IV\\S*\\-?|AVGPR\\-?)\\S*$` cannot match a token with a space inside it, so the
      reference stayed in the money tokens as a third numeric -> `discount` was
      overwritten with the line's own value and `amount` took the unparseable
      reference, i.e. 0.00.

  C2  a reference prefix outside the IV/AVGPR allowlist (`STNPR`). Same outcome.

  C3  a blank unit_price column. One lone money token was assigned to `unit_price`
      when it is in fact the line total (รวมเงิน).

Every fixture line below is rendered from a REAL Express row read out of
`projects/express-integration/data/BSN5657` (STCRD/ARTRN snapshot 2026-08-25);
`RDOCNUM` supplies the reference column. Ten prod rows were damaged by these three
shapes — reproducing them here reproduces the prod values exactly.

The lone-money-token rule (C3) is not a guess: across all 5,621 SR lines in that
Express book, a row with a printed unit_price but no line total occurs **0** times,
while a row with a line total but no unit_price occurs 15 times. A lone money
column is therefore always รวมเงิน.
"""
import pytest

import parse_weekly


# ── Fixture: real Express rows rendered back into the report's layout ─────────
#
# Layout (after parse_weekly._clean strips quotes and maps \xa0 -> ' '):
#   marker  seq  bsn_code  <2+ sp>  product_name  <2+ sp>  qty+unit(GLUED)
#   <pad> unit_price  <pad> discount  <pad> amount  <pad> reference
#
# Columns are right-aligned, so a blank Express value prints as whitespace and
# simply disappears from the split — which is the whole defect.

# -- damaged shapes ----------------------------------------------------------
LINE_C1_TWO_DIGIT_REF = (
    "Y   1 045ล9996  ชุดมือจับประตูใหญ่ HL#9996 AC'S/D        2.00ชด"
    "             850.00        10%       1530.00"
    "                                IV6601858- 12"
)
LINE_C1_TWO_DIGIT_REF_B = (
    "Y   1 900ป7214  แปรงทาสี EAGLE-33 ขนาด 2นิ้ว       10.00หล"
    "             190.00        20%       1520.00"
    "                                IV6701819- 15"
)
LINE_C2_UNKNOWN_REF_PREFIX = (
    "Y   6 045ก2341  กุญแจบานเลื่อน SD 'SL-234' AC     2.00ชด"
    "             650.00        50%        650.00"
    "                                STNPR"
)
LINE_C3_BLANK_UNIT_PRICE_PERCENT_DISC = (
    "N   1 902ค3240  คีมผูกลวดจีน 8\"(คละสี)            หล"
    "                        10+10%         86.00"
    "                                IV6703468-  5"
)
LINE_C3_BLANK_UNIT_PRICE_NO_DISC = (
    "N   1 930บ6330  บานพับหน้าต่าง(ใบโพธิ์ทอง)8\"(เงิน)           ชด"
    "                                     5.35"
    "                                IV6701099-  1"
)

# -- controls: shapes that already parsed correctly and must not move ---------
CONTROL_PERCENT_DISCOUNT = (
    "Y   1 026ต2710-3  รีเวทแผง White 4-6 SENDAI      1.00ผง"
    "             100.00        25%         75.00"
    "                                IV4691912-  9"
)
CONTROL_BAHT_DISCOUNT = (
    "Y   1 045ก6005  กุญแจประตูเหล็ก #HL316'CR'      1.00ชด"
    "             499.00      41.00        458.00"
    "                                IV6400123-  4"
)
CONTROL_NO_DISCOUNT = (
    "N   1 532ร1039  ระดับน้ำมีแม่เหล็ก9\" 'META'         6.00อน"
    "              58.67                   352.02"
    "                                IV6701251-  6"
)
CONTROL_AVGPR_REF = (
    "Y   2 045ล9990-4  ชุดมือจับประตูใหญ่ SD#9990-4 AC'S/D    1.00ชด"
    "            1150.00      5+20%        874.00"
    "                                AVGPR"
)
CONTROL_NO_MONEY_COLUMNS = (
    "Y   1 041ม5560  มือจับ(P)#555-350มิล.AC 'S/D'       2.00ผง"
    "                                                                 "
    "           IV6602028-  3"
)


# ── C1 / C2: the reference column must never be read as money ────────────────

@pytest.mark.parametrize("line, unit_price, discount, amount, ref", [
    (LINE_C1_TWO_DIGIT_REF,   850.00, '10%', 1530.00, 'IV6601858-12'),
    (LINE_C1_TWO_DIGIT_REF_B, 190.00, '20%', 1520.00, 'IV6701819-15'),
])
def test_two_digit_reference_line_does_not_steal_the_money_columns(
        line, unit_price, discount, amount, ref):
    """C1: "IV…- 12" is one token (single space). It must be the reference, not a
    third numeric that overwrites `discount` and zeroes `amount`."""
    d = parse_weekly._parse_detail_line(line)
    assert d is not None, "head regex did not match the line at all"
    assert d['unit_price'] == pytest.approx(unit_price)
    assert d['discount'] == discount
    assert d['amount'] == pytest.approx(amount)
    assert d['ref_line'] == ref


def test_unrecognised_reference_prefix_does_not_steal_the_money_columns():
    """C2: a reference outside the old IV/AVGPR allowlist (here Express's STNPR)
    is still the reference column."""
    d = parse_weekly._parse_detail_line(LINE_C2_UNKNOWN_REF_PREFIX)
    assert d is not None
    assert d['unit_price'] == pytest.approx(650.00)
    assert d['discount'] == '50%'
    assert d['amount'] == pytest.approx(650.00)
    assert d['ref_line'] == 'STNPR'


# ── C3: a lone money column is the line total, not the unit price ────────────

def test_lone_money_column_is_the_line_total_not_the_unit_price():
    """C3: blank unit_price + percent discount. The single numeric is รวมเงิน."""
    d = parse_weekly._parse_detail_line(LINE_C3_BLANK_UNIT_PRICE_PERCENT_DISC)
    assert d is not None
    assert d['unit_price'] == pytest.approx(0.00)
    assert d['discount'] == '10+10%'
    assert d['amount'] == pytest.approx(86.00)


def test_lone_money_column_with_no_discount_is_the_line_total():
    """C3 again with the discount column blank too — one token in the whole tail."""
    d = parse_weekly._parse_detail_line(LINE_C3_BLANK_UNIT_PRICE_NO_DISC)
    assert d is not None
    assert d['unit_price'] == pytest.approx(0.00)
    assert d['discount'] == ''
    assert d['amount'] == pytest.approx(5.35)


# ── CONTROLS: the shapes that already worked must parse EXACTLY as before ────

def test_controls_percent_baht_and_absent_discounts_are_unchanged():
    """Each discount kind the column accepts — percent, baht, blank — plus the
    AVGPR reference and a row with no money columns at all.

    This is the anti-regression half of the fix: it is what would go red if the
    reference-stripping or the numeric assignment were widened too far. The COUNT
    is asserted before any property so an empty/short table cannot pass vacuously.
    """
    cases = [
        # label,              line,                      unit_price, discount, amount,  ref
        ('percent',  CONTROL_PERCENT_DISCOUNT,            100.00,  '25%',   75.00,  'IV4691912-9'),
        ('baht',     CONTROL_BAHT_DISCOUNT,               499.00,  '41.00', 458.00, 'IV6400123-4'),
        ('blank',    CONTROL_NO_DISCOUNT,                  58.67,  '',      352.02, 'IV6701251-6'),
        ('avgpr',    CONTROL_AVGPR_REF,                  1150.00,  '5+20%', 874.00, 'AVGPR'),
        ('no money', CONTROL_NO_MONEY_COLUMNS,              0.00,  '',        0.00, 'IV6602028-3'),
    ]
    assert len(cases) == 5, "control table shrank — a control is missing"

    parsed = [(label, parse_weekly._parse_detail_line(line), exp)
              for label, line, *exp in cases]
    assert len(parsed) == 5
    assert all(d is not None for _, d, _ in parsed), \
        "a control line failed the head regex: " + \
        repr([l for l, d, _ in parsed if d is None])

    for label, d, (unit_price, discount, amount, ref) in parsed:
        assert d['unit_price'] == pytest.approx(unit_price), f"{label}: unit_price"
        assert d['discount'] == discount, f"{label}: discount"
        assert d['amount'] == pytest.approx(amount), f"{label}: amount"
        assert d['ref_line'] == ref, f"{label}: ref_line"


def test_controls_keep_the_non_money_fields():
    """The head of the row (seq / code / name / qty / unit) is untouched by this
    fix — pinned so a change to the tail handling cannot silently move it."""
    d = parse_weekly._parse_detail_line(CONTROL_PERCENT_DISCOUNT)
    assert d['seq'] == 1
    assert d['bsn_code'] == '026ต2710-3'
    assert d['product_name'] == 'รีเวทแผง White 4-6 SENDAI'
    assert d['qty'] == pytest.approx(1.00)
    assert d['unit'] == 'ผง'


# ── End-to-end: the values that actually reach sales_transactions ────────────

CREDIT_NOTE_FILE_LINES = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                     หน้า   :        1"',
    '"  รายงานใบลดหนี้/รับคืนสินค้า\xa0เรียงตามเลขที่"',
    '"  SR6700040    25/06/67  ร้านค้าตัวอย่าง                      02         IV6601858    1                  1530.00         0.00       1530.00        N      2"',
    '"     ' + LINE_C1_TWO_DIGIT_REF + '"',
    '"  SR6700125    19/03/68  ร้านค้าตัวอย่าง                      06         IV6703468    1                    86.00         0.00         86.00        N      2"',
    '"     ' + LINE_C3_BLANK_UNIT_PRICE_PERCENT_DISC + '"',
    '"  SR6700060    29/08/67  ร้านค้าตัวอย่าง                      06         IV4691912    1                    75.00         0.00         75.00        N      2"',
    '"     ' + CONTROL_PERCENT_DISCOUNT + '"',
]


@pytest.fixture
def credit_note_file(tmp_path):
    p = tmp_path / "ใบลดหนี้_556.csv"
    p.write_text("\n".join(CREDIT_NOTE_FILE_LINES) + "\n", encoding="cp874")
    return str(p)


def test_total_and_net_reach_the_entry_not_zero(credit_note_file):
    """`amount` feeds BOTH `total` and `net` in _make_entry. The prod damage was
    net = 0.00 on all ten rows, so assert the end-to-end values, not just the
    intermediate dict."""
    entries = parse_weekly.parse_credit_notes(credit_note_file)
    by_doc = {e['doc_no']: e for e in entries}
    assert len(entries) == 3, f"expected 3 detail entries, got {sorted(by_doc)}"

    damaged_c1 = by_doc['SR6700040-1']
    assert damaged_c1['discount'] == '10%'
    assert damaged_c1['total'] == pytest.approx(1530.00)
    assert damaged_c1['net'] == pytest.approx(1530.00)

    damaged_c3 = by_doc['SR6700125-1']
    assert damaged_c3['unit_price'] == pytest.approx(0.00)
    assert damaged_c3['total'] == pytest.approx(86.00)
    assert damaged_c3['net'] == pytest.approx(86.00)

    control = by_doc['SR6700060-1']          # CONTROL in the same test
    assert control['discount'] == '25%'
    assert control['unit_price'] == pytest.approx(100.00)
    assert control['total'] == pytest.approx(75.00)
    assert control['net'] == pytest.approx(75.00)


# ── The clobber guard itself ─────────────────────────────────────────────────

# SYNTHETIC, not an observed Express row: a money-shaped extra column trailing the
# money region, which the reference-strip (correctly) will not remove because it
# cannot be told apart from a money value. This is the residual shape of the #556
# damage — a stray numeric reaching the `len(numerics) >= 3` branch — and it is the
# only thing that exercises that branch's `not discount` guard. Without a case like
# this the guard is untestable, and an untested guard is a checkmark, not a test.
LINE_PERCENT_PLUS_STRAY_MONEY_COLUMN = (
    "Y   1 045ล9996  ชุดมือจับประตูใหญ่ HL#9996 AC'S/D        2.00ชด"
    "             850.00        10%       1530.00         0.00"
)


def test_percent_discount_is_never_overwritten_by_a_stray_numeric():
    """A '%' read from the discount column wins over the three-numeric baht rule.

    The #556 damage was precisely `discount` being replaced by the line's own
    value, so this pins the guard that prevents it rather than trusting that the
    reference-strip alone will always keep the token count at three.
    """
    d = parse_weekly._parse_detail_line(LINE_PERCENT_PLUS_STRAY_MONEY_COLUMN)
    assert d is not None
    assert d['discount'] == '10%', "a percent discount was clobbered by a numeric"
    assert d['unit_price'] == pytest.approx(850.00)


def test_three_numerics_without_a_percent_still_read_the_baht_discount():
    """CONTROL for the guard above: with no '%' present the middle numeric IS the
    baht discount, so the guard must not have disabled that path."""
    d = parse_weekly._parse_detail_line(CONTROL_BAHT_DISCOUNT)
    assert d is not None
    assert d['discount'] == '41.00'
    assert d['unit_price'] == pytest.approx(499.00)
    assert d['amount'] == pytest.approx(458.00)
