"""Mobile-first flows blueprint (Phase 4 of mobile-friendly project).

Routes under /m are intentionally narrow, thumb-friendly, and one-task-per-screen.
They co-exist with the full responsive routes (e.g. /products) — bottom nav points
the most frequent mobile tasks here. Desktop users typically don't visit /m/* but
the routes work there too.
"""
from flask import Blueprint, render_template, request, jsonify, abort

import models
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

@bp_mobile.route('/customer/<path:customer_name>')
def customer_detail(customer_name):
    """Mobile-optimised customer card: header + contact + outstanding + last bills/sales."""
    conn = get_connection()
    # Customer master row (joined via name; existing schema keys customers by code
    # but invoices reference name, so we look up via name to match).
    customer = conn.execute(
        "SELECT * FROM customers WHERE name = ? LIMIT 1", (customer_name,)
    ).fetchone()

    # Region / salesperson from customers MASTER + lookup tables (post-D1).
    # Returns a dict-shaped row with display fields:
    #   region        → regions.name_th, fall back to regions.code, then NULL
    #   salesperson   → salespersons.name, fall back to raw code, then NULL
    region_row = None
    if customer:
        region_row = conn.execute(
            """
            SELECT COALESCE(r.name_th, r.code)        AS region,
                   COALESCE(sp.name, c.salesperson)   AS salesperson,
                   c.salesperson                      AS salesperson_code,
                   (c.salesperson IS NOT NULL
                      AND c.salesperson != ''
                      AND sp.code IS NULL)            AS salesperson_orphan,
                   r.code                             AS region_code
              FROM customers c
              LEFT JOIN salespersons sp ON sp.code = c.salesperson
              LEFT JOIN regions      r  ON r.id    = c.region_id
             WHERE c.code = ?
            """,
            (customer['code'],),
        ).fetchone()

    conn.close()
    # Use existing model fn — handles VAT, SR/HS doc filtering, paid-status correctly
    unpaid_full, unpaid_snapshot_date = models.get_customer_unpaid_bills(customer_name)
    unpaid = unpaid_full[:5]
    unpaid_total = sum((b['total_net'] or 0) for b in unpaid_full)
    conn = get_connection()

    # Last 5 sales docs (any status) — quick reference of recent activity
    last_sales = conn.execute(
        """
        SELECT date_iso, doc_no, ROUND(SUM(net), 2) AS total, COUNT(*) AS lines
          FROM sales_transactions
         WHERE customer = ?
         GROUP BY doc_no
         ORDER BY date_iso DESC
         LIMIT 5
        """,
        (customer_name,),
    ).fetchall()

    # Aggregate stats
    stats = conn.execute(
        """
        SELECT COUNT(DISTINCT doc_no) AS doc_count,
               ROUND(SUM(net), 2) AS total_net,
               MIN(date_iso) AS first_seen,
               MAX(date_iso) AS last_seen
          FROM sales_transactions WHERE customer = ?
        """,
        (customer_name,),
    ).fetchone()

    conn.close()
    return render_template(
        'm/customer.html',
        customer_name=customer_name,
        customer=customer,
        region=region_row,
        unpaid=unpaid,
        unpaid_total=unpaid_total,
        unpaid_snapshot_date=unpaid_snapshot_date,
        last_sales=last_sales,
        stats=stats,
    )


# ── Sales trip (zone-grouped) ─────────────────────────────────────────────────

@bp_mobile.route('/sales-trip')
def sales_trip():
    """Customers grouped by region → quick view for sales-rep field trip planning."""
    # Filter by regions.id (new) — fall back to legacy ?region=<code|name_th> for old bookmarks.
    region_id_raw = (request.args.get('region_id') or '').strip()
    region_legacy = (request.args.get('region') or '').strip()
    region_id = int(region_id_raw) if region_id_raw.isdigit() else None

    conn = get_connection()
    if region_id is None and region_legacy:
        match = conn.execute(
            "SELECT id FROM regions WHERE code = ? OR name_th = ? LIMIT 1",
            (region_legacy, region_legacy),
        ).fetchone()
        if match:
            region_id = match['id']

    # All regions for filter chips, sorted by sort_order then code (master)
    all_regions = [dict(r) for r in conn.execute(
        "SELECT id, code, name_th FROM regions ORDER BY sort_order, code"
    ).fetchall()]

    # Customers + outstanding total + last sale, optionally filtered by region.
    # Read from customers MASTER + salespersons + regions JOINs (post-D1);
    # customer_regions is no longer touched.
    sql = """
        SELECT c.code, c.name, c.zone, c.phone, c.address,
               COALESCE(r.name_th, r.code)      AS region,
               r.code                           AS region_code,
               c.region_id,
               COALESCE(sp.name, c.salesperson) AS salesperson,
               c.salesperson                    AS salesperson_code,
               (c.salesperson IS NOT NULL
                  AND c.salesperson != ''
                  AND sp.code IS NULL)          AS salesperson_orphan,
               (SELECT MAX(date_iso) FROM sales_transactions s WHERE s.customer = c.name) AS last_sale,
               (SELECT ROUND(SUM(CASE WHEN s.vat_type = 2 THEN s.net * 1.07 ELSE s.net END), 2)
                  FROM sales_transactions s
                  LEFT JOIN paid_invoices pi ON pi.doc_no = s.doc_base
                  WHERE s.customer = c.name
                    AND s.doc_base IS NOT NULL
                    AND s.doc_base NOT LIKE 'SR%'
                    AND s.doc_base NOT LIKE 'HS%'
                    AND pi.doc_no IS NULL
               ) AS outstanding
          FROM customers c
     LEFT JOIN salespersons sp ON sp.code = c.salesperson
     LEFT JOIN regions      r  ON r.id    = c.region_id
    """
    params = []
    if region_id is not None:
        sql += " WHERE c.region_id = ? "
        params.append(region_id)
    sql += """
         ORDER BY COALESCE(r.name_th, r.code, 'zzz'), c.name
         LIMIT 300
    """
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    # Group by region. Key is the FK id so two regions with an identical
    # name_th can't merge accidentally; the section header pulls the display
    # name from the customer row.
    grouped = {}
    total_outstanding = 0.0
    for r in rows:
        key = r['region_id'] if r['region_id'] is not None else '__none__'
        grouped.setdefault(key, []).append(r)
        if r['outstanding']:
            total_outstanding += r['outstanding']

    return render_template('m/sales_trip.html',
                           grouped=grouped,
                           all_regions=all_regions,
                           region_id=region_id,
                           total_outstanding=total_outstanding)
