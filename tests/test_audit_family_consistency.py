"""Tests for scripts/audit_family_consistency.py (Phase 1 of the
product-naming-round2 project — read-only family-consistency detector).

Fixture cases mirror the plan's seed findings so a detector bug surfaces
here before the full-DB run: the ถุงหิ้ว two-pattern family (pids 683-687 vs
1369-1374), the บานพับผีเสื้อ twin (pid 96 / pid 1888, shared bsn_code
030บ4000), a canonical-name collision, and the pid 686 brand_id anomaly.
"""
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import audit_family_consistency as afc  # noqa: E402

COLOR_CODES = {"AC": "สีรมดำ", "SS": "สีเงิน-สแตนเลส", "GP": "สีทองเคลือบ"}


# ---------------------------------------------------------------------------
# Structural signature primitives
# ---------------------------------------------------------------------------

class TestHasUnitSize:
    def test_detects_in_suffix(self):
        assert afc.has_unit_size("ถุงหิ้ว ข้าวสาลี 8x16in คละสี") is True

    def test_no_unit_is_false(self):
        assert afc.has_unit_size("ถุงหิ้วคละสี ข้าวสาลี 6x11") is False


class TestHasBareSize:
    def test_bare_dimension_detected(self):
        assert afc.has_bare_size("ถุงหิ้วคละสี ข้าวสาลี 6x11") is True

    def test_unit_suffixed_dimension_not_bare(self):
        # '16in' fused — the trailing \b in _BARE_DIM_RE must NOT match here
        assert afc.has_bare_size("ถุงหิ้ว ข้าวสาลี 8x16in คละสี") is False

    def test_rivet_dash_size_not_bare(self):
        # rule 23 bare-size rivet notation ('4-2') uses '-' not 'x' — not a
        # naming bug, must not be flagged as a bare WxH dimension.
        assert afc.has_bare_size("รีเวท 4-2") is False


class TestColorRepr:
    def test_coded(self):
        assert afc.color_repr("บานพับ Sendai #170-3in สีเงิน-สแตนเลส (SS)", COLOR_CODES) == "coded"

    def test_bare(self):
        # 'สี' + word glued together (Thai convention) with a space boundary
        # right after — matches the explicit 'สี<word>' prefix mode.
        assert afc.color_repr("ไม้กวาดสีเขียว 12in", COLOR_CODES) == "bare"

    def test_glued_inside_unrelated_word_not_matched(self):
        # 'ดำ' glued inside 'ขยะดำ' with no boundary before/after and no
        # 'สี' prefix — must NOT match (mirrors round-1's strict-boundary
        # design that keeps 'ทอง' inside 'ใบโพธิ์ทอง' from false-positiving).
        assert afc.color_repr("ถุงขยะดำหนา ตราพญาช้าง 36x45", COLOR_CODES) == "none"

    def test_none(self):
        assert afc.color_repr("ถุงหิ้ว ข้าวสาลี 8x16in คละสี", COLOR_CODES) == "none"


class TestClassifyDivergence:
    def test_size_axis_only_is_size_format(self):
        row_sig = (False, False, True, "none")       # bare size
        majority_sig = (False, True, False, "none")  # unit size
        assert afc.classify_divergence(row_sig, majority_sig) == "size_format"

    def test_missing_model_is_identity_missing(self):
        row_sig = (False, True, False, "none")
        majority_sig = (True, True, False, "none")
        assert afc.classify_divergence(row_sig, majority_sig) == "identity_missing"

    def test_missing_color_is_identity_missing(self):
        row_sig = (True, True, False, "none")
        majority_sig = (True, True, False, "coded")
        assert afc.classify_divergence(row_sig, majority_sig) == "identity_missing"


# ---------------------------------------------------------------------------
# Leading-token clustering
# ---------------------------------------------------------------------------

