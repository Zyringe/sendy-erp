"""
Tests for the history-export guard on /import-weekly.

Phase D of the stock-ledger-rebuild plan: /import-weekly must REJECT
full-history Express exports (ประวัติการขาย_แยกตามลูกค้า / ประวัติการซื้อ)
and redirect with a flash error, without inserting any rows.

Coverage:
  (a) is_history_export() returns True for a history-format cp874 fixture
      and False for normal weekly fixtures.
  (b) /import-weekly route rejects a history file: flashes the expected
      Thai message, redirects back, and inserts 0 rows.
  (c) /import-weekly route accepts a normal weekly sales file: returns
      a redirect (not a 400/500), and inserts ≥ 1 row.
"""
import io
import os
import sqlite3

import pytest

os.environ.setdefault('SKIP_DB_INIT', '1')

import parse_weekly

# ── Fixture content ──────────────────────────────────────────────────────────
#
# History export header: title + วันที่จาก spanning 2567→2569 (3-year range).
# Content is minimal — the guard only reads the first ~15 lines.
_HISTORY_SALES_LINES = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                                                                             หน้า   :        1"',
    '"  รายงานประวัติการขาย\xa0แยกตามลูกค้า"',
    '"รหัสลูกค้า  01ก01                ถึง  Zหน้าร้าน                                                                      วันที่ : 29/05/69"',
    '"วันที่จาก   1\xa0ม.ค.\xa02567          ถึง  31\xa0ธ.ค.\xa02569"',
    '"รหัสสินค้า  000ก4001             ถึง  แบบ"',
    '"พนักงานขาย                       ถึง  S02                เลือกแผนก  *"',
    '"--------------------------------------------------------------------------------------------------------------------------------------"',
    '"  สินค้า วันที่ เลขที่เอกสาร          จำนวน   คืน   ราคาต่อหน่วย\xa0VAT   ส่วนลด       รวมเงิน  ส่วนลดรวม  ยอดขายสุทธิ  อ้างอิง  หมายเหตุ"',
    '"--------------------------------------------------------------------------------------------------------------------------------------"',
    '"  เกียรติทวีฮาร์ดแวร์ /01ก11"',
    '"   ใบตัดเพชร 4" #GL-888(แดง) /031บ4120"',
    '"      04/07/68   IV6801757-  1        50.00 ใบ          149.54  2                  7477.00                  7477.00"',
]

# History export header — SINGLE Buddhist year (the real-world re-export shape
# that the year-crossing heuristic missed). Modeled byte-for-byte on the real
# file data/source/new_source/bsn_ประวัติขาย_1.3.69-19.4.69.csv:
#   report date  วันที่ : 20/04/69   (export run 20 เม.ย. 2569)
#   filter       วันที่จาก 1 มี.ค. 2569 ถึง 19 เม.ย. 2569  (same BE year)
# Reach-back = report(2026-04-20) − filter_start(2026-03-01) = 50 days → history.
_HISTORY_SALES_SINGLE_YEAR_LINES = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                                                                             หน้า   :        1"',
    '"  รายงานประวัติการขาย\xa0แยกตามลูกค้า"',
    '"รหัสลูกค้า                       ถึง  Zหน้าร้าน                                                                      วันที่ : 20/04/69"',
    '"วันที่จาก   1\xa0มี.ค.\xa02569         ถึง  19\xa0เม.ย.\xa02569"',
    '"รหัสสินค้า  000ก4001             ถึง  แบบ"',
    '"พนักงานขาย                       ถึง  S02                เลือกแผนก  *"',
    '"--------------------------------------------------------------------------------------------------------------------------------------"',
    '"  สินค้า วันที่ เลขที่เอกสาร          จำนวน   คืน   ราคาต่อหน่วย\xa0VAT   ส่วนลด       รวมเงิน  ส่วนลดรวม  ยอดขายสุทธิ  อ้างอิง  หมายเหตุ"',
    '"--------------------------------------------------------------------------------------------------------------------------------------"',
    '"  เกียรติทวีฮาร์ดแวร์ /01ก11"',
    '"   ใบตัดเพชร 4" #GL-888(แดง) /031บ4120"',
    '"      02/03/69   IV6900100-  1        10.00 ใบ          149.54  2                  1495.40                  1495.40"',
    '"      18/04/69   IV6900200-  1        20.00 ใบ          149.54  2                  2990.80                  2990.80"',
]

