"""Migration 160 — ar_followup_log soft delete (deleted_at / deleted_by).

See docs/superpowers/plans/2026-08-15-customers-ar-review-fixes.md Task 4.
Schema only: two nullable TEXT columns mirroring the existing call-log soft
delete (`customer_call_log.deleted_at/.deleted_by`, consumed by call_card.py)
plus a partial index over the LIVE rows for the per-customer newest-first
reader shape. The reader filters, `delete_outreach()` and the route
attribution land separately in the same task.

Fixture `pre160_conn`: `empty_db` clones the LIVE LOCAL schema, which on any
machine that has already booted this branch's app carries migration 160 — so
this file must NOT assume the columns are absent. Detect and roll back first
so the fixture reconstructs the true pre-160 state either way (same shape as
`pre134_conn` in test_mig134_product_generic_standins.py and `pre158_conn` in
test_mig158_conversion_input_role.py).

What each direction is pinned on:
  forward  — both columns exist, declared TEXT, nullable, no default;
             the index is PARTIAL on `deleted_at IS NULL` over
             (customer_code, customer, log_date DESC) and actually serves the
             reader query (break-it-once: dropping it forces a temp b-tree);
             the file does NOT self-stamp applied_migrations (the runner does).
  rollback — native ops on the CURRENT table, so a row inserted AFTER the
             forward migration survives with every other column intact;
             the three pre-existing indexes come back byte-identical;
             the runner's applied_migrations row is removed and only that one;
             sqlite_master for the table is byte-identical to pre-forward.
"""
import itertools
import os
import sqlite3

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_160 = os.path.join(
    REPO, "data", "migrations", "160_ar_followup_soft_delete.sql")
ROLLBACK_159 = os.path.join(
    REPO, "data", "migrations", "160_ar_followup_soft_delete.rollback.sql")
MIG_160_NAME = "160_ar_followup_soft_delete.sql"

INDEX_NAME = "idx_ar_followup_active_customer"
ORIGINAL_INDEXES = (
    "idx_ar_followup_customer",
    "idx_ar_followup_next_action",
    "idx_ar_followup_log_date",
)

# The per-customer, newest-first read every follow-up reader performs after
# Task 4 adds its `deleted_at IS NULL` filter.
READER_QUERY = (
    "SELECT id, log_date, result FROM ar_followup_log "
    "WHERE customer_code=? AND customer=? AND deleted_at IS NULL "
    "ORDER BY log_date DESC"
)


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


def _cols(conn):
    return {r["name"]: r for r in conn.execute("PRAGMA table_info(ar_followup_log)")}


def _master(conn):
    """sqlite_master rows for everything hanging off ar_followup_log, as
    comparable text (the table DDL plus each index DDL)."""
    return [
        (r["type"], r["name"], r["sql"])
        for r in conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE tbl_name='ar_followup_log' ORDER BY type, name"
        )
    ]


_probe_counter = itertools.count()


def _plan(conn, sql, params):
    """EXPLAIN QUERY PLAN, forced to re-prepare on every call.

    ⚠ Python's sqlite3 caches prepared statements BY SQL TEXT per connection,
    and an EQP statement prepared before a `DROP INDEX` keeps reporting the
    OLD plan afterwards — measured on this interpreter (3.9.6 / SQLite 3.51.0):
    after dropping the index the cached probe still answered
    "SEARCH ... USING COVERING INDEX ix". Without the unique trailing comment
    the break-it-once half below is incapable of failing.
    """
    probe = "\n-- plan probe %d" % next(_probe_counter)
    rows = conn.execute("EXPLAIN QUERY PLAN " + sql + probe, params).fetchall()
    return "\n".join(r["detail"] for r in rows)


@pytest.fixture
def pre160_conn(empty_db_conn):
    if "deleted_at" in _cols(empty_db_conn):
        _apply(empty_db_conn, ROLLBACK_159)
    return empty_db_conn


def _insert_log(conn, customer, code, log_date, notes, **extra):
    """One ar_followup_log row. `extra` carries deleted_at/deleted_by when the
    caller is writing a post-forward row."""
    cols = ["customer", "customer_code", "log_date", "channel", "result",
            "promised_amount", "notes", "created_by"]
    vals = [customer, code, log_date, "phone", "promised", 1500.5, notes, "tester"]
    for k, v in extra.items():
        cols.append(k)
        vals.append(v)
    placeholders = ",".join("?" for _ in cols)
    cur = conn.execute(
        "INSERT INTO ar_followup_log (%s) VALUES (%s)" % (",".join(cols), placeholders),
        vals,
    )
    conn.commit()
    return cur.lastrowid


