#!/usr/bin/env python3
"""price_lookup.py — prod-runnable, read-only CLI over the B2B price resolver
(`inventory_app/price_lookup.py::resolve_price` / `find_products` /
`find_customers`). Adds no pricing logic of its own beyond the family_hint
sibling lookup below (a small query gated by the resolver's own
`evidence_filter`, not a fork of any resolver logic).

stdin (JSON), read from stdin:
    {"lines": [{"product_query"|"product_id", "customer_query"|"customer_code",
                "unit", "qty", "extra_disc"}, ...],
     "today": null}

stdout (JSON), written to stdout, always exit code 0:
    {"db_max_sale_date": "...", "db_path_basename": "inventory.db",
     "lines": [{"candidates": {...}} | {"result": {...}} | {"error": "..."}]}

Per line:
  - `product_id` (if given) is used as-is; else `product_query` is matched
    via find_products(). `customer_code`/`customer_query` work the same way,
    except a customer is OPTIONAL — no code and no query resolves to "no
    customer", not an error.
  - >1 match on either query -> "candidates" for that line (no "result").
    Zero matches, a missing product identifier, or an unresolvable unit
    (resolve_price raises ValueError) -> "error" (a string; never a Python
    traceback on stderr).
  - Exactly one match (or an explicit id/code) resolves normally via
    resolve_price(); its return dict is passed through to JSON verbatim,
    including any `None` values (JSON `null`) it carries on pack units with
    no piece ratio.
  - When a resolved line has NO real price at all (`list.list_for_unit ==
    0` — the base×ratio branch price_lookup.py actually takes for a
    base_sell_price=0 product with no tier; NOTE the brief's literal
    `list_source == 'none'` condition does not occur in price_lookup.py —
    'none' is a value of the DIFFERENT field `unit.ratio_source`, verified
    against the source, see task-1b-report.md), the result gets
    `no_list_price: true` (so the skill can say "ยังไม่มีราคาตั้ง" outright
    instead of inferring it from a ฿0) plus a `family_hint`: up to 5 active
    sibling products sharing the same `products.family_id` (excluding this
    product), each with its own base_sell_price, โหล tier price (if any),
    and most recent evidence-filtered B2B cash per piece (if any). Neither
    key is present when the product has a real price; `family_hint` is `[]`
    (with `no_list_price` still `true`) when the product has no family.

Env: importing price_lookup pulls inventory_app/models -> config, which
requires SECRET_KEY and ADMIN_PASSWORD (raises RuntimeError otherwise).
Prod sets both via the Railway environment dashboard. For a local run:
    set -a; source sendy_erp/.env; set +a
DB path: $DATABASE_PATH env var if set, else inventory_app/config.py's own
default (instance/inventory.db locally; $DATA_DIR/inventory.db on Railway).
Opened read-write — row_factory=sqlite3.Row, 10s busy timeout (via the
connect() timeout= param, same mechanism database.get_connection uses),
foreign_keys=ON — then immediately locked read-only with
`PRAGMA query_only = 1`. Deliberately NOT a `mode=ro` URI: that fails to
open a WAL database whose `-shm` sidecar does not exist yet (the documented
`empty_db` trap); `query_only` gives the same "this process cannot write"
guarantee without it, and never issues `PRAGMA journal_mode=WAL` (a normal,
idempotent statement on an already-WAL db) so the connection touches
nothing beyond ordinary reads.

Run:
    echo '{"lines":[{"product_id":50,"unit":"โหล"}]}' | python scripts/price_lookup.py
"""
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "inventory_app"))

import config  # noqa: E402  (after sys.path insert, matches other scripts/*.py)
import price_lookup as pl  # noqa: E402

# Same normalization price_lookup.py uses internally (its private
# _strip_tier_qty / _TIER_QTY_PREFIX_RE) to match a tier's qty_label
# ('1 โหล') against a bare unit name ('โหล'). Duplicated here rather than
# imported — that helper is private, and this is two lines.
_TIER_QTY_PREFIX_RE = re.compile(r'^\d+\s*')


def _open_readonly(db_path):
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA query_only = 1")
    return conn


def _db_path():
    return os.environ.get('DATABASE_PATH') or config.DATABASE_PATH


def _dozen_tier_price(conn, product_id):
    """This product's 'โหล' tier price (pack total), or None."""
    rows = conn.execute(
        "SELECT qty_label, price FROM product_price_tiers WHERE product_id = ?",
        (product_id,)
    ).fetchall()
    for r in rows:
        if _TIER_QTY_PREFIX_RE.sub('', r['qty_label'] or '').strip() == 'โหล':
            return round(float(r['price']), 2)
    return None


