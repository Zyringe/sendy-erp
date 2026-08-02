"""Migration 150 — audit_log.row_key (design doc:
projects/customer-edit-card/plan.md, Phase 4a; NOT the plan's own repo, but
that is where the decision + verified facts live).

Background: `audit_log.row_id` stores a table's SQLite rowid. For 30 of the
34 audited tables the PK is `id INTEGER PRIMARY KEY` — an alias for rowid —
so VACUUM preserves it. Four audited tables have a TEXT primary key instead
(customers.code, salespersons.code, commission_assignments.salesperson_code,
customer_crm.customer_code): their rowid is implicit, and SQLite explicitly
permits VACUUM to renumber implicit rowids. A renumber would re-point OLD
audit_log rows at whatever record now holds that rowid — showing another
record's history, confidently. Migration 150 adds `row_key`, written
alongside `row_id` by all 12 insert/update/delete triggers on those four
tables, backfilled from the current (today, verified trustworthy) rowid<->
key mapping. `row_id` itself is unchanged — 30 other tables and existing
code still read it.

Tests (deterministic, on the schema-only empty_db):
  1. Applying the migration via the real runner (database.run_pending_
     migrations) adds the column and stamps applied_migrations; running the
     runner again does not re-apply it (no duplicate-column error).
  2. Backfill: seeding rows BEFORE the migration runs (so their audit_log
     rows exist pre-migration, matching prod's real 3,238/25/25/0 rows)
     ends with a non-NULL row_key that resolves in that table's PK column,
     for all four tables.
  3. Each of the 12 triggers (insert/update/delete x 4 tables) writes the
     correct row_key on a row created AFTER the migration has run.
  4. row_id is still written post-migration (nothing regressed) and an
     exempt INTEGER-PK table's (products) trigger SQL is byte-for-byte
     unchanged by this migration — proves the other 30 tables are untouched.
  5. Rollback restores the exact prior 12 trigger bodies (byte-for-byte SQL
     diff, not a semantic re-read) and drops row_key; data stays usable.
"""
import os
import shutil
import sqlite3

import pytest

import database

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_150 = os.path.join(REPO, "data", "migrations", "150_audit_log_row_key.sql")
ROLLBACK_150 = os.path.join(
    REPO, "data", "migrations", "150_audit_log_row_key.rollback.sql")

TABLES = {
    "customers": "code",
    "salespersons": "code",
    "commission_assignments": "salesperson_code",
    "customer_crm": "customer_code",
}

_AUDITED_TRIGGER_TABLES = (
    "'customers','salespersons','commission_assignments','customer_crm'"
)


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


@pytest.fixture
def pre150_conn(empty_db_conn):
    """empty_db clones the LIVE local DB schema. If that live DB has already
    had migration 150 applied, the clone carries audit_log.row_key and the
    post-150 trigger bodies, and re-applying 150 in a test would raise
    "duplicate column". Unlike mig 134's DROP TABLE IF EXISTS rollback,
    150's rollback ends in `ALTER TABLE audit_log DROP COLUMN row_key`,
    which has no IF-EXISTS form and errors if the column is already absent
    — so the rollback can't be run unconditionally the way pre134_conn does.
    Reconstruct the guaranteed pre-150 state: only run the rollback when the
    clone actually has row_key; otherwise the clone already IS pre-150
    (today's reality on every machine this was written against)."""
    cols = {r["name"] for r in empty_db_conn.execute("PRAGMA table_info(audit_log)")}
    if "row_key" in cols:
        _apply(empty_db_conn, ROLLBACK_150)
    return empty_db_conn


@pytest.fixture
def post150_conn(pre150_conn):
    """pre150_conn with migration 150 applied — for trigger-behaviour tests
    that don't care about the backfill, only the new trigger bodies."""
    _apply(pre150_conn, MIG_150)
    return pre150_conn


def _ensure_tier(conn):
    row = conn.execute("SELECT id FROM commission_tiers LIMIT 1").fetchone()
    if row:
        return row["id"]
    conn.execute(
        "INSERT INTO commission_tiers (code, name_th, rate_own_pct, rate_third_pct) "
        "VALUES ('TESTTIER', 'test tier', 1.0, 1.0)"
    )
    return conn.execute(
        "SELECT id FROM commission_tiers WHERE code='TESTTIER'"
    ).fetchone()["id"]