def _row_without_soft_delete_cols(conn, log_id):
    r = conn.execute(
        "SELECT id, customer, customer_code, log_date, channel, result, "
        "       contact_person, promised_amount, promised_date, next_action_date, "
        "       notes, created_by, created_at, updated_at "
        "  FROM ar_followup_log WHERE id=?",
        (log_id,),
    ).fetchone()
    return tuple(r) if r is not None else None


# ── forward migration ────────────────────────────────────────────────────────

def test_forward_adds_both_columns_as_nullable_text(pre160_conn):
    before = _cols(pre160_conn)
    assert "deleted_at" not in before, "fixture failed to reconstruct pre-160 state"
    assert "deleted_by" not in before

    _apply(pre160_conn, MIG_160)

    after = _cols(pre160_conn)
    for name in ("deleted_at", "deleted_by"):
        assert name in after, f"{name} missing after forward migration"
        col = after[name]
        assert col["type"] == "TEXT", f"{name} declared {col['type']!r}, expected TEXT"
        assert col["notnull"] == 0, f"{name} must be nullable (NULL = live row)"
        assert col["dflt_value"] is None, f"{name} must have no DEFAULT"


def test_forward_creates_partial_index_over_live_rows(pre160_conn):
    _apply(pre160_conn, MIG_160)

    row = pre160_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (INDEX_NAME,)
    ).fetchone()
    assert row is not None, f"{INDEX_NAME} was not created"
    assert "WHERE deleted_at IS NULL" in row["sql"], (
        "index must be PARTIAL on the live rows — got: " + row["sql"])

    meta = {
        r["name"]: r
        for r in pre160_conn.execute("SELECT * FROM pragma_index_list('ar_followup_log')")
    }
    assert meta[INDEX_NAME]["partial"] == 1
    assert meta[INDEX_NAME]["unique"] == 0, (
        "a customer legitimately has many follow-up rows — this index must not be UNIQUE")

    cols = [
        (r["name"], r["desc"])
        for r in pre160_conn.execute(
            "SELECT * FROM pragma_index_xinfo(?) WHERE key=1 ORDER BY seqno", (INDEX_NAME,))
    ]
    assert cols == [("customer_code", 0), ("customer", 0), ("log_date", 1)], (
        "index key columns/direction changed — the reader's ORDER BY log_date DESC "
        "stops being satisfiable from the index: " + repr(cols))


def test_forward_index_serves_the_reader_query_and_break_it_once(pre160_conn):
    """A guard that has never gone red is not a test: show the planner uses
    this index (and needs no temp b-tree for the ORDER BY), then drop it and
    show the same query degrades to a temp b-tree sort."""
    _apply(pre160_conn, MIG_160)
    params = ("C-1", "ร้านทดสอบ")

    with_index = _plan(pre160_conn, READER_QUERY, params)
    assert INDEX_NAME in with_index, with_index
    assert "TEMP B-TREE" not in with_index.upper(), (
        "ORDER BY log_date DESC should come straight off the index: " + with_index)

    pre160_conn.execute("DROP INDEX %s" % INDEX_NAME)
    without_index = _plan(pre160_conn, READER_QUERY, params)
    assert INDEX_NAME not in without_index
    assert "TEMP B-TREE" in without_index.upper(), (
        "guard removed but the plan did not change — this test cannot fail: "
        + without_index)

    # restore, so the fixture's DB is left in the state the migration defines
    _apply(pre160_conn, ROLLBACK_159)
    _apply(pre160_conn, MIG_160)
    assert INDEX_NAME in _plan(pre160_conn, READER_QUERY, params)


def test_forward_keeps_the_three_original_indexes_byte_identical(pre160_conn):
    before = {
        name: sql for typ, name, sql in _master(pre160_conn) if typ == "index"
    }
    assert set(ORIGINAL_INDEXES) <= set(before), (
        "pre-160 state is missing an original index: " + repr(sorted(before)))

    _apply(pre160_conn, MIG_160)

    after = {name: sql for typ, name, sql in _master(pre160_conn) if typ == "index"}
    for name in ORIGINAL_INDEXES:
        assert name in after, f"{name} disappeared during the forward migration"
        assert after[name] == before[name], f"{name} DDL changed: {after[name]!r}"


