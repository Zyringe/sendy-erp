"""Employee identity values are STORED canonical (#464, spec #460).

The display half shipped in #463: `filters.py` turns bare digits into what a
human reads. This is the storage half. Whatever an admin types into the bank
account, phone or national-ID box — dashes or not, spaces, parentheses, a
line pasted from a chat — what reaches `employees` is bare digits, and a field
left blank is SQL NULL, never ''. Before this, "never entered" (NULL) and
"cleared" ('') were two states no query could tell apart, and nothing
normalized on write, so new drift kept arriving.

One function decides it — `hr_queries._identity_to_digits`, mutating the
mapping in place exactly like its neighbour `_blank_dates_to_none` — and both
employee write paths call it from inside the data-access layer, so no caller
can reach the SQL around it.

Every value here is invented. None is a real national ID, phone or account.
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

import hr_queries as hrq

FIELDS = ('national_id', 'phone', 'bank_account_no')


# ── the normalizer, tested directly on the data mapping ─────────────────────

@pytest.mark.parametrize('field, typed, stored', [
    ('national_id',     '1-2345-67890-12-3',  '1234567890123'),
    ('national_id',     '1 2345 67890 12 3',  '1234567890123'),
    ('phone',           '081-234-5678',       '0812345678'),
    ('phone',           '(02) 123 4567',      '021234567'),
    ('phone',           'โทร 081-234-5678',   '0812345678'),   # pasted from a chat
    ('bank_account_no', '123-4-56789-0',      '1234567890'),
    ('bank_account_no', '123.4.56789.0',      '1234567890'),
])
def test_whatever_was_typed_is_stored_as_bare_digits(field, typed, stored):
    data = {field: typed}
    hrq._identity_to_digits(data)
    assert data[field] == stored


@pytest.mark.parametrize('field, value', [
    ('national_id', '1234567890123'),
    ('phone', '0812345678'),
    ('bank_account_no', '1234567890'),
])
def test_an_already_canonical_value_is_left_exactly_as_it_is(field, value):
    data = {field: value}
    hrq._identity_to_digits(data)
    assert data[field] == value


@pytest.mark.parametrize('field', FIELDS)
@pytest.mark.parametrize('blank', ['', '   ', '-', '( )', None])
def test_a_cleared_field_becomes_null_never_an_empty_string(field, blank):
    """'' and NULL were two spellings of 'not recorded'. Only NULL survives.
    A box holding nothing but separators is blank too — there is no number
    in it to keep."""
    data = {field: blank}
    hrq._identity_to_digits(data)
    assert data[field] is None


def test_thai_numerals_are_stored_as_the_same_digits():
    """A Thai document or a Thai keyboard can hand over ๐-๙. Those are digits,
    not separators: stripping them would silently store NULL (or half a
    number) for an ID that was typed correctly."""
    data = {'national_id': '๑-๒๓๔๕-๖๗๘๙๐-๑๒-๓', 'phone': '๐๘๑-๒๓๔-๕๖๗๘'}
    hrq._identity_to_digits(data)
    assert data == {'national_id': '1234567890123', 'phone': '0812345678'}


def test_only_the_three_identity_fields_are_touched():
    """Digits inside a name, an address or an account holder's name are part
    of that text. The CONTROL is the identity field changing in the same call,
    so an implementation that touched nothing cannot pass."""
    data = {'full_name': 'นาย ทดสอบ 2', 'bank_account_name': 'บ-ช 1',
            'address': '12/3 ม.4', 'emp_code': 'EMP-01', 'note': '',
            'national_id': '1-2345-67890-12-3'}
    hrq._identity_to_digits(data)
    assert data['national_id'] == '1234567890123', "CONTROL — the call did run"
    assert data == {'full_name': 'นาย ทดสอบ 2', 'bank_account_name': 'บ-ช 1',
                    'address': '12/3 ม.4', 'emp_code': 'EMP-01', 'note': '',
                    'national_id': '1234567890123'}
