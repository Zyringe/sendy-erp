"""Migration 122 — products.created_via provenance column.

Adds a nullable TEXT `created_via` column to `products` and backfills every
pre-existing row to `'legacy'`. No CHECK constraint (SQLite can't cleanly add
one via ALTER); value validity is enforced in app code in a later phase. App
code stamps `'manual'` (hand form) / `'smart_mapping'` (Smart Suggest approve)
on the create paths in Phase 3.

`tmp_db` clones the LIVE DB, which may already have mig 122 applied (it ships
applied to the real dev/live DB alongside this test) — so every test resets to
a clean pre-mig state via the rollback script first (matching the
`test_mig090_split_code_ghost_rows.py` pattern), then drives the scenario from
there. This keeps the tests correct whether or not the live DB already has the
migration.

Tests verify:
  - `run_pending_migrations` applies mig 122 and records it in
    `applied_migrations`.
  - Post-mig: column exists; every pre-existing product row is stamped
    `'legacy'`; none are NULL.
  - Idempotent: re-running the runner is a no-op (migration not re-executed,
    backfill unchanged) since it's already recorded as applied.
"""
import os
import sqlite3

import pytest

import database

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_122 = os.path.join(REPO, "data", "migrations", "122_product_created_via.sql")
ROLLBACK_122 = os.path.join(REPO, "data", "migrations", "122_product_created_via.rollback.sql")
FILENAME = "122_product_created_via.sql"


def _table_cols(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _reset_to_pre_mig(conn):
    """If mig 122 is already applied on the cloned live DB, roll it back so
    tests start from the pre-mig state (no created_via column)."""
    applied = conn.execute(
        "SELECT 1 FROM applied_migrations WHERE filename=?", (FILENAME,)
    ).fetchone()
    if applied is not None:
        with open(ROLLBACK_122, encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.commit()


@pytest.fixture
def conn(tmp_db):
    c = sqlite3.connect(tmp_db)
    c.row_factory = sqlite3.Row
    _reset_to_pre_mig(c)
    yield c
    c.close()


def test_migration_file_exists():
    assert os.path.exists(MIG_122), "migration 122 file must exist"


def test_pre_state_column_absent(conn):
    """After reset, no created_via column yet (pre-mig state)."""
    assert "created_via" not in _table_cols(conn, "products")


def test_column_added_and_backfilled_to_legacy(conn):
    total_before = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]

    ran = database.run_pending_migrations(conn, verbose=False)
    assert FILENAME in ran, f"mig 122 should have run; ran={ran}"

    assert "created_via" in _table_cols(conn, "products")

    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN created_via='legacy' THEN 1 ELSE 0 END) AS legacy, "
        "SUM(CASE WHEN created_via IS NULL THEN 1 ELSE 0 END) AS nullc "
        "FROM products"
    ).fetchone()

    assert row["total"] == total_before, "row count must be unchanged by the migration"
    assert row["legacy"] == total_before, "every pre-existing product must be stamped 'legacy'"
    assert row["nullc"] == 0, "no product row may be left with a NULL created_via"


def test_applied_migrations_has_122(conn):
    database.run_pending_migrations(conn, verbose=False)
    row = conn.execute(
        "SELECT 1 FROM applied_migrations WHERE filename=?", (FILENAME,)
    ).fetchone()
    assert row is not None, "applied_migrations must record mig 122"


def test_rerun_is_idempotent_noop(conn):
    """Running the migration runner a second time must not error and must
    not change the backfill (the file is already recorded as applied, so the
    runner skips it entirely — it does not re-execute the ALTER/UPDATE)."""
    first = database.run_pending_migrations(conn, verbose=False)
    assert FILENAME in first

    legacy_after_first = conn.execute(
        "SELECT SUM(CASE WHEN created_via='legacy' THEN 1 ELSE 0 END) FROM products"
    ).fetchone()[0]

    second = database.run_pending_migrations(conn, verbose=False)  # should be a no-op, no error
    assert FILENAME not in second, "mig 122 must not re-run once already recorded"

    legacy_after_second = conn.execute(
        "SELECT SUM(CASE WHEN created_via='legacy' THEN 1 ELSE 0 END) FROM products"
    ).fetchone()[0]
    assert legacy_after_second == legacy_after_first, "re-run must not change the backfill"
