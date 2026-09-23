"""Customers + regions + BSN customer-master import + geocode — extracted
verbatim from models.py (behavior-preserving split, Phase 11) — see
models/__init__.py's module docstring for the overall file-split
rationale. No behavior changes.
"""
import json
import customer_geo
import sales_filters
import vat_math
from database import get_connection


def _customer_sales_scope(key_col, key_value, date_from, date_to):
    """WHERE clause + params selecting one customer's sales rows.

    `key_col` is `customer` (the bill name) or `customer_code` (the master code)
    — a literal chosen by the caller at the call site, never user input, so it is
    safe to interpolate. `key_value` stays a bound parameter.
    """
    conds = [f'{key_col} = ?']
    params = [key_value]
    if date_from:
        conds.append('date_iso >= ?'); params.append(date_from)
    if date_to:
        conds.append('date_iso <= ?'); params.append(date_to)
    # Documents invoiced in error are not purchases — วรสวัสดิ์ never bought the
    # giveaway, so it must not show on their page either (see sales_filters).
    conds.append(sales_filters.not_a_sale_clause())
    return ' AND '.join(conds), params


def _customer_documents(conn, where, params, limit=None):
    """One row per DOCUMENT (doc_base), never per line — the shared grouping
    #493 introduced. `sales_transactions.doc_no` carries a per-line '-N'
    suffix, so grouping by it (the pre-#493 code) produced one "document" per
    LINE: 247 lines on customer 23ท06 read as 247 documents when the real
    count is 66. `_customer_sales_aggregates`'s docs list and the mobile quick
    page (models.get_customer_documents) both call this now, so they cannot
    drift apart again.

    Each row: doc_base, date_iso (latest line date on the doc), item_count
    ('รายการ' — the line count, not a quantity summed across units),
    vat_type (of the doc's lines; ยกเว้น/ไม่บวก VAT are never mixed with แยก
    VAT on one document in practice), total (ยอดรวมเอกสาร — VAT added through
    vat_math for แยก VAT documents, summed; NEGATIVE for a credit note),
    is_credit_note, ref_invoice (the invoice an SR credits, NULL otherwise).
    """
    limit_sql = f'LIMIT {int(limit)}' if limit is not None else ''
    rows = conn.execute(f"""
        SELECT doc_base,
               MAX(date_iso) AS date_iso,
               COUNT(*) AS item_count,
               MAX(vat_type) AS vat_type,
               SUM({vat_math.cash_sql()}) AS raw_total,
               (doc_base LIKE 'SR%') AS is_credit_note,
               MAX(ref_invoice) AS ref_invoice
        FROM sales_transactions
        WHERE {where}
        GROUP BY doc_base
        ORDER BY date_iso DESC, doc_base
        {limit_sql}
    """, params).fetchall()
    docs = []
    for r in rows:
        d = dict(r)
        total = d.pop('raw_total') or 0
        d['is_credit_note'] = bool(d['is_credit_note'])
        d['total'] = -total if d['is_credit_note'] else total
        docs.append(d)
    return docs


def get_customer_documents(key_col, key_value, date_from=None, date_to=None, limit=None):
    """Public wrapper around `_customer_documents` — used directly by the
    mobile quick page (code-keyed), so its document list can never drift from
    the desktop customer page's (#493)."""
    conn = get_connection()
    where, params = _customer_sales_scope(key_col, key_value, date_from, date_to)
    docs = _customer_documents(conn, where, params, limit=limit)
    conn.close()
    return docs


def _customer_sales_aggregates(conn, where, params):
    """The four per-customer sales aggregates, defined ONCE.

    The name-keyed and code-keyed summaries below differ only in their WHERE, and
    two copies of these queries drift invisibly — the same reason `sales_filters`
    and `commission_attribution` exist as single definitions.

    `total_net` in summary and monthly is ยอดซื้อรวม: before VAT, credit notes
    subtracted (sales_filters.purchase_net_sql, #494). The summary's
    `total_qty` (จำนวนชิ้น) follows the same rule (sales_qty_sql, #627).

    Returns (summary, top_products, monthly, docs).
    """
    import price_lookup

    summary = dict(conn.execute(f"""
        SELECT COUNT(DISTINCT doc_base) AS doc_count,
               COALESCE(SUM({sales_filters.purchase_net_sql()}), 0) AS total_net,
               COALESCE(SUM({sales_filters.sales_qty_sql()}), 0) AS total_qty,
               MIN(date_iso)          AS first_date,
               MAX(date_iso)          AS last_date
        FROM sales_transactions
        WHERE {where}
    """, params).fetchone())

    # The PURCHASE population (#493, #513): evidence-filtered lines only — never
    # a credit note (SR), a document invoiced in error, or a free/zero-net line.
    # ONE query answers both figures, so the count and the date can never come
    # to describe different sets of documents: the newest of the documents
    # `purchase_doc_count` counts IS `last_purchase_date`, which is what the
    # call card prints side by side.
    #
    # Deliberately NOT the same thing as `doc_count`/`last_date` above, which
    # stay raw: the customer page labels them จำนวนเอกสาร and the ช่วงเวลา range
    # end, and a credit note IS a document and IS activity. Only a surface that
    # says ซื้อ ("bought") may read these two.
    purchases = conn.execute(f"""
        SELECT COUNT(DISTINCT doc_base) AS n,
               MAX(date_iso)            AS d
        FROM sales_transactions
        WHERE {where} AND {price_lookup.purchase_population_filter('')}
    """, params).fetchone()
    summary['last_purchase_date'] = purchases['d']
    summary['purchase_doc_count'] = purchases['n']

    # Money-ordered NET of returns (#627), like the trade screens: the call
    # card's แบรนด์เด่น reads the first name. A MAPPED line groups on
    # product_id alone — a credit note prints `product_name_raw` differently
    # from the invoice it reverses often enough that keeping it in the key
    # left the return as its own negative row (#627 review). An UNMAPPED line
    # has no product to group on and keeps the raw name.
    top_products = conn.execute(f"""
        SELECT COALESCE(p.product_name, s.product_name_raw) AS name,
               p.id AS product_id,
               s.unit,
               SUM({sales_filters.sales_qty_sql('s')}) AS total_qty,
               SUM({sales_filters.sales_net_sql('s')}) AS total_net,
               COUNT(DISTINCT s.doc_base) AS doc_count
        FROM sales_transactions s
        LEFT JOIN products p ON p.id = s.product_id
        WHERE {where}
        GROUP BY s.product_id,
                 CASE WHEN s.product_id IS NULL THEN s.product_name_raw END
        ORDER BY total_net DESC
        LIMIT 20
    """, params).fetchall()

    monthly = conn.execute(f"""
        SELECT strftime('%Y-%m', date_iso) AS month,
               COUNT(DISTINCT doc_base) AS doc_count,
               SUM({sales_filters.purchase_net_sql()}) AS total_net
        FROM sales_transactions
        WHERE {where}
        GROUP BY month
        ORDER BY month
    """, params).fetchall()

    # Every document (#493 — no 200-LINE cap cutting off old invoices; the old
    # cap was on `doc_no`, i.e. lines, so it silently dropped whole invoices).
    docs = _customer_documents(conn, where, params)

    return summary, top_products, monthly, docs


def get_customer_summary(customer, date_from=None, date_to=None):
    """
    Returns summary + top products + monthly trend for a specific customer.

    Keyed on the BILL name. One bill name can span >1 physical company (BUG 2 —
    ทรัพย์ทวี), so this merges them; `get_customer_summary_by_code` is the
    unambiguous form and is what the customer page uses. This one survives for
    `call_card.py`, which only ever has a name.
    """
    conn = get_connection()
    where, params = _customer_sales_scope('customer', customer, date_from, date_to)
    summary, top_products, monthly, docs = _customer_sales_aggregates(
        conn, where, params)

    # Pull salesperson from customers MASTER (post-D1 view migration).
    # 3-way fallback: salespersons.name → customers.salesperson code → '(ไม่กำหนด)'.
    master_row = conn.execute("""
        SELECT s.customer_code,
               c.code AS master_code, c.name AS master_name,
               c.salesperson AS sp_code,
               sp.name AS sp_name, sp.is_active AS sp_active
        FROM sales_transactions s
        LEFT JOIN customers     c  ON c.code  = s.customer_code
        LEFT JOIN salespersons  sp ON sp.code = c.salesperson
        WHERE s.customer = ?
        LIMIT 1
    """, [customer]).fetchone()

    customer_info = None
    customer_code = None
    salesperson_code = None
    salesperson_display = None
    salesperson_orphan = False
    region_display = None

    if master_row:
        customer_code = master_row['customer_code']
        if master_row['master_code']:
            row = conn.execute(
                "SELECT * FROM customers WHERE code=?", [master_row['master_code']]
            ).fetchone()
            if row:
                customer_info = dict(row)
                # #528: ภาค from the address, same source the call card and
                # every other surface now uses (customer_geo.region_of) —
                # retires the last remaining เขตการขาย FK read in this file.
                region_display = customer_geo.region_of(row['address'])
            salesperson_code = master_row['sp_code']
            if salesperson_code:
                if master_row['sp_name']:
                    salesperson_display = master_row['sp_name']
                else:
                    salesperson_display = salesperson_code
                    salesperson_orphan = True

    conn.close()
    return {
        'customer': customer,
        'customer_code': customer_code,
        'region': region_display,
        'salesperson': salesperson_display,
        'salesperson_code': salesperson_code,
        'salesperson_orphan': salesperson_orphan,
        'customer_info': customer_info,
        'date_from': date_from,
        'date_to': date_to,
        'summary': dict(summary),
        'top_products': [dict(r) for r in top_products],
        'monthly': [dict(r) for r in monthly],
        'docs': [dict(r) for r in docs],
    }


