"""Tests for scripts/import_catalog_pricing.py (idempotent catalog importer,
phase-2-finish-plan.md PR B / 2c).

Covers:
  - plan_writes_for_row / _tiers_from_row / _promo_intents_from_row shapes
  - dry-run and commit produce the identical plan (same planner, B5)
  - idempotency: re-running the same file (same or newer --batch-date)
    reports all-zero counters and changes nothing
  - tier reconciliation matrix (insert / update-in-place / leave-alone)
  - promo reconciliation: preserve-if-identical, close+insert if changed
  - B3: a `mixed` occupant is refused on partial replacement, never
    silently half-closed
  - B4: two price-slot promos in one row aborts the WHOLE run
  - B5: duplicate product_id in the file aborts the whole run; concurrency
  - B1: a batch date that would backdate a close is refused
  - B7: a row with no promo columns leaves existing promos untouched
  - audit triggers still fire; CHECK-constraint rows still fail loudly
"""
import csv
import json
import sqlite3
import sys
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
            w.writerow({k: r.get(k, "") for k in fieldnames})


def _pick_real_product_id(conn):
    row = conn.execute("SELECT id FROM products LIMIT 1").fetchone()
    return row[0]


def _pick_two_product_ids(conn):
    rows = conn.execute("SELECT id FROM products LIMIT 2").fetchall()
    return rows[0][0], rows[1][0]


def _clean_product(conn, pid):
    """Force fixture state: this product carries no tiers/promos to start."""
    conn.execute("DELETE FROM product_price_tiers WHERE product_id=?", (pid,))
    conn.execute("DELETE FROM promotions WHERE product_id=?", (pid,))


def _insert_promo(conn, pid, promo_type, discount_value=None, bundle_buy=None,
                  bundle_free=None, gift_desc=None, gift_qty=None,
                  date_start="2026-06-01", source="catalog-import"):
    conn.execute(
        "INSERT INTO promotions (product_id, promo_name, promo_type, "
        "discount_value, bundle_buy, bundle_free, gift_desc, gift_qty, "
        "date_start, is_active, source) VALUES (?, 'existing', ?, ?, ?, ?, ?, ?, ?, 1, ?)",
        (pid, promo_type, discount_value, bundle_buy, bundle_free, gift_desc,
         gift_qty, date_start, source))


BATCH = "2026-06-01"


# ── plan_writes_for_row / helper unit tests ─────────────────────────────────

