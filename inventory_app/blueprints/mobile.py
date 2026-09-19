"""Mobile-first flows blueprint (Phase 4 of mobile-friendly project).

Routes under /m are intentionally narrow, thumb-friendly, and one-task-per-screen.
They co-exist with the full responsive routes (e.g. /products) — bottom nav points
the most frequent mobile tasks here. Desktop users typically don't visit /m/* but
the routes work there too.
"""
from flask import Blueprint, render_template, request, jsonify, abort

import cashflow
import customer_geo
import models
import payments_alloc
import price_lookup
import sales_filters
from database import get_connection
import vat_math

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
    # How fast this customer pays (#499), keyed directly by the URL's code.
    pay_speed = payments_alloc.payment_speed(customer_code)
    # Use existing model fn — handles VAT, SR/HS doc filtering, paid-status correctly
    unpaid_full, unpaid_snapshot_date = models.get_customer_unpaid_bills_by_code(customer_code)
    # #493: the shared document grouping — same one the desktop customer page
    # uses — so this page's doc list/count can never drift from it again.
    last_sales = models.get_customer_documents('customer_code', customer_code, limit=5)
    unpaid = unpaid_full[:5]
    unpaid_total = sum((b['total_net'] or 0) for b in unpaid_full)
    # What was REMOVED from that total (ADR 0012, #468), keyed identically to
    # the chaseable list. This surface renders only the phone-sized count.
    excluded_docs, _excluded_snapshot = cashflow.bsn_ar_excluded_docs_by_code(customer_code)
    # A rep on a sales trip reads the outstanding total off this screen, so it
    # needs the staleness warning most of the four, not least.
    aging = cashflow.ar_aging()
    conn = get_connection()

    # Aggregate stats. total_net is ยอดซื้อรวม, the desktop header's own
    # definition (sales_filters.purchase_net_sql, #494: before VAT, credit notes
    # subtracted). doc_count counts documents (#493):
    # doc_no carries a per-line '-N' suffix, so COUNT(DISTINCT doc_no) counted
    # LINES, not documents. Also applies the same not_a_sale_clause() exclusion
    # `last_sales` (via get_customer_documents -> _customer_sales_scope)
    # already carries — without it, a document invoiced in error would count
    # here but be silently absent from the list right below it.
    stats = conn.execute(
        f"""
        SELECT COUNT(DISTINCT doc_base) AS doc_count,
               ROUND(SUM({sales_filters.purchase_net_sql()}), 2) AS total_net,
               MIN(date_iso) AS first_seen,
               MAX(date_iso) AS last_seen
          FROM sales_transactions
         WHERE customer_code = ? AND {sales_filters.not_a_sale_clause()}
        """,
        (customer_code,),
    ).fetchone()

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
    Python, then groups/filters/sorts. ~2,665 customers is fine for this (no
    heavier than the SUM/EXISTS subqueries below already ran on every
    candidate row before the old query's own LIMIT 300 truncated it).
    A legacy `?region_id=` bookmark is simply ignored.
    """
    region = (request.args.get('region') or '').strip() or None

    conn = get_connection()
    # Customers + outstanding total + last sale. Read from customers MASTER +
    # salespersons; customer_regions/regions no longer touched.
    sql = f"""
        SELECT c.code, c.name, c.zone, c.phone, COALESCE(c.address, '') AS address,
               COALESCE(sp.name, c.salesperson) AS salesperson,
               c.salesperson                    AS salesperson_code,
               (c.salesperson IS NOT NULL
                  AND c.salesperson != ''
                  AND sp.code IS NULL)          AS salesperson_orphan,
               -- ล่าสุด on the trip row is the customer's last PURCHASE (#513):
               -- the purchase population, imported from price_lookup, same as
               -- the customer page's ซื้อล่าสุด and the /call worklist. A raw
               -- MAX(date_iso) showed a rep a RETURN as a recent sale — 6 of
               -- the 2,665 customers here, measured on the prod snapshot
               -- 2026-09-14 (none of them loses a date).
               (SELECT MAX(date_iso) FROM sales_transactions s
                 WHERE s.customer = c.name
                   AND {price_lookup.purchase_population_filter('s')}) AS last_sale,
               (SELECT ROUND(SUM({vat_math.cash_sql('s')}), 2)
                  FROM sales_transactions s
                  WHERE s.customer = c.name
                    AND s.doc_base IS NOT NULL
                    AND s.doc_base NOT LIKE 'SR%'
                    AND s.doc_base NOT LIKE 'HS%'
                    -- HS is paid on the spot, never a receivable (#514)
                    -- "paid" means an ACTIVE receipt, same contract as
                    -- models.payments._ACTIVE_PAID_DOCS_CTE. The old
                    -- `LEFT JOIN paid_invoices ... IS NULL` had no
                    -- received_payments join at all, so a cancelled receipt
                    -- erased real debt from this customer-facing figure.
                    AND NOT EXISTS (
                        SELECT 1
                          FROM paid_invoices pi
                          JOIN received_payments rp ON rp.id = pi.re_id
                         WHERE pi.doc_no = s.doc_base
                           AND rp.cancelled = 0)
                    -- A rep opens this screen before walking into the shop,
                    -- so this figure is COLLECTABILITY — which excludes the
                    -- WHOLE ar_writeoffs table, not the revenue-only
                    -- `excludes_revenue = 1` subset (#568, ADR 0012; the four
                    -- readings of this table sit side by side at
                    -- price_lookup._WRITEOFF_SUBQUERY). Same clause and reason
                    -- as models.payments.find_customers_for_transfer. Note the
                    -- ล่าสุด subquery above deliberately does NOT take the
                    -- whole table (Put, 2026-09-17: a written-off bill is
                    -- still a purchase). Measured on prod 2026-09-17: both
                    -- write-offs reaching this population are flagged 0, so
                    -- the flag reading removes nothing — นางด้วง (เมืองพีน)
                    -- showed ฿10,200.00 owed on IV6701775, written off
                    -- 2026-06-05.
                    -- LOAD-BEARING: ar_writeoffs.doc_no must stay NOT NULL
                    -- (mig 095) — one NULL makes `NOT IN (SELECT ...)`
                    -- evaluate to NULL for every row and every figure here
                    -- silently becomes 0.
                    AND s.doc_base NOT IN (SELECT doc_no FROM ar_writeoffs)
               ) AS outstanding
          FROM customers c
     LEFT JOIN salespersons sp ON sp.code = c.salesperson
    """
    rows = [dict(r) for r in conn.execute(sql).fetchall()]
    conn.close()

    for r in rows:
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
                           total_outstanding=total_outstanding)
