"""Smoke test for normalize_products_round1.py.

Verifies the script runs end-to-end on a temp DB and produces a CSV with
the expected columns + a sensible row count. Does NOT validate the full
parsing logic — that's covered by parse_sku_names tests + the audit
cross-reference in the larger normalize pass itself.
"""
import csv
import os
import subprocess
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT = os.path.join(REPO, "scripts", "normalize_products_round1.py")


EXPECTED_COLUMNS = {
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
}


def test_script_runs_and_produces_csv(tmp_db, tmp_path):
    out_csv = tmp_path / "round1.csv"
    env = os.environ.copy()
    env["NORMALIZE_DB_PATH"] = tmp_db
    result = subprocess.run(
        [sys.executable, SCRIPT, "--output", str(out_csv)],
        capture_output=True, text=True, env=env, cwd=REPO,
    )
    assert result.returncode == 0, (
        f"script exited {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert out_csv.exists(), "CSV not written"


def test_csv_has_expected_columns(tmp_db, tmp_path):
    out_csv = tmp_path / "round1.csv"
    env = os.environ.copy()
    env["NORMALIZE_DB_PATH"] = tmp_db
    subprocess.run(
        [sys.executable, SCRIPT, "--output", str(out_csv)],
        capture_output=True, text=True, env=env, cwd=REPO, check=True,
    )
    with open(out_csv, encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        cols = set(r.fieldnames or [])
        first = next(r, None)

    missing = EXPECTED_COLUMNS - cols
    extra = cols - EXPECTED_COLUMNS
    assert not missing, f"missing columns: {missing}"
    assert not extra, f"unexpected columns: {extra}"
    assert first is not None, "CSV has no data rows"


def test_csv_row_count_matches_products(tmp_db, tmp_path):
    import sqlite3
    out_csv = tmp_path / "round1.csv"
    env = os.environ.copy()
    env["NORMALIZE_DB_PATH"] = tmp_db
    subprocess.run(
        [sys.executable, SCRIPT, "--output", str(out_csv)],
        capture_output=True, text=True, env=env, cwd=REPO, check=True,
    )
    expected = sqlite3.connect(tmp_db).execute(
        "SELECT COUNT(*) FROM products"
    ).fetchone()[0]
    with open(out_csv, encoding="utf-8-sig", newline="") as f:
        actual = sum(1 for _ in csv.DictReader(f))
    assert actual == expected, f"expected {expected} rows, got {actual}"


def test_csv_flags_pack_variant_1_for_change(tmp_db, tmp_path):
    """Products with pack_variant=1 in DB should have proposed_pack_variant blank,
    needs_change=True (pack_variant=1 should be suppressed)."""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    # Find a product with pack_variant=1 (audit row 1 example)
    row = conn.execute(
        "SELECT id, pack_variant FROM products WHERE pack_variant = '1' LIMIT 1"
    ).fetchone()
    if row is None:
        pytest.skip("no products with pack_variant='1' in fixture DB — skip rule check")
    pid = row[0]
    conn.close()

    out_csv = tmp_path / "round1.csv"
    env = os.environ.copy()
    env["NORMALIZE_DB_PATH"] = tmp_db
    subprocess.run(
        [sys.executable, SCRIPT, "--output", str(out_csv)],
        capture_output=True, text=True, env=env, cwd=REPO, check=True,
    )
    with open(out_csv, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if int(r["product_id"]) == pid:
                assert r["pack_variant_new"] in ("", None), (
                    f"pack_variant=1 should be suppressed, got {r['pack_variant_new']!r}"
                )
                # If old was '1' and new is '', sku_code likely changed (no trailing -1)
                assert r["pack_variant_old"] == "1"
                return
    pytest.fail(f"product_id {pid} not in CSV output")
