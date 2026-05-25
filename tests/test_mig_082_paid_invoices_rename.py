"""Migration 082 — paid_invoices: rename iv_no → doc_no + add doc_kind.

Verifies schema rebuild, doc_kind backfill, index + audit trigger recreation,
rollback path, and CHECK constraint enforcement.
"""
import os
import sqlite3

import pytest


REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_082      = os.path.join(REPO, "data", "migrations",
                            "082_paid_invoices_doc_no_doc_kind.sql")
ROLLBACK_082 = os.path.join(REPO, "data", "migrations",
                            "082_paid_invoices_doc_no_doc_kind.rollback.sql")


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


def _columns(conn, table):
    return {r[1]: r[2] for r in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA index_list({table})")}


def _triggers(conn, table):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
        (table,)
    )}


# ── Schema state ─────────────────────────────────────────────────────────────

def test_post_mig_columns_renamed_and_added(tmp_db):
    """Live DB has mig 082 applied — verify the expected shape."""
    conn = sqlite3.connect(tmp_db)
    cols = _columns(conn, 'paid_invoices')
    assert 'doc_no' in cols, "doc_no column should exist"
    assert 'doc_kind' in cols, "doc_kind column should exist"
    assert 'iv_no' not in cols, "iv_no should be gone"
    assert cols['doc_no'] == 'TEXT'
    assert cols['doc_kind'] == 'TEXT'
    conn.close()


def test_post_mig_doc_kind_check_constraint_enforced(tmp_db):
    """doc_kind CHECK(IN ('IV','SR')) must reject other values."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = OFF")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount) "
            "VALUES (99999, 'XX-T1', 'XX', 100.0)"
        )
    conn.close()


def test_post_mig_index_recreated(tmp_db):
    conn = sqlite3.connect(tmp_db)
    idx = _indexes(conn, 'paid_invoices')
    assert 'idx_pi_doc_no' in idx, "new index missing"
    assert 'idx_pi_iv_no' not in idx, "old index should be dropped"
    conn.close()


def test_post_mig_audit_triggers_exist(tmp_db):
    conn = sqlite3.connect(tmp_db)
    trigs = _triggers(conn, 'paid_invoices')
    assert 'audit_paid_invoices_insert' in trigs
    assert 'audit_paid_invoices_update' in trigs
    assert 'audit_paid_invoices_delete' in trigs
    conn.close()


def test_post_mig_audit_payload_uses_new_column_names(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = OFF")
    cur = conn.execute(
        "INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount) "
        "VALUES (99999, 'IV-MIG082', 'IV', 100.0)"
    )
    pid = cur.lastrowid
    conn.commit()
    payload = conn.execute(
        "SELECT changed_fields FROM audit_log "
        "WHERE table_name='paid_invoices' AND row_id=? AND action='INSERT'",
        (pid,)
    ).fetchone()[0]
    # New column names must be in the audit payload
    assert 'doc_no' in payload
    assert 'doc_kind' in payload
    assert 'iv_no' not in payload
    conn.close()


# ── Backfill correctness ─────────────────────────────────────────────────────

def test_doc_kind_backfill_matches_prefix(tmp_db):
    """Every existing row's doc_kind must match what the prefix would say."""
    conn = sqlite3.connect(tmp_db)
    mismatched = conn.execute("""
        SELECT COUNT(*) FROM paid_invoices
        WHERE (doc_no LIKE 'SR%' AND doc_kind != 'SR')
           OR (doc_no NOT LIKE 'SR%' AND doc_kind != 'IV')
    """).fetchone()[0]
    assert mismatched == 0, f"{mismatched} rows have doc_kind != prefix"
    conn.close()


