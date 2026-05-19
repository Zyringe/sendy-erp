"""
TDD tests for inventory_app/import_credit_notes.py

Covers:
  1. Existing SR (doc_no in sales_transactions) with NULL ref → ref backfilled
  2. Same import re-run → idempotent (no second backfill, counts stable)
  3. Brand-new SR not in sales_transactions → recorded in credit_note_imports
  4. Re-run of new SR → already_new, no duplicate row in credit_note_imports
  5. Existing SR where DB ref is already set to SAME value → not counted as backfill
  6. Existing SR where DB ref conflicts with file ref → NOT overwritten, logged in ref_conflicts
  7. Σ SR net in sales_transactions unchanged after any import run
  8. SR with NULL ref in both DB and file → no backfill, no error
  9. Full summary-dict invariant: existing_matched + new_recorded + already_new + skipped == parsed

All tests use the `tmp_db` / `tmp_db_conn` fixtures from conftest.py (copy of
live DB + monkeypatched config.DATABASE_PATH).  The `empty_db` / `empty_db_conn`
fixture is used for tests that need a clean slate.

cp874 fixture lines follow the style of test_credit_notes_parse.py.
"""
import sqlite3
import pytest

# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_cn_file(tmp_path, lines, filename="ใบลดหนี้_test.csv"):
    """Write a cp874-encoded credit-note fixture file."""
    p = tmp_path / filename
    p.write_text("\n".join(lines) + "\n", encoding="cp874")
    return str(p)


# ── Synthetic cp874 fixture lines ────────────────────────────────────────────
#
# Header shared by all fixtures (BSN report header).
_CN_HEADER = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                                                                                                      หน้า   :        1"',
    '"  รายงานใบลดหนี้/รับคืนสินค้า\xa0เรียงตามเลขที่"',
    '"---------------------------------------------------------------------------------------------------------------------------------------------------------------"',
    '"   เลขที่       วันที่   ลูกค้า                               พนักงานขาย\xa0\xa0อ้างถึงใบกำกับ\xa0\xa0V  ส่วนลด     มูลค่าสินค้า     VAT.       รวมทั้งสิ้น ตัดหนี้แล้ว\xa0ประเภท"',
    '"---------------------------------------------------------------------------------------------------------------------------------------------------------------"',
]

# SR8800001: one detail row, ref_invoice=IV8800100
# Used to represent an SR that DOES exist in sales_transactions (we seed it below)
# and whose ref_invoice we want to backfill.
_SR_WITH_REF = _CN_HEADER + [
    '"  SR8800001    08/01/67  ร้านทดสอบA                           06         IV8800100    1                  1000.00         0.00       1000.00        Y      2"',
    '"     Y   1 041ม5560\xa0\xa0มือจับ(P)#555-350มิล.              2.00แผง             500.00                  1000.00                                IV8800100-  1"',
    '',
]

# SR8800002: ref_invoice=None in the file (master has no ref col)
_SR_NO_REF = _CN_HEADER + [
    '"  SR8800002    09/01/67  ร้านทดสอบB                           06                      1                   500.00         0.00        500.00        Y      2"',
    '"     Y   1 044ล0700\xa0\xa0ลูกบิด\xa0#700(P)\xa0SS                  1.00แผง             500.00                   500.00                                IV0000000-  1"',
    '',
]

# SR8800003: brand new — NOT seeded in sales_transactions
_SR_NEW = _CN_HEADER + [
    '"  SR8800003    10/01/67  ร้านทดสอบC                           31         IV8800300    1                   750.00         0.00        750.00        Y      2"',
    '"     Y   1 031บ4124\xa0\xa0ใบตัดเพชร\xa04.5"                   3.00ใบ              250.00                   750.00                                IV8800300-  1"',
    '',
]

# SR8800004: in DB with SAME ref already set (IV8800400 == IV8800400)
_SR_ALREADY_REF = _CN_HEADER + [
    '"  SR8800004    11/01/67  ร้านทดสอบD                           06         IV8800400    1                   200.00         0.00        200.00        Y      2"',
    '"     Y   1 553ด5118\xa0\xa0ดจ.แสตนเลส                          1.00ดอก             200.00                   200.00                                IV8800400-  1"',
    '',
]

