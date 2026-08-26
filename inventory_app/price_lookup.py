"""price_lookup.py — evidence-ordered B2B price resolver.

One pure function (`resolve_price`) + two search helpers (`find_products`,
`find_customers`). No Flask imports, no writes — `conn` in, `dict` out.
stdlib + `sales_filters` / `models.promotions` only, so it can run on prod
under `/opt/venv/bin/python`.

Design (see .superpowers/sdd/plan/task-1-brief.md for the numbered rules
R1-R8 this file implements):

  - `evidence_filter(alias)` is the ONE population predicate — every
    money-relevant query in this module (last-paid, lowest, promo
    evidence, the customer's typical-%) filters through it, so there is
    exactly one answer to "which sales_transactions rows count as
    evidence" (see sales_filters.py's own rationale for why that matters).
  - `epochs_for` / `_epoch_candidates` find the most recent "price regime
    change" for a product (base price change, promo start, promo end with
    no replacement, or — for the ~124 dozen-only products whose only price
    lives in product_price_tiers — a tier change via audit_log). The
    window used for last-paid/lowest/promo-evidence never reaches behind
    that date.
  - `latest_evidence` is the one place that turns a sales_transactions row
    into "cash the customer paid, per some unit" — used both for the
    in-window last-paid answer and (with an unbounded window_from) for the
    pre-window informational lookup that drives `price_changed_since_last`.

Python 3.9+ compatible (no `X | None` syntax) — same constraint as
sales_filters.py, this runs on prod's older interpreter too.
"""
import re
import statistics
from collections import defaultdict
from datetime import date, timedelta

import sales_filters
from models import promotions as promo_models

# Cost-basis dummy invoices (every line at exactly cost, 2026-04-28) and the
# marketplace customer-code prefix. See task-1-brief.md "Verified codebase
# facts" — these two exclusions are NOT part of sales_filters.revenue_filter
# (that filter is shared with pages that DO want marketplace/dummy rows).
_DUMMY_DOC_BASES = ('IV6900401', 'IV6900402', 'IV6900403')

# R1 unit normalization — only these three free-text forms collapse to the
# canonical 'โหล'. Everything else (unit_type, 'แผง', 'ลัง', ...) passes
# through unchanged; there is no general alias-table lookup here (YAGNI —
# bsn_unit_alias exists but nothing in the app reads it yet, and the brief
# names exactly these three forms).
_UNIT_ALIASES = {'โหล': 'โหล', '1 โหล': 'โหล', 'หล': 'โหล'}

# A tier's qty_label carries a leading count ('1 โหล', '1กิโล') that isn't
# part of the unit identity. Stripped before comparing to a normalized ask
# unit. Deliberately exact-match after stripping: '1 โหล (special)' and
# '1 โหลคู่' must NOT collapse onto 'โหล' — they are different SKUs of tier
# (a hand-set special price, and dozen-PAIRS respectively).
_TIER_QTY_PREFIX_RE = re.compile(r'^\d+\s*')


def _strip_tier_qty(qty_label):
    return _TIER_QTY_PREFIX_RE.sub('', qty_label or '').strip()


def evidence_filter(alias):
    """The population predicate for 'does this sales_transactions row count
    as evidence of a real B2B price'. Built on sales_filters.revenue_filter
    (excludes SR/HS/write-offs, doc_base-keyed) plus the two exclusions
    that filter does not cover: marketplace รายการหน้าร้าน (customer
    prefix) and the cost-basis dummy invoices (doc_base-keyed, per
    quote_worsawat.py's EXCLUDED_DOC_BASES). qty > 0 / net > 0 are folded
    in here too (not left to each caller) — every consumer of this
    predicate needs both checks, and a caller that forgot one is exactly
    the kind of silent population drift verification-discipline.md warns
    about.

    `alias` is the table alias used in the caller's FROM clause ('st' for
    every query in this module).
    """
    p = f'{alias}.' if alias else ''
    return (
        f"{sales_filters.revenue_filter(alias)} "
        f"AND {p}qty > 0 AND {p}net > 0 "
        f"AND {p}customer NOT LIKE 'หน้าร้าน%' "
        f"AND {p}doc_base NOT IN ('{_DUMMY_DOC_BASES[0]}','{_DUMMY_DOC_BASES[1]}','{_DUMMY_DOC_BASES[2]}')"
    )


# ── product / tier / ratio lookups ──────────────────────────────────────────

