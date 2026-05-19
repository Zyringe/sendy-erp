"""
Tests for models.parse_payment_csv — การรับชำระหนี้ (AR) cp874 parser.
"""
import models


PAYMENT_SAMPLE_LINES = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                                                                          หน้า   :        1"',
    '"  รายงานการรับชำระหนี้ เรียงตามวันที่ของใบเสร็จ"',
    '"---------------------------------------------------------------------------------------------------------------------------------"',
    '"  วันที่  เลขที่ใบเสร็จ  ชื่อลูกค้า                          พนักงานขาย     ตัดเงินมัดจำ ยอดตามใบกำกับ   ชำระเป็น ง/ส       เช็ครับ"',
    '"---------------------------------------------------------------------------------------------------------------------------------"',
    # Standard salesperson code (digits only) — works with original regex
    '"03/01/67  RE6700001    สหภัณฑ์เคหะกิจ (V)                       06                               7524.99        7524.99"',
    '"                             IV6602085    12/09/66          4368.49"',
    '"                             IV6602095    13/09/66          3156.50"',
    '"                     หมายเหตุ:"',
    '"                        โอน BSN 27/12/66"',
    '',
    # Hyphenated salesperson code "06-L" — was the bug; should now parse
    '"22/01/67  RE6700064    มหาชัยวัสดุ (MAHAXAY TRADING)            06-L                            25972.00       25972.00"',
    '"                             IV6700123    20/01/67         25972.00"',
    '',
    # Cancelled record (asterisk prefix on RE no)
    '"05/02/67  *RE6700100    เลิกสัญญา                                06                                 100.00         100.00"',
    '"                             IV6700200    01/02/67           100.00"',
    '',
]


import pytest


@pytest.fixture
def sample_payment_file(tmp_path):
    p = tmp_path / "การรับชำระหนี้_sample.csv"
    p.write_text("\n".join(PAYMENT_SAMPLE_LINES) + "\n", encoding="cp874")
    return str(p)


def test_parse_payment_basic(sample_payment_file):
    records = models.parse_payment_csv(sample_payment_file)
    re_nos = [r["re_no"] for r in records]
    assert "RE6700001" in re_nos
    assert "RE6700064" in re_nos  # hyphenated salesperson — was the bug
    assert "RE6700100" in re_nos


def test_parse_payment_hyphenated_salesperson(sample_payment_file):
    """Salesperson code can be '06-L' (hyphen). Old regex required \\w+, missed it."""
    records = models.parse_payment_csv(sample_payment_file)
    by_re = {r["re_no"]: r for r in records}
    assert by_re["RE6700064"]["salesperson"] == "06-L"
    assert by_re["RE6700064"]["customer"] == "มหาชัยวัสดุ (MAHAXAY TRADING)"
    assert by_re["RE6700064"]["date_iso"] == "2024-01-22"


def test_parse_payment_cancelled_flag(sample_payment_file):
    """RE no with leading * marks cancelled receipts."""
    records = models.parse_payment_csv(sample_payment_file)
    by_re = {r["re_no"]: r for r in records}
    assert by_re["RE6700100"]["cancelled"] is True
    assert by_re["RE6700001"]["cancelled"] is False


def test_parse_payment_iv_list(sample_payment_file):
    """iv_list items are dicts with iv_no and amount keys."""
    records = models.parse_payment_csv(sample_payment_file)
    by_re = {r["re_no"]: r for r in records}
    # Shape: list of dicts
    ivs_001 = by_re["RE6700001"]["iv_list"]
    assert [iv["iv_no"] for iv in ivs_001] == ["IV6602085", "IV6602095"]
    ivs_064 = by_re["RE6700064"]["iv_list"]
    assert [iv["iv_no"] for iv in ivs_064] == ["IV6700123"]


# ── NEW tests (write first — RED, then implement → GREEN) ────────────────────

# --- Fixture for amount-capture tests ---

PAYMENT_AMOUNT_LINES = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                                                                          หน้า   :        1"',
    '"  รายงานการรับชำระหนี้ เรียงตามวันที่ของใบเสร็จ"',
    '"---------------------------------------------------------------------------------------------------------------------------------"',
    '"  วันที่  เลขที่ใบเสร็จ  ชื่อลูกค้า                          พนักงานขาย     ตัดเงินมัดจำ ยอดตามใบกำกับ   ชำระเป็น ง/ส       เช็ครับ"',
    '"---------------------------------------------------------------------------------------------------------------------------------"',
    # RE with one IV — plain amount no comma
    '"15/05/67  RE6700500    ร้านวัสดุก่อสร้างดี                      08                              25972.00       25972.00"',
    '"                             IV6700400    10/05/67         25972.00"',
    '',
    # RE with two IVs — one has thousands comma (1,234.56), one plain
    '"20/05/67  RE6700501    ร้านค้าทดสอบ                             09                               2734.56        2734.56"',
    '"                             IV6700401    15/05/67          1,234.56"',
    '"                             IV6700402    16/05/67          1500.00"',
    '',
    # Cancelled RE — amount still captured
    '"22/05/67  *RE6700502    ลูกค้ายกเลิก                             08                                 500.00         500.00"',
    '"                             IV6700403    18/05/67           500.00"',
    '',
]


