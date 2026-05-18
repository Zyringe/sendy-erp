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
