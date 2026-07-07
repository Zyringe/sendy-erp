"""Tests for scripts/audit_product_naming.py checker functions (Phase 1 of the
product-naming-audit project — read-only audit engine, no DB writes).

Covers the rule-doc before/after examples relevant to each implemented tier-A
fix, plus the edge cases named in projects/product-naming-audit/plan.md:
bundle `(+)`/`(-)` markers, Golden Lion กล่องสี series, bare rivet sizes
(`4-2`), EXP:MM/YYYY condition, and hand-tuned names that diverge from
build() (must NOT be flagged for divergence alone).
"""
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import audit_product_naming as apn  # noqa: E402


# ---------------------------------------------------------------------------
# Tier A: mechanical text fixes
# ---------------------------------------------------------------------------

class TestFixInchToIn:
    def test_quote(self):
        assert apn.fix_inch_to_in('ฉากวัดไม้ META #10"') == 'ฉากวัดไม้ META #10in'

    def test_spaced_niu(self):
        assert apn.fix_inch_to_in('บานพับสแตนเลส #170 Sendai 3 นิ้ว') == \
            'บานพับสแตนเลส #170 Sendai 3in'

    def test_bare_niu(self):
        assert apn.fix_inch_to_in('กลอนพฤกษา Sendai #260-4นิ้ว สีรมดำ (AC)') == \
            'กลอนพฤกษา Sendai #260-4in สีรมดำ (AC)'

    def test_space_fraction(self):
        assert apn.fix_inch_to_in('ไขควงสลับ META หัวโต 1 1/2นิ้ว') == \
            'ไขควงสลับ META หัวโต 1.5in'

    def test_dot_fraction(self):
        assert apn.fix_inch_to_in('ไขควงสลับ META หัวโต 1.1/2นิ้ว') == \
            'ไขควงสลับ META หัวโต 1.5in'

    def test_multi_segment(self):
        assert apn.fix_inch_to_in('บานพับ #2543-4นิ้วx3นิ้วx2.5mm') == \
            'บานพับ #2543-4inx3inx2.5mm'

    def test_already_canonical_is_noop(self):
        assert apn.fix_inch_to_in('กลอนพฤกษา Sendai #260-4in') == \
            'กลอนพฤกษา Sendai #260-4in'

    def test_does_not_touch_bare_rivet_size(self):
        # '4-2' is a recognized bare rivet size (rule 23), not an inch token.
        assert apn.fix_inch_to_in('รีเวท 4-2') == 'รีเวท 4-2'


class TestFixMmCmFormat:
    def test_mm_with_dot_and_space(self):
        assert apn.fix_mm_cm_format('มือจับ #1780-120 mm.') == 'มือจับ #1780-120mm'

    def test_cm_uppercase(self):
        assert apn.fix_mm_cm_format('กรอบจตุคาม (P) 5 CM. สีทอง') == \
            'กรอบจตุคาม (P) 5cm สีทอง'

    def test_mil_to_mm(self):
        assert apn.fix_mm_cm_format('มือจับ 50 มิล') == 'มือจับ 50mm'

    def test_already_canonical_is_noop(self):
        assert apn.fix_mm_cm_format('มือจับ #1780-94mm') == 'มือจับ #1780-94mm'


class TestFixHashPrefix:
    def test_bare_model_letters_digits(self):
        assert apn.fix_hash_prefix('ลูกบิด Sendai SD9951') == 'ลูกบิด Sendai #SD9951'

    def test_bare_model_with_variant_suffix(self):
        assert apn.fix_hash_prefix('บานพับ HL9991-2') == 'บานพับ #HL9991-2'

    def test_already_hashed_is_noop(self):
        assert apn.fix_hash_prefix('บานพับ Sendai #170') == 'บานพับ Sendai #170'

    def test_no_bare_model_present_is_noop(self):
        assert apn.fix_hash_prefix('ไขควงสลับ META 3in') == 'ไขควงสลับ META 3in'


class TestFixHashSpace:
    def test_space_after_hash(self):
        assert apn.fix_hash_space('# HL316') == '#HL316'

    def test_space_inside_letters_digits(self):
        assert apn.fix_hash_space('#HL 9991-2') == '#HL9991-2'

    def test_no_space_is_noop(self):
        assert apn.fix_hash_space('#HL316') == '#HL316'