def _get_product(conn, product_id):
    return conn.execute("""
        SELECT p.id, p.product_name, p.unit_type, p.base_sell_price, p.cost_price,
               p.is_active,
               COALESCE(b.name_th, b.name) AS brand_name,
               COALESCE(b.is_own_brand, 0) AS is_own_brand
        FROM products p LEFT JOIN brands b ON b.id = p.brand_id
        WHERE p.id = ?
    """, (product_id,)).fetchone()


def _find_matching_tier(conn, product_id, unit):
    """The tier row (if any) for this product whose qty_label — with its
    leading count stripped — equals `unit` exactly."""
    rows = conn.execute(
        "SELECT id, qty_label, price FROM product_price_tiers WHERE product_id = ?",
        (product_id,)
    ).fetchall()
    for r in rows:
        if _strip_tier_qty(r['qty_label']) == unit:
            return r
    return None


def _first_tier(conn, product_id):
    """This product's 'primary' tier — used only for the dozen-only
    fallback (base_sell_price = 0, asked unit is the piece, no tier
    matches the piece itself). Same ordering as
    models.promotions.get_product_price_tiers."""
    return conn.execute(
        "SELECT id, qty_label, price FROM product_price_tiers "
        "WHERE product_id = ? ORDER BY sort_order, price LIMIT 1",
        (product_id,)
    ).fetchone()


def _normalize_unit(unit, unit_type):
    if unit is None:
        return unit_type
    return _UNIT_ALIASES.get(unit, unit)


def _resolve_ratio(conn, product_id, unit):
    """(ratio, ratio_source) for `unit`, per R1: unit_conversions row first;
    else a matching โหล tier implies ratio 12; else 1.0 / 'none'."""
    row = conn.execute(
        "SELECT ratio FROM unit_conversions WHERE product_id = ? AND bsn_unit = ?",
        (product_id, unit)
    ).fetchone()
    if row is not None:
        return float(row['ratio']), 'unit_conversions'
    tier = _find_matching_tier(conn, product_id, unit)
    if tier is not None and unit == 'โหล':
        return 12.0, 'tier-implied'
    return 1.0, 'none'


def _bill_ratio(conn, product_id, unit_type, bill_unit, cache):
    """Ratio to convert a sales_transactions row's own `unit` back to the
    product's base unit_type. None when the bill's unit has no
    unit_conversions row — the caller must skip the row, never assume 1
    (R4: 'a bill whose unit has no ratio is skipped and counted in
    window.n_unratioed')."""
    if not bill_unit or bill_unit == unit_type:
        return 1.0
    if bill_unit in cache:
        return cache[bill_unit]
    row = conn.execute(
        "SELECT ratio FROM unit_conversions WHERE product_id = ? AND bsn_unit = ?",
        (product_id, bill_unit)
    ).fetchone()
    val = float(row['ratio']) if row is not None else None
    cache[bill_unit] = val
    return val


def _resolve_list(conn, product_id, unit_type, base, asked_unit):
    """R1: unit + list. Returns a dict with ratio/ratio_source/answer_unit
    (answer_unit differs from asked_unit only in the dozen-only case, where
    the answer switches to the tier's own unit) /list_for_unit/list_source/
    tier_equals_base_x_ratio."""
    ratio, ratio_source = _resolve_ratio(conn, product_id, asked_unit)
    tier = _find_matching_tier(conn, product_id, asked_unit)
    answer_unit = asked_unit

    if tier is not None:
        list_for_unit = round(float(tier['price']), 2)
        list_source = 'tier'
    elif base == 0 and asked_unit == unit_type:
        fallback = _first_tier(conn, product_id)
        if fallback is not None:
            fb_unit = _strip_tier_qty(fallback['qty_label'])
            ratio, ratio_source = _resolve_ratio(conn, product_id, fb_unit)
            answer_unit = fb_unit
            list_for_unit = round(float(fallback['price']), 2)
            list_source = 'dozen-only'
        else:
            list_for_unit = round(base * ratio, 2)
            list_source = 'base×ratio'
    else:
        list_for_unit = round(base * ratio, 2)
        list_source = 'base×ratio'

    tier_equals_base_x_ratio = abs(list_for_unit - round(base * ratio, 2)) < 0.01

    return {
        'ratio': ratio,
        'ratio_source': ratio_source,
        'answer_unit': answer_unit,
        'list_for_unit': list_for_unit,
        'list_source': list_source,
        'tier_equals_base_x_ratio': tier_equals_base_x_ratio,
    }