def test_doc_kind_distribution_iv_majority(tmp_db):
    """Sanity check the IV/SR distribution roughly matches expectations.

    Pre-mig survey (2026-05-25): 7,688 IV + 143 SR.
    Numbers will drift over time; we only assert (a) both kinds exist,
    (b) IV dominates by an order of magnitude.
    """
    conn = sqlite3.connect(tmp_db)
    counts = dict(conn.execute(
        "SELECT doc_kind, COUNT(*) FROM paid_invoices GROUP BY doc_kind"
    ).fetchall())
    assert counts.get('IV', 0) > 0
    assert counts.get('SR', 0) > 0
    assert counts['IV'] > counts['SR'] * 10, \
        "IV should outnumber SR by >10x in normal operation"
    conn.close()


# ── Rollback path ────────────────────────────────────────────────────────────

def test_rollback_restores_iv_no(tmp_db):
    """Rollback should drop doc_kind and restore iv_no naming.

    Test path: mig already applied on live → snapshot table from forward run
    is preserved → rollback uses it. (In production, you'd run the forward
    mig first within the test; here the live state already reflects it.)
    """
    conn = sqlite3.connect(tmp_db)
    # snapshot exists from the prior forward-mig run
    snap = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='migration_082_snapshot'"
    ).fetchone()
    if snap is None:
        pytest.skip("migration_082_snapshot not present in cloned DB "
                    "(empty_db won't have it either) — skipping live-rollback test")

    _apply(conn, ROLLBACK_082)

    cols = _columns(conn, 'paid_invoices')
    assert 'iv_no' in cols, "iv_no must be restored after rollback"
    assert 'doc_no' not in cols, "doc_no should be gone"
    assert 'doc_kind' not in cols, "doc_kind should be gone"

    idx = _indexes(conn, 'paid_invoices')
    assert 'idx_pi_iv_no' in idx
    assert 'idx_pi_doc_no' not in idx

    trigs = _triggers(conn, 'paid_invoices')
    assert 'audit_paid_invoices_insert' in trigs
    assert 'audit_paid_invoices_update' in trigs
    assert 'audit_paid_invoices_delete' in trigs

    # snapshot table should be dropped
    snap = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='migration_082_snapshot'"
    ).fetchone()
    assert snap is None, "snapshot table should be DROPped by rollback"

    conn.close()


# ── End-to-end forward + rollback cycle on empty_db ──────────────────────────