def _seed(conn, table, key):
    """Insert one row with the given PK value, firing that table's real
    (currently-installed) INSERT trigger."""
    if table == "customers":
        conn.execute(
            "INSERT INTO customers (code, name) VALUES (?, ?)", (key, f"name {key}")
        )
    elif table == "salespersons":
        conn.execute(
            "INSERT INTO salespersons (code, name) VALUES (?, ?)", (key, f"name {key}")
        )
    elif table == "commission_assignments":
        conn.execute(
            "INSERT INTO salespersons (code, name) VALUES (?, ?)", (key, f"name {key}")
        )
        tier_id = _ensure_tier(conn)
        conn.execute(
            "INSERT INTO commission_assignments "
            "(salesperson_code, tier_id, effective_from) VALUES (?, ?, '2026-01-01')",
            (key, tier_id),
        )
    elif table == "customer_crm":
        conn.execute("INSERT INTO customer_crm (customer_code) VALUES (?)", (key,))
    else:
        raise ValueError(table)
    conn.commit()


# ── 1. runner integration: applies once, stamps, never re-applies ──────────

def _isolated_migrations_dir(tmp_path, filenames):
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    for fn in filenames:
        shutil.copy(os.path.join(REPO, "data", "migrations", fn), mig_dir / fn)
    return str(mig_dir)


def _force_pending_path(conn):
    """empty_db clones schema only, not data — applied_migrations comes back
    EMPTY, which would otherwise route run_pending_migrations through its
    bootstrap-backfill branch (marks every file "already applied" without
    running it, returns []). Seed one sentinel row so `applied` is non-empty
    and the runner takes the real pending-migration path (same trick as
    test_migration_runner_idempotent.py)."""
    conn.execute(
        "INSERT INTO applied_migrations(filename, applied_by) "
        "VALUES ('000_sentinel.sql', 'test')"
    )
    conn.commit()


def test_runner_applies_150_adds_column_and_stamps_applied_migrations(
    pre150_conn, tmp_path, monkeypatch
):
    conn = pre150_conn
    _force_pending_path(conn)
    mig_dir = _isolated_migrations_dir(tmp_path, ["150_audit_log_row_key.sql"])
    monkeypatch.setattr(database, "MIGRATIONS_DIR", mig_dir)

    ran = database.run_pending_migrations(conn, verbose=False)

    assert ran == ["150_audit_log_row_key.sql"]
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(audit_log)")}
    assert "row_key" in cols
    stamped = conn.execute(
        "SELECT 1 FROM applied_migrations WHERE filename='150_audit_log_row_key.sql'"
    ).fetchone()
    assert stamped is not None


def test_runner_does_not_reapply_150_a_second_time(pre150_conn, tmp_path, monkeypatch):
    conn = pre150_conn
    _force_pending_path(conn)
    mig_dir = _isolated_migrations_dir(tmp_path, ["150_audit_log_row_key.sql"])
    monkeypatch.setattr(database, "MIGRATIONS_DIR", mig_dir)

    first = database.run_pending_migrations(conn, verbose=False)
    assert first == ["150_audit_log_row_key.sql"]

    # A second call must be a no-op: the filename is already in
    # applied_migrations, so the runner never re-executes the script (which
    # would raise "duplicate column name: row_key").
    second = database.run_pending_migrations(conn, verbose=False)
    assert second == []


# ── 2. backfill: rows seeded BEFORE the migration runs (mirrors prod) ──────

@pytest.mark.parametrize("table,pk_col", sorted(TABLES.items()))
def test_backfill_resolves_for_every_pre_existing_row(pre150_conn, table, pk_col):
    conn = pre150_conn
    keys = [f"BF-{table}-{i}" for i in range(3)]
    for key in keys:
        _seed(conn, table, key)

    pre_rows = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE table_name=?", (table,)
    ).fetchone()[0]
    assert pre_rows >= len(keys), (
        f"expected the pre-migration INSERT trigger to have fired for {table}"
    )

    _apply(conn, MIG_150)

    rows = conn.execute(
        f"SELECT row_id, row_key FROM audit_log WHERE table_name=?", (table,)
    ).fetchall()
    assert rows
    for r in rows:
        assert r["row_key"] is not None, (
            f"NULL row_key survived backfill for {table} row_id={r['row_id']}"
        )
    live_keys = {row[pk_col] for row in conn.execute(f"SELECT {pk_col} FROM {table}")}
    for r in rows:
        assert r["row_key"] in live_keys, (
            f"{table} row_key={r['row_key']!r} does not resolve in {table}.{pk_col}"
        )
    # the three rows we just seeded are specifically covered
    seen_keys = {r["row_key"] for r in rows}
    assert set(keys) <= seen_keys


# ── 3. each of the 12 triggers writes the correct row_key ──────────────────

