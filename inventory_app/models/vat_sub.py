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
import os
import re
import sqlite3

import book_registry
from database import get_connection
from .products import get_product

# Stock threshold — kills the 6e-154 FoxPro garbage + dust (plan §4.3).
# Every candidate/guess/planning read filters on this; the own-stock card is
# the one exception (always renders raw, threshold gates only its verdict).
STOCK_THRESHOLD = 0.5

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


# ── VAT book access (always cross-read, independent of the session's
# active_book toggle — this feature lives in MAIN view, plan §4.10) ────────

def open_vat_book():
    """A fresh, short-lived read-only vat_book.db connection, regardless of
    the active_book session toggle. Mirrors book_registry.vat_book_freshness
    ()'s recipe (deliberately not the g-cached get_book_connection(), which
    returns the MAIN db when the session is in 'novat' mode). None if the
    book was never built."""
    path = book_registry.book_db_path('vat')
    if not os.path.exists(path):
        return None
    conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def get_color_codes(main_conn):
    return {r['code'] for r in main_conn.execute("SELECT code FROM color_finish_codes")}


def _fetch_book_row(book_conn, xp5_code):
    """One xp5 code's book-side row: name/unit/cost/stock/stkgrp/vatcod.
    None if the code isn't in the currently published book."""
    row = book_conn.execute("""
        SELECT m.bsn_code AS xp5_code, p.product_name, p.unit_type, p.cost_price,
               COALESCE(sl.quantity, 0) AS stock,
               COALESCE(sm.stkgrp, '') AS stkgrp, COALESCE(sm.vatcod, '') AS vatcod
        FROM product_code_mapping m
        JOIN products p ON p.id = m.product_id
        LEFT JOIN stock_levels sl ON sl.product_id = p.id
        LEFT JOIN stmas_meta sm ON sm.stkcod = m.bsn_code
        WHERE m.bsn_code = ?
    """, (xp5_code,)).fetchone()
    return dict(row) if row else None


def _own_identity_code(main_conn, product_id):
    """X's own xp5 code via xp5_product_mapping, or None. 'ignored' rows
    don't count as mapped (same convention as the existing VAT-view badge
    in blueprints/products.py). Ties go to a reviewed row over an auto one,
    then lowest xp5_code (the table's own PK) — a product mapping to >1
    xp5 code is an edge case the plan does not address; this is a
    documented v1 simplification."""
    row = main_conn.execute(
        "SELECT xp5_code FROM xp5_product_mapping "
        "WHERE product_id = ? AND status != 'ignored' "
        "ORDER BY (status = 'reviewed') DESC, xp5_code LIMIT 1",
        (product_id,)).fetchone()
    return row['xp5_code'] if row else None


def get_own_stock_card(product_id, main_conn, book_conn):
    """§4.1: X's own xp5 row, ALWAYS rendered when identity-mapped —
    independent of curated groups, before/regardless of candidate/guess
    resolution. None only when X has no (non-ignored) identity mapping at
    all."""
    xp5_code = _own_identity_code(main_conn, product_id)
    if xp5_code is None:
        return None
    if book_conn is None:
        return {'xp5_code': xp5_code, 'product_name': None, 'unit_type': None,
               'cost_price': None, 'stock': 0, 'eligible': False,
               'reason': 'สมุด VAT ยังไม่ถูกสร้าง'}
    row = _fetch_book_row(book_conn, xp5_code)
    if row is None:
        return {'xp5_code': xp5_code, 'product_name': None, 'unit_type': None,
               'cost_price': None, 'stock': 0, 'eligible': False,
               'reason': 'ไม่พบรหัสนี้ในสมุด VAT ปัจจุบัน'}
    eligible = row['stock'] >= STOCK_THRESHOLD and row['vatcod'] == '1'
    reason = None
    if not eligible:
        reason = ('สต็อกกระดาษหมด' if row['stock'] < STOCK_THRESHOLD
                  else 'VAT code ไม่ใช่ 1')
    return {**row, 'eligible': eligible, 'reason': reason}


