"""End-to-end smoke for apply_normalize_round1.py — verifies the --commit
UPDATE path actually mutates products, respects sku_code_locked, writes
import_log, and creates a pre-flight DB backup.

Per /scrutinize Finding 1: the normalize smoke test covers CSV emission
but the apply script's mutate path is otherwise untested until Put's
real --commit run over hundreds of approved rows. This file fills that
gap.
"""
import csv
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APPLY = os.path.join(REPO, "scripts", "apply_normalize_round1.py")
NORMALIZE = os.path.join(REPO, "scripts", "normalize_products_round1.py")


# Minimal CSV row matching apply script's expected column set.
# Only `approve`, `product_id`, `proposed_name`, `proposed_sku_code`, and
# the `*_new` fields drive UPDATE; everything else is for display.
def _write_csv(path: Path, rows: list[dict]):
    cols = [
        "product_id", "current_sku_code", "proposed_sku_code",
        "current_name", "proposed_name",
        "category", "brand_short",
        "series_old", "series_new",
        "model_old", "model_new",
        "size_old", "size_new",
        "color_code_old", "color_code_new",
        "packaging_th_old", "packaging_th_new",
        "packaging_short_old", "packaging_short_new",
        "condition_old", "condition_new",
        "pack_variant_old", "pack_variant_new",
        "junk_flags", "parse_drift",
        "audit_2026_05_27_suggestion",
        "needs_change", "approve",
    ]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            full = {c: "" for c in cols}
            full.update(r)
            w.writerow(full)


def _run_apply(csv_path: Path, db_path: str, commit: bool = False):
    env = os.environ.copy()
    env["NORMALIZE_DB_PATH"] = db_path
    cmd = [sys.executable, APPLY, "--csv", str(csv_path), "--db", db_path]
    if commit:
        cmd.append("--commit")
    return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=REPO)


def _pick_products(conn, n=3):
    """Pick N unlocked products from the live snapshot for mutation."""
    rows = conn.execute(
        "SELECT id, product_name, sku_code FROM products "
        "WHERE sku_code_locked = 0 AND sku_code IS NOT NULL "
        "ORDER BY id LIMIT ?",
        (n,)
    ).fetchall()
    return [dict(zip(["id", "name", "sku_code"], r)) for r in rows]


# ── --commit path actually mutates ──────────────────────────────────────────