def _latest_cash_per_piece(conn, product_id, unit_type):
    """Most recent evidence-filtered sale of this product (any customer),
    converted to cash per unit_type — same cash formula price_lookup.py
    uses everywhere (net/qty, ×1.07 when vat_type=2, /ratio). A bill in a
    unit with no unit_conversions row is skipped, never treated as ratio 1
    (same rule as price_lookup._bill_ratio); checks the 5 most recent
    evidence-filtered bills before giving up rather than only the single
    latest one."""
    rows = conn.execute(f"""
        SELECT net, qty, vat_type, unit FROM sales_transactions st
        WHERE st.product_id = ? AND {pl.evidence_filter('st')}
        ORDER BY st.date_iso DESC, st.id DESC LIMIT 5
    """, (product_id,)).fetchall()
    for row in rows:
        if row['unit'] == unit_type:
            ratio = 1.0
        else:
            r = conn.execute(
                "SELECT ratio FROM unit_conversions WHERE product_id = ? AND bsn_unit = ?",
                (product_id, row['unit'])
            ).fetchone()
            if r is None:
                continue
            ratio = float(r['ratio'])
        cash_pp = (row['net'] / row['qty']) * (1.07 if row['vat_type'] == 2 else 1.0) / ratio
        return round(cash_pp, 2)
    return None


def _family_hint(conn, product_id):
    """Up to 5 active sibling products (same products.family_id, excluding
    this product) — surfaced when a line has no real price at all, so a
    human has something to anchor a manual quote against. [] when the
    product has no family."""
    fam = conn.execute(
        "SELECT family_id FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    family_id = fam['family_id'] if fam is not None else None
    if not family_id:
        return []
    siblings = conn.execute("""
        SELECT id, product_name, base_sell_price, unit_type
        FROM products WHERE family_id = ? AND id != ? AND is_active = 1
        ORDER BY id LIMIT 5
    """, (family_id, product_id)).fetchall()
    return [{
        'id': s['id'],
        'product_name': s['product_name'],
        'base_sell_price': s['base_sell_price'],
        'dozen_tier_price': _dozen_tier_price(conn, s['id']),
        'latest_b2b_cash_per_piece': _latest_cash_per_piece(conn, s['id'], s['unit_type']),
    } for s in siblings]


def _resolve_product(conn, line):
    """(product_id, candidates_list_or_None, error_or_None)."""
    product_id = line.get('product_id')
    if product_id is not None:
        return product_id, None, None
    query = line.get('product_query')
    if not query:
        return None, None, "line needs 'product_id' or 'product_query'"
    matches = pl.find_products(conn, query)
    if not matches:
        return None, None, f"no product matches '{query}'"
    if len(matches) > 1:
        return None, matches, None
    return matches[0]['id'], None, None


def _resolve_customer(conn, line):
    """(customer_code, candidates_list_or_None, error_or_None). Customer is
    OPTIONAL: no code and no query is (None, None, None), not an error."""
    customer_code = line.get('customer_code')
    if customer_code is not None:
        return customer_code, None, None
    query = line.get('customer_query')
    if not query:
        return None, None, None
    matches = pl.find_customers(conn, query)
    if not matches:
        return None, None, f"no customer matches '{query}'"
    if len(matches) > 1:
        out = []
        for m in matches:
            m = dict(m)
            m['last_seen_date'] = m.pop('last_purchase_date', None)
            out.append(m)
        return None, out, None
    return matches[0]['code'], None, None


def _resolve_line(conn, line, today):
    product_id, product_candidates, product_error = _resolve_product(conn, line)
    customer_code, customer_candidates, customer_error = _resolve_customer(conn, line)

    candidates = {}
    if product_candidates is not None:
        candidates['products'] = product_candidates
    if customer_candidates is not None:
        candidates['customers'] = customer_candidates
    if candidates:
        return {'candidates': candidates}

    errors = [e for e in (product_error, customer_error) if e]
    if errors:
        return {'error': '; '.join(errors)}

    qty = line.get('qty')
    if qty is None:
        qty = 1
    extra_disc = line.get('extra_disc')
    if extra_disc is None:
        extra_disc = 0.0

    try:
        result = pl.resolve_price(
            conn, product_id=product_id, customer_code=customer_code,
            unit=line.get('unit'), qty=qty, extra_disc=extra_disc, today=today,
        )
    except ValueError as e:
        return {'error': str(e)}

    if result['list']['list_for_unit'] == 0:
        result['no_list_price'] = True
        result['family_hint'] = _family_hint(conn, product_id)

    return {'result': result}


def main():
    payload = json.load(sys.stdin)
    today = payload.get('today')
    db_path = _db_path()
    conn = _open_readonly(db_path)
    try:
        lines_out = [_resolve_line(conn, line, today) for line in payload.get('lines', [])]
        db_max_sale_date = conn.execute(
            "SELECT MAX(date_iso) FROM sales_transactions"
        ).fetchone()[0]
    finally:
        conn.close()

    json.dump({
        'db_max_sale_date': db_max_sale_date,
        'db_path_basename': os.path.basename(db_path),
        'lines': lines_out,
    }, sys.stdout, ensure_ascii=False)
    sys.stdout.write('\n')


if __name__ == '__main__':
    main()