class TestSharesPrefix:
    def test_glued_qualifier_shares_prefix(self):
        assert afc.shares_prefix("ถุงหิ้วคละสี", "ถุงหิ้ว") is True

    def test_unrelated_words_do_not_share_prefix(self):
        assert afc.shares_prefix("น็อตหัวจม", "บานพับ") is False

    def test_short_prefix_below_threshold_rejected(self):
        assert afc.shares_prefix("ก", "กลอน", min_prefix=4) is False


# ---------------------------------------------------------------------------
# Seed case 1: ถุงหิ้ว two-pattern family (pids 683/684/687 vs 1373/1374,
# brand 43 ข้าวสาลี / Wheat)
# ---------------------------------------------------------------------------

def _thung_hiew_rows():
    return [
        {"id": 683, "product_name": "ถุงหิ้วคละสี ข้าวสาลี 6x11", "category_id": 25, "brand_id": 43},
        {"id": 684, "product_name": "ถุงหิ้วคละสี ข้าวสาลี 6x14", "category_id": 25, "brand_id": 43},
        {"id": 687, "product_name": "ถุงหิ้วคละสี ข้าวสาลี 12x26", "category_id": 25, "brand_id": 43},
        {"id": 1373, "product_name": "ถุงหิ้ว ข้าวสาลี 8x16in คละสี", "category_id": 25, "brand_id": 43},
        {"id": 1374, "product_name": "ถุงหิ้ว ข้าวสาลี 9x18in คละสี", "category_id": 25, "brand_id": 43},
    ]


class TestFindDivergentFamilies:
    def test_thung_hiew_family_is_divergent(self):
        families = afc.find_divergent_families(_thung_hiew_rows(), COLOR_CODES)
        assert len(families) == 1
        fam = families[0]
        assert fam["category_id"] == 25 and fam["brand_id"] == 43
        assert {r["id"] for r in fam["rows"]} == {683, 684, 687, 1373, 1374}
        # Old-style (683/684/687, bare size) OUTNUMBERS new-style (1373/1374,
        # unit-bearing size) 3-to-2, but the canonical pick must still be the
        # unit-bearing pattern — rule 7/8 mandates the unit, count doesn't
        # override the rule (see test_unit_size_wins_over_bare_majority).
        assert fam["majority_signature"] == afc.structural_signature(
            "ถุงหิ้ว ข้าวสาลี 8x16in คละสี", COLOR_CODES)


    def test_uniform_family_is_not_divergent(self):
        rows = [
            {"id": 1, "product_name": "ถุงหิ้ว ข้าวสาลี 8x16in คละสี", "category_id": 25, "brand_id": 43},
            {"id": 2, "product_name": "ถุงหิ้ว ข้าวสาลี 9x18in คละสี", "category_id": 25, "brand_id": 43},
        ]
        assert afc.find_divergent_families(rows, COLOR_CODES) == []


class TestPickCanonicalSignature:
    def test_unit_size_wins_over_bare_majority(self):
        # 3 bare-size rows vs 2 unit-size rows — unit-bearing must still win.
        from collections import Counter
        bare_sig = (False, False, True, "none")
        unit_sig = (False, True, False, "none")
        counts = Counter({bare_sig: 3, unit_sig: 2})
        assert afc._pick_canonical_signature(counts) == unit_sig

    def test_falls_back_to_plain_majority_when_no_unit_axis_involved(self):
        from collections import Counter
        sig_a = (True, True, False, "coded")
        sig_b = (False, True, False, "coded")
        counts = Counter({sig_a: 5, sig_b: 1})
        assert afc._pick_canonical_signature(counts) == sig_a


