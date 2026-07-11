"""Link marketplace orders (Shopee/Lazada) to their Express invoice (IV).

The team books each marketplace order as ONE Express invoice under customer codes
``Zหน้าร้าน`` (Shopee) / ``Lหน้าร้าน`` (Lazada), keyed in Express shortly AFTER the
order is placed on the platform. There is no stored order_sn↔IV key, so we match
on three signals:

  1. product overlap — the order's line items (resolved to internal product_ids via
     the marketplace SKU mapping) vs the IV's product_ids. A shared product is the
     strongest identity signal, robust to the amount noise below.
  2. date — the IV is dated on/after the platform order date, almost always within a
     day or two (IV.date_iso ≥ order_date). An IV dated BEFORE the order can't be
     that order's invoice.
  3. amount — IV (VAT-aware) net vs the Shopee payout. Usually close, but Shopee
     often ADJUSTS the payout after the order (fee/rounding today; a full lump
     discount once the team moves it to รับชำระหนี้), so a ฿ gap is normal, not a
     rejection reason.

``run_automatch`` is a GLOBAL assignment, not a greedy per-order pass (a greedy
oldest-first/exact-amount-first matcher strands date-constrained later orders and
lets amount coincidences steal a different order's rightful product match — see
projects/marketplace-iv-matching/plan.md §3b / matcher-rebuild-spec.md):

  1. Build every valid product-compatible + date-valid candidate edge (see
     ``_product_compatible`` — multi-item orders/invoices need amount
     corroboration too, so a bundle can't be split by a single-item neighbour).
  2. Solve the assignment that maximizes matched orders, then minimizes total
     date-gap (nearest-date), then total |amount diff| (amount is a tiebreaker
     ONLY, never a gate) — a min-cost bipartite match. Every result here is
     ``'confident'``: product + valid date is trusted regardless of the ฿ gap.
  3. Orders still unmatched (no product info, or their product's IVs were all
     claimed) get one more pass: the nearest plausible AMOUNT-only guess among
     remaining unclaimed IVs — but never across two KNOWN, DIFFERING products
     (the cross-product steal this rebuild fixes). Labelled ``'review'``, never
     ``'confident'``.
  4. Nothing plausible → left unmatched; no fabricated link.

Manual links are never clobbered and their IV is never reused.
"""
import logging
from collections import deque
from datetime import datetime

# Customer code per platform (sales_transactions.customer_code).
_CUST_CODE = {'shopee': 'Zหน้าร้าน', 'lazada': 'Lหน้าร้าน'}

# The IV is keyed within this many days AFTER the platform order date.
FORWARD_WINDOW_DAYS = 7
PICKER_WINDOW_DAYS = 14          # the manual picker looks a bit further out
# When the product mapping can't confirm a candidate, only trust a near amount.
FUZZY_AMOUNT_TOL = 15.0
# A multi-item order/invoice (a bundle) additionally needs amount corroboration
# to product-match on a partial overlap — band = max(FUZZY_AMOUNT_TOL, this % of
# the payout). Keeps a small single-item order from grabbing a big bundle IV (or
# vice versa) just because they share one product.
_CORROB_PCT = 0.40
# Cost weighting for the min-cost bipartite match: nearest-date is the PRIMARY
# objective, amount a tiebreaker only. Must exceed the largest possible summed
# amount-diff across all edges on a platform so one extra day of gap always
# outweighs any amount saving (verified: ~1,500-2,500 orders/platform, amounts
# in the hundreds of ฿ — nowhere near 1e9 satang). Integer satang avoids float
# drift in the shortest-path search over thousands of edges.
_GAP_WEIGHT = 10 ** 9

# Amount the customer actually paid = VAT-aware net (the idiom used across the
# codebase: models.py · payments_alloc.py · cashflow). vat_type 2 = VAT-exclusive.
_VAT_NET = "CASE WHEN vat_type=2 THEN net*1.07 ELSE net END"


def _signed_gap(iv_date, order_date):
    """Days from order_date to iv_date (positive = IV dated after the order)."""
    if not iv_date or not order_date:
        return None
    di = datetime.strptime(str(iv_date)[:10], '%Y-%m-%d')
    do = datetime.strptime(str(order_date)[:10], '%Y-%m-%d')
    return (di - do).days


