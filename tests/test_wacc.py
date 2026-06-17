"""
Smoke tests for WACC (Weighted Average Cost) recalculation.

See models.recalculate_product_wacc (models.py:2391+).
WACC events fire only on/after _WACC_INITIAL_DATE ('2026-03-03').

Regressions covered:
- b6f67ee: reaching zero stock keeps last WACC (does not reset to next purchase price)
- 5ce0b79: same-day stock import is included in INITIAL ledger entry's display_stock
"""
import sqlite3


def _mk_product(conn, sku, name, cost_price=10.0, unit_type='ตัว'):
    # opening_cost mirrors cost_price, the production invariant since mig 111: the
    # ledger seeds from opening_cost, and create_product / the mig backfill always set
    # it alongside cost_price. Tests needing a costless seed override opening_cost=0.
    cur = conn.execute("INSERT INTO products (product_name, unit_type, cost_price, opening_cost) VALUES (?, ?, ?, ?)", (name, unit_type, cost_price, cost_price))
    pid = cur.lastrowid
    conn.execute("INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?, 0)", (pid,))
    return pid


def _add_purchase_txn(conn, product_id, doc_no, qty, net, date_iso='2026-03-10'):
    """Add a purchase_transactions row + paired BSN-style transactions IN.
    purchase_transactions has no doc_base column (only sales_transactions does)."""
    conn.execute("""
        INSERT INTO purchase_transactions
            (date_iso, doc_no, product_id, bsn_code, product_name_raw,
             supplier, supplier_code, qty, unit, unit_price, vat_type,
             discount, total, net, synced_to_stock)
        VALUES (?, ?, ?, 'X', 'x', 's', 'sc', ?, 'ตัว', ?, 0, '', ?, ?, 1)
    """, (date_iso, doc_no, product_id, qty, net / qty, net, net))
    conn.execute("""
        INSERT INTO transactions
            (product_id, txn_type, quantity_change, unit_mode, reference_no, note, created_at)
        VALUES (?, 'IN', ?, 'unit', ?, 'BSN ซื้อ', ?)
    """, (product_id, int(qty), doc_no, date_iso + ' 00:00:00'))


def _add_sale_txn(conn, product_id, doc_no, qty, date_iso='2026-03-15'):
    conn.execute("""
        INSERT INTO transactions
            (product_id, txn_type, quantity_change, unit_mode, reference_no, note, created_at)
        VALUES (?, 'OUT', ?, 'unit', ?, 'BSN ขาย', ?)
    """, (product_id, -int(qty), doc_no, date_iso + ' 00:00:00'))


# ── Basic purchase IN drives WACC ────────────────────────────────────────────

def test_purchase_in_sets_wacc(empty_db_conn):
    """First purchase on/after INITIAL_DATE establishes WACC = unit_cost."""
    import models

    pid = _mk_product(empty_db_conn, 91001, "WACC A", cost_price=0.0)
    _add_purchase_txn(empty_db_conn, pid, "HPW001", qty=10, net=200.0,
                      date_iso='2026-03-10')
    empty_db_conn.commit()

    wacc = models.recalculate_product_wacc(pid, empty_db_conn)
    empty_db_conn.commit()

    # 200 / 10 = 20.0
    assert wacc == 20.0

    # Ledger has a PURCHASE event with the right unit_cost
    row = empty_db_conn.execute(
        "SELECT unit_cost, wacc_after FROM product_cost_ledger"
        " WHERE product_id=? AND event_type='PURCHASE'", (pid,)
    ).fetchone()
    assert row['unit_cost']  == 20.0
    assert row['wacc_after'] == 20.0


# ── Regression for commit b6f67ee ────────────────────────────────────────────

def test_zero_stock_keeps_last_wacc(empty_db_conn):
    """
    Regression for b6f67ee: when stock hits zero, the next purchase must NOT
    reset WACC to the new purchase price — it should keep the last known WACC.

    Sequence:
      buy 10 @ 20  → WACC = 20, stock = 10
      sell 10      → stock = 0
      buy 5 @ 50   → because stock == 0, keep WACC = 20 (NOT 50)
    """
    import models

    pid = _mk_product(empty_db_conn, 91002, "WACC B", cost_price=0.0)
    _add_purchase_txn(empty_db_conn, pid, "HPW100", qty=10, net=200.0,
                      date_iso='2026-03-10')
    _add_sale_txn(empty_db_conn, pid, "IVW100-1", qty=10, date_iso='2026-03-12')
    _add_purchase_txn(empty_db_conn, pid, "HPW101", qty=5, net=250.0,  # 50/unit
                      date_iso='2026-03-14')
    empty_db_conn.commit()

    wacc = models.recalculate_product_wacc(pid, empty_db_conn)
    empty_db_conn.commit()

    # After the second purchase, WACC must remain 20.0 (last known), not 50.0
    assert wacc == 20.0, (
        f"Expected WACC to stay at 20.0 (last known) after zero-stock, got {wacc}. "
        "See models.py:2502-2504 (zero-stock branch)."
    )


# ── Regression for commit 5ce0b79 ────────────────────────────────────────────