def resolve_customer_codes(name):
    """Bill names can span >1 physical company (BUG 2, 2026-08 grilling —
    e.g. 'ทรัพย์ทวี' is both 43ท013 'ร้าน ทรัพย์ทวี' and 01พ14 'บจก. พงศ์ทรัพย์ทวี').
    Callers must never silently pick one — this just reports what's there.
    """
    conn = get_connection()
    rows = conn.execute("""
        SELECT DISTINCT customer_code
        FROM sales_transactions
        WHERE customer = ? AND customer_code IS NOT NULL
        ORDER BY customer_code
    """, [name]).fetchall()
    conn.close()
    return [r['customer_code'] for r in rows]


def _card_cost(conn, pid, unit, last_row, freebie_rows, resolved):
    """The cost block for one product card (#493 slice 3). Only ever called
    when the route asked for cost, i.e. for admin/manager — every other
    role's page data never carries any of this.

    READS ONLY. `models.get_cost_history` (the product page's loader)
    lazily recalculates the ledger and COMMITS, so it is never called here;
    the latest PURCHASE event is read straight from product_cost_ledger.

    Put's two rulings (2026-09-11):
      * the last price's badges judge the money we KEEP — net ÷ qty, ex-VAT
        (on a แยก VAT bill the displayed price includes the 7% we remit);
      * the margin at today's price is the resolver's own internal margin,
        from a call made at the customer's LAST quantity so a bundle's free
        units count — the caller made that call and passes `resolved`.

    Unit conversions go through price_lookup._bill_ratio — the resolver's
    own bill-unit → base-unit lookup, the one it converts every evidence
    row with — never a re-typed unit_conversions query. A ratio it cannot
    find degrades the figure to None ("—"), never to 1.

    "ไม่มีทุน" (cost_price <= 0) means no WACC, no margin and no 🔴 — but a
    real PURCHASE in the ledger still shows as ทุนซื้อล่าสุด with its ⚠
    badges (Put, 2026-09-11): 21 products on prod sit exactly there, and
    for them it is the only cost figure there is.
    """
    import price_lookup

    prod = conn.execute(
        "SELECT cost_price, unit_type FROM products WHERE id = ?", (pid,)).fetchone()
    cost = prod['cost_price'] or 0
    has_cost = cost > 0
    out = {
        'has_cost': has_cost, 'wacc_per_unit': None, 'last_purchase': None,
        'margin_last': None, 'margin_today': None,
        'last_below_wacc': False, 'last_below_last_purchase': False,
        'today_below_wacc': False, 'today_below_last_purchase': False,
    }

    ratio_cache = {}
    row_ratio = price_lookup._bill_ratio(conn, pid, prod['unit_type'], unit, ratio_cache)
    lp = conn.execute("""
        SELECT unit_cost, event_date, reference_no FROM product_cost_ledger
        WHERE product_id = ? AND event_type = 'PURCHASE'
        ORDER BY event_date DESC, id DESC LIMIT 1
    """, (pid,)).fetchone()

    # Per-unit costs are rounded to 2 decimals FIRST and everything below
    # multiplies the rounded figure — the resolver's own rule (price_lookup:
    # cost_per_unit = round(cost * ratio, 2)). The reveal prints "ทุน 55.51
    # × 12", so 666.12 is what must be subtracted, not 55.5061 × 12 = 666.07
    # (seen on real data, 38จ01). Both margins on a row then use one method.
    wacc_pu = round(cost * row_ratio, 2) if has_cost and row_ratio is not None else None
    lp_pu = (round(lp['unit_cost'] * row_ratio, 2)
             if lp is not None and row_ratio is not None else None)
    out['wacc_per_unit'] = wacc_pu
    if lp is not None:
        out['last_purchase'] = {
            'per_unit': lp_pu,
            'date': lp['event_date'],
            'ref': lp['reference_no'],
        }

    # #527: ที่ทุนใหม่ — both margins ALSO get a "at replacement cost" reading
    # when the last purchase cost is above WACC. Judged at the figures as
    # printed (wacc_pu / lp_pu, both already rounded 2dp), same as every
    # other badge on this card.
    above_wacc = has_cost and lp_pu is not None and lp_pu > wacc_pu
    out['last_purchase_above_wacc'] = above_wacc
    # The cost column's "↑gap%" beside ทุนซื้อล่าสุด. None when the WACC
    # prints as 0.00 (a real cost below half a satang per row unit): above,
    # but there is no percentage of zero to show.
    out['last_purchase_gap_pct'] = (round((lp_pu - wacc_pu) / wacc_pu * 100, 1)
                                    if above_wacc and wacc_pu > 0 else None)

    if last_row is not None and row_ratio is not None:
        kept = last_row['net']
        # Badges judge the figures at the precision they print: 181.6875 kept
        # against 181.69 must not read "181.69 🔴 ต่ำกว่าทุน" beside
        # "ทุนเฉลี่ย 181.69" (seen on real rows).
        kept_per_unit = round(kept / last_row['qty'], 2)
        if lp_pu is not None:
            out['last_below_last_purchase'] = kept_per_unit < lp_pu
        free_ratios = [price_lookup._bill_ratio(conn, pid, prod['unit_type'], f['unit'], ratio_cache)
                       for f in freebie_rows]
        if has_cost:
            out['last_below_wacc'] = kept_per_unit < wacc_pu
        if has_cost and None not in free_ratios:
            paid_cost = wacc_pu * last_row['qty']
            free_cost = sum(round(cost * r, 2) * f['qty'] for f, r in zip(freebie_rows, free_ratios))
            profit = kept - paid_cost - free_cost
            out['margin_last'] = {
                'kept': round(kept, 2),
                'paid_qty': last_row['qty'],
                'wacc_per_unit': wacc_pu,
                'paid_cost': round(paid_cost, 2),
                'free_cost': round(free_cost, 2),
                'profit': round(profit, 2),
                'pct': round(profit / kept * 100, 2) + 0.0,   # + 0.0: -0.0 would print "-0.00%"
            }
            if above_wacc:
                # Same shape, priced at ทุนซื้อล่าสุด instead of ทุนเฉลี่ย —
                # freebies too, so a bundle-heavy row doesn't understate the
                # replacement-cost margin.
                free_cost_at_lp = sum(round(lp['unit_cost'] * r, 2) * f['qty']
                                       for f, r in zip(freebie_rows, free_ratios))
                profit_at_lp = kept - lp_pu * last_row['qty'] - free_cost_at_lp
                out['margin_last']['at_new_cost_pct'] = round(profit_at_lp / kept * 100, 2) + 0.0

    if resolved is not None and resolved['list']['list_for_unit'] != 0:
        internal = resolved['internal']
        price = resolved['answer']['price_per_unit']
        if has_cost and internal['margin_at_answer_pct'] is not None:
            out['margin_today'] = {
                'pct': internal['margin_at_answer_pct'] + 0.0,
                'price': price,
                'unit': resolved['answer']['unit'],
                'cost_per_unit': internal['cost_per_unit'],
                'cost_side': internal['cost_side'],
                'incl_free_units': internal['margin_incl_free_units'],
            }
            if above_wacc:
                # #527: same today's-bundle-deal, priced at ทุนซื้อล่าสุด —
                # reuses the resolver's own cost_mult so the buy-N-get-M
                # rule is never re-derived here.
                new_cost_side = round(lp_pu * internal['cost_mult'], 2)
                out['margin_today']['at_new_cost_pct'] = (
                    round((price - new_cost_side) / price * 100, 2) + 0.0)
        out['today_below_wacc'] = has_cost and internal['below_cost_by'] is not None
        ratio = resolved['unit']['ratio']
        if lp is not None and ratio is not None:
            out['today_below_last_purchase'] = price < round(lp['unit_cost'] * ratio, 2)
    return out