def _ivs_for(conn, customer_code):
    """Aggregate every Express IV for one หน้าร้าน customer code, one row per doc_base."""
    rows = conn.execute(
        f"""SELECT doc_base,
                   MIN(date_iso)                  AS date_iso,
                   ROUND(SUM({_VAT_NET}), 2)      AS iv_net,
                   COUNT(*)                       AS line_count
            FROM sales_transactions
            WHERE customer_code = ? AND doc_base LIKE 'IV%'
            GROUP BY doc_base""",
        (customer_code,)
    ).fetchall()
    return [dict(r) for r in rows]


def _iv_products(conn, customer_code):
    """doc_base -> set(product_id) for one หน้าร้าน customer code."""
    out = {}
    for r in conn.execute(
        """SELECT doc_base, product_id FROM sales_transactions
           WHERE customer_code = ? AND doc_base LIKE 'IV%' AND product_id IS NOT NULL""",
        (customer_code,)
    ):
        out.setdefault(r['doc_base'], set()).add(r['product_id'])
    return out


# ── Bucket 3 (2026-07-10 /grilling session): settled cancel/return orders ──
#
# _is_matchable_status excludes the WHOLE cancel/return family forever —
# correct for the ~95% that never got invoiced (cancelled before shipping),
# but Put wants the handful that DID settle (shipped, then returned/lost,
# with a real Express document) linked too. This is a SEPARATE pass, run
# only for settled cancel/return orders, searching BOTH 'IV%' and 'SR%' docs
# (the main pool only ever touches 'IV%', so widening here can't create a
# double-claim risk against it). Real return processing lag can exceed the
# main pool's 7-day forward window (verified up to 9 days on real data), so
# this pass uses a wider one.
_RETURN_WINDOW_DAYS = 30


def _ivs_and_srs_for(conn, customer_code):
    """Like _ivs_for but also includes SR (credit-note) docs."""
    rows = conn.execute(
        f"""SELECT doc_base,
                   MIN(date_iso)                  AS date_iso,
                   ROUND(SUM({_VAT_NET}), 2)      AS iv_net,
                   COUNT(*)                       AS line_count
            FROM sales_transactions
            WHERE customer_code = ? AND (doc_base LIKE 'IV%' OR doc_base LIKE 'SR%')
            GROUP BY doc_base""",
        (customer_code,)
    ).fetchall()
    return [dict(r) for r in rows]


def _iv_and_sr_products(conn, customer_code):
    """doc_base -> set(product_id), IV and SR docs both included."""
    out = {}
    for r in conn.execute(
        """SELECT doc_base, product_id FROM sales_transactions
           WHERE customer_code = ? AND (doc_base LIKE 'IV%' OR doc_base LIKE 'SR%')
             AND product_id IS NOT NULL""",
        (customer_code,)
    ):
        out.setdefault(r['doc_base'], set()).add(r['product_id'])
    return out


def _settled_cancel_return_orders(conn, platform):
    """Settled orders (actual_payout + settled_at both present) whose status
    is in the cancel/return family — the only subset of that family this
    module ever tries to link (see module-level rationale above)."""
    placeholders = ",".join("?" * len(_STATUS_CANCEL_RETURN))
    rows = conn.execute(
        f"""SELECT id, order_sn, platform, status, order_date
            FROM marketplace_orders
            WHERE platform = ? AND status IN ({placeholders})
              AND settled_at IS NOT NULL AND actual_payout IS NOT NULL
            ORDER BY order_date""",
        (platform, *_STATUS_CANCEL_RETURN)
    ).fetchall()
    return list(rows)


def _product_compatible_return(my_prod, docp):
    """Return-only compatibility: no amount signal is trustworthy here (a
    return's actual_payout can be positive, negative, or zero with no
    consistent relationship to a doc's net — verified on real data). Trust
    product identity ONLY. Single<->single is trusted like the main matcher's
    D12; multi-item requires an EXACT product-set match (no partial-overlap +
    amount-band fallback, since there is nothing trustworthy to corroborate
    with)."""
    if not (my_prod & docp):
        return False
    if len(my_prod) <= 1 and len(docp) <= 1:
        return True
    return my_prod == docp


