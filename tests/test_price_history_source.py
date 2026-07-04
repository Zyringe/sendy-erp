"""Source/provenance tracking on product_price_history (mig 130 — TDD, written first).

product_price_history is populated by the SQLite trigger
`product_price_history_update` (AFTER UPDATE ON products). A trigger can't know
WHY a price changed, so mig 130 adds:
  - a nullable `source` column on product_price_history,
  - a single-row context table `price_change_source` the trigger reads,
  - a rewritten trigger that stamps source from that context row.

The app write-path (models.update_product, recalculate_product_wacc) sets the
context row in the SAME transaction right before UPDATE products, so the trigger
stamps the right source. Paths that set nothing default to NULL (nothing breaks).

Schema + trigger + money-adjacent → mandatory TDD (project rule).
"""
import sqlite3

import pytest

import config
import models
from database import get_connection


# ── schema ────────────────────────────────────────────────────────────────────

def test_source_column_exists(tmp_db_conn):
    cols = {r["name"] for r in tmp_db_conn.execute(
        "PRAGMA table_info(product_price_history)"
    )}
    assert "source" in cols


def test_context_table_seeded(tmp_db_conn):
    # table exists and holds the single id=1 row (default NULL)
    row = tmp_db_conn.execute(
        "SELECT id, source FROM price_change_source WHERE id = 1"
    ).fetchone()
    assert row is not None
    assert row["id"] == 1


# ── trigger reads the context row ────────────────────────────────────────────

def _some_active_pid(conn):
    r = conn.execute(
        "SELECT id, base_sell_price FROM products WHERE is_active = 1 LIMIT 1"
    ).fetchone()
    return r["id"], (r["base_sell_price"] or 0.0)


def test_trigger_stamps_source_from_context(tmp_db_conn):
    conn = tmp_db_conn
    pid, base = _some_active_pid(conn)

    # set context, then change a watched field → trigger stamps our source
    conn.execute("UPDATE price_change_source SET source = ? WHERE id = 1", ("test-src",))
    conn.execute("UPDATE products SET base_sell_price = ? WHERE id = ?", (base + 11111.0, pid))
    conn.commit()

    row = conn.execute(
        "SELECT source FROM product_price_history "
        "WHERE product_id = ? AND field_name = 'base_sell_price' "
        "ORDER BY id DESC LIMIT 1", (pid,)
    ).fetchone()
    assert row["source"] == "test-src"


def test_source_defaults_null_when_context_unset(tmp_db_conn):
    conn = tmp_db_conn
    pid, base = _some_active_pid(conn)

    # context explicitly NULL → new price-history row gets NULL source
    conn.execute("UPDATE price_change_source SET source = NULL WHERE id = 1")
    conn.execute("UPDATE products SET base_sell_price = ? WHERE id = ?", (base + 22222.0, pid))
    conn.commit()

    row = conn.execute(
        "SELECT source FROM product_price_history "
        "WHERE product_id = ? AND field_name = 'base_sell_price' "
        "ORDER BY id DESC LIMIT 1", (pid,)
    ).fetchone()
    assert row["source"] is None


# ── models.update_product threads the source param ───────────────────────────

def _product_data(conn, pid, **overrides):
    r = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
    data = {
        "product_name": r["product_name"],
        "units_per_carton": r["units_per_carton"],
        "units_per_box": r["units_per_box"],
        "unit_type": r["unit_type"],
        "hard_to_sell": r["hard_to_sell"],
        "cost_price": r["cost_price"],
        "base_sell_price": r["base_sell_price"],
        "low_stock_threshold": r["low_stock_threshold"],
        "shopee_stock": r["shopee_stock"],
        "lazada_stock": r["lazada_stock"],
    }
    data.update(overrides)
    return data


def test_update_product_stamps_source(tmp_db):
    conn = get_connection()
    pid, base = _some_active_pid(conn)
    data = _product_data(conn, pid, base_sell_price=base + 33333.0)
    conn.close()

    models.update_product(pid, data, source="manual:tester ราคาตั้ง")

    conn = get_connection()
    row = conn.execute(
        "SELECT source FROM product_price_history "
        "WHERE product_id = ? AND field_name = 'base_sell_price' "
        "ORDER BY id DESC LIMIT 1", (pid,)
    ).fetchone()
    conn.close()
    assert row["source"] == "manual:tester ราคาตั้ง"


def test_update_product_default_source_null(tmp_db):
    conn = get_connection()
    pid, base = _some_active_pid(conn)
    data = _product_data(conn, pid, base_sell_price=base + 44444.0)
    conn.close()

    models.update_product(pid, data)  # no source → NULL

    conn = get_connection()
    row = conn.execute(
        "SELECT source FROM product_price_history "
        "WHERE product_id = ? AND field_name = 'base_sell_price' "
        "ORDER BY id DESC LIMIT 1", (pid,)
    ).fetchone()
    conn.close()
    assert row["source"] is None


# ── WAC sync stamps 'wac-sync' ───────────────────────────────────────────────

def test_wac_sync_stamps_source(tmp_db):
    """recalculate_product_wacc writes cost_price = live WACC. When that changes
    the stored cost_price, the new price-history row must carry source 'wac-sync'."""
    conn = get_connection()
    # a product with a real (>0) WACC in its ledger
    cand = conn.execute("""
        SELECT p.id FROM products p
         WHERE p.cost_price > 0
           AND EXISTS (SELECT 1 FROM product_cost_ledger l WHERE l.product_id = p.id)
         LIMIT 1
    """).fetchone()
    if cand is None:
        conn.close()
        pytest.skip("no product with cost ledger history in the test DB")
    pid = cand["id"]
    # corrupt cost so the recompute writes a different value (fires the trigger)
    conn.execute("UPDATE price_change_source SET source = NULL WHERE id = 1")
    conn.execute("UPDATE products SET cost_price = 0.01 WHERE id = ?", (pid,))
    conn.commit()
    conn.close()

    wacc = models.recalculate_product_wacc(pid)
    assert wacc and wacc > 0 and abs(wacc - 0.01) > 1e-9

    conn = get_connection()
    row = conn.execute(
        "SELECT source FROM product_price_history "
        "WHERE product_id = ? AND field_name = 'cost_price' "
        "ORDER BY id DESC LIMIT 1", (pid,)
    ).fetchone()
    conn.close()
    assert row["source"] == "wac-sync"


# ── 6 สีฝุ่น backfill (mig 130) ──────────────────────────────────────────────

def test_color_powder_backfill(tmp_db_conn):
    rows = tmp_db_conn.execute("""
        SELECT product_id, source FROM product_price_history
         WHERE product_id IN (472,473,474,475,476,477)
           AND field_name = 'base_sell_price'
           AND date(changed_at) = '2026-07-04'
    """).fetchall()
    assert len(rows) == 6
    for r in rows:
        assert r["source"] == "manual: Put ราคาตั้ง 2026-07-04 (ราคาตั้ง/ลัง ÷ 20)"
