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
import functools
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


# ── Planning mode / all-groups view (§4.9) ──────────────────────────────────

def list_all_groups(main_conn, book_conn):
    """§4.9: every group, sorted by total VAT stock desc — a group's total
    = Σ over its DISTINCT member codes passing the same eligibility filters
    as candidate lists (stock >= threshold, VATCOD = '1'). Zero-total groups
    sink to the bottom but stay in the list (they answer "nothing to drain
    here", never hidden). member_count/product_count are raw membership
    counts (NOT eligibility-filtered) — the group-management surface needs
    to show every row, eligible or not."""
    groups = main_conn.execute(
        "SELECT id, label, created_at, updated_at FROM vat_sub_groups ORDER BY id").fetchall()
    out = []
    for g in groups:
        member_rows = main_conn.execute(
            "SELECT xp5_code, added_from FROM vat_sub_members WHERE group_id=?",
            (g['id'],)).fetchall()
        linked = main_conn.execute(
            "SELECT p.id, p.product_name AS name FROM vat_sub_product_links l "
            "JOIN products p ON p.id = l.product_id WHERE l.group_id=? ORDER BY p.product_name",
            (g['id'],)).fetchall()
        members = []
        total_stock = 0.0
        for m in member_rows:
            row = _fetch_book_row(book_conn, m['xp5_code']) if book_conn is not None else None
            if row is None:
                # Stale member (code no longer in the published book, or the
                # book was never built) — still shown so it can be removed,
                # just carries no book data and never counts toward the total.
                row = {'xp5_code': m['xp5_code'], 'product_name': None,
                      'unit_type': None, 'cost_price': None, 'stock': 0,
                      'stkgrp': '', 'vatcod': ''}
            else:
                if row['stock'] >= STOCK_THRESHOLD and row['vatcod'] == '1':
                    total_stock += row['stock']
            members.append({**row, 'added_from': m['added_from']})
        out.append({
            'id': g['id'], 'label': g['label'],
            'created_at': g['created_at'], 'updated_at': g['updated_at'],
            'member_count': len(member_rows), 'product_count': len(linked),
            'total_stock': total_stock,
            'members': members,
            'linked_products': [dict(r) for r in linked],
        })
    out.sort(key=lambda x: -x['total_stock'])
    return out


def get_group_detail(group_id, main_conn, book_conn):
    """One group's full detail (members + linked products) for the group-
    management surface. None if the group doesn't exist."""
    for g in list_all_groups(main_conn, book_conn):
        if g['id'] == group_id:
            return g
    return None


# ── Curation writes (§4.5/§4.6/§5) ──────────────────────────────────────────
# Every write below: ONE `BEGIN IMMEDIATE` transaction, every refusal check
# runs BEFORE any mutation, `conn=None` (the route path) opens+owns its own
# connection so the lock is acquired the instant the call starts (mirrors
# models/reconcile.py::apply_reconcile_flag). `book_conn=None` (promote/
# add_member, the two that validate against the book) opens a fresh
# open_vat_book() and closes it before returning — tests inject their own.