def _apply_price_promo(list_for_unit, ratio, price_promo):
    """R2 list_after_promo. percent (and a 'mixed' row using discount_value
    as a percent — see price_lookup module docstring / task-1-report for
    why: the 15 real mixed+discount rows in the catalog carry values like
    10/15/20, which are percentages, not final per-piece prices; a FIXED
    final price of ฿10-20 on a ฿35-250 product is not a real catalog
    price) → list × (1 − d/100). fixed → discount_value × ratio (fixed IS
    the final per-PIECE price, mirrors models.promotions.effective_price)."""
    if price_promo is None:
        return list_for_unit
    if price_promo['promo_type'] == 'fixed':
        return round(price_promo['discount_value'] * ratio, 2)
    d = price_promo['discount_value']
    if d is None:
        return list_for_unit
    return round(list_for_unit * (1 - d / 100), 2)


def _bundle_multiplier(qty_promo):
    """How many physical units are delivered per BILLED unit, for a
    buy-N-get-M-free qty promo. 1.0 when there is no qty promo or it isn't
    a bundle shape (bundle_buy/bundle_free both populated — a pure 'gift'
    promo has neither). Mirrors quote_worsawat.py's `_bundle_multiplier`
    (R8: dropping this would overstate margin on bundle/mixed products)."""
    if qty_promo is None:
        return 1.0
    buy = qty_promo['bundle_buy']
    free = qty_promo['bundle_free']
    if buy and free is not None and buy > 0:
        return (buy + free) / buy
    return 1.0


# ── epoch (R4) ───────────────────────────────────────────────────────────────

def _add_days(iso_date, n):
    return (date.fromisoformat(iso_date) + timedelta(days=n)).isoformat()


def _epoch_candidates(conn, product_id, unit, today):
    """The 4 epoch sources (R4), each None if not applicable:
      base_changed  — latest product_price_history base_sell_price change
      promo_start   — the CURRENT price-slot promo's date_start
      promo_end     — last price-slot promo that ended with no replacement,
                       date_end + 1 day
      tier_changed  — latest audit_log row for product_price_tiers, for the
                       tier matching `unit` (only possible source for
                       dozen-only products; None when no tier matches).
    """
    out = {'base_changed': None, 'promo_start': None, 'promo_end': None, 'tier_changed': None}

    row = conn.execute(
        "SELECT changed_at FROM product_price_history "
        "WHERE product_id = ? AND field_name = 'base_sell_price' "
        "ORDER BY changed_at DESC, id DESC LIMIT 1",
        (product_id,)
    ).fetchone()
    if row is not None and row['changed_at']:
        out['base_changed'] = row['changed_at'][:10]

    price_expr, _qty_expr = promo_models.promo_slot_sql('')
    current = conn.execute(f"""
        SELECT date_start FROM promotions
        WHERE product_id = ? AND is_active = 1
          AND (date_start IS NULL OR date_start <= ?)
          AND (date_end IS NULL OR date_end >= ?)
          AND {price_expr}
        ORDER BY id DESC LIMIT 1
    """, (product_id, today, today)).fetchone()
    if current is not None:
        if current['date_start']:
            out['promo_start'] = current['date_start']
    else:
        closed = conn.execute(f"""
            SELECT MAX(date_end) AS d FROM promotions
            WHERE product_id = ? AND {price_expr}
              AND date_end IS NOT NULL
              AND (is_active = 0 OR date_end < ?)
        """, (product_id, today)).fetchone()
        if closed is not None and closed['d']:
            out['promo_end'] = _add_days(closed['d'], 1)

    if unit:
        tier = _find_matching_tier(conn, product_id, unit)
        if tier is not None:
            arow = conn.execute(
                "SELECT created_at FROM audit_log "
                "WHERE table_name = 'product_price_tiers' AND row_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (tier['id'],)
            ).fetchone()
            if arow is not None and arow['created_at']:
                out['tier_changed'] = arow['created_at'][:10]

    return out


def _epoch_with_reason(conn, product_id, unit, today):
    cands = _epoch_candidates(conn, product_id, unit, today)
    best_source, best_date = None, None
    for src, d in cands.items():
        if d and (best_date is None or d > best_date):
            best_date, best_source = d, src
    return best_date, best_source