class TestFixPackagingLegacy:
    def test_upper_p(self):
        assert apn.fix_packaging_legacy('ลูกบิด Sendai (P)#5112 SB') == \
            'ลูกบิด Sendai (แผง)#5112 SB'

    def test_lower_p(self):
        assert apn.fix_packaging_legacy('บานพับ(p)') == 'บานพับ(แผง)'

    def test_no_p_is_noop(self):
        assert apn.fix_packaging_legacy('บานพับ (แผง)') == 'บานพับ (แผง)'


class TestFixRunPrefixStrip:
    def test_parenthesized_run_prefix(self):
        assert apn.fix_run_prefix('บานพับ (รุ่นแผง)') == 'บานพับ (แผง)'

    def test_bare_run_packaging_promoted(self):
        assert apn.fix_run_prefix('บานพับ รุ่นแผง') == 'บานพับ (แผง)'

    def test_bare_run_series_token_stripped(self):
        assert apn.fix_run_prefix('กันชนสแตนเลส รุ่นTOP') == 'กันชนสแตนเลส TOP'

    def test_no_run_is_noop(self):
        assert apn.fix_run_prefix('บานพับ Sendai #170') == 'บานพับ Sendai #170'


class TestFixAnnotationStrip:
    def test_strips_has_barcode(self):
        assert apn.fix_annotation_strip('บานพับ Sendai #170 (มีบาโค้ต)') == \
            'บานพับ Sendai #170'

    def test_strips_no_barcode_variant(self):
        assert apn.fix_annotation_strip('บานพับ Sendai #170 (ไม่มีบาโค้ต)') == \
            'บานพับ Sendai #170'

    def test_no_annotation_is_noop(self):
        assert apn.fix_annotation_strip('บานพับ Sendai #170') == 'บานพับ Sendai #170'

    def test_does_not_strip_exp_condition(self):
        # EXP:MM/YYYY is a real condition note, not a stray annotation.
        name = 'กาวซิลิโคน Sendai (หมดอายุ) (EXP:07/2027)'
        assert apn.fix_annotation_strip(name) == name


class TestFixBracketOrder:
    def test_condition_before_packaging_gets_swapped(self):
        assert apn.fix_bracket_order('กลอน Sendai #230 (เก่า) (แผง)') == \
            'กลอน Sendai #230 (แผง) (เก่า)'

    def test_already_correct_order_is_noop(self):
        name = 'กลอน Sendai #230 (แผง) (เก่า)'
        assert apn.fix_bracket_order(name) == name

    def test_no_condition_is_noop(self):
        name = 'กลอน Sendai #230 (แผง)'
        assert apn.fix_bracket_order(name) == name


class TestFixPackVariantSuffix:
    def test_trailing_single_digit_stripped(self):
        assert apn.fix_pack_variant_suffix('กลอนมะยม Sendai #230-4in AC 1') == \
            'กลอนมะยม Sendai #230-4in AC'

    def test_trailing_digit_after_bracket_stripped(self):
        name = 'บานพับสแตนเลส Sendai #2543-4inx3inx2.5mm สีทองแดงรมดำ (JBB) (แผง) 2'
        expected = 'บานพับสแตนเลส Sendai #2543-4inx3inx2.5mm สีทองแดงรมดำ (JBB) (แผง)'
        assert apn.fix_pack_variant_suffix(name) == expected

    def test_bare_rivet_size_not_stripped(self):
        # '4-2' is a bare rivet size (rule 23) — must NOT be mistaken for a
        # trailing pack-variant digit.
        assert apn.fix_pack_variant_suffix('รีเวท 4-2') == 'รีเวท 4-2'

    def test_no_trailing_digit_is_noop(self):
        name = 'บานพับ Sendai #170 สแตนเลส'
        assert apn.fix_pack_variant_suffix(name) == name

    def test_two_digit_product_code_suffix_not_stripped(self):
        # pid 570 real case: 'TH 18' is a product code (กาวร้อน TH-18), not a
        # pack-variant digit — two-digit trailing numbers must be left alone.
        assert apn.fix_pack_variant_suffix('กาวร้อน TH 18') == 'กาวร้อน TH 18'

    def test_size_without_unit_not_stripped(self):
        # pid 1192-1194 real case: 'META 8'/'META 10'/'META 12' are plier
        # sizes with the unit omitted, not pack variants.
        assert apn.fix_pack_variant_suffix('คีมคอม้าปากขยับได้ META 10') == \
            'คีมคอม้าปากขยับได้ META 10'
        assert apn.fix_pack_variant_suffix('คีมคอม้าปากขยับได้ META 12') == \
            'คีมคอม้าปากขยับได้ META 12'