def _build_return_edges(orders, free_docs, doc_prod, o_prod, window_days):
    """Same shape as _build_edges but for the returns pass: no amount
    tiebreak (nothing trustworthy to tiebreak with), nearest-date only."""
    edges_by_order = {}
    for o in orders:
        sn = o['order_sn']
        my_prod = o_prod.get(sn, set())
        if not my_prod:
            continue
        my_edges = []
        for doc in free_docs:
            gap = _signed_gap(doc['date_iso'], o['order_date'])
            if gap is None or gap < 0 or gap > window_days:
                continue
            docp = doc_prod.get(doc['doc_base'], set())
            if not _product_compatible_return(my_prod, docp):
                continue
            my_edges.append((gap, 0, doc['doc_base']))
        if my_edges:
            edges_by_order[sn] = my_edges
    return edges_by_order


def _combo_components(conn):
    """pack product_id -> set(component product_ids), for MULTI-component 'ชุด' packs
    only (e.g. 253 ชุดฝาครอบลูกบิด+กุญแจ = 251 + 252). Read from conversion_formulas:
    the pack is the formula OUTPUT, its components are the inputs. Single-component
    packs (a ตัว/แผง pair, one input) are EXCLUDED — those share a unit and match
    without expansion; expanding them would over-match. Lets a combo marketplace
    order product-match the Express IV, which the team keys as the SEPARATE
    components (253 order ↔ a 251+252 two-line invoice).

    Scoped to is_active=0: a combo marker is stored INACTIVE (prod fid 126) so it
    never appears as a runnable /conversions. is_active=1 formulas are REAL
    manufacturing conversions (assemble one product from parts) and must NOT be
    treated as marketplace combos — expanding one would silently corrupt matching."""
    by_pack = {}
    for r in conn.execute(
        """SELECT f.output_product_id AS pack, i.product_id AS comp
           FROM conversion_formulas f
           JOIN conversion_formula_inputs i ON i.formula_id = f.id
           WHERE i.product_id IS NOT NULL AND f.is_active = 0"""):
        by_pack.setdefault(r['pack'], set()).add(r['comp'])
    return {p: comps for p, comps in by_pack.items() if len(comps) >= 2}


def _generic_standins(conn):
    """variant_product_id -> generic_product_id, from the curated
    product_generic_standins table (mig 134). Some color/size-specific
    products have real, separately-tracked stock but are booked in Express
    under one generic catch-all product instead of the specific variant —
    see Operations/05_analysis-reports/engineering/
    generic-standin-schema-design_2026-07-10.md. Consumed ONLY by Pass 1.5
    below; never joined into any stock/mapping/unit_conversion path (that
    invariant lives in the migration's own header comment too)."""
    return {r['variant_product_id']: r['generic_product_id'] for r in conn.execute(
        "SELECT variant_product_id, generic_product_id FROM product_generic_standins")}


def _apply_standins(prod_set, standins):
    """Replace each pid in ``prod_set`` with its curated generic stand-in, if
    one exists, else leave it unchanged. REPLACE, not union: this is only
    ever called for Pass 1.5, on orders Pass 1 already exhaustively tried (and
    failed) to match with their real product id(s) against the full IV pool —
    nothing is lost by dropping the original id here. Keeping the substituted
    set the SAME SIZE as the original also matters: _product_compatible's
    single<->single "trust regardless of amount" rule (D12) keys off set
    size, so a naive union (which would inflate a 1-product order to size 2)
    would wrongly demote it into the amount-band-corroboration path."""
    return {standins.get(pid, pid) for pid in prod_set}


def _order_products(conn, platform):
    """order_sn -> set(internal_product_id) from the (imperfect) SKU mapping. A combo
    product is REPLACED by its components (see _combo_components) — not unioned in
    alongside the pack id — so a combo order's product set matches EXACTLY what the
    Express IV is keyed as (the separate components; the pack id itself never
    appears on any invoice). This matters beyond overlap: the global matcher's
    multi-item corroboration (_product_compatible) checks product-SET equality, so
    a leftover pack id would make even a perfect combo match (pack -> its own
    2-line component IV) fail equality and need amount corroboration it doesn't
    need."""
    combo = _combo_components(conn)
    out = {}
    for r in conn.execute(
        """SELECT order_sn, internal_product_id FROM marketplace_order_items
           WHERE platform = ? AND internal_product_id IS NOT NULL""",
        (platform,)
    ):
        pid = r['internal_product_id']
        s = out.setdefault(r['order_sn'], set())
        s |= combo.get(pid) or {pid}
    return out


