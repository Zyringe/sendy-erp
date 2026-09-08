"""filters.phone_entries — split a stored customer phone field into one entry
per number, so every surface can render each number on its own tappable line.

Why this exists: `customers.phone` holds a LIST. Measured on PROD 2026-09-08,
1,422 of the 2,307 customers with a phone (61.6%) carry two to five numbers
comma-joined in that one column, and 111 carry an inline `F:`/FAX marker. The
call card passed that raw string into `href="tel:"`, so the กดเพื่อโทร button
handed the dialler a comma-joined blob for the majority of the book.

ADR 0011: the column keeps its comma-joined shape. This is a DISPLAY fix.
"""
from filters import phone_entries


# ── the defect that motivated the work ───────────────────────────────────────

def test_multi_number_field_yields_one_entry_per_number():
    e = phone_entries('02-123-4567,081-234-5678')
    assert len(e) == 2, "count first — the assertions below pin nothing if this is 0"
    assert [x['text'] for x in e] == ['02-123-4567', '081-234-5678']


def test_no_dial_target_ever_contains_a_comma():
    """The bug: tel: got the whole comma-joined string. Guard it directly."""
    raw = '02-123-4567,081-234-5678,,  ,038-111-2222'
    e = phone_entries(raw)
    dials = [x['dial'] for x in e if x['dial']]
    assert len(dials) == 3, "count first"
    assert not any(',' in d for d in dials)
    # and a dial target is bare digits — nothing a dialler has to interpret
    assert all(d.isdigit() for d in dials)


# ── fax ──────────────────────────────────────────────────────────────────────

def test_fax_marked_entry_is_labelled_and_not_callable():
    e = phone_entries('081-234-5678,F:02-123-4567')
    assert len(e) == 2, "count first"
    phone, fax = e
    assert phone['is_fax'] is False and phone['dial'] is not None   # CONTROL
    assert fax['is_fax'] is True
    assert fax['dial'] is None, "a fax must not be offered as a number to call"


# ── the common case must not get noisier ─────────────────────────────────────

def test_single_number_renders_exactly_as_stored():
    e = phone_entries('02-435-8899')
    assert len(e) == 1
    assert e[0]['text'] == '02-435-8899', "unchanged for the 885 single-number customers"
    assert e[0]['dial'] == '024358899'


def test_empty_and_none_yield_no_entries():
    assert phone_entries(None) == []
    assert phone_entries('') == []
    assert phone_entries('   ,  , ') == []


# ── real-world junk found on PROD ────────────────────────────────────────────

def test_unreachable_text_is_shown_but_not_dialable():
    """PROD holds Thai contact names and 2-to-30-digit junk inside this column.
    Showing them is right; offering them as a call is not."""
    e = phone_entries('081-234-5678,คุณสมชาย,123')
    assert len(e) == 3, "count first"
    assert e[0]['dial'] is not None                      # CONTROL
    assert e[1]['text'] == 'คุณสมชาย' and e[1]['dial'] is None
    assert e[2]['text'] == '123' and e[2]['dial'] is None


def test_extension_dials_the_switchboard_not_the_extension():
    """`is_valid_thai_phone` accepts a trailing -NN extension; dialling the
    concatenated 11 digits would reach the wrong number. PROD has 168 entries
    of 11-12 digits, which is this shape."""
    e = phone_entries('02-123-4567-11')
    assert len(e) == 1
    assert e[0]['text'] == '02-123-4567-11', "the reader still sees the extension"
    assert e[0]['dial'] == '021234567', "but the dialler gets the core"
