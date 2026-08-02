"""Migration 151 — index `audit_log(table_name, row_key)` + PK-immutability
guards on the four TEXT-PK audited tables (design doc:
projects/customer-edit-card/plan.md, "P4a MERGED" -> the two acceptance
criteria for P4b).

Background: migration 150 added `audit_log.row_key`, a business-key
companion to `row_id` that survives VACUUM (unlike the implicit rowid
`row_id` stores for these four tables). Codex's review of #351 found two
gaps before a history card can trust it:

  1. `row_key` is stable against VACUUM but NOT against a rename of the
     business key itself — `code`/`customer_code`/`salesperson_code` are
     not in the relevant trigger's WHEN clause, so a rename orphans the
     trail silently. Verified 2026-08-02: no application code path renames
     any of these four keys today. Migration 151 makes that an enforced
     invariant (a BEFORE UPDATE ... RAISE(ABORT) guard per table) rather
     than an unwritten assumption.
  2. The history card queries `audit_log` by `row_key`; no index covers
     it (only `idx_audit_table_row` on (table_name, row_id) and
     `idx_audit_log_table_time` on (table_name, created_at) exist), so
     every render would scan.

Tests (deterministic, on the schema-only empty_db):
  1. Applying the migration via the real runner adds the index + 4
     triggers and stamps applied_migrations; a second run is a no-op.
  2. The index covers (table_name, row_key) and a row_key lookup query
     actually uses it (EXPLAIN QUERY PLAN), not a scan.
  3. Each of the 4 guard triggers: updating that table's business key
     raises sqlite3.IntegrityError; updating a NON-key column on the same
     row still succeeds (break-it-once — deleting either half of this pair
     turns the other assertion's premise false, so this pair pins both
     directions).
  4. Rollback drops the index and all 4 triggers; the tables stay usable.
"""
import os
import sqlite3

import pytest

import database

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_151 = os.path.join(REPO, "data", "migrations", "151_audit_row_key_index_and_pk_guard.sql")
ROLLBACK_151 = os.path.join(
    REPO, "data", "migrations", "151_audit_row_key_index_and_pk_guard.rollback.sql")

_GUARD_TRIGGERS = (
    "customers_code_immutable",
    "salespersons_code_immutable",
    "commission_assignments_salesperson_code_immutable",
    "customer_crm_customer_code_immutable",
)


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


@pytest.fixture
def pre151_conn(empty_db_conn):
    """empty_db clones the LIVE local DB schema, which is expected to
    already carry migration 150 (row_key column + the 12 row_key-writing
    triggers, merged to main as #351) but not yet 151. If a sibling tab
    ever merges 151 before this runs, the clone would carry the guard
    triggers too and re-applying 151 in a test would raise "trigger
    already exists" — reconstruct the guaranteed pre-151 state by rolling
    back only when that's actually the case (same defensive pattern as
    test_mig150's pre150_conn)."""
    triggers = {r["name"] for r in empty_db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}
    if "customers_code_immutable" in triggers:
        _apply(empty_db_conn, ROLLBACK_151)
    return empty_db_conn


@pytest.fixture
def post151_conn(pre151_conn):
    _apply(pre151_conn, MIG_151)
    return pre151_conn


def _ensure_tier(conn):
    row = conn.execute("SELECT id FROM commission_tiers LIMIT 1").fetchone()
    if row:
        return row["id"]
    conn.execute(
        "INSERT INTO commission_tiers (code, name_th, rate_own_pct, rate_third_pct) "
        "VALUES ('TESTTIER151', 'test tier', 1.0, 1.0)"
    )
    return conn.execute(
        "SELECT id FROM commission_tiers WHERE code='TESTTIER151'"
    ).fetchone()["id"]


# ── 1. runner integration: applies once, stamps, never re-applies ──────────