def _customer_product_cards(conn, where, params, include_cost=False):
    """สินค้าที่ซื้อบ่อย, enriched (#493 slice 2, trimmed scope B): one row per
    (product, unit), keyed and populated by the price resolver's evidence
    predicate (the one definition of "a bill that counts") restricted to
    this customer — same population `resolve_price`'s customer.last uses.

    #646 (Put, 2026-09-23): the MONEY and QUANTITY are net of credit notes,
    the way the header above these cards has been since #494/#627. `times_bought`
    and `last` stay on the purchase population alone — a credit note is not a
    purchase, but the invoice it reverses still is (Put, 2026-09-17). The card
    also carries `returned_qty`/`returned_net` so the template can say a return
    happened rather than silently shrinking a number nobody prints; `total_qty`
    is rendered nowhere and `total_net` only drives the ยอด sort, so without
    the badge this fix would be invisible on the page.
    `HAVING times_bought > 0` is what keeps a return-only (product, unit) from
    becoming a negative card: prod 2026-09-23 holds 82 credit-note lines whose
    customer has no invoice line for that product at all, and a
    "สินค้าที่ซื้อบ่อย" entry for something they never bought is worse than
    the gap it would close.

    ⚠ It also drops a return booked in a DIFFERENT unit from the purchase it
    reverses, because the key is (product, unit). Prod 2026-09-23: 4 lines,
    ฿2,866.72, 3 customers, every one bought by the โหล and returned by the
    piece, against 114 countable credit-note lines worth ฿164,222.77. Netting
    those needs the product's unit_conversions ratio and would print a
    fractional โหล on a card labelled โหล, so it is Put's call, not an
    inference. Pinned by test_646_card_returns.py::
    test_a_return_in_a_different_unit_does_not_net_and_says_nothing.

    ADDITIVE, not a replacement for `top_products`: the call card
    (`call_card.py::get_card` → `get_customer_summary`, name-keyed) reads
    `top_products[0].name` as "แบรนด์เด่น", money-ordered — changing that
    query's shape or order would silently change what the call card shows.
    This is its own query, its own field, wired only into the code-keyed
    page get_customer_summary_by_code renders.

    Round 2 (Put, 2026-09-11 — finishing the deferred remainder): the union
    of both top-20 orderings (times bought, and money — the template's
    ครั้ง/ยอด toggle just re-sorts this ONE rendered set client-side, it
    never re-fetches), the stale-note (reusing price_lookup's own
    price-regime epochs, the call card's mechanism), the stock badge, and
    on `last`: vat_type/unit_price/discount for the VAT note, the
    bill-level discount (net vs total), and same-product freebies on that
    document.

    Slice 3: `include_cost=True` (the route passes it for admin/manager
    only) adds a `cost` key to every card — see _card_cost. With the
    default False the key is absent entirely, so nothing about cost can
    reach a staff page, not even through a data attribute.
    """
    import price_lookup
    import vat_math
    import invoice_formula

    rows = [dict(r) for r in conn.execute(f"""
        SELECT s.product_id, COALESCE(p.product_name, s.product_name_raw) AS name,
               s.unit,
               COUNT(DISTINCT CASE WHEN {price_lookup.purchase_population_filter('s')}
                                   THEN s.doc_base END) AS times_bought,
               COALESCE(SUM(CASE WHEN {price_lookup.returned_lines_filter('s')}
                                 THEN s.qty ELSE 0 END), 0) AS returned_qty,
               COALESCE(SUM(CASE WHEN {price_lookup.returned_lines_filter('s')}
                                 THEN s.net ELSE 0 END), 0) AS returned_net,
               SUM(CASE WHEN {price_lookup.returned_lines_filter('s')}
                        THEN -s.qty ELSE s.qty END) AS total_qty,
               SUM(CASE WHEN {price_lookup.returned_lines_filter('s')}
                        THEN -s.net ELSE s.net END) AS total_net
        FROM sales_transactions s
        LEFT JOIN products p ON p.id = s.product_id
        WHERE {where} AND ({price_lookup.purchase_population_filter('s')}
                           OR {price_lookup.returned_lines_filter('s')})
        GROUP BY s.product_id, s.unit
        HAVING times_bought > 0
        ORDER BY s.product_id, s.unit
    """, params).fetchall()]

    # Two top-20 orderings, unioned (the issue's own decision) — a one-off
    # big-ticket purchase must still be ON the page even when 20 OTHER
    # products each outrank it on times bought alone. The template renders
    # this ONE set and re-sorts it client-side; it opens on times-bought.
    by_times = sorted(rows, key=lambda r: (-r['times_bought'], -r['total_net']))[:20]
    by_money = sorted(rows, key=lambda r: -r['total_net'])[:20]
    seen = set()
    union = []
    for r in by_times + by_money:
        key = (r['product_id'], r['unit'])
        if key not in seen:
            seen.add(key)
            union.append(r)
    union.sort(key=lambda r: (-r['times_bought'], -r['total_net']))

    cards = []
    for r in union:
        card = dict(r)
        pid, unit = card['product_id'], card['unit']
        card['last'] = None
        card['today'] = None
        card['stock'] = None
        if include_cost:
            card['cost'] = None
        if pid is None:
            # Unmapped BSN raw name — no product row to price against.
            cards.append(card)
            continue

        last_row = conn.execute(f"""
            SELECT date_iso, doc_base, net, qty, vat_type, unit_price, discount, total
            FROM sales_transactions s
            -- #554: the PRICE predicate here, not the purchase one two
            -- queries up. times_bought above answers "did this shop buy"
            -- (a written-off bill still counts, Put 2026-09-17); this row's
            -- net/qty is rendered to a rep AS A PRICE, so a ฿1.00 written-off
            -- line must never be it. A product whose only bill was written
            -- off therefore shows its count with no last price (card['last']
            -- stays None, already handled below) — which is the truthful
            -- answer to both questions.
            WHERE {where} AND {price_lookup.price_evidence_filter('s')}
              AND s.product_id = ? AND s.unit = ?
            ORDER BY s.date_iso DESC, s.id DESC LIMIT 1
        """, list(params) + [pid, unit]).fetchone()
        freebie_rows = []
        if last_row is not None:
            price_per_unit = vat_math.cash_from_net(last_row['net'] / last_row['qty'],
                                                      last_row['vat_type'])

            # Freebies: OTHER lines on the SAME document, SAME product, that
            # earned no revenue — shown in their OWN unit (may differ from
            # this row's unit), never restricted by unit.
            freebie_rows = conn.execute("""
                SELECT qty, unit FROM sales_transactions
                WHERE doc_base = ? AND product_id = ?
                  AND qty > 0 AND (net IS NULL OR net = 0)
            """, (last_row['doc_base'], pid)).fetchall()

            # #527: "list -discount = paid", derived from this line's OWN
            # numbers (pure helper, reconciliation-guarded) — replaces the
            # separate VAT-note / bill-discount / stale-note lines the card
            # used to print. None ("no formula") renders the price alone.
            formula = invoice_formula.invoice_line_formula(
                unit_price=last_row['unit_price'], qty=last_row['qty'],
                discount=last_row['discount'], total=last_row['total'],
                net=last_row['net'], vat_type=last_row['vat_type'])

            card['last'] = {
                'date_iso': last_row['date_iso'],
                'doc_base': last_row['doc_base'],
                'price_per_unit': round(price_per_unit, 2) if price_per_unit is not None else None,
                'vat_type': last_row['vat_type'],
                'unit_price': last_row['unit_price'],
                'discount': last_row['discount'],
                'formula': formula,
                'freebies': [{'qty': fr['qty'], 'unit': fr['unit']} for fr in freebie_rows],
            }

        # Today's price = resolve_price with no customer_code, so the
        # resolver's basis is always list_after_promo/dozen_only, never
        # last_paid — exactly "the list price for the row's unit (tier
        # first, then base × ratio) and the active price promotion" the
        # issue asks for, reusing the ONE pricing engine (never re-derived
        # here — .claude/rules/quoting-and-pricing.md).
        # Called at the customer's LAST quantity (Put, 2026-09-11, slice 3) so
        # the internal margin counts a bundle's free units when that order
        # size reaches the bundle; the price itself does not depend on qty.
        resolved = None
        try:
            resolved = price_lookup.resolve_price(
                conn, product_id=pid, unit=unit,
                qty=last_row['qty'] if last_row is not None else 1)
        except ValueError:
            pass  # unit has no resolvable ratio/tier — leave today/stock=None
        else:
            has_list_price = resolved['list']['list_for_unit'] != 0
            price_promo = resolved['list']['price_promo']
            # #527: "60.00 -10% = 54.00" only for the percent-shaped branch
            # of promotions.promo_price — 'percent', or a 'mixed' row
            # carrying a discount_value. NOT promotions.affects_price (that
            # predicate also covers 'fixed', which the issue explicitly
            # keeps on the plain promo_summary text: a fixed promo's
            # discount_value IS the final price, not a percentage to print
            # in this formula's shape).
            promo_affects_price = (
                price_promo is not None and price_promo['promo_type'] != 'fixed'
                and price_promo['discount_value'] is not None
            )
            card['today'] = {
                'price_per_unit': resolved['answer']['price_per_unit'] if has_list_price else None,
                'has_list_price': has_list_price,
                'base_per_piece': resolved['list']['base_per_piece'],
                'promo': price_promo,
                'list_for_unit': resolved['list']['list_for_unit'],
                'promo_affects_price': promo_affects_price,
            }

            # Stock badge: current stock in BASE units vs this row unit's
            # ratio. "No badge when the ratio is unknown" (issue's own
            # decision) — without a ratio there is no way to relate a
            # base-unit count to the row's own unit, so leave stock=None
            # rather than show a figure nobody can judge.
            ratio = resolved['unit']['ratio']
            if ratio is not None:
                stock_row = conn.execute(
                    "SELECT quantity FROM stock_levels WHERE product_id = ?", (pid,)
                ).fetchone()
                base_qty = stock_row['quantity'] if stock_row else 0
                card['stock'] = {
                    'base_qty': base_qty,
                    'insufficient': base_qty < ratio,
                }

        if include_cost:
            card['cost'] = _card_cost(conn, pid, unit, last_row, freebie_rows, resolved)
        cards.append(card)
    return cards


