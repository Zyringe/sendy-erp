"""Link marketplace orders (Shopee/Lazada) to their Express invoice (IV).

The team books each marketplace order as ONE Express invoice under customer codes
``Zหน้าร้าน`` (Shopee) / ``Lหน้าร้าน`` (Lazada), keyed in Express shortly AFTER the
order is placed on the platform. There is no stored order_sn↔IV key, so we match
on three signals (strongest first):

  1. product overlap — the order's line items (resolved to internal product_ids via
     the marketplace SKU mapping) vs the IV's product_ids. A shared product is the
     strongest identity signal, robust to the amount noise below.
  2. date — the IV is dated on/after the platform order date, almost always within a
     day or two (IV.date_iso ≥ order_date; nearest-first). An IV dated BEFORE the
     order can't be that order's invoice.
  3. amount — IV (VAT-aware) net vs the Shopee payout. Usually equal, but Shopee
     often ADJUSTS the payout after the order (e.g. 246 shown → 236 paid), so a
     different amount is a real discrepancy to surface, not a reason to reject.

``run_automatch`` greedily assigns each order (oldest first) its lowest-cost
unclaimed IV in the forward window. Labels 'confident' when the matched IV's amount
equals the payout, else 'review' (billed ≠ payout — a discrepancy for the team to
fix in Express). Manual links are never clobbered and their IV is never reused.
"""
from datetime import datetime

# Customer code per platform (sales_transactions.customer_code).
_CUST_CODE = {'shopee': 'Zหน้าร้าน', 'lazada': 'Lหน้าร้าน'}

# The IV is keyed within this many days AFTER the platform order date.
FORWARD_WINDOW_DAYS = 7
PICKER_WINDOW_DAYS = 14          # the manual picker looks a bit further out
_AMOUNT_TOL = 0.005              # amounts within half a satang are "equal"
# When the product mapping can't confirm a candidate, only trust a near amount.
FUZZY_AMOUNT_TOL = 15.0

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


def _order_products(conn, platform):
    """order_sn -> set(internal_product_id) from the (imperfect) SKU mapping."""
    out = {}
    for r in conn.execute(
        """SELECT order_sn, internal_product_id FROM marketplace_order_items
           WHERE platform = ? AND internal_product_id IS NOT NULL""",
        (platform,)
    ):
        out.setdefault(r['order_sn'], set()).add(r['internal_product_id'])
    return out


