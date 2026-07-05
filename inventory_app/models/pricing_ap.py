"""Pricing summary + AP (accounts payable) helpers — extracted verbatim
from models.py (behavior-preserving split, Phase 12) — see
models/__init__.py's module docstring for the overall file-split rationale.
No behavior changes.
"""

from database import get_connection


def get_product_pricing_summary(product_id):
    """สรุปราคา BSN สำหรับหน้า product detail (avg_list_price, avg_effective)"""
    conn = get_connection()
    row = conn.execute("""
        SELECT
            SUM(unit_price * qty) / NULLIF(SUM(qty), 0) AS avg_list_price,
            SUM(CASE WHEN vat_type = 2 THEN net * 1.07 ELSE net END)
              / NULLIF(SUM(qty), 0)                      AS avg_effective,
            COUNT(DISTINCT unit_price)                   AS price_variants
        FROM sales_transactions
        WHERE product_id = ? AND qty > 0 AND unit_price > 0
    """, [product_id]).fetchone()
    conn.close()
    return {
        'avg_list_price': row['avg_list_price'] or 0.0,
        'avg_effective':  row['avg_effective']  or 0.0,
        'price_variants': row['price_variants'] or 0,
    }


def get_product_pricing(product_id):
    """ราคาขายสินค้า: list_prices (GROUP BY unit_price,vat_type) + effective_per_customer"""
    from collections import defaultdict

    conn = get_connection()

    # ── ราคาตั้งต่อ (unit_price, vat_type) ──────────────────────────────────
    price_rows = conn.execute("""
        SELECT
            unit_price,
            vat_type,
            COUNT(DISTINCT doc_no)  AS invoice_count,
            SUM(qty)                AS total_qty,
            MAX(date_iso)           AS last_sale,
            COUNT(DISTINCT customer) AS customer_count
        FROM sales_transactions
        WHERE product_id = ?
          AND qty > 0
          AND unit_price > 0
        GROUP BY unit_price, vat_type
        ORDER BY invoice_count DESC
    """, [product_id]).fetchall()

    # ── รายร้านค้าต่อ (unit_price, vat_type, customer) ───────────────────────
    cust_rows = conn.execute("""
        SELECT
            unit_price,
            vat_type,
            customer,
            customer_code,
            COUNT(DISTINCT doc_no)          AS invoice_count,
            SUM(qty)                        AS total_qty,
            MAX(date_iso)                   AS last_sale,
            GROUP_CONCAT(DISTINCT discount) AS discounts
        FROM sales_transactions
        WHERE product_id = ?
          AND qty > 0
          AND unit_price > 0
        GROUP BY unit_price, vat_type, customer
        ORDER BY unit_price, last_sale DESC
    """, [product_id]).fetchall()

    # ── ราคาเฉลี่ยต่อร้าน (actual customer-paid, after discount + VAT) ──────
    eff_rows = conn.execute("""
        SELECT
            customer,
            customer_code,
            COUNT(DISTINCT doc_no)  AS invoice_count,
            SUM(qty)                AS total_qty,
            SUM(CASE WHEN vat_type = 2 THEN net * 1.07 ELSE net END)
              / NULLIF(SUM(qty), 0) AS avg_effective,
            MAX(date_iso)           AS last_sale
        FROM sales_transactions
        WHERE product_id = ?
          AND qty > 0
          AND unit_price > 0
        GROUP BY customer
        ORDER BY avg_effective DESC
    """, [product_id]).fetchall()

    # ── สรุปภาพรวม ────────────────────────────────────────────────────────────
    summary = conn.execute("""
        SELECT
            SUM(unit_price * qty) / NULLIF(SUM(qty), 0)                          AS avg_list_price,
            SUM(CASE WHEN vat_type = 2 THEN net * 1.07 ELSE net END)
              / NULLIF(SUM(qty), 0)                                               AS avg_effective,
            COUNT(DISTINCT doc_no)                                                AS total_invoices,
            SUM(qty)                                                              AS total_qty
        FROM sales_transactions
        WHERE product_id = ?
          AND qty > 0
          AND unit_price > 0
    """, [product_id]).fetchone()

    conn.close()

    # ── group customers เข้า list_prices ─────────────────────────────────────
    cust_map = defaultdict(list)
    for r in cust_rows:
        key = (r['unit_price'], r['vat_type'])
        cust_map[key].append({
            'customer':       r['customer'],
            'customer_code':  r['customer_code'],
            'invoice_count':  r['invoice_count'],
            'total_qty':      r['total_qty'],
            'last_sale':      r['last_sale'],
            'discounts':      r['discounts'] or '',
        })

    list_prices = []
    for r in price_rows:
        key = (r['unit_price'], r['vat_type'])
        list_prices.append({
            'unit_price':     r['unit_price'],
            'vat_type':       r['vat_type'],
            'invoice_count':  r['invoice_count'],
            'total_qty':      r['total_qty'],
            'last_sale':      r['last_sale'],
            'customer_count': r['customer_count'],
            'customers':      cust_map.get(key, []),
        })

    effective_per_customer = [
        {
            'customer':      r['customer'],
            'customer_code': r['customer_code'],
            'invoice_count': r['invoice_count'],
            'total_qty':     r['total_qty'],
            'avg_effective': r['avg_effective'],
            'last_sale':     r['last_sale'],
        }
        for r in eff_rows
    ]

    return {
        'list_prices':            list_prices,
        'effective_per_customer': effective_per_customer,
        'avg_list_price':         summary['avg_list_price'] or 0.0,
        'avg_effective':          summary['avg_effective'] or 0.0,
        'total_invoices':         summary['total_invoices'] or 0,
        'total_qty':              summary['total_qty'] or 0.0,
    }