def _isolated_migrations_dir(tmp_path, filenames):
    import shutil
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
    and the runner takes the real pending-migration path."""
    conn.execute(
        "INSERT INTO applied_migrations(filename, applied_by) "
        "VALUES ('000_sentinel.sql', 'test')"
    )
    conn.commit()


def test_runner_applies_151_adds_index_and_triggers_and_stamps(
    pre151_conn, tmp_path, monkeypatch
):
    conn = pre151_conn
    _force_pending_path(conn)
    mig_dir = _isolated_migrations_dir(
        tmp_path, ["151_audit_row_key_index_and_pk_guard.sql"])
    monkeypatch.setattr(database, "MIGRATIONS_DIR", mig_dir)

    ran = database.run_pending_migrations(conn, verbose=False)

    assert ran == ["151_audit_row_key_index_and_pk_guard.sql"]
    idx = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' "
        "AND name='idx_audit_log_table_row_key'"
    ).fetchone()
    assert idx is not None
    triggers = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert set(_GUARD_TRIGGERS) <= triggers
    stamped = conn.execute(
        "SELECT 1 FROM applied_migrations "
        "WHERE filename='151_audit_row_key_index_and_pk_guard.sql'"
    ).fetchone()
    assert stamped is not None


def test_runner_does_not_reapply_151_a_second_time(pre151_conn, tmp_path, monkeypatch):
    conn = pre151_conn
    _force_pending_path(conn)
    mig_dir = _isolated_migrations_dir(
        tmp_path, ["151_audit_row_key_index_and_pk_guard.sql"])
    monkeypatch.setattr(database, "MIGRATIONS_DIR", mig_dir)

    first = database.run_pending_migrations(conn, verbose=False)
    assert first == ["151_audit_row_key_index_and_pk_guard.sql"]

    # A second call must be a no-op: re-running the script would raise
    # "index already exists" / "trigger already exists".
    second = database.run_pending_migrations(conn, verbose=False)
    assert second == []


# ── 2. the index covers (table_name, row_key) and is actually used ─────────

def test_index_columns_are_table_name_and_row_key(post151_conn):
    conn = post151_conn
    cols = [r["name"] for r in conn.execute(
        "PRAGMA index_info(idx_audit_log_table_row_key)")]
    assert cols == ["table_name", "row_key"]


def test_row_key_lookup_uses_the_new_index_not_a_scan(post151_conn):
    conn = post151_conn
    plan = conn.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT * FROM audit_log WHERE table_name='customers' AND row_key='X'"
    ).fetchall()
    plan_text = " ".join(r["detail"] for r in plan)
    assert "idx_audit_log_table_row_key" in plan_text, (
        f"expected the new index in the query plan, got: {plan_text}")
    assert "SCAN" not in plan_text.upper() or "USING INDEX" in plan_text.upper()


# ── 3. PK-immutability guards: rename raises, non-key edit still works ─────

def test_customers_code_rename_raises(post151_conn):
    conn = post151_conn
    conn.execute("INSERT INTO customers (code, name) VALUES ('RK-OLD', 'co')")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE customers SET code='RK-NEW' WHERE code='RK-OLD'")


def test_customers_non_key_edit_still_works(post151_conn):
    conn = post151_conn
    conn.execute("INSERT INTO customers (code, name) VALUES ('RK-A', 'before')")
    conn.commit()
    conn.execute("UPDATE customers SET name='after' WHERE code='RK-A'")
    conn.commit()
    assert conn.execute(
        "SELECT name FROM customers WHERE code='RK-A'"
    ).fetchone()["name"] == "after"


def test_salespersons_code_rename_raises(post151_conn):
    conn = post151_conn
    conn.execute("INSERT INTO salespersons (code, name) VALUES ('RK-S-OLD', 'rep')")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE salespersons SET code='RK-S-NEW' WHERE code='RK-S-OLD'")


def test_salespersons_non_key_edit_still_works(post151_conn):
    conn = post151_conn
    conn.execute("INSERT INTO salespersons (code, name) VALUES ('RK-S-A', 'before')")
    conn.commit()
    conn.execute("UPDATE salespersons SET name='after' WHERE code='RK-S-A'")
    conn.commit()
    assert conn.execute(
        "SELECT name FROM salespersons WHERE code='RK-S-A'"
    ).fetchone()["name"] == "after"


def test_commission_assignments_salesperson_code_rename_raises(post151_conn):
    conn = post151_conn
    conn.execute("INSERT INTO salespersons (code, name) VALUES ('RK-CA-OLD', 'rep')")
    conn.execute("INSERT INTO salespersons (code, name) VALUES ('RK-CA-NEW', 'rep2')")
    tier_id = _ensure_tier(conn)
    conn.execute(
        "INSERT INTO commission_assignments "
        "(salesperson_code, tier_id, effective_from) VALUES ('RK-CA-OLD', ?, '2026-01-01')",
        (tier_id,),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE commission_assignments SET salesperson_code='RK-CA-NEW' "
            "WHERE salesperson_code='RK-CA-OLD'"
        )


def test_commission_assignments_non_key_edit_still_works(post151_conn):
    conn = post151_conn
    conn.execute("INSERT INTO salespersons (code, name) VALUES ('RK-CA-B', 'rep')")
    tier_id = _ensure_tier(conn)
    conn.execute(
        "INSERT INTO commission_assignments "
        "(salesperson_code, tier_id, effective_from) VALUES ('RK-CA-B', ?, '2026-01-01')",
        (tier_id,),
    )
    conn.commit()
    conn.execute(
        "UPDATE commission_assignments SET note='changed' WHERE salesperson_code='RK-CA-B'")
    conn.commit()
    assert conn.execute(
        "SELECT note FROM commission_assignments WHERE salesperson_code='RK-CA-B'"
    ).fetchone()["note"] == "changed"


def test_customer_crm_customer_code_rename_raises(post151_conn):
    conn = post151_conn
    conn.execute("INSERT INTO customer_crm (customer_code) VALUES ('RK-CRM-OLD')")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE customer_crm SET customer_code='RK-CRM-NEW' "
            "WHERE customer_code='RK-CRM-OLD'"
        )


def test_customer_crm_non_key_edit_still_works(post151_conn):
    conn = post151_conn
    conn.execute("INSERT INTO customer_crm (customer_code) VALUES ('RK-CRM-A')")
    conn.commit()
    conn.execute(
        "UPDATE customer_crm SET tags='changed' WHERE customer_code='RK-CRM-A'")
    conn.commit()
    assert conn.execute(
        "SELECT tags FROM customer_crm WHERE customer_code='RK-CRM-A'"
    ).fetchone()["tags"] == "changed"


# ── 4. rollback ──────────────────────────────────────────────────────────

def test_rollback_drops_index_and_all_four_guard_triggers(pre151_conn):
    conn = pre151_conn
    _apply(conn, MIG_151)
    _apply(conn, ROLLBACK_151)

    idx = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' "
        "AND name='idx_audit_log_table_row_key'"
    ).fetchone()
    assert idx is None
    triggers = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert not (set(_GUARD_TRIGGERS) & triggers)


def test_data_usable_and_rename_allowed_again_after_rollback(pre151_conn):
    conn = pre151_conn
    _apply(conn, MIG_151)
    _apply(conn, ROLLBACK_151)

    conn.execute("INSERT INTO customers (code, name) VALUES ('RB151-OLD', 'co')")
    conn.commit()
    # The guard is gone post-rollback — a rename must succeed again.
    conn.execute("UPDATE customers SET code='RB151-NEW' WHERE code='RB151-OLD'")
    conn.commit()
    assert conn.execute(
        "SELECT 1 FROM customers WHERE code='RB151-NEW'"
    ).fetchone() is not None
