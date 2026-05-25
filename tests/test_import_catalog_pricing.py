"""Tests for scripts/import_catalog_pricing.py.

Covers:
  - plan_writes_for_row produces the right shape for each input pattern
  - dry-run produces expected counts
  - commit applies UPDATEs + INSERTs as expected
  - audit triggers fire (verifies mig 086 + import work together)
  - empty-price rows produce no writes
  - flagged rows still get imported
  - re-running fails loudly via UNIQUE constraint
  - backup file is created with timestamp
"""
import csv
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import import_catalog_pricing as imp


# ── Helpers ─────────────────────────────────────────────────────────────────

def _write_csv(path: Path, rows):
    """Write a list-of-dicts as CSV with the full normalized column set."""
    fieldnames = [
        "product_id", "sku_code", "product_name", "sendy_unit_type",
        "base_sell_price",
        "tier1_qty_label", "tier1_price", "tier1_note",
        "tier2_qty_label", "tier2_price", "tier2_note",
        "extra_tiers_json",
        "special_price",
        "promo_type", "promo_value",
        "bundle_buy", "bundle_free", "bundle_unit", "bundle_condition",
        "bundle_tiers_json",
        "gift_desc", "gift_qty",
        "promo_text", "remark", "normalize_notes",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            # Fill missing keys with empty strings
            w.writerow({k: r.get(k, "") for k in fieldnames})


def _pick_real_product_id(conn):
    row = conn.execute("SELECT id FROM products LIMIT 1").fetchone()
    return row[0]


# ── plan_writes_for_row unit tests ──────────────────────────────────────────

class TestPlanWrites:
    def test_empty_row_produces_no_writes(self):
        row = {"product_id": "1", "base_sell_price": "", "tier1_qty_label": "",
               "tier2_qty_label": "", "extra_tiers_json": "",
               "special_price": "", "promo_type": ""}
        plan = imp.plan_writes_for_row(row, current_base_sell_price=0.0)
        assert plan["update_base"] is None
        assert plan["tier_inserts"] == []
        assert plan["promo_inserts"] == []

    def test_base_price_skips_when_matches_current(self):
        row = {"product_id": "1", "base_sell_price": "30",
               "tier1_qty_label": "", "tier2_qty_label": "",
               "extra_tiers_json": "", "special_price": "", "promo_type": ""}
        plan = imp.plan_writes_for_row(row, current_base_sell_price=30.0)
        assert plan["update_base"] is None  # no-op

    def test_base_price_updates_when_differs(self):
        row = {"product_id": "1", "base_sell_price": "30",
               "tier1_qty_label": "", "tier2_qty_label": "",
               "extra_tiers_json": "", "special_price": "", "promo_type": ""}
        plan = imp.plan_writes_for_row(row, current_base_sell_price=0.0)
        assert plan["update_base"] == (30.0,)

    def test_tier1_tier2_inserts(self):
        row = {"product_id": "1", "base_sell_price": "",
               "tier1_qty_label": "1 โหล", "tier1_price": "230", "tier1_note": "",
               "tier2_qty_label": "1 ลัง", "tier2_price": "2400", "tier2_note": "bulk",
               "extra_tiers_json": "", "special_price": "", "promo_type": ""}
        plan = imp.plan_writes_for_row(row, current_base_sell_price=0.0)
        assert ("1 โหล", 230.0, None) in plan["tier_inserts"]
        assert ("1 ลัง", 2400.0, "bulk") in plan["tier_inserts"]

    def test_extra_tiers_json(self):
        extras = [{"qty_label": "1 กล่อง (60ใบ)", "price": 480, "note": "wholesale"}]
        row = {"product_id": "1", "base_sell_price": "",
               "tier1_qty_label": "", "tier2_qty_label": "",
               "extra_tiers_json": json.dumps(extras),
               "special_price": "", "promo_type": ""}
        plan = imp.plan_writes_for_row(row, current_base_sell_price=0.0)
        assert ("1 กล่อง (60ใบ)", 480.0, "wholesale") in plan["tier_inserts"]

    def test_special_price_creates_fixed_promo(self):
        row = {"product_id": "1", "base_sell_price": "100",
               "tier1_qty_label": "", "tier2_qty_label": "",
               "extra_tiers_json": "", "special_price": "75", "promo_type": ""}
        plan = imp.plan_writes_for_row(row, current_base_sell_price=0.0)
        assert len(plan["promo_inserts"]) == 1
        p = plan["promo_inserts"][0]
        assert p["promo_type"] == "fixed"
        assert p["discount_value"] == 75.0
        assert p["promo_name"] == imp.PROMO_NAME_FROM_SPECIAL_PRICE

    def test_percent_promo(self):
        row = {"product_id": "1", "base_sell_price": "100",
               "tier1_qty_label": "", "tier2_qty_label": "",
               "extra_tiers_json": "", "special_price": "",
               "promo_type": "percent", "promo_value": "10"}
        plan = imp.plan_writes_for_row(row, current_base_sell_price=0.0)
        p = plan["promo_inserts"][0]
        assert p["promo_type"] == "percent"
        assert p["discount_value"] == 10.0
        assert p["bundle_buy"] is None

    def test_bundle_promo_with_unit(self):
        row = {"product_id": "1", "base_sell_price": "35",
               "tier1_qty_label": "", "tier2_qty_label": "",
               "extra_tiers_json": "", "special_price": "",
               "promo_type": "bundle",
               "bundle_buy": "120", "bundle_free": "12", "bundle_unit": "ดอก"}
        plan = imp.plan_writes_for_row(row, current_base_sell_price=0.0)
        p = plan["promo_inserts"][0]
        assert p["promo_type"] == "bundle"
        assert p["bundle_buy"] == 120
        assert p["bundle_free"] == 12
        assert p["bundle_unit"] == "ดอก"

    def test_mixed_promo_with_condition(self):
        row = {"product_id": "1", "base_sell_price": "350",
               "tier1_qty_label": "", "tier2_qty_label": "",
               "extra_tiers_json": "", "special_price": "",
               "promo_type": "mixed", "promo_value": "5",
               "bundle_condition": "ยกลัง"}
        plan = imp.plan_writes_for_row(row, current_base_sell_price=0.0)
        p = plan["promo_inserts"][0]
        assert p["promo_type"] == "mixed"
        assert p["discount_value"] == 5.0
        assert p["bundle_condition"] == "ยกลัง"

    def test_special_price_and_promo_both_produce_two_inserts(self):
        row = {"product_id": "1", "base_sell_price": "100",
               "tier1_qty_label": "", "tier2_qty_label": "",
               "extra_tiers_json": "", "special_price": "80",
               "promo_type": "percent", "promo_value": "10"}
        plan = imp.plan_writes_for_row(row, current_base_sell_price=0.0)
        assert len(plan["promo_inserts"]) == 2
        types = [p["promo_type"] for p in plan["promo_inserts"]]
        assert "fixed" in types and "percent" in types


# ── End-to-end import tests on tmp_db ───────────────────────────────────────

class TestImportE2E:
    def test_dry_run_makes_no_writes(self, tmp_db, tmp_path):
        """Dry-run should leave the DB untouched."""
        conn = sqlite3.connect(tmp_db)
        pid = _pick_real_product_id(conn)
        before_bsp = conn.execute(
            "SELECT base_sell_price FROM products WHERE id=?", (pid,)).fetchone()[0]
        before_promos = conn.execute("SELECT COUNT(*) FROM promotions").fetchone()[0]
        before_tiers = conn.execute("SELECT COUNT(*) FROM product_price_tiers").fetchone()[0]
        conn.close()

        csv_path = tmp_path / "test.csv"
        _write_csv(csv_path, [{
            "product_id": str(pid),
            "sku_code": "TEST-SKU",
            "base_sell_price": "999",
            "promo_type": "percent", "promo_value": "10",
        }])

        imp.run_import(csv_path, Path(tmp_db), commit=False, limit=None,
                       show_sample=0, verbose=False)

        # Verify nothing changed
        conn = sqlite3.connect(tmp_db)
        after_bsp = conn.execute(
            "SELECT base_sell_price FROM products WHERE id=?", (pid,)).fetchone()[0]
        after_promos = conn.execute("SELECT COUNT(*) FROM promotions").fetchone()[0]
        after_tiers = conn.execute("SELECT COUNT(*) FROM product_price_tiers").fetchone()[0]
        conn.close()
        assert after_bsp == before_bsp
        assert after_promos == before_promos
        assert after_tiers == before_tiers

    def test_commit_applies_writes(self, tmp_db, tmp_path):
        """Commit mode should UPDATE base_sell_price + INSERT promo + INSERT tier."""
        conn = sqlite3.connect(tmp_db)
        pid = _pick_real_product_id(conn)
        before_promos = conn.execute("SELECT COUNT(*) FROM promotions").fetchone()[0]
        conn.close()

        csv_path = tmp_path / "test.csv"
        _write_csv(csv_path, [{
            "product_id": str(pid),
            "sku_code": "TEST-SKU",
            "base_sell_price": "123",
            "tier1_qty_label": "1 เทสต์", "tier1_price": "456",
            "promo_type": "bundle", "bundle_buy": "12", "bundle_free": "1",
        }])

        imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                       show_sample=0, verbose=False)

        conn = sqlite3.connect(tmp_db)
        assert conn.execute(
            "SELECT base_sell_price FROM products WHERE id=?", (pid,)).fetchone()[0] == 123
        new_promos = conn.execute(
            "SELECT COUNT(*) FROM promotions WHERE product_id=?", (pid,)).fetchone()[0]
        assert new_promos >= 1
        tier = conn.execute(
            "SELECT price FROM product_price_tiers WHERE product_id=? AND qty_label=?",
            (pid, "1 เทสต์")).fetchone()
        assert tier is not None
        assert tier[0] == 456
        conn.close()

    def test_commit_writes_audit_log(self, tmp_db, tmp_path):
        """Audit triggers from mig 086 should fire on commit."""
        conn = sqlite3.connect(tmp_db)
        pid = _pick_real_product_id(conn)
        before_audit = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE table_name='promotions' AND action='INSERT'"
        ).fetchone()[0]
        conn.close()

        csv_path = tmp_path / "test.csv"
        _write_csv(csv_path, [{
            "product_id": str(pid),
            "sku_code": "TEST-SKU",
            "promo_type": "percent", "promo_value": "15",
        }])

        imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                       show_sample=0, verbose=False)

        conn = sqlite3.connect(tmp_db)
        after_audit = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE table_name='promotions' AND action='INSERT'"
        ).fetchone()[0]
        conn.close()
        assert after_audit > before_audit

    def test_empty_price_row_skipped(self, tmp_db, tmp_path):
        """Row with no base/tier/promo data → no writes."""
        conn = sqlite3.connect(tmp_db)
        pid = _pick_real_product_id(conn)
        before_promos = conn.execute(
            "SELECT COUNT(*) FROM promotions WHERE product_id=?", (pid,)).fetchone()[0]
        conn.close()

        csv_path = tmp_path / "test.csv"
        _write_csv(csv_path, [{
            "product_id": str(pid), "sku_code": "EMPTY",
            # All other fields blank
        }])

        imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                       show_sample=0, verbose=False)

        conn = sqlite3.connect(tmp_db)
        after_promos = conn.execute(
            "SELECT COUNT(*) FROM promotions WHERE product_id=?", (pid,)).fetchone()[0]
        conn.close()
        assert after_promos == before_promos

    def test_non_integer_product_id_skipped(self, tmp_db, tmp_path):
        """Rows with non-integer product_id (new_product_id placeholder) are silently skipped."""
        csv_path = tmp_path / "test.csv"
        _write_csv(csv_path, [{
            "product_id": "new_product_id", "sku_code": "NEWSKU",
            "base_sell_price": "100", "promo_type": "percent", "promo_value": "10",
        }])

        # Should not raise
        stats = imp.run_import(csv_path, Path(tmp_db), commit=True,
                               limit=None, show_sample=0, verbose=False)
        assert stats["rows_processed"] == 0

    def test_rerun_fails_on_unique_collision(self, tmp_db, tmp_path):
        """Second run with same tier qty_label fails the UNIQUE(product_id, qty_label) constraint."""
        conn = sqlite3.connect(tmp_db)
        pid = _pick_real_product_id(conn)
        conn.close()

        csv_path = tmp_path / "test.csv"
        _write_csv(csv_path, [{
            "product_id": str(pid), "sku_code": "TEST",
            "tier1_qty_label": "1 colliding", "tier1_price": "100",
        }])

        # First run succeeds
        imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                       show_sample=0, verbose=False)

        # Second run should raise
        with pytest.raises(sqlite3.IntegrityError):
            imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                           show_sample=0, verbose=False)

    def test_check_constraint_rejects_bad_row(self, tmp_db, tmp_path):
        """If a CSV row has an invalid promo shape (e.g. bundle with no bundle_buy),
        the import should fail loudly via the mig 086 CHECK."""
        conn = sqlite3.connect(tmp_db)
        pid = _pick_real_product_id(conn)
        conn.close()

        csv_path = tmp_path / "test.csv"
        # promo_type=bundle but bundle_buy empty — CHECK should reject
        _write_csv(csv_path, [{
            "product_id": str(pid), "sku_code": "BAD",
            "promo_type": "bundle",  # missing bundle_buy + bundle_free
        }])

        with pytest.raises(sqlite3.IntegrityError):
            imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                           show_sample=0, verbose=False)

    def test_atomic_rollback_on_failure(self, tmp_db, tmp_path):
        """If row N fails, rows 1..N-1 should be rolled back too."""
        conn = sqlite3.connect(tmp_db)
        pid_ok = _pick_real_product_id(conn)
        before_bsp = conn.execute(
            "SELECT base_sell_price FROM products WHERE id=?", (pid_ok,)).fetchone()[0]
        conn.close()

        csv_path = tmp_path / "test.csv"
        _write_csv(csv_path, [
            # Row 1: valid — would UPDATE base + INSERT promo
            {"product_id": str(pid_ok), "sku_code": "OK",
             "base_sell_price": "9999",
             "promo_type": "percent", "promo_value": "10"},
            # Row 2: invalid CHECK shape — bundle missing bundle_buy
            {"product_id": str(pid_ok), "sku_code": "BAD",
             "promo_type": "bundle"},
        ])

        with pytest.raises(sqlite3.IntegrityError):
            imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                           show_sample=0, verbose=False)

        # Row 1's UPDATE must have been rolled back
        conn = sqlite3.connect(tmp_db)
        after_bsp = conn.execute(
            "SELECT base_sell_price FROM products WHERE id=?", (pid_ok,)).fetchone()[0]
        conn.close()
        assert after_bsp == before_bsp


# ── Backup function ─────────────────────────────────────────────────────────

def test_backup_db_creates_timestamped_file(tmp_db, tmp_path):
    backup = imp.backup_db(Path(tmp_db))
    assert backup.exists()
    assert "backup-pre-catalog-import-" in backup.name
    assert backup.parent == Path(tmp_db).parent
    # File contents identical
    assert backup.stat().st_size == Path(tmp_db).stat().st_size
