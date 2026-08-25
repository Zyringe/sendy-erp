"""Migration 171 — backfill products.packaging_th from packaging_short.

Built on a purpose-made SQLite file rather than the `empty_db` fixture: that
fixture clones the LIVE local DB, which may already have 171 applied, and a
migration test that inherits its own post-state cannot fail.

The three exclusions are the point of the migration, so each has its own case:
a name that does NOT already carry the word (filling would ADD text), a row with
BOTH packaging columns NULL (filling would move the generated sku_code — issue
#383), and a row whose packaging_th is already curated (never overwrite).
"""
import sqlite3
from pathlib import Path

import pytest

MIG = Path(__file__).resolve().parents[1] / "data/migrations/171_backfill_packaging_th.sql"
ROLLBACK = Path(__file__).resolve().parents[1] / "data/migrations/171_backfill_packaging_th.rollback.sql"

ROWS = [
    # id, product_name,                          packaging_th, packaging_short, note
    (1, "กลอน Sendai #230-4in (แผง)",   None,  "PN", "fills"),
    (2, "บานพับ Sendai TOP-8in (ถุง)",  None,  "BG", "fills"),
    (3, "ตะไบ Sendai 6in (อัดแผง)",     None,  "SP", "fills"),
    (4, "กลอน Sendai #231-4in",         None,  "PN", "name lacks the word -> skip"),
    (5, "กลอน Sendai #520-6in (ตัว)",   None,  None, "both NULL -> skip (sku_code hazard)"),
    (6, "กลอน Sendai #232-4in (แผง)",  "แผง",  "PN", "already curated -> untouched"),
]
FILLED = {1: "แผง", 2: "ถุง", 3: "อัดแผง"}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "mig171.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, product_name TEXT,"
                 " packaging_th TEXT, packaging_short TEXT)")
    conn.execute("CREATE TABLE applied_migrations (filename TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO applied_migrations VALUES ('171_backfill_packaging_th.sql')")
    conn.executemany("INSERT INTO products VALUES (?,?,?,?)",
                     [(r[0], r[1], r[2], r[3]) for r in ROWS])
    conn.commit()
    return conn, path


def _pkg(conn):
    return {r[0]: r[1] for r in conn.execute("SELECT id, packaging_th FROM products")}


def test_it_fills_only_the_rows_whose_name_already_carries_the_word(db):
    conn, _ = db
    before = _pkg(conn)
    # CONTROL: the pre-state must actually be NULL, or "it got filled" proves nothing
    assert before[1] is None and before[4] is None and before[5] is None
    assert before[6] == "แผง"

    conn.executescript(MIG.read_text(encoding="utf-8"))
    after = _pkg(conn)

    assert after[1] == "แผง"
    assert after[2] == "ถุง"
    assert after[3] == "อัดแผง"
    assert after[4] is None, "name has no (แผง) — filling would ADD text to the name"
    assert after[5] is None, "both columns NULL — filling would move the generated sku_code"
    assert after[6] == "แผง", "a curated value must never be overwritten"

    touched = {r[0] for r in conn.execute("SELECT product_id FROM mig171_packaging_th_backfill")}
    assert touched == set(FILLED), touched


def test_it_never_edits_product_name_itself(db):
    conn, _ = db
    before = dict(conn.execute("SELECT id, product_name FROM products").fetchall())
    conn.executescript(MIG.read_text(encoding="utf-8"))
    assert dict(conn.execute("SELECT id, product_name FROM products").fetchall()) == before


def test_rollback_restores_exactly_the_rows_it_filled(db):
    conn, _ = db
    conn.executescript(MIG.read_text(encoding="utf-8"))
    assert _pkg(conn)[1] == "แผง"          # CONTROL: it really was applied

    conn.executescript(ROLLBACK.read_text(encoding="utf-8"))
    after = _pkg(conn)
    for pid in FILLED:
        assert after[pid] is None, pid
    assert after[6] == "แผง", "the pre-existing curated value must survive the rollback"
    assert not conn.execute(
        "SELECT name FROM sqlite_master WHERE name='mig171_packaging_th_backfill'").fetchall()


