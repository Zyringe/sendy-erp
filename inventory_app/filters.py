"""Jinja template filters.

Extracted verbatim from app.py (behavior-preserving split).
"""
import json
import re
from html import unescape

_HTML_BLOCK_RE = re.compile(r'(?i)<\s*(?:br|/p|/div|/li|/tr|/h[1-6]|/article|/ul|/ol)\s*/?>')
_HTML_TAG_RE = re.compile(r'<[^>]+>')


def fmt_price(v):
    if v is None:
        return '-'
    return f'{v:,.2f}'


def fmt_qty(v):
    if v is None:
        return '-'
    return f'{v:,}'


_TH_MONTHS = ['', 'ม.ค.', 'ก.พ.', 'มี.ค.', 'เม.ย.', 'พ.ค.', 'มิ.ย.',
              'ก.ค.', 'ส.ค.', 'ก.ย.', 'ต.ค.', 'พ.ย.', 'ธ.ค.']


def thaidate(v):
    """'YYYY-MM-DD' (or a datetime string) -> 'D <Thai-month-abbr> YYYY'.
    Returns the raw string if it can't be parsed (never raises)."""
    if not v:
        return ''
    s = str(v)[:10]
    try:
        y, m, d = s.split('-')
        return f'{int(d)} {_TH_MONTHS[int(m)]} {y}'
    except (ValueError, IndexError):
        return s


def from_json(v):
    """Parse a JSON string into a Python value for in-template iteration.

    Returns None for empty input or invalid JSON, so templates can use
    `{% if … %}` guards naturally.
    """
    if not v:
        return None
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


def html_text(v):
    """Marketplace HTML (Lazada descriptions) -> readable plain text:
    <br>/block-closing tags become newlines, other tags drop, entities
    unescape. Plain text (Shopee) passes through with its blank lines kept.
    Output is meant for an autoescaped element with white-space:pre-line."""
    if not v:
        return ''
    s = _HTML_BLOCK_RE.sub('\n', str(v))
    s = _HTML_TAG_RE.sub('', s)
    s = unescape(s)
    s = re.sub(r'[ \t]+', ' ', s)
    s = re.sub(r' ?\n ?', '\n', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()


# `customers.phone` holds a LIST, not a number: measured on PROD 2026-09-08,
# 1,422 of the 2,307 customers with a phone carry two to five numbers
# comma-joined in that one column. Fax markers, counted two ways because the
# predicate matters: 83 chunks contain `F:`, 111 contain `F:` OR `FAX`.
# ADR 0011 keeps the column that shape deliberately, so the split belongs here,
# at display. Since #462 this is the ONLY phone rendering in the app: the call
# card, call list, customer summary, customer map popup, contact-review screens
# and both mobile screens all read it, and the two hand-rolled `.split(',')`
# workarounds are gone. tests/test_customer_phone_render_coverage.py sweeps the
# template tree so a new surface cannot quietly introduce a second convention.
# The map popup is built in JS, so `partners._with_phone_entries` runs this
# function server-side and ships its output rather than re-deciding in JS.
_FAX_MARKER_RE = re.compile(r'(?i)^\s*(?:f|fax|แฟกซ์)\s*[:.]?\s*')

# The same marker INSIDE a chunk splits it in two. 58 chunks on the dev DB glue
# a fax straight onto a phone with no comma (`053-295633-7 F:053-295638`,
# `053-812993-7F:053-272114`), and the whole run then fails `is_valid_thai_phone`
# — so the number gets no dial target at all.
#
# #461 left these alone on purpose ("messy DATA, the normalizer's job"), and
# that was right while the call card was the only consumer: the card had been
# dialling the whole blob anyway, so it lost nothing. It stopped being right in
# #462, which moved `m/customer.html` here — that screen's inline workaround did
# `.split('F:')[0]` and DID dial them. Without this, 7 real customers lose a
# working call button.
#
# Two conditions, both load-bearing: the ':'/'.' terminator, and a NON-LETTER
# before the marker. `OFF:9218909` is a real stored value — an OFFICE number —
# and a bare-`f` rule tears it into a junk `OF` entry plus a mislabelled "fax".
_INLINE_FAX_RE = re.compile(r'(?i)(?<=[^A-Za-z])(?:fax|แฟกซ์|f)\s*[:.]')


def _split_inline_fax(chunk):
    """`'02-111 F:02-222'` -> `['02-111', 'F:02-222']`; anything else unchanged.

    Splits at most once: a second marker stays inside the fax part, where it
    changes nothing.
    """
    m = _INLINE_FAX_RE.search(chunk)
    if not m:
        return [chunk]
    return [chunk[:m.start()], chunk[m.start():]]


def phone_entries(v):
    """Split a stored phone field into one entry per number.

    Each entry is {'text', 'dial', 'is_fax'}:
      text    what to show — the stored spelling, untouched, so a single-number
              customer renders exactly as before. The one edit is structural:
              a chunk gluing a fax onto a phone is split between the two, so
              neither is shown as part of the other.
      dial    bare digits for a tel: link, or None when the entry is not a
              number anyone should call (a fax, a contact name, junk)
      is_fax  the entry carried an F:/FAX marker

    Reuses `is_valid_thai_phone` / `_landline_core_digits` rather than
    re-deciding what a dialable number is: one definition, one place. The
    private import is deliberate — a second copy of that rule is exactly how
    the two would drift.
    """
    if not v:
        return []
    from customer_contact_normalize import (is_valid_thai_phone,
                                            _landline_core_digits)
    out = []
    for chunk in str(v).split(','):
        for part in _split_inline_fax(chunk):
            text = part.strip()
            if not text:
                continue
            stripped = _FAX_MARKER_RE.sub('', text)
            is_fax = stripped != text
            # A fax is shown, never offered as a call.
            dial = None
            if not is_fax and is_valid_thai_phone(stripped):
                dial = _landline_core_digits(stripped)
            out.append({'text': text, 'dial': dial, 'is_fax': is_fax})
    return out


def register_filters(app):
    app.template_filter('fmt_price')(fmt_price)
    app.template_filter('fmt_qty')(fmt_qty)
    app.template_filter('thaidate')(thaidate)
    app.template_filter('from_json')(from_json)
    app.template_filter('html_text')(html_text)
    app.template_filter('phone_entries')(phone_entries)