# History export header for purchases (ซื้อ variant, multi-year).
_HISTORY_PURCH_LINES = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                                                                            หน้า   :        1"',
    '"  รายงานประวัติการซื้อ\xa0แยกตามผู้จำหน่าย"',
    '"รหัสผู้จำหน่ายจาก  AA01             ถึง  ZZ99                                                                       วันที่ : 29/05/69"',
    '"วันที่จาก           1\xa0ม.ค.\xa02567   ถึง  31\xa0ธ.ค.\xa02569"',
    '"รหัสสินค้าจาก      000ก4001             ถึง  แบบ                    เลือกแผนก [*   ]"',
    '"-------------------------------------------------------------------------------------------------------------------------------------"',
    '"   สินค้า  วันที่  เลขที่เอกสาร       จำนวน   คืน  ราคาต่อหน่วย\xa0VAT\xa0\xa0 ส่วนลด       รวมเงิน  ส่วนลดรวม     ยอดซื้อสุทธิ อ้างถึง"',
    '"-------------------------------------------------------------------------------------------------------------------------------------"',
]

# Normal weekly sales file header (same year: 2569→2569).
# Taken from the conftest SALES_SAMPLE_LINES (which is already a valid weekly
# fixture), so we re-use that fixture for the route test.

# Normal weekly purchase with same-year date range.
_WEEKLY_PURCH_SAME_YEAR_LINES = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                                                                            หน้า   :        1"',
    '"  รายงานประวัติการซื้อ\xa0แยกตามผู้จำหน่าย"',
    '"รหัสผู้จำหน่ายจาก                       ถึง  ไพ                                                                     วันที่ : 24/04/69"',
    '"วันที่จาก          23\xa0เม.ย.\xa02569        ถึง  31\xa0ธ.ค.\xa02569"',
    '"รหัสสินค้าจาก      000ก4001             ถึง  แบบ                    เลือกแผนก [*   ]"',
    '"-------------------------------------------------------------------------------------------------------------------------------------"',
    '"   สินค้า  วันที่  เลขที่เอกสาร       จำนวน   คืน  ราคาต่อหน่วย\xa0VAT\xa0\xa0 ส่วนลด       รวมเงิน  ส่วนลดรวม     ยอดซื้อสุทธิ อ้างถึง"',
    '"-------------------------------------------------------------------------------------------------------------------------------------"',
    '"  ย้งเจริญการพิมพ์\xa0/ย้ง"',
    '"   กล่องในปุ๊ก#7\xa0/Pกล่อง3"',
    '"        24/04/69   HP6900023       22965.00 กล            0.69  0                 15845.85                 15845.85 PO0000227-  1"',
]


@pytest.fixture
def history_sales_file(tmp_path):
    p = tmp_path / "ประวัติการขาย_แยกตามลูกค้า_full_29.5.69.csv"
    p.write_text("\n".join(_HISTORY_SALES_LINES) + "\n", encoding="cp874")
    return str(p)


@pytest.fixture
def history_sales_single_year_file(tmp_path):
    p = tmp_path / "ประวัติการขาย_แยกตามลูกค้า_1.3.69-19.4.69.csv"
    p.write_text("\n".join(_HISTORY_SALES_SINGLE_YEAR_LINES) + "\n", encoding="cp874")
    return str(p)


@pytest.fixture
def history_purch_file(tmp_path):
    p = tmp_path / "ประวัติการซื้อ_full.csv"
    p.write_text("\n".join(_HISTORY_PURCH_LINES) + "\n", encoding="cp874")
    return str(p)


@pytest.fixture
def weekly_purch_file(tmp_path):
    p = tmp_path / "ซื้อ_sample_weekly.csv"
    p.write_text("\n".join(_WEEKLY_PURCH_SAME_YEAR_LINES) + "\n", encoding="cp874")
    return str(p)


# ── is_history_export() unit tests ──────────────────────────────────────────

def test_history_sales_detected(history_sales_file):
    """History sales export (start 2567 < end 2569) must return True."""
    assert parse_weekly.is_history_export(history_sales_file) is True


def test_history_purch_detected(history_purch_file):
    """History purchase export (start 2567 < end 2569) must return True."""
    assert parse_weekly.is_history_export(history_purch_file) is True


def test_history_sales_single_year_detected(history_sales_single_year_file):
    """A full-history export confined to ONE Buddhist year (filter start far
    before the report date) must still be detected as history.

    Regression for the blocker: the old year-crossing heuristic returned False
    for single-year history dumps, letting them through /import-weekly and
    re-corrupting stock. Reach-back = 50 days (2026-03-01 → 2026-04-20).
    """
    assert parse_weekly.is_history_export(history_sales_single_year_file) is True


def test_weekly_sales_not_history(sample_sales_file):
    """Normal weekly sales (same-year date range) must return False."""
    assert parse_weekly.is_history_export(sample_sales_file) is False


def test_weekly_purch_not_history(sample_purchase_file):
    """Normal weekly purchase (same-year date range) must return False."""
    assert parse_weekly.is_history_export(sample_purchase_file) is False