class TestPlanWrites:
    def test_empty_row_produces_no_writes(self):
        row = {"product_id": "1", "base_sell_price": "", "tier1_qty_label": "",
               "tier2_qty_label": "", "extra_tiers_json": "",
               "special_price": "", "promo_type": ""}
        plan = imp.plan_writes_for_row(row, current_base_sell_price=0.0, batch_date=BATCH)
        assert plan["update_base"] is None
        assert plan["tier_inserts"] == []
        assert plan["promo_intents"] == []

    def test_base_price_skips_when_matches_current(self):
        row = {"product_id": "1", "base_sell_price": "30",
               "tier1_qty_label": "", "tier2_qty_label": "",
               "extra_tiers_json": "", "special_price": "", "promo_type": ""}
        plan = imp.plan_writes_for_row(row, current_base_sell_price=30.0, batch_date=BATCH)
        assert plan["update_base"] is None

    def test_base_price_updates_when_differs(self):
        row = {"product_id": "1", "base_sell_price": "30",
               "tier1_qty_label": "", "tier2_qty_label": "",
               "extra_tiers_json": "", "special_price": "", "promo_type": ""}
        plan = imp.plan_writes_for_row(row, current_base_sell_price=0.0, batch_date=BATCH)
        assert plan["update_base"] == (30.0,)

    def test_tier1_tier2_inserts(self):
        row = {"product_id": "1", "base_sell_price": "",
               "tier1_qty_label": "1 โหล", "tier1_price": "230", "tier1_note": "",
               "tier2_qty_label": "1 ลัง", "tier2_price": "2400", "tier2_note": "bulk",
               "extra_tiers_json": "", "special_price": "", "promo_type": ""}
        plan = imp.plan_writes_for_row(row, current_base_sell_price=0.0, batch_date=BATCH)
        assert ("1 โหล", 230.0, None) in plan["tier_inserts"]
        assert ("1 ลัง", 2400.0, "bulk") in plan["tier_inserts"]

    def test_extra_tiers_json(self):
        extras = [{"qty_label": "1 กล่อง (60ใบ)", "price": 480, "note": "wholesale"}]
        row = {"product_id": "1", "base_sell_price": "",
               "tier1_qty_label": "", "tier2_qty_label": "",
               "extra_tiers_json": json.dumps(extras),
               "special_price": "", "promo_type": ""}
        plan = imp.plan_writes_for_row(row, current_base_sell_price=0.0, batch_date=BATCH)
        assert ("1 กล่อง (60ใบ)", 480.0, "wholesale") in plan["tier_inserts"]

    def test_special_price_creates_fixed_promo_intent(self):
        row = {"product_id": "1", "base_sell_price": "100",
               "tier1_qty_label": "", "tier2_qty_label": "",
               "extra_tiers_json": "", "special_price": "75", "promo_type": ""}
        plan = imp.plan_writes_for_row(row, current_base_sell_price=0.0, batch_date=BATCH)
        assert len(plan["promo_intents"]) == 1
        p = plan["promo_intents"][0]
        assert p["promo_type"] == "fixed"
        assert p["discount_value"] == 75.0
        assert p["promo_name"] == f"catalog {BATCH} (special_price)"

    def test_percent_promo(self):
        row = {"product_id": "1", "base_sell_price": "100",
               "tier1_qty_label": "", "tier2_qty_label": "",
               "extra_tiers_json": "", "special_price": "",
               "promo_type": "percent", "promo_value": "10"}
        plan = imp.plan_writes_for_row(row, current_base_sell_price=0.0, batch_date=BATCH)
        p = plan["promo_intents"][0]
        assert p["promo_type"] == "percent"
        assert p["discount_value"] == 10.0
        assert p["bundle_buy"] is None

    def test_bundle_promo_with_unit(self):
        row = {"product_id": "1", "base_sell_price": "35",
               "tier1_qty_label": "", "tier2_qty_label": "",
               "extra_tiers_json": "", "special_price": "",
               "promo_type": "bundle",
               "bundle_buy": "120", "bundle_free": "12", "bundle_unit": "ดอก"}
        plan = imp.plan_writes_for_row(row, current_base_sell_price=0.0, batch_date=BATCH)
        p = plan["promo_intents"][0]
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
        plan = imp.plan_writes_for_row(row, current_base_sell_price=0.0, batch_date=BATCH)
        p = plan["promo_intents"][0]
        assert p["promo_type"] == "mixed"
        assert p["discount_value"] == 5.0
        assert p["bundle_condition"] == "ยกลัง"

    def test_special_price_and_percent_both_produce_two_price_slot_intents(self):
        """The raw per-row planner still produces two intents (validation
        that rejects this shape lives in _validate_file_shape, not here)."""
        row = {"product_id": "1", "base_sell_price": "100",
               "tier1_qty_label": "", "tier2_qty_label": "",
               "extra_tiers_json": "", "special_price": "80",
               "promo_type": "percent", "promo_value": "10"}
        plan = imp.plan_writes_for_row(row, current_base_sell_price=0.0, batch_date=BATCH)
        assert len(plan["promo_intents"]) == 2
        types = [p["promo_type"] for p in plan["promo_intents"]]
        assert "fixed" in types and "percent" in types

    def test_offer_identity_ignores_promo_name(self):
        a = {"promo_type": "percent", "discount_value": 10.0, "bundle_buy": None,
             "bundle_free": None, "bundle_unit": None, "bundle_condition": None,
             "bundle_tiers_json": None, "gift_desc": None, "gift_qty": None,
             "promo_name": f"catalog {BATCH} (promo)"}
        b = dict(a, promo_name="catalog 2026-07-01 (promo)")
        assert imp._offer_identity(a) == imp._offer_identity(b)


# ── End-to-end import tests on tmp_db ───────────────────────────────────────