# SR8800005: in DB with DIFFERENT ref (DB=IV8800501, file=IV8800502) → conflict
_SR_CONFLICT_REF = _CN_HEADER + [
    '"  SR8800005    12/01/67  ร้านทดสอบE                           06         IV8800502    1                   300.00         0.00        300.00        Y      2"',
    '"     Y   1 900พ1000\xa0\xa0สินค้าทดสอบ                         1.00ตัว             300.00                   300.00                                IV8800502-  1"',
    '',
]

# Combined fixture: SR8800001 (backfill) + SR8800003 (new) in one file
_SR_MIXED = _CN_HEADER + [
    '"  SR8800001    08/01/67  ร้านทดสอบA                           06         IV8800100    1                  1000.00         0.00       1000.00        Y      2"',
    '"     Y   1 041ม5560\xa0\xa0มือจับ(P)#555-350มิล.              2.00แผง             500.00                  1000.00                                IV8800100-  1"',
    '',
    '"  SR8800003    10/01/67  ร้านทดสอบC                           31         IV8800300    1                   750.00         0.00        750.00        Y      2"',
    '"     Y   1 031บ4124\xa0\xa0ใบตัดเพชร\xa04.5"                   3.00ใบ              250.00                   750.00                                IV8800300-  1"',
    '',
]


# ── Seed helpers ──────────────────────────────────────────────────────────────

def _seed_sr(conn, doc_no, doc_base, ref_invoice, net=1000.0):
    """Insert a minimal SR row into sales_transactions."""
    conn.execute(
        """INSERT INTO sales_transactions
               (date_iso, doc_no, doc_base, customer, qty, net, vat_type, ref_invoice,
                discount, total, unit_price, synced_to_stock)
           VALUES ('2024-01-08', ?, ?, 'ร้านทดสอบ', 1.0, ?, 1, ?,
                   '', ?, 0.0, 0)""",
        (doc_no, doc_base, net, ref_invoice, net)
    )
    conn.commit()


def _sr_ref(conn, doc_no):
    """Return the ref_invoice for a given doc_no from sales_transactions."""
    row = conn.execute(
        "SELECT ref_invoice FROM sales_transactions WHERE doc_no=?", (doc_no,)
    ).fetchone()
    return row["ref_invoice"] if row else None


def _sr_net_sum(conn):
    """Sum of net for all SR rows in sales_transactions."""
    return conn.execute(
        "SELECT COALESCE(SUM(net),0) FROM sales_transactions WHERE doc_base LIKE 'SR%'"
    ).fetchone()[0]


def _sr_count(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM sales_transactions WHERE doc_base LIKE 'SR%'"
    ).fetchone()[0]


def _cni_count(conn):
    return conn.execute("SELECT COUNT(*) FROM credit_note_imports").fetchone()[0]


# ── Test 1: NULL ref in DB → backfilled from file ─────────────────────────────

def test_ref_backfilled_when_db_has_null(tmp_path, tmp_db_conn):
    """SR exists in sales_transactions with NULL ref; file provides IV → backfilled."""
    import import_credit_notes as icn

    conn = tmp_db_conn
    _seed_sr(conn, "SR8800001-1", "SR8800001", ref_invoice=None, net=1000.0)
    assert _sr_ref(conn, "SR8800001-1") is None

    path = _write_cn_file(tmp_path, _SR_WITH_REF)
    result = icn.import_credit_notes(path, conn=conn)
    conn.commit()

    assert result["refs_backfilled"] == 1
    assert result["existing_matched"] == 1
    assert result["new_recorded"] == 0
    assert result["errors"] == []
    assert _sr_ref(conn, "SR8800001-1") == "IV8800100"


# ── Test 2: Idempotency — re-run same file, backfill already done ────────────

def test_idempotent_second_run_no_double_backfill(tmp_path, tmp_db_conn):
    """Running the same file a second time: refs_backfilled=0 (already done)."""
    import import_credit_notes as icn

    conn = tmp_db_conn
    _seed_sr(conn, "SR8800001-1", "SR8800001", ref_invoice=None, net=1000.0)

    path = _write_cn_file(tmp_path, _SR_WITH_REF)

    result1 = icn.import_credit_notes(path, conn=conn)
    conn.commit()
    assert result1["refs_backfilled"] == 1

    # Run 2
    result2 = icn.import_credit_notes(path, conn=conn)
    conn.commit()
    assert result2["refs_backfilled"] == 0, (
        f"Run2 should not re-backfill; got refs_backfilled={result2['refs_backfilled']}"
    )
    assert result2["existing_matched"] == 1
    # ref unchanged
    assert _sr_ref(conn, "SR8800001-1") == "IV8800100"