@pytest.fixture
def amount_payment_file(tmp_path):
    p = tmp_path / "payment_amounts.csv"
    p.write_text("\n".join(PAYMENT_AMOUNT_LINES) + "\n", encoding="cp874")
    return str(p)


def test_iv_amount_no_comma(amount_payment_file):
    """Single IV without thousands comma — amount parsed to float."""
    records = models.parse_payment_csv(amount_payment_file)
    by_re = {r["re_no"]: r for r in records}
    iv = by_re["RE6700500"]["iv_list"][0]
    assert iv["iv_no"] == "IV6700400"
    assert iv["amount"] == pytest.approx(25972.00)


def test_iv_amount_thousands_comma(amount_payment_file):
    """IV amount with thousands comma (1,234.56) — comma stripped, parsed to float."""
    records = models.parse_payment_csv(amount_payment_file)
    by_re = {r["re_no"]: r for r in records}
    ivs = by_re["RE6700501"]["iv_list"]
    assert ivs[0]["iv_no"] == "IV6700401"
    assert ivs[0]["amount"] == pytest.approx(1234.56)
    assert ivs[1]["iv_no"] == "IV6700402"
    assert ivs[1]["amount"] == pytest.approx(1500.00)


def test_re_total_equals_sum_of_iv_amounts(amount_payment_file):
    """RE total = sum of its IV amounts (sum-of-IVs is the source of truth)."""
    records = models.parse_payment_csv(amount_payment_file)
    by_re = {r["re_no"]: r for r in records}

    r500 = by_re["RE6700500"]
    assert r500["total"] == pytest.approx(25972.00)

    r501 = by_re["RE6700501"]
    assert r501["total"] == pytest.approx(1234.56 + 1500.00)  # 2734.56


def test_cancelled_re_amount_captured(amount_payment_file):
    """Cancelled RE: amount still parsed; cancelled flag True."""
    records = models.parse_payment_csv(amount_payment_file)
    by_re = {r["re_no"]: r for r in records}
    r = by_re["RE6700502"]
    assert r["cancelled"] is True
    assert r["iv_list"][0]["amount"] == pytest.approx(500.00)
    assert r["total"] == pytest.approx(500.00)


def test_buddhist_era_date_conversion_still_correct(amount_payment_file):
    """BE → CE date conversion: 67 → 2024 (2567 - 543)."""
    records = models.parse_payment_csv(amount_payment_file)
    by_re = {r["re_no"]: r for r in records}
    assert by_re["RE6700500"]["date_iso"] == "2024-05-15"
    assert by_re["RE6700501"]["date_iso"] == "2024-05-20"


# --- Import-level tests using tmp_db_conn ---

def _build_payment_file(tmp_path, lines, filename="pay.csv"):
    p = tmp_path / filename
    p.write_text("\n".join(lines) + "\n", encoding="cp874")
    return str(p)


IMPORT_LINES_V1 = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                                                                          หน้า   :        1"',
    '"  รายงานการรับชำระหนี้ เรียงตามวันที่ของใบเสร็จ"',
    '"---------------------------------------------------------------------------------------------------------------------------------"',
    '"  วันที่  เลขที่ใบเสร็จ  ชื่อลูกค้า                          พนักงานขาย     ตัดเงินมัดจำ ยอดตามใบกำกับ   ชำระเป็น ง/ส       เช็ครับ"',
    '"---------------------------------------------------------------------------------------------------------------------------------"',
    '"10/06/67  RE6799001    ลูกค้าทดสอบนำเข้า                        11                               3000.00        3000.00"',
    '"                             IV6799001    05/06/67          1500.00"',
    '"                             IV6799002    06/06/67          1500.00"',
    '',
    '"11/06/67  RE6799002    ลูกค้าสอง                                11                               2000.00        2000.00"',
    '"                             IV6799003    08/06/67          2000.00"',
    '',
]