def test_forward_does_not_self_stamp_applied_migrations(pre160_conn):
    """The runner owns the bookkeeping (filename/sha256/duration_ms). A
    self-INSERT here would win the filename PRIMARY KEY and turn the runner's
    INSERT OR IGNORE into a no-op, landing the row with sha256 NULL."""
    _apply(pre160_conn, MIG_160)

    stamped = pre160_conn.execute(
        "SELECT COUNT(*) AS n FROM applied_migrations WHERE filename=?", (MIG_160_NAME,)
    ).fetchone()["n"]
    assert stamped == 0, "migration 160 stamped its own applied_migrations row"

    # CONTROL: the same query DOES see a row when one exists, so the assertion
    # above is not passing merely because the lookup is broken.
    pre160_conn.execute(
        "INSERT INTO applied_migrations (filename, applied_by) VALUES (?, 'control')",
        (MIG_160_NAME,),
    )
    assert pre160_conn.execute(
        "SELECT COUNT(*) AS n FROM applied_migrations WHERE filename=?", (MIG_160_NAME,)
    ).fetchone()["n"] == 1


def test_runner_applies_it_and_writes_the_stamp(pre160_conn):
    """Drive the REAL runner (database.run_pending_migrations), not a hand
    executescript: it is what decides 160 is pending, executes it, and records
    filename/sha256/duration_ms. `applied_by == 'auto'` plus a non-NULL sha256
    is the positive proof the runner (not the file) wrote the row."""
    import database

    # Mark every other migration as already applied so the runner takes its
    # normal pending path instead of the bootstrap backfill (which fires only
    # when applied_migrations is empty).
    files = database._list_migration_files()
    assert MIG_160_NAME in files, "160 is not visible to the runner"
    pre160_conn.executemany(
        "INSERT INTO applied_migrations (filename, applied_by) VALUES (?, 'test-preseed')",
        [(f,) for f in files if f != MIG_160_NAME],
    )
    pre160_conn.commit()

    ran = database.run_pending_migrations(pre160_conn, verbose=False)

    assert ran == [MIG_160_NAME], f"runner ran {ran!r}"
    assert "deleted_at" in _cols(pre160_conn), "runner stamped without applying?"

    row = pre160_conn.execute(
        "SELECT applied_by, sha256, duration_ms FROM applied_migrations WHERE filename=?",
        (MIG_160_NAME,),
    ).fetchone()
    assert row is not None, "runner did not record the migration"
    assert row["applied_by"] == "auto"
    assert row["sha256"] == database._file_sha256(MIG_160), (
        "recorded checksum is not this file's — the file self-stamped and the "
        "runner's INSERT OR IGNORE was a no-op")
    assert row["duration_ms"] is not None


# ── rollback ─────────────────────────────────────────────────────────────────

def test_rollback_removes_both_columns_and_the_index(pre160_conn):
    _apply(pre160_conn, MIG_160)
    assert "deleted_at" in _cols(pre160_conn)  # control: forward really ran

    _apply(pre160_conn, ROLLBACK_159)

    cols = _cols(pre160_conn)
    assert "deleted_at" not in cols
    assert "deleted_by" not in cols
    assert pre160_conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (INDEX_NAME,)
    ).fetchone() is None


def test_rollback_preserves_rows_inserted_after_the_forward_migration(pre160_conn):
    """The whole point of native-op rollback: DROP COLUMN rewrites the CURRENT
    table, so outreach the team logged AFTER the migration shipped survives.
    A snapshot-restore rollback would silently delete it."""
    pre_id = _insert_log(pre160_conn, "ร้านก่อน", "C-PRE", "2026-08-01", "pre-forward row")

    _apply(pre160_conn, MIG_160)

    post_id = _insert_log(
        pre160_conn, "ร้านหลัง", "C-POST", "2026-08-15", "post-forward row",
        deleted_at="2026-08-15 10:00:00", deleted_by="admin",
    )
    soft_deleted_id = _insert_log(
        pre160_conn, "ร้านลบ", "C-DEL", "2026-08-14", "soft-deleted row",
        deleted_at="2026-08-15 11:00:00", deleted_by="admin",
    )

    # CONTROL: all three rows are really there before the rollback, so the
    # comparison below cannot pass over an empty set.
    n_before = pre160_conn.execute(
        "SELECT COUNT(*) AS n FROM ar_followup_log").fetchone()["n"]
    assert n_before == 3
    before = {
        i: _row_without_soft_delete_cols(pre160_conn, i)
        for i in (pre_id, post_id, soft_deleted_id)
    }

    _apply(pre160_conn, ROLLBACK_159)

    n_after = pre160_conn.execute(
        "SELECT COUNT(*) AS n FROM ar_followup_log").fetchone()["n"]
    assert n_after == 3, (
        f"rollback lost rows: {n_before} -> {n_after} (a snapshot-restore rollback "
        "would look exactly like this)")
    after = {
        i: _row_without_soft_delete_cols(pre160_conn, i)
        for i in (pre_id, post_id, soft_deleted_id)
    }
    assert after == before, "surviving rows changed value across the rollback"


