"""Migration 175 — order-driven deduction: widen platform_stock_deductions'
source_table CHECK to accept 'marketplace_orders', and add
platform_skus.stock_as_of as the per-listing deduction baseline.

Design doc: projects/order-driven-platform-deduction/plan.md, Task 1.1.
Builds on mig 172 (data/migrations/172_platform_stock_deductions.sql), which
created platform_stock_deductions with a narrow
CHECK(source_table IN ('sales_transactions')).

Tests (deterministic, on the schema-only empty_db reconstructed to pre-175):
  1. Applying the migration widens the CHECK (accepts 'marketplace_orders',
     still rejects garbage) and backfills stock_as_of = imported_at for a
     listing with no deduction provenance. A listing WITH provenance whose
     deduction predates its snapshot also lands on imported_at (the normal
     case — the snapshot is already ahead of the deduction).
  2. The Codex-HIGH cutover fix: a listing whose recorded deduction
     created_at is AFTER its imported_at backfills to that deduction's
     created_at instead (the walk kept deducting between mig 172 and this
     migration; the baseline must not be older than a sale already
     deducted). A sibling listing with no provenance keeps imported_at.
  3. Rollback rebuilds the narrow CHECK from the CURRENT table (so a
     'sales_transactions' row written after the forward migration survives,
     while any 'marketplace_orders' row is dropped — that IS the rollback's
     meaning) and drops stock_as_of; sqlite_master for the table+index comes
     back byte-identical to the pre-migration state.
"""
import os
import sqlite3

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_172 = os.path.join(REPO, "data", "migrations", "172_platform_stock_deductions.sql")
MIG_175 = os.path.join(REPO, "data", "migrations", "175_order_driven_deduction.sql")
ROLLBACK_175 = os.path.join(
    REPO, "data", "migrations", "175_order_driven_deduction.rollback.sql")


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


def _platform_skus_cols(conn):
    return {r[1] for r in conn.execute("PRAGMA table_info(platform_skus)")}


def _deductions_ddl(conn):
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='platform_stock_deductions'"
    ).fetchone()[0]
    index_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND name='idx_platform_stock_deductions_sku'"
    ).fetchone()[0]
    return table_sql, index_sql


def _normalize_ddl(sql):
    """Two cosmetic-only sources of divergence, neither a schema change:

    1. SQLite's `ALTER TABLE x RENAME TO platform_stock_deductions` rewrites
       the stored CREATE TABLE text with the new name double-quoted (verified
       empirically: `CREATE TABLE foo_new (...)` + RENAME TO foo yields
       `CREATE TABLE "foo" (...)`, confirmed against a throwaway :memory: DB)
       even though the table is otherwise identical. The rebuild-via-rename
       recipe this migration and its rollback both use (mandated by the task
       brief, mirrors mig 140) means the table's DDL text picks up this
       quoting the FIRST time it is ever rebuilt and never loses it again —
       including on the pristine forward-only path, no rollback involved. The
       original mig-172 table was created directly (no rename), so it never
       has it.
    2. CREATE INDEX/TABLE statements are stored verbatim as typed (SQLite does
       NOT reformat these the way it does a renamed table's own CREATE TABLE).
       mig 172's original index is wrapped across two lines; this migration's
       verbatim-per-brief text is one line. Same index, different line-wrap.

    Collapsing whitespace + stripping the one quoted-name artifact verifies
    actual schema shape (columns, CHECK, PK, indexed column) rather than
    either quirk."""
    sql = sql.replace(
        'CREATE TABLE "platform_stock_deductions"',
        "CREATE TABLE platform_stock_deductions",
    )
    return " ".join(sql.split())


def _seed_platform_sku(conn, imported_at):
    cur = conn.execute(
        "INSERT INTO platform_skus (platform, product_name, imported_at) "
        "VALUES ('shopee', ?, ?)",
        (f"test listing {imported_at}", imported_at),
    )
    conn.commit()
    return cur.lastrowid


