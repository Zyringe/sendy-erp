"""Smoke test for scripts/generate_sku_codes.py — catches re-introduction
of a duplicate `build_sku_code` that drifts from the canonical impl in
sku_code_utils. Per scrutinize Finding 2 on PR #82.

Three seeded rows exercise the slots that were missing from the old
local duplicate: subcat (slot 2), condition (slot 9), pack_variant=1
suppression (slot 10).
"""
import os
import sqlite3
import subprocess
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT = os.path.join(REPO, "scripts", "generate_sku_codes.py")


def _seed(conn, rows):
    """Insert minimal product rows for sku_code regen testing.
    Each row: (id, sku, name, brand_short, cat_short, subcat, model, size,
              color_code, packaging_th, packaging_short, condition, pack_variant)
    The legacy `sku` slot (r[1]) is no longer a products column (dropped in
    mig 097); kept in the tuple only so the existing seed data still lines up.
    """
    conn.execute("PRAGMA foreign_keys = OFF")
    for r in rows:
        brand_id = conn.execute(
            "SELECT id FROM brands WHERE short_code = ?", (r[3],)
        ).fetchone()
        cat_id = conn.execute(
            "SELECT id FROM categories WHERE short_code = ?", (r[4],)
        ).fetchone()
        conn.execute(
            """INSERT INTO products
               (id, product_name, unit_type, brand_id, category_id,
                sub_category_short_code, model, size, color_code,
                packaging_th, packaging_short, condition, pack_variant,
                sku_code, sku_code_locked, is_active)
               VALUES (?,?,'ตัว',?,?,?,?,?,?,?,?,?,?,NULL,0,1)""",
            (r[0], r[2],
             brand_id[0] if brand_id else None,
             cat_id[0] if cat_id else None,
             r[5], r[6], r[7], r[8], r[9], r[10], r[11], r[12])
        )
    conn.commit()


def _run(tmp_db, *args):
    env = os.environ.copy()
    env["NORMALIZE_DB_PATH"] = tmp_db
    return subprocess.run(
        [sys.executable, SCRIPT, *args],
        capture_output=True, text=True, env=env, cwd=REPO,
    )


def test_regen_includes_subcat_slot(tmp_db):
    """If someone re-introduces a local build_sku_code without subcat, this
    test fails immediately — caught the regression that prompted PR #82."""
    conn = sqlite3.connect(tmp_db)
    _seed(conn, [
        (999991, 999991, "test product subcat", "SD", "BLT", "MYM",
         "#999", "4in", "AC", "ตัว", "UN", None, None),
    ])
    conn.close()

    r = _run(tmp_db, "--regen", "--apply")
    assert r.returncode == 0, f"STDERR: {r.stderr}"

    conn = sqlite3.connect(tmp_db)
    sku_code = conn.execute(
        "SELECT sku_code FROM products WHERE id = 999991"
    ).fetchone()[0]
    conn.close()
    assert sku_code == "BLT-MYM-SD-#999-4in-AC-UN", (
        f"subcat slot stripped — local duplicate may have been re-introduced. "
        f"Got: {sku_code}"
    )


def test_regen_includes_condition_slot_mapped_to_code(tmp_db):
    """Condition slot 9: Thai value should map to 3-letter code (เก่า → OLD)
    via _condition_segment in sku_code_utils. If the local duplicate creeps
    back without this mapping, condition would be missing or wrong."""
    conn = sqlite3.connect(tmp_db)
    _seed(conn, [
        (999992, 999992, "test product condition", "SD", "BLT", "MYM",
         "#999", "5in", "AC", "ตัว", "UN", "เก่า", None),
    ])
    conn.close()

    r = _run(tmp_db, "--regen", "--apply")
    assert r.returncode == 0, f"STDERR: {r.stderr}"

    conn = sqlite3.connect(tmp_db)
    sku_code = conn.execute(
        "SELECT sku_code FROM products WHERE id = 999992"
    ).fetchone()[0]
    conn.close()
    assert sku_code == "BLT-MYM-SD-#999-5in-AC-UN-OLD", (
        f"condition slot missing or unmapped. Got: {sku_code}"
    )


def test_regen_suppresses_pack_variant_1(tmp_db):
    """pack_variant=1 should NOT appear in sku_code (rule: only ≥ 2 visible)."""
    conn = sqlite3.connect(tmp_db)
    _seed(conn, [
        (999993, 999993, "test pv1 suppress", "SD", "BLT", "MYM",
         "#999", "6in", "AC", "ตัว", "UN", None, "1"),
    ])
    conn.close()

    r = _run(tmp_db, "--regen", "--apply")
    assert r.returncode == 0

    conn = sqlite3.connect(tmp_db)
    sku_code = conn.execute(
        "SELECT sku_code FROM products WHERE id = 999993"
    ).fetchone()[0]
    conn.close()
    assert sku_code == "BLT-MYM-SD-#999-6in-AC-UN", (
        f"pack_variant=1 not suppressed. Got: {sku_code}"
    )


def test_regen_keeps_pack_variant_2_visible(tmp_db):
    conn = sqlite3.connect(tmp_db)
    _seed(conn, [
        (999994, 999994, "test pv2 visible", "SD", "BLT", "MYM",
         "#999", "7in", "AC", "ตัว", "UN", None, "2"),
    ])
    conn.close()

    r = _run(tmp_db, "--regen", "--apply")
    assert r.returncode == 0

    conn = sqlite3.connect(tmp_db)
    sku_code = conn.execute(
        "SELECT sku_code FROM products WHERE id = 999994"
    ).fetchone()[0]
    conn.close()
    assert sku_code == "BLT-MYM-SD-#999-7in-AC-UN-2"


def test_apply_writes_import_log(tmp_db):
    """Per scrutinize Finding 3: --apply should record a single import_log
    row so the bulk regen is findable without grepping audit_log."""
    conn = sqlite3.connect(tmp_db)
    _seed(conn, [
        (999995, 999995, "test import log", "SD", "BLT", "MYM",
         "#999", "8in", "AC", "ตัว", "UN", None, None),
    ])
    pre_count = conn.execute(
        "SELECT COUNT(*) FROM import_log WHERE filename LIKE 'generate_sku_codes:%'"
    ).fetchone()[0]
    conn.close()

    r = _run(tmp_db, "--regen", "--apply")
    assert r.returncode == 0

    conn = sqlite3.connect(tmp_db)
    post_count = conn.execute(
        "SELECT COUNT(*) FROM import_log WHERE filename LIKE 'generate_sku_codes:%'"
    ).fetchone()[0]
    conn.close()
    assert post_count == pre_count + 1, "missing import_log entry for --apply run"