def iv_candidates(conn, order, window_days=PICKER_WINDOW_DAYS, max_results=20):
    """IVs that could be ``order``, for the manual picker — Zหน้าร้าน/Lหน้าร้าน IVs
    dated on/after the platform order date within the window, ranked by
    product-match → amount-closeness → nearest date. Each candidate carries the ฿
    difference from the payout, whether it shares a product (directly or via a
    curated generic stand-in — see _apply_standins), and which order (if any)
    currently holds it.
    """
    code = _CUST_CODE.get(order['platform'])
    d = dict(order)
    basis = d.get('billed_basis', d.get('actual_payout'))
    if code is None or basis is None or not order['order_date']:
        return []
    payout = round(basis, 2)
    linked = {r['doc_base']: r['order_sn'] for r in conn.execute(
        "SELECT doc_base, order_sn FROM marketplace_order_invoice WHERE platform = ?",
        (order['platform'],)).fetchall()}
    iv_prod = _iv_products(conn, code)
    my_prod = _order_products(conn, order['platform']).get(order['order_sn'], set())
    my_prod_standin = _apply_standins(my_prod, _generic_standins(conn))
    out = []
    for iv in _ivs_for(conn, code):
        gap = _signed_gap(iv['date_iso'], order['order_date'])
        if gap is None or gap < 0 or gap > window_days:
            continue
        ivp = iv_prod.get(iv['doc_base'], set())
        direct = bool(my_prod & ivp)
        standin = (not direct) and bool(my_prod_standin & ivp)
        out.append({**iv, 'date_gap': gap, 'product_match': direct or standin,
                    'standin_match': standin,
                    'amount_diff': round((iv['iv_net'] or 0) - payout, 2),
                    'linked_to': linked.get(iv['doc_base'])})
    # product-match first; then AMOUNT-closeness, then nearest date. Amount before
    # date matters for the common sibling-pid order (its listing maps to a sibling
    # of the IV's pid, so NO candidate can be a product-match): the team keys
    # หน้าร้าน IVs at the net payout, so an exact ฿ match is the strongest identity
    # signal and must not be buried behind a nearer-date wrong-amount IV
    # (260701HNJFD30G: IV6901054 gap0/฿-146 vs the right IV6901057 gap1/฿0).
    # Integer satang keeps the primary key free of float drift.
    out.sort(key=lambda x: (0 if x['product_match'] else 1,
                            round(abs(x['amount_diff']) * 100), x['date_gap'], x['doc_base']))
    return out[:max_results]


# STATUS-BASED matchable gate (2026-07-10 relax, replaces the old settled-only
# gate below). An order enters the automatch pool once its lifecycle status
# says the team has (or is about to have) keyed its Express IV — NOT only once
# Sendy has imported a settlement file for it. Before this, a large 2024
# Shopee tranche (สำเร็จแล้ว, keyed by the team long before Sendy ever imported
# a settlement file) was invisible to the matcher forever (see
# projects/express-integration/marketplace-iv-mapping-plan.md, ground truth).
# Every value below is a REAL status seen on prod (verified 2026-07-10) —
# nothing here is speculative.
_STATUS_COMPLETED = {
    'สำเร็จแล้ว', 'จัดส่งสำเร็จแล้ว', 'delivered', 'confirmed',
}
# Shopee appends a dynamic return-window deadline to this one
# ("...จนถึง 2026-07-04") — prefix match, not exact string equality.
_STATUS_COMPLETED_PREFIX = 'ผู้ซื้อได้รับสินค้าแล้ว'
_STATUS_IN_TRANSIT = {'การจัดส่ง', 'shipped'}          # team keys the IV at pack/ship
# 'ready_to_ship' (Lazada, prod-only string, discovered 2026-07-10) is the same
# kind of not-shipped-yet status as ที่ต้องจัดส่ง — classifying it here silences
# the unknown-status warning for it; unsettled behaviour is unchanged (skip).
_STATUS_NOT_SHIPPED = {'ที่ต้องจัดส่ง', 'ready_to_ship'}  # IV may not exist yet — skip
_STATUS_CANCEL_RETURN = {
    'ยกเลิกแล้ว', 'canceled', 'returned', 'Package Returned',
    'Package scrapped', 'Lost by 3PL', 'In Transit: Returning to seller',
}


