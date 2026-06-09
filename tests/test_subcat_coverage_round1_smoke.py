"""Smoke tests for subcat coverage round1 — generate + apply.

Mirrors test_normalize_round1_smoke + test_apply_normalize_round1_smoke
patterns. Verifies CSV emission, confidence tiers, dry-run safety, and
real --commit UPDATE path.
"""
import csv
import os
import subprocess
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
GEN = os.path.join(REPO, "scripts", "subcat_coverage_round1.py")
APPLY = os.path.join(REPO, "scripts", "apply_subcat_coverage.py")


EXPECTED_GEN_COLUMNS = {
    "product_id", "category_short", "current_subcat",
    "product_name",
    "proposed_subcat", "proposed_score", "proposed_confidence",
    "matched_sibling_pid", "matched_sibling_name",
    "alternate_subcat", "alternate_score", "alternate_sibling_name",
    "n_subcat_candidates_in_category",
    "approve",
}


def _env(tmp_db):
    e = os.environ.copy()
    e["NORMALIZE_DB_PATH"] = tmp_db
    return e


# ── generate script ─────────────────────────────────────────────────────────

def test_gen_runs_and_writes_csv(tmp_db, tmp_path):
    out = tmp_path / "subcat.csv"
    r = subprocess.run(
        [sys.executable, GEN, "--output", str(out), "--db", tmp_db],
        capture_output=True, text=True, env=_env(tmp_db), cwd=REPO,
    )
    assert r.returncode == 0, f"STDERR: {r.stderr}"
    assert out.exists()


