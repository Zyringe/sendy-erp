"""Migration 078 — data-quality mapping + UC cleanup (PR-B scope, schema only).

Tests the schema-cleanup half of Put's decision sheet
(Operations/05_analysis-reports/mig078_pr_b3_decision_sheet_2026-05-22.csv).

NO historical recompute, NO sales retargeting, NO platform_skus changes.
"""
import os
import sqlite3

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_078 = os.path.join(REPO, "data", "migrations",
                      "078_data_quality_mapping_uc_cleanup.sql")
ROLLBACK_078 = os.path.join(REPO, "data", "migrations",
                           "078_data_quality_mapping_uc_cleanup.rollback.sql")


# Pids — kept in sync with the migration body
UT_CHANGES = {
    'แผง':   [142, 992, 1492, 1493, 1495, 1503, 1505, 1506, 1511, 2003],
    'แพ็ค':  [1357, 1369, 1370, 1371, 1372, 1373, 1374],
    'อัน':   [771],
}
UT_UNCHANGED = [128, 148, 149, 150, 162, 991, 994, 1142, 1143, 1999, 2000]

MAPPING_DELETES = [59, 150]                # legacy rows that conflict with unit-aware
MAPPING_UPDATES = {
    399:  'ตัว',  163:  'แผง',  60:   'แผง',  61:   'แผง',
    1680: 'ตัว',  1681: 'ตัว',  1728: 'ตัว',  1763: 'แผง',
    1766: 'ตัว',  1767: 'ตัว',  1739: 'แพ็ค',
    1489: 'แพ็ค', 1682: 'แพ็ค', 1488: 'แพ็ค', 852:  'แพ็ค', 1487: 'แพ็ค', 972:  'แพ็ค',
    1738: 'แผง',  1729: 'แผง',  846:  'แผง',  1557: 'แผง',  1509: 'แผง',
    476:  'แผง',  1017: 'แผง',  537:  'แผง',
}

UC_DELETES = [
    # Only identity UCs that become redundant after their ut change + phantom orphans.
    # Non-identity 1.0 UCs are KEPT for future relabel (see UC_KEPT below).
    (142, 'แผง'),
    (149, 'แผง'),
    (150, 'แผง'),
    (771, 'อัน'),
    (991, 'ตัว'),  (991, 'แผง'),                    # phantom orphans
    (1369, 'แพ็ค'),
    (1370, 'แพ็ค'),
    (1371, 'แพ็ค'),
    (1372, 'แพ็ค'),
    (1373, 'แพ็ค'),
    (1374, 'แพ็ค'),
    (1495, 'แผง'),
    (1503, 'แผง'),
    (1505, 'แผง'),
    (1506, 'แผง'),
    (1511, 'แผง'),
    (2003, 'แผง'),
]

# UCs that MUST survive the migration (relabel for future BSN imports
# that still send the legacy unit string)
UC_KEPT = [
    (142, 'ตัว'),
    (149, 'ตัว'),   # ratio 0.5, untouched
    (150, 'ตัว'),
    (162, 'ตัว'),   # ratio updated 0.5 → 1.0
    (992, 'ตัว'),
    (1142, 'ชุด'), (1142, 'แผง'),
    (1143, 'ชุด'), (1143, 'แผง'),
    (1357, 'กิโลกรัม'),
    (1369, 'กิโลกรัม'),
    (1370, 'กิโลกรัม'),
    (1371, 'กิโลกรัม'),
    (1372, 'กิโลกรัม'),
    (1373, 'กิโลกรัม'),
    (1374, 'กิโลกรัม'),
    (1492, 'ตัว'),
    (1493, 'ตัว'),
    (1495, 'ตัว'),
]


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


def _snapshot_uc(conn, pids):
    placeholders = ",".join("?" * len(pids))
    return {
        (r[0], r[1]): r[2]
        for r in conn.execute(
            f"SELECT product_id, bsn_unit, ratio FROM unit_conversions "
            f"WHERE product_id IN ({placeholders})", pids
        )
    }