def _seed_deduction(conn, source_table, source_id, platform_sku_id, units, created_at):
    conn.execute(
        "INSERT INTO platform_stock_deductions "
        "(source_table, source_id, platform_sku_id, units, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (source_table, source_id, platform_sku_id, units, created_at),
    )
    conn.commit()


@pytest.fixture
def pre175_conn(empty_db_conn):
    """Reconstruct the guaranteed pre-175 state, deterministically —
    independent of whether the worktree's live DB (empty_db_conn's clone
    source) has already had migration 175 applied THIS session.

    Unlike pre134_conn (tests/test_mig134_product_generic_standins.py,
    written long after mig 134 had been live on every dev machine, so
    empty_db_conn's clone-source was ALWAYS already migrated), migration 175
    is new this session: the live DB starts pre-175, but the moment anything
    (including this test file's own rehearsal, or `from app import app`)
    applies it, the change is permanent, on disk. A fixture that branches on
    "is stock_as_of present, then roll back" is deceptive: mig 175's rollback
    ALSO rebuilds platform_stock_deductions via CREATE+RENAME, so a fixture
    built that way inherits the rename-quoting artifact (see
    _normalize_ddl) whenever the live DB has already been migrated, but NOT
    on a still-pristine DB — a test comparing raw DDL text would then pass
    or fail depending on execution history, not on the migration's
    correctness (caught live: green before `from app import app` ran once
    against this worktree's DB, red after).

    So don't inherit platform_stock_deductions' shape from empty_db_conn at
    all: drop it and rebuild from mig 172's OWN forward file, the pristine,
    never-renamed definition. That is the same object regardless of session
    history. stock_as_of only ever needs a plain presence check (ALTER TABLE
    ADD/DROP COLUMN never triggers the rename-quoting behavior, so
    platform_skus' own CREATE TABLE text is untouched by any of this)."""
    conn = empty_db_conn
    conn.executescript(
        "DROP INDEX IF EXISTS idx_platform_stock_deductions_sku;"
        "DROP TABLE IF EXISTS platform_stock_deductions;"
    )
    _apply(conn, MIG_172)
    if "stock_as_of" in _platform_skus_cols(conn):
        conn.execute("ALTER TABLE platform_skus DROP COLUMN stock_as_of")
    conn.commit()
    return conn


def test_pre_state_is_narrow_check_no_stock_as_of(pre175_conn):
    """Sanity on the fixture itself, not the migration."""
    table_sql, _ = _deductions_ddl(pre175_conn)
    assert "marketplace_orders" not in table_sql
    assert "sales_transactions" in table_sql
    assert "stock_as_of" not in _platform_skus_cols(pre175_conn)


def test_apply_widens_check_and_backfills_stock_as_of(pre175_conn):
    conn = pre175_conn

    sku_no_ded = _seed_platform_sku(conn, imported_at="2026-08-02 09:00:00")
    sku_stale_ded = _seed_platform_sku(conn, imported_at="2026-08-01 10:00:00")
    # Deduction predates the snapshot (normal case) — must NOT move stock_as_of.
    _seed_deduction(
        conn, "sales_transactions", 1, sku_stale_ded, 3, "2026-07-01 00:00:00")

    assert conn.execute(
        "SELECT COUNT(*) FROM platform_stock_deductions"
    ).fetchone()[0] == 1  # vacuity guard: the seed actually landed

    _apply(conn, MIG_175)

    # CHECK widened: 'marketplace_orders' now accepted, garbage still rejected.
    conn.execute(
        "INSERT INTO platform_stock_deductions "
        "(source_table, source_id, platform_sku_id, units) "
        "VALUES ('marketplace_orders', 999, ?, 2)",
        (sku_no_ded,),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO platform_stock_deductions "
            "(source_table, source_id, platform_sku_id, units) "
            "VALUES ('garbage', 998, ?, 1)",
            (sku_no_ded,),
        )
    conn.rollback()

    # Backfill: no provenance -> stock_as_of = imported_at.
    row = conn.execute(
        "SELECT stock_as_of FROM platform_skus WHERE id = ?", (sku_no_ded,)
    ).fetchone()
    assert row[0] == "2026-08-02 09:00:00"

    # Backfill: provenance older than the snapshot -> stays imported_at.
    row = conn.execute(
        "SELECT stock_as_of FROM platform_skus WHERE id = ?", (sku_stale_ded,)
    ).fetchone()
    assert row[0] == "2026-08-01 10:00:00"


