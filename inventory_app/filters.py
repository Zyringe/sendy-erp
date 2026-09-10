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

# The same marker INSIDE a chunk splits it in two: a fax glued straight onto a
# phone with no comma (`053-295633-7 F:053-295638`, `053-812993-7F:053-272114`)
# makes the whole run fail `is_valid_thai_phone`, so the number gets no dial
# target at all. 57 chunks on the dev DB split under the regex BELOW (⚠ prod
# lags this, re-derive there). A looser predicate without the non-letter rule
# says 58 — the extra one is `OFF:9218909`, the exact false positive that rule
# exists to stop, so the two counts answer different questions and only the
# first describes what ships.
#
# #461 left these alone on purpose ("messy DATA, the normalizer's job"), and
# that was right while the call card was the only consumer: the card had been
# dialling the whole blob anyway, so it lost nothing. It stopped being right in
# #462, which moved `m/customer.html` here — that screen's inline workaround did
# `.split('F:')[0]` and DID dial them. Three populations, three numbers, each
# measured on the dev DB 2026-09-09: without this, 7 customers LOSE a dial the
# old mobile screen gave them (the regression, and why this is not optional);
# 11 customers / 20 chunks GAIN one the pre-fix filter could not produce; 28
# customers end up dialable where the old screen never could.
#
# Two conditions, both load-bearing and both pinned by a test: the ':'/'.'
# terminator, and a NON-LETTER before the marker. Without the terminator any
# word holding an f would cut a number in half; `OFF:9218909` is a real stored
# value — an OFFICE number — and without the non-letter rule a bare-`f` tears
# it into a junk `OF` entry plus a callable line mislabelled as a fax.
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


def mask_national_id(nid):
    """Thai national ID -> last four digits only: 'x-xxxx-xxxx0-12-3'.

    Moved verbatim from `blueprints/hr.py::_mask_national_id`, which handed it
    to `hr/employees.html` by hand as `mask_nid`. It lives here so the employee
    DETAIL page can mask the number the same way the list already does, instead
    of a second copy of the rule (#463).

    Separators in the stored value are ignored, so it masks the same whether
    the number was typed with dashes or without.
    """
    if not nid:
        return "-"
    digits = "".join(c for c in str(nid) if c.isdigit())
    if len(digits) < 4:
        return "xxxx"
    masked = "x" * (len(digits) - 4) + digits[-4:]
    # format as Thai 13-digit blocks: x-xxxx-xxxxx-xx-x
    if len(masked) == 13:
        return (f"{masked[0]}-{masked[1:5]}-{masked[5:10]}"
                f"-{masked[10:12]}-{masked[12]}")
    return masked


def thai_phone(v):
    """A single bare Thai number -> conventional blocks; '' when not recorded.

    Three shapes, and nothing else is guessed at:
      10 digits          mobile           081-234-5678
       9 digits from 02  Bangkok          02-123-4567
       9 digits          provincial       038-123-456

    Anything of another length is returned exactly as stored — inventing a
    grouping for a number we do not recognise is the same mistake the
    bank-account filter refuses to make for an unknown bank.

    Separators in the stored value are dropped before grouping, so a legacy row
    holding dashes renders identically to a canonical one holding bare digits.

    Takes ONE number. A customer's phone column holds a LIST and belongs to
    `phone_entries`; this is for the single-value columns (employees.phone).
    """
    if not v:
        return ''
    digits = ''.join(c for c in str(v) if c.isdigit())
    if len(digits) == 10:
        return f'{digits[:3]}-{digits[3:6]}-{digits[6:]}'
    if len(digits) == 9:
        if digits.startswith('02'):
            return f'{digits[:2]}-{digits[2:5]}-{digits[5:]}'
        return f'{digits[:3]}-{digits[3:6]}-{digits[6:]}'
    return str(v)


# Thai bank accounts are grouped differently by bank, so the dashes are only
# ever applied when we know THAT bank's convention. A number under a bank we
# have no convention for is shown as plain digits — the same stance the ticket
# takes for a bank that was never recorded, and for the same reason: a guessed
# grouping reads as authoritative and cannot be told apart from a real one.
#
# The six below all print xxx-x-xxxxx-x on their own passbooks. Two of them are
# confirmed against stored data rather than from memory: on PROD 2026-09-10 the
# only two accounts saved WITH separators are a ธนาคารกรุงไทย and a
# ธนาคารกสิกรไทย row, both already in this shape.
#
# Deliberately NOT here, and each would need Put to confirm before it is:
#   ธนาคารยูโอบี            10 digits, but grouped xxx-xxx-xxx-x, not 3-1-5-1
#   ธนาคารออมสิน            12 digits
#   ธนาคารเพื่อการเกษตรฯ     12 digits
#   ธนาคารเกียรตินาคินภัทร / ธนาคารทิสโก้ / ธนาคารซีไอเอ็มบีไทย   unverified
# All five fall through to plain digits, which is correct-but-plain, never wrong.
_BANK_ACCOUNT_GROUPS = {
    'ธนาคารกสิกรไทย':      (3, 1, 5, 1),
    'ธนาคารไทยพาณิชย์':     (3, 1, 5, 1),
    'ธนาคารกรุงเทพ':        (3, 1, 5, 1),
    'ธนาคารกรุงไทย':        (3, 1, 5, 1),
    'ธนาคารกรุงศรีอยุธยา':   (3, 1, 5, 1),
    'ธนาคารทหารไทยธนชาต':  (3, 1, 5, 1),
}


def bank_account(v, bank=None):
    """Bank account number -> the grouping that bank's passbook uses.

    Grouped ONLY when `bank` names a convention we hold AND the stored number
    has exactly the digits that convention describes. Every other case —
    no bank, an unknown bank, a digit count that does not fit — renders plain
    digits rather than a guess (spec #460).

    Returns '' for a missing value so a template's own 'not recorded' marker
    still shows.
    """
    if not v:
        return ''
    digits = ''.join(c for c in str(v) if c.isdigit())
    groups = _BANK_ACCOUNT_GROUPS.get((bank or '').strip())
    if not groups or sum(groups) != len(digits):
        return digits
    out, i = [], 0
    for n in groups:
        out.append(digits[i:i + n])
        i += n
    return '-'.join(out)


def register_filters(app):
    app.template_filter('fmt_price')(fmt_price)
    app.template_filter('fmt_qty')(fmt_qty)
    app.template_filter('thaidate')(thaidate)
    app.template_filter('from_json')(from_json)
    app.template_filter('html_text')(html_text)
    app.template_filter('phone_entries')(phone_entries)
    app.template_filter('mask_national_id')(mask_national_id)
    app.template_filter('thai_phone')(thai_phone)
    app.template_filter('bank_account')(bank_account)