# #498 เสนอเพิ่ม tuning — module constants, not caller-configurable parameters.
# Nothing in the app (route or test) has ever needed a different value; the
# ladder says cut speculative configurability (review round 1, N-yagni).
_SUGGESTION_MIN_OTHER_SHOPS = 3
_SUGGESTION_WINDOW_DAYS = 730


def _cross_sell_suggestions(conn, customer_code, today=None, limit=10):
    """เสนอเพิ่ม → ขายดีที่ร้านนี้ยังไม่มี (#498): up to `limit` in-stock,
    active, priced products that at least `_SUGGESTION_MIN_OTHER_SHOPS`
    OTHER B2B shops bought in the trailing `_SUGGESTION_WINDOW_DAYS` (24
    months), that THIS shop has never bought (all-time — same population as
    ครั้งที่ซื้อ). One row per `products.sub_category`.

    Population = `price_lookup.purchase_population_filter` throughout — never
    a second "does this count as a sale" predicate, and never
    price_evidence_filter: "3 other shops bought it" is a purchase question, so
    a written-off bill still counts (Put, 2026-09-17, #554). All THREE call
    sites below must use the SAME one — the self-exclusion has to match the
    counting, or a shop gets its own product suggested back to it. Shop key = the call list's own
    canonical key (`COALESCE(NULLIF(TRIM(customer_code),''), customer)`), so
    a bill name shared by two companies is never conflated into one "shop"
    (same reasoning winback.py's module docstring gives for the same key).
    Review round 1 (SF5): this shop's OWN history is now looked up by the
    SAME canonical key, via a sub-select — previously it used an exact
    `customer_code = ?` match, which a whitespace-padded code could dodge
    while the self-exclusion (already on the canonical key) still caught it
    for counting, letting a shop's own product get suggested back to it.

    Ranking: distinct-other-shop count desc, then document count desc, then
    product id asc — deterministic. This shop's own code never counts
    toward any product's shop_count (excluded in the WHERE).

    Grouping: at most one row per sub_category, taking the HIGHEST-RANKED
    candidate in that sub_category THAT ACTUALLY QUALIFIES (has a
    resolvable price). Review round 1 (SF4): the sub-category claim used to
    happen BEFORE the price check, so an unpriced top-ranked product could
    silently hide a priced, lower-ranked product sharing its sub-category —
    the spec orders exclusions (including "no resolvable price") BEFORE
    grouping. A sub-category this shop already buys in ANY variant is
    dropped entirely up front (in SQL, before ranking). A NULL sub_category
    product stands on its own (never grouped with another NULL row).

    Independent of any date filter: the caller must never thread
    date_from/date_to through — there is no such parameter here at all, so
    it cannot happen by accident.

    Performance: ONE aggregate query does the ranking + every cheap filter
    (population, 24-month window, self-exclusion, is_active, in-stock,
    already-bought product, already-bought sub-category — the last two via
    sub-selects on the SAME canonical key, not a growing `NOT IN (?,?,...)`
    bound list: review round 1 (N-params) measured that list at 431 params
    for one real shop, close enough to SQLite's default 999-variable cap to
    be a real risk as a shop's history grows) in SQL. `price_lookup.
    resolve_price` — the only per-candidate call, and the only way to know
    whether a product has a resolvable list price — runs in ranked order
    and stops as soon as `limit` rows have qualified, never for every
    candidate.

    Returns a list of dicts: product_id, product_name, unit, shop_count,
    price_per_unit, list_for_unit, promo, promo_affects_price, stock_qty
    (None when the unit's piece ratio isn't derivable — same convention as
    the product card's stock badge; a whole-number result is an `int`, not
    a trailing-`.0` float, matching how ซื้อบ่อย's own stock column reads).
    NEVER a cost/WACC/margin field, for any caller/role — `resolve_price`'s
    `internal` block is never touched here (#498's own rule: no cost on
    suggestions, admin included), and no other shop's name or code is ever
    included (counts only).
    """
    import datetime as dt
    import price_lookup

    today = today or dt.date.today().isoformat()
    cutoff = (dt.date.fromisoformat(today)
              - dt.timedelta(days=_SUGGESTION_WINDOW_DAYS)).isoformat()

    key_expr = "COALESCE(NULLIF(TRIM(s.customer_code),''), s.customer)"
    own_key_expr = "COALESCE(NULLIF(TRIM(s2.customer_code),''), s2.customer)"

    rows = conn.execute(f"""
        SELECT s.product_id AS product_id,
               p.product_name AS product_name,
               p.sub_category AS sub_category,
               COUNT(DISTINCT {key_expr}) AS shop_count,
               COUNT(DISTINCT s.doc_base) AS doc_count
        FROM sales_transactions s
        JOIN products p ON p.id = s.product_id
        LEFT JOIN stock_levels sl ON sl.product_id = p.id
        WHERE {price_lookup.purchase_population_filter('s')}
          AND s.date_iso >= ?
          AND {key_expr} != ?
          AND p.is_active = 1
          AND COALESCE(sl.quantity, 0) > 0
          AND s.product_id NOT IN (
              SELECT DISTINCT s2.product_id
              FROM sales_transactions s2
              WHERE {own_key_expr} = ? AND {price_lookup.purchase_population_filter('s2')}
          )
          AND (p.sub_category IS NULL OR p.sub_category NOT IN (
              SELECT DISTINCT p2.sub_category
              FROM sales_transactions s2
              JOIN products p2 ON p2.id = s2.product_id
              WHERE {own_key_expr} = ? AND {price_lookup.purchase_population_filter('s2')}
                AND p2.sub_category IS NOT NULL
          ))
        GROUP BY s.product_id
        HAVING COUNT(DISTINCT {key_expr}) >= ?
        ORDER BY shop_count DESC, doc_count DESC, s.product_id ASC
    """, (cutoff, customer_code, customer_code, customer_code,
          _SUGGESTION_MIN_OTHER_SHOPS)).fetchall()

    out = []
    seen_subcats = set()
    for r in rows:
        if len(out) >= limit:
            break
        subcat = r['sub_category']
        if subcat is not None and subcat in seen_subcats:
            continue

        try:
            resolved = price_lookup.resolve_price(conn, product_id=r['product_id'],
                                                   unit=None, today=today)
        except ValueError:
            continue
        if resolved['list']['list_for_unit'] == 0:
            continue

        # SF4: claim the sub-category only once the candidate has actually
        # cleared every exclusion (here: has a resolvable price) — never
        # before. A sub-category whose top candidate fails this check is
        # NOT backfilled from the next-ranked candidate sharing it; it is
        # simply never claimed, so a lower-ranked one is free to claim it.
        if subcat is not None:
            seen_subcats.add(subcat)

        price_promo = resolved['list']['price_promo']
        promo_affects_price = (
            price_promo is not None and price_promo['promo_type'] != 'fixed'
            and price_promo['discount_value'] is not None
        )
        ratio = resolved['unit']['ratio']
        stock_qty = None
        if ratio is not None:
            stock_row = conn.execute(
                "SELECT quantity FROM stock_levels WHERE product_id = ?", (r['product_id'],)
            ).fetchone()
            base_qty = stock_row['quantity'] if stock_row else 0
            raw_qty = base_qty / ratio
            # N-stock: a whole-number result prints as `4298` not `4298.0`
            # (fmt_qty formats a float with its decimal point kept) — the
            # common ratio=1.0 case would otherwise ALWAYS show ".0" even
            # though the underlying stock_levels.quantity is an integer.
            stock_qty = int(raw_qty) if raw_qty == int(raw_qty) else round(raw_qty, 4)

        out.append({
            'product_id': r['product_id'],
            'product_name': r['product_name'],
            'unit': resolved['answer']['unit'],
            'shop_count': r['shop_count'],
            'price_per_unit': resolved['answer']['price_per_unit'],
            'list_for_unit': resolved['list']['list_for_unit'],
            'promo': price_promo,
            'promo_affects_price': promo_affects_price,
            'stock_qty': stock_qty,
        })
    return out