class TestProposeMechanicalFix:
    def test_rebuilds_canonical_template(self):
        proposed, kind = afc.propose_mechanical_fix(
            "ถุงหิ้วคละสี ข้าวสาลี 6x11", "ถุงหิ้ว", "ข้าวสาลี", default_unit="in")
        assert kind == "mechanical_auto"
        assert proposed == "ถุงหิ้ว ข้าวสาลี 6x11in คละสี"

    def test_returns_none_when_already_unit_bearing(self):
        proposed, kind = afc.propose_mechanical_fix(
            "ถุงหิ้ว ข้าวสาลี 8x16in คละสี", "ถุงหิ้ว", "ข้าวสาลี")
        assert proposed is None
        assert kind == "needs_manual_family_review"

    def test_fraction_spec_not_mangled(self):
        # 'ดจ.ปูน STAR 1/4x4' is a '1/4in x 4in' drill-bit fraction spec.
        # _BARE_DIM_RE only matches the '4x4' tail, stranding '1/' — the
        # function must bail (not emit 'ดจ.ปูน STAR 4x4in 1/'), caught by
        # the full-DB run on 2026-07-21.
        proposed, kind = afc.propose_mechanical_fix(
            "ดจ.ปูน STAR 1/4x4", "ดจ.ปูน", "STAR")
        assert proposed is None
        assert kind == "needs_manual_family_review"


# ---------------------------------------------------------------------------
# Seed case 2: บานพับผีเสื้อ twin (pid 96 แผง / pid 1888 ตัว, bsn_code 030บ4000)
# ---------------------------------------------------------------------------

class TestTwinAlignment:
    def test_misaligned_pair_detected(self):
        entries = [
            {"product_id": 96, "bsn_unit": "แผง",
             "product_name": "บานพับผีเสื้อสแตนเลส Sendai #SUS304-4inx3inx2mm (แผง)"},
            {"product_id": 1888, "bsn_unit": "ตัว",
             "product_name": "บานพับผีเสื้อสแตนเลส 4in"},
        ]
        stripped = {afc.strip_packaging_bracket(e["product_name"]) for e in entries}
        assert len(stripped) == 2  # misaligned

    def test_spec_inheritance_direction_favors_richer_row(self):
        entries = [
            {"product_id": 96, "bsn_unit": "แผง",
             "product_name": "บานพับผีเสื้อสแตนเลส Sendai #SUS304-4inx3inx2mm (แผง)"},
            {"product_id": 1888, "bsn_unit": "ตัว",
             "product_name": "บานพับผีเสื้อสแตนเลส 4in"},
        ]
        proposal = afc.propose_twin_inheritance(entries)
        assert proposal == {1888: "บานพับผีเสื้อสแตนเลส Sendai #SUS304-4inx3inx2mm (ตัว)"}

    def test_aligned_pair_gets_no_proposal_needed(self):
        entries = [
            {"product_id": 1397, "bsn_unit": "แผง",
             "product_name": "บานพับหัวเรียบ Sendai #500 สีรมดำ (AC) (แผง)"},
            {"product_id": 2033, "bsn_unit": "ตัว",
             "product_name": "บานพับหัวเรียบ Sendai #500 สีรมดำ (AC) (ตัว)"},
        ]
        stripped = {afc.strip_packaging_bracket(e["product_name"]) for e in entries}
        assert len(stripped) == 1  # already aligned

    def test_richness_tie_returns_no_forced_direction(self):
        entries = [
            {"product_id": 81, "bsn_unit": "แผง",
             "product_name": "บานพับ Sendai #412 สีทองเคลือบ (GP) (แผง)"},
            {"product_id": 111, "bsn_unit": "ตัว",
             "product_name": "บานพับสีทอง Sendai #412 สีทองเคลือบ (GP) (ตัว)"},
        ]
        # both have model + no unit-size + color-coded bracket -> tied richness
        # (the extra 'สีทอง' glued onto pid 111's category is redundant text,
        # not more structural info, so it must NOT break the tie)
        assert afc.name_richness(entries[0]["product_name"]) == \
            afc.name_richness(entries[1]["product_name"])
        proposal = afc.propose_twin_inheritance(entries)
        assert proposal == {}