def test_the_migration_is_rerunnable(db):
    """The runner never repeats a migration, but a rehearsal on a snapshot copy does.
    A non-rerunnable migration fights simulate-before-mutate."""
    conn, _ = db
    conn.executescript(MIG.read_text(encoding="utf-8"))
    first = _pkg(conn)
    conn.executescript(MIG.read_text(encoding="utf-8"))   # must not raise
    assert _pkg(conn) == first
    # ⚠ the record must SURVIVE the second run. A drop-first snapshot table would be
    # rebuilt empty here, and the rollback below would then restore nothing at all.
    touched = {r[0] for r in conn.execute("SELECT product_id FROM mig171_packaging_th_backfill")}
    assert touched == set(FILLED), touched
    conn.executescript(ROLLBACK.read_text(encoding="utf-8"))
    assert all(_pkg(conn)[pid] is None for pid in FILLED)


def test_rollback_leaves_a_value_an_operator_curated_afterwards(db):
    """Keying the rollback on product_id ALONE would blank a row someone has since
    corrected through /naming — destroying the correction without even restoring what
    171 put there. Only rows still holding 171's own value may be undone."""
    conn, _ = db
    conn.executescript(MIG.read_text(encoding="utf-8"))
    assert _pkg(conn)[1] == "แผง"                      # CONTROL: 171 really filled it

    conn.execute("UPDATE products SET packaging_th='ถุง' WHERE id=1")   # operator edit
    conn.commit()
    conn.executescript(ROLLBACK.read_text(encoding="utf-8"))

    assert _pkg(conn)[1] == "ถุง", "rollback destroyed a later curated value"
    assert _pkg(conn)[2] is None, "an untouched 171 row must still roll back"


def test_rollback_un_records_the_migration_so_it_can_run_again(db):
    """Reverting the DATA but leaving the filename in applied_migrations makes the
    runner skip 171 forever — reverted and un-reappliable. Migration 170's rollback
    deletes its own row for the same reason."""
    conn, _ = db
    conn.executescript(MIG.read_text(encoding="utf-8"))
    # CONTROL: it is recorded before the rollback, so the assertion below can fail
    assert conn.execute("SELECT 1 FROM applied_migrations WHERE filename="
                        "'171_backfill_packaging_th.sql'").fetchall()
    conn.executescript(ROLLBACK.read_text(encoding="utf-8"))
    assert not conn.execute("SELECT 1 FROM applied_migrations WHERE filename="
                            "'171_backfill_packaging_th.sql'").fetchall()


def test_a_failure_partway_through_leaves_NOTHING_behind(db, tmp_path, monkeypatch):
    """Through the REAL runner, and WITHOUT the test supplying the rollback.

    The first version called conn.rollback() itself before asserting, which proved only
    that the transaction was rollbackable — not that run_pending_migrations rolls it
    back (Codex, 2026-08-24). The runner's own comment claimed SQLite had already done
    so; it had not, and the open transaction kept the write lock.
    """
    import database

    conn, path = db
    conn.execute("DELETE FROM applied_migrations")
    conn.execute("INSERT INTO applied_migrations VALUES ('000_seed.sql')")  # skip bootstrap
    conn.commit()
    before = {r[0]: r[1] for r in conn.execute("SELECT id, packaging_th FROM products")}

    broken_dir = tmp_path / "migs"
    broken_dir.mkdir()
    broken = MIG.read_text(encoding="utf-8").replace(
        "UPDATE products\n   SET packaging_th =",
        "UPDATE products\n   SET no_such_column =")
    assert "no_such_column" in broken, "failure injection did not patch the script"
    (broken_dir / "999_broken.sql").write_text(broken, encoding="utf-8")
    monkeypatch.setattr(database, "MIGRATIONS_DIR", str(broken_dir))

    with pytest.raises(sqlite3.OperationalError):
        database.run_pending_migrations(conn, verbose=False)

    # the runner must have released the transaction itself
    assert conn.in_transaction is False, "the runner left the write transaction OPEN"
    # ...and an INDEPENDENT connection must see no partial state
    other = sqlite3.connect(path)
    assert {r[0]: r[1] for r in other.execute("SELECT id, packaging_th FROM products")} == before
    assert not other.execute(
        "SELECT name FROM sqlite_master WHERE name='mig171_packaging_th_backfill'").fetchall(), \
        "the snapshot table survived a failed migration"
    assert not other.execute(
        "SELECT 1 FROM applied_migrations WHERE filename='999_broken.sql'").fetchall()
    other.close()
