"""Migration 134 — product_generic_standins (curated generic stand-in table).

Background (design doc: Operations/05_analysis-reports/engineering/
generic-standin-schema-design_2026-07-10.md, Put-approved 2026-07-10): some
color/size-specific SKUs are individually stocked and family-grouped, but
Express books their marketplace sales under one generic catch-all product
instead of the specific variant. This table records that curated equivalence
at the PRODUCT level (not family level) so a later matcher change can consume
it as a fallback candidate.

Tests (deterministic, on the schema-only empty_db):
  1. Applying the migration creates the table + all 18 seeded rows.
  2. Every one of the 21 referenced product ids (3 generic + 18 variant)
     actually exists in `products` (guards against a curation typo).
  3. variant_product_id is unique per generic (no accidental duplicate row).
  4. The CHECK constraint rejects a self-referencing row (variant == generic).
  5. Rollback drops the table cleanly (no trace, re-apply works from scratch).
  6. Re-applying the seed a second time does NOT duplicate rows (UNIQUE
     constraint on (variant_product_id, generic_product_id) makes a raw
     re-run fail loud rather than silently double-seed — this pins that
     migrations are meant to run once, matching the runner's applied_
     migrations bookkeeping, not smoke-tested as idempotent like a relabel
     migration would be).
  7. Invariant pin: no source file other than a migration/test currently
     references product_generic_standins (the matcher consumer is a LATER
     session's job — this test locks in "nothing new quietly started
     consuming it" until that lands).
"""
import os
import sqlite3
import subprocess

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_134 = os.path.join(
    REPO, "data", "migrations", "134_product_generic_standins.sql")
ROLLBACK_134 = os.path.join(
    REPO, "data", "migrations", "134_product_generic_standins.rollback.sql")

# The 18 curated (variant, generic) pairs from the migration's seed.
_FAMILY_441 = [519, 520, 521, 522, 523, 524, 525]   # -> 908 เฉพาะหัวสายชำระ Sendai
_FAMILY_443 = [512, 513, 514, 515, 516, 517, 518]   # -> 907 เฉพาะหัวฝักบัว Sendai
_FAMILY_458_46 = [982, 983, 2016, 2017]              # -> 848 ลูกรีเวท Sendai 4-6

EXPECTED_PAIRS = (
    [(v, 908) for v in _FAMILY_441]
    + [(v, 907) for v in _FAMILY_443]
    + [(v, 848) for v in _FAMILY_458_46]
)


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


@pytest.fixture
def pre134_conn(empty_db_conn):
    """empty_db clones the LIVE local DB schema, which already contains
    product_generic_standins on any machine where mig 134 has been applied
    (live local got it 2026-07-11) — re-applying the migration there raises
    'table already exists'. Reconstruct the true pre-134 state by running the
    rollback (exactly the drop of everything 134 creates) before each
    apply-test."""
    _apply(empty_db_conn, ROLLBACK_134)
    return empty_db_conn


def _seed_products(conn, ids):
    """Minimal products rows so the migration's WHERE EXISTS guard and its
    FK references are satisfied (empty_db_conn carries the schema only)."""
    conn.executemany(
        "INSERT INTO products (id, product_name) VALUES (?, ?)",
        [(pid, f"test product {pid}") for pid in ids],
    )
    conn.commit()


def _all_referenced_pids():
    return sorted({908, 907, 848} | {v for v, _g in EXPECTED_PAIRS})


def test_migration_creates_table_and_seeds_18_rows(pre134_conn):
    _seed_products(pre134_conn, _all_referenced_pids())
    _apply(pre134_conn, MIG_134)
    rows = pre134_conn.execute(
        "SELECT variant_product_id, generic_product_id FROM product_generic_standins "
        "ORDER BY generic_product_id, variant_product_id"
    ).fetchall()
    got = sorted((r["variant_product_id"], r["generic_product_id"]) for r in rows)
    assert got == sorted(EXPECTED_PAIRS)
    assert len(got) == 18


