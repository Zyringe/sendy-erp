"""Migration 197 — widen marketplace_orders' platform CHECK to accept 'tiktok'.

Plan: projects/tiktok-order-import/tiktok-order-import-plan.md, PR-1 step 1.
SQLite cannot ALTER a CHECK, so the migration rebuilds the table (mig 175's
shape). What must survive the rebuild, byte for byte:
  - every marketplace_orders row, ids included, and the column list;
  - every marketplace_order_items row. They hang off
    `order_id ... ON DELETE CASCADE`, so a DROP with foreign_keys ON would
    wipe them; the migration turns foreign_keys OFF before BEGIN;
  - the platform_stock_deductions keys pointing at order ids (value
    references, no FK);
  - sqlite_sequence.seq. A rebuild resets it to MAX(id), which would let a new
    order reuse an id a deduction row still names.

The migration runs through the real runner (`run_pending_migrations`), on an
empty_db clone reset to pre-197 by the guarded rollback script. Rollback is a
Python script, not SQL: under the sqlite3 CLI a failing CHECK does not stop a
script, which would then DROP the table and COMMIT with every order gone.
"""
import importlib.util
import os
import sqlite3

import pytest

import database

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_197 = "197_marketplace_orders_tiktok.sql"
ROLLBACK_SCRIPT = os.path.join(REPO, "scripts", "rollback_197_marketplace_orders_tiktok.py")


def _rollback_mod():
    spec = importlib.util.spec_from_file_location("rollback_197", ROLLBACK_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stamp_all_but_197(conn):
    """empty_db carries no applied_migrations rows, which would send the runner
    down its bootstrap path (stamp everything, run nothing). Stamp every file
    except 197 so the runner applies exactly 197."""
    for name in database._list_migration_files():
        if name != MIG_197:
            conn.execute("INSERT OR IGNORE INTO applied_migrations (filename, applied_by, sha256) "
                         "VALUES (?, 'test', 'x')", (name,))
    conn.execute("DELETE FROM applied_migrations WHERE filename = ?", (MIG_197,))
    conn.commit()


@pytest.fixture
def pre197_conn(empty_db_conn):
    _rollback_mod().rollback(empty_db_conn)
    _stamp_all_but_197(empty_db_conn)
    return empty_db_conn


def _seed(conn):
    """3 orders with lines + one deduction row, then delete the newest order so
    seq (4) runs ahead of MAX(id) (3), the shape prod has (7884 vs 7868 on the
    dev DB, 2026-09-30)."""
    conn.execute("INSERT INTO products (id, product_name) VALUES (9101, 'p')")
    conn.execute("INSERT INTO platform_skus (id, platform, product_name, variation_id, stock) "
                 "VALUES (9201, 'shopee', 'x', 'V1', 5)")
    for oid, plat, sn in ((1, 'shopee', 'S1'), (2, 'lazada', 'L1'), (3, 'shopee', 'S2'),
                          (4, 'shopee', 'S3')):
        conn.execute(
            "INSERT INTO marketplace_orders (id, platform, order_sn, status, item_total, "
            "actual_payout, settled_at, settlement_source, payout_batch_id, payout_id) "
            "VALUES (?,?,?,'done',100,90,'2026-09-01','income',NULL,NULL)", (oid, plat, sn))
        conn.execute(
            "INSERT INTO marketplace_order_items (order_id, platform, order_sn, line_key, "
            "internal_product_id, qty) VALUES (?,?,?,?,9101,1)", (oid, plat, sn, f'k{oid}'))
    conn.execute("INSERT INTO platform_stock_deductions (source_table, source_id, platform_sku_id, "
                 "units) VALUES ('marketplace_orders', 3, 9201, 1)")
    conn.execute("DELETE FROM marketplace_orders WHERE id = 4")
    conn.commit()


def _snapshot(conn):
    return {
        'orders': [tuple(r) for r in conn.execute("SELECT * FROM marketplace_orders ORDER BY id")],
        'cols': [tuple(r) for r in conn.execute("PRAGMA table_info(marketplace_orders)")],
        'items': [tuple(r) for r in conn.execute("SELECT * FROM marketplace_order_items ORDER BY id")],
        'deductions': [tuple(r) for r in conn.execute(
            "SELECT source_table, source_id, platform_sku_id, units FROM platform_stock_deductions "
            "ORDER BY 1, 2, 3")],
        'seq': conn.execute("SELECT seq FROM sqlite_sequence WHERE name='marketplace_orders'").fetchone()[0],
        'indexes': sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='marketplace_orders' "
            "AND sql IS NOT NULL")),
    }


