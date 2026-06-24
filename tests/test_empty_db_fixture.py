"""Guard test for the `empty_db` fixture.

`empty_db` is a SCHEMA-ONLY CLONE of the live DB (zero rows). A from-empty
`database.init_db()` now works too (it builds from data/schema.sql — see
test_fresh_db_build.py), but the fixture still clones the LIVE schema so it
reflects the exact current schema including any drift not yet folded into
schema.sql. This test fails loudly if someone reverts the fixture to a partial
hand-built schema.
"""
import pytest

# Tables the broken-replay path never reached (commission_* are created by the
# very migrations that FK-failed) plus the core tables the 14 affected tests use.
# NOTE: commission_product_overrides is created by mig 018 then DROPPed by mig
# 019, so it is intentionally NOT in the final live schema — use the surviving
# commission_overrides instead to prove the 018→019 chain's schema is present.
REQUIRED_TABLES = [
    "customers",
    "sales_transactions",
    "purchase_transactions",
    "stock_levels",
    "unit_conversions",
    "transactions",
    "commission_assignments",  # mig 014 — survives
    "commission_overrides",    # mig 019 — proves 018→019 chain schema present
]


def _names(conn, type_):
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type=?", (type_,)
        )
    }


def test_empty_db_has_full_schema(empty_db_conn):
    tables = _names(empty_db_conn, "table")
    missing = [t for t in REQUIRED_TABLES if t not in tables]
    assert not missing, f"empty_db schema incomplete, missing: {missing}"
    # full live schema, not a hand-picked subset
    assert len(tables) >= 50, f"expected full schema (~62 tables), got {len(tables)}"


def test_empty_db_is_dataless(empty_db_conn):
    for t in REQUIRED_TABLES:
        n = empty_db_conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        assert n == 0, f"{t} should be empty in empty_db, has {n} rows"


def test_empty_db_has_triggers(empty_db_conn):
    triggers = _names(empty_db_conn, "trigger")
    # the stock ledger trigger is relied on by unit-conversion / WACC tests
    assert "after_transaction_insert" in triggers, (
        f"after_transaction_insert trigger missing; "
        f"schema clone dropped triggers (got {len(triggers)})"
    )


def test_empty_db_views_present(empty_db_conn):
    views = _names(empty_db_conn, "view")
    assert "products_full" in views, "products_full VIEW missing from empty_db"
