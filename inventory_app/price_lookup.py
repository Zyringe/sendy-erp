"""price_lookup.py — evidence-ordered B2B price resolver.

One pure function (`resolve_price`) + two search helpers (`find_products`,
`find_customers`). No Flask imports, no writes — `conn` in, `dict` out.
stdlib + `sales_filters` / `models.promotions` only, so it can run on prod
under `/opt/venv/bin/python`.

Design (see .superpowers/sdd/plan/task-1-brief.md for the numbered rules
R1-R8 this file implements, and .superpowers/sdd/plan/task-1-report.md for
the review-round fixes and their reasoning — two rounds so far):

  - `evidence_filter(alias)` is the ONE population predicate — every
    money-relevant query in this module (last-paid, lowest, promo
    evidence, the customer's typical-%) filters through it, so there is
    exactly one answer to "which sales_transactions rows count as
    evidence" (see sales_filters.py's own rationale for why that matters).
  - `epochs_for` / `_epoch_candidates` find the most recent "price regime
    change" for a product (base price change, promo start, promo end with
    no replacement, or — for the ~124 dozen-only products whose only price
    lives in product_price_tiers — a PRICE-carrying tier change via
    audit_log; a note/sort_order-only tier edit does not count). The
    window used for last-paid/lowest/promo-evidence never reaches behind
    that date.
  - `latest_evidence` is the one place that turns a sales_transactions row
    into "cash the customer paid, per some unit" — used both for the
    in-window last-paid answer and (with an unbounded window_from) for the
    pre-window informational lookup that drives `price_changed_since_last`.
  - `_resolve_unit` looks up a tier FIRST, then a ratio — a tier row
    matching the asked unit always answers at the tier's own price,
    whether or not a piece-equivalent ratio is derivable for it (review
    round 2). It refuses to guess a ratio: for a unit that resolves via
    neither `unit_conversions` nor a matching tier at all, it raises
    rather than silently returning ratio 1.0 (round 1, C1). When a tier
    DOES answer but no piece ratio is derivable (ratio_source='unknown',
    ratio=None), `resolve_price` degrades every ratio-dependent number
    (qty/line_total in the dozen-only case, internal cost/margin, the
    below_cost flag) to None/skipped rather than deriving them from a
    fabricated ratio of 1.0 — see `resolve_price`'s inline comments at
    each such site.

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


def _known_ratio_units(conn, product_id):
    """Units this product can answer a price question for at all — used to
    build the C1 error message. Includes units with a KNOWN ratio
    (`unit_conversions` rows) AND units that can only be answered at a
    tier's own price with the ratio itself unknown (review round 2 — the
    message must list tier-only units too, e.g. a box tier with no
    unit_conversions row, so a human reading the error knows 'กล่อง' is a
    real, answerable ask even though its piece-equivalent isn't known)."""
    units = [r['bsn_unit'] for r in conn.execute(
        "SELECT DISTINCT bsn_unit FROM unit_conversions WHERE product_id = ?",
        (product_id,)
    ).fetchall()]
    tier_units = [_strip_tier_qty(r['qty_label']) for r in conn.execute(
        "SELECT qty_label FROM product_price_tiers WHERE product_id = ?",
        (product_id,)
    ).fetchall()]
    for u in tier_units:
        if u not in units:
            units.append(u)
    return units