class TestFixDoubleSpace:
    def test_collapses_double_space(self):
        assert apn.fix_double_space('บานพับ  Sendai') == 'บานพับ Sendai'

    def test_single_space_is_noop(self):
        assert apn.fix_double_space('บานพับ Sendai') == 'บานพับ Sendai'


class TestFixTypoCurated:
    def test_satanless_typo(self):
        assert apn.fix_typo_curated('บานพับสแตนแลส Sendai') == 'บานพับสแตนเลส Sendai'

    def test_chromium_typo(self):
        assert apn.fix_typo_curated('สายยูประกบ สีโครเมี่ยม') == 'สายยูประกบ สีโครเมียม'

    def test_sataen_variant(self):
        assert apn.fix_typo_curated('ดจ.แสตนเลส META') == 'ดจ.แสตนเลส META'.replace(
            'แสตนเลส', 'สแตนเลส')

    def test_not_variant(self):
        assert apn.fix_typo_curated('น๊อตตัวผู้') == 'น็อตตัวผู้'

    def test_puk_to_puk_canonical(self):
        assert apn.fix_typo_curated('ปุ๊กตะกั่ว 1/4"') == 'พุกตะกั่ว 1/4"'

    def test_no_typo_is_noop(self):
        assert apn.fix_typo_curated('บานพับสแตนเลส Sendai') == 'บานพับสแตนเลส Sendai'


class TestBundleAndSeriesNotFlagged:
    """Rule-24 bundle markers and Golden Lion กล่องสี series must not trip
    the tier-A checks meant for unrelated patterns."""

    def test_bundle_plus_marker_no_findings(self):
        name = 'ดอกไขควงลม 6x65 mm (+)(+) META'
        findings = apn.mechanical_findings(name)
        classes = {c for c, _, _ in findings}
        assert 'PACK_VARIANT_SUFFIX' not in classes
        assert 'BRACKET_ORDER' not in classes

    def test_screwdriver_head_marker_no_hash_prefix(self):
        name = 'ไขควงด้ามใสสีดำ-น้ำเงิน หัวเดียว 6in (+)'
        findings = apn.mechanical_findings(name)
        classes = {c for c, _, _ in findings}
        assert 'HASH_PREFIX' not in classes

    def test_golden_lion_series_name_is_clean(self):
        name = 'ดจ.สแตนเลส Golden Lion กล่องน้ำเงิน 3/16นิ้ว'
        # Only the inch-normalization class should fire; series text untouched.
        findings = apn.mechanical_findings(name)
        for cls, proposed, _ in findings:
            assert 'กล่องน้ำเงิน' in proposed


class TestMechanicalFindings:
    def test_clean_name_has_no_findings(self):
        assert apn.mechanical_findings('กรรไกรตัดกิ่ง META #S-101') == []

    def test_multiple_classes_each_reported_independently(self):
        name = 'บานพับ  Sendai(P)#170 3 นิ้ว'
        findings = apn.mechanical_findings(name)
        classes = {c for c, _, _ in findings}
        assert 'DOUBLE_SPACE' in classes
        assert 'PACKAGING_LEGACY' in classes
        assert 'INCH_TO_IN' in classes


# ---------------------------------------------------------------------------
# Tier B: name <-> structured field mismatch
# ---------------------------------------------------------------------------