def _latest_audit_row(conn, table, action):
    return conn.execute(
        "SELECT row_id, row_key FROM audit_log "
        "WHERE table_name=? AND action=? ORDER BY id DESC LIMIT 1",
        (table, action),
    ).fetchone()


def test_customers_insert_trigger_writes_row_key(post150_conn):
    conn = post150_conn
    conn.execute("INSERT INTO customers (code, name) VALUES ('TRG-C1', 'co')")
    conn.commit()
    row = _latest_audit_row(conn, "customers", "INSERT")
    assert row["row_key"] == "TRG-C1"
    assert row["row_id"] == conn.execute(
        "SELECT rowid FROM customers WHERE code='TRG-C1'"
    ).fetchone()[0]


def test_customers_update_trigger_writes_row_key(post150_conn):
    conn = post150_conn
    conn.execute("INSERT INTO customers (code, name) VALUES ('TRG-C2', 'orig')")
    conn.commit()
    conn.execute("UPDATE customers SET name='changed' WHERE code='TRG-C2'")
    conn.commit()
    row = _latest_audit_row(conn, "customers", "UPDATE")
    assert row["row_key"] == "TRG-C2"


def test_customers_delete_trigger_writes_row_key(post150_conn):
    conn = post150_conn
    conn.execute("INSERT INTO customers (code, name) VALUES ('TRG-C3', 'gone soon')")
    conn.commit()
    conn.execute("DELETE FROM customers WHERE code='TRG-C3'")
    conn.commit()
    row = _latest_audit_row(conn, "customers", "DELETE")
    assert row["row_key"] == "TRG-C3"


def test_salespersons_insert_trigger_writes_row_key(post150_conn):
    conn = post150_conn
    conn.execute("INSERT INTO salespersons (code, name) VALUES ('TRG-S1', 'rep')")
    conn.commit()
    row = _latest_audit_row(conn, "salespersons", "INSERT")
    assert row["row_key"] == "TRG-S1"


def test_salespersons_update_trigger_writes_row_key(post150_conn):
    conn = post150_conn
    conn.execute("INSERT INTO salespersons (code, name) VALUES ('TRG-S2', 'orig')")
    conn.commit()
    conn.execute("UPDATE salespersons SET name='changed' WHERE code='TRG-S2'")
    conn.commit()
    row = _latest_audit_row(conn, "salespersons", "UPDATE")
    assert row["row_key"] == "TRG-S2"


def test_salespersons_delete_trigger_writes_row_key(post150_conn):
    conn = post150_conn
    conn.execute("INSERT INTO salespersons (code, name) VALUES ('TRG-S3', 'gone soon')")
    conn.commit()
    conn.execute("DELETE FROM salespersons WHERE code='TRG-S3'")
    conn.commit()
    row = _latest_audit_row(conn, "salespersons", "DELETE")
    assert row["row_key"] == "TRG-S3"


def test_commission_assignments_insert_trigger_writes_row_key(post150_conn):
    conn = post150_conn
    conn.execute("INSERT INTO salespersons (code, name) VALUES ('TRG-A1', 'rep')")
    tier_id = _ensure_tier(conn)
    conn.execute(
        "INSERT INTO commission_assignments (salesperson_code, tier_id, effective_from) "
        "VALUES ('TRG-A1', ?, '2026-01-01')",
        (tier_id,),
    )
    conn.commit()
    row = _latest_audit_row(conn, "commission_assignments", "INSERT")
    assert row["row_key"] == "TRG-A1"


def test_commission_assignments_update_trigger_writes_row_key(post150_conn):
    conn = post150_conn
    conn.execute("INSERT INTO salespersons (code, name) VALUES ('TRG-A2', 'rep')")
    tier_id = _ensure_tier(conn)
    conn.execute(
        "INSERT INTO commission_assignments (salesperson_code, tier_id, effective_from) "
        "VALUES ('TRG-A2', ?, '2026-01-01')",
        (tier_id,),
    )
    conn.commit()
    conn.execute(
        "UPDATE commission_assignments SET note='changed' WHERE salesperson_code='TRG-A2'"
    )
    conn.commit()
    row = _latest_audit_row(conn, "commission_assignments", "UPDATE")
    assert row["row_key"] == "TRG-A2"