# V2: same REs, but IV6799001 amount changed from 1500 → 1750 (RE total changes too)
IMPORT_LINES_V2 = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                                                                          หน้า   :        1"',
    '"  รายงานการรับชำระหนี้ เรียงตามวันที่ของใบเสร็จ"',
    '"---------------------------------------------------------------------------------------------------------------------------------"',
    '"  วันที่  เลขที่ใบเสร็จ  ชื่อลูกค้า                          พนักงานขาย     ตัดเงินมัดจำ ยอดตามใบกำกับ   ชำระเป็น ง/ส       เช็ครับ"',
    '"---------------------------------------------------------------------------------------------------------------------------------"',
    '"10/06/67  RE6799001    ลูกค้าทดสอบนำเข้า                        11                               3250.00        3250.00"',
    '"                             IV6799001    05/06/67          1750.00"',
    '"                             IV6799002    06/06/67          1500.00"',
    '',
    '"11/06/67  RE6799002    ลูกค้าสอง                                11                               2000.00        2000.00"',
    '"                             IV6799003    08/06/67          2000.00"',
    '',
]


def test_import_amounts_stored(tmp_path, tmp_db_conn):
    """import_payments stores paid_invoices.amount and received_payments.total."""
    import models as m
    path = _build_payment_file(tmp_path, IMPORT_LINES_V1)
    result = m.import_payments(path)

    assert result["total"] == 2
    assert result["imported"] == 2

    conn = tmp_db_conn
    # received_payments totals
    rows = conn.execute(
        "SELECT re_no, total FROM received_payments WHERE re_no IN ('RE6799001','RE6799002') ORDER BY re_no"
    ).fetchall()
    by_re = {r["re_no"]: r["total"] for r in rows}
    assert by_re["RE6799001"] == pytest.approx(3000.00)
    assert by_re["RE6799002"] == pytest.approx(2000.00)

    # paid_invoices amounts
    ivs = conn.execute(
        """SELECT pi.iv_no, pi.amount
           FROM paid_invoices pi
           JOIN received_payments rp ON rp.id = pi.re_id
           WHERE rp.re_no IN ('RE6799001','RE6799002')
           ORDER BY pi.iv_no"""
    ).fetchall()
    iv_map = {r["iv_no"]: r["amount"] for r in ivs}
    assert iv_map["IV6799001"] == pytest.approx(1500.00)
    assert iv_map["IV6799002"] == pytest.approx(1500.00)
    assert iv_map["IV6799003"] == pytest.approx(2000.00)


def test_import_idempotent_same_file(tmp_path, tmp_db_conn):
    """Re-importing the same file: row counts unchanged, amounts unchanged."""
    import models as m
    path = _build_payment_file(tmp_path, IMPORT_LINES_V1)

    result1 = m.import_payments(path)
    result2 = m.import_payments(path)

    # second import: all 2 REs already exist → 0 inserted, 2 skipped
    # (upsert ON CONFLICT: re_no unique → overwrites with same values — no new rows)
    conn = tmp_db_conn
    re_count = conn.execute(
        "SELECT COUNT(*) FROM received_payments WHERE re_no IN ('RE6799001','RE6799002')"
    ).fetchone()[0]
    iv_count = conn.execute(
        """SELECT COUNT(*) FROM paid_invoices pi
           JOIN received_payments rp ON rp.id = pi.re_id
           WHERE rp.re_no IN ('RE6799001','RE6799002')"""
    ).fetchone()[0]
    assert re_count == 2
    assert iv_count == 3

    # Amounts unchanged after second import
    rows = conn.execute(
        "SELECT re_no, total FROM received_payments WHERE re_no IN ('RE6799001','RE6799002') ORDER BY re_no"
    ).fetchall()
    by_re = {r["re_no"]: r["total"] for r in rows}
    assert by_re["RE6799001"] == pytest.approx(3000.00)

    assert result1["total"] == 2
    assert result2["total"] == 2