def _resolve_unit(conn, product_id, unit, unit_type, strict=False):
    """(ratio, ratio_source, tier_row) for `unit`.

    Tier lookup happens FIRST — a tier row matching `unit` always means
    the price question CAN be answered (at the tier's own price),
    regardless of whether a piece-ratio is separately derivable for it.
    This governs the STRICT raise condition below (review round 2 — the
    round 1 fix checked `unit_conversions`/unit_type BEFORE the tier,
    so a valid tier-only ask like `unit='กล่อง'` on a box-tiered product
    raised even though the DB has a perfectly good answer for it).

    ratio_source values:
      'unit_conversions' -- a unit_conversions row exists for (pid, unit)
      'tier-implied'      -- unit == 'โหล', a โหล tier exists, no
                              unit_conversions row (ratio = 12.0)
      'none'              -- unit == unit_type (ratio trivially 1.0)
      'unknown'           -- a tier row (any label) answers the price, but
                              no unit_conversions row exists for `unit` and
                              it isn't the โหล-implied case — ratio is
                              genuinely not derivable (ratio = None).
                              `resolve_price` must never treat this the
                              same as ratio=1.0: every ratio-dependent
                              number (qty conversion, internal cost/
                              margin, below_cost) degrades to None/skipped
                              at the one place ratio is consumed, not
                              silently computed here.

    `strict=True` raises `ValueError` (message names the product, the
    unit, and every unit that DOES resolve — including tier-only units)
    only when NONE of the above apply: `unit != unit_type`, no
    `unit_conversions` row, and no tier row matches `unit` at all. This
    is the literal C1 bug (an ask like `unit='ลัง'` with nothing in the DB
    to answer it) — a tier answering the price, even without a ratio, is
    never itself grounds for raising.

    `strict=False` (the default) is for the INTERNAL reuse site
    (`latest_evidence`'s bill-unit conversion) where a genuinely
    unresolvable unit should make that lookup return None (its existing
    "nothing usable found" contract) rather than raise.
    """
    tier = _find_matching_tier(conn, product_id, unit)

    row = conn.execute(
        "SELECT ratio FROM unit_conversions WHERE product_id = ? AND bsn_unit = ?",
        (product_id, unit)
    ).fetchone()
    if row is not None:
        return float(row['ratio']), 'unit_conversions', tier
    if tier is not None and unit == 'โหล':
        return 12.0, 'tier-implied', tier
    if unit == unit_type:
        return 1.0, 'none', tier
    if tier is not None:
        return None, 'unknown', tier
    if strict:
        prod = _get_product(conn, product_id)
        pname = prod['product_name'] if prod is not None else f'product_id {product_id}'
        known = _known_ratio_units(conn, product_id)
        resolves = ', '.join(known) if known else f'only {unit_type!r} (the base unit)'
        raise ValueError(
            f"{pname} (id {product_id}): no ratio known for unit {unit!r} "
            f"(unit_type={unit_type!r}; units that DO resolve: {resolves})"
        )
    return 1.0, 'none', tier