class TestFindTwins:
    def test_find_twins_from_db_rows(self, empty_db_conn):
        conn = empty_db_conn
        conn.execute("INSERT INTO brands (id, code, name, name_th, short_code) VALUES (3,'SD','Sendai','เซ็นได','SD')")
        conn.execute("INSERT INTO categories (id, code, name_th, short_code) VALUES (3,'HNG','บานพับ','HNG')")
        conn.execute("""INSERT INTO products (id, product_name, is_active, brand_id, category_id)
                         VALUES (96, 'บานพับผีเสื้อสแตนเลส Sendai #SUS304-4inx3inx2mm (แผง)', 1, 3, 3)""")
        conn.execute("""INSERT INTO products (id, product_name, is_active, brand_id, category_id)
                         VALUES (1888, 'บานพับผีเสื้อสแตนเลส 4in', 1, 3, 3)""")
        conn.execute("""INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id, bsn_unit)
                         VALUES ('030บ4000', 'บานพับผีเสื้อ สแตนเลส 4 นิ้ว', 96, 'แผง')""")
        conn.execute("""INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id, bsn_unit)
                         VALUES ('030บ4000', 'บานพับผีเสื้อ สแตนเลส 4 นิ้ว', 1888, 'ตัว')""")
        conn.commit()
        twins = afc.find_twins(conn)
        assert len(twins) == 1
        assert twins[0]["bsn_code"] == "030บ4000"
        assert twins[0]["aligned"] is False
        pids = {e["product_id"] for e in twins[0]["entries"]}
        assert pids == {96, 1888}


# ---------------------------------------------------------------------------
# Seed case 3: canonical-name collision
# ---------------------------------------------------------------------------

class TestFindCollisions:
    def test_duplicate_final_names_flagged(self):
        proposed = {1: "บานพับ Sendai #170-3in สแตนเลส", 2: "บานพับ Sendai #170-3in สแตนเลส", 3: "อื่นๆ"}
        collisions = afc.find_collisions(proposed)
        assert collisions == [{"name": "บานพับ Sendai #170-3in สแตนเลส", "product_ids": [1, 2]}]

    def test_no_collision_when_all_unique(self):
        proposed = {1: "a", 2: "b", 3: "c"}
        assert afc.find_collisions(proposed) == []


# ---------------------------------------------------------------------------
# Seed case 4: pid 686 generic-brand text anomaly
# ---------------------------------------------------------------------------

class TestGenericBrandTextAnomaly:
    def test_pid686_style_anomaly_detected(self):
        brand_lookup = {
            13: {"id": 13, "name": "Other", "name_th": "ทั่วไป"},
            43: {"id": 43, "name": "Wheat", "name_th": "ข้าวสาลี"},
        }
        products = [
            {"id": 683, "product_name": "ถุงหิ้วคละสี ข้าวสาลี 6x11", "category_id": 25, "brand_id": 43},
            {"id": 684, "product_name": "ถุงหิ้วคละสี ข้าวสาลี 6x14", "category_id": 25, "brand_id": 43},
            {"id": 686, "product_name": "ถุงหิ้วคละสี ข้าวสาลี 12x20", "category_id": 25, "brand_id": 13},
        ]
        anomalies = afc.find_generic_brand_text_anomalies(products, brand_lookup)
        assert len(anomalies) == 1
        a = anomalies[0]
        assert a["product_id"] == 686
        assert a["current_brand_id"] == 13
        assert a["detected_brand_id"] == 43
        assert a["sibling_count"] == 2

    def test_no_corroborating_siblings_no_anomaly(self):
        # coincidental substring match with zero real siblings in the
        # category shouldn't be flagged (guards against a fluke text hit).
        brand_lookup = {
            13: {"id": 13, "name": "Other", "name_th": "ทั่วไป"},
            99: {"id": 99, "name": "Ghost", "name_th": "ผี"},
        }
        products = [
            {"id": 1, "product_name": "ตะปูผีเสื้อ", "category_id": 5, "brand_id": 13},
        ]
        assert afc.find_generic_brand_text_anomalies(products, brand_lookup) == []