class TestImportE2E:
    def test_dry_run_makes_no_writes(self, tmp_db, tmp_path):
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        pid = _pick_real_product_id(conn)
        _clean_product(conn, pid)  # force fixture state: no pre-existing occupant to conflict
        conn.commit()
        before_bsp = conn.execute(
            "SELECT base_sell_price FROM products WHERE id=?", (pid,)).fetchone()[0]
        before_promos = conn.execute("SELECT COUNT(*) FROM promotions").fetchone()[0]
        before_tiers = conn.execute("SELECT COUNT(*) FROM product_price_tiers").fetchone()[0]
        conn.close()

        csv_path = tmp_path / "test.csv"
        _write_csv(csv_path, [{
            "product_id": str(pid), "sku_code": "TEST-SKU",
            "base_sell_price": "999",
            "promo_type": "percent", "promo_value": "10",
        }])

        imp.run_import(csv_path, Path(tmp_db), commit=False, limit=None,
                       show_sample=0, verbose=False, batch_date=BATCH)

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
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        pid = _pick_real_product_id(conn)
        _clean_product(conn, pid)
        conn.commit()
        conn.close()

        csv_path = tmp_path / "test.csv"
        _write_csv(csv_path, [{
            "product_id": str(pid), "sku_code": "TEST-SKU",
            "base_sell_price": "123",
            "tier1_qty_label": "1 เทสต์", "tier1_price": "456",
            "promo_type": "bundle", "bundle_buy": "12", "bundle_free": "1",
        }])

        stats = imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                               show_sample=0, verbose=False, batch_date=BATCH)
        assert stats["base_updated"] == 1
        assert stats["tiers_inserted"] == 1
        assert stats["promos_inserted"] == 1

        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        assert conn.execute(
            "SELECT base_sell_price FROM products WHERE id=?", (pid,)).fetchone()[0] == 123
        new_promos = conn.execute(
            "SELECT COUNT(*) FROM promotions WHERE product_id=?", (pid,)).fetchone()[0]
        assert new_promos == 1
        promo = conn.execute(
            "SELECT date_start, date_end, source FROM promotions WHERE product_id=?",
            (pid,)).fetchone()
        assert promo["date_start"] == BATCH
        assert promo["date_end"] is None
        assert promo["source"] == "catalog-import"
        tier = conn.execute(
            "SELECT price FROM product_price_tiers WHERE product_id=? AND qty_label=?",
            (pid, "1 เทสต์")).fetchone()
        assert tier is not None
        assert tier[0] == 456
        conn.close()

    def test_commit_writes_audit_log(self, tmp_db, tmp_path):
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        pid = _pick_real_product_id(conn)
        _clean_product(conn, pid)
        conn.commit()
        before_audit = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE table_name='promotions' AND action='INSERT'"
        ).fetchone()[0]
        conn.close()

        csv_path = tmp_path / "test.csv"
        _write_csv(csv_path, [{
            "product_id": str(pid), "sku_code": "TEST-SKU",
            "promo_type": "percent", "promo_value": "15",
        }])

        imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                       show_sample=0, verbose=False, batch_date=BATCH)

        conn = sqlite3.connect(tmp_db)
        after_audit = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE table_name='promotions' AND action='INSERT'"
        ).fetchone()[0]
        conn.close()
        assert after_audit > before_audit

    def test_empty_price_row_skipped(self, tmp_db, tmp_path):
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        pid = _pick_real_product_id(conn)
        _clean_product(conn, pid)
        conn.commit()
        before_promos = conn.execute(
            "SELECT COUNT(*) FROM promotions WHERE product_id=?", (pid,)).fetchone()[0]
        conn.close()

        csv_path = tmp_path / "test.csv"
        _write_csv(csv_path, [{"product_id": str(pid), "sku_code": "EMPTY"}])

        imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                       show_sample=0, verbose=False, batch_date=BATCH)

        conn = sqlite3.connect(tmp_db)
        after_promos = conn.execute(
            "SELECT COUNT(*) FROM promotions WHERE product_id=?", (pid,)).fetchone()[0]
        conn.close()
        assert after_promos == before_promos

    def test_non_integer_product_id_skipped(self, tmp_db, tmp_path):
        csv_path = tmp_path / "test.csv"
        _write_csv(csv_path, [{
            "product_id": "new_product_id", "sku_code": "NEWSKU",
            "base_sell_price": "100", "promo_type": "percent", "promo_value": "10",
        }])
        stats = imp.run_import(csv_path, Path(tmp_db), commit=True,
                               limit=None, show_sample=0, verbose=False, batch_date=BATCH)
        assert stats["rows_processed"] == 0

    def test_check_constraint_rejects_bad_row(self, tmp_db, tmp_path):
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        pid = _pick_real_product_id(conn)
        _clean_product(conn, pid)
        conn.commit()
        conn.close()

        csv_path = tmp_path / "test.csv"
        _write_csv(csv_path, [{
            "product_id": str(pid), "sku_code": "BAD",
            "promo_type": "bundle",  # missing bundle_buy + bundle_free
        }])

        with pytest.raises(sqlite3.IntegrityError):
            imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                           show_sample=0, verbose=False, batch_date=BATCH)

    def test_atomic_rollback_on_failure(self, tmp_db, tmp_path):
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        pid_ok, pid_bad = _pick_two_product_ids(conn)
        _clean_product(conn, pid_ok)
        _clean_product(conn, pid_bad)
        conn.commit()
        before_bsp = conn.execute(
            "SELECT base_sell_price FROM products WHERE id=?", (pid_ok,)).fetchone()[0]
        conn.close()

        csv_path = tmp_path / "test.csv"
        _write_csv(csv_path, [
            {"product_id": str(pid_ok), "sku_code": "OK",
             "base_sell_price": "9999",
             "promo_type": "percent", "promo_value": "10"},
            {"product_id": str(pid_bad), "sku_code": "BAD",
             "promo_type": "bundle"},
        ])

        with pytest.raises(sqlite3.IntegrityError):
            imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                           show_sample=0, verbose=False, batch_date=BATCH)

        conn = sqlite3.connect(tmp_db)
        after_bsp = conn.execute(
            "SELECT base_sell_price FROM products WHERE id=?", (pid_ok,)).fetchone()[0]
        conn.close()
        assert after_bsp == before_bsp