def get_customer_summary_by_code(customer_code, date_from=None, date_to=None,
                                 include_cost=False):
    """Code-keyed counterpart to get_customer_summary().

    `include_cost` (#493 slice 3) is the DATA-layer cost gate: the route
    passes True only for admin/manager, and only then do the product cards
    carry a `cost` key (WACC, last purchase cost, margins, badges).

    Unlike get_customer_summary (keyed on the bill name in sales_transactions),
    this resolves the master row DIRECTLY from `customers` by code, so the
    2,390 customers with no sales_transactions rows still render, and the two
    companies that share a bill name (BUG 2) never merge into one page.
    Returns the same dict shape; `data['customer']` is the bill name when one
    exists for this code, else the master name.
    """
    conn = get_connection()

    # N-404 (review round 1, #498): a cheap existence probe BEFORE the
    # suggestions helper's own aggregate query + up to 10 resolve_price
    # calls, so a typo'd code costs 2 lookups instead of a full top-10
    # computation before its eventual 404. This is deliberately a SEPARATE,
    # narrower check than the `exists` field returned below (which also
    # derives `display_name`/`salesperson`/etc. from the same two rows) —
    # duplicating just the boolean here keeps this an additive, low-risk
    # change rather than restructuring the rest of the function's order.
    exists_early = bool(conn.execute(
        "SELECT EXISTS(SELECT 1 FROM customers WHERE code = ?) "
        "OR EXISTS(SELECT 1 FROM sales_transactions WHERE customer_code = ?)",
        (customer_code, customer_code)
    ).fetchone()[0])

    where, params = _customer_sales_scope(
        'customer_code', customer_code, date_from, date_to)
    summary, top_products, monthly, docs = _customer_sales_aggregates(
        conn, where, params)
    product_cards = _customer_product_cards(conn, where, params, include_cost=include_cost)

    # Win-back (#497): the ONE shared computation (winback.py), ALWAYS over
    # the customer's FULL history — a fresh, date-INDEPENDENT scope built
    # here, never the (possibly date-filtered) `where` the product cards
    # above use. `import winback` is local (not at module top) for the same
    # reason `_customer_product_cards` imports price_lookup locally: both
    # pull in `models.promotions` at their own top level, and this file is
    # itself a submodule `models/__init__.py` is still in the middle of
    # importing at module-load time.
    import winback
    wb_where, wb_params = _customer_sales_scope('customer_code', customer_code, None, None)
    winback_rows = winback.compute_winback(conn, wb_where, wb_params)

    # เสนอเพิ่ม (#498): ALWAYS all-time / trailing-24-months, independent of
    # date_from/date_to — the helper takes no date params at all, so the
    # page's date filter can never reach it by accident (see the helper's
    # own docstring + tests/test_498_cross_sell_suggestions.py). Skipped
    # entirely for a code that doesn't exist at all (N-404 above).
    suggestions = _cross_sell_suggestions(conn, customer_code) if exists_early else []
    winback_by_key = {(w['product_id'], w['unit']): w for w in winback_rows}
    card_keys = set()
    for card in product_cards:
        key = (card['product_id'], card['unit'])
        card_keys.add(key)
        card['winback'] = winback_by_key.get(key)
    # Flagged items the card's top-40 union doesn't render at all — surfaced
    # as a count only (the customer page links it to the call card, which
    # lists every one of them).
    winback_overflow_count = sum(1 for k in winback_by_key if k not in card_keys)

    # Bill (short) name for THIS code specifically — most recent sale wins.
    # This is what distinguishes ทรัพย์ทวี's two codes; a name-keyed lookup
    # cannot (BUG 2).
    bill_name_row = conn.execute("""
        SELECT customer FROM sales_transactions
        WHERE customer_code = ? AND customer IS NOT NULL
        ORDER BY date_iso DESC LIMIT 1
    """, [customer_code]).fetchone()
    bill_name = bill_name_row['customer'] if bill_name_row else None

    # Master row is the anchor — resolves even when this code has zero sales.
    master_row = conn.execute("""
        SELECT c.code AS master_code, c.name AS master_name, c.address,
               c.salesperson AS sp_code,
               sp.name AS sp_name, sp.is_active AS sp_active
        FROM customers c
        LEFT JOIN salespersons sp ON sp.code = c.salesperson
        WHERE c.code = ?
    """, [customer_code]).fetchone()

    customer_info = None
    salesperson_code = None
    salesperson_display = None
    salesperson_orphan = False
    region_display = None
    display_name = bill_name or customer_code

    if master_row:
        row = conn.execute(
            "SELECT * FROM customers WHERE code=?", [customer_code]
        ).fetchone()
        if row:
            customer_info = dict(row)
        if not bill_name:
            display_name = master_row['master_name']
        salesperson_code = master_row['sp_code']
        if salesperson_code:
            if master_row['sp_name']:
                salesperson_display = master_row['sp_name']
            else:
                salesperson_display = salesperson_code
                salesperson_orphan = True
        # #528: ภาค from the address, same source the call card uses
        # (customer_geo.region_of) — retires the เขตการขาย FK, a one-time
        # copy of Express's โซน that was never maintained.
        region_display = customer_geo.region_of(master_row['address'])

    conn.close()
    return {
        # Date-INDEPENDENT: both source queries above ignore date_from/date_to, so
        # a customer whose filter excludes every bill still reads exists=True. The
        # route 404s on False; keying that off the filtered rows would 404 real
        # customers mid-filter.
        'exists': bool(master_row or bill_name_row),
        'customer': display_name,
        'customer_code': customer_code,
        'region': region_display,
        'salesperson': salesperson_display,
        'salesperson_code': salesperson_code,
        'salesperson_orphan': salesperson_orphan,
        'customer_info': customer_info,
        'date_from': date_from,
        'date_to': date_to,
        'summary': dict(summary),
        'top_products': [dict(r) for r in top_products],
        'product_cards': product_cards,
        # Raw shared list (#497) — same shape call_card.get_card returns under
        # its own 'winback' key, so a caller comparing the two surfaces never
        # has to reach into product_cards to rebuild it.
        'winback': winback_rows,
        'winback_overflow_count': winback_overflow_count,
        # เสนอเพิ่ม (#498) — additive, no cost/margin key ever (see the
        # helper's own docstring). Always all-time, never date-filtered.
        'suggestions': suggestions,
        'monthly': [dict(r) for r in monthly],
        'docs': [dict(r) for r in docs],
    }