def test_apply_anchors_baseline_to_deduction_when_newer_codex_high(pre175_conn):
    """The Codex-HIGH cutover fix: a listing whose deduction happened AFTER
    its last snapshot must not backfill to the (older, would-double-deduct)
    imported_at."""
    conn = pre175_conn

    sku_late_ded = _seed_platform_sku(conn, imported_at="2026-08-01 08:00:00")
    sku_no_ded = _seed_platform_sku(conn, imported_at="2026-08-02 09:00:00")
    _seed_deduction(
        conn, "sales_transactions", 42, sku_late_ded, 5, "2026-08-24 15:30:00")

    assert conn.execute(
        "SELECT COUNT(*) FROM platform_stock_deductions"
    ).fetchone()[0] == 1  # vacuity guard

    _apply(conn, MIG_175)

    row = conn.execute(
        "SELECT stock_as_of FROM platform_skus WHERE id = ?", (sku_late_ded,)
    ).fetchone()
    assert row[0] == "2026-08-24 15:30:00"  # anchored to the deduction, not imported_at

    row = conn.execute(
        "SELECT stock_as_of FROM platform_skus WHERE id = ?", (sku_no_ded,)
    ).fetchone()
    assert row[0] == "2026-08-02 09:00:00"  # sibling with no provenance: unaffected


def test_rollback_restores_pre_state_byte_identical(pre175_conn):
    conn = pre175_conn
    pre_table_sql, pre_index_sql = _deductions_ddl(conn)

    sku_a = _seed_platform_sku(conn, imported_at="2026-08-01 10:00:00")
    sku_b = _seed_platform_sku(conn, imported_at="2026-08-02 09:00:00")
    _seed_deduction(conn, "sales_transactions", 1, sku_a, 3, "2026-07-01 00:00:00")

    _apply(conn, MIG_175)
    _seed_deduction(conn, "marketplace_orders", 555, sku_b, 2, "2026-08-24 12:00:00")

    # sanity: forward actually applied before we roll it back
    post_table_sql, _ = _deductions_ddl(conn)
    assert "marketplace_orders" in post_table_sql
    assert "stock_as_of" in _platform_skus_cols(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM platform_stock_deductions"
    ).fetchone()[0] == 2  # vacuity guard: both rows present pre-rollback

    _apply(conn, ROLLBACK_175)

    rb_table_sql, rb_index_sql = _deductions_ddl(conn)
    # Control: prove the normalizer isn't vacuously making everything equal —
    # the RAW text must actually differ (rename-quoting + index line-wrap are
    # both real), and normalizing must be what closes the gap, not a no-op.
    assert rb_table_sql != pre_table_sql
    assert rb_index_sql != pre_index_sql
    assert _normalize_ddl(rb_table_sql) == _normalize_ddl(pre_table_sql)
    assert _normalize_ddl(rb_index_sql) == _normalize_ddl(pre_index_sql)
    assert "stock_as_of" not in _platform_skus_cols(conn)

    remaining = [
        (r[0], r[1]) for r in conn.execute(
            "SELECT source_table, source_id FROM platform_stock_deductions"
        ).fetchall()
    ]
    assert remaining == [("sales_transactions", 1)]  # marketplace_orders row dropped