BRAND_LOOKUP = {
    6:  {"id": 6,  "name": "TOA",       "name_th": "จระเข้", "short_code": "TOA"},
    44: {"id": 44, "name": "Crocodile", "name_th": "จระเข้", "short_code": "CROC"},
    1:  {"id": 1,  "name": "Sendai",    "name_th": "เซ็นได", "short_code": "SD"},
}

COLOR_LOOKUP = {
    "AB": "สีทองเหลืองรมดำ",
    "BN": "สีน้ำตาลเข้ม",
    "AC": "สีรมดำ",
}


class TestCheckBrandMismatch:
    def test_brand_present_in_name_no_mismatch(self):
        row = {"product_name": "บานพับ Sendai #170", "brand_id": 1}
        assert apn.check_brand_mismatch(row, BRAND_LOOKUP) is None

    def test_brand_id_set_but_absent_from_name(self):
        row = {"product_name": "บานพับ #170", "brand_id": 1}
        assert apn.check_brand_mismatch(row, BRAND_LOOKUP) is not None

    def test_brand_id_null_but_name_has_token(self):
        row = {"product_name": "จารบี TOA #306-1kg", "brand_id": None}
        result = apn.check_brand_mismatch(row, BRAND_LOOKUP)
        assert result is not None
        assert "TOA" in result

    def test_brand_id_null_and_no_token_no_mismatch(self):
        row = {"product_name": "กรรไกรตัดกิ่ง #S-101", "brand_id": None}
        assert apn.check_brand_mismatch(row, BRAND_LOOKUP) is None

    def test_generic_other_bucket_not_flagged(self):
        # Real DB case: brand_id=13 'Other'/'ทั่วไป' (62 products) — rule doc
        # says the REAL 3rd-party name goes in product_name, not 'Other'.
        lookup = {13: {"id": 13, "name": "Other", "name_th": "ทั่วไป", "short_code": "3RD"}}
        row = {"product_name": "น้ำยาสเตดฟาส Chaindrite 1000cc", "brand_id": 13}
        assert apn.check_brand_mismatch(row, lookup) is None

    def test_generic_no_name_bucket_not_flagged(self):
        lookup = {15: {"id": 15, "name": "No Name", "name_th": "", "short_code": "NN"}}
        row = {"product_name": "น็อตตัวผู้ 3mm", "brand_id": 15}
        assert apn.check_brand_mismatch(row, lookup) is None


class TestCheckColorMismatch:
    def test_bracketed_code_matches_no_mismatch(self):
        row = {"product_name": "ลูกบิดรมดำ BEYOND #5794 สีน้ำตาลเข้ม (BN)",
               "color_code": "BN"}
        assert apn.check_color_mismatch(row, COLOR_LOOKUP) is None

    def test_code_embedded_in_model_is_mismatch(self):
        # pid 1768 real case: color_code='AB' but 'AB' is part of model #118AB,
        # not a standalone color token.
        row = {"product_name": "ใบเลื่อยจิ๊กซอตัดเหล็ก #118AB", "color_code": "AB"}
        assert apn.check_color_mismatch(row, COLOR_LOOKUP) is not None

    def test_no_color_code_no_mismatch(self):
        row = {"product_name": "กรรไกรตัดกิ่ง META #S-101", "color_code": None}
        assert apn.check_color_mismatch(row, COLOR_LOOKUP) is None

    def test_bare_code_word_boundary_matches(self):
        row = {"product_name": "สายยูประกบ Sendai 3mm AC", "color_code": "AC"}
        assert apn.check_color_mismatch(row, COLOR_LOOKUP) is None

    def test_bare_thai_name_th_matches_no_mismatch(self):
        # Real-DB case: rule 9's primary form is the Thai word, often with NO
        # bracketed code at all — must count as full support, not a mismatch.
        row = {"product_name": "รีเวทแผง Sendai 4-6 สีขาว", "color_code": "WHT"}
        lookup = {"WHT": "สีขาว"}
        assert apn.check_color_mismatch(row, lookup) is None


