"""Identity-value display filters (#463, spec #460).

`Store the canonical value, format it at display.` These three filters are the
DISPLAY half of that rule for the employee record, and they are the only place
that decides how a national ID, a Thai phone or a bank account number is
spelled on screen.

Pure functions, tested directly: no Flask app, no database, no fixtures — the
highest seam available, and the same shape as `test_phone_entries_filter.py`.
Every consuming surface gets its own render test in
`test_employee_identity_surfaces.py`.
"""
from filters import bank_account, mask_national_id, thai_phone


# ── national ID masking (moved here from the HR blueprint, unchanged) ────────

def test_thirteen_digit_id_shows_only_its_last_four_digits():
    out = mask_national_id('1234567890123')
    assert out.count('x') == 9, "count first — a wrong split would still contain x"
    assert out.endswith('3')
    assert '0123' in out.replace('-', ''), "the last four survive"
    assert '123456789' not in out.replace('-', ''), "the first nine do NOT"


def test_thirteen_digit_id_is_grouped_in_thai_national_id_blocks():
    assert mask_national_id('1234567890123') == 'x-xxxx-xxxx0-12-3'


def test_stored_separators_do_not_change_the_masking():
    """The value may arrive with the dashes a person typed."""
    assert mask_national_id('1-2345-67890-12-3') == mask_national_id('1234567890123')


def test_missing_id_renders_the_not_recorded_marker():
    assert mask_national_id(None) == '-'
    assert mask_national_id('') == '-'


def test_too_short_to_mask_never_leaks_the_digits_it_has():
    out = mask_national_id('12')
    assert out == 'xxxx'
    assert '1' not in out and '2' not in out


def test_an_unexpected_length_is_masked_but_not_grouped():
    """Nine digits is not a Thai national ID; mask it, do not invent blocks."""
    assert mask_national_id('123456789') == 'xxxxx6789'


# ── phone in conventional Thai blocks ────────────────────────────────────────

def test_mobile_number_is_grouped_three_three_four():
    assert thai_phone('0812345678') == '081-234-5678'


def test_bangkok_landline_is_grouped_two_three_four():
    """Bangkok is the one Thai area code written as a 2-digit block."""
    assert thai_phone('021234567') == '02-123-4567'


def test_provincial_landline_is_grouped_three_three_three():
    assert thai_phone('038123456') == '038-123-456'


def test_separators_in_the_stored_value_are_re_grouped_not_kept():
    """Canonical storage is bare digits, but a legacy row may hold dashes,
    spaces or parentheses; display must not depend on which."""
    for stored in ['081-234-5678', '081 234 5678', '(081)234-5678', '081.234.5678']:
        assert thai_phone(stored) == '081-234-5678', stored


def test_a_number_of_unexpected_length_is_shown_as_stored():
    """Do not invent a grouping for something that is not a Thai phone —
    the same stance the bank-account filter takes for an unknown bank."""
    assert thai_phone('12345') == '12345'
    assert thai_phone('66812345678') == '66812345678'


def test_missing_phone_renders_empty_so_the_template_marker_survives():
    assert thai_phone(None) == ''
    assert thai_phone('') == ''


# ── bank account: grouped only when the bank is known ────────────────────────
#
# Both branches below are asserted on purpose. A filter that always grouped,
# or never grouped, would pass a one-sided test while breaking exactly the case
# the ticket exists for.

def test_known_bank_groups_the_number_the_way_its_passbook_does():
    assert bank_account('1234567890', 'ธนาคารกสิกรไทย') == '123-4-56789-0'


def test_no_bank_recorded_shows_plain_digits_and_invents_no_grouping():
    out = bank_account('1234567890', None)
    assert out == '1234567890'
    assert '-' not in out, "a guessed grouping is worse than none"


def test_a_bank_we_have_no_convention_for_also_shows_plain_digits():
    """Recorded-but-unknown is the same evidence problem as not-recorded:
    ธนาคารออมสิน uses a different digit count, so grouping it as a commercial
    account would be a guess."""
    assert bank_account('1234567890', 'ธนาคารออมสิน') == '1234567890'


def test_separators_in_the_stored_value_are_re_grouped_not_kept():
    assert bank_account('123-4-56789-0', 'ธนาคารกรุงไทย') == '123-4-56789-0'
    assert bank_account('123 4 56789 0', 'ธนาคารกรุงไทย') == '123-4-56789-0'


def test_separators_are_stripped_when_there_is_no_bank_to_group_by():
    assert bank_account('123-4-56789-0', None) == '1234567890'


def test_known_bank_but_an_unexpected_digit_count_is_left_plain():
    """The pattern is pinned to a length. Forcing it onto a number that does
    not fit would move the dashes to meaningless places."""
    assert bank_account('12345678901234', 'ธนาคารกสิกรไทย') == '12345678901234'


def test_missing_account_renders_empty_so_the_template_marker_survives():
    assert bank_account(None, 'ธนาคารกสิกรไทย') == ''
    assert bank_account('', None) == ''