def test_gen_csv_has_expected_columns(tmp_db, tmp_path):
    out = tmp_path / "subcat.csv"
    subprocess.run(
        [sys.executable, GEN, "--output", str(out), "--db", tmp_db],
        env=_env(tmp_db), cwd=REPO, check=True,
    )
    with open(out, encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        cols = set(r.fieldnames or [])
    missing = EXPECTED_GEN_COLUMNS - cols
    extra = cols - EXPECTED_GEN_COLUMNS
    assert not missing, f"missing: {missing}"
    assert not extra, f"unexpected: {extra}"


def test_gen_only_includes_missing_subcat_rows(tmp_db, tmp_path):
    """Every CSV row must have current_subcat empty."""
    out = tmp_path / "subcat.csv"
    subprocess.run(
        [sys.executable, GEN, "--output", str(out), "--db", tmp_db],
        env=_env(tmp_db), cwd=REPO, check=True,
    )
    with open(out, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            assert (row["current_subcat"] or "").strip() == "", (
                f"row pid={row['product_id']} has current_subcat='{row['current_subcat']}'"
            )


def test_gen_confidence_tier_in_expected_set(tmp_db, tmp_path):
    out = tmp_path / "subcat.csv"
    subprocess.run(
        [sys.executable, GEN, "--output", str(out), "--db", tmp_db],
        env=_env(tmp_db), cwd=REPO, check=True,
    )
    valid = {"high", "medium", "low", "unmatched", "unmatched_no_siblings"}
    with open(out, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            assert row["proposed_confidence"] in valid


# ── apply script ────────────────────────────────────────────────────────────

def _write_apply_csv(path, rows):
    cols = [
        "product_id", "category_short", "current_subcat",
        "product_name", "proposed_subcat", "proposed_score",
        "proposed_confidence", "matched_sibling_pid",
        "matched_sibling_name", "alternate_subcat", "alternate_score",
        "alternate_sibling_name", "n_subcat_candidates_in_category",
        "approve",
    ]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            full = {c: "" for c in cols}
            full.update(r)
            w.writerow(full)


def test_apply_commit_updates_subcat(tmp_db, tmp_path):
    conn = sqlite3.connect(tmp_db)
    target = conn.execute(
        "SELECT id FROM products WHERE sub_category_short_code IS NULL "
        "AND is_active = 1 AND sku_code_locked = 0 AND category_id IS NOT NULL "
        "LIMIT 1"
    ).fetchone()
    assert target is not None, "fixture should have ≥1 missing-subcat active product"
    pid = target[0]
    conn.close()

    csv_path = tmp_path / "approved.csv"
    _write_apply_csv(csv_path, [{
        "product_id": str(pid),
        "proposed_subcat": "TESTSC",
        "approve": "Y",
    }])

    r = subprocess.run(
        [sys.executable, APPLY, "--csv", str(csv_path), "--db", tmp_db, "--commit"],
        capture_output=True, text=True, env=_env(tmp_db), cwd=REPO,
    )
    assert r.returncode == 0, f"STDERR: {r.stderr}"

    conn = sqlite3.connect(tmp_db)
    new_subcat = conn.execute(
        "SELECT sub_category_short_code FROM products WHERE id = ?", (pid,)
    ).fetchone()[0]
    conn.close()
    assert new_subcat == "TESTSC"


def test_apply_dry_run_does_not_mutate(tmp_db, tmp_path):
    conn = sqlite3.connect(tmp_db)
    target = conn.execute(
        "SELECT id, sub_category_short_code FROM products "
        "WHERE category_id IS NOT NULL LIMIT 1"
    ).fetchone()
    pid, before = target[0], target[1]
    conn.close()

    csv_path = tmp_path / "approved.csv"
    _write_apply_csv(csv_path, [{
        "product_id": str(pid),
        "proposed_subcat": "DRYRUN",
        "approve": "Y",
    }])
    subprocess.run(
        [sys.executable, APPLY, "--csv", str(csv_path), "--db", tmp_db],
        env=_env(tmp_db), cwd=REPO, check=True,
    )

    conn = sqlite3.connect(tmp_db)
    after = conn.execute(
        "SELECT sub_category_short_code FROM products WHERE id = ?", (pid,)
    ).fetchone()[0]
    conn.close()
    assert after == before


def test_apply_skips_when_proposed_subcat_blank(tmp_db, tmp_path):
    """approve='Y' but proposed_subcat='' → row counted as no_proposal, no UPDATE."""
    conn = sqlite3.connect(tmp_db)
    target = conn.execute(
        "SELECT id, sub_category_short_code FROM products "
        "WHERE sub_category_short_code IS NULL AND category_id IS NOT NULL LIMIT 1"
    ).fetchone()
    pid, before = target[0], target[1]
    conn.close()

    csv_path = tmp_path / "approved.csv"
    _write_apply_csv(csv_path, [{
        "product_id": str(pid),
        "proposed_subcat": "",
        "approve": "Y",
    }])
    r = subprocess.run(
        [sys.executable, APPLY, "--csv", str(csv_path), "--db", tmp_db, "--commit"],
        capture_output=True, text=True, env=_env(tmp_db), cwd=REPO,
    )
    assert r.returncode == 0
    assert "no-proposal:    1" in r.stdout

    conn = sqlite3.connect(tmp_db)
    after = conn.execute(
        "SELECT sub_category_short_code FROM products WHERE id = ?", (pid,)
    ).fetchone()[0]
    conn.close()
    assert after == before


def test_apply_rejects_invalid_subcat_format(tmp_db, tmp_path):
    """Per naming rule: subcat = [A-Z0-9]{2,15}. Lowercase / dashes /
    whitespace / single-char must be rejected, NOT silently written."""
    conn = sqlite3.connect(tmp_db)
    target = conn.execute(
        "SELECT id, sub_category_short_code FROM products "
        "WHERE sub_category_short_code IS NULL AND category_id IS NOT NULL "
        "AND is_active = 1 AND sku_code_locked = 0 LIMIT 1"
    ).fetchone()
    pid, before = target[0], target[1]
    conn.close()

    csv_path = tmp_path / "approved.csv"
    _write_apply_csv(csv_path, [{
        "product_id": str(pid),
        "proposed_subcat": "bad-code",  # lowercase + dash
        "approve": "Y",
    }])
    r = subprocess.run(
        [sys.executable, APPLY, "--csv", str(csv_path), "--db", tmp_db, "--commit"],
        capture_output=True, text=True, env=_env(tmp_db), cwd=REPO,
    )
    assert "invalid-format: 1" in r.stdout
    assert "invalid subcat format" in r.stdout

    conn = sqlite3.connect(tmp_db)
    after = conn.execute(
        "SELECT sub_category_short_code FROM products WHERE id = ?", (pid,)
    ).fetchone()[0]
    conn.close()
    assert after == before, "invalid format must not write"


def test_apply_skips_locked(tmp_db, tmp_path):
    conn = sqlite3.connect(tmp_db)
    target = conn.execute(
        "SELECT id, sub_category_short_code FROM products WHERE category_id IS NOT NULL LIMIT 1"
    ).fetchone()
    pid, before = target[0], target[1]
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("UPDATE products SET sku_code_locked = 1 WHERE id = ?", (pid,))
    conn.commit()
    conn.close()

    csv_path = tmp_path / "approved.csv"
    _write_apply_csv(csv_path, [{
        "product_id": str(pid),
        "proposed_subcat": "SHOULDNOT",
        "approve": "Y",
    }])
    r = subprocess.run(
        [sys.executable, APPLY, "--csv", str(csv_path), "--db", tmp_db, "--commit"],
        capture_output=True, text=True, env=_env(tmp_db), cwd=REPO,
    )
    assert r.returncode == 0
    assert "skipped-locked: 1" in r.stdout

    conn = sqlite3.connect(tmp_db)
    after = conn.execute(
        "SELECT sub_category_short_code FROM products WHERE id = ?", (pid,)
    ).fetchone()[0]
    conn.close()
    assert after == before
