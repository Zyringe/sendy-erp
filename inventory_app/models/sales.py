"""Sales / purchases / trade-summary readers — extracted verbatim from
models.py (behavior-preserving split, Phase 12) — see models/__init__.py's
module docstring for the overall file-split rationale. No behavior changes.
"""

from datetime import date

from database import get_connection


def get_sales(product_id=None, date_from=None, date_to=None,
              vat_type=None, page=1, per_page=50, doc_no=None):
    conn = get_connection()
    conds = ['1=1']
    params = []
    if product_id:
        conds.append('s.product_id = ?'); params.append(product_id)
    if doc_no:
        # doc_no search overrides the date range entirely — search all history.
        conds.append("s.doc_no LIKE '%'||?||'%'"); params.append(doc_no)
    else:
        if date_from:
            conds.append('s.date_iso >= ?'); params.append(date_from)
        if date_to:
            conds.append('s.date_iso <= ?'); params.append(date_to)
    if vat_type is not None:
        conds.append('s.vat_type = ?'); params.append(vat_type)
    where = ' AND '.join(conds)
    sql = f"""
        SELECT s.*,
               COALESCE(p.product_name, s.product_name_raw) AS display_name
        FROM sales_transactions s
        LEFT JOIN products p ON p.id = s.product_id
        WHERE {where}
        ORDER BY s.date_iso DESC, s.doc_no
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, params + [per_page, (page-1)*per_page]).fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) FROM sales_transactions s WHERE {where}", params
    ).fetchone()[0]
    conn.close()
    return rows, total


def get_purchases_by_doc(doc_base):
    """ดึงทุก line item ของใบสั่งซื้อ (เช่น HP6900017 → HP6900017-1, -2, ...)"""
    conn = get_connection()
    rows = conn.execute("""
        SELECT p2.*,
               COALESCE(p.product_name, p2.product_name_raw) AS display_name
        FROM purchase_transactions p2
        LEFT JOIN products p ON p.id = p2.product_id
        WHERE p2.doc_no LIKE ? OR p2.doc_no = ?
        ORDER BY p2.doc_no
    """, (doc_base + '-%', doc_base)).fetchall()
    conn.close()
    return rows


def get_sales_summary(date_from=None, date_to=None, doc_no=None):
    """Returns totals split by vat_type."""
    conn = get_connection()
    conds = ['1=1']
    params = []
    if doc_no:
        # doc_no search overrides the date range entirely — search all history.
        conds.append("doc_no LIKE '%'||?||'%'"); params.append(doc_no)
    else:
        if date_from:
            conds.append('date_iso >= ?'); params.append(date_from)
        if date_to:
            conds.append('date_iso <= ?'); params.append(date_to)
    where = ' AND '.join(conds)
    rows = conn.execute(f"""
        SELECT vat_type,
               COUNT(*)       AS txn_count,
               SUM(qty)       AS total_qty,
               SUM(net)       AS total_net
        FROM sales_transactions
        WHERE {where}
        GROUP BY vat_type
    """, params).fetchall()
    conn.close()
    return rows


def get_sales_by_doc(doc_base):
    """ดึงทุก line item ของ invoice (เช่น IV6900394 → IV6900394-1, -2, ...)"""
    conn = get_connection()
    rows = conn.execute("""
        SELECT s.*,
               COALESCE(p.product_name, s.product_name_raw) AS display_name
        FROM sales_transactions s
        LEFT JOIN products p ON p.id = s.product_id
        WHERE s.doc_no LIKE ? OR s.doc_no = ?
        ORDER BY CAST(SUBSTR(s.doc_no, INSTR(s.doc_no, '-') + 1) AS INTEGER)
    """, (doc_base + '-%', doc_base)).fetchall()
    conn.close()
    return rows


def get_trade_dashboard(date_from=None, date_to=None):
    """
    date_from / date_to: 'YYYY-MM-DD' strings.
    Defaults to the most recent month that has actual data.
    Returns dict with summary cards, weekly trend, top products/customers/suppliers.
    """
    import calendar as _cal

    conn = get_connection()

    if not date_from and not date_to:
        today = date.today()
        date_from = today.strftime('%Y-%m-01')
        date_to   = today.strftime(f'%Y-%m-{_cal.monthrange(today.year, today.month)[1]:02d}')
    elif date_from and not date_to:
        date_to = date.today().isoformat()
    elif date_to and not date_from:
        date_from = '2000-01-01'

    # ── Summary this month ────────────────────────────────────────────────────
    s = conn.execute("""
        SELECT COUNT(DISTINCT doc_no) AS doc_count,
               COALESCE(SUM(net), 0)  AS total_net,
               COALESCE(SUM(qty), 0)  AS total_qty
        FROM sales_transactions
        WHERE date_iso >= ? AND date_iso <= ?
          AND doc_no NOT LIKE 'SR%' AND doc_no NOT LIKE 'HS%'
    """, (date_from, date_to)).fetchone()

    p = conn.execute("""
        SELECT COUNT(DISTINCT doc_no) AS doc_count,
               COALESCE(SUM(net), 0)  AS total_net,
               COALESCE(SUM(qty), 0)  AS total_qty
        FROM purchase_transactions
        WHERE date_iso >= ? AND date_iso <= ?
    """, (date_from, date_to)).fetchone()

    # ── Weekly trend (within selected date range) ─────────────────────────────
    weekly_sales = conn.execute("""
        SELECT strftime('%Y-W%W', date_iso) AS week,
               COALESCE(SUM(net), 0) AS net
        FROM sales_transactions
        WHERE date_iso >= ? AND date_iso <= ?
          AND doc_no NOT LIKE 'SR%' AND doc_no NOT LIKE 'HS%'
        GROUP BY week ORDER BY week
    """, (date_from, date_to)).fetchall()

    weekly_pur = conn.execute("""
        SELECT strftime('%Y-W%W', date_iso) AS week,
               COALESCE(SUM(net), 0) AS net
        FROM purchase_transactions
        WHERE date_iso >= ? AND date_iso <= ?
        GROUP BY week ORDER BY week
    """, (date_from, date_to)).fetchall()

    all_weeks   = sorted(set(r['week'] for r in weekly_sales) |
                         set(r['week'] for r in weekly_pur))
    s_by_week   = {r['week']: r['net'] for r in weekly_sales}
    p_by_week   = {r['week']: r['net'] for r in weekly_pur}
    weekly_trend = [
        {'week': w, 'sales': s_by_week.get(w, 0), 'purchases': p_by_week.get(w, 0)}
        for w in all_weeks
    ]

    # ── Top 10 สินค้าขายดี (by net) ──────────────────────────────────────────
    top_products = conn.execute("""
        SELECT COALESCE(pr.product_name, s.product_name_raw) AS name,
               s.product_id,
               SUM(s.qty)  AS total_qty,
               SUM(s.net)  AS total_net
        FROM sales_transactions s
        LEFT JOIN products pr ON pr.id = s.product_id
        WHERE s.date_iso >= ? AND s.date_iso <= ?
          AND s.doc_no NOT LIKE 'SR%' AND s.doc_no NOT LIKE 'HS%'
        GROUP BY s.product_id, s.product_name_raw
        ORDER BY total_net DESC
        LIMIT 10
    """, (date_from, date_to)).fetchall()

    # ── Top 10 ลูกค้า ─────────────────────────────────────────────────────────
    top_customers = conn.execute("""
        SELECT customer,
               COUNT(DISTINCT doc_no) AS doc_count,
               SUM(net)               AS total_net
        FROM sales_transactions
        WHERE date_iso >= ? AND date_iso <= ?
          AND doc_no NOT LIKE 'SR%' AND doc_no NOT LIKE 'HS%'
          AND customer IS NOT NULL AND customer != ''
        GROUP BY customer
        ORDER BY total_net DESC
        LIMIT 10
    """, (date_from, date_to)).fetchall()

    # ── Top 10 ซัพพลายเออร์ ──────────────────────────────────────────────────
    top_suppliers = conn.execute("""
        SELECT supplier,
               COUNT(DISTINCT doc_no) AS doc_count,
               SUM(net)               AS total_net
        FROM purchase_transactions
        WHERE date_iso >= ? AND date_iso <= ?
          AND supplier IS NOT NULL AND supplier != ''
        GROUP BY supplier
        ORDER BY total_net DESC
        LIMIT 10
    """, (date_from, date_to)).fetchall()

    conn.close()

    return {
        'date_from': date_from,
        'date_to': date_to,
        'sales': {
            'doc_count': s['doc_count'],
            'total_net': float(s['total_net']),
            'total_qty': s['total_qty'],
        },
        'purchases': {
            'doc_count': p['doc_count'],
            'total_net': float(p['total_net']),
            'total_qty': p['total_qty'],
        },
        'gross_profit': float(s['total_net']) - float(p['total_net']),
        'weekly_trend': weekly_trend,
        'top_products':  [dict(r) for r in top_products],
        'top_customers': [dict(r) for r in top_customers],
        'top_suppliers': [dict(r) for r in top_suppliers],
    }


def get_product_trade_summary(product_id, date_from=None, date_to=None):
    """
    Returns sales summary for a specific product:
    top customers, monthly trend, recent docs.
    """
    conn = get_connection()
    conds = ['s.product_id = ?']
    params = [product_id]
    if date_from:
        conds.append('s.date_iso >= ?'); params.append(date_from)
    if date_to:
        conds.append('s.date_iso <= ?'); params.append(date_to)
    where = ' AND '.join(conds)

    product = conn.execute(
        'SELECT id, product_name FROM products WHERE id = ?', (product_id,)
    ).fetchone()

    summary = conn.execute(f"""
        SELECT COUNT(DISTINCT s.doc_no) AS doc_count,
               COALESCE(SUM(s.net), 0)  AS total_net,
               COALESCE(SUM(s.qty), 0)  AS total_qty,
               MIN(s.date_iso)          AS first_date,
               MAX(s.date_iso)          AS last_date
        FROM sales_transactions s
        WHERE {where}
    """, params).fetchone()

    top_customers = conn.execute(f"""
        SELECT s.customer,
               SUM(s.qty)            AS total_qty,
               SUM(s.net)            AS total_net,
               COUNT(DISTINCT s.doc_no) AS doc_count
        FROM sales_transactions s
        WHERE {where}
          AND s.customer IS NOT NULL AND s.customer != ''
        GROUP BY s.customer
        ORDER BY total_net DESC
        LIMIT 20
    """, params).fetchall()

    monthly = conn.execute(f"""
        SELECT strftime('%Y-%m', s.date_iso) AS month,
               COUNT(DISTINCT s.doc_no) AS doc_count,
               SUM(s.qty)  AS total_qty,
               SUM(s.net)  AS total_net
        FROM sales_transactions s
        WHERE {where}
        GROUP BY month
        ORDER BY month
    """, params).fetchall()

    docs = conn.execute(f"""
        SELECT s.date_iso, s.doc_no, s.customer,
               SUM(s.qty) AS total_qty,
               SUM(s.net) AS total_net
        FROM sales_transactions s
        WHERE {where}
        GROUP BY s.doc_no
        ORDER BY s.date_iso DESC, s.doc_no
        LIMIT 200
    """, params).fetchall()

    conn.close()
    return {
        'product':    dict(product) if product else {},
        'date_from':  date_from,
        'date_to':    date_to,
        'summary':    dict(summary),
        'top_customers': [dict(r) for r in top_customers],
        'monthly':    [dict(r) for r in monthly],
        'docs':       [dict(r) for r in docs],
    }


def get_purchases(product_id=None, date_from=None, date_to=None, page=1,
                   per_page=50, vat_type=None, doc_no=None):
    conn = get_connection()
    conds = ['1=1']
    params = []
    if product_id:
        conds.append('p2.product_id = ?'); params.append(product_id)
    if doc_no:
        # doc_no search overrides the date range entirely — search all history.
        conds.append("p2.doc_no LIKE '%'||?||'%'"); params.append(doc_no)
    else:
        if date_from:
            conds.append('p2.date_iso >= ?'); params.append(date_from)
        if date_to:
            conds.append('p2.date_iso <= ?'); params.append(date_to)
    if vat_type is not None:
        conds.append('p2.vat_type = ?'); params.append(vat_type)
    where = ' AND '.join(conds)
    sql = f"""
        SELECT p2.*,
               COALESCE(p.product_name, p2.product_name_raw) AS display_name
        FROM purchase_transactions p2
        LEFT JOIN products p ON p.id = p2.product_id
        WHERE {where}
        ORDER BY p2.date_iso DESC, p2.doc_no
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, params + [per_page, (page-1)*per_page]).fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) FROM purchase_transactions p2 WHERE {where}", params
    ).fetchone()[0]
    conn.close()
    return rows, total