def test_import_upsert_updates_changed_amount(tmp_path, tmp_db_conn):
    """Re-import with modified IV amount → that row updated in place, no duplicates."""
    import models as m

    path_v1 = _build_payment_file(tmp_path, IMPORT_LINES_V1, "pay_v1.csv")
    path_v2 = _build_payment_file(tmp_path, IMPORT_LINES_V2, "pay_v2.csv")

    m.import_payments(path_v1)
    m.import_payments(path_v2)

    conn = tmp_db_conn

    # No duplicate rows
    re_count = conn.execute(
        "SELECT COUNT(*) FROM received_payments WHERE re_no IN ('RE6799001','RE6799002')"
    ).fetchone()[0]
    assert re_count == 2

    iv_count = conn.execute(
        """SELECT COUNT(*) FROM paid_invoices pi
           JOIN received_payments rp ON rp.id = pi.re_id
           WHERE rp.re_no IN ('RE6799001','RE6799002')"""
    ).fetchone()[0]
    assert iv_count == 3

    # IV6799001 amount updated to 1750
    row = conn.execute(
        """SELECT pi.amount FROM paid_invoices pi
           JOIN received_payments rp ON rp.id = pi.re_id
           WHERE rp.re_no = 'RE6799001' AND pi.iv_no = 'IV6799001'"""
    ).fetchone()
    assert row["amount"] == pytest.approx(1750.00)

    # IV6799002 unchanged
    row2 = conn.execute(
        """SELECT pi.amount FROM paid_invoices pi
           JOIN received_payments rp ON rp.id = pi.re_id
           WHERE rp.re_no = 'RE6799001' AND pi.iv_no = 'IV6799002'"""
    ).fetchone()
    assert row2["amount"] == pytest.approx(1500.00)

    # RE total updated for RE6799001
    re_row = conn.execute(
        "SELECT total FROM received_payments WHERE re_no = 'RE6799001'"
    ).fetchone()
    assert re_row["total"] == pytest.approx(3250.00)


# ── Regression tests for stale-lastrowid / wrong-re_id bug ──────────────────
#
# These tests exercise the case where:
#   - Legacy received_payments rows already exist (pre-migration, amounts NULL)
#   - A fresh import tries to upsert them AND insert brand-new REs in the same run
#   - In the buggy code, `cur.lastrowid` is stale after an ON CONFLICT...DO UPDATE,
#     so paid_invoices rows are linked to the WRONG re_id (the last INSERT's id
#     instead of the row whose re_no matches this record).
#
# The tests MUST FAIL on the current code and pass after the fix.

# ── Helpers ───────────────────────────────────────────────────────────────────

def _seed_legacy_rows(conn):
    """Insert legacy received_payments + paid_invoices rows with NULL amounts.

    This simulates the pre-migration-058 state: rows imported before amount
    columns existed.  The paid_invoices.amount = NULL, received_payments.total = NULL.

    RE8801 id will be 1 (or some value); RE8802 id will be 2.
    We return the real ids so tests can assert against them.
    """
    conn.execute(
        "INSERT INTO received_payments (re_no, date_iso, customer, salesperson, cancelled)"
        " VALUES ('RE6788801', '2024-01-10', 'ลูกค้าเก่าA', '05', 0)"
    )
    id_8801 = conn.execute(
        "SELECT id FROM received_payments WHERE re_no='RE6788801'"
    ).fetchone()[0]

    conn.execute(
        "INSERT INTO paid_invoices (re_id, iv_no, amount) VALUES (?, 'IV6788801', NULL)",
        (id_8801,)
    )
    conn.execute(
        "INSERT INTO paid_invoices (re_id, iv_no, amount) VALUES (?, 'IV6788802', NULL)",
        (id_8801,)
    )

    conn.execute(
        "INSERT INTO received_payments (re_no, date_iso, customer, salesperson, cancelled)"
        " VALUES ('RE6788802', '2024-01-11', 'ลูกค้าเก่าB', '05', 0)"
    )
    id_8802 = conn.execute(
        "SELECT id FROM received_payments WHERE re_no='RE6788802'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO paid_invoices (re_id, iv_no, amount) VALUES (?, 'IV6788803', NULL)",
        (id_8802,)
    )
    conn.commit()
    return id_8801, id_8802


# CSV lines for the regression tests.
# RE6788801 (legacy, already in DB with NULL amounts) comes AFTER a brand-new RE
# so that the new-RE INSERT primes lastrowid to a stale value.
# RE6788803 is a second existing RE with two IVs.
REGRESSION_LINES = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                                                                          หน้า   :        1"',
    '"  รายงานการรับชำระหนี้ เรียงตามวันที่ของใบเสร็จ"',
    '"---------------------------------------------------------------------------------------------------------------------------------"',
    '"  วันที่  เลขที่ใบเสร็จ  ชื่อลูกค้า                          พนักงานขาย     ตัดเงินมัดจำ ยอดตามใบกำกับ   ชำระเป็น ง/ส       เช็ครับ"',
    '"---------------------------------------------------------------------------------------------------------------------------------"',
    # BRAND-NEW RE — this INSERT primes lastrowid to a high value
    '"05/01/67  RE6799901    ลูกค้าใหม่สด                             12                               5000.00        5000.00"',
    '"                             IV6799901    01/01/67          5000.00"',
    '',
    # EXISTING legacy RE — UPSERT takes UPDATE path; stale lastrowid would be RE6799901's id
    '"10/01/67  RE6788801    ลูกค้าเก่าA                              05                               3700.00        3700.00"',
    '"                             IV6788801    05/01/67          2000.00"',
    '"                             IV6788802    06/01/67          1700.00"',
    '',
    # EXISTING legacy RE (2nd) — also UPDATE path
    '"11/01/67  RE6788802    ลูกค้าเก่าB                              05                               4200.00        4200.00"',
    '"                             IV6788803    07/01/67          4200.00"',
    '',
]