def iv_candidates(conn, order, window_days=PICKER_WINDOW_DAYS, max_results=20):
    """IVs that could be ``order``, for the manual picker — Zหน้าร้าน/Lหน้าร้าน IVs
    dated on/after the platform order date within the window, ranked by
    product-match → nearest date → amount-closeness. Each candidate carries the ฿
    difference from the payout, whether it shares a product, and which order (if
    any) currently holds it.
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
    out = []
    for iv in _ivs_for(conn, code):
        gap = _signed_gap(iv['date_iso'], order['order_date'])
        if gap is None or gap < 0 or gap > window_days:
            continue
        overlap = len(my_prod & iv_prod.get(iv['doc_base'], set()))
        out.append({**iv, 'date_gap': gap, 'product_match': overlap > 0,
                    'amount_diff': round((iv['iv_net'] or 0) - payout, 2),
                    'linked_to': linked.get(iv['doc_base'])})
    out.sort(key=lambda x: (0 if x['product_match'] else 1, x['date_gap'],
                            abs(x['amount_diff']), x['doc_base']))
    return out[:max_results]


def _settled_orders(conn, platform):
    return conn.execute(
        """SELECT o.id, o.order_sn, o.platform, o.actual_payout, o.settled_at, o.order_date,
                  CASE WHEN o.platform='lazada'
                       THEN COALESCE(f.item_value, o.item_total, o.actual_payout)
                       ELSE o.actual_payout END AS billed_basis
           FROM marketplace_orders o
           LEFT JOIN marketplace_order_fees f
                  ON f.platform=o.platform AND f.order_sn=o.order_sn
           WHERE o.platform = ? AND o.settled_at IS NOT NULL AND o.actual_payout IS NOT NULL
           ORDER BY o.order_date""",
        (platform,)
    ).fetchall()


def _pick_iv(o, ivs, claimed, iv_prod, o_prod, window_days, mode):
    """Lowest-cost unclaimed valid IV for one order, or None.

    Three modes, run in this order so a product match is never displaced by an
    amount coincidence (greedy-oldest-first otherwise lets an older amount-only
    order grab an IV that a later product-overlap order needs):
      'exact'   (pass 1): shared product AND exact amount (adiff < _AMOUNT_TOL).
      'product' (pass 2): shared product, ANY amount. Locks every product-overlap
          match before the amount-only fallback — the fee-divergent product match
          (Lazada payout < IV net) that the exact pass can't catch. Without this,
          an unrelated near-amount order steals it (the 913281 case).
      'amount'  (pass 3): near amount (adiff <= FUZZY_AMOUNT_TOL), product optional
          — the fallback for orders left with no unclaimed product IV.
    The union of eligible matches is unchanged from the old two-pass rule
    (product OR near-amount); only the assignment PRIORITY changes.
    """
    payout = round(o['billed_basis'], 2)
    my_prod = o_prod.get(o['order_sn'], set())
    best = None  # (cost_tuple, doc_base, amount_diff)
    for iv in ivs:
        if iv['doc_base'] in claimed:
            continue
        gap = _signed_gap(iv['date_iso'], o['order_date'])
        if gap is None or gap < 0 or gap > window_days:
            continue
        overlap = len(my_prod & iv_prod.get(iv['doc_base'], set()))
        adiff = abs((iv['iv_net'] or 0) - payout)
        if mode == 'exact':
            if overlap == 0 or adiff >= _AMOUNT_TOL:
                continue
        elif mode == 'product':
            if overlap == 0:
                continue
        else:  # 'amount' — the fuzzy fallback; a near amount can stand alone
            if adiff > FUZZY_AMOUNT_TOL:
                continue
        cost = (0 if overlap else 1, gap, adiff)
        if best is None or cost < best[0]:
            best = (cost, iv['doc_base'], adiff)
    return best


def run_automatch(conn, platform, window_days=FORWARD_WINDOW_DAYS):
    """Auto-link settled ``platform`` orders to their Express IV by product + date
    (after the order) + amount. THREE passes, each greedy oldest-order-first:
    'exact' (shared-product AND exact-amount) → 'product' (shared-product, any
    amount) → 'amount' (near-amount fallback). Assigning all product-overlap
    matches before the amount-only fallback stops an amount coincidence from
    stealing an IV that is another order's product match (incl. the fee-divergent
    product match the exact pass can't lock). Rebuilds all 'auto' rows; never
    touches 'manual' rows nor reuses a manually-held IV. Labels 'confident'
    (amount == payout) or 'review' (amount differs).
    Returns {matched, confident, review, unmatched}.
    """
    code = _CUST_CODE[platform]
    orders = _settled_orders(conn, platform)        # oldest order_date first
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

    results = {}  # order_sn -> (doc_base, adiff)
    for mode in ('exact', 'product', 'amount'):
        for o in orders:
            sn = o['order_sn']
            if sn in manual_orders or sn in results:
                continue
            best = _pick_iv(o, ivs, claimed, iv_prod, o_prod, window_days, mode)
            if best is None:
                continue
            _cost, doc_base, adiff = best
            claimed.add(doc_base)
            results[sn] = (doc_base, adiff)

    confident = review = 0
    for sn, (doc_base, adiff) in results.items():
        confidence = 'confident' if adiff < _AMOUNT_TOL else 'review'
        conn.execute(
            """INSERT INTO marketplace_order_invoice
                   (platform, order_sn, doc_base, customer_code, match_method, confidence)
               VALUES (?,?,?,?, 'auto', ?)""",
            (platform, sn, doc_base, code, confidence))
        if confidence == 'confident':
            confident += 1
        else:
            review += 1
    unmatched = sum(1 for o in orders
                    if o['order_sn'] not in manual_orders and o['order_sn'] not in results)

    conn.commit()
    return {'matched': confident + review, 'confident': confident,
            'review': review, 'unmatched': unmatched}


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