def _snapshot_mapping(conn, ids):
    placeholders = ",".join("?" * len(ids))
    return {
        r[0]: (r[1], r[2])  # id → (bsn_unit, product_id)
        for r in conn.execute(
            f"SELECT id, bsn_unit, product_id FROM product_code_mapping "
            f"WHERE id IN ({placeholders})", ids
        )
    }


def _snapshot_products(conn, pids):
    placeholders = ",".join("?" * len(pids))
    return {
        r[0]: r[1]
        for r in conn.execute(
            f"SELECT id, unit_type FROM products WHERE id IN ({placeholders})", pids
        )
    }


# ── Counts ───────────────────────────────────────────────────────────────────

def test_pid_count_matches_csv():
    """29 pids total: 25 source + 4 retarget targets."""
    all_ut_change = sum(len(v) for v in UT_CHANGES.values())
    assert all_ut_change == 18, "ut-change pids: 10 แผง + 7 แพ็ค + 1 อัน = 18"
    assert all_ut_change + len(UT_UNCHANGED) == 29


# ── unit_type changes ────────────────────────────────────────────────────────

def test_mig_078_unit_type_changes(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply(conn, MIG_078)

    for new_ut, pids in UT_CHANGES.items():
        for pid in pids:
            row = conn.execute(
                "SELECT unit_type FROM products WHERE id = ?", (pid,)
            ).fetchone()
            assert row is not None, f"pid {pid} not found"
            assert row[0] == new_ut, f"pid {pid} unit_type={row[0]!r}, expected {new_ut!r}"


def test_mig_078_does_not_change_other_pids(tmp_db):
    """ut for UT_UNCHANGED pids stays the same."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")

    before = _snapshot_products(conn, UT_UNCHANGED)
    _apply(conn, MIG_078)
    after = _snapshot_products(conn, UT_UNCHANGED)
    assert before == after, f"unchanged pids drifted: {set(before.items()) ^ set(after.items())}"


# ── product_code_mapping changes ─────────────────────────────────────────────

def test_mig_078_deletes_legacy_mapping_rows(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply(conn, MIG_078)

    for mid in MAPPING_DELETES:
        row = conn.execute(
            "SELECT id FROM product_code_mapping WHERE id = ?", (mid,)
        ).fetchone()
        assert row is None, f"mapping row id={mid} should have been DELETEd"


def test_mig_078_updates_mapping_bsn_units(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply(conn, MIG_078)

    for mid, expected_bsn_unit in MAPPING_UPDATES.items():
        row = conn.execute(
            "SELECT bsn_unit FROM product_code_mapping WHERE id = ?", (mid,)
        ).fetchone()
        assert row is not None, f"mapping row id={mid} should exist after mig"
        assert row[0] == expected_bsn_unit, (
            f"mapping id={mid} bsn_unit={row[0]!r}, expected {expected_bsn_unit!r}"
        )


def test_mig_078_no_duplicate_unique_violations(tmp_db):
    """After mig, (bsn_code, bsn_unit) UNIQUE constraint must hold."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply(conn, MIG_078)

    dups = list(conn.execute("""
        SELECT bsn_code, bsn_unit, COUNT(*) n
        FROM product_code_mapping
        GROUP BY bsn_code, bsn_unit
        HAVING n > 1
    """))
    assert dups == [], f"duplicate (bsn_code, bsn_unit) rows: {dups}"


# ── unit_conversions changes ─────────────────────────────────────────────────

def test_mig_078_deletes_targeted_uc_rows(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply(conn, MIG_078)

    for pid, bsn_unit in UC_DELETES:
        row = conn.execute(
            "SELECT ratio FROM unit_conversions WHERE product_id = ? AND bsn_unit = ?",
            (pid, bsn_unit)
        ).fetchone()
        assert row is None, f"UC ({pid}, {bsn_unit!r}) should have been DELETEd"


def test_mig_078_pid_771_uc_ratio_updated_to_12(tmp_db):
    """pid 771 UC (โหล, 12.0) per ORIGINAL tracker decision (not Put's CSV)."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply(conn, MIG_078)

    row = conn.execute(
        "SELECT ratio FROM unit_conversions WHERE product_id = 771 AND bsn_unit = 'โหล'"
    ).fetchone()
    assert row is not None, "UC (771, โหล) should exist after mig"
    assert row[0] == 12.0, f"UC (771, โหล) ratio={row[0]}, expected 12.0"


def test_mig_078_pid_162_uc_ratio_updated_to_1(tmp_db):
    """pid 162 UC (ตัว) ratio 0.5 → 1.0 per Put's 'ratio=1.0 default' policy."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply(conn, MIG_078)

    row = conn.execute(
        "SELECT ratio FROM unit_conversions WHERE product_id = 162 AND bsn_unit = 'ตัว'"
    ).fetchone()
    assert row is not None, "UC (162, ตัว) should still exist (ratio updated, not deleted)"
    assert row[0] == 1.0, f"UC (162, ตัว) ratio={row[0]}, expected 1.0"


def test_mig_078_keeps_relabel_ucs_for_future_bsn_imports(tmp_db):
    """All UC_KEPT rows survive the migration. Future BSN imports that still
    send the old unit string (e.g. กิโลกรัม after ut→แพ็ค) will sync as 1:1
    relabel via these UCs — no manual review needed."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply(conn, MIG_078)

    missing = []
    for pid, bsn_unit in UC_KEPT:
        row = conn.execute(
            "SELECT ratio FROM unit_conversions WHERE product_id = ? AND bsn_unit = ?",
            (pid, bsn_unit)
        ).fetchone()
        if row is None:
            missing.append((pid, bsn_unit))
    assert missing == [], f"UCs that should have survived but didn't: {missing}"


# ── No-touch invariants ──────────────────────────────────────────────────────

def test_mig_078_does_not_touch_transactions(tmp_db):
    """Schema-only cleanup — historical transactions are NOT modified."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")

    before_count = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE note LIKE 'BSN%'"
    ).fetchone()[0]
    before_sum = conn.execute(
        "SELECT COALESCE(SUM(quantity_change), 0) FROM transactions WHERE note LIKE 'BSN%'"
    ).fetchone()[0]

    _apply(conn, MIG_078)

    after_count = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE note LIKE 'BSN%'"
    ).fetchone()[0]
    after_sum = conn.execute(
        "SELECT COALESCE(SUM(quantity_change), 0) FROM transactions WHERE note LIKE 'BSN%'"
    ).fetchone()[0]

    assert before_count == after_count
    assert before_sum == after_sum