def epochs_for(conn, product_ids, unit_by_pid, today=None):
    """{pid: date | None} — the most recent price-regime-change date for
    each product, or None if none of the 4 sources apply. `unit_by_pid[pid]`
    is the unit whose tier (if any) should be watched for source 4 — pass
    the resolved answer unit for that product. `today` is an addition
    beyond the brief's literal 3-arg signature: without it, epoch
    computation for 'the current promo' / 'a promo that ended' would always
    use real wall-clock date.today() even when a caller (resolve_price)
    was asked to simulate a different `today` for determinism — silently
    breaking any test that pins `today`. Purely additive/keyword-only, so
    every 3-positional-arg call site still works unchanged."""
    today = today or date.today().isoformat()
    result = {}
    for pid in product_ids:
        cands = _epoch_candidates(conn, pid, unit_by_pid.get(pid), today)
        vals = [v for v in cands.values() if v]
        result[pid] = max(vals) if vals else None
    return result


# ── evidence lookups ─────────────────────────────────────────────────────────

def latest_evidence(conn, product_id, customer_code, window_from, unit=None):
    """The customer's most recent evidence-filtered, ratio-convertible bill
    for this product at/after `window_from` (pass '' for an unbounded
    search — used by resolve_price for the pre-window informational
    lookup), converted to `unit` (default: the product's unit_type).
    None when customer_code is falsy, the product doesn't exist, or no
    matching bill exists."""
    if not customer_code:
        return None
    prod = _get_product(conn, product_id)
    if prod is None:
        return None
    unit_type = prod['unit_type']
    target_unit = unit or unit_type
    ratio, _source = _resolve_ratio(conn, product_id, target_unit)

    rows = conn.execute(f"""
        SELECT * FROM sales_transactions st
        WHERE st.product_id = ? AND st.customer_code = ?
          AND st.date_iso >= ?
          AND {evidence_filter('st')}
        ORDER BY st.date_iso DESC, st.id DESC
    """, (product_id, customer_code, window_from)).fetchall()

    cache = {}
    for row in rows:
        bill_ratio = _bill_ratio(conn, product_id, unit_type, row['unit'], cache)
        if bill_ratio is None:
            continue
        cash_pp = (row['net'] / row['qty']) * (1.07 if row['vat_type'] == 2 else 1.0) / bill_ratio
        return {
            'cash_per_unit': round(cash_pp * ratio, 2),
            'unit': target_unit,
            'qty': row['qty'],
            'date': row['date_iso'],
            'doc_no': row['doc_no'],
        }
    return None


def _evidence_rows(conn, product_id, from_date, today):
    return conn.execute(f"""
        SELECT * FROM sales_transactions st
        WHERE st.product_id = ?
          AND st.date_iso >= ? AND st.date_iso <= ?
          AND {evidence_filter('st')}
        ORDER BY st.date_iso DESC, st.id DESC
    """, (product_id, from_date, today)).fetchall()


def _window(conn, product_id, epoch, today):
    """R4 window. Returns (from_date, n_bills, widened)."""
    today_d = date.fromisoformat(today)
    from_365 = (today_d - timedelta(days=365)).isoformat()
    if epoch:
        from_365 = max(epoch, from_365)
    n_bills = len(_evidence_rows(conn, product_id, from_365, today))
    if n_bills < 3:
        from_730 = (today_d - timedelta(days=730)).isoformat()
        if epoch:
            from_730 = max(epoch, from_730)
        n_bills = len(_evidence_rows(conn, product_id, from_730, today))
        return from_730, n_bills, True
    return from_365, n_bills, False


# ── customer context (R7) ────────────────────────────────────────────────────

def _customer_context(conn, customer_code, today):
    today_d = date.fromisoformat(today)
    from_365 = (today_d - timedelta(days=365)).isoformat()
    rows = conn.execute(f"""
        SELECT st.product_id, st.qty, st.net, st.vat_type, st.unit
        FROM sales_transactions st
        WHERE st.customer_code = ?
          AND st.date_iso >= ? AND st.date_iso <= ?
          AND {evidence_filter('st')}
    """, (customer_code, from_365, today)).fetchall()

    by_pid = defaultdict(list)
    for r in rows:
        by_pid[r['product_id']].append(r)
    n_products_12m = len(by_pid)

    pct_list = []
    for pid, prows in by_pid.items():
        prod = _get_product(conn, pid)
        if prod is None or float(prod['base_sell_price']) == 0:
            continue
        cache = {}
        cash_list = []
        for r in prows:
            bill_ratio = _bill_ratio(conn, pid, prod['unit_type'], r['unit'], cache)
            if bill_ratio is None:
                continue
            cash_pp = (r['net'] / r['qty']) * (1.07 if r['vat_type'] == 2 else 1.0) / bill_ratio
            cash_list.append(cash_pp)
        if not cash_list:
            continue
        cash_pp_med = statistics.median(cash_list)
        pct_list.append((cash_pp_med / float(prod['base_sell_price']) - 1) * 100)

    typical = round(statistics.median(pct_list), 2) if len(pct_list) >= 3 else None
    return {'typical_disc_pct': typical, 'n_products_12m': n_products_12m}


