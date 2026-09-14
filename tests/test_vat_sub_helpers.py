"""vat-substitute — pure-function helpers: category-noun extraction/matching
(plan §5 guess section), xp5 name size/color parsing + ordering (§4.4). No DB —
these are unit tests only. The badge formula (§5, decisions 9/10) is JS only
since #485; tests/test_vat_sub_render.py executes it."""
import models.vat_sub as vs


# ── extract_category_noun / nouns_compatible (§5 guess bridge) ─────────────

def test_extract_category_noun_stops_at_digit_and_hash():
    assert vs.extract_category_noun('กลอนเหล็ก#511-4นิ้ว AC') == 'กลอนเหล็ก'


def test_extract_category_noun_empty_when_name_starts_with_digit_or_hash():
    assert vs.extract_category_noun('4 นิ้ว บานพับ') == ''
    assert vs.extract_category_noun('#511 กลอน') == ''


def test_extract_category_noun_strips_sd_suffix():
    assert vs.extract_category_noun('แผ่นตัด 14 นิ้ว S/D') == 'แผ่นตัด '.strip()


def test_nouns_compatible_prefix_either_direction():
    assert vs.nouns_compatible('กลอน', 'กลอนเหล็ก') is True
    assert vs.nouns_compatible('กลอนเหล็ก', 'กลอน') is True


def test_nouns_compatible_requires_min_3_chars():
    assert vs.nouns_compatible('กล', 'กลอนเหล็ก') is False
    assert vs.nouns_compatible('', 'กลอนเหล็ก') is False


def test_nouns_compatible_no_common_prefix():
    assert vs.nouns_compatible('บานพับ', 'กลอนเหล็ก') is False


# ── parse_xp5_size_color (§4.4) ─────────────────────────────────────────────

def test_parse_xp5_size_color_finds_size_and_color():
    codes = {'AC', 'SS', 'CR'}
    size, color = vs.parse_xp5_size_color('กลอนเหล็ก#511-4 นิ้ว AC', codes)
    assert size == '4'
    assert color == 'AC'


def test_parse_xp5_size_color_fraction_size():
    codes = {'AC'}
    size, color = vs.parse_xp5_size_color('บานพับ 1/2 นิ้ว AC', codes)
    assert size == '1/2'
    assert color == 'AC'


def test_parse_xp5_size_color_nullable_on_no_match():
    codes = {'AC'}
    size, color = vs.parse_xp5_size_color('ของทั่วไปไม่มีขนาด', codes)
    assert size is None and color is None


def test_parse_xp5_size_color_unknown_token_not_treated_as_color():
    codes = {'AC'}
    size, color = vs.parse_xp5_size_color('น็อต 4 นิ้ว XYZ', codes)
    assert size == '4'
    assert color is None            # XYZ isn't a known color_finish_code


# ── x_size_token (Sendy structured `size` column, e.g. "4in") ──────────────

def test_x_size_token_strips_unit_suffix():
    assert vs.x_size_token('4in') == '4'
    assert vs.x_size_token('5.5cm') == '5.5'


def test_x_size_token_none_on_blank():
    assert vs.x_size_token(None) is None
    assert vs.x_size_token('') is None


# ── ordering tiers (§4.4: same size+color > same size > same color > rest) ─

def test_order_candidates_tiers_and_stock_desc_within_tier():
    rows = [
        {'xp5_code': 'A', 'size': '4', 'color': 'SS', 'stock': 5},   # size only
        {'xp5_code': 'B', 'size': '4', 'color': 'AC', 'stock': 2},   # size+color
        {'xp5_code': 'C', 'size': '4', 'color': 'AC', 'stock': 9},   # size+color, more stock
        {'xp5_code': 'D', 'size': '6', 'color': 'AC', 'stock': 1},   # color only
        {'xp5_code': 'E', 'size': None, 'color': None, 'stock': 100},  # rest (unparsed)
    ]
    ordered = vs.order_candidates(rows, x_size='4', x_color='AC')
    assert [r['xp5_code'] for r in ordered] == ['C', 'B', 'A', 'D', 'E']


def test_order_candidates_unparsed_attrs_never_hide_a_candidate():
    """Attribute parse failures must not hide a candidate — they just rank
    in the last tier (plan §4.4)."""
    rows = [{'xp5_code': 'X', 'size': None, 'color': None, 'stock': 1}]
    ordered = vs.order_candidates(rows, x_size='4', x_color='AC')
    assert [r['xp5_code'] for r in ordered] == ['X']