def _is_matchable_status(status, settled=False):
    """True if an order at this ``status`` should enter the automatch pool.

    ``status`` is a STALE snapshot from the last order-export upload — it can
    lag reality (2026-07-10 regression: settled orders still showing
    ที่ต้องจัดส่ง). ``settled`` (``settled_at`` and ``actual_payout`` both
    present) is stronger, more current evidence that the order shipped, so it
    OVERRIDES a not-shipped-yet or unrecognized status. It does NOT override
    the cancel/return family — those are FINAL states, not stale ones (a
    returned order can settle with a clawback).

    Fail-safe: an unsettled order at a status outside the known inventory
    above is SKIPPED (never guessed) and logged — a platform introducing a
    new status must not silently start (or stop) matching until someone
    classifies it."""
    if status in _STATUS_CANCEL_RETURN:
        return False
    if status in _STATUS_COMPLETED or status in _STATUS_IN_TRANSIT:
        return True
    if status and status.startswith(_STATUS_COMPLETED_PREFIX):
        return True
    if settled:
        return True
    if status in _STATUS_NOT_SHIPPED:
        return False
    logging.getLogger(__name__).warning(
        "marketplace_match: unknown order status %r — skipping (fail-safe)", status)
    return False


def _matchable_orders(conn, platform):
    """Orders eligible for the automatch pool (see ``_is_matchable_status``).
    ``billed_basis`` still prefers the real settlement figure when one exists;
    a matchable-but-unsettled order falls back to ``item_total`` (its pre-fee
    subtotal, populated at order-import time, never settlement time — so it's
    always available even when nothing has settled yet). Lazada's existing
    gross-first COALESCE is untouched (the team keys Lazada IVs at gross, not
    net payout, whenever a fee row exists)."""
    rows = conn.execute(
        """SELECT o.id, o.order_sn, o.platform, o.status, o.actual_payout, o.settled_at,
                  o.order_date,
                  CASE WHEN o.platform='lazada'
                       THEN COALESCE(f.item_value, o.item_total, o.actual_payout)
                       ELSE COALESCE(o.actual_payout, o.item_total) END AS billed_basis
           FROM marketplace_orders o
           LEFT JOIN marketplace_order_fees f
                  ON f.platform=o.platform AND f.order_sn=o.order_sn
           WHERE o.platform = ?
           ORDER BY o.order_date""",
        (platform,)
    ).fetchall()
    return [r for r in rows
            if _is_matchable_status(
                r['status'], settled=r['settled_at'] is not None and r['actual_payout'] is not None)
            and r['billed_basis'] is not None]


def _product_compatible(my_prod, ivp, payout, iv_net):
    """True iff an order (product set ``my_prod``) and an IV (product set ``ivp``,
    net ``iv_net``) are a valid product-matched candidate edge. Single-product
    order AND single-product IV sharing that product need no amount check (D12:
    trusted regardless of ฿ gap). A multi-item order or IV (a bundle) additionally
    needs either an EXACT product-set match, or amount corroboration within a
    sane band — a ฿57 2-item order must not grab a ฿29 1-item invoice on one
    shared product, and a ฿29 1-item order must not grab a ฿57 bundle IV either."""
    if not (my_prod & ivp):
        return False
    if len(my_prod) <= 1 and len(ivp) <= 1:
        return True
    if my_prod == ivp:
        return True
    band = max(FUZZY_AMOUNT_TOL, _CORROB_PCT * payout)
    return abs((iv_net or 0) - payout) <= band


