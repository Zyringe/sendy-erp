"""Regression: a from-empty `init_db()` (bare `git clone` + `sendy-up`) must
complete without error.

Migrations 014 (commission_assignments → salespersons) and 018
(commission_product_overrides → products, hardcoded product_id=398) seed rows
that FK-reference data only present after a real import. They are guarded with
`WHERE EXISTS` so a from-empty build no-ops those seeds instead of raising
`FOREIGN KEY constraint failed`. On a seeded DB every target row exists, so the
seeds insert exactly as before (and on prod the migrations are already applied
and never re-run). See the 014/018 migration headers.
"""
import os
import sqlite3

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_LIVE_DB = os.path.join(_REPO, "inventory_app", "instance", "inventory.db")


def _schema_fingerprint(conn):
    """(objects, columns) of a DB, excluding sqlite internals and the forensic
    `migration_*_snapshot` cruft that schema.sql intentionally drops.

    objects = {(type, name)} over table/index/trigger/view; columns = {(table,
    column)}. Compares names/shape (the ALTER-drift failure mode), not full DDL
    text (which would be brittle to whitespace/ordering)."""
    objs = {
        (typ, name) for typ, name in conn.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' "
            "  AND tbl_name NOT LIKE 'migration\\_%' ESCAPE '\\'"
        )
    }
    cols = set()
    tables = [t for (t,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "  AND name NOT LIKE 'sqlite_%' "
        "  AND name NOT LIKE 'migration\\_%' ESCAPE '\\'"
    )]
    for t in tables:
        for r in conn.execute(f'PRAGMA table_info("{t}")'):
            cols.add((t, r[1]))
    return objs, cols


def test_schema_sql_in_sync_with_live(tmp_path, monkeypatch):
    """data/schema.sql must match the live schema. If this FAILS after a
    migration, regenerate it: `scripts/dump_schema.py` (then commit).

    Without this guard a stale schema.sql would silently build fresh DBs with
    the wrong schema while the runner stamps every migration as already-applied
    — so the missing change would never self-heal. (Blind spot: the few columns/
    tables init_db re-adds inline — synced_to_stock,
    doc_base, ref_invoice, conversion_formulas/customers/*_cost_* — are
    self-healed regardless, so drift in those is harmless and not flagged.)
    """
    if not os.path.exists(_LIVE_DB):
        pytest.skip(f"live DB not found at {_LIVE_DB}")

    db_path = str(tmp_path / "fresh.db")
    import config
    import database
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(database, "DATABASE_PATH", db_path)
    database.init_db()

    fresh = sqlite3.connect(db_path)
    live = sqlite3.connect(f"file:{_LIVE_DB}?mode=ro", uri=True)
    try:
        f_objs, f_cols = _schema_fingerprint(fresh)
        l_objs, l_cols = _schema_fingerprint(live)
    finally:
        fresh.close()
        live.close()

    msg = (
        "data/schema.sql is OUT OF SYNC with the live schema — regenerate with "
        "scripts/dump_schema.py.\n"
        f"  objects missing from schema.sql: {sorted(l_objs - f_objs)[:10]}\n"
        f"  objects only in schema.sql:      {sorted(f_objs - l_objs)[:10]}\n"
        f"  columns missing from schema.sql: {sorted(l_cols - f_cols)[:10]}\n"
        f"  columns only in schema.sql:      {sorted(f_cols - l_cols)[:10]}"
    )
    assert (f_objs, f_cols) == (l_objs, l_cols), msg


def test_fresh_build_requires_schema_sql(tmp_path, monkeypatch):
    """A from-empty build with schema.sql missing must fail LOUD, not silently
    fall back to the broken migration replay."""
    db_path = str(tmp_path / "fresh.db")
    import config
    import database
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(database, "DATABASE_PATH", db_path)
    monkeypatch.setattr(database, "SCHEMA_SQL_PATH", str(tmp_path / "missing.sql"))
    with pytest.raises(RuntimeError, match="schema.sql"):
        database.init_db()