# ── Idempotency (the headline) ──────────────────────────────────────────────

class TestIdempotency:
    def test_rerun_all_zero(self, tmp_db, tmp_path):
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        pid = _pick_real_product_id(conn)
        _clean_product(conn, pid)
        conn.commit()
        conn.close()

        csv_path = tmp_path / "cat.csv"
        _write_csv(csv_path, [{
            "product_id": str(pid), "sku_code": "IDEMP",
            "base_sell_price": "111",
            "tier1_qty_label": "1 โหล", "tier1_price": "1200",
            "promo_type": "percent", "promo_value": "10",
        }])

        stats1 = imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                                show_sample=0, verbose=False, batch_date=BATCH)
        assert stats1["base_updated"] == 1
        assert stats1["tiers_inserted"] == 1
        assert stats1["promos_inserted"] == 1

        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        promo_count_before = conn.execute("SELECT COUNT(*) FROM promotions").fetchone()[0]
        date_start_before = conn.execute(
            "SELECT date_start FROM promotions WHERE product_id=?", (pid,)).fetchone()[0]
        tier_count_before = conn.execute("SELECT COUNT(*) FROM product_price_tiers").fetchone()[0]
        conn.close()

        stats2 = imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                                show_sample=0, verbose=False, batch_date=BATCH)
        assert stats2["base_updated"] == 0
        assert stats2["tiers_inserted"] == 0
        assert stats2["tiers_updated"] == 0
        assert stats2["promos_closed"] == 0
        assert stats2["promos_inserted"] == 0

        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        assert conn.execute("SELECT COUNT(*) FROM promotions").fetchone()[0] == promo_count_before
        assert conn.execute(
            "SELECT date_start FROM promotions WHERE product_id=?", (pid,)
        ).fetchone()[0] == date_start_before
        assert conn.execute("SELECT COUNT(*) FROM product_price_tiers").fetchone()[0] == tier_count_before
        conn.close()

    def test_control_one_change_moves_only_that_product(self, tmp_db, tmp_path):
        """Change one discount_value → exactly 1 closed / 1 inserted, that
        product's epoch (date_start) moves, a neighbour's does not."""
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        pid_changed, pid_neighbour = _pick_two_product_ids(conn)
        _clean_product(conn, pid_changed)
        _clean_product(conn, pid_neighbour)
        _insert_promo(conn, pid_changed, "percent", discount_value=10.0, date_start=BATCH)
        _insert_promo(conn, pid_neighbour, "percent", discount_value=10.0, date_start=BATCH)
        conn.commit()
        neighbour_start_before = conn.execute(
            "SELECT date_start FROM promotions WHERE product_id=?", (pid_neighbour,)
        ).fetchone()[0]
        conn.close()

        csv_path = tmp_path / "cat.csv"
        _write_csv(csv_path, [
            {"product_id": str(pid_changed), "sku_code": "CHANGED",
             "promo_type": "percent", "promo_value": "20"},   # was 10 -> changed
            {"product_id": str(pid_neighbour), "sku_code": "SAME",
             "promo_type": "percent", "promo_value": "10"},   # unchanged
        ])

        new_batch = "2026-07-01"
        stats = imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                               show_sample=0, verbose=False, batch_date=new_batch)
        assert stats["promos_closed"] == 1
        assert stats["promos_inserted"] == 1

        # Closing only date-closes (never flips is_active early, per 2a's
        # rule) — so the OLD row for pid_changed is still is_active=1 with a
        # past date_end. Query the most-recently-INSERTED row (highest id)
        # per product, which is the one that actually answers "today".
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        changed_latest = conn.execute(
            "SELECT date_start FROM promotions WHERE product_id=? ORDER BY id DESC LIMIT 1",
            (pid_changed,)).fetchone()
        assert changed_latest["date_start"] == new_batch  # epoch moved
        neighbour_latest = conn.execute(
            "SELECT date_start FROM promotions WHERE product_id=? ORDER BY id DESC LIMIT 1",
            (pid_neighbour,)).fetchone()
        assert neighbour_latest["date_start"] == neighbour_start_before  # untouched
        conn.close()

    def test_rerun_newer_batch_date_identical_offer_still_zero(self, tmp_db, tmp_path):
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        pid = _pick_real_product_id(conn)
        _clean_product(conn, pid)
        conn.commit()
        conn.close()

        csv_path = tmp_path / "cat.csv"
        _write_csv(csv_path, [{
            "product_id": str(pid), "sku_code": "SAME",
            "promo_type": "percent", "promo_value": "10",
        }])

        imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                       show_sample=0, verbose=False, batch_date=BATCH)

        conn = sqlite3.connect(tmp_db)
        date_start_before = conn.execute(
            "SELECT date_start FROM promotions WHERE product_id=?", (pid,)).fetchone()[0]
        conn.close()

        stats2 = imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                                show_sample=0, verbose=False, batch_date="2026-09-01")
        assert stats2["promos_closed"] == 0
        assert stats2["promos_inserted"] == 0

        conn = sqlite3.connect(tmp_db)
        date_start_after = conn.execute(
            "SELECT date_start FROM promotions WHERE product_id=?", (pid,)).fetchone()[0]
        conn.close()
        assert date_start_after == date_start_before  # NOT moved to the newer batch date

    def test_already_closed_occupant_is_not_touched_by_a_later_run(self, tmp_db, tmp_path):
        """Regression (found via the real-CSV rehearsal): closing an occupant
        only date-closes it (is_active stays 1, per 2a's rule) — so a THIRD
        run, at a batch date newer than that close, must not re-select the
        already-closed row as a candidate occupant and touch it again. Real
        shape hit: product 445 carries a 'fixed' promo (price slot) AND a
        separate 'mixed' promo (qty slot only, discount_value NULL) at the
        same time; replacing just the qty-slot one across two runs re-closed
        the same already-closed row a second time."""
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        pid = _pick_real_product_id(conn)
        _clean_product(conn, pid)
        # Two independent occupants on two different slots, like pid 445 above.
        _insert_promo(conn, pid, "fixed", discount_value=220.0, date_start=BATCH)
        _insert_promo(conn, pid, "mixed", bundle_buy=1, bundle_free=1,
                      gift_desc="clean text", gift_qty="1", date_start=BATCH)
        conn.commit()
        conn.close()

        csv_path = tmp_path / "cat.csv"
        _write_csv(csv_path, [{
            "product_id": str(pid), "sku_code": "X",
            "special_price": "220",                      # matches the 'fixed' occupant exactly
            "promo_type": "mixed",
            "bundle_buy": "1", "bundle_free": "1",
            "gift_desc": "messy raw catalogue text", "gift_qty": "1",  # differs -> changed
        }])

        run1 = imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                              show_sample=0, verbose=False, batch_date="2026-08-01")
        assert run1["promos_closed"] == 1     # only the mixed occupant, fixed is preserved
        assert run1["promos_inserted"] == 1

        # Re-run the SAME file at a NEWER batch date. The only live occupant
        # of the qty slot is now the row run1 just inserted, with the SAME
        # (messy) gift_desc as the CSV — must match and preserve. The row
        # run1 already closed must be left alone, not re-closed.
        run2 = imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                              show_sample=0, verbose=False, batch_date="2026-09-01")
        assert run2["promos_closed"] == 0
        assert run2["promos_inserted"] == 0