def test_full_cycle_on_empty_db(empty_db):
    """Build a synthetic pre-mig schema on empty_db, run mig 082 forward,
    verify, then rollback and verify.

    empty_db comes pre-cloned with the live schema (which now has mig 082
    applied), so we first construct the pre-mig shape by hand.
    """
    conn = sqlite3.connect(empty_db)
    conn.execute("PRAGMA foreign_keys = OFF")

    # Strip mig 082's outputs and rebuild old schema
    conn.executescript("""
        DROP TRIGGER IF EXISTS audit_paid_invoices_insert;
        DROP TRIGGER IF EXISTS audit_paid_invoices_update;
        DROP TRIGGER IF EXISTS audit_paid_invoices_delete;
        DROP INDEX  IF EXISTS idx_pi_doc_no;
        DROP TABLE  IF EXISTS paid_invoices;
        DROP TABLE  IF EXISTS migration_082_snapshot;

        CREATE TABLE paid_invoices (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            re_id      INTEGER NOT NULL REFERENCES received_payments(id),
            iv_no      TEXT    NOT NULL,
            amount     REAL,
            UNIQUE(re_id, iv_no)
        );
        CREATE INDEX idx_pi_iv_no ON paid_invoices(iv_no);

        -- Mimic the mig 076 audit triggers (old shape)
        CREATE TRIGGER audit_paid_invoices_insert
        AFTER INSERT ON paid_invoices BEGIN
            INSERT INTO audit_log (table_name, row_id, action, changed_fields)
            VALUES ('paid_invoices', NEW.id, 'INSERT',
                json_object('re_id', NEW.re_id, 'iv_no', NEW.iv_no, 'amount', NEW.amount));
        END;
        CREATE TRIGGER audit_paid_invoices_update
        AFTER UPDATE ON paid_invoices BEGIN
            INSERT INTO audit_log (table_name, row_id, action, changed_fields)
            VALUES ('paid_invoices', NEW.id, 'UPDATE',
                json_object('iv_no', json_array(OLD.iv_no, NEW.iv_no)));
        END;
        CREATE TRIGGER audit_paid_invoices_delete
        BEFORE DELETE ON paid_invoices BEGIN
            INSERT INTO audit_log (table_name, row_id, action, changed_fields)
            VALUES ('paid_invoices', OLD.id, 'DELETE',
                json_object('re_id', OLD.re_id, 'iv_no', OLD.iv_no, 'amount', OLD.amount));
        END;
    """)
    # Seed sample rows
    conn.executemany(
        "INSERT INTO paid_invoices (re_id, iv_no, amount) VALUES (?, ?, ?)",
        [(1, 'IV-A', 100.0), (1, 'IV-B', 200.0), (2, 'SR-C', -50.0)],
    )
    conn.commit()

    pre_count = conn.execute("SELECT COUNT(*) FROM paid_invoices").fetchone()[0]
    assert pre_count == 3

    # ── Forward ──
    _apply(conn, MIG_082)

    cols = _columns(conn, 'paid_invoices')
    assert 'doc_no' in cols and 'doc_kind' in cols
    assert 'iv_no' not in cols

    rows = dict(conn.execute(
        "SELECT doc_no, doc_kind FROM paid_invoices ORDER BY doc_no"
    ).fetchall())
    assert rows == {'IV-A': 'IV', 'IV-B': 'IV', 'SR-C': 'SR'}, \
        f"doc_kind backfill wrong: {rows}"

    snap_count = conn.execute(
        "SELECT COUNT(*) FROM migration_082_snapshot"
    ).fetchone()[0]
    assert snap_count == 3, "snapshot should hold all original rows"

    # ── Rollback ──
    _apply(conn, ROLLBACK_082)

    cols = _columns(conn, 'paid_invoices')
    assert 'iv_no' in cols
    assert 'doc_no' not in cols and 'doc_kind' not in cols

    post_rb = dict(conn.execute(
        "SELECT iv_no, amount FROM paid_invoices ORDER BY iv_no"
    ).fetchall())
    assert post_rb == {'IV-A': 100.0, 'IV-B': 200.0, 'SR-C': -50.0}, \
        f"rollback lost data: {post_rb}"

    snap_after = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='migration_082_snapshot'"
    ).fetchone()
    assert snap_after is None, "snapshot should be DROPped"

    conn.close()


# ── Money-path math invariance ───────────────────────────────────────────────

