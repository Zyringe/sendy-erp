"""Tests for scripts/normalize_base_price.py (TDD — written first).

The normalizer turns the RAW catalog CSV (messy base_price + Thai free-text
promos) into the normalized schema that import_catalog_pricing.py reads.

Money math + parser → mandatory TDD (project rule). Cases cover:
  - clean number (with/without comma thousands)
  - /โหล divide picked via answer-key match
  - /ตัว or /แผง as-is picked via answer-key match (suffix == unit_type ratio 1)
  - below-cost guard (no existing base, divide would go below cost → blank+note)
  - answer-key present but neither candidate close → blank+note
  - multi-tier split ("40/แผง,360/โหล" → tier1+tier2)
  - malformed no-slash ("560โหล") → best-effort or blank+note
  - percent promo ("30% OFF", "ลด 20%")
  - bundle promo ("10 แถม 1", "ซื้อ 12 ฟรี 1")
  - mixed promo ("10% OFF ซื้อ 12 แถม 1")
  - gift promo ("แถม...")
  - special_price numeric → special_price column
  - special_price non-numeric ("ลด 10%") → routed to promo parser
  - unparseable promo ("มีขายในออนไลน์ ...") → blank promo_type + note
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import normalize_base_price as nz


# ── parse_promo ─────────────────────────────────────────────────────────────

class TestParsePromo:
    def test_percent_off_english(self):
        p = nz.parse_promo("30% OFF")
        assert p["promo_type"] == "percent"
        assert p["promo_value"] == 30.0
        assert not p["normalize_notes"]

    def test_percent_thai_lod(self):
        p = nz.parse_promo("ลด 20%")
        assert p["promo_type"] == "percent"
        assert p["promo_value"] == 20.0

    def test_percent_thai_no_space(self):
        p = nz.parse_promo("ลด10%")
        assert p["promo_type"] == "percent"
        assert p["promo_value"] == 10.0

    def test_bundle_thaem(self):
        p = nz.parse_promo("10 แถม 1")
        assert p["promo_type"] == "bundle"
        assert p["bundle_buy"] == 10
        assert p["bundle_free"] == 1

    def test_bundle_sue_free(self):
        p = nz.parse_promo("ซื้อ 12 ฟรี 1")
        assert p["promo_type"] == "bundle"
        assert p["bundle_buy"] == 12
        assert p["bundle_free"] == 1

    def test_bundle_one_plus_one(self):
        p = nz.parse_promo("1 แถม 1")
        assert p["promo_type"] == "bundle"
        assert p["bundle_buy"] == 1
        assert p["bundle_free"] == 1

    def test_mixed_percent_plus_bundle(self):
        p = nz.parse_promo("10% OFF ซื้อ 12 แถม 1")
        assert p["promo_type"] == "mixed"
        assert p["promo_value"] == 10.0
        assert p["bundle_buy"] == 12
        assert p["bundle_free"] == 1

    def test_percent_with_yok_lang_condition(self):
        # "ซื้อยกลัง ลด5%" → percent 5 + condition ยกลัง (no bundle qty)
        p = nz.parse_promo("ซื้อยกลัง ลด5%")
        assert p["promo_type"] == "percent"
        assert p["promo_value"] == 5.0
        assert p["bundle_condition"] == "ยกลัง"
        assert p["bundle_buy"] is None  # percent CHECK forbids bundle_buy

    def test_gift_freebie(self):
        p = nz.parse_promo("ซื้อ 1 แถม 1+แถมใบเลื่อยคันธนู30\" 1 ใบ")
        # bundle 1+1 AND a gift → mixed
        assert p["promo_type"] == "mixed"
        assert p["bundle_buy"] == 1
        assert p["bundle_free"] == 1
        assert p["gift_desc"]

    def test_mixed_percent_plus_gift(self):
        p = nz.parse_promo("10% OFF แถมดจ.สแตนเลส 20 ดอก")
        assert p["promo_type"] == "mixed"
        assert p["promo_value"] == 10.0
        assert p["gift_desc"]

    def test_unparseable_online_note_blank(self):
        p = nz.parse_promo("มีขายในออนไลน์ อันละ 45 บาท")
        assert p["promo_type"] == ""
        assert p["normalize_notes"]

    def test_blank_promo(self):
        p = nz.parse_promo("")
        assert p["promo_type"] == ""
        assert not p["normalize_notes"]

    def test_emitted_percent_satisfies_check_shape(self):
        # percent: only discount_value set, no bundle/gift
        p = nz.parse_promo("30% OFF")
        assert p["bundle_buy"] is None and p["bundle_free"] is None
        assert not p["gift_desc"]

    def test_emitted_bundle_satisfies_check_shape(self):
        # bundle: buy+free set, discount_value None, no gift
        p = nz.parse_promo("10 แถม 1")
        assert p["promo_value"] is None
        assert not p["gift_desc"]


# ── parse_special_price ─────────────────────────────────────────────────────

class TestSpecialPrice:
    def test_numeric_special(self):
        sp, promo_passthru, note = nz.parse_special_price("20.00")
        assert sp == 20.0
        assert promo_passthru is None

    def test_numeric_with_comma(self):
        sp, promo_passthru, note = nz.parse_special_price("1,250.00")
        assert sp == 1250.0

    def test_nonnumeric_special_routes_to_promo(self):
        # "ลด 10%" in the ราคาพิเศษ column is a promo, not a price
        sp, promo_passthru, note = nz.parse_special_price("ลด 10%")
        assert sp is None
        assert promo_passthru == "ลด 10%"

    def test_blank_special(self):
        sp, promo_passthru, note = nz.parse_special_price("")
        assert sp is None
        assert promo_passthru is None


# ── resolve_base_price ──────────────────────────────────────────────────────

class TestResolveBase:
    def test_clean_number(self):
        r = nz.resolve_base_price("30.00", existing_base=0.0, cost=10.0,
                                  unit_type="ตัว", uc_ratios={})
        assert r["base_sell_price"] == 30.0
        assert r["tiers"] == []

    def test_clean_number_with_comma(self):
        r = nz.resolve_base_price("1,250.00", existing_base=0.0, cost=400.0,
                                  unit_type="ตัว", uc_ratios={})
        assert r["base_sell_price"] == 1250.0

    def test_divide_picked_by_answer_key(self):
        # pid 306-like: 170/โหล, existing base 15 (per-unit), ratio 12 → divided=14.17 ≈ 15
        r = nz.resolve_base_price("170/โหล", existing_base=15.0, cost=6.0,
                                  unit_type="ตัว", uc_ratios={"โหล": 12})
        assert abs(r["base_sell_price"] - 14.166666666666666) < 1e-6

    def test_as_is_picked_by_answer_key(self):
        # pid 47-like: 90/ตัว, ratio 1 (suffix==unit_type), existing base 90
        r = nz.resolve_base_price("90/ตัว", existing_base=90.0, cost=30.0,
                                  unit_type="ตัว", uc_ratios={"ตัว": 1})
        assert r["base_sell_price"] == 90.0

    def test_as_is_phaeng_ratio_one(self):
        # pid 351-like: 75/แผง, ratio 1, existing base 75, cost 45
        r = nz.resolve_base_price("75/แผง", existing_base=75.0, cost=45.0,
                                  unit_type="แผง", uc_ratios={"แผง": 1})
        assert r["base_sell_price"] == 75.0

    def test_below_cost_guard_no_existing_base(self):
        # no existing base; divide would go below cost; as_is gives sane margin → pick as_is
        # 230/โหล, cost stored per-โหล already at e.g. 180 (cost*4 band: 180<=230<=720)
        r = nz.resolve_base_price("230/โหล", existing_base=0.0, cost=180.0,
                                  unit_type="ตัว", uc_ratios={"โหล": 12})
        # divided = 19.17 < cost 180 → insane; as_is 230 within [180, 720] → pick as_is
        assert r["base_sell_price"] == 230.0

    def test_answer_key_neither_close_blanks(self):
        # pid 300-like: 70/โหล, existing base 7, cost 2.7, ratio 12 → div 5.83 (17% off 7), as_is 70
        # neither within tolerance → blank + note, DO NOT overwrite existing 7
        r = nz.resolve_base_price("70/โหล", existing_base=7.0, cost=2.7,
                                  unit_type="ตัว", uc_ratios={"โหล": 12})
        assert r["base_sell_price"] is None
        assert r["normalize_notes"]

    def test_no_existing_base_no_sane_candidate_blanks(self):
        # cost junk/tiny → cannot disambiguate → blank + note
        r = nz.resolve_base_price("535/กล่อง", existing_base=0.0, cost=0.0004,
                                  unit_type="ตัว", uc_ratios={"กล่อง": 1000})
        assert r["base_sell_price"] is None
        assert r["normalize_notes"]

    def test_multi_tier_split(self):
        # "85/1กิโล,1700/ลัง" → two tiers, base blank (smallest unit 'กิโล' != unit_type)
        r = nz.resolve_base_price("85/1กิโล,1700/ลัง", existing_base=0.0, cost=70.0,
                                  unit_type="ตัว", uc_ratios={})
        labels = {t["qty_label"]: t["price"] for t in r["tiers"]}
        assert labels.get("1กิโล") == 85.0
        assert labels.get("ลัง") == 1700.0
        assert r["base_sell_price"] is None
        assert r["normalize_notes"]

    def test_multi_tier_phaeng_loh(self):
        # "40/แผง,360/โหล" → tiers แผง=40, โหล=360
        r = nz.resolve_base_price("40/แผง,360/โหล", existing_base=0.0, cost=0.0,
                                  unit_type="ตัว", uc_ratios={})
        labels = {t["qty_label"]: t["price"] for t in r["tiers"]}
        assert labels.get("แผง") == 40.0
        assert labels.get("โหล") == 360.0

    def test_malformed_no_slash_blanks(self):
        # "560โหล" — no separator → best-effort, but blank base + note (unsure)
        r = nz.resolve_base_price("560โหล", existing_base=0.0, cost=0.0,
                                  unit_type="ตัว", uc_ratios={"โหล": 12})
        assert r["base_sell_price"] is None
        assert r["normalize_notes"]

    def test_blank_base(self):
        r = nz.resolve_base_price("", existing_base=0.0, cost=0.0,
                                  unit_type="ตัว", uc_ratios={})
        assert r["base_sell_price"] is None
        assert r["tiers"] == []
        assert not r["normalize_notes"]


# ── normalize_row (integration of base + promo + special) ──────────────────

class TestNormalizeRow:
    def test_full_row_clean(self):
        row = {"product_id": "1", "sku_code": "X", "product_name": "n",
               "base_price": "30.00", "ราคาพิเศษ": "20.00", "โปรโมชั่น": ""}
        out = nz.normalize_row(row, existing_base=30.0, cost=10.0,
                               unit_type="ตัว", uc_ratios={})
        assert out["base_sell_price"] == "30.0"
        assert out["special_price"] == "20.0"
        assert out["promo_type"] == ""

    def test_row_with_percent_promo(self):
        row = {"product_id": "15", "sku_code": "X", "product_name": "n",
               "base_price": "100.00", "ราคาพิเศษ": "", "โปรโมชั่น": "20% OFF"}
        out = nz.normalize_row(row, existing_base=100.0, cost=40.0,
                               unit_type="ตัว", uc_ratios={})
        assert out["promo_type"] == "percent"
        assert out["promo_value"] == "20.0"

    def test_row_nonnumeric_special_becomes_promo(self):
        row = {"product_id": "15", "sku_code": "X", "product_name": "n",
               "base_price": "100.00", "ราคาพิเศษ": "20% OFF", "โปรโมชั่น": ""}
        out = nz.normalize_row(row, existing_base=100.0, cost=40.0,
                               unit_type="ตัว", uc_ratios={})
        assert out["special_price"] == ""        # not a numeric special
        assert out["promo_type"] == "percent"     # routed to promo
        assert out["promo_value"] == "20.0"


# ── F3: slash-separated multi-tier WITHOUT a comma must not drop a tier ──────

class TestF3SlashMultiTier:
    def test_slash_multi_phaeng_loh(self):
        # "40/แผง360/โหล" — two priced segments, slash-separated, NO comma.
        # Must parse BOTH (tier1 40/แผง, tier2 360/โหล), not swallow 360 into the
        # first suffix.
        kind, payload = nz.classify_base_price("40/แผง360/โหล")
        assert kind == "multi"
        labels = {lab: val for val, lab in payload}
        assert labels.get("แผง") == 40.0
        assert labels.get("โหล") == 360.0

    def test_slash_multi_kilo_lang(self):
        # "85/1กิโล1700/ลัง" — same shape with a qty-prefixed first unit.
        kind, payload = nz.classify_base_price("85/1กิโล1700/ลัง")
        assert kind == "multi"
        labels = {lab: val for val, lab in payload}
        assert labels.get("1กิโล") == 85.0
        assert labels.get("ลัง") == 1700.0

    def test_legit_single_suffix_unaffected(self):
        # A genuine single suffixed price must stay 'suffixed', not become multi.
        kind, payload = nz.classify_base_price("230/โหล")
        assert kind == "suffixed"
        assert payload == (230.0, "โหล")

    def test_slash_multi_resolves_both_tiers(self):
        # Through resolve_base_price the two tiers must surface (no silent drop).
        r = nz.resolve_base_price("40/แผง360/โหล", existing_base=0.0, cost=0.0,
                                  unit_type="ตัว", uc_ratios={})
        labels = {t["qty_label"]: t["price"] for t in r["tiers"]}
        assert labels.get("แผง") == 40.0
        assert labels.get("โหล") == 360.0


# ── F5: out-of-range percent promo must not be emitted (would crash import) ──

class TestF5PercentRangeGuard:
    def test_percent_over_100_not_emitted(self):
        # "ลด 150%" — 150 > 100 violates promotions CHECK. Must NOT emit a
        # percent promo; leave promo_type blank + flag for review.
        p = nz.parse_promo("ลด 150%")
        assert p["promo_type"] == ""
        assert p["promo_value"] is None
        assert p["normalize_notes"]
        assert "150" in p["normalize_notes"]

    def test_valid_percent_still_emitted(self):
        p = nz.parse_promo("ลด 30%")
        assert p["promo_type"] == "percent"
        assert p["promo_value"] == 30.0
        assert not p["normalize_notes"]

    def test_percent_exactly_100_ok(self):
        # 100 is within BETWEEN 0 AND 100 — still valid.
        p = nz.parse_promo("ลด 100%")
        assert p["promo_type"] == "percent"
        assert p["promo_value"] == 100.0

    def test_mixed_drops_out_of_range_percent_keeps_bundle(self):
        # "ลด 150% ซื้อ 12 แถม 1" — the percent is out of range; the bundle is
        # valid. Must NOT carry the 150 into the emitted promo (would crash).
        p = nz.parse_promo("ลด 150% ซื้อ 12 แถม 1")
        assert p["promo_value"] is None
        # bundle survives as a clean bundle promo
        assert p["bundle_buy"] == 12
        assert p["bundle_free"] == 1
        assert p["normalize_notes"]
        assert "150" in p["normalize_notes"]


# ── F6: duplicate product_id across input rows must be flagged ───────────────

class TestF6DuplicateProductId:
    def test_run_flags_duplicate_product_ids(self, tmp_path):
        # Two rows with product_id=18 (different sku/price) → both flagged, plus
        # a summary warning printed at the end.
        in_csv = tmp_path / "raw.csv"
        out_csv = tmp_path / "norm.csv"
        in_csv.write_text(
            "product_id,sku,sku_code,product_name,base_price,ราคาพิเศษ,โปรโมชั่น,Remark\n"
            "18,A,SKU-A,nameA,30.00,,,\n"
            "18,B,SKU-B,nameB,60.00,,,\n"
            "19,C,SKU-C,nameC,40.00,,,\n",
            encoding="utf-8")
        db_path = _make_answer_key_db(tmp_path, {
            18: (30.0, 10.0, "ตัว"), 19: (40.0, 12.0, "ตัว")})
        out_rows = nz.run(in_csv, out_csv, db_path, verbose=False)

        by_pid = {}
        for o in out_rows:
            by_pid.setdefault(o["product_id"], []).append(o)
        dup_rows = by_pid["18"]
        assert len(dup_rows) == 2
        for o in dup_rows:
            assert "DUPLICATE product_id 18" in o["normalize_notes"]
        # the unique row is not flagged
        assert "DUPLICATE" not in by_pid["19"][0]["normalize_notes"]

    def test_run_prints_dup_summary(self, tmp_path, capsys):
        in_csv = tmp_path / "raw.csv"
        out_csv = tmp_path / "norm.csv"
        in_csv.write_text(
            "product_id,sku,sku_code,product_name,base_price,ราคาพิเศษ,โปรโมชั่น,Remark\n"
            "18,A,SKU-A,nameA,30.00,,,\n"
            "18,B,SKU-B,nameB,60.00,,,\n",
            encoding="utf-8")
        db_path = _make_answer_key_db(tmp_path, {18: (30.0, 10.0, "ตัว")})
        nz.run(in_csv, out_csv, db_path, verbose=True)
        captured = capsys.readouterr().out
        assert "duplicate product_id" in captured.lower()
        assert "18" in captured


# ── ISL-008: pack-unit suffix resolved via unit_conversions ─────────────────
#
# The prior scrutiny pass flagged "pack-unit suffixes blanked even when
# unit_conversions has a ratio". Verified against this version of the script:
# _ratio_for() already consults uc_ratios FIRST, so the conversion lookup
# already happens for กล่อง/ถุง/ชุด/etc. These are regression guards that lock
# in the correct behaviour so a future refactor can't reintroduce the bug the
# audit feared.

class TestISL008PackUnitConversion:
    def test_pack_unit_with_conversion_resolves_cost_band(self):
        # "240/กล่อง", no existing base, cost 18 (per ตัว). unit_conversions has
        # ratio 12 for กล่อง → divided=20.0 sits in [cost, cost*MAX_MARGIN] band,
        # as_is 240 does NOT → must pick divided, not blank.
        r = nz.resolve_base_price("240/กล่อง", existing_base=0.0, cost=18.0,
                                  unit_type="ตัว", uc_ratios={"กล่อง": 12})
        assert r["base_sell_price"] == 20.0

    def test_pack_unit_with_conversion_answer_key(self):
        # existing base 20 per ตัว, "240/กล่อง" ratio 12 → divided 20 == 20
        # within tolerance; as_is 240 nowhere near. Must pick divided via answer key.
        r = nz.resolve_base_price("240/กล่อง", existing_base=20.0, cost=10.0,
                                  unit_type="ตัว", uc_ratios={"กล่อง": 12})
        assert abs(r["base_sell_price"] - 20.0) < 1e-6

    def test_pack_unit_no_conversion_still_blanks(self):
        # No unit_conversions row for ถุง and not canonical → ratio None → can't
        # divide; as_is 240 out of cost band → blank + note (correct, unchanged).
        r = nz.resolve_base_price("240/ถุง", existing_base=0.0, cost=20.0,
                                  unit_type="ตัว", uc_ratios={})
        assert r["base_sell_price"] is None
        assert r["normalize_notes"]

    def test_pack_unit_conversion_flows_through_run(self, tmp_path):
        # End-to-end: a unit_conversions row in the DB must reach the resolver
        # (the path the audit worried was skipped for pack units).
        in_csv = tmp_path / "raw.csv"
        out_csv = tmp_path / "norm.csv"
        in_csv.write_text(
            "product_id,sku,sku_code,product_name,base_price,ราคาพิเศษ,โปรโมชั่น,Remark\n"
            "50,A,SKU-A,nameA,240/กล่อง,,,\n",
            encoding="utf-8")
        db_path = _make_answer_key_db(
            tmp_path, {50: (0.0, 18.0, "ตัว")}, uc_rows=[(50, "กล่อง", 12)])
        out_rows = nz.run(in_csv, out_csv, db_path, verbose=False)
        assert out_rows[0]["base_sell_price"] == "20.0"


# ── helper: build a minimal read-only answer-key DB ─────────────────────────

def _make_answer_key_db(tmp_path, products, uc_rows=None):
    """products: {pid: (base_sell_price, cost_price, unit_type)}.
    uc_rows: optional list of (pid, bsn_unit, ratio)."""
    import sqlite3
    db = tmp_path / "answer.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE products (id INTEGER PRIMARY KEY, base_sell_price REAL, "
        "cost_price REAL, unit_type TEXT)")
    conn.execute(
        "CREATE TABLE unit_conversions (id INTEGER PRIMARY KEY, product_id INTEGER, "
        "bsn_unit TEXT, ratio REAL)")
    for pid, (base, cost, ut) in products.items():
        conn.execute("INSERT INTO products (id, base_sell_price, cost_price, unit_type) "
                     "VALUES (?,?,?,?)", (pid, base, cost, ut))
    for pid, unit, ratio in (uc_rows or []):
        conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) "
                     "VALUES (?,?,?)", (pid, unit, ratio))
    conn.commit()
    conn.close()
    return db