def get_customers(search=None, region=None, page=1, per_page=50,
                   include_billless=False):
    """Customer list backed by customers master + salespersons.

    `search` matches the code, the name on the bill, and the name on the
    customers master — on BOTH halves of the union, so a customer answers to
    the same search before and after its first bill.
    Returns customer rows with display fields:
        salesperson  → name from salespersons master, or raw code if orphan
        region       → ภาค derived from the master address (customer_geo.
                       region_of), same source the call card uses.

    `region`, when given, must be one of `customer_geo.REGION_ORDER` (e.g.
    "ภาคตะวันออก", "ไม่ระบุภาค") — #528 retired เขตการขาย (customers.region_id),
    a one-time, never-maintained copy of Express's โซน. A legacy `?region_id=`
    or the old FK-based `?region=<code|name_th>` bookmark is simply IGNORED:
    neither value matches a REGION_ORDER string, so the filter no-ops rather
    than erroring.

    ภาค can't be filtered in SQL (it's derived in Python from the address),
    so unlike the old region_id filter this queries UNPAGINATED, derives ภาค
    per row, filters, THEN paginates — the same shape call_card.get_call_list
    already uses. 276 billing rows, or 2,665 with include_billless, is fine.

    `include_billless=True` unions in `customers` master rows with NO
    sales_transactions row at all (doc_count 0, total_net 0, last_date NULL)
    — 2,390 of 2,665 customers, invisible here otherwise. Default False keeps
    today's billing-only, 275-row view unchanged.
    """
    import price_lookup

    conn = get_connection()
    conds = []
    billing_params = []
    if search:
        # Match the master name here too, not just the bill name. The two
        # disagree for 198 of the 276 billing customers — the master carries a
        # legal prefix the bill drops ('หจก. ไทยทวีกิจ' vs 'ไทยทวีกิจ') — and
        # the bill-less half below matches on `c.name`. Without this, a
        # customer found by its registered name while bill-less stops
        # answering to that same search the moment its first bill lands.
        # No `c.code` predicate: the join pins `c.code = s.customer_code`, so
        # it could only ever match rows `s.customer_code` already matches.
        conds.append(
            "(s.customer LIKE ? OR s.customer_code LIKE ? OR c.name LIKE ?)")
        billing_params += [f"%{search}%"] * 3

    # Same exclusion as the customer DETAIL page — without it the list and the
    # detail disagree by the giveaway (proved: วรสวัสดิ์ ฿499,577.31 vs ฿345,454.51).
    conds.append(sales_filters.not_a_sale_clause('s'))
    where = ("WHERE " + " AND ".join(conds)) if conds else ""

    billing_sql = f"""
        SELECT s.customer                                AS customer,
               s.customer_code                            AS customer_code,
               COALESCE(c.address, '')                    AS address,
               COALESCE(sp.name, c.salesperson)           AS salesperson,
               c.salesperson                              AS salesperson_code,
               (c.salesperson IS NOT NULL
                  AND c.salesperson != ''
                  AND sp.code IS NULL)                    AS salesperson_orphan,
               COUNT(DISTINCT s.doc_base)                 AS doc_count,
               -- ยอดซื้อรวม, the detail page header's definition (#494)
               COALESCE(SUM({sales_filters.purchase_net_sql('s')}), 0) AS total_net,
               MAX(s.date_iso)                            AS last_date,
               (c.code IS NULL)                           AS missing_master,
               -- ซื้อล่าสุด (#493): same evidence-filtered definition as the
               -- customer detail page's header — MAX(s.date_iso) above stays
               -- a raw activity date (it can land on a credit note) and is
               -- not shown to Put; this column is what the list renders.
               -- `IS`, not `=`: ~21 rows carry a NULL customer_code (a real,
               -- acknowledged population — GROUP BY already collapses them
               -- into one row); `=` against NULL is never true in SQL, so
               -- that row's last_purchase_date silently read NULL even with
               -- real recent activity. `IS` is SQLite's NULL-safe equality.
               (SELECT MAX(s2.date_iso) FROM sales_transactions s2
                 WHERE s2.customer_code IS s.customer_code
                   AND {price_lookup.purchase_population_filter('s2')}) AS last_purchase_date
        FROM sales_transactions s
        LEFT JOIN customers     c  ON c.code  = s.customer_code
        LEFT JOIN salespersons  sp ON sp.code = c.salesperson
        {where}
        GROUP BY s.customer_code
    """
    union_parts = [billing_sql]
    params = list(billing_params)

    if include_billless:
        bl_conds = ["NOT EXISTS (SELECT 1 FROM sales_transactions s2 "
                    "WHERE s2.customer_code = c.code)"]
        bl_params = []
        if search:
            bl_conds.append("(c.name LIKE ? OR c.code LIKE ?)")
            bl_params += [f"%{search}%", f"%{search}%"]
        bl_where = "WHERE " + " AND ".join(bl_conds)

        billless_sql = f"""
            SELECT c.name                                 AS customer,
                   c.code                                  AS customer_code,
                   COALESCE(c.address, '')                 AS address,
                   COALESCE(sp.name, c.salesperson)         AS salesperson,
                   c.salesperson                            AS salesperson_code,
                   (c.salesperson IS NOT NULL
                      AND c.salesperson != ''
                      AND sp.code IS NULL)                  AS salesperson_orphan,
                   0                                         AS doc_count,
                   0                                         AS total_net,
                   NULL                                      AS last_date,
                   0                                         AS missing_master,
                   NULL                                      AS last_purchase_date
            FROM customers c
            LEFT JOIN salespersons sp ON sp.code = c.salesperson
            {bl_where}
        """
        union_parts.append(billless_sql)
        params = params + bl_params

    union_sql = "\nUNION ALL\n".join(union_parts)
    rows = [dict(r) for r in conn.execute(union_sql, params).fetchall()]
    conn.close()

    for r in rows:
        r['region'] = customer_geo.region_of(r.pop('address'))
    if region:
        rows = [r for r in rows if r['region'] == region]

    rows.sort(key=lambda r: r['customer'] or '')
    total = len(rows)
    start = (page - 1) * per_page
    page_rows = rows[start:start + per_page]
    return page_rows, total


# ── Customer Assignment (salesperson on customers master) ─────────────────────
# Migration 010 introduced customers.salesperson (TEXT code). It also added
# customers.region_id (FK regions.id) — เขตการขาย, retired in #528 (a
# one-time, never-maintained copy of Express's โซน; see customer_geo.region_of
# for the ภาค this workspace actually uses). The helpers below write to the
# MASTER table only — audit triggers on customers cover the change
# automatically.

