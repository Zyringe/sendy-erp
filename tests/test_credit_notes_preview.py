"""TDD tests for preview_credit_notes_import() — the dry-run wrapper used by
the /import-credit-notes/preview UI route.

Covers:
  1. Preview does NOT write — row counts and Σ credited_amount unchanged on
     disk after preview returns.
  2. Preview cna_diff correctly classifies SRs as new / unchanged / changed
     relative to the existing credit_note_amounts state.
  3. Commit (import_credit_notes) after preview produces result counts
     identical to what preview reported.
  4. Preview surfaces ref_conflicts the same way the real importer does.

Uses fixtures and helpers from tests/test_credit_notes_import.py — re-imports
the cp874 fixture builder and seed helpers.
"""
import sqlite3
import pytest

from tests.test_credit_notes_import import (
    _write_cn_file,
    _seed_sr,
    _sr_net_sum,
    _sr_count,
    _ensure_062,
    _CN_HEADER,
    _SR_WITH_REF,
    _SR_MIXED,
    _SR_ORACLE,
    _SR_CONFLICT_REF,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _cna_state(conn):
    """Snapshot {sr_doc_base: credited_amount} from credit_note_amounts."""
    return {
        r["sr_doc_base"]: r["credited_amount"]
        for r in conn.execute(
            "SELECT sr_doc_base, credited_amount FROM credit_note_amounts"
        )
    }


def _cna_count(conn):
    return conn.execute("SELECT COUNT(*) FROM credit_note_amounts").fetchone()[0]


# ── 1: Preview does not write ────────────────────────────────────────────────

def test_preview_does_not_write_credit_note_amounts(tmp_path, tmp_db, tmp_db_conn):
    """Running preview must leave credit_note_amounts row count and values
    identical to the pre-preview state on disk."""
    import import_credit_notes as icn

    _ensure_062(tmp_db_conn)

    # Pre-seed one credit_note_amounts row so we can detect any disturbance
    tmp_db_conn.execute(
        """INSERT INTO credit_note_amounts
               (sr_doc_base, ref_invoice, credited_amount, sr_date_iso,
                customer, source)
           VALUES ('SR9999998', 'IV9999998', 11.11, '2024-01-01',
                   'seed-pre-existing', 'seed')"""
    )
    tmp_db_conn.commit()

    # Also seed an SR row in sales_transactions with NULL ref so we can verify
    # preview does NOT backfill it (UPDATEs don't change row count — so a
    # ref-check is the only way to catch a leaked write here).
    _seed_sr(tmp_db_conn, "SR8800001-1", "SR8800001",
             ref_invoice=None, net=1000.0)
    tmp_db_conn.commit()

    state_before = _cna_state(tmp_db_conn)
    count_before = _cna_count(tmp_db_conn)
    sr_count_before = _sr_count(tmp_db_conn)
    sr_net_before = _sr_net_sum(tmp_db_conn)

    path = _write_cn_file(tmp_path, _SR_MIXED, "preview_nowrite.csv")
    preview = icn.preview_credit_notes_import(path, db_path=tmp_db)
    # Sanity: preview reports it WOULD have backfilled
    assert preview["refs_backfilled"] == 1

    # Preview itself reports what WOULD happen — non-empty
    assert preview["parsed"] >= 2

    # Force a fresh look at the disk via a new connection (rules out per-
    # connection transaction effects)
    fresh = sqlite3.connect(tmp_db)
    fresh.row_factory = sqlite3.Row
    try:
        state_after = _cna_state(fresh)
        count_after = _cna_count(fresh)
        sr_count_after = _sr_count(fresh)
        sr_net_after = _sr_net_sum(fresh)
        seeded_ref = fresh.execute(
            "SELECT ref_invoice FROM sales_transactions WHERE doc_no=?",
            ("SR8800001-1",)
        ).fetchone()["ref_invoice"]
    finally:
        fresh.close()

    assert count_after == count_before, "credit_note_amounts row count must not change"
    assert state_after == state_before, "credit_note_amounts values must not change"
    assert sr_count_after == sr_count_before, "sales_transactions row count must not change"
    assert sr_net_after == pytest.approx(sr_net_before), "Σ SR net must not change"
    assert seeded_ref is None, (
        "ref_invoice must NOT be backfilled — preview leaked an UPDATE write "
        "(this is the canary for SAVEPOINT/auto-txn interaction bugs)"
    )


# ── 2: cna_diff classification — new / unchanged / changed ───────────────────

def test_preview_classifies_new_unchanged_changed(tmp_path, empty_db, empty_db_conn):
    """Seed two credit_note_amounts rows (one matching file, one drifted),
    then a third SR in the file that's brand new.  Preview must classify them
    correctly.

    Uses empty_db so credit_note_amounts starts at exactly zero rows — the
    live DB already has ~191 SR rows which would inflate `unchanged`.
    """
    import import_credit_notes as icn

    _ensure_062(empty_db_conn)

    # File has SR8800001 (1000.0) and SR8800003 (750.0) — see _SR_MIXED
    # Seed SR8800001 with the SAME credited_amount → should be "unchanged"
    # Seed SR8800003 with a DIFFERENT credited_amount → should be "changed"
    empty_db_conn.execute(
        """INSERT INTO credit_note_amounts (sr_doc_base, ref_invoice,
              credited_amount, sr_date_iso, customer, source)
           VALUES ('SR8800001', 'IV8800100', 1000.0, '2024-01-08',
                   'ร้านทดสอบA', 'seed')"""
    )
    empty_db_conn.execute(
        """INSERT INTO credit_note_amounts (sr_doc_base, ref_invoice,
              credited_amount, sr_date_iso, customer, source)
           VALUES ('SR8800003', 'IV8800300', 999.99, '2024-01-10',
                   'ร้านทดสอบC', 'seed')"""
    )
    empty_db_conn.commit()

    # Add a third SR-master in the file (SR8800099) not in DB → should be "new"
    fixture = list(_SR_MIXED) + [
        '"  SR8800099    11/01/67  ร้านทดสอบZ                           06         IV8800999    1                   222.00         0.00        222.00        Y      2"',
        '"     Y   1 041ม5560\xa0\xa0มือจับ(P)#555-350มิล.              1.00แผง             222.00                   222.00                                IV8800999-  1"',
        '',
    ]
    path = _write_cn_file(tmp_path, fixture, "preview_classify.csv")

    preview = icn.preview_credit_notes_import(path, db_path=empty_db)
    diff = preview["cna_diff"]

    new_keys     = {r["sr_doc_base"] for r in diff["new"]}
    changed_keys = {r["sr_doc_base"] for r in diff["changed"]}

    assert "SR8800099" in new_keys, "Brand-new SR must be in cna_diff.new"
    assert "SR8800001" not in new_keys
    assert "SR8800001" not in changed_keys, "SR8800001 matches DB exactly → unchanged"
    assert "SR8800003" in changed_keys, "SR8800003 differs from DB → changed"

    # Verify the changed row carries the right numbers
    sr03 = next(r for r in diff["changed"] if r["sr_doc_base"] == "SR8800003")
    assert sr03["db_amount"] == pytest.approx(999.99)
    assert sr03["file_amount"] == pytest.approx(750.0)
    assert sr03["diff"] == pytest.approx(750.0 - 999.99)
    assert sr03["ref_invoice"] == "IV8800300"

    # unchanged count includes SR8800001 (1 row)
    assert diff["unchanged"] == 1


# ── 3: Commit after preview produces identical result counts ─────────────────

def test_commit_after_preview_matches_preview_result(tmp_path, tmp_db, tmp_db_conn):
    """Running preview then import_credit_notes() must produce the same
    `existing_matched`, `refs_backfilled`, `new_recorded`, `ref_conflicts`
    counts (preview is a fully-faithful dry-run)."""
    import import_credit_notes as icn

    _ensure_062(tmp_db_conn)
    _seed_sr(tmp_db_conn, "SR8800001-1", "SR8800001", ref_invoice=None, net=1000.0)
    tmp_db_conn.commit()

    path = _write_cn_file(tmp_path, _SR_MIXED, "commit_match.csv")

    preview = icn.preview_credit_notes_import(path, db_path=tmp_db)
    actual  = icn.import_credit_notes(path, db_path=tmp_db)

    for key in ("parsed", "existing_matched", "refs_backfilled",
                "new_recorded", "already_new", "skipped"):
        assert preview[key] == actual[key], (
            f"preview[{key}]={preview[key]} != actual[{key}]={actual[key]}"
        )
    assert len(preview["ref_conflicts"]) == len(actual["ref_conflicts"])


# ── 4: regression — `unchanged` must be scoped to file SRs only ─────────────

def test_preview_unchanged_scoped_to_file_srs(tmp_path, tmp_db, tmp_db_conn):
    """Regression: previously the diff loop iterated ALL rows in
    credit_note_amounts, so on the live DB (~192 existing SR rows) a 3-master
    file would report `unchanged ≈ 192` — burying real CHANGED rows in noise.

    The contract: `unchanged + len(new) + len(changed) <= parsed_masters`
    (≤ because non-mig062 fixtures may skip classification entirely).  In
    particular it must NEVER include rows the file did not touch.
    """
    import import_credit_notes as icn

    _ensure_062(tmp_db_conn)

    # Verify the live DB really has many pre-existing rows (the trap)
    pre_existing = tmp_db_conn.execute(
        "SELECT COUNT(*) FROM credit_note_amounts"
    ).fetchone()[0]

    # Build a tiny 3-master file with NONE of those SR numbers
    path = _write_cn_file(
        tmp_path,
        _CN_HEADER + [
            '"  SR9988771    08/01/67  ทดสอบA                                06         IV9988771    1                  1000.00         0.00       1000.00        Y      2"',
            '"     Y   1 041ม5560\xa0\xa0มือจับ                              2.00แผง             500.00                  1000.00                                IV9988771-  1"',
            '',
            '"  SR9988772    08/01/67  ทดสอบB                                06         IV9988772    1                   500.00         0.00        500.00        Y      2"',
            '"     Y   1 041ม5560\xa0\xa0มือจับ                              1.00แผง             500.00                   500.00                                IV9988772-  1"',
            '',
            '"  SR9988773    08/01/67  ทดสอบC                                06         IV9988773    1                   250.00         0.00        250.00        Y      2"',
            '"     Y   1 041ม5560\xa0\xa0มือจับ                              1.00แผง             250.00                   250.00                                IV9988773-  1"',
            '',
        ],
        "scoped_regression.csv",
    )

    preview = icn.preview_credit_notes_import(path, db_path=tmp_db)
    diff = preview["cna_diff"]
    cna = preview["credit_note_amounts"]

    # Sanity: file has 3 masters
    assert cna["parsed_masters"] == 3
    assert cna["upserted"] == 3

    total_classified = diff["unchanged"] + len(diff["new"]) + len(diff["changed"])
    assert total_classified <= cna["parsed_masters"], (
        f"unchanged ({diff['unchanged']}) + new ({len(diff['new'])}) + "
        f"changed ({len(diff['changed'])}) = {total_classified} must be "
        f"≤ parsed_masters ({cna['parsed_masters']}). "
        f"Live DB has {pre_existing} pre-existing rows — diff must NOT count "
        f"any of them."
    )

    # And specifically: unchanged must not balloon to pre_existing
    assert diff["unchanged"] < pre_existing, (
        f"unchanged={diff['unchanged']} approaches pre_existing={pre_existing} "
        "→ diff is still iterating untouched rows"
    )

    # The 3 file SRs are brand-new on the live DB → expect 3 new, 0 unchanged, 0 changed
    new_keys = {r["sr_doc_base"] for r in diff["new"]}
    assert new_keys == {"SR9988771", "SR9988772", "SR9988773"}
    assert diff["unchanged"] == 0
    assert diff["changed"] == []


# ── 5: ref_conflicts surface through preview the same way ───────────────────

def test_preview_surfaces_ref_conflicts(tmp_path, tmp_db, tmp_db_conn):
    """SR exists in DB with one ref; file has a different ref → preview must
    expose the conflict (and not change the DB)."""
    import import_credit_notes as icn

    _ensure_062(tmp_db_conn)
    _seed_sr(
        tmp_db_conn,
        "SR8800005-1", "SR8800005",
        ref_invoice="IV8800501",  # DB ref
        net=300.0,
    )
    tmp_db_conn.commit()

    path = _write_cn_file(tmp_path, _SR_CONFLICT_REF, "preview_conflict.csv")
    preview = icn.preview_credit_notes_import(path, db_path=tmp_db)

    assert len(preview["ref_conflicts"]) == 1
    conflict = preview["ref_conflicts"][0]
    assert conflict["doc_no"] == "SR8800005-1"
    assert conflict["db_ref"] == "IV8800501"
    assert conflict["file_ref"] == "IV8800502"

    # DB still has the original ref (preview did not write)
    fresh = sqlite3.connect(tmp_db)
    fresh.row_factory = sqlite3.Row
    try:
        row = fresh.execute(
            "SELECT ref_invoice FROM sales_transactions WHERE doc_no='SR8800005-1'"
        ).fetchone()
    finally:
        fresh.close()
    assert row["ref_invoice"] == "IV8800501"