# ── Tier reconciliation matrix (B0) ──────────────────────────────────────────

class TestTierReconciliation:
    def test_tier_price_change_updates_in_place(self, tmp_db, tmp_path):
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        pid = _pick_real_product_id(conn)
        _clean_product(conn, pid)
        conn.execute(
            "INSERT INTO product_price_tiers (product_id, qty_label, price, note) "
            "VALUES (?, '1 โหล', 100, NULL)", (pid,))
        conn.commit()
        tier_id_before = conn.execute(
            "SELECT id FROM product_price_tiers WHERE product_id=?", (pid,)).fetchone()[0]
        conn.close()

        csv_path = tmp_path / "cat.csv"
        _write_csv(csv_path, [{
            "product_id": str(pid), "sku_code": "T",
            "tier1_qty_label": "1 โหล", "tier1_price": "150",
        }])
        stats = imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                               show_sample=0, verbose=False, batch_date=BATCH)
        assert stats["tiers_inserted"] == 0
        assert stats["tiers_updated"] == 1

        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, price FROM product_price_tiers WHERE product_id=?", (pid,)).fetchone()
        assert row["id"] == tier_id_before  # same row, updated in place
        assert row["price"] == 150
        conn.close()

    def test_tier_note_only_change_updates_but_does_not_move_epoch(self, tmp_db, tmp_path):
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        pid = _pick_real_product_id(conn)
        _clean_product(conn, pid)
        conn.execute(
            "INSERT INTO product_price_tiers (product_id, qty_label, price, note) "
            "VALUES (?, '1 โหล', 100, 'old note')", (pid,))
        conn.commit()
        tier_id = conn.execute(
            "SELECT id FROM product_price_tiers WHERE product_id=?", (pid,)).fetchone()[0]
        # Scope the audit_log check to THIS tier's row_id — tmp_db clones the
        # live dev DB WITH its history, so an unscoped COUNT(*) would also
        # count every unrelated price-changing tier edit already on record.
        price_changed_audit_before = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE table_name='product_price_tiers' "
            "AND row_id=? AND action='UPDATE' "
            "AND json_extract(changed_fields, '$.price') IS NOT NULL", (tier_id,)
        ).fetchone()[0]
        conn.close()

        csv_path = tmp_path / "cat.csv"
        _write_csv(csv_path, [{
            "product_id": str(pid), "sku_code": "T",
            "tier1_qty_label": "1 โหล", "tier1_price": "100", "tier1_note": "new note",
        }])
        stats = imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                               show_sample=0, verbose=False, batch_date=BATCH)
        assert stats["tiers_updated"] == 1

        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT price, note FROM product_price_tiers WHERE product_id=?", (pid,)).fetchone()
        assert row["note"] == "new note"
        # Epoch (audit_log signal for price_lookup.epochs_for) must NOT record
        # a price change for a note-only edit — the count for THIS row_id
        # must not have moved.
        price_changed_audit_after = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE table_name='product_price_tiers' "
            "AND row_id=? AND action='UPDATE' "
            "AND json_extract(changed_fields, '$.price') IS NOT NULL", (tier_id,)
        ).fetchone()[0]
        assert price_changed_audit_after == price_changed_audit_before
        conn.close()

    def test_tier_in_db_not_in_csv_is_untouched(self, tmp_db, tmp_path):
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        pid = _pick_real_product_id(conn)
        _clean_product(conn, pid)
        conn.execute(
            "INSERT INTO product_price_tiers (product_id, qty_label, price, note) "
            "VALUES (?, '1 ลัง', 999, NULL)", (pid,))
        conn.commit()
        conn.close()

        csv_path = tmp_path / "cat.csv"
        _write_csv(csv_path, [{
            "product_id": str(pid), "sku_code": "T",
            "tier1_qty_label": "1 โหล", "tier1_price": "50",  # a DIFFERENT label
        }])
        stats = imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                               show_sample=0, verbose=False, batch_date=BATCH)
        assert stats["tiers_inserted"] == 1  # only the new label

        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        untouched = conn.execute(
            "SELECT price FROM product_price_tiers WHERE product_id=? AND qty_label='1 ลัง'",
            (pid,)).fetchone()
        assert untouched["price"] == 999
        conn.close()


