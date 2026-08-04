"""Shared fixture helper for the mig-124 (`product_code_mapping.bsn_unit`) replay.

Why this exists (Codex review finding 4, 2026-08-03)
----------------------------------------------------
When these tests were written the local live DB had NOT had mig 124 applied, so
each one replayed `124_restore_mapping_bsn_unit.sql` onto its own `tmp_db` copy
just to get the `bsn_unit` column. The live DB has had mig 124 since 2026-07-02
and now carries REAL split rows (21 codes billed in two units as of 2026-08-03).
Mig 124 rebuilds the table with `bsn_unit=''` for every row, so replaying it on
a current schema collapses those pairs and trips
`UNIQUE(bsn_code, bsn_unit)` — 26 tests died during setup, before reaching a
single assertion.

`apply_mig124_if_needed()` makes the replay idempotent: it runs the migration
only when the column is ABSENT, which is exactly what every caller wanted
("guarantee the schema is unit-aware"), and is a no-op on a current schema.

`reset_codes()` clears real mapping rows for the synthetic codes a test is about
to seed. Only needed where a test seeds a code that also exists in production
data (the real split code `030บ3412`); the other suites use Z-/C-prefixed codes
that never collide.

Neither helper weakens production's composite UNIQUE, and no migration was
added — the fix is entirely in test setup.

`rollback_mig124()` + `apply_mig124()` are the drop-first pair for the ONE test
that exercises the migration's own mechanics, mirroring
`tests/test_mig134_product_generic_standins.py::pre134_conn`.
"""
import os

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_124 = os.path.join(_REPO, "data", "migrations",
                       "124_restore_mapping_bsn_unit.sql")
ROLLBACK_124 = os.path.join(_REPO, "data", "migrations",
                            "124_restore_mapping_bsn_unit.rollback.sql")


def _exec_script(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


def has_bsn_unit(conn):
    return any(r[1] == "bsn_unit"
               for r in conn.execute("PRAGMA table_info(product_code_mapping)"))


def apply_mig124(conn):
    """Run the forward migration unconditionally (mechanics test only)."""
    _exec_script(conn, MIG_124)


def rollback_mig124(conn):
    """Reconstruct the genuine pre-124 state: drop bsn_unit and collapse back to
    UNIQUE(bsn_code). Also removes 124's `applied_migrations` row — harmless on a
    tmp_db copy, which is the only place this is ever called."""
    _exec_script(conn, ROLLBACK_124)


def apply_mig124_if_needed(conn):
    """Ensure `product_code_mapping` is unit-aware. Returns True if the migration
    actually ran (i.e. the DB predated it), False if it was already current."""
    if has_bsn_unit(conn):
        return False
    apply_mig124(conn)
    return True


def reset_codes(conn, *codes):
    """Drop any pre-existing mapping rows for `codes` so a test's own seed is
    deterministic against a live-DB copy."""
    for code in codes:
        conn.execute("DELETE FROM product_code_mapping WHERE bsn_code=?", (code,))
    conn.commit()