def get_candidates(product_id, main_conn, book_conn):
    """§5 pool: union of X's groups' xp5 members, deduplicated by xp5_code,
    excluding X's own identity code (renders only as the own-stock card),
    joined to the book (stock >= threshold, VATCOD = '1'), ordered per §4.4.
    [] when X has no groups or the book isn't available."""
    if book_conn is None:
        return []
    group_ids = [r['group_id'] for r in main_conn.execute(
        "SELECT group_id FROM vat_sub_product_links WHERE product_id = ?", (product_id,))]
    if not group_ids:
        return []
    placeholders = ','.join('?' * len(group_ids))
    codes = {r['xp5_code'] for r in main_conn.execute(
        f"SELECT DISTINCT xp5_code FROM vat_sub_members WHERE group_id IN ({placeholders})",
        group_ids)}
    own_code = _own_identity_code(main_conn, product_id)
    codes.discard(own_code)
    if not codes:
        return []
    color_codes = get_color_codes(main_conn)
    rows = []
    for code in codes:
        row = _fetch_book_row(book_conn, code)
        if row is None or row['stock'] < STOCK_THRESHOLD or row['vatcod'] != '1':
            continue
        size, color = parse_xp5_size_color(row['product_name'], color_codes)
        rows.append({**row, 'size': size, 'color': color})
    x = get_product(product_id, conn=main_conn)
    x_size = x_size_token(x['size']) if x else None
    x_color = x['color_code'] if x else None
    return order_candidates(rows, x_size, x_color)


def get_guesses(product_id, main_conn, book_conn, exclude_codes=frozenset()):
    """§5 cold-start guess section. STKGRP category bridge when X is
    identity-mapped; noun-prefix match otherwise. Always filters stock >=
    threshold + VATCOD = '1', and excludes exclude_codes (the caller passes
    the candidate pool's codes) + X's own identity code (never in guesses,
    it renders only as the own-stock card).

    Returns {'empty': True, 'reason': ...} for the explicit empty states
    (no book, no STKGRP on the identity code, no extractable category noun)
    — never an unfiltered list."""
    if book_conn is None:
        return {'empty': True, 'reason': 'สมุด VAT ยังไม่ถูกสร้าง'}
    own_code = _own_identity_code(main_conn, product_id)
    excl = set(exclude_codes) | ({own_code} if own_code else set())
    color_codes = get_color_codes(main_conn)
    raw_rows = []

    if own_code:
        own_row = _fetch_book_row(book_conn, own_code)
        stkgrp = (own_row or {}).get('stkgrp') or ''
        if not stkgrp:
            return {'empty': True, 'reason': 'ไม่พบหมวดของสินค้านี้ในสมุด VAT'}
        raw_rows = [dict(r) for r in book_conn.execute("""
            SELECT m.bsn_code AS xp5_code, p.product_name, p.unit_type, p.cost_price,
                   COALESCE(sl.quantity, 0) AS stock, sm.stkgrp, sm.vatcod
            FROM stmas_meta sm
            JOIN product_code_mapping m ON m.bsn_code = sm.stkcod
            JOIN products p ON p.id = m.product_id
            LEFT JOIN stock_levels sl ON sl.product_id = p.id
            WHERE sm.stkgrp = ? AND sm.vatcod = '1'
        """, (stkgrp,))]
    else:
        x = get_product(product_id, conn=main_conn)
        noun = extract_category_noun(x['product_name'] if x else '')
        if not noun:
            return {'empty': True,
                    'reason': 'เดาหมวดไม่ได้ — เพิ่มตัวแทนเองผ่านหน้ากลุ่ม'}
        for r in book_conn.execute("""
            SELECT m.bsn_code AS xp5_code, p.product_name, p.unit_type, p.cost_price,
                   COALESCE(sl.quantity, 0) AS stock,
                   COALESCE(sm.vatcod, '') AS vatcod
            FROM product_code_mapping m
            JOIN products p ON p.id = m.product_id
            LEFT JOIN stock_levels sl ON sl.product_id = p.id
            LEFT JOIN stmas_meta sm ON sm.stkcod = m.bsn_code
            WHERE COALESCE(sm.vatcod, '') = '1'
        """):
            cand_noun = extract_category_noun(r['product_name'])
            if nouns_compatible(noun, cand_noun):
                raw_rows.append(dict(r))

    rows = []
    for r in raw_rows:
        if r['xp5_code'] in excl or r['stock'] < STOCK_THRESHOLD:
            continue
        size, color = parse_xp5_size_color(r['product_name'], color_codes)
        rows.append({**r, 'size': size, 'color': color})

    x = get_product(product_id, conn=main_conn)
    x_size = x_size_token(x['size']) if x else None
    x_color = x['color_code'] if x else None
    return {'empty': False, 'items': order_candidates(rows, x_size, x_color)}


def get_unit_options(product_id, main_conn):
    """X's unit selector options for the badge input (§5 badge formula):
    the base unit_type (ratio 1.0) plus every unit_conversions row."""
    x = get_product(product_id, conn=main_conn)
    opts = [{'unit': x['unit_type'], 'ratio': 1.0, 'is_base': True}]
    for r in main_conn.execute(
            "SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id = ?", (product_id,)):
        if r['bsn_unit'] == x['unit_type']:
            continue
        opts.append({'unit': r['bsn_unit'], 'ratio': float(r['ratio']), 'is_base': False})
    return opts