def test_weekly_purch_same_year_not_history(weekly_purch_file):
    """Weekly purchase with same-year วันที่จาก must return False."""
    assert parse_weekly.is_history_export(weekly_purch_file) is False


# ── Route-level tests ────────────────────────────────────────────────────────

@pytest.fixture
def admin_client(tmp_db):
    """Flask test client with an admin session, DATABASE_PATH already patched."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id']  = 1
        sess['username'] = 'test-admin'
        sess['role']     = 'admin'
    return c


def _count_rows(db_path, table):
    return sqlite3.connect(db_path).execute(
        f"SELECT COUNT(*) FROM {table}"
    ).fetchone()[0]


def test_route_rejects_history_file_no_insert(admin_client, tmp_db, tmp_path):
    """
    POST a history-format file to /import-weekly.
    Expect: redirect back (302), flash contains the Thai error phrase,
    and zero rows inserted into sales_transactions.
    """
    before = _count_rows(tmp_db, 'sales_transactions')

    file_content = ("\n".join(_HISTORY_SALES_LINES) + "\n").encode('cp874')
    data = {
        'weekly_file': (io.BytesIO(file_content),
                        'ประวัติการขาย_full.csv',
                        'text/csv'),
    }
    resp = admin_client.post(
        '/import-weekly',
        data=data,
        content_type='multipart/form-data',
        follow_redirects=False,
    )
    assert resp.status_code == 302, f"Expected redirect, got {resp.status_code}"

    after = _count_rows(tmp_db, 'sales_transactions')
    assert after == before, (
        f"History file inserted {after - before} rows into sales_transactions; expected 0"
    )

    # Flash message must contain the key Thai phrase
    with admin_client.session_transaction() as sess:
        flashes = sess.get('_flashes', [])
    messages = [msg for (cat, msg) in flashes]
    assert any('รายงานประวัติเต็ม' in m for m in messages), (
        f"Expected Thai error flash not found. Got flashes: {messages}"
    )


def test_route_rejects_single_year_history_no_insert(admin_client, tmp_db):
    """
    POST a SINGLE-Buddhist-year history file (the shape the old heuristic
    missed). Expect: rejected with the history flash and 0 rows inserted.
    """
    before = _count_rows(tmp_db, 'sales_transactions')

    file_content = ("\n".join(_HISTORY_SALES_SINGLE_YEAR_LINES) + "\n").encode('cp874')
    data = {
        'weekly_file': (io.BytesIO(file_content),
                        'ประวัติการขาย_1.3.69-19.4.69.csv',
                        'text/csv'),
    }
    resp = admin_client.post(
        '/import-weekly',
        data=data,
        content_type='multipart/form-data',
        follow_redirects=False,
    )
    assert resp.status_code == 302, f"Expected redirect, got {resp.status_code}"

    after = _count_rows(tmp_db, 'sales_transactions')
    assert after == before, (
        f"Single-year history file inserted {after - before} rows; expected 0"
    )

    with admin_client.session_transaction() as sess:
        flashes = sess.get('_flashes', [])
    messages = [msg for (cat, msg) in flashes]
    assert any('รายงานประวัติเต็ม' in m for m in messages), (
        f"Expected history-guard flash not found. Got flashes: {messages}"
    )


def test_route_accepts_weekly_file(admin_client, tmp_db, sample_purchase_file):
    """
    POST a normal weekly purchase file. Expect: the import path actually
    runs (success flash "นำเข้าสำเร็จ …"), not just any 302 — a 302 alone
    also fires on the empty-file / unknown-type early-outs, so asserting it
    is too weak to prove "weekly still imports" (finding #10).
    """
    file_content = open(sample_purchase_file, 'rb').read()
    data = {
        'weekly_file': (io.BytesIO(file_content),
                        'ซื้อ_sample.csv',
                        'text/csv'),
    }
    resp = admin_client.post(
        '/import-weekly',
        data=data,
        content_type='multipart/form-data',
        follow_redirects=False,
    )
    # Must redirect (not fail with 4xx/5xx)
    assert resp.status_code == 302, f"Expected redirect, got {resp.status_code}: {resp.data[:300]}"

    with admin_client.session_transaction() as sess:
        flashes = sess.get('_flashes', [])
    messages = [msg for (cat, msg) in flashes]
    # The history-guard flash must NOT appear …
    assert not any('รายงานประวัติเต็ม' in m for m in messages), (
        f"History-guard flash wrongly triggered on weekly file: {messages}"
    )
    # … and the weekly import must have actually run (parse found rows +
    # models.import_weekly executed), proven by the success flash.
    assert any('นำเข้าสำเร็จ' in m for m in messages), (
        f"Weekly import did not run (no success flash). Got flashes: {messages}"
    )