@pytest.fixture
def regression_payment_file(tmp_path):
    p = tmp_path / "regression_pay.csv"
    p.write_text("\n".join(REGRESSION_LINES) + "\n", encoding="cp874")
    return str(p)


def test_existing_re_reimport_idempotency(tmp_path, tmp_db_conn, regression_payment_file):
    """Existing-RE re-import idempotency — the missed-fix case.

    Setup: legacy RE rows (NULL amounts) already in DB + unrelated other RE
    so that lastrowid is 'primed' with a stale value.

    Run 1:
      - New RE inserted (RE6799901) — lastrowid primed to its id
      - Legacy RE upserted (RE6788801, RE6788802) — must use CORRECT re_id, not stale one
      - paid_invoices.amount filled in and linked to the RIGHT re_id
      - received_payments.total set
      - counts: imported=1 new, updated=2 existing, skipped=0, total=3; sum==total

    Runs 2 + 3 (re-import the same file):
      - received_payments and paid_invoices row counts unchanged
      - every amount/total byte-identical to after run 1
      - re_id linkages intact (no cross-contamination)
      - no errors
    """
    import models as m

    conn = tmp_db_conn
    id_8801, id_8802 = _seed_legacy_rows(conn)

    # ── Run 1 ────────────────────────────────────────────────────────────────
    result1 = m.import_payments(regression_payment_file)

    # Counts must add up
    assert result1["total"] == 3
    assert result1["imported"] + result1["updated"] + result1["skipped"] == result1["total"], (
        f"Counts don't add up: {result1}"
    )
    assert result1["imported"] == 1, f"Expected 1 new insert, got {result1}"
    assert result1["updated"] == 2, f"Expected 2 updates, got {result1}"
    assert result1["skipped"] == 0
    assert result1.get("errors", []) == []

    # RE6788801: paid_invoices must be linked to id_8801 (NOT a stale lastrowid)
    pi_rows = conn.execute(
        """SELECT pi.re_id, pi.iv_no, pi.amount
           FROM paid_invoices pi
           WHERE pi.iv_no IN ('IV6788801','IV6788802')
           ORDER BY pi.iv_no"""
    ).fetchall()
    for row in pi_rows:
        assert row["re_id"] == id_8801, (
            f"IV {row['iv_no']}: re_id={row['re_id']} but expected {id_8801} "
            f"(stale lastrowid bug — re_id points to wrong RE)"
        )
    amounts_8801 = {r["iv_no"]: r["amount"] for r in pi_rows}
    assert amounts_8801["IV6788801"] == pytest.approx(2000.00)
    assert amounts_8801["IV6788802"] == pytest.approx(1700.00)

    # RE6788802: IV6788803 linked to id_8802
    pi_8802 = conn.execute(
        "SELECT re_id, amount FROM paid_invoices WHERE iv_no='IV6788803'"
    ).fetchone()
    assert pi_8802["re_id"] == id_8802, (
        f"IV6788803: re_id={pi_8802['re_id']} but expected {id_8802}"
    )
    assert pi_8802["amount"] == pytest.approx(4200.00)

    # received_payments.total set
    rp = conn.execute(
        "SELECT re_no, total FROM received_payments WHERE re_no IN ('RE6788801','RE6788802') ORDER BY re_no"
    ).fetchall()
    totals = {r["re_no"]: r["total"] for r in rp}
    assert totals["RE6788801"] == pytest.approx(3700.00)
    assert totals["RE6788802"] == pytest.approx(4200.00)

    # ── Snapshot counts + values after run 1 ─────────────────────────────────
    def snapshot(c):
        rp_count = c.execute("SELECT COUNT(*) FROM received_payments").fetchone()[0]
        pi_count = c.execute("SELECT COUNT(*) FROM paid_invoices").fetchone()[0]
        pi_amt_count = c.execute(
            "SELECT COUNT(*) FROM paid_invoices WHERE amount IS NOT NULL"
        ).fetchone()[0]
        pi_sum = c.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM paid_invoices"
        ).fetchone()[0]
        rp_total_sum = c.execute(
            "SELECT COALESCE(SUM(total), 0) FROM received_payments"
        ).fetchone()[0]
        # re_id linkages: each (iv_no, re_id) pair
        links = c.execute(
            """SELECT pi.iv_no, pi.re_id, pi.amount
               FROM paid_invoices pi ORDER BY pi.iv_no"""
        ).fetchall()
        return {
            "rp_count": rp_count,
            "pi_count": pi_count,
            "pi_amt_count": pi_amt_count,
            "pi_sum": round(pi_sum, 2),
            "rp_total_sum": round(rp_total_sum, 2),
            "links": [(r["iv_no"], r["re_id"], r["amount"]) for r in links],
        }

    snap1 = snapshot(conn)

    # ── Run 2 (re-import same file) ───────────────────────────────────────────
    result2 = m.import_payments(regression_payment_file)
    assert result2.get("errors", []) == []
    snap2 = snapshot(conn)
    assert snap2 == snap1, f"Run2 snapshot differs from Run1:\nRun1={snap1}\nRun2={snap2}"

    # ── Run 3 (re-import same file again) ─────────────────────────────────────
    result3 = m.import_payments(regression_payment_file)
    assert result3.get("errors", []) == []
    snap3 = snapshot(conn)
    assert snap3 == snap1, f"Run3 snapshot differs from Run1:\nRun1={snap1}\nRun3={snap3}"