def _insert_tiktok(conn):
    conn.execute("INSERT INTO marketplace_orders (platform, order_sn) VALUES ('tiktok', 'T1')")


def test_rebuild_preserves_every_row_and_the_sequence(pre197_conn):
    conn = pre197_conn
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    _seed(conn)
    before = _snapshot(conn)
    fk_before = conn.execute("PRAGMA foreign_key_check").fetchall()
    # Controls: the snapshot really holds what the checks below compare.
    assert len(before['orders']) == 3 and len(before['items']) == 3
    assert before['seq'] == 4
    assert len(before['indexes']) == 4
    with pytest.raises(sqlite3.IntegrityError):
        _insert_tiktok(conn)
    conn.rollback()

    ran = database.run_pending_migrations(conn, verbose=False)

    assert ran == [MIG_197]
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert _snapshot(conn) == before
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == fk_before


def test_tiktok_accepted_and_garbage_still_refused(pre197_conn):
    conn = pre197_conn
    database.run_pending_migrations(conn, verbose=False)
    _insert_tiktok(conn)
    conn.commit()
    assert conn.execute("SELECT platform FROM marketplace_orders WHERE order_sn='T1'").fetchone()[0] == 'tiktok'
    for bad in ('thaimart', 'ebay'):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO marketplace_orders (platform, order_sn) VALUES (?, 'X')", (bad,))


def test_new_order_id_continues_after_the_old_sequence(pre197_conn):
    """The reason seq is restored: id 4 was used and deleted, so the next order
    must get 5, never 4 again."""
    conn = pre197_conn
    _seed(conn)
    database.run_pending_migrations(conn, verbose=False)
    _insert_tiktok(conn)
    conn.commit()
    assert conn.execute("SELECT id FROM marketplace_orders WHERE order_sn='T1'").fetchone()[0] == 5


def test_sequence_kept_when_every_order_was_deleted(pre197_conn):
    """An emptied table keeps its sqlite_sequence row; the rebuilt one would
    start without one, so the migration must INSERT it, not only UPDATE it."""
    conn = pre197_conn
    _seed(conn)
    conn.execute("DELETE FROM marketplace_orders")
    conn.commit()
    database.run_pending_migrations(conn, verbose=False)
    assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name='marketplace_orders'").fetchone()[0] == 4


def test_rollback_round_trip_restores_rows_and_refuses_tiktok_again(pre197_conn):
    conn = pre197_conn
    _seed(conn)
    before = _snapshot(conn)
    database.run_pending_migrations(conn, verbose=False)

    _rollback_mod().rollback(conn)

    assert _snapshot(conn) == before
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM applied_migrations WHERE filename=?",
                        (MIG_197,)).fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError):
        _insert_tiktok(conn)


def test_rollback_refuses_before_touching_anything_when_tiktok_rows_exist(pre197_conn):
    conn = pre197_conn
    _seed(conn)
    database.run_pending_migrations(conn, verbose=False)
    _insert_tiktok(conn)
    conn.commit()
    before = _snapshot(conn)

    with pytest.raises(SystemExit):
        _rollback_mod().rollback(conn)

    assert _snapshot(conn) == before
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM applied_migrations WHERE filename=?",
                        (MIG_197,)).fetchone()[0] == 1