# ── Test 3: Brand-new SR → recorded in credit_note_imports ────────────────────

def test_new_sr_goes_to_side_table(tmp_path, tmp_db_conn):
    """SR not in sales_transactions → inserted into credit_note_imports, not sales_transactions."""
    import import_credit_notes as icn

    conn = tmp_db_conn
    # Do NOT seed SR8800003 in sales_transactions
    net_before = _sr_net_sum(conn)
    count_before = _sr_count(conn)

    path = _write_cn_file(tmp_path, _SR_NEW)
    result = icn.import_credit_notes(path, conn=conn)
    conn.commit()

    assert result["new_recorded"] == 1
    assert result["existing_matched"] == 0
    assert result["errors"] == []

    # sales_transactions completely unchanged
    assert _sr_count(conn) == count_before, "No new rows inserted into sales_transactions"
    assert _sr_net_sum(conn) == pytest.approx(net_before), "Σ net unchanged"

    # Side table has the new row
    assert _cni_count(conn) >= 1
    row = conn.execute(
        "SELECT * FROM credit_note_imports WHERE doc_no='SR8800003-1'"
    ).fetchone()
    assert row is not None
    assert row["ref_invoice"] == "IV8800300"
    assert row["net"] == pytest.approx(750.0)


# ── Test 4: Re-run of new SR → already_new, no duplicate ─────────────────────

def test_new_sr_idempotent_second_run(tmp_path, tmp_db_conn):
    """Re-running a file with a new SR: second run produces already_new=1, no dup row."""
    import import_credit_notes as icn

    conn = tmp_db_conn
    path = _write_cn_file(tmp_path, _SR_NEW)

    result1 = icn.import_credit_notes(path, conn=conn)
    conn.commit()
    assert result1["new_recorded"] == 1

    cni_before = _cni_count(conn)

    result2 = icn.import_credit_notes(path, conn=conn)
    conn.commit()

    assert result2["new_recorded"] == 0
    assert result2["already_new"] == 1
    # No duplicate row added
    assert _cni_count(conn) == cni_before, "credit_note_imports row count must not increase"


# ── Test 5: DB ref already set to same value → not counted as backfill ────────

def test_existing_ref_same_value_not_double_counted(tmp_path, tmp_db_conn):
    """If DB ref == file ref, refs_backfilled stays 0."""
    import import_credit_notes as icn

    conn = tmp_db_conn
    _seed_sr(conn, "SR8800004-1", "SR8800004", ref_invoice="IV8800400", net=200.0)

    path = _write_cn_file(tmp_path, _SR_ALREADY_REF)
    result = icn.import_credit_notes(path, conn=conn)
    conn.commit()

    assert result["refs_backfilled"] == 0
    assert result["existing_matched"] == 1
    assert _sr_ref(conn, "SR8800004-1") == "IV8800400"


# ── Test 6: Ref conflict (DB≠file, both non-null) → logged, DB unchanged ──────

def test_ref_conflict_logged_not_overwritten(tmp_path, tmp_db_conn):
    """DB has IV8800501, file has IV8800502 → conflict logged, DB not changed."""
    import import_credit_notes as icn

    conn = tmp_db_conn
    _seed_sr(conn, "SR8800005-1", "SR8800005", ref_invoice="IV8800501", net=300.0)

    path = _write_cn_file(tmp_path, _SR_CONFLICT_REF)
    result = icn.import_credit_notes(path, conn=conn)
    conn.commit()

    assert result["refs_backfilled"] == 0
    assert len(result["ref_conflicts"]) == 1
    conflict = result["ref_conflicts"][0]
    assert conflict["doc_no"] == "SR8800005-1"
    assert conflict["db_ref"] == "IV8800501"
    assert conflict["file_ref"] == "IV8800502"
    # DB unchanged
    assert _sr_ref(conn, "SR8800005-1") == "IV8800501"


# ── Test 7: Σ SR net in sales_transactions never increases from import ─────────

def test_sr_net_sum_unchanged_by_import(tmp_path, tmp_db_conn):
    """Importing the mixed file (1 backfill + 1 new to side table)
    must not change Σ net in sales_transactions."""
    import import_credit_notes as icn

    conn = tmp_db_conn
    _seed_sr(conn, "SR8800001-1", "SR8800001", ref_invoice=None, net=1000.0)

    net_before = _sr_net_sum(conn)

    path = _write_cn_file(tmp_path, _SR_MIXED)
    result = icn.import_credit_notes(path, conn=conn)
    conn.commit()

    assert result["errors"] == []
    assert _sr_net_sum(conn) == pytest.approx(net_before), (
        f"Σ net changed! before={net_before} after={_sr_net_sum(conn)}"
    )