def _busy_to_result(fn):
    """§4.6 timeout behavior (Codex r1): a writer lock that outlives
    busy_timeout surfaces as sqlite3.OperationalError('database is locked')
    AFTER the function's own rollback — convert it into the standard busy
    result so every POST route flashes "ระบบกำลังยุ่ง ลองใหม่อีกครั้ง" and
    redirects instead of 500ing. Any other OperationalError re-raises."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            if 'locked' in str(exc).lower():
                return {'ok': False, 'error': 'ระบบกำลังยุ่ง ลองใหม่อีกครั้ง'}
            raise
    return wrapper


def _x_group_ids(c, product_id):
    """X's current group ids, lowest first. Pulled out as its own function
    so it can be the concurrency-test seam: it is the LAST read before
    promote's create-vs-add decision — and thus before its first possible
    write — matching the check-then-write span rule (plan §4.6)."""
    return [r['group_id'] for r in c.execute(
        "SELECT group_id FROM vat_sub_product_links WHERE product_id=? ORDER BY group_id",
        (product_id,)).fetchall()]


@_busy_to_result
def promote(product_id, xp5_code, target_group_id=None, conn=None, book_conn=None):
    """§4.5 promote = idempotent add-membership. target_group_id: None =
    default (X's lowest existing group id, or cold-start create if X has
    none); an int = that specific existing group (validated); 'new' =
    force-create a new group even if X already has groups (many-to-many,
    decision 11). Validation, in order, ALL inside the write lock: X is an
    existing active product; target group exists (when a specific int is
    given); Y exists in the published book; Y's VATCOD = '1'. Category
    compatibility is NOT server-enforced (curation is human judgment) and
    stock level is NOT rechecked (zero-stock membership is legitimate)."""
    own = conn is None
    own_book = book_conn is None
    c = conn if conn is not None else get_connection()
    bc = book_conn if book_conn is not None else open_vat_book()
    try:
        c.execute("BEGIN IMMEDIATE")
        prod = c.execute(
            "SELECT product_name FROM products WHERE id=? AND is_active=1",
            (product_id,)).fetchone()
        if prod is None:
            c.rollback()
            return {'ok': False, 'error': 'ไม่พบสินค้านี้ หรือถูกปิดใช้งานแล้ว'}
        if bc is None:
            c.rollback()
            return {'ok': False, 'error': 'สมุด VAT ยังไม่ถูกสร้าง'}
        y_row = _fetch_book_row(bc, xp5_code)
        if y_row is None:
            c.rollback()
            return {'ok': False, 'error': 'ไม่พบรหัสนี้ในสมุด VAT ปัจจุบัน'}
        if y_row['vatcod'] != '1':
            c.rollback()
            return {'ok': False, 'error': 'รหัสนี้ VAT code ไม่ใช่ 1 — ใช้แทนไม่ได้'}

        existing_group_ids = _x_group_ids(c, product_id)
        force_new = target_group_id == 'new'
        created_group = False
        if force_new or (target_group_id is None and not existing_group_ids):
            label = extract_category_noun(prod['product_name']) or prod['product_name']
            gid = c.execute(
                "INSERT INTO vat_sub_groups (label) VALUES (?)", (label,)).lastrowid
            c.execute(
                "INSERT INTO vat_sub_product_links (group_id, product_id) VALUES (?, ?) "
                "ON CONFLICT DO NOTHING", (gid, product_id))
            created_group = True
        elif target_group_id is None:
            gid = existing_group_ids[0]
        else:
            grp = c.execute("SELECT 1 FROM vat_sub_groups WHERE id=?", (target_group_id,)).fetchone()
            if grp is None:
                c.rollback()
                return {'ok': False, 'error': 'ไม่พบกลุ่มที่ระบุ'}
            gid = target_group_id
            c.execute(
                "INSERT INTO vat_sub_product_links (group_id, product_id) VALUES (?, ?) "
                "ON CONFLICT DO NOTHING", (gid, product_id))

        already = c.execute(
            "SELECT 1 FROM vat_sub_members WHERE group_id=? AND xp5_code=?",
            (gid, xp5_code)).fetchone()
        c.execute(
            "INSERT INTO vat_sub_members (group_id, xp5_code, added_from) "
            "VALUES (?, ?, 'promote') ON CONFLICT(group_id, xp5_code) DO NOTHING",
            (gid, xp5_code))
        c.commit()
        return {'ok': True, 'group_id': gid, 'created_group': created_group,
                'noop': already is not None}
    except Exception:
        c.rollback()
        raise
    finally:
        if own:
            c.close()
        if own_book and bc is not None:
            bc.close()


@_busy_to_result
def add_member(group_id, xp5_code, conn=None, book_conn=None):
    """§5 Routes: group-page search-and-add. Validation: target group
    exists + Y exists in the published book + Y's VATCOD='1' — no X in this
    flow. Zero-stock codes are accepted on purpose (only the guess section's
    >= threshold filter hides them, not this write)."""
    own = conn is None
    own_book = book_conn is None
    c = conn if conn is not None else get_connection()
    bc = book_conn if book_conn is not None else open_vat_book()
    try:
        c.execute("BEGIN IMMEDIATE")
        grp = c.execute("SELECT 1 FROM vat_sub_groups WHERE id=?", (group_id,)).fetchone()
        if grp is None:
            c.rollback()
            return {'ok': False, 'error': 'ไม่พบกลุ่ม'}
        if bc is None:
            c.rollback()
            return {'ok': False, 'error': 'สมุด VAT ยังไม่ถูกสร้าง'}
        y_row = _fetch_book_row(bc, xp5_code)
        if y_row is None:
            c.rollback()
            return {'ok': False, 'error': 'ไม่พบรหัสนี้ในสมุด VAT ปัจจุบัน'}
        if y_row['vatcod'] != '1':
            c.rollback()
            return {'ok': False, 'error': 'รหัสนี้ VAT code ไม่ใช่ 1 — ใช้แทนไม่ได้'}
        already = c.execute(
            "SELECT 1 FROM vat_sub_members WHERE group_id=? AND xp5_code=?",
            (group_id, xp5_code)).fetchone()
        c.execute(
            "INSERT INTO vat_sub_members (group_id, xp5_code, added_from) "
            "VALUES (?, ?, 'manual') ON CONFLICT(group_id, xp5_code) DO NOTHING",
            (group_id, xp5_code))
        c.commit()
        return {'ok': True, 'noop': already is not None}
    except Exception:
        c.rollback()
        raise
    finally:
        if own:
            c.close()
        if own_book and bc is not None:
            bc.close()


def _member_state(c, source_group_id, target_group_id, xp5_code):
    """(source_row, target_row) — the LAST read before move_member's four-
    state branch, and thus before its first possible write. Concurrency-
    test seam, same role as _x_group_ids for promote."""
    source_row = c.execute(
        "SELECT added_from FROM vat_sub_members WHERE group_id=? AND xp5_code=?",
        (source_group_id, xp5_code)).fetchone()
    target_row = c.execute(
        "SELECT 1 FROM vat_sub_members WHERE group_id=? AND xp5_code=?",
        (target_group_id, xp5_code)).fetchone()
    return source_row, target_row


@_busy_to_result
def move_member(source_group_id, target_group_id, xp5_code, conn=None):
    """§5 Routes: re-home a member in ONE transaction. Four membership
    states, all defined (plan §5): source-only = normal move (insert target
    preserving source's added_from, delete source); both = keep the
    EXISTING target row + its provenance, delete source; target-only =
    friendly no-op; neither = reject."""
    own = conn is None
    c = conn if conn is not None else get_connection()
    try:
        c.execute("BEGIN IMMEDIATE")
        if target_group_id == source_group_id:
            c.rollback()
            return {'ok': False, 'error': 'กลุ่มต้นทางและปลายทางต้องไม่เหมือนกัน'}
        target_exists = c.execute(
            "SELECT 1 FROM vat_sub_groups WHERE id=?", (target_group_id,)).fetchone()
        if target_exists is None:
            c.rollback()
            return {'ok': False, 'error': 'ไม่พบกลุ่มปลายทาง'}
        source_row, target_row = _member_state(c, source_group_id, target_group_id, xp5_code)
        if source_row is None and target_row is None:
            c.rollback()
            return {'ok': False, 'error': 'ไม่พบสินค้าทดแทนนี้ในกลุ่มต้นทางหรือปลายทาง'}
        if source_row is None and target_row is not None:
            c.commit()
            return {'ok': True, 'noop': True, 'message': 'ย้ายแล้ว'}
        if source_row is not None and target_row is not None:
            c.execute(
                "DELETE FROM vat_sub_members WHERE group_id=? AND xp5_code=?",
                (source_group_id, xp5_code))
            c.commit()
            return {'ok': True, 'message': 'ย้ายแล้ว — ปลายทางมีอยู่แล้ว'}
        c.execute(
            "INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, ?, ?)",
            (target_group_id, xp5_code, source_row['added_from']))
        c.execute(
            "DELETE FROM vat_sub_members WHERE group_id=? AND xp5_code=?",
            (source_group_id, xp5_code))
        c.commit()
        return {'ok': True}
    except Exception:
        c.rollback()
        raise
    finally:
        if own:
            c.close()


@_busy_to_result
def remove_member(group_id, xp5_code, conn=None):
    own = conn is None
    c = conn if conn is not None else get_connection()
    try:
        c.execute("BEGIN IMMEDIATE")
        existed = c.execute(
            "SELECT 1 FROM vat_sub_members WHERE group_id=? AND xp5_code=?",
            (group_id, xp5_code)).fetchone()
        c.execute(
            "DELETE FROM vat_sub_members WHERE group_id=? AND xp5_code=?",
            (group_id, xp5_code))
        c.commit()
        return {'ok': True, 'noop': existed is None}
    except Exception:
        c.rollback()
        raise
    finally:
        if own:
            c.close()


@_busy_to_result
def link_product(group_id, product_id, conn=None):
    """The inverse of unlink_product. Rejects now-inactive products even on
    re-link (safety condition, documented — plan §4.7 reversibility)."""
    own = conn is None
    c = conn if conn is not None else get_connection()
    try:
        c.execute("BEGIN IMMEDIATE")
        grp = c.execute("SELECT 1 FROM vat_sub_groups WHERE id=?", (group_id,)).fetchone()
        if grp is None:
            c.rollback()
            return {'ok': False, 'error': 'ไม่พบกลุ่ม'}
        prod = c.execute(
            "SELECT 1 FROM products WHERE id=? AND is_active=1", (product_id,)).fetchone()
        if prod is None:
            c.rollback()
            return {'ok': False, 'error': 'ไม่พบสินค้านี้ หรือถูกปิดใช้งานแล้ว'}
        existed = c.execute(
            "SELECT 1 FROM vat_sub_product_links WHERE group_id=? AND product_id=?",
            (group_id, product_id)).fetchone()
        c.execute(
            "INSERT INTO vat_sub_product_links (group_id, product_id) VALUES (?, ?) "
            "ON CONFLICT DO NOTHING", (group_id, product_id))
        c.commit()
        return {'ok': True, 'noop': existed is not None}
    except Exception:
        c.rollback()
        raise
    finally:
        if own:
            c.close()


@_busy_to_result
def unlink_product(group_id, product_id, conn=None):
    own = conn is None
    c = conn if conn is not None else get_connection()
    try:
        c.execute("BEGIN IMMEDIATE")
        existed = c.execute(
            "SELECT 1 FROM vat_sub_product_links WHERE group_id=? AND product_id=?",
            (group_id, product_id)).fetchone()
        c.execute(
            "DELETE FROM vat_sub_product_links WHERE group_id=? AND product_id=?",
            (group_id, product_id))
        c.commit()
        return {'ok': True, 'noop': existed is None}
    except Exception:
        c.rollback()
        raise
    finally:
        if own:
            c.close()


@_busy_to_result
def rename_group(group_id, label, conn=None):
    own = conn is None
    c = conn if conn is not None else get_connection()
    try:
        c.execute("BEGIN IMMEDIATE")
        label = (label or '').strip()
        if not label:
            c.rollback()
            return {'ok': False, 'error': 'ต้องระบุชื่อกลุ่ม'}
        grp = c.execute("SELECT 1 FROM vat_sub_groups WHERE id=?", (group_id,)).fetchone()
        if grp is None:
            c.rollback()
            return {'ok': False, 'error': 'ไม่พบกลุ่ม'}
        c.execute(
            "UPDATE vat_sub_groups SET label=?, updated_at=datetime('now','localtime') WHERE id=?",
            (label, group_id))
        c.commit()
        return {'ok': True}
    except Exception:
        c.rollback()
        raise
    finally:
        if own:
            c.close()


@_busy_to_result
def delete_group(group_id, conn=None):
    """Empty groups only (zero members AND zero links, verified inside the
    transaction). Repeat/stale deletes are a defined no-op — a group is
    label-only, so deleting an already-gone one loses nothing (plan §4.7)."""
    own = conn is None
    c = conn if conn is not None else get_connection()
    try:
        c.execute("BEGIN IMMEDIATE")
        grp = c.execute("SELECT 1 FROM vat_sub_groups WHERE id=?", (group_id,)).fetchone()
        if grp is None:
            c.commit()
            return {'ok': True, 'noop': True, 'message': 'กลุ่มถูกลบไปแล้ว'}
        member_count = c.execute(
            "SELECT COUNT(*) AS n FROM vat_sub_members WHERE group_id=?", (group_id,)).fetchone()['n']
        link_count = c.execute(
            "SELECT COUNT(*) AS n FROM vat_sub_product_links WHERE group_id=?", (group_id,)).fetchone()['n']
        if member_count > 0 or link_count > 0:
            c.rollback()
            return {'ok': False, 'error': 'ลบไม่ได้ — กลุ่มนี้ยังมีสมาชิกหรือสินค้าเชื่อมอยู่'}
        c.execute("DELETE FROM vat_sub_groups WHERE id=?", (group_id,))
        c.commit()
        return {'ok': True}
    except Exception:
        c.rollback()
        raise
    finally:
        if own:
            c.close()


@_busy_to_result
def apply_substitution_sheet(rows, conn=None, book_conn=None):
    """§4.7 sheet apply — the executable algorithm for rows marked
    "ใช้แทนกันได้" on the review sheet. `rows`: iterable of {'product_id':
    int, 'xp5_code': str}. Deduplicated + sorted here (deterministic order:
    product_id, xp5_code) so callers need not pre-sort. ONE transaction; the
    caller is responsible for the `.backup` (plan §4.7 — this function only
    owns the apply+verify step, same division as apply_reconcile_flag).

    Per pair (X, Y):
      1. X already linked to >=1 group -> target = X's lowest-id group; add
         Y as member (added_from='sheet', ON CONFLICT no-op).
      2. Else Y already a member of >=1 group -> target = Y's lowest-id
         group; link X (ON CONFLICT no-op).
      3. Else -> create a group (label = X's category noun, set ONLY at
         creation) with X linked + Y as member.
    Groups are NEVER merged (existing groups always survive; a bridging row
    resolves at step 1). Idempotent, rename-stable, additive-only.

    Independent re-verify in the SAME transaction: every processed pair
    must be co-resident (a group containing X's link AND Y's membership) —
    ANY mismatch rolls back the WHOLE transaction, not just the bad row."""
    own = conn is None
    own_book = book_conn is None
    c = conn if conn is not None else get_connection()
    bc = book_conn if book_conn is not None else open_vat_book()
    dedup_rows = sorted({(r['product_id'], r['xp5_code']) for r in rows})
    try:
        c.execute("BEGIN IMMEDIATE")
        # Codex r1 finding 3: validate the WHOLE batch BEFORE the first
        # write — X must exist and be active, Y must be in the CURRENTLY
        # published book with VATCOD='1' (same eligibility promote/
        # add_member enforce). A stale or hand-edited CSV must never seed
        # orphan or book-invalid rows; ANY invalid row refuses the whole
        # batch (consistent with the mismatch-rollback stance below) so
        # Put fixes the sheet once instead of silently applying a subset.
        if bc is None:
            c.rollback()
            return {'ok': False, 'applied': 0, 'mismatches': [],
                    'invalid': [(pid, code, 'สมุด VAT ยังไม่ถูกสร้าง')
                                for pid, code in dedup_rows]}
        invalid = []
        for product_id, xp5_code in dedup_rows:
            if c.execute("SELECT 1 FROM products WHERE id=? AND is_active=1",
                         (product_id,)).fetchone() is None:
                invalid.append((product_id, xp5_code, 'X ไม่พบหรือถูกปิดใช้งาน'))
                continue
            y = _fetch_book_row(bc, xp5_code)
            if y is None:
                invalid.append((product_id, xp5_code, 'Y ไม่อยู่ในสมุด VAT ปัจจุบัน'))
            elif y['vatcod'] != '1':
                invalid.append((product_id, xp5_code, 'VATCOD ไม่ใช่ 1'))
        if invalid:
            c.rollback()
            return {'ok': False, 'applied': 0, 'mismatches': [], 'invalid': invalid}

        for product_id, xp5_code in dedup_rows:
            x_groups = [r['group_id'] for r in c.execute(
                "SELECT group_id FROM vat_sub_product_links WHERE product_id=? ORDER BY group_id",
                (product_id,)).fetchall()]
            if x_groups:
                gid = x_groups[0]
                c.execute(
                    "INSERT INTO vat_sub_members (group_id, xp5_code, added_from) "
                    "VALUES (?, ?, 'sheet') ON CONFLICT(group_id, xp5_code) DO NOTHING",
                    (gid, xp5_code))
                continue
            y_groups = [r['group_id'] for r in c.execute(
                "SELECT group_id FROM vat_sub_members WHERE xp5_code=? ORDER BY group_id",
                (xp5_code,)).fetchall()]
            if y_groups:
                gid = y_groups[0]
                c.execute(
                    "INSERT INTO vat_sub_product_links (group_id, product_id) VALUES (?, ?) "
                    "ON CONFLICT DO NOTHING", (gid, product_id))
                continue
            prod = c.execute(
                "SELECT product_name FROM products WHERE id=?", (product_id,)).fetchone()
            prod_name = prod['product_name'] if prod else str(product_id)
            label = extract_category_noun(prod_name) or prod_name
            gid = c.execute("INSERT INTO vat_sub_groups (label) VALUES (?)", (label,)).lastrowid
            c.execute(
                "INSERT INTO vat_sub_product_links (group_id, product_id) VALUES (?, ?) "
                "ON CONFLICT DO NOTHING", (gid, product_id))
            c.execute(
                "INSERT INTO vat_sub_members (group_id, xp5_code, added_from) "
                "VALUES (?, ?, 'sheet') ON CONFLICT(group_id, xp5_code) DO NOTHING",
                (gid, xp5_code))

        mismatches = []
        for product_id, xp5_code in dedup_rows:
            ok = c.execute("""
                SELECT 1 FROM vat_sub_product_links l
                JOIN vat_sub_members m ON m.group_id = l.group_id
                WHERE l.product_id = ? AND m.xp5_code = ?
            """, (product_id, xp5_code)).fetchone()
            if not ok:
                mismatches.append((product_id, xp5_code))
        if mismatches:
            c.rollback()
            return {'ok': False, 'applied': 0, 'mismatches': mismatches, 'invalid': []}
        c.commit()
        return {'ok': True, 'applied': len(dedup_rows), 'mismatches': [], 'invalid': []}
    except Exception:
        c.rollback()
        raise
    finally:
        if own:
            c.close()
        if own_book and bc is not None:
            bc.close()


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