def test_wrong_reid_cross_contamination(tmp_path, tmp_db_conn):
    """Wrong-re_id regression: ≥2 REs where first is NEW (primes lastrowid) and
    second already EXISTS with multiple IVs.

    Asserts that every paid_invoices row's re_id maps back (via
    received_payments.re_no) to the RE that actually listed that IV in the CSV.

    This test MUST FAIL on the current (buggy) code and PASS after the fix.
    """
    import models as m

    conn = tmp_db_conn

    # Pre-seed: RE6755001 already exists (legacy, NULL amounts)
    conn.execute(
        "INSERT INTO received_payments (re_no, date_iso, customer, salesperson, cancelled)"
        " VALUES ('RE6755001', '2023-06-01', 'ลูกค้าเก่า', '07', 0)"
    )
    existing_id = conn.execute(
        "SELECT id FROM received_payments WHERE re_no='RE6755001'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO paid_invoices (re_id, iv_no, amount) VALUES (?, 'IV6755001', NULL)",
        (existing_id,)
    )
    conn.execute(
        "INSERT INTO paid_invoices (re_id, iv_no, amount) VALUES (?, 'IV6755002', NULL)",
        (existing_id,)
    )
    conn.execute(
        "INSERT INTO paid_invoices (re_id, iv_no, amount) VALUES (?, 'IV6755003', NULL)",
        (existing_id,)
    )
    conn.commit()

    # CSV: brand-new RE6799999 (will be INSERT → primes lastrowid to a NEW high id)
    #      followed by existing RE6755001 (will be UPSERT-UPDATE → stale lastrowid = RE6799999's id)
    lines = [
        '"(BSN)บจก.บุญสวัสดิ์นำชัย                                                                                          หน้า   :        1"',
        '"  รายงานการรับชำระหนี้ เรียงตามวันที่ของใบเสร็จ"',
        '"---------------------------------------------------------------------------------------------------------------------------------"',
        '"  วันที่  เลขที่ใบเสร็จ  ชื่อลูกค้า                          พนักงานขาย     ตัดเงินมัดจำ ยอดตามใบกำกับ   ชำระเป็น ง/ส       เช็ครับ"',
        '"---------------------------------------------------------------------------------------------------------------------------------"',
        # NEW RE (INSERT path — primes lastrowid)
        '"01/01/67  RE6799999    ลูกค้าใหม่                             07                               9999.00        9999.00"',
        '"                             IV6799999    28/12/66          9999.00"',
        '',
        # EXISTING RE (UPDATE path — stale lastrowid would be RE6799999's id)
        '"01/06/66  RE6755001    ลูกค้าเก่า                             07                               7500.00        7500.00"',
        '"                             IV6755001    25/05/66          2500.00"',
        '"                             IV6755002    26/05/66          2500.00"',
        '"                             IV6755003    27/05/66          2500.00"',
        '',
    ]
    p = tmp_path / "cross_contamination.csv"
    p.write_text("\n".join(lines) + "\n", encoding="cp874")

    result = m.import_payments(str(p))
    assert result["skipped"] == 0
    assert result.get("errors", []) == []

    # Every paid_invoices row for RE6755001's IVs must point to existing_id
    contaminated = conn.execute(
        """SELECT pi.iv_no, pi.re_id, rp.re_no
           FROM paid_invoices pi
           JOIN received_payments rp ON rp.id = pi.re_id
           WHERE pi.iv_no IN ('IV6755001','IV6755002','IV6755003')"""
    ).fetchall()

    assert len(contaminated) == 3, f"Expected 3 IV rows, got {len(contaminated)}"

    # Fetch the id of the new RE to name the wrong target clearly in error messages
    new_re_id = conn.execute(
        "SELECT id FROM received_payments WHERE re_no='RE6799999'"
    ).fetchone()[0]

    for row in contaminated:
        assert row["re_id"] == existing_id, (
            f"CROSS-CONTAMINATION: {row['iv_no']} has re_id={row['re_id']} "
            f"(RE '{row['re_no']}') but should be {existing_id} (RE6755001). "
            f"Stale lastrowid pointed to new RE id={new_re_id}."
        )
        assert row["re_no"] == "RE6755001"

    # Amounts set correctly
    iv_amounts = {r["iv_no"]: conn.execute(
        "SELECT amount FROM paid_invoices WHERE iv_no=? AND re_id=?",
        (r["iv_no"], existing_id)
    ).fetchone()["amount"] for r in contaminated}
    for iv_no in ("IV6755001", "IV6755002", "IV6755003"):
        assert iv_amounts[iv_no] == pytest.approx(2500.00), (
            f"{iv_no} amount wrong: {iv_amounts[iv_no]}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# NEW: SR(−) receipt-link sub-rows
# ═══════════════════════════════════════════════════════════════════════════════
#
# A receipt can apply a credit note against the invoices it settles. Express
# emits these as an "SR…" sub-row carrying a NEGATIVE amount, e.g.
#   "                             SR6900009    27/03/69         -2293.20"
# inside an RE block. The OLD parser only matched "IV\S+" sub-rows, so the
# negative SR line was silently dropped — the receipt then looked like it
# applied only the +IV amounts and the invoice read as fantasy "overpaid".
#
# Parse contract (NEW):
#   - capture SR sub-rows with an optional leading '-' on the amount
#   - tag each iv_list item with kind = 'IV' | 'SR'
#   - received_payments.total stays Σ IV(+) ONLY (legacy total-based tests
#     must remain green): the SR(−) is a netting link, not extra collected.

PAYMENT_SR_LINES = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                                                                          หน้า   :        1"',
    '"  รายงานการรับชำระหนี้ เรียงตามวันที่ของใบเสร็จ"',
    '"---------------------------------------------------------------------------------------------------------------------------------"',
    '"  วันที่  เลขที่ใบเสร็จ  ชื่อลูกค้า                          พนักงานขาย     ตัดเงินมัดจำ ยอดตามใบกำกับ   ชำระเป็น ง/ส       เช็ครับ"',
    '"---------------------------------------------------------------------------------------------------------------------------------"',
    # Oracle: RE6900208 — IV6802996 +5242.02, SR6900009 -2293.20 → net 2948.82
    '"27/03/69  RE6900208    เจริญทรัพย์การค้า                        06                               2948.82        2949.00                      0.18"',
    '"                             IV6802996    13/12/68          5242.02"',
    '"                             SR6900009    27/03/69         -2293.20"',
    '',
    # Plain IV-only RE — unaffected, total = Σ IV(+)
    '"28/03/69  RE6900300    ลูกค้าเงินสด                             06                               1000.00        1000.00"',
    '"                             IV6900400    20/03/69          1000.00"',
    '',
    # SR with thousands-comma negative amount
    '"29/03/69  RE6900301    ลูกค้าทดสอบ                              06                               5000.00        5000.00"',
    '"                             IV6900401    21/03/69          6,234.56"',
    '"                             SR6900050    22/03/69         -1,234.56"',
    '',
]


