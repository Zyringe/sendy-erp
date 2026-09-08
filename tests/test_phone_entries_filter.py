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


def test_bare_digits_with_no_separators_are_still_dialable():
    """Employee-side storage (#463/#464) will be bare digits, and customers hold
    some already. The splitter must not depend on the dashes being there."""
    e = phone_entries('0812345678,024358899')
    assert len(e) == 2, "count first"
    assert [x['dial'] for x in e] == ['0812345678', '024358899']
    assert [x['text'] for x in e] == ['0812345678', '024358899']


# ── a fax glued to a phone with no comma (#462) ──────────────────────────────
#
# #461 deliberately left these alone: "messy DATA, and the
# customer_contact_normalize rollout is what fixes it". That held while the
# call card was the only consumer, because the card lost nothing — it had been
# dialling the whole blob anyway.
#
# It stopped holding when #462 moved `m/customer.html` onto this filter. That
# screen's inline workaround did `.split('F:')[0]`, so it DID dial these. Left
# unhandled, the rollout would have taken a working call button away from 7
# real customers (measured on the dev DB 2026-09-09: 27ช001, 039ว06, 056ต02,
# 038ธ04, 053อ02, 053ช22, 053อ09) — the one thing #462's acceptance criteria
# rule out ("no behaviour regression on that screen").

def test_a_fax_glued_to_a_phone_without_a_comma_still_yields_a_dial_target():
    """Real stored value, customer 053ช22."""
    e = phone_entries('053-295633-7 F:053-295638')
    assert len(e) == 2, "count first — one number, one fax"
    phone, fax = e
    assert phone['dial'] == '053295633', 'the phone lost its dial target'
    assert phone['is_fax'] is False
    assert fax['is_fax'] is True and fax['dial'] is None


def test_the_glue_needs_no_space_either():
    """Real stored value, customer 053อ02 — the marker sits tight against the
    last digit."""
    e = phone_entries('053-812993-7F:053-272114,')
    assert [x['dial'] for x in e] == ['053812993', None]
    assert e[1]['is_fax'] is True


def test_every_inline_marker_spelling_splits():
    """F: is 40 of the 58 inline markers on the dev DB; the rest are FAX:, F.,
    FAX., Fax:, 'FAX :', 'Fax :', 'F :'. One rule, not seven."""
    for marker in ('F:', 'FAX:', 'F.', 'FAX.', 'Fax:', 'FAX :', 'Fax :', 'F :', 'แฟกซ์:'):
        e = phone_entries('02-123-4567 %s02-999-8888' % marker)
        assert len(e) == 2, f'{marker!r} did not split: {e}'
        assert e[0]['dial'] == '021234567', f'{marker!r} ate the phone'
        assert e[1]['is_fax'] is True, f'{marker!r} not marked a fax'


def test_a_marker_inside_a_word_is_not_a_fax_marker():
    """`OFF:9218909` is a real stored value — an OFFICE number. Splitting on
    the bare `F:` inside it would invent a junk 'OF' entry AND mislabel a
    callable office line as a fax.
    """
    e = phone_entries('OFF:9218909')
    assert len(e) == 1, f'split a word that merely contains an f: {e}'
    assert e[0]['text'] == 'OFF:9218909'
    assert e[0]['is_fax'] is False


def test_a_leading_marker_still_makes_the_whole_chunk_one_fax():
    """The #461 behaviour must not change: a chunk that STARTS with the marker
    is one fax entry, not an empty entry plus a fax."""
    e = phone_entries('081-234-5678,F:02-123-4567')
    assert len(e) == 2, "count first"
    assert e[1]['text'] == 'F:02-123-4567' and e[1]['is_fax'] is True