def _bundle_buy_ratio(conn, product_id, bundle_unit, unit_type):
    """Ratio to convert a qty_promo's `bundle_buy` (denominated in
    `bundle_unit`) into pieces, for I2's qty-gating check. 1.0 when
    `bundle_unit` is NULL or already the piece unit; else the
    `unit_conversions` ratio for `bundle_unit` specifically (per the
    ruling — not the general `_resolve_unit` chain, no tier-implied
    step). Best-effort 1.0 when no row exists: this is an internal
    margin-gating computation, not a user-facing ask, so it degrades
    rather than raising."""
    if not bundle_unit or bundle_unit == unit_type:
        return 1.0
    row = conn.execute(
        "SELECT ratio FROM unit_conversions WHERE product_id = ? AND bsn_unit = ?",
        (product_id, bundle_unit)
    ).fetchone()
    return float(row['ratio']) if row is not None else 1.0


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
    the answer switches to the tier's own unit) / asked_ratio (the ratio
    for the unit actually asked about — needed by I1's qty conversion; can
    be None in the (rare) case the asked unit itself is only answerable
    via a non-โหล tier with no unit_conversions row) / list_for_unit /
    list_source / tier_equals_base_x_ratio (None when ratio is None — it
    can't be computed without one).

    The FIRST `_resolve_unit` call below is `strict=True` — this is the
    literal ask-level resolution C1 is about.
    """
    asked_ratio, asked_ratio_source, tier = _resolve_unit(conn, product_id, asked_unit, unit_type, strict=True)
    ratio, ratio_source = asked_ratio, asked_ratio_source
    answer_unit = asked_unit

    if tier is not None:
        list_for_unit = round(float(tier['price']), 2)
        list_source = 'tier'
    elif base == 0 and asked_unit == unit_type:
        fallback = _first_tier(conn, product_id)
        if fallback is not None:
            fb_unit = _strip_tier_qty(fallback['qty_label'])
            ratio, ratio_source, _fb_tier = _resolve_unit(conn, product_id, fb_unit, unit_type, strict=False)
            answer_unit = fb_unit
            list_for_unit = round(float(fallback['price']), 2)
            list_source = 'dozen-only'
        else:
            list_for_unit = round(base * ratio, 2)
            list_source = 'base×ratio'
    else:
        list_for_unit = round(base * ratio, 2)
        list_source = 'base×ratio'

    tier_equals_base_x_ratio = (
        abs(list_for_unit - round(base * ratio, 2)) < 0.01 if ratio is not None else None
    )

    return {
        'ratio': ratio,
        'ratio_source': ratio_source,
        'answer_unit': answer_unit,
        'asked_ratio': asked_ratio,
        'list_for_unit': list_for_unit,
        'list_source': list_source,
        'tier_equals_base_x_ratio': tier_equals_base_x_ratio,
    }


def _apply_price_promo(list_for_unit, ratio, price_promo):
    """R2 list_after_promo. percent (and a 'mixed' row using discount_value
    as a percent — see the module docstring / task-1-report for why: the
    15 real mixed+discount rows in the catalog carry values like 10/15/20,
    which are percentages, not final per-piece prices; a FIXED final price
    of ฿10-20 on a ฿35-250 product is not a real catalog price) → list ×
    (1 − d/100) — this branch never needs `ratio`. fixed → discount_value
    × ratio (fixed IS the final per-PIECE price, mirrors
    models.promotions.effective_price) — when `ratio is None` (review
    round 2: a tier answers the price but no piece-ratio is derivable),
    that per-piece conversion cannot be computed; the fixed promo is left
    unapplied (list_for_unit unchanged) rather than guessed."""
    if price_promo is None:
        return list_for_unit
    if price_promo['promo_type'] == 'fixed':
        if ratio is None:
            return list_for_unit
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
    (R8: dropping this would overstate margin on bundle/mixed products).
    Whether this multiplier actually APPLIES to a given ask (I2's
    piece-qty gate) is decided by the caller in `resolve_price`, not
    here — this function only computes the ratio for when it does."""
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
      tier_changed  — latest audit_log row for product_price_tiers, for
                       the tier matching `unit`, that actually carries a
                       PRICE change: an INSERT or DELETE action (the whole
                       row is new/gone, so 'price' is always part of it),
                       or an UPDATE whose changed_fields JSON names
                       'price' specifically. A note/sort_order-only UPDATE
                       must NOT move the epoch (review round 1 ruling) —
                       audit_product_price_tiers_update's changed_fields
                       only includes fields that actually changed, so
                       `json_extract(changed_fields, '$.price')` is NULL
                       for a note-only edit and non-NULL when price moved.
                       Only possible source for dozen-only products; None
                       when no tier matches `unit`.
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
                "  AND (action IN ('INSERT','DELETE') "
                "       OR (action = 'UPDATE' "
                "           AND json_extract(changed_fields, '$.price') IS NOT NULL)) "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (tier['id'],)
            ).fetchone()
            if arow is not None and arow['created_at']:
                out['tier_changed'] = arow['created_at'][:10]

    return out


def epochs_for(conn, product_ids, unit_by_pid, today=None):
    """{pid: date | None} — the most recent price-regime-change date for
    each product, or None if none of the 4 sources apply. `unit_by_pid[pid]`
    is the unit whose tier (if any) should be watched for source 4 — pass
    the resolved answer unit for that product.

    `today` is an addition beyond the brief's literal 3-arg signature:
    without it, epoch computation for 'the current promo' / 'a promo that
    ended' would always use real wall-clock date.today() even when a
    caller (resolve_price) was asked to simulate a different `today` for
    determinism — silently breaking any test that pins `today`. It is a
    plain trailing parameter with a default (positional-or-keyword, NOT
    keyword-only — nothing stops a 4th positional argument), so every
    existing 3-positional-arg call site still works unchanged.

    `resolve_price` calls this function directly (via `_epoch_with_reason`
    below) for the date it uses, rather than duplicating the
    epoch-selection logic — so this is the one and only place that turns
    the 4 candidate sources into a single "most recent" date, exercised on
    every `resolve_price` call, not a parallel, untested bulk-only path.
    """
    today = today or date.today().isoformat()
    result = {}
    for pid in product_ids:
        cands = _epoch_candidates(conn, pid, unit_by_pid.get(pid), today)
        vals = [v for v in cands.values() if v]
        result[pid] = max(vals) if vals else None
    return result


