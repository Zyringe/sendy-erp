"""B2a regression: import_payments idempotency under real-world re-import scenario.

Covers the exact B2a fix: running import_payments on the same file twice must
not change total row counts or any amounts. This complements the existing
test_import_idempotent_same_file in test_payment_parse.py but exercises the
specific scenario of the B2a fix: a file that is first imported once (adding
N new records) and then imported again (must produce 0 new records, same totals).

Key invariants:
  1. imported + updated + skipped == total on every run
  2. Re-run: imported=0, updated=N (all existing), skipped=0
  3. received_payments row count unchanged after re-run
  4. paid_invoices row count unchanged after re-run
  5. SUM(received_payments.total) unchanged after re-run
  6. SUM(paid_invoices.amount) unchanged after re-run
"""
import pytest
import models


# A payment file with 3 REs and 5 IV references
# Using distinctive re_nos unlikely to collide with real DB rows
PAYMENT_LINES = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                                                                          หน้า   :        1"',
    '"  รายงานการรับชำระหนี้ เรียงตามวันที่ของใบเสร็จ"',
    '"---------------------------------------------------------------------------------------------------------------------------------"',
    '"  วันที่  เลขที่ใบเสร็จ  ชื่อลูกค้า                          พนักงานขาย     ตัดเงินมัดจำ ยอดตามใบกำกับ   ชำระเป็น ง/ส       เช็ครับ"',
    '"---------------------------------------------------------------------------------------------------------------------------------"',
    # RE6990001 — two IVs
    '"01/05/69  RE6990001    ลูกค้าทดสอบ B2A                          11                               3500.00        3500.00"',
    '"                             IV6990001    28/04/69          2000.00"',
    '"                             IV6990002    29/04/69          1500.00"',
    '',
    # RE6990002 — one IV
    '"02/05/69  RE6990002    ลูกค้าทดสอบ B2B                          11                               5000.00        5000.00"',
    '"                             IV6990003    30/04/69          5000.00"',
    '',
    # RE6990003 — two IVs (one SR netting link)
    '"03/05/69  RE6990003    ลูกค้าทดสอบ B2C                          12                               4000.00        4000.00"',
    '"                             IV6990004    01/05/69          5200.00"',
    '"                             SR6990001    01/05/69         -1200.00"',
    '',
]


@pytest.fixture
def payment_file(tmp_path):
    p = tmp_path / "b2a_regression.csv"
    p.write_text("\n".join(PAYMENT_LINES) + "\n", encoding="cp874")
    return str(p)


def _snapshot(conn):
    """Capture the full state that must be invariant across re-runs."""
    rp_count = conn.execute(
        "SELECT COUNT(*) FROM received_payments WHERE re_no LIKE 'RE6990%'"
    ).fetchone()[0]
    pi_count = conn.execute(
        """SELECT COUNT(*) FROM paid_invoices pi
           JOIN received_payments rp ON rp.id = pi.re_id
           WHERE rp.re_no LIKE 'RE6990%'"""
    ).fetchone()[0]
    rp_total_sum = conn.execute(
        "SELECT COALESCE(SUM(total), 0) FROM received_payments WHERE re_no LIKE 'RE6990%'"
    ).fetchone()[0]
    pi_amount_sum = conn.execute(
        """SELECT COALESCE(SUM(pi.amount), 0) FROM paid_invoices pi
           JOIN received_payments rp ON rp.id = pi.re_id
           WHERE rp.re_no LIKE 'RE6990%'"""
    ).fetchone()[0]
    return {
        "rp_count": rp_count,
        "pi_count": pi_count,
        "rp_total_sum": round(rp_total_sum, 2),
        "pi_amount_sum": round(pi_amount_sum, 2),
    }


def test_b2a_first_import(payment_file, tmp_db_conn):
    """First import inserts 3 new RE records and 5 IV/SR rows."""
    result = models.import_payments(payment_file)
    assert result["imported"] == 3
    assert result["skipped"] == 0
    assert result["errors"] == []
    assert result["imported"] + result["updated"] + result["skipped"] == result["total"]


def test_b2a_reimport_is_noop(payment_file, tmp_db_conn):
    """Re-importing the same file: imported=0, row counts and amounts unchanged."""
    conn = tmp_db_conn

    # First run
    r1 = models.import_payments(payment_file)
    assert r1["imported"] == 3, f"First run should import 3: {r1}"
    snap1 = _snapshot(conn)

    # Second run (B2a scenario: re-run after first import)
    r2 = models.import_payments(payment_file)
    assert r2["imported"] == 0, f"Re-run must not import new rows: {r2}"
    assert r2["updated"] == 3, f"Re-run must update all 3 existing: {r2}"
    assert r2["skipped"] == 0
    assert r2["errors"] == []
    assert r2["imported"] + r2["updated"] + r2["skipped"] == r2["total"]

    snap2 = _snapshot(conn)
    assert snap2 == snap1, (
        f"State changed after re-run:\nBefore={snap1}\nAfter={snap2}"
    )


def test_b2a_third_run_still_noop(payment_file, tmp_db_conn):
    """Three consecutive runs: state after run 1 = state after run 2 = state after run 3."""
    conn = tmp_db_conn

    models.import_payments(payment_file)
    snap1 = _snapshot(conn)

    models.import_payments(payment_file)
    snap2 = _snapshot(conn)
    assert snap2 == snap1, f"Run2 != Run1: {snap1} vs {snap2}"

    models.import_payments(payment_file)
    snap3 = _snapshot(conn)
    assert snap3 == snap1, f"Run3 != Run1: {snap1} vs {snap3}"


def test_b2a_sr_netting_link_idempotent(payment_file, tmp_db_conn):
    """SR netting link (negative amount) is stored correctly and unchanged on re-import."""
    conn = tmp_db_conn

    models.import_payments(payment_file)
    models.import_payments(payment_file)  # re-run

    sr_row = conn.execute(
        """SELECT pi.amount FROM paid_invoices pi
           JOIN received_payments rp ON rp.id = pi.re_id
           WHERE rp.re_no = 'RE6990003' AND pi.doc_no = 'SR6990001'"""
    ).fetchone()
    assert sr_row is not None, "SR netting link must persist across re-runs"
    assert sr_row["amount"] == pytest.approx(-1200.00), (
        f"SR amount must be -1200, got {sr_row['amount']}"
    )


def test_b2a_count_invariant_holds(payment_file, tmp_db_conn):
    """imported + updated + skipped == total must hold on every run."""
    for _ in range(3):
        result = models.import_payments(payment_file)
        assert result["imported"] + result["updated"] + result["skipped"] == result["total"], (
            f"Count invariant violated: {result}"
        )
