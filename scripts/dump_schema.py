#!/usr/bin/env python3
"""Regenerate data/schema.sql — the COMPLETE current Sendy schema baseline.

`database.init_db()` applies this to a brand-new DB (bare `git clone` + first
`sendy-up`) in one shot instead of replaying the 112-migration history (which
the embedded partial SCHEMA + the historical ALTER chain cannot survive — a
from-empty replay hits duplicate-column and unseeded-FK errors). Once this
schema is in place, `run_pending_migrations()` sees the `brands` table and
backfills every shipped migration as already-applied.

Dump order is table → index → trigger → view so CREATE TRIGGER/VIEW always
follow the tables they reference. sqlite_* internal objects are excluded, and
only DDL is emitted, except for the reference-data tables in DATA_TABLES,
whose rows follow the DDL. A fresh DB runs no migration, so rows a migration
seeds (or a later one changes) reach it only this way.

Run:  ~/.virtualenvs/erp/bin/python scripts/dump_schema.py
Source of truth = the live DB at inventory_app/instance/inventory.db.
Re-run + commit whenever a migration changes the schema, or changes the rows
of a DATA_TABLES table (tests/test_fresh_db_build.py checks both against the
live DB).
"""
import os
import sqlite3

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_DB = os.path.join(REPO, "inventory_app", "instance", "inventory.db")
OUT = os.path.join(REPO, "data", "schema.sql")

HEADER = """\
-- schema.sql — COMPLETE current Sendy schema baseline (AUTO-GENERATED).
-- Do not hand-edit. Regenerate with: scripts/dump_schema.py
--
-- Applied by database.init_db() to build a brand-new DB in one shot (bare
-- `git clone` + first `sendy-up`), instead of replaying the migration history.
-- After it applies, run_pending_migrations() backfills all shipped migrations
-- as already-applied (it keys on the `brands` table existing).
--
-- Re-run dump_schema.py and commit whenever a migration changes the schema
-- or the rows of a reference-data table (unit_map), which close this file.

PRAGMA foreign_keys = OFF;
BEGIN;
"""

FOOTER = "\nCOMMIT;\nPRAGMA foreign_keys = ON;\n"

# Reference data a working DB cannot do without: table -> (columns, ORDER BY).
# unit_map (#596): an empty map imports every Express unit code untranslated.
DATA_TABLES = {
    "unit_map": (("book", "spelling", "word"), "book, spelling"),
}


def _sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def _data_sql(src):
    out = []
    for table, (cols, order_by) in DATA_TABLES.items():
        rows = src.execute(
            f"SELECT {', '.join(cols)} FROM {table} ORDER BY {order_by}").fetchall()
        if not rows:
            raise SystemExit(f"{table} is empty in the live DB; refusing to dump "
                             "a fresh-DB baseline without its reference data")
        values = ",\n".join(
            "  (" + ", ".join(_sql_literal(v) for v in row) + ")" for row in rows)
        out.append(f"-- data: {table} ({len(rows)} rows)\n"
                   f"INSERT INTO {table} ({', '.join(cols)}) VALUES\n{values};\n")
    return "\n".join(out)


def main():
    if not os.path.exists(LIVE_DB):
        raise SystemExit(f"live DB not found at {LIVE_DB}")
    src = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    try:
        # Exclude sqlite internals and the forensic `migration_NNN_snapshot*`
        # tables (leftover from data-transform migrations 077/078/081-085) — they
        # are not part of the logical schema and a fresh dev DB shouldn't carry
        # them. tbl_name filter also drops any index/trigger sitting on them.
        objects = src.execute(
            """SELECT sql FROM sqlite_master
                WHERE sql IS NOT NULL
                  AND name NOT LIKE 'sqlite_%'
                  AND tbl_name NOT LIKE 'migration\\_%' ESCAPE '\\'
                ORDER BY CASE type
                    WHEN 'table' THEN 0 WHEN 'index' THEN 1
                    WHEN 'trigger' THEN 2 WHEN 'view' THEN 3 ELSE 4 END,
                    name"""
        ).fetchall()
        data = _data_sql(src)
    finally:
        src.close()

    body = ";\n\n".join(sql.strip().rstrip(";") for (sql,) in objects) + ";\n"
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(HEADER + "\n" + body + "\n" + data + FOOTER)
    print(f"wrote {OUT} ({len(objects)} schema objects, data: {', '.join(DATA_TABLES)})")


if __name__ == "__main__":
    main()
