"""Migration 061 — product_code_mapping becomes unit-aware.

- up: adds `bsn_unit TEXT NOT NULL DEFAULT ''`, swaps single-`bsn_code`
  UNIQUE for composite UNIQUE(bsn_code, bsn_unit), backfills every row
  bsn_unit='' (catch-all = pre-061 behavior), preserves all data incl.
  ignore_reason / ids.
- down: restores single-`bsn_code` UNIQUE, keeps only catch-all rows
  (non-'' overrides intentionally dropped), deletes the applied_migrations
  row.
- runner records 061 exactly once (no self-insert).
"""
import os
import sqlite3

import pytest

import database

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG = os.path.join(REPO, "data", "migrations", "061_mapping_unit_aware.sql")
ROLLBACK = os.path.join(
    REPO, "data", "migrations", "061_mapping_unit_aware.rollback.sql"
)


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


def _cols(conn):
    return {r[1]: r for r in conn.execute(
        "PRAGMA table_info(product_code_mapping)")}


def _reset_pre061(conn):
    """Force product_code_mapping back to the exact PRE-061 shape so the
    test is deterministic whether or not live has migration 061 applied."""
    conn.executescript("""
        DROP TABLE IF EXISTS product_code_mapping;
        CREATE TABLE product_code_mapping (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            bsn_code    TEXT UNIQUE NOT NULL,
            bsn_name    TEXT NOT NULL,
            product_id  INTEGER REFERENCES products(id),
            is_ignored  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        ALTER TABLE product_code_mapping ADD COLUMN ignore_reason TEXT;
    """)
    conn.commit()


def test_up_adds_bsn_unit_and_composite_unique(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _reset_pre061(conn)
    _apply(conn, MIG)

    cols = _cols(conn)
    assert "bsn_unit" in cols
    # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk)
    assert cols["bsn_unit"][3] == 1                       # NOT NULL
    assert cols["bsn_unit"][4] in ("''", "'' "), cols["bsn_unit"][4]

    # composite UNIQUE: same code, different unit → OK
    pid = conn.execute(
        "SELECT id FROM products WHERE is_active=1 LIMIT 1").fetchone()[0]
    conn.execute("INSERT INTO product_code_mapping "
                 "(bsn_code,bsn_name,product_id,bsn_unit) "
                 "VALUES ('ZUNIT9','n',?, 'แผง')", (pid,))
    conn.execute("INSERT INTO product_code_mapping "
                 "(bsn_code,bsn_name,product_id,bsn_unit) "
                 "VALUES ('ZUNIT9','n',?, 'ตัว')", (pid,))
    conn.commit()
    # same (code, unit) → IntegrityError (composite UNIQUE enforced)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO product_code_mapping "
                     "(bsn_code,bsn_name,product_id,bsn_unit) "
                     "VALUES ('ZUNIT9','n',?, 'แผง')", (pid,))
    conn.rollback()
    # old single-bsn_code UNIQUE is GONE (two ZUNIT9 rows already coexist)
    n = conn.execute("SELECT COUNT(*) FROM product_code_mapping "
                     "WHERE bsn_code='ZUNIT9'").fetchone()[0]
    assert n == 2
    conn.close()