def test_rollback_keeps_the_three_original_indexes_byte_identical(pre160_conn):
    before = {name: sql for typ, name, sql in _master(pre160_conn) if typ == "index"}
    assert set(ORIGINAL_INDEXES) <= set(before)

    _apply(pre160_conn, MIG_160)
    _apply(pre160_conn, ROLLBACK_159)

    after = {name: sql for typ, name, sql in _master(pre160_conn) if typ == "index"}
    assert set(after) == set(before), (
        f"index set changed across forward+rollback: {sorted(after)} vs {sorted(before)}")
    for name in ORIGINAL_INDEXES:
        assert after[name] == before[name], f"{name} DDL changed: {after[name]!r}"


def test_rollback_removes_only_this_migrations_stamp(pre160_conn):
    _apply(pre160_conn, MIG_160)
    pre160_conn.executemany(
        "INSERT INTO applied_migrations (filename, applied_by, sha256, duration_ms) "
        "VALUES (?, 'auto', 'deadbeef', 3)",
        [(MIG_160_NAME,), ("158_conversion_input_role.sql",)],
    )
    pre160_conn.commit()
    assert pre160_conn.execute(
        "SELECT COUNT(*) AS n FROM applied_migrations").fetchone()["n"] == 2

    _apply(pre160_conn, ROLLBACK_159)

    names = {r["filename"] for r in pre160_conn.execute(
        "SELECT filename FROM applied_migrations")}
    assert MIG_160_NAME not in names, "rollback left the runner's stamp behind"
    assert "158_conversion_input_role.sql" in names, (
        "rollback deleted a neighbouring migration's stamp — the DELETE is not scoped")


def test_rollback_index_drop_must_precede_the_column_drop(pre160_conn):
    """Pins the ORDER-IS-LOAD-BEARING claim in the rollback file's header:
    SQLite refuses to drop a column that a partial index's WHERE clause
    references, so a rollback that dropped the columns first would fail."""
    _apply(pre160_conn, MIG_160)
    with pytest.raises(sqlite3.OperationalError) as exc:
        pre160_conn.execute("ALTER TABLE ar_followup_log DROP COLUMN deleted_at")
    assert INDEX_NAME in str(exc.value)


def test_rollback_leaves_sqlite_master_byte_identical_to_pre_forward(pre160_conn):
    before = _master(pre160_conn)
    assert before, "no sqlite_master objects for ar_followup_log — fixture is wrong"

    _apply(pre160_conn, MIG_160)
    assert _master(pre160_conn) != before  # control: forward really changed it

    _apply(pre160_conn, ROLLBACK_159)

    assert _master(pre160_conn) == before, (
        "forward+rollback did not restore the table/index DDL byte-for-byte")


def test_forward_rollback_forward_is_clean(pre160_conn):
    """The index half is drop-first, so a re-apply after a rollback must not
    hit 'index already exists' / 'duplicate column name'."""
    _apply(pre160_conn, MIG_160)
    _apply(pre160_conn, ROLLBACK_159)
    _apply(pre160_conn, MIG_160)

    cols = _cols(pre160_conn)
    assert "deleted_at" in cols and "deleted_by" in cols
    assert pre160_conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (INDEX_NAME,)
    ).fetchone() is not None


# ── invariant the rollback depends on ────────────────────────────────────────

def test_no_trigger_or_view_depends_on_ar_followup_log(pre160_conn):
    """Both migration files assert that nothing but the table and its indexes
    reference ar_followup_log. If a later change adds a trigger or view over
    it, `ALTER TABLE ... DROP COLUMN` in the rollback can start failing —
    this pins that assumption so it fails here instead of during a rollback."""
    _apply(pre160_conn, MIG_160)

    # CONTROL: the LIKE pattern really does match objects on this table (an
    # empty result below must mean "no trigger/view", not "the query is broken").
    matched_any = [
        r["name"] for r in pre160_conn.execute(
            "SELECT name FROM sqlite_master WHERE sql LIKE '%ar_followup_log%'")
    ]
    assert INDEX_NAME in matched_any and "ar_followup_log" in matched_any, matched_any

    others = [
        (r["type"], r["name"])
        for r in pre160_conn.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE sql LIKE '%ar_followup_log%' AND type IN ('trigger','view')")
    ]
    assert others == [], (
        "a trigger/view now references ar_followup_log: " + repr(others)
        + " — re-verify migration 160's rollback (DROP COLUMN) against it")