# ── promo evidence (R6) ──────────────────────────────────────────────────────

def _promo_evidence(comparable, list_after_promo, ratio):
    """comparable: [(cash_per_piece, row), ...] over the window. Returns
    (promo_last_used, promo_stale)."""
    if not comparable:
        return None, None
    per_piece_promo_price = (list_after_promo / ratio) if ratio else list_after_promo
    threshold = per_piece_promo_price * 1.01
    at_or_below = [(cpp, r) for cpp, r in comparable if cpp <= threshold]
    if at_or_below:
        return max(r['date_iso'] for _cpp, r in at_or_below), False
    return None, True


# ── breadcrumb (rendering only — not asserted by any test beyond the
#    dozen-only example in the brief) ────────────────────────────────────────

def _build_breadcrumb(list_info, price_promo, list_after_promo, basis,
                       customer_last, extra_disc):
    lines = []
    if list_info['list_source'] == 'dozen-only':
        per_piece = round(list_info['list_for_unit'] / list_info['ratio'], 2) if list_info['ratio'] else list_info['list_for_unit']
        lines.append(f"ขายยกโหล ฿{list_info['list_for_unit']:g} (≈ ฿{per_piece:.2f}/ชิ้น)")
    else:
        lines.append(f"ราคาตั้ง {list_info['list_for_unit']:g}/{list_info['answer_unit']}"
                     + (" (จากช่องราคาโหล)" if list_info['list_source'] == 'tier' else ""))
    if price_promo is not None:
        if price_promo['promo_type'] == 'fixed':
            lines.append(f"ราคาพิเศษ {list_after_promo:g}")
        elif price_promo['discount_value'] is not None:
            lines.append(f"ลด {price_promo['discount_value']:g}% → {list_after_promo:g}")
    if basis == 'last_paid' and customer_last is not None:
        lines.append(f"ลูกค้านี้ครั้งล่าสุดได้ {customer_last['cash_per_unit']:g} "
                     f"({customer_last['doc_no']}, {customer_last['date']})")
    elif extra_disc:
        lines.append(f"ลดเพิ่ม {extra_disc * 100:g}%")
    return lines


# ── search helpers ───────────────────────────────────────────────────────────

def find_products(conn, query, limit=8):
    """Active products whose name matches every whitespace-separated token
    of `query` (AND, LIKE '%token%')."""
    tokens = [t for t in (query or '').split() if t]
    if not tokens:
        return []
    where = " AND ".join(["p.product_name LIKE ?"] * len(tokens))
    params = [f"%{t}%" for t in tokens]
    rows = conn.execute(f"""
        SELECT p.id, p.product_name, p.unit_type, p.base_sell_price,
               COALESCE(b.name_th, b.name) AS brand
        FROM products p LEFT JOIN brands b ON b.id = p.brand_id
        WHERE p.is_active = 1 AND {where}
        ORDER BY p.product_name
        LIMIT ?
    """, params + [limit]).fetchall()
    return [dict(r) for r in rows]


def find_customers(conn, query, limit=8):
    """Customers matching `query` exactly against code, or LIKE against
    name/nickname. last_purchase_date is a plain MAX(date_iso) — this is a
    picker/search aid, not a money computation, so it is not run through
    evidence_filter."""
    q = (query or '').strip()
    if not q:
        return []
    like = f"%{q}%"
    rows = conn.execute("""
        SELECT c.code, c.name, c.nickname,
               (SELECT MAX(date_iso) FROM sales_transactions st
                WHERE st.customer_code = c.code) AS last_purchase_date
        FROM customers c
        WHERE c.code = ? OR c.name LIKE ? OR c.nickname LIKE ?
        ORDER BY (c.code = ?) DESC, c.name
        LIMIT ?
    """, (q, like, like, q, limit)).fetchall()
    return [dict(r) for r in rows]