def test_mig_078_does_not_touch_stock_levels(tmp_db):
    """stock_levels for affected pids must NOT change."""
    affected = sum(UT_CHANGES.values(), []) + UT_UNCHANGED
    placeholders = ",".join("?" * len(affected))
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")

    before = {r[0]: r[1] for r in conn.execute(
        f"SELECT product_id, quantity FROM stock_levels WHERE product_id IN ({placeholders})",
        affected
    )}
    _apply(conn, MIG_078)
    after = {r[0]: r[1] for r in conn.execute(
        f"SELECT product_id, quantity FROM stock_levels WHERE product_id IN ({placeholders})",
        affected
    )}
    assert before == after


def test_mig_078_does_not_touch_platform_skus(tmp_db):
    """platform_skus.stock for affected pids stays the same."""
    affected = sum(UT_CHANGES.values(), []) + UT_UNCHANGED
    placeholders = ",".join("?" * len(affected))
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")

    before = {r[0]: r[1] for r in conn.execute(
        f"SELECT id, stock FROM platform_skus WHERE internal_product_id IN ({placeholders})",
        affected
    )}
    _apply(conn, MIG_078)
    after = {r[0]: r[1] for r in conn.execute(
        f"SELECT id, stock FROM platform_skus WHERE internal_product_id IN ({placeholders})",
        affected
    )}
    assert before == after