def _build_edges(orders, free_ivs, iv_prod, o_prod, window_days):
    """order_sn -> [(gap, adiff, doc_base), ...] for every valid product+date
    candidate edge (see ``_product_compatible``). Orders with no resolved
    product get no edges here — they fall through to the amount-only guess pass."""
    edges_by_order = {}
    for o in orders:
        sn = o['order_sn']
        my_prod = o_prod.get(sn, set())
        if not my_prod:
            continue
        payout = round(o['billed_basis'], 2)
        my_edges = []
        for iv in free_ivs:
            gap = _signed_gap(iv['date_iso'], o['order_date'])
            if gap is None or gap < 0 or gap > window_days:
                continue
            ivp = iv_prod.get(iv['doc_base'], set())
            if not _product_compatible(my_prod, ivp, payout, iv['iv_net']):
                continue
            adiff = abs((iv['iv_net'] or 0) - payout)
            my_edges.append((gap, adiff, iv['doc_base']))
        if my_edges:
            edges_by_order[sn] = my_edges
    return edges_by_order


def _min_cost_bipartite_match(edges_by_order):
    """Global assignment: maximize the number of matched orders, then minimize
    total cost (date-gap primary, amount secondary — see ``_GAP_WEIGHT``) among
    all maximum matchings. A min-cost max-flow over a bipartite graph with
    unit-capacity edges, solved via successive shortest augmenting paths
    (SPFA/Bellman-Ford — correct here despite the negative-cost reverse residual
    edges the algorithm creates, since always augmenting along the shortest path
    is exactly what keeps a min-cost-flow network free of negative cycles).

    ``edges_by_order``: order_sn -> [(gap, adiff, doc_base), ...].
    Returns order_sn -> doc_base for every matched order.

    Dataset is small and sparse (verified on prod: ~1,500-2,500 orders/platform,
    ~1.6-1.8 candidate edges/order, max ~11) — plain SPFA per augmentation is
    comfortably fast; no need for Dijkstra+potentials.
    """
    order_ids = list(edges_by_order.keys())
    doc_bases = sorted({db for edges in edges_by_order.values() for (_g, _a, db) in edges})
    if not order_ids or not doc_bases:
        return {}
    o_idx = {sn: i for i, sn in enumerate(order_ids)}
    d_idx = {db: i for i, db in enumerate(doc_bases)}
    n_o, n_d = len(order_ids), len(doc_bases)
    SRC, SINK = 0, n_o + n_d + 1
    n_nodes = n_o + n_d + 2

    # adjacency: node -> list of [to, cap, cost, rev_index_in_graph[to]]
    graph = [[] for _ in range(n_nodes)]

    def add_edge(u, v, cap, cost):
        graph[u].append([v, cap, cost, len(graph[v])])
        graph[v].append([u, 0, -cost, len(graph[u]) - 1])

    for sn in order_ids:
        add_edge(SRC, 1 + o_idx[sn], 1, 0)
    for db in doc_bases:
        add_edge(1 + n_o + d_idx[db], SINK, 1, 0)
    for sn, edges in edges_by_order.items():
        u = 1 + o_idx[sn]
        for gap, adiff, db in edges:
            cost = gap * _GAP_WEIGHT + round(abs(adiff) * 100)
            add_edge(u, 1 + n_o + d_idx[db], 1, cost)

    while True:
        dist = [None] * n_nodes
        dist[SRC] = 0
        prev = [None] * n_nodes          # node -> (from_node, edge_index_in_from_node)
        in_queue = [False] * n_nodes
        dq = deque([SRC])
        in_queue[SRC] = True
        while dq:
            u = dq.popleft()
            in_queue[u] = False
            du = dist[u]
            for ei, e in enumerate(graph[u]):
                v, cap, cost, _rev = e
                if cap <= 0:
                    continue
                nd = du + cost
                if dist[v] is None or nd < dist[v]:
                    dist[v] = nd
                    prev[v] = (u, ei)
                    if not in_queue[v]:
                        dq.append(v)
                        in_queue[v] = True
        if dist[SINK] is None:
            break
        v = SINK
        while v != SRC:
            u, ei = prev[v]
            e = graph[u][ei]
            e[1] -= 1
            graph[v][e[3]][1] += 1
            v = u

    match = {}
    for sn in order_ids:
        u = 1 + o_idx[sn]
        for v, cap, _cost, _rev in graph[u]:
            if cap == 0 and 1 + n_o <= v < 1 + n_o + n_d:
                match[sn] = doc_bases[v - (1 + n_o)]
                break
    return match