def test_up_backfills_catchall_and_preserves_data(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _reset_pre061(conn)
    # seed a row with ignore_reason to prove preservation
    conn.execute("INSERT INTO product_code_mapping "
                 "(bsn_code,bsn_name,product_id,is_ignored,ignore_reason) "
                 "VALUES ('ZPRES1','keepme',NULL,1,'because reasons')")
    conn.commit()
    pre_n = conn.execute(
        "SELECT COUNT(*) FROM product_code_mapping").fetchone()[0]
    pre_ids = conn.execute(
        "SELECT id FROM product_code_mapping ORDER BY id").fetchall()

    _apply(conn, MIG)

    assert conn.execute(
        "SELECT COUNT(*) FROM product_code_mapping").fetchone()[0] == pre_n
    assert conn.execute("SELECT COUNT(*) FROM product_code_mapping "
                        "WHERE bsn_unit<>''").fetchone()[0] == 0
    assert conn.execute(
        "SELECT id FROM product_code_mapping ORDER BY id"
    ).fetchall() == pre_ids
    r = conn.execute("SELECT bsn_name,is_ignored,ignore_reason,bsn_unit "
                     "FROM product_code_mapping WHERE bsn_code='ZPRES1'"
                     ).fetchone()
    assert r == ("keepme", 1, "because reasons", "")
    conn.close()


def test_down_restores_single_unique_and_drops_overrides(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _reset_pre061(conn)
    _apply(conn, MIG)
    pid = conn.execute(
        "SELECT id FROM products WHERE is_active=1 LIMIT 1").fetchone()[0]
    conn.execute("INSERT INTO product_code_mapping "
                 "(bsn_code,bsn_name,product_id,bsn_unit) "
                 "VALUES ('ZDOWN1','c',?, '')", (pid,))
    conn.execute("INSERT INTO product_code_mapping "
                 "(bsn_code,bsn_name,product_id,bsn_unit) "
                 "VALUES ('ZDOWN1','o',?, 'แผง')", (pid,))
    conn.execute("INSERT OR IGNORE INTO applied_migrations(filename) "
                 "VALUES ('061_mapping_unit_aware.sql')")
    conn.commit()

    _apply(conn, ROLLBACK)

    # override dropped, catch-all kept
    rows = conn.execute("SELECT bsn_name FROM product_code_mapping "
                        "WHERE bsn_code='ZDOWN1'").fetchall()
    assert rows == [("c",)]
    # single-bsn_code UNIQUE restored
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO product_code_mapping "
                     "(bsn_code,bsn_name,product_id) VALUES "
                     "('ZDOWN1','dup',?)", (pid,))
    conn.rollback()
    assert "bsn_unit" not in _cols(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM applied_migrations WHERE "
        "filename='061_mapping_unit_aware.sql'").fetchone()[0] == 0
    conn.close()


def test_runner_records_061_exactly_once(empty_db, tmp_path, monkeypatch):
    conn = sqlite3.connect(empty_db)
    conn.row_factory = sqlite3.Row
    # sentinel → runner takes the PENDING path (not bootstrap-backfill)
    conn.execute("INSERT INTO applied_migrations(filename,applied_by) "
                 "VALUES ('000_sentinel.sql','test')")
    conn.commit()
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    import shutil
    shutil.copy(MIG, mig_dir / "061_mapping_unit_aware.sql")
    monkeypatch.setattr(database, "MIGRATIONS_DIR", str(mig_dir))

    ran = database.run_pending_migrations(conn, verbose=False)

    assert ran == ["061_mapping_unit_aware.sql"]
    assert conn.execute(
        "SELECT COUNT(*) FROM applied_migrations WHERE "
        "filename='061_mapping_unit_aware.sql'").fetchone()[0] == 1
    assert "bsn_unit" in {r[1] for r in conn.execute(
        "PRAGMA table_info(product_code_mapping)")}
    conn.close()


# ── Regression: the product_code_mapping rebuild must not orphan the
#    refresh_brand_kind_on_product_brand_change trigger (migration 021).
#
#    Bug (prod-down 2026-05-19): trigger 021's body references
#    product_code_mapping. 061's DROP TABLE / ALTER RENAME made SQLite
#    re-validate the trigger mid-swap on Railway's volume DB →
#    "error in trigger refresh_brand_kind_on_product_brand_change:
#     no such table: main.product_code_mapping" → boot crash-loop.
#    Local never re-runs 061 (already in applied_migrations) so it was
#    invisible until deploy. Fix: 061 drops the trigger before the swap
#    and recreates it verbatim after. This test locks that contract by
#    asserting the trigger survives 061 AND still fires end-to-end. ──────

TRIGGER = "refresh_brand_kind_on_product_brand_change"


def _trigger_sql(conn):
    r = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
        (TRIGGER,)).fetchone()
    return r[0] if r else None


def test_061_preserves_brand_kind_trigger(tmp_db):
    """061 must leave trigger 021 present and pointing at the rebuilt
    product_code_mapping. tmp_db is a copy of the live DB, which carries
    trigger 021 — the exact precondition that crashed Railway."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")

    assert _trigger_sql(conn) is not None, (
        "precondition: live DB must carry trigger 021 for this test to "
        "mean anything")

    _reset_pre061(conn)          # pre-061 table shape; trigger 021 untouched
    _apply(conn, MIG)            # must NOT raise (the prod failure point)

    sql = _trigger_sql(conn)
    assert sql is not None, "061 dropped the trigger and never recreated it"
    assert "product_code_mapping" in sql
    assert "bsn_unit" in {r[1] for r in conn.execute(
        "PRAGMA table_info(product_code_mapping)")}
    conn.close()


def test_061_trigger_still_fires_after_rebuild(tmp_db):
    """End-to-end: after 061, updating products.brand_id must still
    refresh express_sales.brand_kind through the rebuilt mapping."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _reset_pre061(conn)
    _apply(conn, MIG)

    own = conn.execute(
        "SELECT id FROM brands WHERE is_own_brand=1 LIMIT 1").fetchone()[0]
    third = conn.execute(
        "SELECT id FROM brands WHERE is_own_brand=0 LIMIT 1").fetchone()[0]
    pid = conn.execute(
        "SELECT id FROM products WHERE is_active=1 LIMIT 1").fetchone()[0]

    conn.execute("INSERT INTO product_code_mapping "
                 "(bsn_code,bsn_name,product_id,bsn_unit) "
                 "VALUES ('ZTRG1','t',?, '')", (pid,))
    conn.execute(
        "INSERT INTO express_sales "
        "(batch_id,doc_no,line_no,doc_type,date_iso,company_id,"
        " product_code,brand_kind) "
        "VALUES (1,'ZD1',1,'IV','2026-05-19',1,'ZTRG1','stale')")
    conn.execute("UPDATE products SET brand_id=? WHERE id=?", (third, pid))
    conn.execute("UPDATE products SET brand_id=? WHERE id=?", (own, pid))
    conn.commit()

    assert conn.execute(
        "SELECT brand_kind FROM express_sales WHERE product_code='ZTRG1'"
    ).fetchone()[0] == "own"
    conn.close()