# ── B3: mixed occupant partial replacement ──────────────────────────────────

class TestMixedOccupantRefusal:
    def test_price_only_row_against_mixed_occupant_aborts(self, tmp_db, tmp_path):
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        pid = _pick_real_product_id(conn)
        _clean_product(conn, pid)
        _insert_promo(conn, pid, "mixed", discount_value=10.0, bundle_buy=120, bundle_free=12)
        conn.commit()
        conn.close()

        csv_path = tmp_path / "cat.csv"
        _write_csv(csv_path, [{
            "product_id": str(pid), "sku_code": "X", "special_price": "80",
        }])
        with pytest.raises(imp.RunAbort) as exc_info:
            imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                           show_sample=0, verbose=False, batch_date="2026-07-01")
        assert str(pid) in str(exc_info.value)  # names the offending product

        conn = sqlite3.connect(tmp_db)
        cnt = conn.execute(
            "SELECT COUNT(*) FROM promotions WHERE product_id=? AND is_active=1",
            (pid,)).fetchone()[0]
        assert cnt == 1  # nothing written; the mixed occupant survives untouched
        conn.close()

    def test_qty_only_row_against_mixed_occupant_aborts(self, tmp_db, tmp_path):
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        pid = _pick_real_product_id(conn)
        _clean_product(conn, pid)
        _insert_promo(conn, pid, "mixed", discount_value=10.0, bundle_buy=120, bundle_free=12)
        conn.commit()
        conn.close()

        csv_path = tmp_path / "cat.csv"
        _write_csv(csv_path, [{
            "product_id": str(pid), "sku_code": "X",
            "promo_type": "bundle", "bundle_buy": "60", "bundle_free": "6",
        }])
        with pytest.raises(imp.RunAbort) as exc_info:
            imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                           show_sample=0, verbose=False, batch_date="2026-07-01")
        assert str(pid) in str(exc_info.value)  # names the offending product

        conn = sqlite3.connect(tmp_db)
        cnt = conn.execute(
            "SELECT COUNT(*) FROM promotions WHERE product_id=? AND is_active=1",
            (pid,)).fetchone()[0]
        assert cnt == 1
        conn.close()

    def test_mixed_occupant_fully_replaced_by_matching_mixed_intent_preserves(self, tmp_db, tmp_path):
        """A mixed occupant re-supplied identically via promo_type=mixed →
        preserved (0 writes), the common real-world re-import case."""
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        pid = _pick_real_product_id(conn)
        _clean_product(conn, pid)
        _insert_promo(conn, pid, "mixed", discount_value=10.0, bundle_buy=120, bundle_free=12,
                      date_start=BATCH)
        conn.commit()
        conn.close()

        csv_path = tmp_path / "cat.csv"
        _write_csv(csv_path, [{
            "product_id": str(pid), "sku_code": "X",
            "promo_type": "mixed", "promo_value": "10",
            "bundle_buy": "120", "bundle_free": "12",
        }])
        stats = imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                               show_sample=0, verbose=False, batch_date="2026-07-01")
        assert stats["promos_closed"] == 0
        assert stats["promos_inserted"] == 0


