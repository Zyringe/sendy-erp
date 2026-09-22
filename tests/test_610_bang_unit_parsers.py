"""The `!` Express glues between qty and unit is stripped by every parser.

Express prints `!` in its text reports to flag a line whose factor does not
fit the item's base unit ("2.00!หล"). It is not part of the unit. The weekly
CSV parser always stripped it (parse_weekly._QTY_UNIT_SEP); the two
credit-note parsers did not, and relied on the unit map's `!หล -> โหล` rows as
a safety net. Migration 193 removes those five rows (#595's approved list),
so both parsers strip the mark themselves now (#610 review).

Each test carries a CONTROL: the same line without `!` parses to the same unit
and quantity, so a parser that dropped the unit entirely cannot pass.
"""
import os
import sys

import parse_weekly

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts')
if _SCRIPTS not in sys.path:
    sys.path.append(_SCRIPTS)


def _sr_line(qty_unit):
    return ("Y   1 631ก9111  ไขควง                          " + qty_unit +
            "             100.00                    200.00"
            "                                IV6901000-  1")


def test_sr_credit_note_line_strips_the_bang():
    """parse_weekly.parse_credit_notes -> credit_note_imports (the AR side)."""
    glued = parse_weekly._parse_detail_line(_sr_line('2.00!หล'))
    plain = parse_weekly._parse_detail_line(_sr_line('2.00หล'))      # CONTROL
    assert plain['unit'] == 'หล' and plain['qty'] == 2.0
    assert (glued['unit'], glued['qty']) == (plain['unit'], plain['qty'])
    assert (glued['unit_price'], glued['amount']) == (100.0, 200.0)


_AP_HEADER = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย"',
    '"  รายงานใบลดหนี้-ส่งคืน"',
    '"  GR6900099    12/02/69  ผู้ขายทดสอบ                  RR6900386    1     0.00     0.00    200.00    Y      2"',
]


def _ap_file(tmp_path, qty_unit):
    p = tmp_path / f'gr_{len(qty_unit)}.csv'
    detail = ('"     Y   1 631ก9111  ไขควง                      ' + qty_unit +
              '   100.00          200.00"')
    p.write_text('\n'.join(_AP_HEADER + [detail]) + '\n', encoding='cp874')
    return p


def test_ap_credit_note_line_strips_the_bang(tmp_path):
    """scripts/parse_express_credit_notes.py -> express_credit_note_lines."""
    import parse_express_credit_notes as p_cn
    glued = list(p_cn.parse_credit_notes(_ap_file(tmp_path, '2.00!หล')))
    plain = list(p_cn.parse_credit_notes(_ap_file(tmp_path, '2.00หล')))   # CONTROL
    assert [(l.unit, l.qty) for r in plain for l in r.lines] == [('หล', 2.0)]
    assert [(l.unit, l.qty) for r in glued for l in r.lines] == [('หล', 2.0)]