def test_initial_ledger_includes_same_day_stock_import(empty_db_conn):
    """
    Regression for 5ce0b79: stock-import IN dated exactly on _WACC_INITIAL_DATE
    must be reflected in the INITIAL ledger entry's display_stock (not 0).
    """
    import models

    pid = _mk_product(empty_db_conn, 91003, "WACC C", cost_price=15.0)

    # Stock-import IN: a non-BSN, non-conversion IN with a non-matching note
    # on exactly the INITIAL_DATE — this is the case that was wrong before 5ce0b79.
    empty_db_conn.execute("""
        INSERT INTO transactions
            (product_id, txn_type, quantity_change, unit_mode, reference_no, note, created_at)
        VALUES (?, 'IN', 25, 'unit', NULL, 'นำเข้าสต็อค', '2026-03-03 00:00:00')
    """, (pid,))
    empty_db_conn.commit()

    models.recalculate_product_wacc(pid, empty_db_conn)
    empty_db_conn.commit()

    row = empty_db_conn.execute(
        "SELECT stock_after, qty_change FROM product_cost_ledger"
        " WHERE product_id=? AND event_type='INITIAL'", (pid,)
    ).fetchone()
    assert row is not None, "INITIAL ledger entry missing"
    # The fix: display_stock = current_stock + initial_date_stock_imports
    # i.e. the same-day import (25) is visible, not 0.
    assert row['stock_after'] == 25, (
        f"Expected INITIAL stock_after=25 (same-day import), got {row['stock_after']}. "
        "See models.py:2446-2455 + 2477-2485."
    )


# ── Live DB sanity (does not modify data — uses tmp_db copy) ─────────────────

def test_wacc_against_live_db_smoke(tmp_db):
    """
    Pick an active product from the temp copy of the live DB and verify
    recalculate_product_wacc runs without exceptions and returns a number.
    """
    import models, sqlite3

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id FROM products WHERE is_active=1 ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        conn.close()
        return  # no products — nothing to assert

    wacc = models.recalculate_product_wacc(row['id'], conn)
    conn.commit()
    conn.close()
    assert isinstance(wacc, (int, float))
    assert wacc >= 0


# ── Option D: cost_price is the live WACC output, opening_cost is the seed ────

def test_recompute_writes_cost_price_from_opening_cost_idempotently(empty_db_conn):
    """recalculate_product_wacc must (1) seed the INITIAL entry from opening_cost,
    (2) write the resulting live WACC back to products.cost_price (what margin/COGS/
    quote readers consume), and (3) be IDEMPOTENT — re-running it must NOT compound.

    Compounding was the latent bug from the 2026-06-17 bulk sync: when the seed and
    the output are the same column, each recompute re-blends past purchases into the
    seed and drifts the cost upward. opening_cost (immutable) fixes that.

    Scenario: opening 10 units @ 10 (opening_cost), then buy 10 @ 12.
              true WACC = (10*10 + 10*12) / 20 = 11.0
    """
    import models
    c = empty_db_conn

    pid = _mk_product(c, 95001, "D-core", cost_price=10.0)
    c.execute("UPDATE products SET opening_cost=10.0 WHERE id=?", (pid,))
    # opening stock: 10 units on INITIAL_DATE (non-purchase IN → seeds INITIAL stock)
    c.execute("""INSERT INTO transactions
                   (product_id, txn_type, quantity_change, unit_mode, reference_no, note, created_at)
                 VALUES (?, 'IN', 10, 'unit', NULL, 'ยอดยกมา', '2026-03-03 00:00:00')""", (pid,))
    _add_purchase_txn(c, pid, "HPD001", qty=10, net=120.0, date_iso='2026-03-10')  # 12/unit
    c.commit()

    w1 = models.recalculate_product_wacc(pid, c)
    c.commit()
    assert round(w1, 6) == 11.0
    cp1 = c.execute("SELECT cost_price FROM products WHERE id=?", (pid,)).fetchone()[0]
    assert round(cp1, 6) == 11.0, "recompute must write the live WACC to cost_price"

    # Idempotency: opening_cost is the seed (still 10), so a re-run stays at 11.0,
    # NOT 11.5 (which is what seeding from the just-written cost_price would give).
    w2 = models.recalculate_product_wacc(pid, c)
    c.commit()
    assert round(w2, 6) == 11.0, "recompute must be idempotent (no compounding)"
    cp2 = c.execute("SELECT cost_price FROM products WHERE id=?", (pid,)).fetchone()[0]
    assert round(cp2, 6) == 11.0
    opening = c.execute("SELECT opening_cost FROM products WHERE id=?", (pid,)).fetchone()[0]
    assert round(opening, 6) == 10.0, "opening_cost must remain the immutable seed"


def test_recompute_does_not_zero_a_costless_products_cost_price(empty_db_conn):
    """A product with no derivable WACC (opening_cost 0, no purchases) must keep its
    existing cost_price — recompute writes only when it has a real (>0) WACC, so a
    manually-set cost on a not-yet-purchased product is never wiped to 0."""
    import models
    c = empty_db_conn
    pid = _mk_product(c, 95002, "D-costless", cost_price=7.5)
    c.execute("UPDATE products SET opening_cost=0.0 WHERE id=?", (pid,))
    c.commit()

    models.recalculate_product_wacc(pid, c)
    c.commit()
    cp = c.execute("SELECT cost_price FROM products WHERE id=?", (pid,)).fetchone()[0]
    assert round(cp, 6) == 7.5, "cost_price must be preserved when WACC is 0"