# ── B4: two price-slot promos in one row ────────────────────────────────────

class TestTwoPriceSlotPromosAbort:
    def test_special_price_and_percent_in_one_row_aborts_whole_run(self, tmp_db, tmp_path):
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        pid_bad, pid_ok = _pick_two_product_ids(conn)
        _clean_product(conn, pid_bad)
        _clean_product(conn, pid_ok)
        conn.commit()
        conn.close()

        csv_path = tmp_path / "cat.csv"
        _write_csv(csv_path, [
            {"product_id": str(pid_bad), "sku_code": "BAD",
             "special_price": "80", "promo_type": "percent", "promo_value": "10"},
            {"product_id": str(pid_ok), "sku_code": "OK",
             "promo_type": "percent", "promo_value": "5"},
        ])
        with pytest.raises(imp.RunAbort) as exc_info:
            imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                           show_sample=0, verbose=False, batch_date=BATCH)
        assert str(pid_bad) in str(exc_info.value)  # names the offending product/row

        # CONTROL: the clean row in the SAME file was also not written —
        # proves the abort is atomic, not per-row.
        conn = sqlite3.connect(tmp_db)
        cnt_ok = conn.execute(
            "SELECT COUNT(*) FROM promotions WHERE product_id=?", (pid_ok,)).fetchone()[0]
        assert cnt_ok == 0
        conn.close()


# ── B5: file-shape validation + concurrency ─────────────────────────────────

class TestFileShapeAndConcurrency:
    def test_duplicate_product_id_aborts(self, tmp_db, tmp_path):
        conn = sqlite3.connect(tmp_db)
        pid = _pick_real_product_id(conn)
        conn.close()

        csv_path = tmp_path / "cat.csv"
        _write_csv(csv_path, [
            {"product_id": str(pid), "sku_code": "A", "base_sell_price": "10"},
            {"product_id": str(pid), "sku_code": "B", "base_sell_price": "20"},
        ])
        with pytest.raises(imp.RunAbort) as exc_info:
            imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                           show_sample=0, verbose=False, batch_date=BATCH)
        assert str(pid) in str(exc_info.value)  # names the duplicated product_id

    def test_duplicate_tier_label_within_row_aborts(self, tmp_db, tmp_path):
        conn = sqlite3.connect(tmp_db)
        pid = _pick_real_product_id(conn)
        conn.close()

        csv_path = tmp_path / "cat.csv"
        _write_csv(csv_path, [{
            "product_id": str(pid), "sku_code": "A",
            "tier1_qty_label": "1 โหล", "tier1_price": "100",
            "tier2_qty_label": "1 โหล", "tier2_price": "200",
        }])
        with pytest.raises(imp.RunAbort) as exc_info:
            imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                           show_sample=0, verbose=False, batch_date=BATCH)
        assert "1 โหล" in str(exc_info.value)  # names the duplicated tier label

    def test_row_with_no_promo_columns_leaves_existing_promo_untouched(self, tmp_db, tmp_path):
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        pid = _pick_real_product_id(conn)
        _clean_product(conn, pid)
        _insert_promo(conn, pid, "percent", discount_value=10.0, date_start=BATCH)
        conn.commit()
        promo_before = dict(conn.execute(
            "SELECT * FROM promotions WHERE product_id=?", (pid,)).fetchone())
        conn.close()

        csv_path = tmp_path / "cat.csv"
        _write_csv(csv_path, [{
            "product_id": str(pid), "sku_code": "A", "base_sell_price": "50",
        }])
        stats = imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                               show_sample=0, verbose=False, batch_date="2026-07-01")
        assert stats["promos_closed"] == 0
        assert stats["promos_inserted"] == 0

        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        promo_after = dict(conn.execute(
            "SELECT * FROM promotions WHERE product_id=?", (pid,)).fetchone())
        conn.close()
        assert promo_after == promo_before

    def test_backdating_close_before_occupant_start_aborts(self, tmp_db, tmp_path):
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        pid = _pick_real_product_id(conn)
        _clean_product(conn, pid)
        _insert_promo(conn, pid, "percent", discount_value=10.0, date_start="2026-08-01")
        conn.commit()
        conn.close()

        csv_path = tmp_path / "cat.csv"
        _write_csv(csv_path, [{
            "product_id": str(pid), "sku_code": "A",
            "promo_type": "percent", "promo_value": "20",  # changed -> would close
        }])
        # batch date EARLIER than the occupant's own date_start
        with pytest.raises(imp.RunAbort) as exc_info:
            imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                           show_sample=0, verbose=False, batch_date="2026-06-01")
        assert str(pid) in str(exc_info.value)  # names the offending product/promo

        conn = sqlite3.connect(tmp_db)
        cnt = conn.execute(
            "SELECT COUNT(*) FROM promotions WHERE product_id=? AND is_active=1",
            (pid,)).fetchone()[0]
        assert cnt == 1  # untouched
        conn.close()

    def test_concurrent_writer_excluded_after_begin_immediate(self, tmp_db, tmp_path):
        """B5 concurrency: inject a second connection right after BEGIN
        IMMEDIATE (before any read/write) and confirm it is locked out."""
        conn = sqlite3.connect(tmp_db)
        pid = _pick_real_product_id(conn)
        conn.close()

        csv_path = tmp_path / "cat.csv"
        _write_csv(csv_path, [{"product_id": str(pid), "sku_code": "A", "base_sell_price": "77"}])

        blocked = {}

        def hook(_conn):
            conn2 = sqlite3.connect(tmp_db, timeout=0.2)
            try:
                conn2.execute("BEGIN IMMEDIATE")
                blocked["locked"] = False
                conn2.rollback()
            except sqlite3.OperationalError:
                blocked["locked"] = True
            finally:
                conn2.close()

        imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                       show_sample=0, verbose=False, batch_date=BATCH,
                       _after_begin_hook=hook)
        assert blocked.get("locked") is True


