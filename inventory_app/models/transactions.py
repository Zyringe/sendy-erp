"""Transactions ledger — extracted verbatim from models.py (behavior-
preserving split, Phase 11) — see models/__init__.py's module docstring
for the overall file-split rationale. No behavior changes.
"""
from database import get_connection


def add_transaction(product_id: int, txn_type: str, quantity_change: int,
                    unit_mode: str, reference_no=None, note=None, created_at=None):
    conn = get_connection()
    if created_at is None:
        conn.execute("""
            INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, reference_no, note)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (product_id, txn_type, quantity_change, unit_mode, reference_no, note))
    else:
        conn.execute("""
            INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, reference_no, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (product_id, txn_type, quantity_change, unit_mode, reference_no, note, created_at))
    conn.commit()
    conn.close()


def get_current_stock(product_id: int) -> int:
    conn = get_connection()
    row = conn.execute("SELECT quantity FROM stock_levels WHERE product_id = ?", (product_id,)).fetchone()
    conn.close()
    return row['quantity'] if row else 0


def get_transactions(product_id=None, txn_type=None, date_from=None, date_to=None, page=1, per_page=50):
    conn = get_connection()
    conditions = ["1=1"]
    params = []
    if product_id:
        conditions.append("t.product_id = ?")
        params.append(product_id)
    if txn_type:
        conditions.append("t.txn_type = ?")
        params.append(txn_type)
    if date_from:
        conditions.append("DATE(t.created_at) >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("DATE(t.created_at) <= ?")
        params.append(date_to)

    where = " AND ".join(conditions)
    sql = f"""
        SELECT t.*, p.product_name, p.unit_type
        FROM transactions t
        JOIN products p ON p.id = t.product_id
        WHERE {where}
        ORDER BY t.created_at DESC
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, params + [per_page, (page - 1) * per_page]).fetchall()
    total = conn.execute(f"SELECT COUNT(*) FROM transactions t WHERE {where}", params).fetchone()[0]
    conn.close()
    return rows, total


def get_recent_transactions(limit=10):
    conn = get_connection()
    rows = conn.execute("""
        SELECT t.*, p.product_name, p.unit_type
        FROM transactions t
        JOIN products p ON p.id = t.product_id
        ORDER BY t.created_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return rows


def delete_transactions_by_ids(ids):
    if not ids:
        return
    conn = get_connection()
    try:
        placeholders = ','.join(['?']*len(ids))
        affected = [r['product_id'] for r in conn.execute(
            f"SELECT DISTINCT product_id FROM transactions WHERE id IN ({placeholders})", ids
        ).fetchall()]
        conn.execute(f"DELETE FROM transactions WHERE id IN ({placeholders})", ids)
        for pid in affected:
            conn.execute("DELETE FROM stock_levels WHERE product_id=?", (pid,))
            conn.execute("""
                INSERT INTO stock_levels (product_id, quantity)
                SELECT product_id, COALESCE(SUM(quantity_change), 0)
                FROM transactions WHERE product_id=?
                GROUP BY product_id
            """, (pid,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
