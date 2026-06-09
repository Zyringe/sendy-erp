"""Smart-suggest helpers for BSN code mapping (/mapping page).

Used by `/mapping/suggest/<bsn_code>` endpoint to assemble:
  - Top fuzzy matches against existing products (for "map to existing")
  - Parsed structured fields (for "create new SKU" form pre-fill)
  - Cost + unit pulled from latest purchase_transactions on this BSN code

Reuses scripts/parse_sku_names.py::parse_name() so naming logic stays in one
place — tweak the parser there and both the CLI batch tool and this live
suggest endpoint pick up the change.
"""
from __future__ import annotations

import os
import re
import sys
from collections import Counter

# parse_sku_names.py is in sendy_erp/scripts/, sibling of inventory_app/
_SCRIPTS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', 'scripts')
)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import parse_sku_names as psn  # noqa: E402
import build_name_from_columns as bnc  # noqa: E402


FUZZY_THRESHOLD = 0.70  # 70% — match >= this is "likely match"
TOP_N = 5

# Stop tokens that don't help disambiguation (frequent across many products)
_STOP_TOKENS = {
    'mm', 'cm', 'in', 'นิ้ว', 'ตัว', 'แผง', 'ถุง', 'แพ็ค', 'แพ็คหัว',
    'แพ็คถุง', 'ซอง', 'อัดแผง', 'แบบหลอด', 'โหล',
    'ac', 'ss', 'cr', 'sn', 'sb', 'pb', 'gp', 'jbb', 'bz',
    'no', 'no.',
}


def _tokenize(s: str) -> list:
    """Lowercase + split on space/punct + drop stop tokens. Thai-aware in the
    sense that Thai chars stay together; we don't try word-segmentation."""
    s = (s or '').lower()
    # Normalize quote-as-inch
    s = re.sub(r'(\d+)\s*["″]', r'\1นิ้ว', s)
    # Replace common separators with space
    s = re.sub(r'[#\-/()+,]', ' ', s)
    raw = s.split()
    return [t for t in raw if t and t not in _STOP_TOKENS and len(t) >= 1]


def _jaccard(a: list, b: list) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    inter = sa & sb
    union = sa | sb
    return len(inter) / len(union) if union else 0.0


def _fuzzy_match(bsn_name: str, products: list) -> list:
    """Score products by token overlap. Returns list of dicts sorted desc."""
    bsn_tokens = _tokenize(bsn_name)
    if not bsn_tokens:
        return []
    bsn_token_counter = Counter(bsn_tokens)

    scored = []
    for p in products:
        name = p['product_name'] or ''
        p_tokens = _tokenize(name)
        if not p_tokens:
            continue
        score = _jaccard(bsn_tokens, p_tokens)
        # Boost: substring match of BSN's longest token in product name
        longest = max(bsn_tokens, key=len) if bsn_tokens else ''
        if len(longest) >= 3 and longest in name.lower():
            score = min(1.0, score + 0.10)
        if score > 0:
            scored.append({
                'product_id':   p['id'],
                'product_name': name,
                'unit_type':    p['unit_type'] if 'unit_type' in p.keys() else 'ตัว',
                'stock':        p['stock'] if 'stock' in p.keys() else 0,
                'score':        round(score, 3),
                'is_likely':    score >= FUZZY_THRESHOLD,
            })

    scored.sort(key=lambda r: -r['score'])
    return scored[:TOP_N]


def _latest_purchase(conn, bsn_code: str) -> dict:
    """Pull latest purchase_transactions row for this BSN code.
    Returns cost, unit, qty for prefilling new-SKU form. None on no match.
    Caller uses cost_price only — for the *unit* the BSN normally bills in,
    use _latest_bsn_unit() (which falls back to sales for sale-only codes)."""
    row = conn.execute(
        """SELECT unit_price, unit, qty, date_iso
             FROM purchase_transactions
            WHERE bsn_code = ?
            ORDER BY date_iso DESC, id DESC
            LIMIT 1""",
        (bsn_code,),
    ).fetchone()
    if not row:
        return {}
    return {
        'cost_price': row['unit_price'] or 0.0,
        'unit_type':  row['unit'] or 'ตัว',
        'last_qty':   row['qty'] or 0,
        'last_date':  row['date_iso'],
    }


def _latest_bsn_unit(conn, bsn_code: str) -> dict:
    """Latest unit BSN billed this code in, across purchase ∪ sales.
    Returns {unit, source, last_date} or {} if no transactions exist.
    Used for prominent banner display + unit_conversion autofill —
    cost_price is intentionally NOT included (use _latest_purchase for that)."""
    row = conn.execute(
        """SELECT unit, date_iso, src AS source FROM (
             SELECT unit, date_iso, 'purchase' AS src
               FROM purchase_transactions WHERE bsn_code = ?
             UNION ALL
             SELECT unit, date_iso, 'sale' AS src
               FROM sales_transactions WHERE bsn_code = ?
           )
           WHERE unit IS NOT NULL AND unit <> ''
           ORDER BY date_iso DESC, source DESC
           LIMIT 1""",
        (bsn_code, bsn_code),
    ).fetchone()
    if not row:
        return {}
    return {
        'unit':      row['unit'],
        'source':    row['source'],
        'last_date': row['date_iso'],
    }