class TestCheckPackagingMismatch:
    def test_matching_packaging_no_mismatch(self):
        row = {"packaging_th": "แผง"}
        parsed = {"packaging": "แผง"}
        assert apn.check_packaging_mismatch(row, parsed) is None

    def test_conflicting_packaging_is_mismatch(self):
        row = {"packaging_th": "แผง"}
        parsed = {"packaging": "ตัว"}
        assert apn.check_packaging_mismatch(row, parsed) is not None

    def test_name_omits_packaging_no_mismatch(self):
        # Rule doc edge case: single-packaging products may omit the bracket.
        row = {"packaging_th": "ตัว"}
        parsed = {"packaging": ""}
        assert apn.check_packaging_mismatch(row, parsed) is None


class TestCheckModelMismatch:
    def test_matching_model_no_mismatch(self):
        row = {"model": "#230"}
        parsed = {"model": "#230"}
        assert apn.check_model_mismatch(row, parsed) is None

    def test_hash_and_case_insensitive_no_mismatch(self):
        row = {"model": "#SD9951"}
        parsed = {"model": "sd9951"}
        assert apn.check_model_mismatch(row, parsed) is None

    def test_conflicting_model_is_mismatch(self):
        row = {"model": "#230"}
        parsed = {"model": "#260"}
        assert apn.check_model_mismatch(row, parsed) is not None

    def test_model_plus_size_fused_no_mismatch(self):
        # Real-DB case: rule 6 joins model+size with '-' no space, so the
        # parser's model token naturally includes the size ('#306-0.5kg')
        # while the stored column is just the model ('#306').
        row = {"model": "#306"}
        parsed = {"model": "#306-0.5kg"}
        assert apn.check_model_mismatch(row, parsed) is None

    def test_model_prefix_collision_still_flagged(self):
        # Guard: a short model that's merely a PREFIX of an unrelated one
        # must still be flagged (e.g. '#2' vs '#23xyz' sharing no separator).
        row = {"model": "#2"}
        parsed = {"model": "#230"}
        assert apn.check_model_mismatch(row, parsed) is not None

    def test_parsed_has_no_model_no_mismatch(self):
        row = {"model": "#230"}
        parsed = {"model": ""}
        assert apn.check_model_mismatch(row, parsed) is None


class TestCheckSeriesMismatch:
    def test_series_present_in_name_no_mismatch(self):
        row = {"product_name": "ดจ.สแตนเลส Golden Lion กล่องน้ำเงิน 3/16นิ้ว",
               "series": "กล่องน้ำเงิน"}
        assert apn.check_series_mismatch(row) is None

    def test_series_absent_from_name_is_mismatch(self):
        row = {"product_name": "บานพับ Sendai #410", "series": "JAC"}
        assert apn.check_series_mismatch(row) is not None

    def test_no_series_no_mismatch(self):
        row = {"product_name": "บานพับ Sendai #410", "series": ""}
        assert apn.check_series_mismatch(row) is None

    def test_underscore_stored_series_matches_spaced_name(self):
        # Real-DB case: ~200 products store series with '_' joining tokens
        # ('NEW_TOP') while the name itself uses spaces ('NEW TOP') — a
        # stored-value formatting quirk, not a real conflict.
        row = {"product_name": "บานพับหน้าต่าง8 NEW TOP สีรมดำ (AC) (แผง)",
               "series": "NEW_TOP"}
        assert apn.check_series_mismatch(row) is None


class TestHandTunedDivergenceNotFlagged:
    """~42% of names intentionally diverge from build() — divergence from a
    full rebuild is NOT itself a defect (naming_cascade.py docstring). None of
    the mismatch checks should be full-name-vs-build() comparisons."""

    def test_hand_tuned_name_no_field_conflict(self):
        # No brand token, no '(CODE)' — but every structured field it DOES
        # carry is genuinely consistent, so no mismatch should fire.
        row = {"product_name": "บานพับใบโพธิ์ทอง #410", "brand_id": None,
               "color_code": None, "model": "#410", "series": None,
               "packaging_th": None}
        parsed = {"model": "#410", "packaging": ""}
        assert apn.check_brand_mismatch(row, BRAND_LOOKUP) is None
        assert apn.check_color_mismatch(row, COLOR_LOOKUP) is None
        assert apn.check_model_mismatch(row, parsed) is None
        assert apn.check_packaging_mismatch(row, parsed) is None
        assert apn.check_series_mismatch(row) is None


