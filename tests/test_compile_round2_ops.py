"""Tests for scripts/compile_round2_ops.py (product-naming-round2 Phase 3
prep). Compiles APPROVED rows from the round-2 decision-stamped CSVs into the
strict ops format apply_product_naming.py consumes. Read-only w.r.t. the DB
(SELECTs only, for stale-before checks) — never writes.
"""
import csv
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import compile_round2_ops as cro  # noqa: E402


def write_csv(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    return str(path)


# ---------------------------------------------------------------------------
# detect_kind
# ---------------------------------------------------------------------------

def test_detect_kind_family_divergence():
    fields = ["family_key", "product_id", "is_active", "current_name",
              "proposed_name", "fix_kind", "note", "decision"]
    assert cro.detect_kind(fields) == "family_divergence"


def test_detect_kind_twins():
    fields = ["bsn_code", "product_id", "bsn_unit", "current_name", "aligned",
              "proposed_name", "decision"]
    assert cro.detect_kind(fields) == "twins"


def test_detect_kind_field_anomalies():
    fields = ["product_id", "current_name", "current_brand_id", "detected_brand_id",
              "detected_brand_name", "sibling_count", "decision"]
    assert cro.detect_kind(fields) == "field_anomalies"


def test_detect_kind_unstamped_when_no_decision_column():
    fields = ["product_id", "source_file", "recommend", "evidence"]
    assert cro.detect_kind(fields) == "unstamped"


def test_detect_kind_generic_mixed_when_both_shapes_present():
    """Round-2 fix ซ item 3: a single file carrying proposed_name AND
    field/value columns (the shape the main thread stamps ALL future bucket
    approvals in) is its own kind, resolved per-row by extract_op."""
    fields = ["product_id", "current_name", "proposed_name", "field", "value", "before", "decision"]
    assert cro.detect_kind(fields) == "generic_mixed"


# ---------------------------------------------------------------------------
# is_approved
# ---------------------------------------------------------------------------

def test_is_approved_variants():
    assert cro.is_approved("approved (batch1-a): rename per proposal (Put 2026-07-21)")
    assert cro.is_approved("Approved: x")  # case-insensitive
    assert not cro.is_approved("keep (batch1-e): already rule-correct")
    assert not cro.is_approved("rejected-keep (batch1-d): ...")
    assert not cro.is_approved("")
    assert not cro.is_approved("   ")


# ---------------------------------------------------------------------------
# extract_op — per kind
# ---------------------------------------------------------------------------

def test_extract_op_family_divergence_approved_row():
    row = {"family_key": "cat25-brand43-ถุงหิ้ว", "product_id": "683", "is_active": "1",
           "current_name": "ถุงหิ้วคละสี ข้าวสาลี 6x11",
           "proposed_name": "ถุงหิ้ว ข้าวสาลี 6x11in คละสี",
           "fix_kind": "mechanical_auto", "note": "x",
           "decision": "approved (batch1-a): rename per proposal (Put 2026-07-21)"}
    r = cro.extract_op("family_divergence", row, source_label="audit_family_divergence.csv")
    assert r.status == "approved"
    assert r.op == {
        "op": "name", "product_id": "683", "field": "", "value": "",
        "before": "ถุงหิ้วคละสี ข้าวสาลี 6x11", "after": "ถุงหิ้ว ข้าวสาลี 6x11in คละสี",
        "source": "audit_family_divergence.csv:cat25-brand43-ถุงหิ้ว",
    }


def test_extract_op_family_divergence_not_approved_is_skipped():
    row = {"family_key": "x", "product_id": "1", "current_name": "a", "proposed_name": "b",
           "fix_kind": "needs_manual_family_review", "note": "", "decision": ""}
    r = cro.extract_op("family_divergence", row, source_label="f.csv")
    assert r.status == "not_approved"
    assert r.op is None


def test_extract_op_approved_but_missing_proposed_name_fails_loud():
    row = {"family_key": "x", "product_id": "111", "current_name": "a", "proposed_name": "",
           "fix_kind": "needs_manual_family_review", "note": "",
           "decision": "approved (batch1-x): some correction described only in prose"}
    r = cro.extract_op("family_divergence", row, source_label="f.csv")
    assert r.status == "error"
    assert "111" in r.error_msg
    assert r.op is None


def test_extract_op_twins_approved_row():
    row = {"bsn_code": "030บ4000", "product_id": "1888", "bsn_unit": "ตัว",
           "current_name": "บานพับผีเสื้อสแตนเลส 4in", "aligned": "False",
           "proposed_name": "บานพับผีเสื้อสแตนเลส Sendai #SUS304-4inx3inx2mm (ตัว)",
           "decision": "approved (batch1-c): rename per proposal (Put 2026-07-21)"}
    r = cro.extract_op("twins", row, source_label="audit_twins.csv")
    assert r.status == "approved"
    assert r.op["op"] == "name"
    assert r.op["before"] == "บานพับผีเสื้อสแตนเลส 4in"
    assert r.op["after"] == "บานพับผีเสื้อสแตนเลส Sendai #SUS304-4inx3inx2mm (ตัว)"
    assert r.op["source"] == "audit_twins.csv:030บ4000"


def test_extract_op_field_anomalies_approved_row():
    row = {"product_id": "686", "current_name": "ถุงหิ้วคละสี ข้าวสาลี 12x20",
           "current_brand_id": "13", "detected_brand_id": "43",
           "detected_brand_name": "Wheat", "sibling_count": "5",
           "decision": "approved (batch1-b): set brand_id=43 Wheat (Put 2026-07-21)"}
    r = cro.extract_op("field_anomalies", row, source_label="audit_field_anomalies.csv")
    assert r.status == "approved"
    assert r.op == {
        "op": "field", "product_id": "686", "field": "brand_id", "value": "43",
        "before": "13", "after": "43",
        "source": "audit_field_anomalies.csv:detected=Wheat",
    }


def test_extract_op_field_anomalies_approved_missing_detected_id_fails_loud():
    row = {"product_id": "686", "current_name": "x", "current_brand_id": "13",
           "detected_brand_id": "", "detected_brand_name": "", "sibling_count": "5",
           "decision": "approved (batch1-b): set brand_id"}
    r = cro.extract_op("field_anomalies", row, source_label="f.csv")
    assert r.status == "error"
    assert "686" in r.error_msg


# ---------------------------------------------------------------------------
# extract_op — generic_mixed (round-2 fix ซ item 3): per-row shape
# resolution when a single file's header carries both proposed_name and
# field/value columns.
# ---------------------------------------------------------------------------

def test_extract_op_generic_mixed_row_with_proposed_name_resolves_to_name_op():
    row = {"product_id": "700", "current_name": "old name", "proposed_name": "new name",
           "field": "", "value": "", "before": "",
           "decision": "approved (G3-judgment): rename per rule"}
    r = cro.extract_op("generic_mixed", row, source_label="round2_decisions_generic.csv")
    assert r.status == "approved"
    assert r.op["op"] == "name" and r.op["before"] == "old name" and r.op["after"] == "new name"


def test_extract_op_generic_mixed_row_with_field_value_resolves_to_field_op():
    row = {"product_id": "701", "current_name": "", "proposed_name": "",
           "field": "brand_id", "value": "9", "before": "NULL",
           "decision": "approved (G3-judgment): set brand_id=9"}
    r = cro.extract_op("generic_mixed", row, source_label="round2_decisions_generic.csv")
    assert r.status == "approved"
    assert r.op["op"] == "field" and r.op["field"] == "brand_id" and r.op["value"] == "9"
    assert r.op["before"] == "NULL"


def test_extract_op_generic_mixed_row_with_both_shapes_fails_loud():
    row = {"product_id": "702", "current_name": "x", "proposed_name": "y",
           "field": "brand_id", "value": "9", "before": "",
           "decision": "approved: ambiguous row"}
    r = cro.extract_op("generic_mixed", row, source_label="f.csv")
    assert r.status == "error"
    assert "702" in r.error_msg


def test_extract_op_generic_mixed_row_with_neither_shape_fails_loud():
    row = {"product_id": "703", "current_name": "", "proposed_name": "",
           "field": "", "value": "", "before": "", "decision": "approved: empty row"}
    r = cro.extract_op("generic_mixed", row, source_label="f.csv")
    assert r.status == "error"
    assert "703" in r.error_msg


def test_extract_op_generic_mixed_not_approved_row_is_skipped():
    row = {"product_id": "704", "current_name": "x", "proposed_name": "y",
           "field": "", "value": "", "before": "", "decision": ""}
    r = cro.extract_op("generic_mixed", row, source_label="f.csv")
    assert r.status == "not_approved"


# ---------------------------------------------------------------------------
# dedupe + conflict detection
# ---------------------------------------------------------------------------

def test_compose_name_and_field_ops_for_same_pid_no_conflict():
    ops = [
        {"op": "name", "product_id": "686", "field": "", "value": "",
         "before": "old name", "after": "new name", "source": "family_divergence:x"},
        {"op": "field", "product_id": "686", "field": "brand_id", "value": "43",
         "before": "13", "after": "43", "source": "field_anomalies:detected=Wheat"},
    ]
    final, conflicts = cro.dedupe_and_check_conflicts(ops)
    assert conflicts == []
    assert len(final) == 2


def test_identical_duplicate_op_is_deduped_silently():
    op = {"op": "name", "product_id": "1", "field": "", "value": "",
          "before": "a", "after": "b", "source": "file1:x"}
    op2 = dict(op, source="file2:y")  # same effective change, different provenance label
    final, conflicts = cro.dedupe_and_check_conflicts([op, op2])
    assert conflicts == []
    assert len(final) == 1


def test_conflicting_name_ops_for_same_pid_fails_loud():
    ops = [
        {"op": "name", "product_id": "1", "field": "", "value": "",
         "before": "a", "after": "b1", "source": "file1:x"},
        {"op": "name", "product_id": "1", "field": "", "value": "",
         "before": "a", "after": "b2-DIFFERENT", "source": "file2:y"},
    ]
    final, conflicts = cro.dedupe_and_check_conflicts(ops)
    assert len(conflicts) == 1
    assert "1" in conflicts[0]


def test_conflicting_field_ops_same_field_different_value_fails_loud():
    ops = [
        {"op": "field", "product_id": "9", "field": "brand_id", "value": "10",
         "before": "", "after": "10", "source": "file1:x"},
        {"op": "field", "product_id": "9", "field": "brand_id", "value": "99",
         "before": "", "after": "99", "source": "file2:y"},
    ]
    final, conflicts = cro.dedupe_and_check_conflicts(ops)
    assert len(conflicts) == 1


def test_different_fields_same_pid_no_conflict():
    ops = [
        {"op": "field", "product_id": "9", "field": "brand_id", "value": "10",
         "before": "", "after": "10", "source": "file1:x"},
        {"op": "field", "product_id": "9", "field": "color_code", "value": "AC",
         "before": "", "after": "AC", "source": "file2:y"},
    ]
    final, conflicts = cro.dedupe_and_check_conflicts(ops)
    assert conflicts == []
    assert len(final) == 2


# ---------------------------------------------------------------------------
# stale-before check (read-only DB)
# ---------------------------------------------------------------------------

def _seed_product(conn, pid, name, brand_id=None):
    conn.execute(
        "INSERT INTO products (id, product_name, brand_id, is_active) VALUES (?,?,?,1)",
        (pid, name, brand_id))
    conn.commit()


def test_stale_before_check_passes_when_db_matches(empty_db, empty_db_conn):
    _seed_product(empty_db_conn, 1, "ถุงหิ้วคละสี ข้าวสาลี 6x11")
    ops = [{"op": "name", "product_id": "1", "field": "", "value": "",
            "before": "ถุงหิ้วคละสี ข้าวสาลี 6x11", "after": "new name", "source": "x"}]
    stale = cro.check_staleness(empty_db, ops)
    assert stale == []


def test_stale_before_check_flags_mismatch(empty_db, empty_db_conn):
    _seed_product(empty_db_conn, 1, "SOMETHING ELSE ENTIRELY NOW")
    ops = [{"op": "name", "product_id": "1", "field": "", "value": "",
            "before": "ถุงหิ้วคละสี ข้าวสาลี 6x11", "after": "new name", "source": "x"}]
    stale = cro.check_staleness(empty_db, ops)
    assert len(stale) == 1
    assert "1" in stale[0]


def test_stale_before_check_flags_missing_product(empty_db, empty_db_conn):
    ops = [{"op": "name", "product_id": "999999", "field": "", "value": "",
            "before": "x", "after": "y", "source": "z"}]
    stale = cro.check_staleness(empty_db, ops)
    assert len(stale) == 1


def test_stale_before_check_no_longer_bypasses_empty_before_on_field_op(empty_db, empty_db_conn):
    """Round-2 fix (code review item 7): the OLD bypass
    (`if col_index is not None and op["before"]:`) silently skipped the ONLY
    staleness guard for a field op whenever 'before' was empty. An empty
    'before' on a field op must now fail loud, not be waved through."""
    _seed_product(empty_db_conn, 1, "some name", brand_id=None)
    ops = [{"op": "field", "product_id": "1", "field": "brand_id", "value": "43",
            "before": "", "after": "43", "source": "x"}]
    stale = cro.check_staleness(empty_db, ops)
    assert len(stale) == 1
    assert "1" in stale[0]


def test_stale_before_check_null_sentinel_matches_genuine_null(empty_db, empty_db_conn):
    _seed_product(empty_db_conn, 1, "some name", brand_id=None)  # genuinely NULL
    ops = [{"op": "field", "product_id": "1", "field": "brand_id", "value": "43",
            "before": "NULL", "after": "43", "source": "x"}]
    stale = cro.check_staleness(empty_db, ops)
    assert stale == []


def test_stale_before_check_null_sentinel_rejects_non_null_current(empty_db, empty_db_conn):
    empty_db_conn.execute("INSERT INTO brands (id, code, name) VALUES (13, 'OTHER', 'Other')")
    empty_db_conn.commit()
    _seed_product(empty_db_conn, 1, "some name", brand_id=13)  # NOT null
    ops = [{"op": "field", "product_id": "1", "field": "brand_id", "value": "43",
            "before": "NULL", "after": "43", "source": "x"}]
    stale = cro.check_staleness(empty_db, ops)
    assert len(stale) == 1


def test_stale_before_check_covers_new_whitelisted_columns(empty_db, empty_db_conn):
    empty_db_conn.execute(
        "INSERT INTO products (id, product_name, size, is_active) VALUES (1, 'x', '6x11', 1)")
    empty_db_conn.commit()
    ops = [{"op": "field", "product_id": "1", "field": "size", "value": "6x11in",
            "before": "6x11", "after": "6x11in", "source": "x"}]
    assert cro.check_staleness(empty_db, ops) == []
    ops[0]["before"] = "STALE-VALUE"
    assert len(cro.check_staleness(empty_db, ops)) == 1


def test_extract_op_strips_whitespace_on_before():
    row = {"product_id": "686", "current_name": "x", "current_brand_id": "  43  ",
           "detected_brand_id": "43", "detected_brand_name": "Wheat", "sibling_count": "5",
           "decision": "approved: x"}
    r = cro.extract_op("field_anomalies", row, source_label="f.csv")
    assert r.op["before"] == "43"


def test_stale_before_check_is_read_only(empty_db, empty_db_conn):
    _seed_product(empty_db_conn, 1, "name a")
    ops = [{"op": "name", "product_id": "1", "field": "", "value": "",
            "before": "name a", "after": "name b", "source": "x"}]
    before = empty_db_conn.execute("SELECT product_name FROM products WHERE id=1").fetchone()[0]
    cro.check_staleness(empty_db, ops)
    after = empty_db_conn.execute("SELECT product_name FROM products WHERE id=1").fetchone()[0]
    assert before == after == "name a"


# ---------------------------------------------------------------------------
# _widen_truncated_size — repairs a real parse_name() limitation discovered
# building item 8: UNIT_GROUP anchors 'in' with \b, so 'in' immediately
# followed by another word char (e.g. the 'x' in '4inx3in') never matches,
# and a compound like '6x11in' (unit only on the LAST segment) can't even
# start matching at the leading bare digit. Either way only the trailing
# segment survives parse_name's own size_re. This is a string-level repair
# on the EXISTING parser's output, not a new parser.
#
# Returns (size, ambiguous) — round-2 fix ค (re-review) item 2: if the parsed
# segment occurs more than once in the proposed name, str.find()'s first-
# occurrence anchor can't safely tell which one the parser actually matched,
# so the function refuses to guess.
# ---------------------------------------------------------------------------

def test_widen_size_leading_bare_digit_no_own_unit():
    # the exact ถุงหิ้ว seed failure mode
    assert cro._widen_truncated_size("ถุงหิ้ว ข้าวสาลี 6x11in คละสี", "11in") == ("6x11in", False)


def test_widen_size_every_segment_has_unit_but_in_lacks_boundary():
    # the exact pid 1888 failure mode ('in\\b' fails when followed by 'x')
    assert cro._widen_truncated_size(
        "บานพับผีเสื้อสแตนเลส Sendai #SUS304-4inx3inx2mm (ตัว)", "2mm") == ("4inx3inx2mm", False)


def test_widen_size_three_bare_leading_segments():
    assert cro._widen_truncated_size(
        "กล่องนอก 4B 10x18x10.1/4in", "10.1/4in") == ("10x18x10.1/4in", False)


def test_widen_size_already_complete_is_a_noop():
    assert cro._widen_truncated_size("บานพับ Sendai #170-3in", "3in") == ("3in", False)


def test_widen_size_empty_input_is_a_noop():
    assert cro._widen_truncated_size("anything", "") == ("", False)


def test_widen_size_does_not_reach_past_a_non_x_boundary():
    # '#SUS304-' ends in '-', not 'x'/'×' — must not swallow the model code
    assert cro._widen_truncated_size("รหัส #SUS304-2mm", "2mm") == ("2mm", False)


def test_widen_size_multiple_occurrences_refuses_to_guess():
    """The re-reviewer's probe: 'รุ่น 2in ท่อ 4inx3inx2in' — parse_name's
    leftmost-match search grabs the standalone decoy '2in' near the start
    (it independently satisfies SIZE_SEG on its own), NOT the compound
    '4inx3inx2in''s own trailing '2in' segment. '2in' occurs twice in the
    string, so there's no safe way to tell which one was actually matched —
    must flag ambiguous and NOT emit a widened (or even the raw) size."""
    size, ambiguous = cro._widen_truncated_size("รุ่น 2in ท่อ 4inx3inx2in", "2in")
    assert ambiguous is True
    assert size == "2in"  # returned unrepaired — caller must not use it


def test_widen_size_single_occurrence_still_widens_normally():
    # sanity: the multi-occurrence guard must not misfire on an ordinary
    # single-occurrence compound.
    assert cro._widen_truncated_size("ถุงหิ้ว ข้าวสาลี 6x11in คละสี", "11in") == ("6x11in", False)


# ---------------------------------------------------------------------------
# derive_structured_sync_ops — real ถุงหิ้ว seed data (code review item 8)
# ---------------------------------------------------------------------------

def test_derive_sync_ops_thung_hiew_seed_emits_size_op(empty_db, empty_db_conn):
    """pid 683's REAL current values (2026-07-21 live DB): size='6x11',
    series='คละสี', brand_id=43 (Wheat/ข้าวสาลี). The approved rename's
    proposed name is 'ถุงหิ้ว ข้าวสาลี 6x11in คละสี'. Parsing it must produce
    exactly one confident sync op: size '6x11' -> '6x11in' (this is the
    literal case the code review asked for). series is conservatively
    flagged for manual review rather than silently trusted: parse_name's own
    internal leftover-text tracking only stripped the un-widened '11in'
    fragment, so its series extraction is contaminated by the stray '6x' —
    fail-open to the ambiguous list is the correct, safe outcome here, not a
    silent (possibly-right-by-luck) auto-match."""
    empty_db_conn.execute("INSERT INTO brands (id, code, name, name_th) VALUES (43,'RICE','Wheat','ข้าวสาลี')")
    empty_db_conn.execute(
        "INSERT INTO products (id, product_name, size, series, brand_id, is_active) "
        "VALUES (683, 'ถุงหิ้วคละสี ข้าวสาลี 6x11', '6x11', 'คละสี', 43, 1)")
    empty_db_conn.commit()

    name_ops = [{"op": "name", "product_id": "683", "field": "", "value": "",
                 "before": "ถุงหิ้วคละสี ข้าวสาลี 6x11", "after": "ถุงหิ้ว ข้าวสาลี 6x11in คละสี",
                 "source": "audit_family_divergence_2026-07-21.csv:cat25-brand43-ถุงหิ้ว"}]
    sync_ops, ambiguous = cro.derive_structured_sync_ops(name_ops, empty_db)

    assert len(sync_ops) == 1
    op = sync_ops[0]
    assert op["op"] == "field" and op["product_id"] == "683" and op["field"] == "size"
    assert op["before"] == "6x11" and op["after"] == op["value"] == "6x11in"
    assert any("683" in a and "series" in a for a in ambiguous)


def test_derive_sync_ops_ambiguous_when_parser_finds_nothing_but_db_has_value(empty_db, empty_db_conn):
    """pid 1888-style real case: DB series='สแตนเลส' but the proposed name
    'บานพับผีเสื้อสแตนเลส Sendai #SUS304-4inx3inx2mm (ตัว)' has no leftover
    text for the parser to assign to series once category/brand/model/size/
    packaging are stripped — parser returns '' for series while DB has a
    real value. Must be flagged ambiguous, NEVER auto-cleared to empty."""
    empty_db_conn.execute("INSERT INTO brands (id, code, name, name_th) VALUES (3,'SD','Sendai','เซ็นได')")
    empty_db_conn.execute(
        "INSERT INTO products (id, product_name, size, series, brand_id, is_active) "
        "VALUES (1888, 'บานพับผีเสื้อสแตนเลส 4in', '4in', 'สแตนเลส', NULL, 1)")
    empty_db_conn.commit()

    name_ops = [{"op": "name", "product_id": "1888", "field": "", "value": "",
                 "before": "บานพับผีเสื้อสแตนเลส 4in",
                 "after": "บานพับผีเสื้อสแตนเลส Sendai #SUS304-4inx3inx2mm (ตัว)",
                 "source": "audit_twins_2026-07-21.csv:030บ4000"}]
    sync_ops, ambiguous = cro.derive_structured_sync_ops(name_ops, empty_db)

    series_ops = [o for o in sync_ops if o["field"] == "series"]
    assert series_ops == []   # never auto-cleared
    assert any("1888" in a and "series" in a for a in ambiguous)
    # size DID parse confidently and DOES differ ('4in' -> '4inx3inx2mm') -> real sync op
    size_ops = [o for o in sync_ops if o["field"] == "size"]
    assert len(size_ops) == 1 and size_ops[0]["after"] == "4inx3inx2mm"


def test_derive_sync_ops_matching_value_produces_no_op(empty_db, empty_db_conn):
    """Size already matches (post-widening) -> no size op. series is still
    conservatively flagged (same widening-contamination reasoning as the
    683 test above) rather than silently trusted."""
    empty_db_conn.execute("INSERT INTO brands (id, code, name, name_th) VALUES (43,'RICE','Wheat','ข้าวสาลี')")
    empty_db_conn.execute(
        "INSERT INTO products (id, product_name, size, series, brand_id, is_active) "
        "VALUES (1373, 'ถุงหิ้ว ข้าวสาลี 8x16in คละสี', '8x16in', 'คละสี', 43, 1)")
    empty_db_conn.commit()
    name_ops = [{"op": "name", "product_id": "1373", "field": "", "value": "",
                 "before": "ถุงหิ้ว ข้าวสาลี 8x16in คละสี", "after": "ถุงหิ้ว ข้าวสาลี 8x16in คละสี",
                 "source": "x"}]
    sync_ops, ambiguous = cro.derive_structured_sync_ops(name_ops, empty_db)
    assert sync_ops == []   # size already matches, post-widening — no op needed
    assert any("1373" in a and "series" in a for a in ambiguous)


def test_derive_sync_ops_packaging_th_and_short_both_emitted(empty_db, empty_db_conn):
    empty_db_conn.execute("INSERT INTO brands (id, code, name, name_th) VALUES (3,'SD','Sendai','เซ็นได')")
    empty_db_conn.execute(
        "INSERT INTO products (id, product_name, brand_id, is_active) "
        "VALUES (99, 'ตัวอย่างสินค้า Sendai', 3, 1)")  # no packaging yet
    empty_db_conn.commit()
    name_ops = [{"op": "name", "product_id": "99", "field": "", "value": "",
                 "before": "ตัวอย่างสินค้า Sendai", "after": "ตัวอย่างสินค้า Sendai (แผง)",
                 "source": "x"}]
    sync_ops, ambiguous = cro.derive_structured_sync_ops(name_ops, empty_db)
    fields = {o["field"]: o for o in sync_ops}
    assert fields["packaging_th"]["after"] == "แผง"
    assert fields["packaging_th"]["before"] == "NULL"
    assert fields["packaging_short"]["after"] == "PN"
    assert fields["packaging_short"]["before"] == "NULL"


def test_derive_sync_ops_empty_input_is_noop():
    assert cro.derive_structured_sync_ops([], "/nonexistent/does/not/matter.db") == ([], [])


def test_derive_sync_ops_condition_never_auto_emits_even_when_confidently_parsed(empty_db, empty_db_conn):
    """Round-2 fix ค (re-review) item 1: condition never auto-emits, even
    when the parser confidently extracts a DIFFERENT value than the DB has
    (not just 'parser found nothing') — always manual review."""
    empty_db_conn.execute(
        "INSERT INTO products (id, product_name, condition, brand_id, is_active) "
        "VALUES (500, 'อุปกรณ์ตัวอย่าง (ไม่สวย)', 'ไม่สวย', NULL, 1)")
    empty_db_conn.commit()
    name_ops = [{"op": "name", "product_id": "500", "field": "", "value": "",
                 "before": "อุปกรณ์ตัวอย่าง (ไม่สวย)", "after": "อุปกรณ์ตัวอย่าง (เก่า)",
                 "source": "x"}]
    sync_ops, ambiguous = cro.derive_structured_sync_ops(name_ops, empty_db)

    condition_ops = [o for o in sync_ops if o["field"] == "condition"]
    assert condition_ops == []   # never emitted, despite a confident 'เก่า' parse
    assert any("500" in a and "condition" in a for a in ambiguous)


def test_derive_sync_ops_pack_variant_never_auto_emits_even_when_confidently_parsed(empty_db, empty_db_conn):
    """Same guarantee as condition, for pack_variant."""
    empty_db_conn.execute(
        "INSERT INTO products (id, product_name, pack_variant, brand_id, is_active) "
        "VALUES (501, 'อุปกรณ์ตัวอย่าง - 3', '3', NULL, 1)")
    empty_db_conn.commit()
    name_ops = [{"op": "name", "product_id": "501", "field": "", "value": "",
                 "before": "อุปกรณ์ตัวอย่าง - 3", "after": "อุปกรณ์ตัวอย่าง - 5",
                 "source": "x"}]
    sync_ops, ambiguous = cro.derive_structured_sync_ops(name_ops, empty_db)

    pack_variant_ops = [o for o in sync_ops if o["field"] == "pack_variant"]
    assert pack_variant_ops == []   # never emitted, despite a confident '5' parse
    assert any("501" in a and "pack_variant" in a for a in ambiguous)


def test_derive_sync_ops_size_multiple_occurrences_flags_ambiguous_not_fabricated(empty_db, empty_db_conn):
    """Round-2 fix ค (re-review) item 2, integration-level: the re-reviewer's
    probe string through the full derive path. A decoy standalone '2in' near
    the start must NOT be mistaken for the real compound size — no size op
    at all, and the pid must land on the manual-review list instead of
    silently getting a fabricated '2in'."""
    empty_db_conn.execute(
        "INSERT INTO products (id, product_name, size, brand_id, is_active) "
        "VALUES (502, 'ท่อตัวอย่าง 4x3x2in', '4x3x2in', NULL, 1)")
    empty_db_conn.commit()
    name_ops = [{"op": "name", "product_id": "502", "field": "", "value": "",
                 "before": "ท่อตัวอย่าง 4x3x2in", "after": "รุ่น 2in ท่อ 4inx3inx2in",
                 "source": "x"}]
    sync_ops, ambiguous = cro.derive_structured_sync_ops(name_ops, empty_db)

    size_ops = [o for o in sync_ops if o["field"] == "size"]
    assert size_ops == []   # NOT a fabricated '2in' op
    assert any("502" in a and "size" in a and "once" in a for a in ambiguous)


# ---------------------------------------------------------------------------
# End-to-end compile_files() — now includes structured-sync ops
# ---------------------------------------------------------------------------

def test_compile_files_end_to_end(tmp_path, empty_db, empty_db_conn):
    empty_db_conn.execute("INSERT INTO brands (id, code, name) VALUES (13, 'OTHER', 'Other')")
    empty_db_conn.execute("INSERT INTO brands (id, code, name, name_th) VALUES (43, 'RICE', 'Wheat', 'ข้าวสาลี')")
    empty_db_conn.commit()
    _seed_product(empty_db_conn, 683, "ถุงหิ้วคละสี ข้าวสาลี 6x11")
    _seed_product(empty_db_conn, 686, "ถุงหิ้วคละสี ข้าวสาลี 12x20", brand_id=13)

    fam_csv = write_csv(tmp_path / "audit_family_divergence_2026-07-21.csv",
        ["family_key", "product_id", "is_active", "current_name", "proposed_name",
         "fix_kind", "note", "decision"],
        [
            {"family_key": "cat25-brand43-ถุงหิ้ว", "product_id": "683", "is_active": "1",
             "current_name": "ถุงหิ้วคละสี ข้าวสาลี 6x11",
             "proposed_name": "ถุงหิ้ว ข้าวสาลี 6x11in คละสี", "fix_kind": "mechanical_auto",
             "note": "x", "decision": "approved (batch1-a): rename per proposal"},
            {"family_key": "cat25-brand43-ถุงหิ้ว", "product_id": "686", "is_active": "1",
             "current_name": "ถุงหิ้วคละสี ข้าวสาลี 12x20",
             "proposed_name": "ถุงหิ้ว ข้าวสาลี 12x20in คละสี", "fix_kind": "manual_add",
             "note": "x", "decision": "approved (batch1-b): rename per proposal"},
            {"family_key": "cat3-x", "product_id": "9999", "is_active": "1",
             "current_name": "unrelated", "proposed_name": "",
             "fix_kind": "needs_manual_family_review", "note": "x", "decision": ""},
        ])
    field_csv = write_csv(tmp_path / "audit_field_anomalies_2026-07-21.csv",
        ["product_id", "current_name", "current_brand_id", "detected_brand_id",
         "detected_brand_name", "sibling_count", "decision"],
        [
            {"product_id": "686", "current_name": "ถุงหิ้วคละสี ข้าวสาลี 12x20",
             "current_brand_id": "13", "detected_brand_id": "43",
             "detected_brand_name": "Wheat", "sibling_count": "5",
             "decision": "approved (batch1-b): set brand_id=43 Wheat"},
        ])
    unstamped_csv = write_csv(tmp_path / "prescreen_rows_2026-07-21.csv",
        ["product_id", "source_file", "recommend", "evidence"],
        [{"product_id": "1", "source_file": "x.csv", "recommend": "recommend-approve",
          "evidence": "some evidence"}])

    result = cro.compile_files([fam_csv, field_csv, unstamped_csv], empty_db)

    assert result.errors == []
    ops_by_pid = {}
    for o in result.ops:
        ops_by_pid.setdefault(o["product_id"], []).append(o)
    assert any(o["op"] == "name" for o in ops_by_pid["683"])
    # 683 also gets a structured-sync size op (DB size was NULL, proposed name
    # has '6x11in') — code review item 8.
    size_op_683 = [o for o in ops_by_pid["683"] if o["op"] == "field" and o["field"] == "size"]
    assert len(size_op_683) == 1 and size_op_683[0]["after"] == "6x11in"

    # pid 686 composes to 3 ops: name + brand_id field (field_anomalies) +
    # size field (structured-sync) — three DIFFERENT op keys, no conflict.
    pid686_ops = ops_by_pid["686"]
    assert len(pid686_ops) == 3
    assert {(o["op"], o["field"]) for o in pid686_ops} == \
        {("name", ""), ("field", "brand_id"), ("field", "size")}

    assert result.summary["approved"] == 3   # 683 (name), 686 (name), 686 (field) — sync ops are DERIVED, not "approved" rows
    assert result.summary["not_approved"] == 1  # pid 9999
    assert result.summary["unstamped_files"] == 1
    assert result.summary["ops_by_type"] == {"name": 2, "field": 3}
    # _seed_product doesn't set series at all here (unlike the dedicated
    # derive_structured_sync_ops tests above, which pin the real ถุงหิ้ว
    # series='คละสี' contamination guard) — DB series is NULL. series NEVER
    # auto-emits (round-2 fix ค item 1) and the parsed leftover text for both
    # 683 and 686 is non-empty ('...คละสี', contaminated by the widened
    # size's stray prefix) — that differs from the DB's empty value, so BOTH
    # pids are correctly flagged for manual review (this is a real
    # improvement over the old size-widening-conditional gate, which stayed
    # silent whenever the DB side happened to be empty too).
    ambiguous_pids = {a.split(":")[0].replace("pid ", "") for a in result.summary["structured_sync_ambiguous"]}
    assert ambiguous_pids == {"683", "686"}
    assert all("series" in a for a in result.summary["structured_sync_ambiguous"])


def test_compile_files_reports_structured_sync_ambiguous_fail_open(tmp_path, empty_db, empty_db_conn):
    """An ambiguous structured-field parse must NOT block the compile (the
    name op + any unrelated ops still go through) — it's listed in the
    summary for manual review instead (fail-open to a visible list)."""
    empty_db_conn.execute("INSERT INTO brands (id, code, name) VALUES (3, 'SD', 'Sendai')")
    empty_db_conn.execute(
        "INSERT INTO products (id, product_name, series, brand_id, is_active) "
        "VALUES (1888, 'บานพับผีเสื้อสแตนเลส 4in', 'สแตนเลส', NULL, 1)")
    empty_db_conn.commit()
    csv_a = write_csv(tmp_path / "audit_twins_2026-07-21.csv",
        ["bsn_code", "product_id", "bsn_unit", "current_name", "aligned", "proposed_name", "decision"],
        [{"bsn_code": "030บ4000", "product_id": "1888", "bsn_unit": "ตัว",
          "current_name": "บานพับผีเสื้อสแตนเลส 4in", "aligned": "False",
          "proposed_name": "บานพับผีเสื้อสแตนเลส Sendai #SUS304-4inx3inx2mm (ตัว)",
          "decision": "approved (batch1-c): rename per proposal"}])

    result = cro.compile_files([csv_a], empty_db)

    assert result.errors == []   # ambiguity does NOT block the compile
    assert any(o["op"] == "name" for o in result.ops)
    assert any("1888" in a and "series" in a for a in result.summary["structured_sync_ambiguous"])


def test_compile_files_fails_loud_on_conflict(tmp_path, empty_db, empty_db_conn):
    _seed_product(empty_db_conn, 1, "same current name")
    csv_a = write_csv(tmp_path / "audit_family_divergence_2026-07-21.csv",
        ["family_key", "product_id", "is_active", "current_name", "proposed_name",
         "fix_kind", "note", "decision"],
        [{"family_key": "x", "product_id": "1", "is_active": "1",
          "current_name": "same current name", "proposed_name": "version A",
          "fix_kind": "mechanical_auto", "note": "", "decision": "approved: x"}])
    csv_b = write_csv(tmp_path / "audit_twins_2026-07-21.csv",
        ["bsn_code", "product_id", "bsn_unit", "current_name", "aligned",
         "proposed_name", "decision"],
        [{"bsn_code": "x", "product_id": "1", "bsn_unit": "แผง",
          "current_name": "same current name", "aligned": "False",
          "proposed_name": "version B DIFFERENT", "decision": "approved: y"}])

    result = cro.compile_files([csv_a, csv_b], empty_db)
    assert len(result.errors) >= 1
    assert any("1" in e for e in result.errors)
    assert result.ops == []   # refuses to emit ANYTHING when a conflict exists


def test_compile_files_fails_loud_on_missing_proposed_name(tmp_path, empty_db, empty_db_conn):
    _seed_product(empty_db_conn, 111, "some name")
    csv_a = write_csv(tmp_path / "audit_twins_2026-07-21.csv",
        ["bsn_code", "product_id", "bsn_unit", "current_name", "aligned",
         "proposed_name", "decision"],
        [{"bsn_code": "x", "product_id": "111", "bsn_unit": "ตัว",
          "current_name": "some name", "aligned": "False", "proposed_name": "",
          "decision": "approved: corrected proposal described only in prose"}])
    result = cro.compile_files([csv_a], empty_db)
    assert any("111" in e for e in result.errors)
    assert result.ops == []


def test_sku_regen_implied_count_excludes_unrelated_catalog_wide_drift(empty_db, empty_db_conn):
    """plan_sku_regen (reused as-is) scans the WHOLE catalog for drift — a
    product with a pre-existing stale sku_code, unrelated to this batch's
    field ops, must NOT inflate the 'sku-regen implied' count. Regression
    guard for the bug caught during this tool's own smoke test against the
    live DB (18 reported for 1 real field op, because ~17 pre-existing
    unrelated drift rows leaked in)."""
    empty_db_conn.execute("INSERT INTO categories (id, code, name_th, short_code) VALUES (1,'C1','cat','OTH')")
    empty_db_conn.execute("INSERT INTO brands (id, code, name, short_code) VALUES (1,'B1','BrandOne','B1')")
    empty_db_conn.execute("INSERT INTO brands (id, code, name, short_code) VALUES (2,'B2','BrandTwo','B2')")
    # pid 10: UNTOUCHED by this batch, but its stored sku_code is already stale
    # (pre-existing drift the compiler never asked about).
    empty_db_conn.execute(
        "INSERT INTO products (id, product_name, category_id, brand_id, sku_code, is_active) "
        "VALUES (10, 'unrelated product', 1, 1, 'STALE-UNRELATED-CODE', 1)")
    # pid 686-style: THIS batch's field op changes brand_id 1 -> 2, which
    # genuinely changes its computed sku_code.
    empty_db_conn.execute(
        "INSERT INTO products (id, product_name, category_id, brand_id, sku_code, is_active) "
        "VALUES (20, 'touched product', 1, 1, 'OTH-B1', 1)")
    empty_db_conn.commit()

    ops = [{"op": "field", "product_id": "20", "field": "brand_id", "value": "2",
            "before": "1", "after": "2", "source": "test"}]
    n = cro._sku_regen_implied_count(empty_db, ops)
    assert n == 1   # only pid 20 — pid 10's pre-existing drift must not leak in


def test_compile_files_fails_loud_on_stale_before(tmp_path, empty_db, empty_db_conn):
    _seed_product(empty_db_conn, 1, "DB HAS SOMETHING DIFFERENT NOW")
    csv_a = write_csv(tmp_path / "audit_family_divergence_2026-07-21.csv",
        ["family_key", "product_id", "is_active", "current_name", "proposed_name",
         "fix_kind", "note", "decision"],
        [{"family_key": "x", "product_id": "1", "is_active": "1",
          "current_name": "stale csv-recorded name", "proposed_name": "new name",
          "fix_kind": "mechanical_auto", "note": "", "decision": "approved: x"}])
    result = cro.compile_files([csv_a], empty_db)
    assert any("1" in e for e in result.errors)
    assert result.ops == []


# ---------------------------------------------------------------------------
# check_name_collisions — proposed-name collision guard (round-2 fix ซ item 2)
# ---------------------------------------------------------------------------

def test_check_name_collisions_external_collision_fails_loud(empty_db, empty_db_conn):
    """pid 5 is ACTIVE and already holds the name pid 1 wants to move into —
    pid 5 has no rename op of its own in this batch, so this is real."""
    _seed_product(empty_db_conn, 1, "pid 1's current name")
    _seed_product(empty_db_conn, 5, "COLLISION NAME")
    name_ops = [{"op": "name", "product_id": "1", "field": "", "value": "",
                 "before": "pid 1's current name", "after": "COLLISION NAME", "source": "x"}]
    errors = cro.check_name_collisions(empty_db, name_ops)
    assert len(errors) == 1
    assert "1" in errors[0] and "5" in errors[0] and "COLLISION NAME" in errors[0]


def test_check_name_collisions_ignores_inactive_products(empty_db, empty_db_conn):
    """An INACTIVE product holding the target name is not a real collision —
    it's a tombstone/merged-away row, not a live duplicate."""
    empty_db_conn.execute(
        "INSERT INTO products (id, product_name, is_active) VALUES (5, 'COLLISION NAME', 0)")
    _seed_product(empty_db_conn, 1, "pid 1's current name")
    empty_db_conn.commit()
    name_ops = [{"op": "name", "product_id": "1", "field": "", "value": "",
                 "before": "pid 1's current name", "after": "COLLISION NAME", "source": "x"}]
    assert cro.check_name_collisions(empty_db, name_ops) == []


def test_check_name_collisions_intra_batch_duplicate_fails_loud(empty_db, empty_db_conn):
    """Two DIFFERENT products in the same batch both propose the identical
    name — nothing adjudicates which one wins, must fail loud."""
    _seed_product(empty_db_conn, 1, "pid 1 old")
    _seed_product(empty_db_conn, 2, "pid 2 old")
    name_ops = [
        {"op": "name", "product_id": "1", "field": "", "value": "",
         "before": "pid 1 old", "after": "SAME NAME", "source": "x"},
        {"op": "name", "product_id": "2", "field": "", "value": "",
         "before": "pid 2 old", "after": "SAME NAME", "source": "y"},
    ]
    errors = cro.check_name_collisions(empty_db, name_ops)
    assert len(errors) == 1
    assert "1" in errors[0] and "2" in errors[0] and "SAME NAME" in errors[0]


def test_check_name_collisions_legal_swap_chain_passes(empty_db, empty_db_conn):
    """pid 1 renames INTO pid 2's current name, while pid 2 renames to
    something else in the SAME batch — legal, since pid 2 vacates the name
    it's colliding on. Must NOT fail loud."""
    _seed_product(empty_db_conn, 1, "NAME-A")
    _seed_product(empty_db_conn, 2, "NAME-B")
    name_ops = [
        {"op": "name", "product_id": "1", "field": "", "value": "",
         "before": "NAME-A", "after": "NAME-B", "source": "x"},
        {"op": "name", "product_id": "2", "field": "", "value": "",
         "before": "NAME-B", "after": "NAME-C", "source": "y"},
    ]
    assert cro.check_name_collisions(empty_db, name_ops) == []


def test_check_name_collisions_full_two_way_swap_passes(empty_db, empty_db_conn):
    """pid 1 and pid 2 fully swap names with each other — both are renaming
    in this same batch, so neither triggers the external-collision guard."""
    _seed_product(empty_db_conn, 1, "NAME-A")
    _seed_product(empty_db_conn, 2, "NAME-B")
    name_ops = [
        {"op": "name", "product_id": "1", "field": "", "value": "",
         "before": "NAME-A", "after": "NAME-B", "source": "x"},
        {"op": "name", "product_id": "2", "field": "", "value": "",
         "before": "NAME-B", "after": "NAME-A", "source": "y"},
    ]
    assert cro.check_name_collisions(empty_db, name_ops) == []


def test_check_name_collisions_field_only_pid_does_not_count_as_renaming_away(empty_db, empty_db_conn):
    """pid 2 is touched by this batch, but only via a FIELD op — its
    product_name is NOT changing, so pid 1 renaming into pid 2's current
    name is still a real collision (the exclusion only applies to a
    product's OWN 'name' op, per the docstring)."""
    _seed_product(empty_db_conn, 1, "pid 1 old")
    _seed_product(empty_db_conn, 2, "STILL-HELD-NAME")
    name_ops = [{"op": "name", "product_id": "1", "field": "", "value": "",
                 "before": "pid 1 old", "after": "STILL-HELD-NAME", "source": "x"}]
    # pid 2's field op is irrelevant to check_name_collisions (it only ever
    # receives name_ops), so the exclusion correctly can't see it either —
    # this test documents/pins that a field-only pid never suppresses the guard.
    errors = cro.check_name_collisions(empty_db, name_ops)
    assert len(errors) == 1 and "2" in errors[0]


def test_check_name_collisions_empty_input_is_noop():
    assert cro.check_name_collisions("/nonexistent/does/not/matter.db", []) == []


def test_check_name_collisions_is_read_only(empty_db, empty_db_conn):
    _seed_product(empty_db_conn, 1, "pid 1 old")
    _seed_product(empty_db_conn, 5, "COLLISION NAME")
    name_ops = [{"op": "name", "product_id": "1", "field": "", "value": "",
                 "before": "pid 1 old", "after": "COLLISION NAME", "source": "x"}]
    before = empty_db_conn.execute("SELECT product_name FROM products WHERE id=5").fetchone()[0]
    cro.check_name_collisions(empty_db, name_ops)
    after = empty_db_conn.execute("SELECT product_name FROM products WHERE id=5").fetchone()[0]
    assert before == after == "COLLISION NAME"


def test_compile_files_fails_loud_on_external_name_collision(tmp_path, empty_db, empty_db_conn):
    """End-to-end: an external name collision must fail the WHOLE compile,
    same as a stale-before or conflict error."""
    _seed_product(empty_db_conn, 1, "pid 1 old name")
    _seed_product(empty_db_conn, 5, "ALREADY TAKEN NAME")
    csv_a = write_csv(tmp_path / "audit_family_divergence_2026-07-21.csv",
        ["family_key", "product_id", "is_active", "current_name", "proposed_name",
         "fix_kind", "note", "decision"],
        [{"family_key": "x", "product_id": "1", "is_active": "1",
          "current_name": "pid 1 old name", "proposed_name": "ALREADY TAKEN NAME",
          "fix_kind": "mechanical_auto", "note": "", "decision": "approved: x"}])
    result = cro.compile_files([csv_a], empty_db)
    assert any("1" in e and "5" in e for e in result.errors)
    assert result.ops == []


def test_compile_files_legal_swap_chain_compiles_cleanly(tmp_path, empty_db, empty_db_conn):
    """End-to-end: a legal same-batch swap must compile with zero errors."""
    _seed_product(empty_db_conn, 1, "NAME-A")
    _seed_product(empty_db_conn, 2, "NAME-B")
    csv_a = write_csv(tmp_path / "audit_family_divergence_2026-07-21.csv",
        ["family_key", "product_id", "is_active", "current_name", "proposed_name",
         "fix_kind", "note", "decision"],
        [{"family_key": "x", "product_id": "1", "is_active": "1",
          "current_name": "NAME-A", "proposed_name": "NAME-B",
          "fix_kind": "mechanical_auto", "note": "", "decision": "approved: x"},
         {"family_key": "x", "product_id": "2", "is_active": "1",
          "current_name": "NAME-B", "proposed_name": "NAME-C",
          "fix_kind": "mechanical_auto", "note": "", "decision": "approved: x"}])
    result = cro.compile_files([csv_a], empty_db)
    assert result.errors == []
    assert len(result.ops) == 2


# ---------------------------------------------------------------------------
# compile_files — generic_mixed end to end (round-2 fix ซ item 3). Pins the
# ONE CSV shape the main thread will use to stamp ALL future bucket
# approvals (judgment/mechanical/ambiguous/ไม่สวย strips/G-groups): a single
# `round2_decisions_generic.csv` with mixed row shapes.
# ---------------------------------------------------------------------------

def test_generic_mixed_csv_compiles_name_and_field_rows_correctly(tmp_path, empty_db, empty_db_conn):
    _seed_product(empty_db_conn, 700, "เก่า ชื่อสินค้า")
    _seed_product(empty_db_conn, 701, "อีกสินค้าหนึ่ง")  # brand_id genuinely NULL
    empty_db_conn.execute("INSERT INTO brands (id, code, name) VALUES (9, 'X9', 'BrandNine')")
    empty_db_conn.commit()

    generic_csv = write_csv(tmp_path / "round2_decisions_generic.csv",
        ["product_id", "current_name", "proposed_name", "field", "value", "before", "decision"],
        [
            {"product_id": "700", "current_name": "เก่า ชื่อสินค้า", "proposed_name": "ใหม่ ชื่อสินค้า",
             "field": "", "value": "", "before": "",
             "decision": "approved (G3-judgment): rename per rule (Put 2026-07-22)"},
            {"product_id": "701", "current_name": "", "proposed_name": "",
             "field": "brand_id", "value": "9", "before": "NULL",
             "decision": "approved (G3-judgment): set brand_id=9 (Put 2026-07-22)"},
        ])

    result = cro.compile_files([generic_csv], empty_db)

    assert result.errors == []
    ops_by_pid = {o["product_id"]: o for o in result.ops}
    assert ops_by_pid["700"]["op"] == "name" and ops_by_pid["700"]["after"] == "ใหม่ ชื่อสินค้า"
    assert ops_by_pid["701"]["op"] == "field" and ops_by_pid["701"]["field"] == "brand_id"
    assert ops_by_pid["701"]["value"] == "9"


def test_generic_mixed_csv_composes_with_native_kind_files_and_guards_apply(tmp_path, empty_db, empty_db_conn):
    """The generic file's ops must compose alongside a native-kind file in
    the SAME compile — and the collision + staleness guards still apply to
    ops from EITHER source, not just the native ones."""
    _seed_product(empty_db_conn, 700, "เก่า ชื่อสินค้า")
    _seed_product(empty_db_conn, 701, "อีกสินค้าหนึ่ง")
    empty_db_conn.execute("INSERT INTO brands (id, code, name, name_th) VALUES (43, 'RICE', 'Wheat', 'ข้าวสาลี')")
    _seed_product(empty_db_conn, 683, "ถุงหิ้วคละสี ข้าวสาลี 6x11")
    empty_db_conn.commit()

    generic_csv = write_csv(tmp_path / "round2_decisions_generic.csv",
        ["product_id", "current_name", "proposed_name", "field", "value", "before", "decision"],
        [
            {"product_id": "700", "current_name": "เก่า ชื่อสินค้า", "proposed_name": "ใหม่ ชื่อสินค้า",
             "field": "", "value": "", "before": "",
             "decision": "approved (G3-judgment): rename per rule"},
            {"product_id": "701", "current_name": "", "proposed_name": "",
             "field": "brand_id", "value": "9", "before": "NULL",
             "decision": "approved (G3-judgment): set brand_id=9"},
        ])
    fam_csv = write_csv(tmp_path / "audit_family_divergence_2026-07-21.csv",
        ["family_key", "product_id", "is_active", "current_name", "proposed_name",
         "fix_kind", "note", "decision"],
        [{"family_key": "cat25-brand43-ถุงหิ้ว", "product_id": "683", "is_active": "1",
          "current_name": "ถุงหิ้วคละสี ข้าวสาลี 6x11", "proposed_name": "ถุงหิ้ว ข้าวสาลี 6x11in คละสี",
          "fix_kind": "mechanical_auto", "note": "", "decision": "approved (batch1-a): rename per proposal"}])

    result = cro.compile_files([generic_csv, fam_csv], empty_db)

    assert result.errors == []
    pids = {o["product_id"] for o in result.ops}
    assert {"700", "701", "683"} <= pids
    # pid 683 also gets its structured-sync size op — proves the guards ran
    # over the COMBINED op set, not just the generic file's own ops.
    assert any(o["product_id"] == "683" and o["field"] == "size" for o in result.ops)


def test_generic_mixed_csv_ambiguous_row_fails_the_whole_compile(tmp_path, empty_db, empty_db_conn):
    _seed_product(empty_db_conn, 702, "some name")
    generic_csv = write_csv(tmp_path / "round2_decisions_generic.csv",
        ["product_id", "current_name", "proposed_name", "field", "value", "before", "decision"],
        [{"product_id": "702", "current_name": "some name", "proposed_name": "new name",
          "field": "brand_id", "value": "9", "before": "",
          "decision": "approved: ambiguous — has both shapes"}])
    result = cro.compile_files([generic_csv], empty_db)
    assert any("702" in e for e in result.errors)
    assert result.ops == []