def get_active_salespersons():
    conn = get_connection()
    rows = conn.execute(
        "SELECT code, name FROM salespersons WHERE is_active = 1 ORDER BY code"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_orphan_salesperson_codes():
    conn = get_connection()
    rows = conn.execute("""
        SELECT DISTINCT salesperson AS code
        FROM customers
        WHERE salesperson IS NOT NULL
          AND salesperson != ''
          AND salesperson NOT IN (SELECT code FROM salespersons)
    """).fetchall()
    conn.close()
    return {r['code'] for r in rows}


def get_customer_master(customer_code):
    conn = get_connection()
    row = conn.execute(
        "SELECT code, name, salesperson FROM customers WHERE code = ?",
        [customer_code],
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def update_customer_assignment(customer_code, salesperson_code):
    """Legacy assignment-only save (no contact fields in the request) — see
    update_customer_edit for the full-form path. #528: region_id dropped
    entirely (retired เขตการขาย — see customer_geo.region_of / ภาค); this
    function never touches that column, so a stale page posting only
    salesperson can never NULL it."""
    sp = (salesperson_code or '').strip() or None

    conn = get_connection()
    try:
        current = conn.execute(
            "SELECT salesperson FROM customers WHERE code = ?", (customer_code,)
        ).fetchone()
        if current is None:
            return {'ok': False, 'error': f'ไม่พบ customer code "{customer_code}"'}

        # Skip the active-salesperson check when the value is unchanged so a
        # customer with a legacy/orphan code can re-save other fields without
        # being forced to switch salesperson.
        if sp is not None and sp != current['salesperson']:
            if not conn.execute(
                "SELECT 1 FROM salespersons WHERE code = ? AND is_active = 1", (sp,)
            ).fetchone():
                return {'ok': False, 'error': f'ไม่พบ salesperson code "{sp}" (หรือ inactive)'}

        conn.execute(
            "UPDATE customers SET salesperson = ? WHERE code = ?",
            (sp, customer_code),
        )
        conn.commit()
        return {'ok': True, 'error': None}
    finally:
        conn.close()


# Group 2 (customer_summary.html plan.md terminology): the contact fields the
# customer-edit modal writes, alongside group 1 (salesperson above).
# NOT `name` (locked — see plan.md decision 3) and NOT the group-3 operational
# columns (customer_type/credit_days/tax_id/zone), which import_customers_from_bsn
# always overwrites even on a protected row — a form for those would lie.
CUSTOMER_CONTACT_FIELDS = ('nickname', 'phone', 'fax', 'contact', 'address', 'contact_note')

# customers column -> its customer_contact_review.proposed_* twin. Note the last
# pair is NOT a mechanical `proposed_` + name: the review column is
# `proposed_note`, not `proposed_contact_note`.
_REVIEW_COL = {
    'nickname':     'proposed_nickname',
    'phone':        'proposed_phone',
    'fax':          'proposed_fax',
    'contact':      'proposed_contact',
    'address':      'proposed_address',
    'contact_note': 'proposed_note',
}


def update_customer_edit(customer_code, salesperson_code, contact, username):
    """Customer-edit modal's save path: group 1 (salesperson) + group 2
    (contact fields), one UPDATE per save.

    #528: region_id dropped entirely (retired เขตการขาย — see
    customer_geo.region_of / ภาค). This function never touches that
    column, on purpose: the modal no longer sends it, and a form built
    before this change that still posts region_id must not silently NULL
    it — removing the column from the SQL, not defaulting the value, is
    what makes that impossible rather than merely unlikely.

    `contact` is a dict with `CUSTOMER_CONTACT_FIELDS` keys, raw form strings (blank
    means "clear this field" — the modal always echoes the live value back,
    so blank only happens when the field was already blank or Put cleared it
    on purpose).

    `contact_normalized_at`/`_by` are stamped ONLY when a contact field
    actually changed vs the live row — that stamp is what protects the row
    from the next BSN import overwriting it (import_customers_from_bsn checks
    `contact_normalized_at IS NOT NULL`). Stamping on every save, including a
    salesperson-only edit, would over-protect rows nobody touched contact on.

    When a contact field changed and a `customer_contact_review` row exists
    with status='pending', its proposed_* columns are updated to match —
    otherwise a later click on ยืนยัน in that queue silently reverts this
    edit (that page prefills from the frozen proposed_* snapshot, not the
    live row; 17 billing customers are in that state as of 2026-08-01).
    """
    sp = (salesperson_code or '').strip() or None

    # A short payload is a caller bug, never "clear the rest". The route already
    # branches on this, but this function is exported through the models facade,
    # so a future caller that skips the route would otherwise silently wipe the
    # keys it forgot — the exact failure this whole path was hardened against.
    # Re-checking here is a data-mutation invariant, not defensive decoration:
    # `contact[k]` below then cannot raise, and cannot default to blank.
    missing = [k for k in CUSTOMER_CONTACT_FIELDS if k not in contact]
    if missing:
        return {'ok': False,
                'error': f'contact payload ไม่ครบ (ขาด: {", ".join(missing)})'}

    new_contact = {k: (contact[k] or '').strip() or None for k in CUSTOMER_CONTACT_FIELDS}

    conn = get_connection()
    try:
        current = conn.execute(
            "SELECT * FROM customers WHERE code = ?", (customer_code,)
        ).fetchone()
        if current is None:
            return {'ok': False, 'error': f'ไม่พบ customer code "{customer_code}"'}

        if sp is not None and sp != current['salesperson']:
            if not conn.execute(
                "SELECT 1 FROM salespersons WHERE code = ? AND is_active = 1", (sp,)
            ).fetchone():
                return {'ok': False, 'error': f'ไม่พบ salesperson code "{sp}" (หรือ inactive)'}

        changed_fields = [k for k in CUSTOMER_CONTACT_FIELDS if new_contact[k] != current[k]]
        contact_changed = bool(changed_fields)

        if contact_changed:
            # Freeze the pre-edit values ONCE, so the state before the first
            # manual edit is always recoverable even if audit_log is ever
            # pruned. COALESCE means an existing snapshot is never clobbered.
            # ⚠ This is "before the first MANUAL edit", not "as Express sent
            # it" — if the normalizer already rewrote the row, its output is
            # what gets frozen. Same 4-key shape the import and the review
            # page write, so any reader sees one format. 1,674 of 2,665 rows
            # have no snapshot at all today.
            orig_json = json.dumps({
                'name':    current['name'],
                'phone':   current['phone'] or '',
                'contact': current['contact'] or '',
                'address': current['address'] or '',
            }, ensure_ascii=False)
            conn.execute("""
                UPDATE customers
                   SET salesperson = ?,
                       nickname = ?, phone = ?, fax = ?, contact = ?,
                       address = ?, contact_note = ?,
                       contact_orig_json = COALESCE(contact_orig_json, ?),
                       contact_normalized_at = datetime('now','localtime'),
                       contact_normalized_by = ?
                 WHERE code = ?
            """, (sp, *[new_contact[k] for k in CUSTOMER_CONTACT_FIELDS],
                  orig_json, username, customer_code))
        else:
            conn.execute(
                "UPDATE customers SET salesperson = ? WHERE code = ?",
                (sp, customer_code),
            )

        if contact_changed:
            pending = conn.execute("""
                SELECT id FROM customer_contact_review
                 WHERE customer_code = ? AND status = 'pending'
            """, (customer_code,)).fetchone()
            if pending:
                # ONLY the fields that actually changed. Writing all six would
                # destroy the normalizer's un-reviewed proposals for the fields
                # this edit never touched — 53 of the 62 pending rows have at
                # least one proposed_* that differs from the live value
                # (measured 2026-08-01), e.g. 11ม06's proposed_address carries a
                # postcode the live row lacks. Editing only the phone must not
                # silently drop that.
                sets = ', '.join(f'{_REVIEW_COL[k]} = ?' for k in changed_fields)
                conn.execute(
                    f"UPDATE customer_contact_review SET {sets} WHERE id = ?",
                    [new_contact[k] for k in changed_fields] + [pending['id']])

        conn.commit()
        return {'ok': True, 'error': None, 'contact_changed': contact_changed}
    finally:
        conn.close()


_AUDIT_FIELD_LABEL = {
    'name': 'ชื่อในทะเบียน', 'nickname': 'ชื่อเล่น', 'salesperson': 'เซลส์',
    'region_id': 'เขตการขาย', 'zone': 'โซน', 'address': 'ที่อยู่',
    'phone': 'โทรศัพท์', 'fax': 'แฟกซ์', 'contact': 'ผู้ติดต่อ',
    'contact_note': 'หมายเหตุ', 'tax_id': 'Tax ID', 'credit_days': 'เครดิต (วัน)',
    'lat': 'พิกัด lat', 'lng': 'พิกัด lng',
}


def get_customer_audit_history(customer_code, limit=15):
    """Recent changes to this customer's master row, newest first.

    `audit_customers_update` records `{field: [old, new]}` for every change
    (2,050 UPDATE rows as of 2026-08-01) — no page in the app read it, so the
    trail was only reachable by opening SQL. This makes it visible where the
    edits happen.

    Joined on `audit_log.row_key` (migration 150), NOT `customers.rowid`.
    `row_id` stores the SQLite rowid, which is IMPLICIT for this table (PK
    is TEXT `code`) — VACUUM is explicitly permitted to renumber an implicit
    rowid, and a renumber would re-point old rows at whatever customer now
    holds that rowid, confidently showing ANOTHER customer's history. That
    is why the card was pulled from #346 rather than shipped with a comment.
    `row_key` stores the business key (`customers.code`) directly at write
    time, so this query needs no join back through `customers` at all — and
    migration 151 both made `code` itself immutable and added the
    `(table_name, row_key)` index this query uses.

    ⚠ `audit_log.user` is NULL on every customers row (1,188 INSERT + 2,050
    UPDATE, checked 2026-08-01): the trigger is SQL-level and cannot see the
    logged-in user. So this answers "when + what", never "who" — the
    template must not imply otherwise.

    Returns [{created_at, action, changes: [{field, label, old, new}]}].
    """
    conn = get_connection()
    rows = conn.execute("""
        SELECT a.created_at, a.action, a.changed_fields
          FROM audit_log a
         WHERE a.table_name = 'customers'
           AND a.row_key = ?
         ORDER BY a.id DESC
         LIMIT ?
    """, (customer_code, limit)).fetchall()
    conn.close()

    out = []
    for r in rows:
        try:
            parsed = json.loads(r['changed_fields'] or '{}')
        except (ValueError, TypeError):
            parsed = {}
        changes = []
        for field, val in parsed.items():
            # UPDATE rows carry [old, new]; INSERT rows carry a bare value.
            if isinstance(val, list) and len(val) == 2:
                old, new = val
            else:
                old, new = None, val
            changes.append({
                'field': field,
                'label': _AUDIT_FIELD_LABEL.get(field, field),
                'old': '' if old is None else old,
                'new': '' if new is None else new,
            })
        out.append({'created_at': r['created_at'], 'action': r['action'],
                    'changes': changes})
    return out


def get_customers_master(search=None, salesperson=None, region_id=None,
                         orphan_only=False, page=1, per_page=100):
    """Every customer + salesperson (+ legacy region_id), for
    commission_bp.py's admin-only reassign-customer datalist. NOT the
    เขตการขาย write path — read-only, and its only caller
    (_reassign_customer_choices) never passes region_id; kept as-is rather
    than trimmed, since #528 retired the UI that filtered by it, not the
    column itself (see decisions/log.md — no DB change)."""
    conn = get_connection()
    conds = []
    params = []
    if search:
        conds.append("(c.code LIKE ? OR c.name LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    if salesperson == '__none__':
        conds.append("(c.salesperson IS NULL OR c.salesperson = '')")
    elif salesperson:
        conds.append("c.salesperson = ?")
        params.append(salesperson)
    if region_id:
        conds.append("c.region_id = ?")
        params.append(int(region_id))
    if orphan_only:
        conds.append(
            "c.salesperson IS NOT NULL AND c.salesperson != '' "
            "AND c.salesperson NOT IN (SELECT code FROM salespersons)"
        )
    where = ("WHERE " + " AND ".join(conds)) if conds else ""

    sql = f"""
        SELECT c.code, c.name, c.salesperson AS salesperson_code,
               s.name AS salesperson_name, s.is_active AS salesperson_active
        FROM customers c
        LEFT JOIN salespersons s ON s.code = c.salesperson
        {where}
        ORDER BY c.name
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, params + [per_page, (page - 1) * per_page]).fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) FROM customers c {where}", params
    ).fetchone()[0]
    conn.close()
    return [dict(r) for r in rows], total


def import_customers_from_bsn(customers):
    """Import BSN customer master rows with contact-protection and auto-sanitization.

    Returns (inserted, updated, protected):
      - inserted: new rows created
      - updated:  existing un-normalized rows refreshed (contact fields may be sanitized)
      - protected: existing rows with contact_normalized_at IS NOT NULL — only
                   operational fields (salesperson, zone, customer_type, credit_days,
                   tax_id, imported_at) are updated; all contact fields are preserved.
    """
    from customer_contact_normalize import normalize_customer

    conn = get_connection()
    inserted = updated = protected = 0

    # Commission reassignments override the source file's owner.
    #
    # This import refreshes `customers.salesperson` from the file on every
    # path, and Express still lists departed reps as the owner — น้อย /02 is on
    # 168 customers there. Without this, one import silently undoes migrations
    # 143/144 and every rule created through /commission/reassign.
    #
    # Same failure shape the reassignment table exists to solve:
    # `received_payments.salesperson` is UPSERT-ed by the weekly import, which
    # is why the decision lives in its own table rather than being edited into
    # the imported row. The rule is a decision made AFTER the file was
    # produced, so it wins; name/address/contact still refresh normally.
    #
    # Applied BEFORE the write, not corrected after: writing the file's value
    # and fixing it afterwards lands the right data but fires the
    # audit_customers_update trigger twice, recording a change that never
    # happened. The first cut of this guard did exactly that and put 936
    # phantom rows (468 out, 468 back) into a 2,718-row import — 34% noise in
    # the trail people rely on to answer "who changed this customer".
    reassigned = {r['customer_code']: r['to_salesperson'] for r in conn.execute("""
        SELECT customer_code, to_salesperson FROM commission_customer_reassign r
         WHERE is_active = 1
           AND effective_from = (SELECT MAX(r2.effective_from)
                                   FROM commission_customer_reassign r2
                                  WHERE r2.customer_code = r.customer_code
                                    AND r2.is_active = 1)
    """)}

    for c in customers:
        if c['code'] in reassigned:
            c = dict(c)
            c['salesperson'] = reassigned[c['code']]
        existing = conn.execute(
            "SELECT code, contact_normalized_at FROM customers WHERE code=?",
            (c['code'],)
        ).fetchone()

        if existing and existing['contact_normalized_at'] is not None:
            # ── PROTECTED branch: row has been cleaned — touch only operational cols ──
            conn.execute("""
                UPDATE customers
                   SET salesperson=?, zone=?, customer_type=?,
                       credit_days=?, tax_id=?,
                       imported_at=datetime('now','localtime')
                 WHERE code=?
            """, (c['salesperson'], c['zone'], c['customer_type'],
                  c['credit_days'], c['tax_id'], c['code']))
            protected += 1

        else:
            # ── UN-NORMALIZED / NEW branch: sanitize via normalizer ──
            res = normalize_customer({
                'name':    c['name'],
                'phone':   c.get('phone') or '',
                'contact': c.get('contact') or '',
                'address': c.get('address') or '',
            })

            prop = res['proposed']
            imp_phone   = c.get('phone') or ''
            imp_fax     = ''
            imp_contact = c.get('contact') or ''

            # Determine whether the normalizer found a meaningful, lossless change
            auto_changed = (
                res['confidence'] == 'auto'
                and (
                    prop['phone'] != imp_phone
                    or prop['fax']   # non-empty fax extracted
                    or prop['contact'] != imp_contact
                )
            )

            if auto_changed:
                out_phone   = prop['phone'] or None
                out_fax     = prop['fax'] or None
                out_contact = prop['contact'] or None
                out_note    = prop.get('note') or None
                orig_json   = json.dumps({
                    'name':    c['name'],
                    'phone':   imp_phone,
                    'contact': imp_contact,
                    'address': c.get('address') or '',
                }, ensure_ascii=False)
                normalized_at  = "datetime('now','localtime')"
                normalized_by  = 'bsn_import'
            else:
                out_phone   = imp_phone or None
                out_fax     = None
                out_contact = imp_contact or None
                orig_json   = None
                normalized_at  = None
                normalized_by  = None

            if existing:
                if auto_changed:
                    conn.execute("""
                        UPDATE customers
                           SET name=?, salesperson=?, zone=?, customer_type=?,
                               address=?, phone=?, fax=?, tax_id=?, credit_days=?,
                               contact=?, contact_note=?, contact_orig_json=?,
                               contact_normalized_at=datetime('now','localtime'),
                               contact_normalized_by=?,
                               imported_at=datetime('now','localtime')
                         WHERE code=?
                    """, (c['name'], c['salesperson'], c['zone'], c['customer_type'],
                          c.get('address'), out_phone, out_fax,
                          c['tax_id'], c['credit_days'],
                          out_contact, out_note, orig_json, normalized_by, c['code']))
                else:
                    conn.execute("""
                        UPDATE customers
                           SET name=?, salesperson=?, zone=?, customer_type=?,
                               address=?, phone=?, tax_id=?, credit_days=?,
                               contact=?, imported_at=datetime('now','localtime')
                         WHERE code=?
                    """, (c['name'], c['salesperson'], c['zone'], c['customer_type'],
                          c.get('address'), out_phone, c['tax_id'], c['credit_days'],
                          out_contact, c['code']))
                updated += 1
            else:
                if auto_changed:
                    conn.execute("""
                        INSERT INTO customers
                            (code, name, salesperson, zone, customer_type,
                             address, phone, fax, tax_id, credit_days, contact,
                             contact_note, contact_orig_json, contact_normalized_at,
                             contact_normalized_by)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'),?)
                    """, (c['code'], c['name'], c['salesperson'], c['zone'],
                          c['customer_type'], c.get('address'),
                          out_phone, out_fax, c['tax_id'], c['credit_days'],
                          out_contact, out_note, orig_json, normalized_by))
                else:
                    conn.execute("""
                        INSERT INTO customers
                            (code, name, salesperson, zone, customer_type,
                             address, phone, tax_id, credit_days, contact)
                        VALUES (?,?,?,?,?,?,?,?,?,?)
                    """, (c['code'], c['name'], c['salesperson'], c['zone'],
                          c['customer_type'], c.get('address'),
                          out_phone, c['tax_id'], c['credit_days'], out_contact))
                inserted += 1

    conn.commit()
    conn.close()
    return inserted, updated, protected


def get_customers_for_map(zone=None, customer_type=None, geocoded_only=False):
    conn = get_connection()
    conds = ['1=1']
    params = []
    if zone:
        conds.append('zone=?'); params.append(zone)
    if customer_type:
        conds.append('customer_type=?'); params.append(customer_type)
    if geocoded_only:
        conds.append('lat IS NOT NULL')
    where = ' AND '.join(conds)
    rows = conn.execute(
        f"SELECT * FROM customers WHERE {where} ORDER BY zone, code",
        params
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_customer_geocode(code, lat, lng):
    conn = get_connection()
    conn.execute(
        "UPDATE customers SET lat=?, lng=?, geocoded_at=datetime('now','localtime') WHERE code=?",
        (lat, lng, code)
    )
    conn.commit()
    conn.close()


def get_customer_zones():
    conn = get_connection()
    rows = conn.execute(
        "SELECT DISTINCT zone FROM customers WHERE zone IS NOT NULL ORDER BY zone"
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_customer_types():
    conn = get_connection()
    rows = conn.execute(
        "SELECT DISTINCT customer_type FROM customers WHERE customer_type IS NOT NULL ORDER BY customer_type"
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_geocode_progress():
    conn = get_connection()
    total = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    geocoded = conn.execute("SELECT COUNT(*) FROM customers WHERE lat IS NOT NULL").fetchone()[0]
    conn.close()
    return total, geocoded