# ---------------------------------------------------------------------------
# Dictionary-level checks (color_finish_codes / brands) — generic + testable
# ---------------------------------------------------------------------------

class TestFindColorDictIssues:
    def test_duplicate_names_detected(self):
        rows = [("JSN", "สีนิกเกิล"), ("NK", "สีนิกเกิล"), ("AC", "สีรมดำ")]
        result = apn.find_color_dict_issues(rows)
        codes = {tuple(sorted(g["codes"])) for g in result["duplicate_names"]}
        assert ("JSN", "NK") in codes

    def test_combo_conflict_detected_for_bn_pb(self):
        rows = [
            ("BN", "สีน้ำตาลเข้ม"),
            ("PB", "สีทองเงา"),
            ("NK", "สีนิกเกิล"),
            ("JSN", "สีนิกเกิล"),
            ("BN/PB", "สีนิกเกิล/ทองเงา"),
        ]
        result = apn.find_color_dict_issues(rows)
        conflicts = {c["combo_code"] for c in result["combo_conflicts"]}
        assert "BN/PB" in conflicts

    def test_combo_using_own_meaning_not_flagged(self):
        rows = [
            ("BN", "สีน้ำตาลเข้ม"),
            ("AC", "สีรมดำ"),
            ("BN/AC", "สีน้ำตาลเข้ม-รมดำ"),
        ]
        result = apn.find_color_dict_issues(rows)
        assert result["combo_conflicts"] == []

    def test_shorthand_combo_not_falsely_flagged(self):
        # SB/PB abbreviates the shared 'ทอง' root after the slash — this is a
        # legitimate shorthand, not borrowed wording from another code, and
        # must not be flagged (no OTHER code owns the exact segment text).
        rows = [
            ("SB", "สีทองด้าน"),
            ("PB", "สีทองเงา"),
            ("SB/PB", "สีทองด้าน/เงา"),
        ]
        result = apn.find_color_dict_issues(rows)
        assert result["combo_conflicts"] == []


class TestFindBrandAliasConflicts:
    def test_alias_conflict_detected(self):
        brand_rows = [
            {"id": 6, "name": "TOA", "name_th": "จระเข้"},
            {"id": 44, "name": "Crocodile", "name_th": "จระเข้"},
        ]
        conflicts = apn.find_brand_alias_conflicts(brand_rows)
        ids = {c["brand_id"] for c in conflicts}
        assert 44 in ids
        assert 6 not in ids  # TOA is the canonical alias target, not flagged

    def test_no_alias_no_conflict(self):
        brand_rows = [{"id": 1, "name": "Sendai", "name_th": "เซ็นได"}]
        assert apn.find_brand_alias_conflicts(brand_rows) == []


# ---------------------------------------------------------------------------
# SKU drift + collision preview (group c)
# ---------------------------------------------------------------------------

class TestResolveCollisions:
    def test_no_collision_passthrough(self):
        computed = {1: "BLT-SD-#230", 2: "HNG-SD-#410"}
        active = {1: 1, 2: 1}
        result = apn.resolve_collisions(computed, active)
        assert result[1] == ("BLT-SD-#230", "")
        assert result[2] == ("HNG-SD-#410", "")

    def test_active_wins_over_inactive(self):
        computed = {10: "BLT-SD-#230", 20: "BLT-SD-#230"}
        active = {10: 0, 20: 1}
        result = apn.resolve_collisions(computed, active)
        assert result[20][0] == "BLT-SD-#230"
        assert result[10][0] == "BLT-SD-#230-10"

    def test_lower_pid_wins_when_same_status(self):
        computed = {5: "BLT-SD-#230", 9: "BLT-SD-#230"}
        active = {5: 1, 9: 1}
        result = apn.resolve_collisions(computed, active)
        assert result[5][0] == "BLT-SD-#230"
        assert result[9][0] == "BLT-SD-#230-9"


class TestCanonicalSkuForRow:
    def test_uses_build_sku_code(self):
        row = {"id": 1, "cat_short_code": "HNG", "brand_short_code": "SD",
               "model": "#410", "packaging_short": "PN"}
        assert apn.canonical_sku_for_row(row) == "HNG-SD-#410-PN"