def _epoch_with_reason(conn, product_id, unit, today):
    """(epoch_date, epoch_source) — epoch_date comes from `epochs_for`
    itself (so resolve_price's window computation and the public bulk API
    can never silently disagree); epoch_source is recovered by checking
    which of `_epoch_candidates`'s 4 named sources produced that exact
    date, purely so `resolve_price` can label `window.reason`
    (`epochs_for`'s own return type — {pid: date | None} — carries no
    reason, by the brief's contract)."""
    cands = _epoch_candidates(conn, product_id, unit, today)
    epoch = epochs_for(conn, [product_id], {product_id: unit}, today=today)[product_id]
    epoch_source = None
    if epoch is not None:
        for src, d in cands.items():
            if d == epoch:
                epoch_source = src
                break
    return epoch, epoch_source


# ── evidence lookups ─────────────────────────────────────────────────────────

def latest_evidence(conn, product_id, customer_code, window_from, unit=None, today=None):
    """The customer's most recent evidence-filtered bill for this product
    in [`window_from`, `today`] (pass window_from='' for an
    unbounded-from-below search — used by resolve_price for the
    pre-window informational lookup), converted to `unit` (default: the
    product's unit_type). None when customer_code is falsy, the product
    doesn't exist, or no matching bill exists.

    `today` (default: real wall-clock date) is an addition beyond the
    brief's literal signature, for the same reason as `epochs_for`'s —
    positional-or-keyword, not keyword-only, so every existing call site
    is unaffected. Without an upper bound the query had NO ceiling at
    all (review round 1, I4): a future-dated bill would be treated as the
    customer's most recent evidence.

    When `unit`'s ratio is unknown (review round 2 — `_resolve_unit`
    returns `ratio=None`, a tier answers the ask but no piece-equivalent
    is derivable), only a bill whose OWN unit is already exactly `unit`
    can be answered directly (no conversion needed); a bill in any other
    unit cannot be converted and is skipped, same as an ordinary
    unratioed bill.
    """
    if not customer_code:
        return None
    prod = _get_product(conn, product_id)
    if prod is None:
        return None
    today = today or date.today().isoformat()
    unit_type = prod['unit_type']
    target_unit = unit or unit_type
    ratio, _source, _tier = _resolve_unit(conn, product_id, target_unit, unit_type, strict=False)

    rows = conn.execute(f"""
        SELECT * FROM sales_transactions st
        WHERE st.product_id = ? AND st.customer_code = ?
          AND st.date_iso >= ? AND st.date_iso <= ?
          AND {evidence_filter('st')}
        ORDER BY st.date_iso DESC, st.id DESC
    """, (product_id, customer_code, window_from, today)).fetchall()

    cache = {}
    for row in rows:
        if ratio is None:
            if row['unit'] != target_unit:
                continue
            cash_val = round((row['net'] / row['qty']) * (1.07 if row['vat_type'] == 2 else 1.0), 2)
            return {
                'cash_per_unit': cash_val,
                'unit': target_unit,
                'qty': row['qty'],
                'date': row['date_iso'],
                'doc_no': row['doc_no'],
            }
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