# ── Test 8: NULL ref in both DB and file → no backfill, no error ───────────────

def test_both_null_ref_no_backfill(tmp_path, tmp_db_conn):
    """DB ref=NULL, file ref=None → refs_backfilled=0, no error."""
    import import_credit_notes as icn

    conn = tmp_db_conn
    _seed_sr(conn, "SR8800002-1", "SR8800002", ref_invoice=None, net=500.0)

    path = _write_cn_file(tmp_path, _SR_NO_REF)
    result = icn.import_credit_notes(path, conn=conn)
    conn.commit()

    assert result["refs_backfilled"] == 0
    assert result["existing_matched"] == 1
    assert result["errors"] == []
    assert _sr_ref(conn, "SR8800002-1") is None


# ── Test 9: Summary-dict invariant ──────────────────────────────────────────────

def test_summary_invariant(tmp_path, tmp_db_conn):
    """existing_matched + new_recorded + already_new + skipped == parsed."""
    import import_credit_notes as icn

    conn = tmp_db_conn
    _seed_sr(conn, "SR8800001-1", "SR8800001", ref_invoice=None, net=1000.0)

    path = _write_cn_file(tmp_path, _SR_MIXED)
    result = icn.import_credit_notes(path, conn=conn)
    conn.commit()

    total_accounted = (
        result["existing_matched"]
        + result["new_recorded"]
        + result["already_new"]
        + result["skipped"]
    )
    assert total_accounted == result["parsed"], (
        f"Invariant broken: {total_accounted} != {result['parsed']}; result={result}"
    )


# ── Test 10: Three-run idempotency — counts and Σ net identical run2 vs run3 ──

def test_three_run_idempotency(tmp_path, tmp_db_conn):
    """Run 1 may backfill / record new; run 2 and run 3 are no-ops."""
    import import_credit_notes as icn

    conn = tmp_db_conn
    _seed_sr(conn, "SR8800001-1", "SR8800001", ref_invoice=None, net=1000.0)
    net_before = _sr_net_sum(conn)

    path = _write_cn_file(tmp_path, _SR_MIXED)

    result1 = icn.import_credit_notes(path, conn=conn)
    conn.commit()
    net_after_r1 = _sr_net_sum(conn)
    count_after_r1 = _sr_count(conn)
    cni_after_r1 = _cni_count(conn)

    result2 = icn.import_credit_notes(path, conn=conn)
    conn.commit()
    assert _sr_net_sum(conn) == pytest.approx(net_after_r1)
    assert _sr_count(conn) == count_after_r1
    assert _cni_count(conn) == cni_after_r1
    assert result2["refs_backfilled"] == 0
    assert result2["new_recorded"] == 0

    result3 = icn.import_credit_notes(path, conn=conn)
    conn.commit()
    assert _sr_net_sum(conn) == pytest.approx(net_after_r1)
    assert _sr_count(conn) == count_after_r1
    assert _cni_count(conn) == cni_after_r1
    assert result3["refs_backfilled"] == 0
    assert result3["new_recorded"] == 0

    # sales_transactions net must not have grown from the import
    assert net_after_r1 == pytest.approx(net_before), (
        "Σ SR net in sales_transactions must not increase from import"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Tests 11–17: populate_sr_writeoffs + written_off_summary
# ═══════════════════════════════════════════════════════════════════════════════
#
# Fixture topology
# ─────────────────
# SR9900001  ref→IV9900001  which EXISTS in sales_transactions  → NOT written off
# SR9900002  ref→IV9900999  which does NOT exist                → pre_system
# SR9900003  ref→NULL                                           → no_ref
#
# The "real" IV9900001 is seeded as a non-SR sales_transactions row so the
# populate logic can find it via doc_base lookup.
#
# Migration note: migration 060 (sr_writeoffs table) must be applied to the
# temp DB before any of these tests run.  tmp_db_conn copies the live DB at
# test-collection time, which may predate migration 060.  Each test calls
# _ensure_060(conn) to apply the migration DDL if the table is absent.

def _ensure_060(conn):
    """Create sr_writeoffs + indexes in the temp DB if the table doesn't exist yet.
    Applied inline rather than via run_pending_migrations to work safely against
    both tmp_db (copy of live, may predate 060) and empty_db (schema clone with
    empty applied_migrations, which triggers the bootstrap-backfill path and would
    skip running 060's DDL even when the table is absent)."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sr_writeoffs'"
    ).fetchone()
    if exists is None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sr_writeoffs (
                id              INTEGER PRIMARY KEY,
                sr_doc_base     TEXT    NOT NULL,
                sr_doc_no       TEXT    NOT NULL,
                reason          TEXT    NOT NULL CHECK(reason IN ('pre_system','no_ref')),
                ref_invoice_raw TEXT,
                net_amount      REAL    NOT NULL DEFAULT 0.0,
                customer        TEXT,
                sr_date_iso     TEXT,
                created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
                UNIQUE(sr_doc_no)
            );
            CREATE INDEX IF NOT EXISTS idx_srwo_doc_base ON sr_writeoffs(sr_doc_base);
            CREATE INDEX IF NOT EXISTS idx_srwo_reason   ON sr_writeoffs(reason);
        """)