def _amount_only_guesses(remaining, free_ivs, iv_prod, o_prod, window_days):
    """D13/D14: for orders with no valid product-matched edge, guess the nearest
    plausible unclaimed IV by amount (product ignored) — but NEVER across two
    KNOWN, DIFFERING products (that cross-product steal is the root cause this
    rebuild fixes). Ranked (gap, amount-diff) like the primary pass, greedily
    assigned. Returns order_sn -> doc_base; caller labels these 'review', never
    'confident'."""
    candidates = []
    for o in remaining:
        sn = o['order_sn']
        my_prod = o_prod.get(sn, set())
        payout = round(o['billed_basis'], 2)
        for iv in free_ivs:
            db = iv['doc_base']
            gap = _signed_gap(iv['date_iso'], o['order_date'])
            if gap is None or gap < 0 or gap > window_days:
                continue
            ivp = iv_prod.get(db, set())
            if my_prod and ivp and not (my_prod & ivp):
                continue          # both products known and different — never guess across them
            adiff = abs((iv['iv_net'] or 0) - payout)
            if adiff > FUZZY_AMOUNT_TOL:
                continue
            candidates.append((gap, adiff, sn, db))
    candidates.sort()
    matched, claimed_now = {}, set()
    for gap, adiff, sn, db in candidates:
        if sn in matched or db in claimed_now:
            continue
        matched[sn] = db
        claimed_now.add(db)
    return matched


def run_automatch(conn, platform, window_days=FORWARD_WINDOW_DAYS):
    """Auto-link matchable ``platform`` orders (see ``_is_matchable_status`` —
    status-based, not settled-only) to their Express IV — a single GLOBAL
    assignment (see module docstring), not a greedy per-order pass.
    Rebuilds all 'auto' rows on every call (idempotent); never touches 'manual'
    rows nor reuses a manually-held IV.

    ALSO links settled cancel/return orders to their Express document (IV or
    SR) when one exists — see the bucket-3 section above _settled_cancel_
    return_orders. That is a small, separate, no-amount-guessing pass; its
    successes/failures are reported under ``returns_matched`` only, never
    folded into ``unmatched`` (being unmatched is the CORRECT, expected
    outcome for most cancel/return orders, not a problem to flag).

    ALSO retries (Pass 1.5, between Pass 1 and Pass 2) orders Pass 1 couldn't
    place using each product's curated generic stand-in (see
    _generic_standins / product_generic_standins, mig 134) — some color/size
    variants are booked in Express under one generic catch-all product.
    Placed before the amount-only guess since a curated product match is a
    stronger signal than a bare amount coincidence.

    Returns {matched, confident, review, unmatched, returns_matched}.
    """
    code = _CUST_CODE[platform]
    orders = _matchable_orders(conn, platform)
    ivs = _ivs_for(conn, code)
    iv_prod = _iv_products(conn, code)
    o_prod = _order_products(conn, platform)

    manual_rows = conn.execute(
        "SELECT order_sn, doc_base FROM marketplace_order_invoice WHERE platform=? AND match_method='manual'",
        (platform,)).fetchall()
    manual_orders = {r['order_sn'] for r in manual_rows}
    claimed = {r['doc_base'] for r in manual_rows}   # manually-held IVs are off-limits

    conn.execute(
        "DELETE FROM marketplace_order_invoice WHERE platform=? AND match_method='auto'",
        (platform,))

    auto_orders = [o for o in orders if o['order_sn'] not in manual_orders]
    free_ivs = [iv for iv in ivs if iv['doc_base'] not in claimed]

    # Pass 1: global product-matched assignment — every result is 'confident'.
    edges = _build_edges(auto_orders, free_ivs, iv_prod, o_prod, window_days)
    product_match = _min_cost_bipartite_match(edges)

    results = {}  # order_sn -> (doc_base, confidence)
    for sn, doc_base in product_match.items():
        results[sn] = (doc_base, 'confident')
        claimed.add(doc_base)

    # Pass 1.5: retry orders Pass 1 left unmatched, substituting each
    # product's curated generic stand-in (see _apply_standins — replace, not
    # union). Still 'confident': a curated equivalence + product identity +
    # valid date is the same trust bar as Pass 1 (see module docstring).
    standins = _generic_standins(conn)
    remaining_15 = [o for o in auto_orders if o['order_sn'] not in results]
    free_ivs_15 = [iv for iv in free_ivs if iv['doc_base'] not in claimed]
    o_prod_standin = {o['order_sn']: _apply_standins(o_prod.get(o['order_sn'], set()), standins)
                       for o in remaining_15}
    edges_15 = _build_edges(remaining_15, free_ivs_15, iv_prod, o_prod_standin, window_days)
    standin_match = _min_cost_bipartite_match(edges_15)
    for sn, doc_base in standin_match.items():
        results[sn] = (doc_base, 'confident')
        claimed.add(doc_base)

    # Pass 2: amount-only guesses for whatever's left — always 'review'.
    remaining = [o for o in auto_orders if o['order_sn'] not in results]
    free_ivs2 = [iv for iv in free_ivs if iv['doc_base'] not in claimed]
    guesses = _amount_only_guesses(remaining, free_ivs2, iv_prod, o_prod, window_days)
    for sn, doc_base in guesses.items():
        results[sn] = (doc_base, 'review')
        claimed.add(doc_base)

    # Pass 3 (bucket 3): settled cancel/return orders -> IV or SR, product-only,
    # no guessing. Separate order pool, separate claim coordination (still
    # respects `claimed` so it can never steal a doc the passes above took).
    return_orders_all = _settled_cancel_return_orders(conn, platform)
    return_orders = [o for o in return_orders_all if o['order_sn'] not in manual_orders
                      and o['order_sn'] not in results]
    return_docs = _ivs_and_srs_for(conn, code)
    return_doc_prod = _iv_and_sr_products(conn, code)
    free_return_docs = [d for d in return_docs if d['doc_base'] not in claimed]
    return_edges = _build_return_edges(return_orders, free_return_docs, return_doc_prod,
                                        o_prod, _RETURN_WINDOW_DAYS)
    return_match = _min_cost_bipartite_match(return_edges)
    returns_matched = 0
    for sn, doc_base in return_match.items():
        results[sn] = (doc_base, 'confident')
        claimed.add(doc_base)
        returns_matched += 1

    confident = review = 0
    for sn, (doc_base, confidence) in results.items():
        conn.execute(
            """INSERT INTO marketplace_order_invoice
                   (platform, order_sn, doc_base, customer_code, match_method, confidence)
               VALUES (?,?,?,?, 'auto', ?)""",
            (platform, sn, doc_base, code, confidence))
        if confidence == 'confident':
            confident += 1
        else:
            review += 1
    unmatched = sum(1 for o in auto_orders if o['order_sn'] not in results)

    conn.commit()
    return {'matched': confident + review, 'confident': confident,
            'review': review, 'unmatched': unmatched, 'returns_matched': returns_matched}