# ── Dry-run / commit: same planner (B5) ─────────────────────────────────────

class TestSamePlanner:
    def test_dry_run_and_commit_produce_identical_stats(self, tmp_db, tmp_path):
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        pid = _pick_real_product_id(conn)
        _clean_product(conn, pid)
        conn.commit()
        conn.close()

        csv_path = tmp_path / "cat.csv"
        _write_csv(csv_path, [{
            "product_id": str(pid), "sku_code": "A",
            "base_sell_price": "321",
            "tier1_qty_label": "1 โหล", "tier1_price": "3200",
            "promo_type": "percent", "promo_value": "12",
        }])

        stats_dry = imp.run_import(csv_path, Path(tmp_db), commit=False, limit=None,
                                   show_sample=0, verbose=False, batch_date=BATCH)
        stats_commit = imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                                      show_sample=0, verbose=False, batch_date=BATCH)
        assert stats_dry == stats_commit


# ── A 1-row CSV touches only that product ───────────────────────────────────

def test_one_row_csv_moves_only_that_products_promo(tmp_db, tmp_path):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    before_moved = conn.execute(
        "SELECT COUNT(*) FROM promotions WHERE date_end IS NOT NULL").fetchone()[0]
    row = conn.execute(
        "SELECT product_id, discount_value FROM promotions "
        "WHERE promo_type='percent' AND is_active=1 LIMIT 1").fetchone()
    pid, current_value = row["product_id"], row["discount_value"]
    conn.close()

    csv_path = tmp_path / "cat.csv"
    _write_csv(csv_path, [{
        "product_id": str(pid), "sku_code": "A",
        "promo_type": "percent", "promo_value": str((current_value or 0) + 1),
    }])
    imp.run_import(csv_path, Path(tmp_db), commit=True, limit=None,
                   show_sample=0, verbose=False, batch_date="2026-07-15")

    conn = sqlite3.connect(tmp_db)
    after_moved = conn.execute(
        "SELECT COUNT(*) FROM promotions WHERE date_end IS NOT NULL").fetchone()[0]
    conn.close()
    assert after_moved - before_moved == 1


# ── Backup function ─────────────────────────────────────────────────────────

def test_backup_db_creates_timestamped_file(tmp_db, tmp_path):
    backup = imp.backup_db(Path(tmp_db))
    assert backup.exists()
    assert "backup-pre-catalog-import-" in backup.name
    assert backup.parent == Path(tmp_db).parent
    assert backup.stat().st_size == Path(tmp_db).stat().st_size


# ── CLI arg validation ───────────────────────────────────────────────────────

def test_batch_date_required(tmp_db, tmp_path):
    csv_path = tmp_path / "cat.csv"
    _write_csv(csv_path, [{"product_id": "1", "sku_code": "A"}])
    with pytest.raises(ValueError):
        imp.run_import(csv_path, Path(tmp_db), commit=False, limit=None,
                       show_sample=0, verbose=False, batch_date=None)


def test_batch_date_malformed_rejected(tmp_db, tmp_path):
    csv_path = tmp_path / "cat.csv"
    _write_csv(csv_path, [{"product_id": "1", "sku_code": "A"}])
    with pytest.raises(ValueError):
        imp.run_import(csv_path, Path(tmp_db), commit=False, limit=None,
                       show_sample=0, verbose=False, batch_date="2026/06/01")