@pytest.fixture
def sr_payment_file(tmp_path):
    p = tmp_path / "payment_sr.csv"
    p.write_text("\n".join(PAYMENT_SR_LINES) + "\n", encoding="cp874")
    return str(p)


def test_parse_sr_negative_subrow_captured(sr_payment_file):
    """SR sub-row with leading '-' is captured into iv_list (was dropped)."""
    records = models.parse_payment_csv(sr_payment_file)
    by_re = {r["re_no"]: r for r in records}
    items = by_re["RE6900208"]["iv_list"]
    by_no = {it["iv_no"]: it for it in items}
    assert "IV6802996" in by_no
    assert "SR6900009" in by_no, "SR(-) receipt link was dropped by the parser"
    assert by_no["IV6802996"]["amount"] == pytest.approx(5242.02)
    assert by_no["SR6900009"]["amount"] == pytest.approx(-2293.20)


def test_parse_sr_kind_tagging(sr_payment_file):
    """Each iv_list item is tagged kind='IV' or kind='SR'."""
    records = models.parse_payment_csv(sr_payment_file)
    by_re = {r["re_no"]: r for r in records}
    by_no = {it["iv_no"]: it for it in by_re["RE6900208"]["iv_list"]}
    assert by_no["IV6802996"]["kind"] == "IV"
    assert by_no["SR6900009"]["kind"] == "SR"