# ── the resolver ─────────────────────────────────────────────────────────────

def resolve_price(conn, *, product_id, customer_code=None, unit=None, qty=1,
                   extra_disc=0.0, today=None):
    """Pure function: reads conn, never writes. See module docstring + R1-R8
    in task-1-brief.md."""
    today = today or date.today().isoformat()

    prod = _get_product(conn, product_id)
    if prod is None:
        raise ValueError(f"product_id {product_id} not found")

    unit_type = prod['unit_type']
    base = float(prod['base_sell_price'])
    cost = float(prod['cost_price'])
    own_brand = bool(prod['is_own_brand'])

    asked_unit = _normalize_unit(unit, unit_type)
    list_info = _resolve_list(conn, product_id, unit_type, base, asked_unit)
    ratio = list_info['ratio']
    answer_unit = list_info['answer_unit']

    price_promo, qty_promo = promo_models.get_active_promos_by_class(product_id, today, conn)
    list_after_promo = _apply_price_promo(list_info['list_for_unit'], ratio, price_promo)

    epoch, epoch_source = _epoch_with_reason(conn, product_id, answer_unit, today)
    window_from, n_bills, widened = _window(conn, product_id, epoch, today)
    window_reason = epoch_source if (epoch is not None and window_from == epoch) else '12m'

    rows = _evidence_rows(conn, product_id, window_from, today)
    cache = {}
    n_unratioed = 0
    lowest = None
    comparable = []  # (cash_per_piece, row) — used for R6 promo evidence
    for row in rows:
        bill_ratio = _bill_ratio(conn, product_id, unit_type, row['unit'], cache)
        if bill_ratio is None:
            n_unratioed += 1
            continue
        cash_pp = (row['net'] / row['qty']) * (1.07 if row['vat_type'] == 2 else 1.0) / bill_ratio
        comparable.append((cash_pp, row))
        cash_asked = round(cash_pp * ratio, 2)
        if lowest is None or cash_asked < lowest['cash_per_unit']:
            lowest = {
                'cash_per_unit': cash_asked,
                'customer': row['customer'],
                'date': row['date_iso'],
                'doc_no': row['doc_no'],
            }

    pre_epoch = []
    if epoch is not None:
        pre_rows = conn.execute(f"""
            SELECT * FROM sales_transactions st
            WHERE st.product_id = ? AND st.date_iso < ?
              AND {evidence_filter('st')}
            ORDER BY st.date_iso DESC, st.id DESC LIMIT 3
        """, (product_id, epoch)).fetchall()
        pre_epoch = [
            {'customer': r['customer'], 'date': r['date_iso'], 'doc_no': r['doc_no']}
            for r in pre_rows
        ]

    # R3 customer.last
    customer_last = None
    in_window = False
    price_changed_since_last = False
    if customer_code:
        within = latest_evidence(conn, product_id, customer_code, window_from, unit=answer_unit)
        if within is not None:
            customer_last = within
            in_window = True
        else:
            broad = latest_evidence(conn, product_id, customer_code, '', unit=answer_unit)
            if broad is not None:
                customer_last = broad
                in_window = False
                if epoch is not None and broad['date'] < epoch:
                    price_changed_since_last = True

    # R6 promo evidence
    promo_last_used, promo_stale = _promo_evidence(comparable, list_after_promo, ratio)

    # R7 customer context
    cust_row = None
    if customer_code:
        cust_row = conn.execute(
            "SELECT code, name FROM customers WHERE code = ?", (customer_code,)
        ).fetchone()

    customer_out = None
    if cust_row is not None:
        ctx = _customer_context(conn, customer_code, today)
        customer_out = {
            'code': cust_row['code'],
            'name': cust_row['name'],
            'last': ({
                'cash_per_unit': customer_last['cash_per_unit'],
                'unit': customer_last['unit'],
                'qty': customer_last['qty'],
                'date': customer_last['date'],
                'doc_no': customer_last['doc_no'],
                'in_window': in_window,
            } if customer_last is not None else None),
            'typical_disc_pct': ctx['typical_disc_pct'],
            'n_products_12m': ctx['n_products_12m'],
        }

    # answer (R3 order)
    if customer_last is not None and in_window:
        price = customer_last['cash_per_unit']
        basis = 'last_paid'
    else:
        price = round(list_after_promo * (1 - extra_disc), 2)
        if list_info['list_source'] == 'dozen-only':
            basis = 'dozen_only'
        elif extra_disc == 0:
            basis = 'list_after_promo'
        else:
            basis = 'list_after_promo_extra'

    line_total = round(price * qty, 2)

    free_units = None
    mult = _bundle_multiplier(qty_promo)
    if qty_promo is not None and qty_promo['bundle_buy'] and qty_promo['bundle_free'] is not None:
        free_units = {
            'buy': qty_promo['bundle_buy'],
            'free': qty_promo['bundle_free'],
            'unit': qty_promo['bundle_unit'] or answer_unit,
        }

    breadcrumb = _build_breadcrumb(list_info, price_promo, list_after_promo, basis,
                                    customer_last if in_window else None, extra_disc)

    answer = {
        'price_per_unit': price,
        'unit': answer_unit,
        'qty': qty,
        'line_total': line_total,
        'basis': basis,
        'breadcrumb': breadcrumb,
        'free_units': free_units,
    }

    # R8 internal
    cost_per_unit = round(cost * ratio, 2)
    cost_side = round(cost_per_unit * mult, 2)
    margin_incl_free_units = mult != 1.0
    margin_at_answer_pct = round((price - cost_side) / price * 100, 2) if price else None
    margin_at_lowest_pct = (
        round((lowest['cash_per_unit'] - cost_side) / lowest['cash_per_unit'] * 100, 2)
        if lowest else None
    )

    # R5 flags
    flags = []
    if price < cost_side:
        flags.append({'code': 'below_cost', 'text': f'ราคาต่ำกว่าทุน (ทุน ฿{cost_side:g})'})
    if lowest is not None and price < lowest['cash_per_unit']:
        flags.append({'code': 'new_floor',
                      'text': f'ราคานี้ต่ำกว่าที่เคยขายต่ำสุด (฿{lowest["cash_per_unit"]:g})'})
    if price_changed_since_last:
        flags.append({'code': 'price_changed_since_last',
                      'text': 'ราคาเปลี่ยนไปตั้งแต่ลูกค้ารายนี้ซื้อครั้งล่าสุด'})
    if own_brand and basis != 'last_paid' and price > list_info['list_for_unit'] * 0.90:
        flags.append({'code': 'own_brand_hint', 'text': 'own-brand ปกติลดได้ถึง −10%'})
    if widened:
        flags.append({'code': 'window_widened', 'text': 'ขยายช่วงเวลาเป็น 24 เดือนเพราะบิลในช่วง 12 เดือนมีน้อย'})
    if promo_stale:
        flags.append({'code': 'promo_stale',
                      'text': f'โปรนี้ไม่ถูกใช้ในบิล — ทุกบิล {len(comparable)} ใบล่าสุดจ่ายสูงกว่าราคาโปร'})

    return {
        'product': {
            'id': prod['id'], 'name': prod['product_name'], 'unit_type': unit_type,
            'brand': prod['brand_name'], 'own_brand': own_brand,
        },
        'unit': {'asked': asked_unit, 'ratio': ratio, 'ratio_source': list_info['ratio_source']},
        'list': {
            'base_per_piece': base,
            'list_for_unit': list_info['list_for_unit'],
            'list_source': list_info['list_source'],
            'tier_equals_base_x_ratio': list_info['tier_equals_base_x_ratio'],
            'price_promo': dict(price_promo) if price_promo is not None else None,
            'qty_promo': dict(qty_promo) if qty_promo is not None else None,
            'list_after_promo': list_after_promo,
            'promo_since': price_promo['date_start'] if price_promo is not None else None,
            'promo_source': price_promo['source'] if price_promo is not None else None,
        },
        'customer': customer_out,
        'window': {
            'from': window_from, 'reason': window_reason, 'widened_to_24m': widened,
            'n_bills': n_bills, 'n_unratioed': n_unratioed,
        },
        'context': {
            'lowest': lowest, 'promo_last_used': promo_last_used, 'promo_stale': promo_stale,
            'pre_epoch': pre_epoch,
        },
        'answer': answer,
        'flags': flags,
        'internal': {
            'cost_per_unit': cost_per_unit, 'cost_side': cost_side,
            'margin_at_answer_pct': margin_at_answer_pct,
            'margin_at_lowest_pct': margin_at_lowest_pct,
            'margin_incl_free_units': margin_incl_free_units,
        },
    }