def _seed_real_iv(conn, doc_base="IV9900001", net=500.0):
    """Seed a minimal (non-SR) invoice row so ref-lookup can find it."""
    conn.execute(
        """INSERT INTO sales_transactions
               (date_iso, doc_no, doc_base, customer, qty, net, vat_type,
                discount, total, unit_price, synced_to_stock)
           VALUES ('2024-06-01', ?, ?, 'ลูกค้าทดสอบ', 1.0, ?, 1,
                   '', ?, 0.0, 0)""",
        (doc_base + "-1", doc_base, net, net)
    )
    conn.commit()


def _seed_sr_for_writeoff(conn, doc_no, doc_base, ref_invoice, net=200.0,
                           customer="ร้านทดสอบZ"):
    """Seed an SR row with the given ref_invoice (may be None)."""
    conn.execute(
        """INSERT INTO sales_transactions
               (date_iso, doc_no, doc_base, customer, qty, net, vat_type,
                ref_invoice, discount, total, unit_price, synced_to_stock)
           VALUES ('2024-07-01', ?, ?, ?, 1.0, ?, 1, ?,
                   '', ?, 0.0, 0)""",
        (doc_no, doc_base, customer, net, ref_invoice, net)
    )
    conn.commit()


def _wo_count(conn):
    return conn.execute("SELECT COUNT(*) FROM sr_writeoffs").fetchone()[0]


def _wo_rows(conn):
    return conn.execute(
        "SELECT * FROM sr_writeoffs ORDER BY sr_doc_no"
    ).fetchall()


# ── Test 11: classification — real-IV ref excluded, pre_system + no_ref written off ──

def test_populate_classifies_correctly(empty_db_conn):
    """populate_sr_writeoffs classifies the three archetypal SR docs correctly."""
    import import_credit_notes as icn

    conn = empty_db_conn
    _ensure_060(conn)
    # IV9900001 exists → SR9900001 must NOT be written off
    _seed_real_iv(conn, "IV9900001", net=500.0)
    _seed_sr_for_writeoff(conn, "SR9900001-1", "SR9900001", "IV9900001", net=100.0)

    # IV9900999 does NOT exist → pre_system
    _seed_sr_for_writeoff(conn, "SR9900002-1", "SR9900002", "IV9900999", net=200.0)

    # NULL ref → no_ref
    _seed_sr_for_writeoff(conn, "SR9900003-1", "SR9900003", None, net=300.0)

    summary = icn.populate_sr_writeoffs(conn=conn)
    conn.commit()

    assert summary["pre_system"] == 1, f"Expected 1 pre_system; got {summary}"
    assert summary["no_ref"] == 1, f"Expected 1 no_ref; got {summary}"
    # Only 2 rows in sr_writeoffs (the excluded real-IV one must NOT appear)
    rows = _wo_rows(conn)
    assert len(rows) == 2, f"Expected 2 writeoff rows; got {len(rows)}: {[dict(r) for r in rows]}"

    reasons = {r["sr_doc_no"]: r["reason"] for r in rows}
    assert "SR9900002-1" in reasons
    assert reasons["SR9900002-1"] == "pre_system"
    assert "SR9900003-1" in reasons
    assert reasons["SR9900003-1"] == "no_ref"
    # Real-IV SR must be absent
    assert "SR9900001-1" not in reasons