def test_all_21_referenced_pids_exist(pre134_conn):
    """Guard against a curation typo: every pid the seed references must be a
    real product row before/after the migration runs."""
    pids = _all_referenced_pids()
    assert len(pids) == 21
    _seed_products(pre134_conn, pids)
    _apply(pre134_conn, MIG_134)
    for variant, generic in EXPECTED_PAIRS:
        for pid in (variant, generic):
            exists = pre134_conn.execute(
                "SELECT 1 FROM products WHERE id=?", (pid,)
            ).fetchone()
            assert exists, f"pid {pid} referenced by seed but missing from products"


def test_variant_unique_per_generic(pre134_conn):
    """UNIQUE(variant_product_id, generic_product_id) rejects an exact-duplicate
    curated pair (a real re-curation would be delete+insert, not a raw dup)."""
    _seed_products(pre134_conn, _all_referenced_pids())
    _apply(pre134_conn, MIG_134)
    with pytest.raises(sqlite3.IntegrityError):
        pre134_conn.execute(
            "INSERT INTO product_generic_standins (variant_product_id, generic_product_id) "
            "VALUES (519, 908)"
        )


def test_self_reference_rejected(pre134_conn):
    """CHECK(variant_product_id <> generic_product_id) — a product can never
    be curated as its own stand-in."""
    _seed_products(pre134_conn, _all_referenced_pids())
    _apply(pre134_conn, MIG_134)
    with pytest.raises(sqlite3.IntegrityError):
        pre134_conn.execute(
            "INSERT INTO product_generic_standins (variant_product_id, generic_product_id) "
            "VALUES (908, 908)"
        )


def test_missing_product_guard_no_ops_that_pair(pre134_conn):
    """WHERE EXISTS guard (fresh-build safety, mirrors migs 014/018): if a
    referenced pid doesn't exist, that pair is skipped rather than raising a
    FOREIGN KEY error — the table still gets created."""
    # Seed everything EXCEPT pid 848 (the 4-6 generic) to prove its pair-group
    # no-ops cleanly while the other two groups still seed normally.
    pids = [p for p in _all_referenced_pids() if p != 848]
    _seed_products(pre134_conn, pids)
    _apply(pre134_conn, MIG_134)
    n = pre134_conn.execute(
        "SELECT COUNT(*) FROM product_generic_standins"
    ).fetchone()[0]
    assert n == 14  # 7 (family 441) + 7 (family 443), the 848 group (4 rows) skipped
    n_848 = pre134_conn.execute(
        "SELECT COUNT(*) FROM product_generic_standins WHERE generic_product_id=848"
    ).fetchone()[0]
    assert n_848 == 0


def test_rollback_drops_table_cleanly(pre134_conn):
    _seed_products(pre134_conn, _all_referenced_pids())
    _apply(pre134_conn, MIG_134)
    _apply(pre134_conn, ROLLBACK_134)
    tables = {
        r[0] for r in pre134_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "product_generic_standins" not in tables
    # Re-apply from scratch must work identically after rollback.
    _apply(pre134_conn, MIG_134)
    n = pre134_conn.execute(
        "SELECT COUNT(*) FROM product_generic_standins"
    ).fetchone()[0]
    assert n == 18


def test_audit_log_records_seed_inserts(pre134_conn):
    _seed_products(pre134_conn, _all_referenced_pids())
    _apply(pre134_conn, MIG_134)
    n = pre134_conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE table_name='product_generic_standins' "
        "AND action='INSERT'"
    ).fetchone()[0]
    assert n == 18


def test_invariant_no_consumer_outside_marketplace_match_yet():
    """Pin: today, the only application source file referencing
    product_generic_standins is marketplace_match.py (Pass 1.5, 2026-07-11).
    This allow-list must stay single-file forever (the invariant this pins:
    never consulted by stock-deduction paths — see the migration's own
    invariant note)."""
    app_dir = os.path.join(REPO, "inventory_app")
    result = subprocess.run(
        ["grep", "-rl", "product_generic_standins", app_dir],
        capture_output=True, text=True,
    )
    hits = [
        os.path.relpath(p, app_dir) for p in result.stdout.splitlines() if p.strip()
    ]
    allowed = {'marketplace_match.py'}
    unexpected = [h for h in hits if h not in allowed]
    assert not unexpected, (
        f"product_generic_standins is referenced outside the allowed set: {unexpected} "
        "— if this is the Pass 1.5 matcher change, update `allowed` above; if it's a "
        "stock-deduction path, STOP (see the migration's invariant note)."
    )