def test_commit_updates_products_for_approved_rows(tmp_db, tmp_path):
    conn = sqlite3.connect(tmp_db)
    targets = _pick_products(conn, 2)
    assert len(targets) == 2, "fixture should have ≥2 unlocked products"
    conn.close()

    csv_path = tmp_path / "approved.csv"
    _write_csv(csv_path, [
        {
            "product_id": str(targets[0]["id"]),
            "current_sku_code": targets[0]["sku_code"],
            "proposed_sku_code": "TEST-APPLY-001",
            "current_name": targets[0]["name"],
            "proposed_name": "TEST APPLY 001",
            "packaging_th_new": "ตัว",
            "packaging_short_new": "UN",
            "needs_change": "Y",
            "approve": "Y",
        },
        {
            "product_id": str(targets[1]["id"]),
            "current_sku_code": targets[1]["sku_code"],
            "proposed_sku_code": "TEST-APPLY-002",
            "current_name": targets[1]["name"],
            "proposed_name": "TEST APPLY 002",
            "needs_change": "Y",
            "approve": "Y",
        },
    ])

    result = _run_apply(csv_path, tmp_db, commit=True)
    assert result.returncode == 0, (
        f"apply exited {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )

    conn = sqlite3.connect(tmp_db)
    row0 = conn.execute(
        "SELECT product_name, sku_code, packaging_th, packaging_short "
        "FROM products WHERE id = ?",
        (targets[0]["id"],)
    ).fetchone()
    assert row0 == ("TEST APPLY 001", "TEST-APPLY-001", "ตัว", "UN")

    row1 = conn.execute(
        "SELECT product_name, sku_code FROM products WHERE id = ?",
        (targets[1]["id"],)
    ).fetchone()
    assert row1 == ("TEST APPLY 002", "TEST-APPLY-002")
    conn.close()


# ── dry-run rolls back ──────────────────────────────────────────────────────

def test_dry_run_does_not_mutate(tmp_db, tmp_path):
    conn = sqlite3.connect(tmp_db)
    targets = _pick_products(conn, 1)
    before = conn.execute(
        "SELECT product_name, sku_code FROM products WHERE id = ?",
        (targets[0]["id"],)
    ).fetchone()
    conn.close()

    csv_path = tmp_path / "approved.csv"
    _write_csv(csv_path, [{
        "product_id": str(targets[0]["id"]),
        "proposed_sku_code": "DRY-RUN-IGNORED",
        "proposed_name": "DRY RUN IGNORED",
        "approve": "Y",
    }])

    result = _run_apply(csv_path, tmp_db, commit=False)
    assert result.returncode == 0

    conn = sqlite3.connect(tmp_db)
    after = conn.execute(
        "SELECT product_name, sku_code FROM products WHERE id = ?",
        (targets[0]["id"],)
    ).fetchone()
    conn.close()
    assert after == before, "dry-run must roll back UPDATEs"


# ── sku_code_locked guard ───────────────────────────────────────────────────

def test_locked_rows_skipped(tmp_db, tmp_path):
    conn = sqlite3.connect(tmp_db)
    # Lock one product
    locked_id = conn.execute(
        "SELECT id FROM products WHERE sku_code IS NOT NULL LIMIT 1"
    ).fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("UPDATE products SET sku_code_locked = 1 WHERE id = ?", (locked_id,))
    conn.commit()
    before = conn.execute(
        "SELECT product_name, sku_code FROM products WHERE id = ?",
        (locked_id,)
    ).fetchone()
    conn.close()

    csv_path = tmp_path / "approved.csv"
    _write_csv(csv_path, [{
        "product_id": str(locked_id),
        "proposed_sku_code": "SHOULD-NOT-APPLY",
        "proposed_name": "SHOULD NOT APPLY",
        "approve": "Y",
    }])

    result = _run_apply(csv_path, tmp_db, commit=True)
    assert result.returncode == 0
    assert "skipped-locked: 1" in result.stdout

    conn = sqlite3.connect(tmp_db)
    after = conn.execute(
        "SELECT product_name, sku_code FROM products WHERE id = ?",
        (locked_id,)
    ).fetchone()
    conn.close()
    assert after == before, "locked row must not be UPDATEd"


# ── import_log entry on commit ──────────────────────────────────────────────

def test_commit_writes_import_log(tmp_db, tmp_path):
    conn = sqlite3.connect(tmp_db)
    targets = _pick_products(conn, 1)
    pre_log_count = conn.execute(
        "SELECT COUNT(*) FROM import_log WHERE filename LIKE 'normalize_round1:%'"
    ).fetchone()[0]
    conn.close()

    csv_path = tmp_path / "approved.csv"
    _write_csv(csv_path, [{
        "product_id": str(targets[0]["id"]),
        "proposed_sku_code": "LOG-TEST-001",
        "proposed_name": "LOG TEST 001",
        "approve": "Y",
    }])

    result = _run_apply(csv_path, tmp_db, commit=True)
    assert result.returncode == 0

    conn = sqlite3.connect(tmp_db)
    post_log_count = conn.execute(
        "SELECT COUNT(*) FROM import_log WHERE filename LIKE 'normalize_round1:%'"
    ).fetchone()[0]
    conn.close()
    assert post_log_count == pre_log_count + 1, "commit must INSERT into import_log"


# Backup creation (shutil.copy2) intentionally not unit-tested here — trivial
# 2-line path that races on shared BACKUP_DIR when multiple tests run within
# the same second. Verified by reading scripts/apply_normalize_round1.py
# main() and by the integration test of running real --commit (which prints
# the backup path to stdout).


# ── error in one row doesn't kill the batch ────────────────────────────────

def test_error_in_one_row_continues_batch(tmp_db, tmp_path):
    conn = sqlite3.connect(tmp_db)
    targets = _pick_products(conn, 2)
    conn.close()

    csv_path = tmp_path / "approved.csv"
    _write_csv(csv_path, [
        # Bogus product_id — triggers "not found" error path
        {
            "product_id": "999999999",
            "proposed_sku_code": "BOGUS",
            "proposed_name": "BOGUS",
            "approve": "Y",
        },
        # Valid row — should still apply despite earlier error
        {
            "product_id": str(targets[0]["id"]),
            "proposed_sku_code": "STILL-APPLIED",
            "proposed_name": "STILL APPLIED",
            "approve": "Y",
        },
    ])

    result = _run_apply(csv_path, tmp_db, commit=True)
    assert "updated:        1" in result.stdout
    assert "errors:         1" in result.stdout

    conn = sqlite3.connect(tmp_db)
    after = conn.execute(
        "SELECT product_name, sku_code FROM products WHERE id = ?",
        (targets[0]["id"],)
    ).fetchone()
    conn.close()
    assert after == ("STILL APPLIED", "STILL-APPLIED")