def test_parse_re_total_is_sum_of_positive_iv_only(sr_payment_file):
    """received_payments.total stays Σ IV(+) ONLY — SR(-) is NOT subtracted
    from `total` (preserve existing total-based tests). Netting happens in
    payments_alloc via the receipt links, not in the header total."""
    records = models.parse_payment_csv(sr_payment_file)
    by_re = {r["re_no"]: r for r in records}
    assert by_re["RE6900208"]["total"] == pytest.approx(5242.02)
    assert by_re["RE6900300"]["total"] == pytest.approx(1000.00)
    assert by_re["RE6900301"]["total"] == pytest.approx(6234.56)


def test_parse_sr_thousands_comma_negative(sr_payment_file):
    """Negative SR amount with thousands comma parses to negative float."""
    records = models.parse_payment_csv(sr_payment_file)
    by_re = {r["re_no"]: r for r in records}
    by_no = {it["iv_no"]: it for it in by_re["RE6900301"]["iv_list"]}
    assert by_no["SR6900050"]["amount"] == pytest.approx(-1234.56)
    assert by_no["SR6900050"]["kind"] == "SR"


def test_parse_iv_only_record_still_tagged_iv(sr_payment_file):
    """Plain IV-only RE: items still carry kind='IV' (regression guard)."""
    records = models.parse_payment_csv(sr_payment_file)
    by_re = {r["re_no"]: r for r in records}
    items = by_re["RE6900300"]["iv_list"]
    assert all(it["kind"] == "IV" for it in items)
    assert items[0]["iv_no"] == "IV6900400"


def test_import_sr_receipt_link_persisted(tmp_path, tmp_db_conn):
    """import_payments persists the SR(-) receipt link in paid_invoices with a
    NEGATIVE amount and iv_no = SR doc_base; received_payments.total = Σ IV(+).
    """
    import models as m
    path = _build_payment_file(tmp_path, PAYMENT_SR_LINES, "pay_sr.csv")
    result = m.import_payments(path)
    assert result["errors"] == []

    conn = tmp_db_conn
    rp_total = conn.execute(
        "SELECT total FROM received_payments WHERE re_no='RE6900208'"
    ).fetchone()["total"]
    assert rp_total == pytest.approx(5242.02)

    links = conn.execute(
        """SELECT pi.iv_no, pi.amount
           FROM paid_invoices pi
           JOIN received_payments rp ON rp.id = pi.re_id
           WHERE rp.re_no = 'RE6900208'
           ORDER BY pi.iv_no"""
    ).fetchall()
    by_no = {r["iv_no"]: r["amount"] for r in links}
    assert by_no["IV6802996"] == pytest.approx(5242.02)
    assert by_no["SR6900009"] == pytest.approx(-2293.20)


def test_import_sr_receipt_link_idempotent(tmp_path, tmp_db_conn):
    """Re-importing the SR file twice: no duplicate links, amounts identical."""
    import models as m
    path = _build_payment_file(tmp_path, PAYMENT_SR_LINES, "pay_sr2.csv")
    m.import_payments(path)
    m.import_payments(path)

    conn = tmp_db_conn
    cnt = conn.execute(
        """SELECT COUNT(*) FROM paid_invoices pi
           JOIN received_payments rp ON rp.id = pi.re_id
           WHERE rp.re_no IN ('RE6900208','RE6900300','RE6900301')"""
    ).fetchone()[0]
    assert cnt == 5  # IV+SR, IV, IV+SR
    sr = conn.execute(
        """SELECT pi.amount FROM paid_invoices pi
           JOIN received_payments rp ON rp.id = pi.re_id
           WHERE rp.re_no='RE6900208' AND pi.iv_no='SR6900009'"""
    ).fetchone()["amount"]
    assert sr == pytest.approx(-2293.20)