def test_init_db_from_empty_completes(tmp_path, monkeypatch):
    """A fresh DB built purely by replaying every migration must not crash."""
    db_path = str(tmp_path / "fresh.db")

    import config
    import database
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(database, "DATABASE_PATH", db_path)

    # The whole point: this must not raise FOREIGN KEY constraint failed.
    database.init_db()

    conn = sqlite3.connect(db_path)
    try:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        # The commission tables that used to FK-fail now exist...
        assert "commission_assignments" in tables
        assert "commission_overrides" in tables
        # ...and are empty (no salespersons/products to seed against in a
        # data-less build).
        assert conn.execute(
            "SELECT COUNT(*) FROM commission_assignments"
        ).fetchone()[0] == 0
        # FK integrity is intact across the whole fresh schema.
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_init_db_from_empty_seeds_unit_map(tmp_path, monkeypatch):
    """#596 (team-lead review): a brand-new DB built from data/schema.sql
    gets the unit_map TABLE (schema.sql is DDL only) but none of migration
    185's ROWS — run_pending_migrations()'s bootstrap-backfill path records
    every migration as already-applied without re-running it. Without
    init_db()'s explicit re-seed, bsn_units.py would silently treat every
    Express code as unknown on a fresh install (bare git clone, empty
    Railway volume) — the exact failure #595 exists to prevent."""
    db_path = str(tmp_path / "fresh.db")

    import config
    import database
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(database, "DATABASE_PATH", db_path)

    database.init_db()

    conn = sqlite3.connect(db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM unit_map WHERE book = 'BSN5657'"
        ).fetchone()[0]
        assert n == 44, f"expected the 44 real seed rows, found {n}"
        word = conn.execute(
            "SELECT word FROM unit_map WHERE book = 'BSN5657' AND spelling = 'กร'"
        ).fetchone()
        assert word == ('ตัว',), "a fresh build must carry migration 185's REAL data, not a placeholder"
    finally:
        conn.close()


def _unit_map_rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return sorted(conn.execute("SELECT id, book, spelling, word FROM unit_map"))
    finally:
        conn.close()


def test_init_db_unit_map_seed_is_idempotent_and_keeps_learned_rows(tmp_path, monkeypatch):
    """#596: the fresh-DB seed fires only on an EMPTY unit_map. A second boot
    must not re-seed, and a map Put has since changed (a code named on
    /unit-conversions, a meaning corrected) must come through a boot row for
    row, ids included, since re-running 185's drop-first SQL would erase it."""
    db_path = str(tmp_path / "fresh.db")
    import bsn_units
    import config
    import database
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(database, "DATABASE_PATH", db_path)

    database.init_db()
    first = _unit_map_rows(db_path)
    assert len(first) == 44                          # control: the seed ran
    database.init_db()
    assert _unit_map_rows(db_path) == first          # idempotent

    conn = sqlite3.connect(db_path)
    try:
        bsn_units.learn('ZZ596', 'BSN5657', 'ทดสอบ', conn=conn)
        bsn_units.learn('กร', 'BSN5657', 'กุรุส', conn=conn)
        conn.commit()
    finally:
        conn.close()
    learned = _unit_map_rows(db_path)
    assert len(learned) == 45 and learned != first   # control: the edits landed

    database.init_db()
    assert _unit_map_rows(db_path) == learned


def test_no_later_migration_writes_unit_map(tmp_path):
    """init_db() seeds a FRESH db's unit_map by replaying migration 185 only:
    a fresh db is built from schema.sql (DDL) and every migration is then
    stamped as applied without running. So a later migration that writes
    unit_map (#599/#601 will: กร -> กุรุส, xp5 หอ -> หลอด) would be silently
    missing from every fresh db, including the VAT book's build db and CI.
    When this goes red, replay that migration in init_db's seed after 185
    (keeping it empty-table-only), then add it to REPLAYED here."""
    import re
    REPLAYED = set()
    mig_dir = os.path.join(_REPO, "data", "migrations")
    writes = re.compile(
        r"\b(INSERT(\s+OR\s+\w+)?\s+INTO|UPDATE(\s+OR\s+\w+)?|DELETE\s+FROM|REPLACE\s+INTO)"
        r"\s+[\"'`]?unit_map\b", re.I)

    def later_writers(directory):
        out = set()
        for name in os.listdir(directory):
            if (not name.endswith(".sql") or name.endswith(".rollback.sql")
                    or name <= "185_unit_map_table.sql"):
                continue
            with open(os.path.join(directory, name), encoding="utf-8") as f:
                sql = re.sub(r"--[^\n]*", "", f.read())
            if writes.search(sql):
                out.add(name)
        return out

    # control: the scanner fires on a write and ignores a mention in a comment
    # (186's header names unit_map in a comment and writes nothing to it)
    (tmp_path / "190_writes.sql").write_text(
        "-- unit_map\nUPDATE unit_map SET word = 'กุรุส' WHERE spelling = 'กร';\n",
        encoding="utf-8")
    (tmp_path / "191_mentions.sql").write_text(
        "-- reads the unit_map table\nSELECT 1;\n", encoding="utf-8")
    assert later_writers(tmp_path) == {"190_writes.sql"}

    assert "185_unit_map_table.sql" in os.listdir(mig_dir)   # the real dir was read
    assert later_writers(mig_dir) - REPLAYED == set()