def _all_units_seen(conn, bsn_code: str) -> list:
    """Distinct units this BSN code has ever billed in (purchase ∪ sales).
    Returns list of {unit, last_date}, ordered by last_date DESC.
    Length > 1 signals split-unit code (override candidate)."""
    rows = conn.execute(
        """SELECT unit, MAX(date_iso) AS last_date FROM (
             SELECT unit, date_iso FROM purchase_transactions WHERE bsn_code = ?
             UNION ALL
             SELECT unit, date_iso FROM sales_transactions WHERE bsn_code = ?
           )
           WHERE unit IS NOT NULL AND unit <> ''
           GROUP BY unit
           ORDER BY last_date DESC""",
        (bsn_code, bsn_code),
    ).fetchall()
    return [{'unit': r['unit'], 'last_date': r['last_date']} for r in rows]


def _load_parser_context(conn) -> dict:
    """Load brands + colors needed by parse_sku_names.parse_name()."""
    color_rows = conn.execute(
        "SELECT code, name_th FROM color_finish_codes ORDER BY length(code) DESC"
    ).fetchall()
    color_codes = {r['code']: r['name_th'] for r in color_rows}

    brand_rows = conn.execute(
        "SELECT id, code, name, name_th, short_code FROM brands"
    ).fetchall()
    brands_by_id = {r['id']: dict(r) for r in brand_rows}
    all_brand_tokens = []
    token_to_brand = {}
    for r in brand_rows:
        for k in ('name', 'name_th', 'short_code'):
            v = r[k]
            if v:
                all_brand_tokens.append(v)
                token_to_brand[v] = r['name']

    return {
        'color_codes': color_codes,
        'brands_by_id': brands_by_id,
        'all_brand_tokens': all_brand_tokens,
        'token_to_brand': token_to_brand,
    }


def _parse_bsn_name(bsn_name: str, ctx: dict) -> dict:
    """Run parse_sku_names.parse_name on a BSN raw name.
    Brand record unknown at parse time (no brand_id yet) → pass None and let
    parser detect brand from name tokens."""
    return psn.parse_name(
        bsn_name,
        brand_rec=None,
        color_codes=ctx['color_codes'],
        all_brand_tokens=ctx['all_brand_tokens'],
        token_to_brand=ctx.get('token_to_brand'),
    )


def _build_proposed_name(parsed: dict, ctx: dict) -> str:
    """Build canonical product_name from parsed fields.
    Reuse build_name_from_columns.build() so the format matches the
    naming-rule reference (Rule 1–24)."""
    code_to_name = {code: name for code, name in
                    [(c, n) for c, n in ctx['color_codes'].items()]}
    # parse_name returns dict with all the keys build() expects
    return bnc.build(parsed, color_lookup=None, code_to_name=code_to_name)


def suggest_for_bsn(conn, bsn_code: str, bsn_name: str) -> dict:
    """Main entry point: gather everything for the suggest modal.

    Returns:
      {
        'bsn_code': ...,
        'bsn_name': ...,
        'matches': [ { product_id, product_name, score, is_likely }, ... ],
        'parsed':  { category, series, brand, model, size, color_th,
                     color_code, packaging, condition, pack_variant },
        'proposed_name': str,
        'latest_purchase': { cost_price, unit_type, last_qty, last_date },
        'latest_unit':    { unit, source, last_date },     # purchase ∪ sales
        'units_seen':     [ {unit, last_date}, ... ],      # distinct, latest first
        'brand_id_suggested': int|None,
      }
    """
    ctx = _load_parser_context(conn)

    # Fuzzy match existing — include unit_type + stock so modal can show
    # unit mismatch warning + current stock per candidate
    products = conn.execute("""
        SELECT p.id, p.product_name, p.unit_type,
               COALESCE(s.quantity, 0) AS stock
          FROM products p
          LEFT JOIN stock_levels s ON s.product_id = p.id
         WHERE p.is_active = 1
    """).fetchall()
    matches = _fuzzy_match(bsn_name, products)

    # Parse + propose name
    parsed = _parse_bsn_name(bsn_name, ctx)
    proposed = _build_proposed_name(parsed, ctx)

    # Map brand string → brand_id if parser detected one
    brand_id = None
    if parsed.get('brand'):
        for bid, brec in ctx['brands_by_id'].items():
            if brec['name'] == parsed['brand']:
                brand_id = bid
                break

    # Cost from latest purchase (intentionally NOT for unit display)
    latest = _latest_purchase(conn, bsn_code)
    # Unit context: latest unit across both purchase and sales (sale-only codes)
    latest_unit = _latest_bsn_unit(conn, bsn_code)
    # Split-unit awareness (length > 1 → override candidate)
    units_seen = _all_units_seen(conn, bsn_code)

    return {
        'bsn_code': bsn_code,
        'bsn_name': bsn_name,
        'matches': matches,
        'parsed': parsed,
        'proposed_name': proposed,
        'latest_purchase': latest,
        'latest_unit': latest_unit,
        'units_seen': units_seen,
        'brand_id_suggested': brand_id,
    }