# ── Test 12: excluded SR (ref → real IV) is never written off ─────────────────

def test_populate_excludes_matched_sr(empty_db_conn):
    """An SR whose ref_invoice matches a real IV row is never inserted into sr_writeoffs."""
    import import_credit_notes as icn

    conn = empty_db_conn
    _ensure_060(conn)
    _seed_real_iv(conn, "IV9900001", net=500.0)
    _seed_sr_for_writeoff(conn, "SR9900001-1", "SR9900001", "IV9900001", net=100.0)

    summary = icn.populate_sr_writeoffs(conn=conn)
    conn.commit()

    assert summary["pre_system"] == 0
    assert summary["no_ref"] == 0
    assert _wo_count(conn) == 0


# ── Test 13: idempotency — running twice produces the same rows, no duplicates ──

def test_populate_idempotent(empty_db_conn):
    """Running populate_sr_writeoffs twice: second run is a complete no-op on rows."""
    import import_credit_notes as icn

    conn = empty_db_conn
    _ensure_060(conn)
    _seed_sr_for_writeoff(conn, "SR9900002-1", "SR9900002", "IV9900999", net=200.0)
    _seed_sr_for_writeoff(conn, "SR9900003-1", "SR9900003", None, net=300.0)

    summary1 = icn.populate_sr_writeoffs(conn=conn)
    conn.commit()
    count_after_run1 = _wo_count(conn)

    summary2 = icn.populate_sr_writeoffs(conn=conn)
    conn.commit()
    count_after_run2 = _wo_count(conn)

    assert count_after_run2 == count_after_run1, (
        f"Run2 must not add rows; run1={count_after_run1}, run2={count_after_run2}"
    )
    # Summaries should reflect the same totals regardless of which run
    assert summary1["pre_system"] == summary2["pre_system"]
    assert summary1["no_ref"] == summary2["no_ref"]
    assert abs(summary1["total_net"] - summary2["total_net"]) < 0.01


# ── Test 14: net amounts stored and summed correctly ──────────────────────────

def test_populate_net_amounts(empty_db_conn):
    """net_amount on each row matches the SR's net; total_net sums both."""
    import import_credit_notes as icn

    conn = empty_db_conn
    _ensure_060(conn)
    _seed_sr_for_writeoff(conn, "SR9900002-1", "SR9900002", "IV9900999", net=250.0)
    _seed_sr_for_writeoff(conn, "SR9900003-1", "SR9900003", None, net=350.0)

    summary = icn.populate_sr_writeoffs(conn=conn)
    conn.commit()

    rows = {r["sr_doc_no"]: r for r in _wo_rows(conn)}
    assert abs(rows["SR9900002-1"]["net_amount"] - 250.0) < 0.01
    assert abs(rows["SR9900003-1"]["net_amount"] - 350.0) < 0.01
    assert abs(summary["total_net"] - 600.0) < 0.01


# ── Test 15: written_off_summary returns correct counts and Σ net ──────────────

def test_written_off_summary(empty_db_conn):
    """written_off_summary() reads sr_writeoffs and returns counts + Σ net per reason."""
    import import_credit_notes as icn

    conn = empty_db_conn
    _ensure_060(conn)
    _seed_sr_for_writeoff(conn, "SR9900002-1", "SR9900002", "IV9900999", net=200.0)
    _seed_sr_for_writeoff(conn, "SR9900003-1", "SR9900003", None, net=300.0)

    icn.populate_sr_writeoffs(conn=conn)
    conn.commit()

    s = icn.written_off_summary(conn=conn)
    assert s["pre_system"]["count"] == 1
    assert abs(s["pre_system"]["net"] - 200.0) < 0.01
    assert s["no_ref"]["count"] == 1
    assert abs(s["no_ref"]["net"] - 300.0) < 0.01
    assert s["total"]["count"] == 2
    assert abs(s["total"]["net"] - 500.0) < 0.01


# ── Test 16: written_off_summary on empty table returns zero counts ────────────

def test_written_off_summary_empty(empty_db_conn):
    """written_off_summary() returns zeroes when sr_writeoffs is empty."""
    import import_credit_notes as icn

    conn = empty_db_conn
    _ensure_060(conn)
    s = icn.written_off_summary(conn=conn)
    assert s["pre_system"]["count"] == 0
    assert s["no_ref"]["count"] == 0
    assert s["total"]["count"] == 0
    assert s["total"]["net"] == 0.0


