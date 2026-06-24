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
import sqlite3

import pytest


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