def test_rollback_preserves_post_mig_inserts(empty_db):
    """Rollback must preserve rows INSERTed AFTER the forward mig — they're
    real production data, not just a snapshot replay.

    Scenario: forward mig runs, sendy keeps operating and inserts new rows,
    then someone rolls back for an unrelated reason. The new rows must
    survive (their doc_no becomes iv_no in the restored schema).
    """
    conn = sqlite3.connect(empty_db)
    conn.execute("PRAGMA foreign_keys = OFF")

    # Tear down post-mig fixture, build pre-mig schema
    conn.executescript("""
        DROP TRIGGER IF EXISTS audit_paid_invoices_insert;
        DROP TRIGGER IF EXISTS audit_paid_invoices_update;
        DROP TRIGGER IF EXISTS audit_paid_invoices_delete;
        DROP INDEX  IF EXISTS idx_pi_doc_no;
        DROP TABLE  IF EXISTS paid_invoices;
        DROP TABLE  IF EXISTS migration_082_snapshot;

        CREATE TABLE paid_invoices (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            re_id      INTEGER NOT NULL REFERENCES received_payments(id),
            iv_no      TEXT    NOT NULL,
            amount     REAL,
            UNIQUE(re_id, iv_no)
        );
        CREATE INDEX idx_pi_iv_no ON paid_invoices(iv_no);
    """)
    # 2 pre-mig rows
    conn.executemany(
        "INSERT INTO paid_invoices (re_id, iv_no, amount) VALUES (?, ?, ?)",
        [(1, 'IV-PRE1', 100.0), (1, 'IV-PRE2', 200.0)],
    )
    conn.commit()

    # ── Forward mig ──
    _apply(conn, MIG_082)

    # New row inserted AFTER forward mig (simulating ongoing operation)
    conn.execute(
        "INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount) "
        "VALUES (?, ?, ?, ?)",
        (2, 'IV-POST1', 'IV', 300.0)
    )
    conn.execute(
        "INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount) "
        "VALUES (?, ?, ?, ?)",
        (2, 'SR-POST1', 'SR', -50.0)
    )
    conn.commit()

    pre_rollback_count = conn.execute(
        "SELECT COUNT(*) FROM paid_invoices"
    ).fetchone()[0]
    assert pre_rollback_count == 4, "test setup: 2 pre-mig + 2 post-mig rows"

    # ── Rollback ──
    _apply(conn, ROLLBACK_082)

    # All 4 rows must survive
    rows = dict(conn.execute(
        "SELECT iv_no, amount FROM paid_invoices ORDER BY iv_no"
    ).fetchall())
    expected = {
        'IV-PRE1':  100.0,
        'IV-PRE2':  200.0,
        'IV-POST1': 300.0,
        'SR-POST1': -50.0,
    }
    assert rows == expected, \
        f"post-mig rows lost in rollback: {set(expected) - set(rows)}"


def test_init_db_idempotent_on_post_mig_schema(tmp_db, monkeypatch):
    """Regression test for the database.py:446 crash that Codex pass-1 caught.

    Before the fix, init_db() unconditionally ran
        CREATE INDEX IF NOT EXISTS idx_pi_iv_no ON paid_invoices(iv_no)
    which would crash on every restart of a post-mig-082 DB because the
    `iv_no` column no longer exists. The fix gates by column existence via
    PRAGMA table_info. This test exercises the gated path.
    """
    # tmp_db clones the live DB which has mig 082 applied → paid_invoices
    # has doc_no, NOT iv_no. Calling init_db() must not raise.
    import database
    conn = sqlite3.connect(tmp_db)
    try:
        database.init_db()  # would have crashed pre-fix
    finally:
        conn.close()

    # Verify post-condition: idx_pi_doc_no exists (from mig 082),
    # idx_pi_iv_no does NOT (column doesn't exist).
    conn = sqlite3.connect(tmp_db)
    idx = _indexes(conn, 'paid_invoices')
    assert 'idx_pi_doc_no' in idx, "post-mig perf index missing"
    assert 'idx_pi_iv_no' not in idx, "stale iv_no index should not exist"
    conn.close()


def test_payment_summary_math_intact(tmp_db):
    """Spot-check: get_payment_summary still runs and returns non-NULL,
    non-negative aggregates after the schema rename.

    Note: paid_count + unpaid_count != total_bills in general — paid_count
    is a row-level SUM over the LEFT JOIN (an invoice with N receipt links
    contributes N), while total_bills is COUNT(DISTINCT). Asserting their
    equality would test the pre-existing query semantics, not mig 082. The
    mig 082 contract is "column names changed, aggregates unchanged"; we
    verify it didn't crash or return NULLs."""
    import models  # imported here so tmp_db's monkeypatch is active
    row = models.get_payment_summary()
    assert row is not None
    for k in ('total_bills', 'paid_count', 'unpaid_count', 'paid_amount',
              'unpaid_amount'):
        assert row[k] is not None, f"{k} returned NULL"
        assert row[k] >= 0, f"{k} negative: {row[k]}"
