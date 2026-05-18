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