def link_manual(conn, platform, order_sn, doc_base, customer_code=None, confirmed_by=None):
    """Record a human-confirmed link. One IV = one order: if another order already
    holds this IV, it is unlinked (stolen) and reverts to needing a pick. Returns
    the list of order_sns that lost this IV (for a flash message)."""
    if customer_code is None:
        customer_code = _CUST_CODE.get(platform)
    stolen = [r['order_sn'] for r in conn.execute(
        "SELECT order_sn FROM marketplace_order_invoice WHERE platform=? AND doc_base=? AND order_sn<>?",
        (platform, doc_base, order_sn)).fetchall()]
    if stolen:
        conn.execute(
            "DELETE FROM marketplace_order_invoice WHERE platform=? AND doc_base=? AND order_sn<>?",
            (platform, doc_base, order_sn))
    conn.execute(
        """INSERT INTO marketplace_order_invoice
               (platform, order_sn, doc_base, customer_code, match_method, confidence,
                confirmed_by, confirmed_at)
           VALUES (?,?,?,?, 'manual', 'manual', ?, datetime('now','localtime'))
           ON CONFLICT(platform, order_sn) DO UPDATE SET
               doc_base      = excluded.doc_base,
               customer_code = excluded.customer_code,
               match_method  = 'manual',
               confidence    = 'manual',
               confirmed_by  = excluded.confirmed_by,
               confirmed_at  = excluded.confirmed_at""",
        (platform, order_sn, doc_base, customer_code, confirmed_by))
    conn.commit()
    return stolen