def get_ap_outstanding(conn=None):
    """เจ้าหนี้คงค้าง from the latest BSN AP snapshot.

    Returns a dict:
      {
        'snapshot_date': str | None,
        'invoices': [dict, ...],       -- all 7 (or N) invoice rows
        'suppliers': [                 -- grouped by supplier_name
            {
              'supplier_name': str,
              'supplier_type': str,
              'is_intercompany': bool, -- True if supplier_name contains 'เซ็นไดเทรดดิ้ง'
              'subtotal': float,
              'invoices': [dict, ...],
            }, ...
        ],
        'grand_total': float,
        'n_invoices': int,
        'n_suppliers': int,
      }

    Pass an open sqlite3 connection to reuse an existing one (caller must close).
    When conn=None a new connection is opened and closed internally.
    """
    close_after = conn is None
    if close_after:
        conn = get_connection()

    try:
        snap = conn.execute(
            "SELECT MAX(snapshot_date_iso) AS d "
            "FROM express_ap_outstanding WHERE entity='BSN'"
        ).fetchone()
        snapshot_date = snap['d'] if snap else None

        if not snapshot_date:
            return {
                'snapshot_date': None,
                'invoices': [],
                'suppliers': [],
                'grand_total': 0.0,
                'n_invoices': 0,
                'n_suppliers': 0,
            }

        rows = conn.execute("""
            SELECT supplier_name, supplier_type, supplier_code,
                   doc_no, supplier_invoice_no, doc_date_iso,
                   bill_amount, paid_amount, outstanding_amount,
                   CAST(julianday('now') - julianday(doc_date_iso) AS INTEGER) AS age_days
              FROM express_ap_outstanding
             WHERE entity = 'BSN'
               AND snapshot_date_iso = ?
             ORDER BY supplier_name, doc_date_iso
        """, (snapshot_date,)).fetchall()

        invoices = [dict(r) for r in rows]

        # Group by supplier_name (preserving first-seen order)
        seen = {}
        for inv in invoices:
            name = inv['supplier_name']
            if name not in seen:
                seen[name] = {
                    'supplier_name': name,
                    'supplier_type': inv['supplier_type'],
                    'is_intercompany': 'เซ็นไดเทรดดิ้ง' in name,
                    'subtotal': 0.0,
                    'invoices': [],
                }
            seen[name]['invoices'].append(inv)
            seen[name]['subtotal'] = round(
                seen[name]['subtotal'] + (inv['outstanding_amount'] or 0), 2
            )

        suppliers = list(seen.values())
        grand_total = round(sum(s['subtotal'] for s in suppliers), 2)

        return {
            'snapshot_date': snapshot_date,
            'invoices': invoices,
            'suppliers': suppliers,
            'grand_total': grand_total,
            'n_invoices': len(invoices),
            'n_suppliers': len(suppliers),
        }
    finally:
        if close_after:
            conn.close()
