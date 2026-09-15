"""TDD for #527 — the invoice-line price formula (pure, no DB).

Given one sales_transactions line's OWN numbers (unit_price, qty, discount,
total, net, vat_type), decide whether the price shown on the customer page's
product card can be explained as "list -discount = paid", and if so, which
parts to render. See CONTEXT.md "Customer page" (ส่วนลด, ก่อนเปลี่ยนราคา) and
.claude/rules/quoting-and-pricing.md for the Express rounding rules this line
must NOT re-derive.

The reconciliation guard (both required, else -> None, "no formula"):
  1. unit_price*qty reduced by the KEYED discount, rounded to 2dp, is within
     +/-0.01 of `total`.
  2. `net` is not more than `total` (a negative bill discount, #525, must
     never render).
"Nothing to derive" (no line discount, no bill discount, not vat_type 2)
also returns None — printing "55.00 = 55.00" would be noise.

Worked examples are taken verbatim from the issue (measured on prod
2026-09-14, BM99 / pid 115 / IV6701495 unless noted).
"""
from invoice_formula import invoice_line_formula


def test_percent_discount_reconciles():
    # BM99, IV6701495: 55.00 -2% = 53.90 (qty=1 line for the per-unit case).
    f = invoice_line_formula(unit_price=55.00, qty=1, discount='2%',
                              total=53.90, net=53.90, vat_type=1)
    assert f == {
        'unit_price': 55.00,
        'discount_kind': 'percent',
        'discount_label': '2%',
        'bill_discount_pct': None,
        'vat': False,
    }


def test_compounding_percent_discount_is_never_back_computed():
    # 100.00 -15+5% = 80.75 (0.85 * 0.95 = 0.8075). Must render "15+5%" as
    # keyed, never a back-computed "-19.25%".
    f = invoice_line_formula(unit_price=100.00, qty=1, discount='15+5%',
                              total=80.75, net=80.75, vat_type=1)
    assert f['discount_kind'] == 'percent'
    assert f['discount_label'] == '15+5%'


def test_percent_discount_plus_bill_discount():
    # 55.00 -2% -ท้ายบิล 2% = 52.82
    f = invoice_line_formula(unit_price=55.00, qty=1, discount='2%',
                              total=53.90, net=52.82, vat_type=1)
    assert f['discount_kind'] == 'percent'
    assert f['discount_label'] == '2%'
    assert f['bill_discount_pct'] == 2.0
    assert f['vat'] is False


def test_split_vat_line_as_keyed_on_the_invoice():
    # 51.41 -5% +VAT = 52.26 (total = 51.41 * 0.95 = 48.8395)
    f = invoice_line_formula(unit_price=51.41, qty=1, discount='5%',
                              total=48.8395, net=48.8395, vat_type=2)
    assert f['discount_kind'] == 'percent'
    assert f['discount_label'] == '5%'
    assert f['bill_discount_pct'] is None
    assert f['vat'] is True


def test_baht_discount_is_whole_line_not_per_piece():
    # 55.00 -฿28 (ทั้งบรรทัด) = 54.53 — qty=2: total = 55*2 - 28 = 82
    f = invoice_line_formula(unit_price=55.00, qty=2, discount='28.00',
                              total=82.00, net=82.00, vat_type=1)
    assert f['discount_kind'] == 'baht'
    assert f['discount_label'] == '28'
    assert f['bill_discount_pct'] is None


def test_baht_discount_with_comma_grouping_parses():
    # A comma-grouped keyed amount ("1,200.00") must parse and reconcile.
    f = invoice_line_formula(unit_price=500.00, qty=5, discount='1,200.00',
                              total=1300.00, net=1300.00, vat_type=1)
    assert f['discount_kind'] == 'baht'
    assert f['discount_label'] == '1,200'


def test_blank_discount_with_nothing_else_to_derive_is_no_formula():
    # "nothing to derive" -> None, render the price alone.
    f = invoice_line_formula(unit_price=53.90, qty=1, discount=None,
                              total=53.90, net=53.90, vat_type=1)
    assert f is None


def test_blank_discount_string_is_also_no_discount():
    f = invoice_line_formula(unit_price=53.90, qty=1, discount='',
                              total=53.90, net=53.90, vat_type=1)
    assert f is None


def test_blank_discount_but_a_real_bill_discount_still_derives():
    # No line discount, but a doc-level cut still explains the gap.
    f = invoice_line_formula(unit_price=100.00, qty=1, discount=None,
                              total=100.00, net=98.00, vat_type=1)
    assert f['discount_kind'] is None
    assert f['bill_discount_pct'] == 2.0


def test_non_reconciling_line_is_no_formula():
    # The issue's own example: discount 3480.00, total 170, net 3,446.26 —
    # a keying error, never render a wrong derivation.
    f = invoice_line_formula(unit_price=290.00, qty=12, discount='3480.00',
                              total=170.00, net=3446.26, vat_type=1)
    assert f is None


def test_net_greater_than_total_is_no_formula():
    # A negative bill discount (#525) must never render, even when the line
    # discount itself reconciles perfectly.
    f = invoice_line_formula(unit_price=55.00, qty=1, discount='2%',
                              total=53.90, net=60.00, vat_type=1)
    assert f is None


def test_unparseable_discount_text_is_no_formula_not_a_crash():
    # Code-review finding: a keyed discount that is neither blank,
    # percent-suffixed, nor a clean baht number (an Express typo) must
    # degrade to "no formula", never raise.
    f = invoice_line_formula(unit_price=100, qty=1, discount='พิเศษ',
                              total=90, net=90, vat_type=0)
    assert f is None


def test_net_within_float_noise_of_total_is_not_a_negative_discount():
    # Guard 2 tolerance: net a hair above total from float noise must not
    # be mistaken for a negative bill discount.
    f = invoice_line_formula(unit_price=55.00, qty=1, discount='2%',
                              total=53.90, net=53.9005, vat_type=1)
    assert f is not None
    assert f['bill_discount_pct'] is None