# ── Test 17: payments_alloc AR math unchanged after populate ──────────────────

def test_populate_does_not_change_ar(empty_db_conn):
    """populate_sr_writeoffs writes to sr_writeoffs only; payments_alloc results unchanged."""
    import import_credit_notes as icn
    import payments_alloc as pa

    conn = empty_db_conn
    _ensure_060(conn)

    # Seed a real invoice + matched SR + unattributable SR, then capture AR
    _seed_real_iv(conn, "IV9900001", net=1000.0)
    _seed_sr_for_writeoff(conn, "SR9900001-1", "SR9900001", "IV9900001", net=100.0)
    _seed_sr_for_writeoff(conn, "SR9900002-1", "SR9900002", "IV9900999", net=200.0)
    _seed_sr_for_writeoff(conn, "SR9900003-1", "SR9900003", None, net=300.0)

    invs_before = pa.invoice_settlement(conn=conn)
    billed_before = round(sum(i["billed"] for i in invs_before), 2)
    cn_before = round(sum(i["credit_notes"] for i in invs_before), 2)
    outstanding_before = round(sum(i["outstanding"] for i in invs_before), 2)

    icn.populate_sr_writeoffs(conn=conn)
    conn.commit()

    invs_after = pa.invoice_settlement(conn=conn)
    billed_after = round(sum(i["billed"] for i in invs_after), 2)
    cn_after = round(sum(i["credit_notes"] for i in invs_after), 2)
    outstanding_after = round(sum(i["outstanding"] for i in invs_after), 2)

    assert billed_after == pytest.approx(billed_before), "billed must not change"
    assert cn_after == pytest.approx(cn_before), "credit_notes must not change"
    assert outstanding_after == pytest.approx(outstanding_before), "outstanding must not change"


# ═══════════════════════════════════════════════════════════════════════════════
# Tests 18–22: credit_note_amounts (migration 062) — authoritative SR credited
# value parsed from the ใบลดหนี้ master "รวมทั้งสิ้น" column.
# ═══════════════════════════════════════════════════════════════════════════════
#
# The master line's "รวมทั้งสิ้น" (post-doc-discount, post-VAT-policy) is the
# single authoritative credited figure. parse_weekly._SR_MASTER_RE captures it
# as total_amt. _upsert_credit_note_amounts() caches one row per SR doc_base in
# credit_note_amounts so payments_alloc can net the EXACT credited amount.
#
# ORACLE (from the real 18.5.69 file):
#   SR6900009  ref=IV6802996  master total = 2293.20 (detail line net is 2340.00,
#   so using the detail sum over-credits the invoice — that is the bug).