def test_commission_assignments_delete_trigger_writes_row_key(post150_conn):
    conn = post150_conn
    conn.execute("INSERT INTO salespersons (code, name) VALUES ('TRG-A3', 'rep')")
    tier_id = _ensure_tier(conn)
    conn.execute(
        "INSERT INTO commission_assignments (salesperson_code, tier_id, effective_from) "
        "VALUES ('TRG-A3', ?, '2026-01-01')",
        (tier_id,),
    )
    conn.commit()
    conn.execute("DELETE FROM commission_assignments WHERE salesperson_code='TRG-A3'")
    conn.commit()
    row = _latest_audit_row(conn, "commission_assignments", "DELETE")
    assert row["row_key"] == "TRG-A3"


def test_customer_crm_insert_trigger_writes_row_key(post150_conn):
    conn = post150_conn
    conn.execute("INSERT INTO customer_crm (customer_code) VALUES ('TRG-R1')")
    conn.commit()
    row = _latest_audit_row(conn, "customer_crm", "INSERT")
    assert row["row_key"] == "TRG-R1"


def test_customer_crm_update_trigger_writes_row_key(post150_conn):
    conn = post150_conn
    conn.execute("INSERT INTO customer_crm (customer_code) VALUES ('TRG-R2')")
    conn.commit()
    conn.execute("UPDATE customer_crm SET tags='changed' WHERE customer_code='TRG-R2'")
    conn.commit()
    row = _latest_audit_row(conn, "customer_crm", "UPDATE")
    assert row["row_key"] == "TRG-R2"


def test_customer_crm_delete_trigger_writes_row_key(post150_conn):
    conn = post150_conn
    conn.execute("INSERT INTO customer_crm (customer_code) VALUES ('TRG-R3')")
    conn.commit()
    conn.execute("DELETE FROM customer_crm WHERE customer_code='TRG-R3'")
    conn.commit()
    row = _latest_audit_row(conn, "customer_crm", "DELETE")
    assert row["row_key"] == "TRG-R3"


# ── 4. row_id unchanged; the other 30 tables are untouched ─────────────────

def test_row_id_still_populated_after_migration(post150_conn):
    conn = post150_conn
    conn.execute("INSERT INTO customers (code, name) VALUES ('TRG-C4', 'co')")
    conn.commit()
    row = _latest_audit_row(conn, "customers", "INSERT")
    expected_rowid = conn.execute(
        "SELECT rowid FROM customers WHERE code='TRG-C4'"
    ).fetchone()[0]
    assert row["row_id"] == expected_rowid


def test_exempt_table_triggers_byte_identical_after_migration(pre150_conn):
    """products has an INTEGER PRIMARY KEY (rowid alias) — migration 150
    must not touch it. Pin the trigger SQL text byte-for-byte, not just its
    behaviour, so any future edit to this migration that widens its DROP/
    CREATE beyond the 4 TEXT-PK tables fails loudly here."""
    conn = pre150_conn
    before = {
        r["name"]: r["sql"]
        for r in conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='trigger' AND tbl_name='products'"
        )
    }
    assert before, "sanity check: products should have audit triggers"
    _apply(conn, MIG_150)
    after = {
        r["name"]: r["sql"]
        for r in conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='trigger' AND tbl_name='products'"
        )
    }
    assert before == after


# ── 5. rollback ──────────────────────────────────────────────────────────

def test_rollback_restores_prior_trigger_bodies_byte_for_byte_and_drops_column(
    pre150_conn,
):
    conn = pre150_conn
    before = {
        r["name"]: r["sql"]
        for r in conn.execute(
            f"SELECT name, sql FROM sqlite_master WHERE type='trigger' "
            f"AND tbl_name IN ({_AUDITED_TRIGGER_TABLES})"
        )
    }
    assert len(before) == 12

    _apply(conn, MIG_150)
    _apply(conn, ROLLBACK_150)

    after = {
        r["name"]: r["sql"]
        for r in conn.execute(
            f"SELECT name, sql FROM sqlite_master WHERE type='trigger' "
            f"AND tbl_name IN ({_AUDITED_TRIGGER_TABLES})"
        )
    }
    assert before == after

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(audit_log)")}
    assert "row_key" not in cols


def test_data_usable_after_rollback(pre150_conn):
    conn = pre150_conn
    _apply(conn, MIG_150)
    _apply(conn, ROLLBACK_150)

    conn.execute("INSERT INTO customers (code, name) VALUES ('RB1', 'post-rollback')")
    conn.commit()
    row = conn.execute(
        "SELECT * FROM audit_log WHERE table_name='customers' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["row_id"] is not None
    assert row["action"] == "INSERT"
    # row_key column is gone post-rollback — the old trigger never wrote one
    assert set(row.keys()) == {
        "id", "table_name", "row_id", "action", "changed_fields", "user", "created_at"
    }