def test_mig_078_does_not_touch_sales_transactions_product_id(tmp_db):
    """No sales retargets in this mig — sales_transactions.product_id stays."""
    affected = sum(UT_CHANGES.values(), []) + UT_UNCHANGED
    placeholders = ",".join("?" * len(affected))
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")

    before = {r[0]: r[1] for r in conn.execute(
        f"SELECT id, product_id FROM sales_transactions WHERE product_id IN ({placeholders})",
        affected
    )}
    _apply(conn, MIG_078)
    after = {r[0]: r[1] for r in conn.execute(
        f"SELECT id, product_id FROM sales_transactions WHERE product_id IN ({placeholders})",
        affected
    )}
    assert before == after


# ── Snapshot + rollback ──────────────────────────────────────────────────────

def test_mig_078_snapshot_tables_created(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply(conn, MIG_078)

    for tbl in ('migration_078_snapshot_products',
                'migration_078_snapshot_uc',
                'migration_078_snapshot_mapping'):
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (tbl,)
        ).fetchone()
        assert row is not None, f"snapshot table {tbl} missing"


def test_mig_078_idempotent_rerun(tmp_db):
    """Re-applying mig 078 must be a no-op (UPDATEs idempotent, DELETEs idempotent)."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply(conn, MIG_078)

    # Snapshot post-mig state
    post1_uc = _snapshot_uc(conn, sum(UT_CHANGES.values(), []) + UT_UNCHANGED)
    post1_map = _snapshot_mapping(conn, list(MAPPING_UPDATES.keys()))
    post1_prods = _snapshot_products(conn, sum(UT_CHANGES.values(), []))

    # Re-apply
    _apply(conn, MIG_078)

    post2_uc = _snapshot_uc(conn, sum(UT_CHANGES.values(), []) + UT_UNCHANGED)
    post2_map = _snapshot_mapping(conn, list(MAPPING_UPDATES.keys()))
    post2_prods = _snapshot_products(conn, sum(UT_CHANGES.values(), []))

    assert post1_uc == post2_uc
    assert post1_map == post2_map
    assert post1_prods == post2_prods


def test_mig_078_rollback_restores_unit_types(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")

    affected = sum(UT_CHANGES.values(), [])
    before = _snapshot_products(conn, affected)
    _apply(conn, MIG_078)
    _apply(conn, ROLLBACK_078)
    after = _snapshot_products(conn, affected)
    assert before == after


def test_mig_078_rollback_restores_mapping_rows(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")

    affected_ids = list(MAPPING_UPDATES.keys()) + MAPPING_DELETES
    before = _snapshot_mapping(conn, affected_ids)
    _apply(conn, MIG_078)
    _apply(conn, ROLLBACK_078)
    after = _snapshot_mapping(conn, affected_ids)
    assert before == after


def test_mig_078_rollback_restores_uc_rows(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")

    affected_pids = sorted({pid for pid, _ in UC_DELETES} | {771})
    before = _snapshot_uc(conn, affected_pids)
    _apply(conn, MIG_078)
    _apply(conn, ROLLBACK_078)
    after = _snapshot_uc(conn, affected_pids)
    assert before == after


def test_mig_078_rollback_drops_snapshot_tables(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")

    _apply(conn, MIG_078)
    _apply(conn, ROLLBACK_078)

    for tbl in ('migration_078_snapshot_products',
                'migration_078_snapshot_uc',
                'migration_078_snapshot_mapping'):
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (tbl,)
        ).fetchone()
        assert row is None, f"snapshot table {tbl} should be dropped after rollback"