def get_purchases_summary(date_from=None, date_to=None, doc_no=None):
    """Range-total summary for purchases_view (mirrors get_sales_summary's
    pattern). purchases.html used to show `rows | sum(attribute='net')`,
    which only summed the CURRENT PAGE of a paginated list — this gives the
    real total across the whole filtered date range instead. Same WHERE
    filters as get_purchases (date only; purchases_view never passes
    product_id, so this doesn't need to accept it either)."""
    conn = get_connection()
    conds = ['1=1']
    params = []
    if doc_no:
        # doc_no search overrides the date range entirely — search all history.
        conds.append("doc_no LIKE '%'||?||'%'"); params.append(doc_no)
    else:
        if date_from:
            conds.append('date_iso >= ?'); params.append(date_from)
        if date_to:
            conds.append('date_iso <= ?'); params.append(date_to)
    where = ' AND '.join(conds)
    row = conn.execute(f"""
        SELECT COUNT(*)        AS txn_count,
               SUM(net)        AS total_net
        FROM purchase_transactions
        WHERE {where}
    """, params).fetchone()
    conn.close()
    return {'txn_count': row['txn_count'] or 0, 'total_net': row['total_net'] or 0.0}


def get_purchases_summary_by_vat(date_from=None, date_to=None, doc_no=None):
    """Returns purchase totals split by vat_type (mirrors get_sales_summary),
    backing the 3 VAT summary cards on purchases.html."""
    conn = get_connection()
    conds = ['1=1']
    params = []
    if doc_no:
        # doc_no search overrides the date range entirely — search all history.
        conds.append("doc_no LIKE '%'||?||'%'"); params.append(doc_no)
    else:
        if date_from:
            conds.append('date_iso >= ?'); params.append(date_from)
        if date_to:
            conds.append('date_iso <= ?'); params.append(date_to)
    where = ' AND '.join(conds)
    rows = conn.execute(f"""
        SELECT vat_type,
               COUNT(*)       AS txn_count,
               SUM(qty)       AS total_qty,
               SUM(net)       AS total_net
        FROM purchase_transactions
        WHERE {where}
        GROUP BY vat_type
    """, params).fetchall()
    conn.close()
    return rows
