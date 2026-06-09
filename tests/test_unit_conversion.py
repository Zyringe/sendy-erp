"""
Smoke tests for the BSN unit-conversion gating logic in models._sync_bsn_to_stock.

Behavior under test (see models.py:355-470):
- bsn_unit == product.unit_type → implicit 1:1, no row required
- bsn_unit != product.unit_type and no unit_conversions row → row stays
  synced_to_stock = 0 (gated)
- conversion exists → quantity_change = bsn_qty * ratio
"""
import sqlite3

import pytest


def _seed_product(conn, sku, name, unit_type='ตัว'):
    cur = conn.execute("INSERT INTO products (product_name, unit_type) VALUES (?, ?)", (name, unit_type))
    pid = cur.lastrowid
    conn.execute("INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?, 0)", (pid,))
    return pid


def _seed_purchase(conn, product_id, doc_no, qty, unit, *, net=100.0):
    # Note: purchase_transactions has no doc_base column (only sales_transactions does).
    conn.execute("""
        INSERT INTO purchase_transactions
            (date_iso, doc_no, product_id, bsn_code, product_name_raw,
             supplier, supplier_code, qty, unit, unit_price, vat_type,
             discount, total, net, synced_to_stock)
        VALUES ('2026-04-24', ?, ?, 'BSN001', 'test', 'sup', 'sup-code',
                ?, ?, 1.0, 0, '', ?, ?, 0)
    """, (doc_no, product_id, qty, unit, net, net))


def test_implicit_1to1_when_units_match(empty_db_conn):
    """bsn_unit == unit_type → quantity_change == qty, no conversion row needed."""
    import models

    pid = _seed_product(empty_db_conn, 90001, "Test A", unit_type="ตัว")
    _seed_purchase(empty_db_conn, pid, "HP9000001", qty=10, unit="ตัว")
    empty_db_conn.commit()

    models._sync_bsn_to_stock(empty_db_conn, 'purchase_transactions', 'purchase')
    empty_db_conn.commit()

    row = empty_db_conn.execute(
        "SELECT synced_to_stock FROM purchase_transactions WHERE doc_no='HP9000001'"
    ).fetchone()
    assert row['synced_to_stock'] == 1

    qc = empty_db_conn.execute(
        "SELECT SUM(quantity_change) AS q FROM transactions"
        " WHERE product_id=? AND note LIKE 'BSN%'", (pid,)
    ).fetchone()['q']
    assert qc == 10  # 1:1


def test_gated_when_units_differ_and_no_conversion(empty_db_conn):
    """bsn_unit != unit_type and no unit_conversions row → row stays synced_to_stock=0."""
    import models

    pid = _seed_product(empty_db_conn, 90002, "Test B", unit_type="ตัว")
    _seed_purchase(empty_db_conn, pid, "HP9000002", qty=5, unit="กล")
    empty_db_conn.commit()

    models._sync_bsn_to_stock(empty_db_conn, 'purchase_transactions', 'purchase')
    empty_db_conn.commit()

    row = empty_db_conn.execute(
        "SELECT synced_to_stock FROM purchase_transactions WHERE doc_no='HP9000002'"
    ).fetchone()
    assert row['synced_to_stock'] == 0  # still pending

    txn = empty_db_conn.execute(
        "SELECT COUNT(*) AS n FROM transactions WHERE product_id=?", (pid,)
    ).fetchone()
    assert txn['n'] == 0


def test_qty_times_ratio_with_conversion(empty_db_conn):
    """When unit_conversions row exists, quantity_change == qty * ratio."""
    import models

    pid = _seed_product(empty_db_conn, 90003, "Test C", unit_type="ตัว")
    _seed_purchase(empty_db_conn, pid, "HP9000003", qty=3, unit="กล")
    # 1 กล = 12 ตัว
    empty_db_conn.execute(
        "INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?, 'กล', 12)",
        (pid,),
    )
    empty_db_conn.commit()

    models._sync_bsn_to_stock(empty_db_conn, 'purchase_transactions', 'purchase')
    empty_db_conn.commit()

    row = empty_db_conn.execute(
        "SELECT synced_to_stock FROM purchase_transactions WHERE doc_no='HP9000003'"
    ).fetchone()
    assert row['synced_to_stock'] == 1

    qc = empty_db_conn.execute(
        "SELECT quantity_change FROM transactions WHERE product_id=? AND reference_no='HP9000003'",
        (pid,),
    ).fetchone()
    # 3 กล * 12 = 36 ตัว, txn_type=IN so positive
    assert qc['quantity_change'] == 36


def test_get_base_qty_returns_none_when_unmapped(empty_db_conn):
    """_get_base_qty returns None when bsn_unit differs and no conversion is defined."""
    import models

    pid = _seed_product(empty_db_conn, 90004, "Test D", unit_type="ตัว")
    empty_db_conn.commit()

    assert models._get_base_qty(empty_db_conn, pid, "ตัว", "ตัว", 5) == 5
    assert models._get_base_qty(empty_db_conn, pid, "ตัว", "กล", 5) is None

    empty_db_conn.execute(
        "INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?, 'กล', 12)",
        (pid,),
    )
    empty_db_conn.commit()
    assert models._get_base_qty(empty_db_conn, pid, "ตัว", "กล", 5) == 60
