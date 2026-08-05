"""vat-substitute — substitutability groups + invoice-time lookup / paper-
stock drawdown planning against the read-only VAT book.

See projects/vat-substitute/plan.md (rev 12, Codex GO) for the full contract.
Short version:

- Substitutability is human-curated (`vat_sub_groups`/`_members`/
  `_product_links`, mig 154) — never a pure auto rule (plan decision 2).
- Every lookup ALWAYS resolves X's own xp5 identity first (`xp5_product_
  mapping`, mig 152) and renders its own-stock card independent of groups
  (§4.1). Candidates are the union of X's groups' xp5 members; guesses are
  cold-start category suggestions (STKGRP bridge when identity-mapped, noun-
  prefix match otherwise) — never auto-added to a group without a promote
  click.
- The VAT book (vat_book.db) is READ-ONLY: every function here that touches
  it takes an explicit `book_conn` (may be None — the book was never built)
  and never issues a write. Curation writes go to the MAIN db only, one
  `BEGIN IMMEDIATE` transaction per call, refusal checks BEFORE any write
  (plan §4.6 concurrency contract — mirrors models/reconcile.py's
  apply_reconcile_flag pattern).
- Stock threshold: candidate/guess lists and planning totals filter
  `quantity >= 0.5` (kills 6e-154 FoxPro garbage + dust). The own-stock card
  is the one exception — always renders, raw number, threshold gates only
  its "ใช้ตัวเองก่อน" verdict (§4.3).

Conventions: raw SQL via sqlite3 (see database.py), no ORM. Every connection
this module touches must have row_factory = sqlite3.Row.
"""
import re

from database import get_connection
from .products import get_product

# ── Category-noun extraction + matching (§5 guess bridge, cold-start) ──────

_INCH_TOKEN = re.compile(r'(\d+(?:[./]\d+)?)\s*(?:"|″|นิ้ว|นิว)', re.IGNORECASE)
_SD_SUFFIX = re.compile(r'\s*S\s*/\s*D\s*$', re.IGNORECASE)
_LEADING_NON_DIGIT = re.compile(r'^([^\d#]*)')
_LEADING_NUMBER = re.compile(r'(\d+(?:\.\d+)?)')


def extract_category_noun(name):
    """Leading Thai category noun, normalized first (strip spaces/quotes/
    'S/D' suffix, inch marks -> นิ้ว), stopping at the first digit or '#'
    (plan §5). Empty when the name starts with a digit/#/latin code —
    callers must treat that as the explicit no-guesses state, not fall
    through to an unfiltered scan."""
    s = (name or '').strip()
    s = _SD_SUFFIX.sub('', s)
    s = _INCH_TOKEN.sub(lambda m: m.group(1) + 'นิ้ว', s)
    m = _LEADING_NON_DIGIT.match(s)
    noun = (m.group(1) if m else '')
    return noun.strip(' "\'#-_/\t')


def nouns_compatible(a, b):
    """Prefix containment either direction, shorter noun >= 3 chars (plan
    §5). Generic nouns (e.g. "ชุด...") can still cross real categories —
    accepted residual risk; guesses are advisory and human-confirmed."""
    a, b = (a or '').strip(), (b or '').strip()
    if min(len(a), len(b)) < 3:
        return False
    return a.startswith(b) or b.startswith(a)


# ── xp5 name size/color parsing + X's structured size (§4.4 ordering) ──────

_SIZE_TOKEN = re.compile(r'(\d+(?:/\d+)?)\s*(?:"|″|นิ้ว|นิว)', re.IGNORECASE)
_COLOR_SPLIT = re.compile(r'[^A-Za-zก-๙/]+')


def parse_xp5_size_color(name, color_codes):
    """Light parse of an xp5 STMAS name: size = the first inch-marked
    number; color = the first token after it that is a known
    color_finish_codes value (case-insensitive). Nullable on no match —
    unparsed attributes rank in the last ordering tier, never hide the
    candidate (plan §4.4)."""
    name = name or ''
    size = None
    tail = name
    m = _SIZE_TOKEN.search(name)
    if m:
        size = m.group(1)
        tail = name[m.end():]
    color = None
    for tok in _COLOR_SPLIT.split(tail):
        if not tok:
            continue
        tok_u = tok.upper()
        if tok_u in color_codes:
            color = tok_u
            break
    return size, color


def x_size_token(size_field):
    """Leading numeric token of Sendy's structured `products.size` (e.g.
    "4in" -> "4", "5.5cm" -> "5.5"), unit-agnostic — this only feeds a sort
    tier, never a hard filter, so cross-unit imprecision is accepted."""
    if not size_field:
        return None
    m = _LEADING_NUMBER.match(size_field.strip())
    return m.group(1) if m else None


def order_candidates(rows, x_size, x_color):
    """§4.4 ordering: same size+color -> same size -> same color -> rest;
    each tier by stock desc. Rows must already carry 'size'/'color'/'stock'
    (parsed via parse_xp5_size_color + the book stock read). A row with
    unparsed attributes simply lands in the last tier."""
    def key(r):
        size_match = x_size is not None and r.get('size') is not None and r['size'] == x_size
        color_match = (x_color is not None and r.get('color') is not None
                       and r['color'].upper() == x_color.upper())
        if size_match and color_match:
            tier = 0
        elif size_match:
            tier = 1
        elif color_match:
            tier = 2
        else:
            tier = 3
        return (tier, -(r.get('stock') or 0))
    return sorted(rows, key=key)


# ── Badge formula (§5, decisions 9/10) ──────────────────────────────────────

def compute_badge(price_per_unit, unit_ratio, x_unit, candidate_unit, candidate_cost):
    """price_per_unit = VAT-inclusive price the customer pays for ONE of the
    SELECTED deal unit (decision 9 — the workspace-wide carve-out
    convention, never add-on-top). unit_ratio converts that unit to X's own
    base unit_type (1.0 if the selected unit already IS the base unit;
    X's unit_conversions ratio otherwise — decision 10, X's side only, never
    an invented cross-product ratio).

    Compared against the candidate's book cost_price (STMAS.UNITPR-sourced,
    ex-VAT by convention — see vat_book_builder) ONLY when both units are
    non-blank and equal after normalization (both books already store
    normalized Thai unit names via bsn_units, so a plain string compare is
    the normalized compare). Otherwise "เทียบไม่ได้" — both raw numbers
    always returned so the caller can still show them."""
    ex_vat = price_per_unit / 1.07 if price_per_unit is not None else None
    per_base_unit = (ex_vat / unit_ratio
                     if ex_vat is not None and unit_ratio else None)
    x_u = (x_unit or '').strip()
    c_u = (candidate_unit or '').strip()
    result = {
        'ex_vat_per_base_unit': per_base_unit,
        'price_per_unit': price_per_unit,
        'x_unit': x_u,
        'candidate_cost': candidate_cost,
        'candidate_unit': c_u,
    }
    if not x_u or not c_u or x_u != c_u or per_base_unit is None:
        result['badge'] = 'incomparable'
        result['label'] = ('เทียบไม่ได้ (หน่วยต่างกัน)' if (x_u and c_u and x_u != c_u)
                            else 'เทียบไม่ได้')
        return result
    if not candidate_cost or candidate_cost <= 0:
        result['badge'] = 'unknown'
        result['label'] = '❓ ไม่ทราบต้นทุน'
        return result
    if per_base_unit > candidate_cost:
        result['badge'] = 'ok'
        result['label'] = '✅ คุ้ม'
    else:
        result['badge'] = 'warn'
        result['label'] = '⚠️ ไม่คุ้ม'
    return result