def _ensure_062(conn):
    """Apply migration 062 via the real runner against the temp DB."""
    import database
    database.run_pending_migrations(conn, verbose=False)
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='credit_note_amounts'"
    ).fetchone()
    if exists is None:
        # empty_db schema-clone path: applied_migrations empty + brands present
        # → runner takes the bootstrap-backfill branch and never executes 062's
        # DDL. Apply the DDL inline so these tests work on both fixtures.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS credit_note_amounts (
                id              INTEGER PRIMARY KEY,
                sr_doc_base     TEXT    NOT NULL,
                ref_invoice     TEXT,
                credited_amount REAL    NOT NULL DEFAULT 0.0,
                sr_date_iso     TEXT,
                customer        TEXT,
                source          TEXT,
                created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
                UNIQUE(sr_doc_base)
            );
            CREATE INDEX IF NOT EXISTS idx_cna_ref_invoice
                ON credit_note_amounts(ref_invoice);
        """)
        conn.commit()


# ── Oracle fixture: SR6900009, ref IV6802996, master total 2293.20 ────────────
_SR_ORACLE = _CN_HEADER + [
    '"  SR6900009    27/03/69  เจริญทรัพย์การค้า                    06         IV6802996    1         2%       2293.20         0.00       2293.20        Y      2"',
    '"     Y   1 614ก4220\xa0\xa0ก๊อกซิงค์(P)ผนังหางปลา#41/2        12.00แผง             195.00                  2340.00                                IV6802996-  5"',
    '',
]


def test_migration_062_table_exists(tmp_db_conn):
    """Migration 062 creates credit_note_amounts with the documented columns."""
    conn = tmp_db_conn
    _ensure_062(conn)
    cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(credit_note_amounts)"
    ).fetchall()}
    assert {"id", "sr_doc_base", "ref_invoice", "credited_amount",
            "sr_date_iso", "customer", "source", "created_at"} <= cols


def test_credit_note_amounts_oracle_SR6900009(tmp_path, tmp_db_conn):
    """ORACLE: SR6900009 → ref_invoice IV6802996, credited_amount 2293.20.

    This is the master "รวมทั้งสิ้น" value (post 2% doc discount), NOT the
    detail line net 2340.00 — that distinction is the whole point.
    """
    import import_credit_notes as icn

    conn = tmp_db_conn
    _ensure_062(conn)

    path = _write_cn_file(tmp_path, _SR_ORACLE, "oracle.csv")
    icn.import_credit_notes(path, conn=conn)
    conn.commit()

    row = conn.execute(
        "SELECT * FROM credit_note_amounts WHERE sr_doc_base='SR6900009'"
    ).fetchone()
    assert row is not None, "SR6900009 not cached in credit_note_amounts"
    assert row["ref_invoice"] == "IV6802996"
    assert row["credited_amount"] == pytest.approx(2293.20)
    assert row["credited_amount"] != pytest.approx(2340.00), (
        "must be the master total, not the detail-line net"
    )
    # BE 27/03/69 → 2569 - 543 = CE 2026 (parse_weekly._be_to_iso)
    assert row["sr_date_iso"] == "2026-03-27"
    assert row["customer"] == "เจริญทรัพย์การค้า"


def test_credit_note_amounts_idempotent(tmp_path, tmp_db_conn):
    """Re-import: one row per SR doc_base, value identical, no duplicates."""
    import import_credit_notes as icn

    conn = tmp_db_conn
    _ensure_062(conn)
    path = _write_cn_file(tmp_path, _SR_ORACLE, "oracle_idem.csv")

    icn.import_credit_notes(path, conn=conn)
    conn.commit()
    icn.import_credit_notes(path, conn=conn)
    conn.commit()

    rows = conn.execute(
        "SELECT * FROM credit_note_amounts WHERE sr_doc_base='SR6900009'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["credited_amount"] == pytest.approx(2293.20)


def test_credit_note_amounts_multi_sr_one_row_each(tmp_path, tmp_db_conn):
    """A file with several SR masters → one credit_note_amounts row per SR."""
    import import_credit_notes as icn

    conn = tmp_db_conn
    _ensure_062(conn)

    multi = _CN_HEADER + [
        '"  SR8800001    08/01/67  ร้านทดสอบA                           06         IV8800100    1                  1000.00         0.00       1000.00        Y      2"',
        '"     Y   1 041ม5560\xa0\xa0มือจับ(P)#555-350มิล.              2.00แผง             500.00                  1000.00                                IV8800100-  1"',
        '',
        '"  SR8800003    10/01/67  ร้านทดสอบC                           31         IV8800300    1                   750.00         0.00        750.00        Y      2"',
        '"     Y   1 031บ4124\xa0\xa0ใบตัดเพชร\xa04.5"                   3.00ใบ              250.00                   750.00                                IV8800300-  1"',
        '',
    ]
    path = _write_cn_file(tmp_path, multi, "multi.csv")
    icn.import_credit_notes(path, conn=conn)
    conn.commit()

    rows = {r["sr_doc_base"]: r for r in conn.execute(
        "SELECT * FROM credit_note_amounts WHERE sr_doc_base IN ('SR8800001','SR8800003')"
    ).fetchall()}
    assert rows["SR8800001"]["credited_amount"] == pytest.approx(1000.0)
    assert rows["SR8800001"]["ref_invoice"] == "IV8800100"
    assert rows["SR8800003"]["credited_amount"] == pytest.approx(750.0)
    assert rows["SR8800003"]["ref_invoice"] == "IV8800300"


def test_credit_note_amounts_does_not_touch_sales_transactions(tmp_path, tmp_db_conn):
    """Caching credited amounts must not mutate sales_transactions Σ net."""
    import import_credit_notes as icn

    conn = tmp_db_conn
    _ensure_062(conn)
    _seed_sr(conn, "SR8800001-1", "SR8800001", ref_invoice=None, net=1000.0)
    net_before = _sr_net_sum(conn)

    path = _write_cn_file(tmp_path, _SR_MIXED, "cna_mixed.csv")
    icn.import_credit_notes(path, conn=conn)
    conn.commit()

    assert _sr_net_sum(conn) == pytest.approx(net_before)
