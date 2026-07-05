"""Suppliers — extracted verbatim from models.py (behavior-preserving
split, Phase 11) — see models/__init__.py's module docstring for the
overall file-split rationale. No behavior changes.
"""
from database import get_connection


def get_suppliers(search=None, page=1, per_page=50):
    conn = get_connection()
    conds = ["supplier IS NOT NULL AND supplier != ''"]
    params = []
    if search:
        conds.append("(supplier LIKE ? OR supplier_code LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    where = "WHERE " + " AND ".join(conds)

    total = conn.execute(
        f"SELECT COUNT(DISTINCT supplier) FROM purchase_transactions {where}", params
    ).fetchone()[0]

    offset = (page - 1) * per_page
    rows = conn.execute(f"""
        SELECT supplier, supplier_code,
               COUNT(DISTINCT doc_no) AS doc_count,
               COALESCE(SUM(net), 0)  AS total_net,
               MAX(date_iso)          AS last_date
        FROM purchase_transactions
        {where}
        GROUP BY supplier, supplier_code
        ORDER BY total_net DESC
        LIMIT ? OFFSET ?
    """, params + [per_page, offset]).fetchall()

    conn.close()
    return [dict(r) for r in rows], total


def get_supplier_summary(supplier, date_from=None, date_to=None):
    conn = get_connection()
    conds = ['supplier = ?']
    params = [supplier]
    if date_from:
        conds.append('date_iso >= ?'); params.append(date_from)
    if date_to:
        conds.append('date_iso <= ?'); params.append(date_to)
    where = ' AND '.join(conds)

    summary = conn.execute(f"""
        SELECT COUNT(DISTINCT doc_no) AS doc_count,
               COALESCE(SUM(net), 0)  AS total_net,
               COALESCE(SUM(qty), 0)  AS total_qty,
               MIN(date_iso)          AS first_date,
               MAX(date_iso)          AS last_date
        FROM purchase_transactions
        WHERE {where}
    """, params).fetchone()

    top_products = conn.execute(f"""
        SELECT COALESCE(p.product_name, pt.product_name_raw) AS name,
               p.id AS product_id,
               pt.unit,
               SUM(pt.qty)  AS total_qty,
               SUM(pt.net)  AS total_net,
               COUNT(DISTINCT pt.doc_no) AS doc_count
        FROM purchase_transactions pt
        LEFT JOIN products p ON p.id = pt.product_id
        WHERE {where}
        GROUP BY pt.product_id, pt.product_name_raw
        ORDER BY total_net DESC
        LIMIT 20
    """, params).fetchall()

    monthly = conn.execute(f"""
        SELECT strftime('%Y-%m', date_iso) AS month,
               COUNT(DISTINCT doc_no) AS doc_count,
               SUM(net) AS total_net
        FROM purchase_transactions
        WHERE {where}
        GROUP BY month
        ORDER BY month
    """, params).fetchall()

    docs = conn.execute(f"""
        SELECT date_iso, doc_no,
               COUNT(*) AS line_count,
               SUM(qty) AS total_qty,
               SUM(net) AS total_net
        FROM purchase_transactions
        WHERE {where}
        GROUP BY doc_no
        ORDER BY date_iso DESC, doc_no
        LIMIT 200
    """, params).fetchall()

    supplier_code = conn.execute(
        "SELECT supplier_code FROM purchase_transactions WHERE supplier=? LIMIT 1", [supplier]
    ).fetchone()

    conn.close()
    return {
        'supplier': supplier,
        'supplier_code': supplier_code['supplier_code'] if supplier_code else None,
        'date_from': date_from,
        'date_to': date_to,
        'summary': dict(summary),
        'top_products': [dict(r) for r in top_products],
        'monthly': [dict(r) for r in monthly],
        'docs': [dict(r) for r in docs],
    }
