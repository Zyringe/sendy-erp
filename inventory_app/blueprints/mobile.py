"""Mobile-first flows blueprint (Phase 4 of mobile-friendly project).

Routes under /m are intentionally narrow, thumb-friendly, and one-task-per-screen.
They co-exist with the full responsive routes (e.g. /products) — bottom nav points
the most frequent mobile tasks here. Desktop users typically don't visit /m/* but
the routes work there too.
"""
from flask import Blueprint, render_template, request, jsonify, abort

import ar_statement
import customer_geo
import marketplace_match
import payments_alloc
import purchase_history
from database import get_connection

bp_mobile = Blueprint('mobile', __name__, url_prefix='/m',
                      template_folder='../templates/m')


# ── Stock check (live search) ─────────────────────────────────────────────────

@bp_mobile.route('/stock')
def stock_search():
    """Render the search page. Empty initial state; results come from
    /m/stock/api as the user types."""
    return render_template('m/stock.html')


@bp_mobile.route('/stock/api')
def stock_search_api():
    """JSON live-search across products. Returns name/qty/price/unit so
    the card list can render without further fetches."""
    q = (request.args.get('q') or '').strip()
    if len(q) < 1:
        return jsonify({'items': []})
    pat_starts = f'{q}%'
    pat_anywhere = f'%{q}%'
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT p.id, p.product_name, p.unit_type, p.base_sell_price,
               COALESCE(sl.quantity, 0) AS qty,
               p.low_stock_threshold,
               (SELECT floor_no FROM product_locations
                  WHERE product_id = p.id ORDER BY id LIMIT 1) AS location
          FROM products p
     LEFT JOIN stock_levels sl ON sl.product_id = p.id
         WHERE p.is_active = 1
           AND (p.product_name LIKE :anywhere
                OR CAST(p.id AS TEXT) LIKE :anywhere
                OR EXISTS (SELECT 1 FROM product_barcodes pb
                            WHERE pb.product_id = p.id AND pb.barcode LIKE :anywhere))
         ORDER BY
             CASE WHEN CAST(p.id AS TEXT) = :exact THEN 0
                  WHEN p.product_name LIKE :starts THEN 1
                  ELSE 2 END,
             p.product_name
         LIMIT 30
        """,
        {'anywhere': pat_anywhere, 'starts': pat_starts, 'exact': q}
    ).fetchall()
    conn.close()
    items = [
        {
            'id':       r['id'],
            'name':     r['product_name'],
            'unit':     r['unit_type'],
            'price':    r['base_sell_price'] or 0,
            'qty':      r['qty'],
            'low':      (r['qty'] or 0) <= (r['low_stock_threshold'] or 0),
            'location': r['location'] or '',
        }
        for r in rows
    ]
    return jsonify({'items': items, 'q': q})


# ── Customer detail (mobile) ──────────────────────────────────────────────────

@bp_mobile.route('/customer/code/<customer_code>')
def customer_detail(customer_code):
    """Mobile-optimised customer card: header + contact + outstanding + last bills/sales."""
    conn = get_connection()
    customer = conn.execute(
        "SELECT * FROM customers WHERE code = ?", (customer_code,)
    ).fetchone()

    # Use the desktop code page's bill-name lookup, with this surface's
    # master-first display rule.
    bill_name_row = conn.execute(
        """
        SELECT customer FROM sales_transactions
         WHERE customer_code = ? AND customer IS NOT NULL
         ORDER BY date_iso DESC LIMIT 1
        """,
        (customer_code,),
    ).fetchone()
    bill_name = bill_name_row['customer'] if bill_name_row else None
    customer_name = customer['name'] if customer else (bill_name or customer_code)

    # Salesperson from customers MASTER + lookup table; ภาค (#528) derived
    # from the address (customer_geo.region_of), same source the call card
    # uses — retires the เขตการขาย FK lookup this used to run.
    # Returns a dict-shaped row with display fields:
    #   region        → ภาค from the address, or ไม่ระบุภาค
    #   salesperson   → salespersons.name, fall back to raw code, then NULL
    region_row = None
    if customer:
        region_row = dict(conn.execute(
            """
            SELECT COALESCE(sp.name, c.salesperson)   AS salesperson,
                   c.salesperson                      AS salesperson_code,
                   (c.salesperson IS NOT NULL
                      AND c.salesperson != ''
                      AND sp.code IS NULL)            AS salesperson_orphan
              FROM customers c
              LEFT JOIN salespersons sp ON sp.code = c.salesperson
             WHERE c.code = ?
            """,
            (customer_code,),
        ).fetchone())
        region_row['region'] = customer_geo.region_of(customer['address'])

    conn.close()
    # How fast this customer pays (#499). It sits among the header cells that come
    # from the customers row, so a code without one shows none of them, the same
    # rule as the desktop code page.
    pay_speed = payments_alloc.payment_speed(customer_code) if customer else None
    statement = ar_statement.customer_statement(customer_code)
    unpaid_full = sorted(statement['bills'], key=lambda b: b['doc_date_iso'] or '',
                         reverse=True)
    unpaid = unpaid_full[:5]
    # The chaseable NET, as /ar shows it (#708); credits get their own line.
    unpaid_total = statement['total']
    unpaid_credit = round(sum(c['outstanding'] for c in statement['credits']), 2)
    unpaid_snapshot_date = statement['snapshot_date']
    # What was REMOVED from that total (ADR 0012, #468). This surface renders
    # only the phone-sized count.
    excluded_docs = statement['excluded']
    # A rep on a sales trip reads the outstanding total off this screen, so it
    # needs the staleness warning most of the four, not least.
    aging = statement['freshness']
    conn = get_connection()

    # Aggregate stats, from the same purchase_history definitions (totals()) the desktop
    # customer page reads (card C P2), so the two can never disagree: total_net
    # is ยอดซื้อรวม (before VAT, credit notes subtracted, #494), doc_count counts
    # documents (#493), and a document invoiced in error is out of both the
    # figures and the `last_sales` list above.
    totals = purchase_history.totals(conn, customer_code)
    # #493: the shared document grouping — same one the desktop customer page
    # uses — so this page's doc list/count can never drift from it again.
    last_sales = purchase_history.documents(conn, customer_code, limit=5)
    stats = {
        'doc_count': totals['doc_count'],
        'total_net': round(totals['purchase_total'], 2),
        'first_seen': totals['first_activity'],
        'last_seen': totals['last_activity'],
    }

    conn.close()
    return render_template(
        'm/customer.html',
        customer_code=customer_code,
        customer_name=customer_name,
        customer=customer,
        pay_speed=pay_speed,
        region=region_row,
        unpaid=unpaid,
        unpaid_total=unpaid_total,
        unpaid_credit=unpaid_credit,
        unpaid_snapshot_date=unpaid_snapshot_date,
        excluded_docs=excluded_docs,
        aging=aging,
        last_sales=last_sales,
        stats=stats,
    )


# ── Sales trip (zone-grouped) ─────────────────────────────────────────────────

@bp_mobile.route('/sales-trip')
def sales_trip():
    """Customers grouped by ภาค → quick view for sales-rep field trip
    planning. #528: retired เขตการขาย (customers.region_id) — grouped and
    filtered by ภาค (customer_geo.region_of, derived from the address), the
    same source the call card and /customers use.

    ภาค can't be filtered or grouped in SQL, so — same shape as
    get_customers() — this queries every customer, derives ภาค per row in
    Python, then groups/filters/sorts: every customer, fine at current size.
    A legacy `?region_id=` bookmark is simply ignored.
    """
    region = (request.args.get('region') or '').strip() or None

    conn = get_connection()
    sql = f"""
        SELECT c.code, c.name, c.zone, c.phone, COALESCE(c.address, '') AS address,
               COALESCE(sp.name, c.salesperson) AS salesperson,
               c.salesperson                    AS salesperson_code,
               (c.salesperson IS NOT NULL
                  AND c.salesperson != ''
                  AND sp.code IS NULL)          AS salesperson_orphan
          FROM customers c
     LEFT JOIN salespersons sp ON sp.code = c.salesperson
         WHERE c.code NOT IN ({', '.join('?' * len(marketplace_match.MARKETPLACE_CODES))})
    """
    # A marketplace pseudo-customer is not a shop a rep can visit, and its
    # outstanding is platform settlement, not a debt to chase.
    rows = [dict(r) for r in conn.execute(
        sql, tuple(marketplace_match.MARKETPLACE_CODES)).fetchall()]
    hist = purchase_history.histories(conn)
    # A rep opens this screen before walking into the shop, so the figure is
    # chaseable AR, the same one the customer screen shows (ADR 0023).
    owed = {t['customer_code']: t['outstanding']
            for t in ar_statement.customer_totals(conn=conn) if t['customer_code']}
    aging = ar_statement.freshness(conn=conn)
    conn.close()

    for r in rows:
        # Joined on the CODE (#569): the bill name drifts from the master name and
        # the name join blanked 195 of 272 buyers on prod 2026-09-18.
        r['last_sale'] = hist.get(r['code'], {}).get('last_purchase')
        r['outstanding'] = owed.get(r['code'])
        r['region'] = customer_geo.region_of(r.pop('address'))
    if region:
        rows = [r for r in rows if r['region'] == region]

    # Sort by ภาค (customer_geo.REGION_ORDER's geographic sequence, not
    # alphabetical) then customer name — grouping below then builds `grouped`
    # in that same order for free (dict preserves insertion order).
    region_rank = {name: i for i, name in enumerate(customer_geo.REGION_ORDER)}
    rows.sort(key=lambda r: (region_rank[r['region']], r['name'] or ''))

    grouped = {}
    total_outstanding = 0.0
    for r in rows:
        grouped.setdefault(r['region'], []).append(r)
        if r['outstanding']:
            total_outstanding += r['outstanding']

    return render_template('m/sales_trip.html',
                           grouped=grouped,
                           regions=customer_geo.REGION_ORDER,
                           region=region,
                           total_outstanding=total_outstanding,
                           aging=aging)
