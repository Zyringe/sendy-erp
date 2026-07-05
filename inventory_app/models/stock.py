"""Stock alerts + product locations — extracted verbatim from models.py
(behavior-preserving split, Phase 11) — see models/__init__.py's module
docstring for the overall file-split rationale. No behavior changes.
"""
from database import get_connection


def get_stock_alerts():
    """Active products whose ledger stock is negative — a data-integrity red flag
    (sold/adjusted below zero). The -0.001 tolerance keeps IEEE-754 float noise
    on the REAL quantity column (e.g. -1e-14) from firing a false alert."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT p.id, p.product_name, p.unit_type,
               COALESCE(s.quantity, 0) AS quantity
        FROM products p
        JOIN stock_levels s ON s.product_id = p.id
        WHERE p.is_active = 1 AND s.quantity < -0.001
        ORDER BY s.quantity ASC
    """).fetchall()
    conn.close()
    return rows


def count_stock_alerts():
    conn = get_connection()
    n = conn.execute("""
        SELECT COUNT(*) FROM products p
        JOIN stock_levels s ON s.product_id = p.id
        WHERE p.is_active = 1 AND s.quantity < -0.001
    """).fetchone()[0]
    conn.close()
    return n


def get_product_locations(product_id: int):
    conn = get_connection()
    rows = conn.execute(
        "SELECT floor_no FROM product_locations WHERE product_id = ? ORDER BY floor_no",
        (product_id,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def save_product_locations(product_id: int, locations: list):
    conn = get_connection()
    conn.execute("DELETE FROM product_locations WHERE product_id = ?", (product_id,))
    for loc in locations:
        loc = loc.strip()
        if loc:
            conn.execute(
                "INSERT INTO product_locations (product_id, floor_no) VALUES (?, ?)",
                (product_id, loc)
            )
    conn.commit()
    conn.close()


def count_restock_needed(days=30):
    """Active products at/below zero stock that still sold within the last
    `days` — empty but in demand, i.e. should be reordered. Far more actionable
    than the flat low-stock threshold (which flags most of the catalog)."""
    conn = get_connection()
    n = conn.execute("""
        SELECT COUNT(DISTINCT p.id)
        FROM products p
        JOIN stock_levels s ON s.product_id = p.id
        WHERE p.is_active = 1 AND s.quantity <= 0
          AND EXISTS (SELECT 1 FROM sales_transactions st
                       WHERE st.product_id = p.id
                         AND st.net <> 0
                         AND st.date_iso >= date('now', ?))
    """, (f'-{days} days',)).fetchone()[0]
    conn.close()
    return n


def count_active_products():
    conn = get_connection()
    n = conn.execute("SELECT COUNT(*) FROM products WHERE is_active = 1").fetchone()[0]
    conn.close()
    return n


def count_in_stock():
    conn = get_connection()
    n = conn.execute("""
        SELECT COUNT(*) FROM products p
        JOIN stock_levels s ON s.product_id = p.id
        WHERE p.is_active = 1 AND s.quantity > 0
    """).fetchone()[0]
    conn.close()
    return n