def _promo_evidence(price_promo, comparable, list_after_promo, ratio):
    """comparable: [(cash_per_piece, row), ...] over the window. Returns
    (promo_last_used, promo_stale). No price promo -> (None, None), no
    matter what the bills look like (review round 1, C2 — this used to
    compare bills against the plain list price when no promo existed at
    all, false-flagging real live products as 'promo not used' when there
    was never a promo to use). `comparable` is populated by the caller
    only when a per-piece cash figure could actually be computed for a
    bill (round 2: with no piece ratio for the asked unit, R6's per-piece
    comparison has no basis at all, so the caller passes an empty list
    and this naturally returns (None, None) via the 'not comparable'
    branch below — no ratio math happens in this function)."""
    if price_promo is None:
        return None, None
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
    ratio = list_info['ratio']
    answer_unit = list_info['answer_unit']
    if list_info['list_source'] == 'dozen-only':
        if ratio is not None:
            per_piece = round(list_info['list_for_unit'] / ratio, 2) if ratio else list_info['list_for_unit']
            lines.append(f"ขายยก{answer_unit} ฿{list_info['list_for_unit']:g} (≈ ฿{per_piece:.2f}/ชิ้น)")
        else:
            # review round 2: no piece ratio for this pack unit — never
            # fabricate a per-piece figure (that was the round-1 bug: a
            # box's own price rendered as "≈ ฿500.00/ชิ้น").
            lines.append(f"ขายยก{answer_unit} ฿{list_info['list_for_unit']:g}")
    elif list_info['list_source'] == 'tier':
        if ratio is None:
            lines.append(f"ราคาตั้ง {list_info['list_for_unit']:g}/{answer_unit} (จากช่องราคาแพ็ค)")
        elif answer_unit == 'โหล':
            lines.append(f"ราคาตั้ง {list_info['list_for_unit']:g}/{answer_unit} (จากช่องราคาโหล)")
        else:
            lines.append(f"ราคาตั้ง {list_info['list_for_unit']:g}/{answer_unit} (จากช่องราคาแพ็ค)")
    else:
        lines.append(f"ราคาตั้ง {list_info['list_for_unit']:g}/{answer_unit}")
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
    in task-1-brief.md, and task-1-report.md for the review-round fixes
    (round 1: C1-C3, I1-I7, tier-epoch ruling; round 2: the tier-first
    ratio ordering and every ratio=None degrade site) applied here."""
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

    # R4 lowest / promo-evidence population. When `ratio` is None (review
    # round 2: the answer unit is only a tier's own price, no piece
    # equivalent), a bill can only be converted to "cash per answer_unit"
    # if its OWN unit already IS answer_unit — no via-pieces conversion is
    # possible. `new_floor` still works from such direct-unit bills;
    # anything else is uncomparable and counted in n_unratioed like an
    # ordinary unratioed bill. `comparable` (R6, which is inherently a
    # per-PIECE comparison) stays empty in this case — there is no piece
    # basis to compare against at all.
    rows = _evidence_rows(conn, product_id, window_from, today)
    cache = {}
    n_unratioed = 0
    lowest = None
    comparable = []  # (cash_per_piece, row) — used for R6 promo evidence
    for row in rows:
        if ratio is None:
            if row['unit'] != answer_unit:
                n_unratioed += 1
                continue
            cash_asked = round((row['net'] / row['qty']) * (1.07 if row['vat_type'] == 2 else 1.0), 2)
        else:
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
        within = latest_evidence(conn, product_id, customer_code, window_from, unit=answer_unit, today=today)
        if within is not None:
            customer_last = within
            in_window = True
        else:
            broad = latest_evidence(conn, product_id, customer_code, '', unit=answer_unit, today=today)
            if broad is not None:
                customer_last = broad
                in_window = False
                if epoch is not None and broad['date'] < epoch:
                    price_changed_since_last = True

    # R6 promo evidence
    promo_last_used, promo_stale = _promo_evidence(price_promo, comparable, list_after_promo, ratio)

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

    # I1 (round 1) + round 2's no-ratio dozen-only case. In the dozen-only
    # branch, `qty` was expressed in the unit the caller actually asked
    # about (always the piece), but the ANSWER is priced per `answer_unit`
    # (the tier's own unit). When `ratio` (answer_unit's ratio) is known,
    # convert qty the same way (I1); when it is None, we cannot state how
    # many answer_units the ask represents at all — answer.qty/line_total
    # become None rather than silently treating ratio as 1 (review round
    # 2's core finding). Outside the dozen-only branch, asked_unit ==
    # answer_unit always, so qty needs no conversion regardless of ratio
    # (ruling's general no-ratio case: "qty = the asked qty, it is already
    # in the tier unit").
    qty_answer = qty
    pack_only_text = None
    if list_info['list_source'] == 'dozen-only':
        if ratio is not None:
            qty_answer = qty * list_info['asked_ratio'] / ratio
            if qty_answer != round(qty_answer):
                pack_only_text = (
                    f"ขายยก{answer_unit}เท่านั้น: {qty:g} {unit_type} = {qty_answer:g} {answer_unit}"
                )
        else:
            qty_answer = None
            pack_only_text = f"ขายยก{answer_unit}เท่านั้น — ไม่ทราบจำนวนชิ้นต่อแพ็ค"

    line_total = round(price * qty_answer, 2) if qty_answer is not None else None

    # I2 (round 1): a qty (bundle) promo's free-units multiplier only
    # affects cost/margin when the ASK actually reaches the bundle's own
    # threshold, converted to pieces. When `asked_ratio` is None (round 2
    # — the asked unit itself has no known piece-equivalent), whether the
    # threshold is met can't be determined at all; never guess "applies"
    # in that case — treat it the same as not reaching the threshold
    # (mult stays 1.0, applies=False). free_units still reports the deal.
    free_units = None
    mult = 1.0
    if qty_promo is not None and qty_promo['bundle_buy'] and qty_promo['bundle_free'] is not None:
        asked_ratio = list_info['asked_ratio']
        bundle_applies = False
        if asked_ratio is not None:
            qty_pieces = qty * asked_ratio
            bundle_buy_ratio = _bundle_buy_ratio(conn, product_id, qty_promo['bundle_unit'], unit_type)
            bundle_buy_pieces = qty_promo['bundle_buy'] * bundle_buy_ratio
            bundle_applies = qty_pieces >= bundle_buy_pieces
        if bundle_applies:
            mult = _bundle_multiplier(qty_promo)
        free_units = {
            'buy': qty_promo['bundle_buy'],
            'free': qty_promo['bundle_free'],
            'unit': qty_promo['bundle_unit'] or answer_unit,
            'applies': bundle_applies,
        }

    breadcrumb = _build_breadcrumb(list_info, price_promo, list_after_promo, basis,
                                    customer_last if in_window else None, extra_disc)

    answer = {
        'price_per_unit': price,
        'unit': answer_unit,
        'qty': qty_answer,
        'line_total': line_total,
        'basis': basis,
        'breadcrumb': breadcrumb,
        'free_units': free_units,
    }

    # R8 internal. I3 (round 1): below_cost compares against cost_price x
    # ratio specifically, never cost_side (margin display only). Round 2:
    # with no piece ratio (`ratio is None`), NONE of cost_per_unit /
    # cost_side / either margin / below_cost_by can be computed at all —
    # they are None, `internal.note` says why, and below_cost is simply
    # never evaluated (not "evaluated and false").
    below_cost_flag = False
    if ratio is not None:
        cost_per_unit = round(cost * ratio, 2)
        cost_side = round(cost_per_unit * mult, 2)
        margin_incl_free_units = mult != 1.0
        margin_at_answer_pct = round((price - cost_side) / price * 100, 2) if price else None
        margin_at_lowest_pct = (
            round((lowest['cash_per_unit'] - cost_side) / lowest['cash_per_unit'] * 100, 2)
            if lowest else None
        )
        below_cost_by = round(cost_per_unit - price, 2) if price < cost_per_unit else None
        below_cost_flag = price < cost_per_unit
        internal_note = None
    else:
        cost_per_unit = None
        cost_side = None
        margin_incl_free_units = mult != 1.0
        margin_at_answer_pct = None
        margin_at_lowest_pct = None
        below_cost_by = None
        internal_note = 'no piece ratio for this pack unit'

    # R5 flags
    flags = []
    if below_cost_flag:
        flags.append({'code': 'below_cost', 'text': 'ราคาต่ำกว่าทุน'})
    if lowest is not None and price < lowest['cash_per_unit']:
        flags.append({'code': 'new_floor', 'text': 'ราคานี้ต่ำกว่าที่เคยขายต่ำสุด'})
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
    if pack_only_text is not None:
        flags.append({'code': 'pack_only', 'text': pack_only_text})

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
            'below_cost_by': below_cost_by,
            'note': internal_note,
        },
    }
