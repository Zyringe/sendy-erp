"""Tests for Express AR (entity-aware) and AP snapshot parsers + import.

Coverage:
  1. AR parser: parses tiny fixture, correct doc/customer counts + sum.
  2. AP parser: parses tiny fixture, correct doc/supplier counts + sum.
  3. AP parser validates: mismatched subtotal raises AssertionError.
  4. _import_ar_snapshot stamps entity='BSN' on inserted rows.
  5. _import_ar_snapshot entity='SD' (default) preserves backwards compat.
  6. _import_ap_snapshot inserts rows with entity='BSN' + correct amounts.
  7. SD AR rows are NOT touched when a BSN AR snapshot is imported.
  8. Footer total assertion: real BSN AR file hits golden totals (skip if flash drive absent).
  9. Footer total assertion: real BSN AP file hits golden totals (skip if flash drive absent).
"""
import os
import sqlite3
import sys
import textwrap

import pytest

# Make scripts/ importable
_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts'))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import parse_express_ar_snapshot as p_ar
import parse_express_ap_snapshot as p_ap

_AR_FILE = (
    "/Volumes/Zyringe_128/Sendai-Boonsawat/Express/BSN/"
    "รายงานลูกหนี้/ลูกหนี้คงค้าง_29.5.69.csv"
)
_AP_FILE = (
    "/Volumes/Zyringe_128/Sendai-Boonsawat/Express/BSN/"
    "รายงานเจ้าหนี้/เจ้าหนี้คงค้าง_29.5.69.csv"
)

# ── Tiny AR fixture (cp874) ──────────────────────────────────────────────────
# Two customers, two docs each.
_AR_FIXTURE_LINES = [
    '  ประเภทลูกค้า : ลูกค้าประจำ',
    '    ร้านทดสอบ /01ท01',
    '      01/01/69   IV6900001   SP1        1,000.00        500.00        500.00',
    '      02/01/69   IV6900002   SP1        2,000.00        500.00      1,500.00',
    '    รวมลูกค้า ร้านทดสอบ /01ท01    2 ใบ     2,000.00',
    '    ร้านทดสอบ2 /01ท02',
    '      03/01/69   IV6900003   SP2        3,000.00          0.00      3,000.00',
    '      04/01/69   IV6900004   SP2        4,000.00          0.00      4,000.00',
    '    รวมลูกค้า ร้านทดสอบ2 /01ท02    2 ใบ     7,000.00',
    '  รวมตามประเภทลูกค้า   2 ราย   4 ใบ     9,000.00',
    '  รวมทั้งสิ้น  2 ราย  4 ใบ  9,000.00',
]

# ── Tiny AP fixture (cp874) ──────────────────────────────────────────────────
# Two supplier types, one supplier each, one doc each.
_AP_FIXTURE_LINES = [
    '  ประเภทผู้จำหน่าย : ผู้จำหน่ายประจำ',
    '    ซัพพลายเออร์ A จำกัด /SA',
    '    01/02/69  RR2600001    IV6900010             5,000.00           0.00       5,000.00',
    '    รวมเจ้าหนี้ ซัพพลายเออร์ A จำกัด /SA    1 ใบ     5,000.00',
    '  รวมตามประเภท ผู้จำหน่ายประจำ   1 ราย   1 ใบ     5,000.00',
    '  ประเภทผู้จำหน่าย : ผู้ค้าส่ง',
    '    ซัพพลายเออร์ B /SB',
    '    15/02/69  RR2600002    IV26020011             3,000.00        1,000.00       2,000.00',
    '    รวมเจ้าหนี้ ซัพพลายเออร์ B /SB    1 ใบ     2,000.00',
    '  รวมตามประเภท ผู้ค้าส่ง   1 ราย   1 ใบ     2,000.00',
    '  รวมทั้งสิ้น  ผู้จำหน่าย  2 ราย   2 ใบ     7,000.00',
]


@pytest.fixture
def ar_fixture_file(tmp_path):
    p = tmp_path / "ar_sample.csv"
    p.write_text('\n'.join(_AR_FIXTURE_LINES) + '\n', encoding='cp874')
    return str(p)


@pytest.fixture
def ap_fixture_file(tmp_path):
    p = tmp_path / "ap_sample.csv"
    p.write_text('\n'.join(_AP_FIXTURE_LINES) + '\n', encoding='cp874')
    return str(p)


# ── 1. AR parser fixture ─────────────────────────────────────────────────────

def test_ar_parser_fixture_counts(ar_fixture_file):
    records = list(p_ar.parse_ar_snapshot(ar_fixture_file))
    assert len(records) == 4
    customers = {r.customer_code for r in records}
    assert customers == {'01ท01', '01ท02'}


def test_ar_parser_fixture_sum(ar_fixture_file):
    records = list(p_ar.parse_ar_snapshot(ar_fixture_file))
    total = round(sum(r.outstanding_amount for r in records), 2)
    assert total == 9000.00


def test_ar_parser_fixture_entity_field_absent(ar_fixture_file):
    """AROutstanding dataclass has no entity field — that's stamped by the importer."""
    records = list(p_ar.parse_ar_snapshot(ar_fixture_file))
    assert not hasattr(records[0], 'entity')


# ── 2. AP parser fixture ─────────────────────────────────────────────────────

def test_ap_parser_fixture_counts(ap_fixture_file):
    records, grand_total, subtotals = p_ap.parse_ap_snapshot(ap_fixture_file)
    assert len(records) == 2
    suppliers = {r.supplier_code for r in records}
    assert suppliers == {'SA', 'SB'}


def test_ap_parser_fixture_sum(ap_fixture_file):
    records, grand_total, subtotals = p_ap.parse_ap_snapshot(ap_fixture_file)
    total = round(sum(r.outstanding_amount for r in records), 2)
    assert total == 7000.00


def test_ap_parser_fixture_supplier_type(ap_fixture_file):
    records, _, _ = p_ap.parse_ap_snapshot(ap_fixture_file)
    by_code = {r.supplier_code: r for r in records}
    assert by_code['SA'].supplier_type == 'ผู้จำหน่ายประจำ'
    assert by_code['SB'].supplier_type == 'ผู้ค้าส่ง'


def test_ap_parser_fixture_doc_fields(ap_fixture_file):
    records, _, _ = p_ap.parse_ap_snapshot(ap_fixture_file)
    by_code = {r.supplier_code: r for r in records}
    sa = by_code['SA']
    assert sa.doc_no == 'RR2600001'
    assert sa.supplier_invoice_no == 'IV6900010'
    assert sa.doc_date_iso == '2026-02-01'
    assert sa.bill_amount == 5000.00
    assert sa.paid_amount == 0.00
    assert sa.outstanding_amount == 5000.00


def test_ap_parser_validate_ok(ap_fixture_file):
    records, grand_total, subtotals = p_ap.parse_ap_snapshot(ap_fixture_file)
    # Should not raise
    p_ap._validate(records, grand_total, subtotals)


# ── 3. AP parser validation: bad subtotal raises ─────────────────────────────

def test_ap_parser_validate_mismatch_raises(tmp_path):
    """If subtotal count doesn't match parsed docs, _validate raises."""
    bad_lines = _AP_FIXTURE_LINES[:]
    # Replace the per-supplier subtotal count with wrong number (3 instead of 1)
    bad_lines[3] = '    รวมเจ้าหนี้ ซัพพลายเออร์ A จำกัด /SA    3 ใบ     5,000.00'
    p = tmp_path / "bad_ap.csv"
    p.write_text('\n'.join(bad_lines) + '\n', encoding='cp874')
    records, grand_total, subtotals = p_ap.parse_ap_snapshot(str(p))
    with pytest.raises(AssertionError, match='Per-supplier mismatches'):
        p_ap._validate(records, grand_total, subtotals)


# ── 4. _import_ar_snapshot stamps entity='BSN' ──────────────────────────────

def test_import_ar_snapshot_stamps_bsn(tmp_db, ar_fixture_file):
    import import_express as ie
    conn = sqlite3.connect(tmp_db)
    conn.execute('PRAGMA foreign_keys = OFF')
    # Get any valid company_id
    company_id = conn.execute('SELECT id FROM companies LIMIT 1').fetchone()[0]
    # Insert a dummy batch row
    cur = conn.execute(
        "INSERT INTO express_import_log (file_type, source_filename, company_id, status) "
        "VALUES ('ar_snapshot', 'test', ?, 'imported')", (company_id,))
    batch_id = cur.lastrowid
    conn.commit()

    ie._import_ar_snapshot(conn, ar_fixture_file, batch_id, company_id,
                            incremental=True, entity='BSN')
    conn.commit()

    rows = conn.execute(
        "SELECT entity, COUNT(*), ROUND(SUM(outstanding_amount),2) "
        "FROM express_ar_outstanding WHERE batch_id=? GROUP BY entity",
        (batch_id,)
    ).fetchall()
    assert len(rows) == 1
    entity, count, total = rows[0]
    assert entity == 'BSN'
    assert count == 4
    assert total == 9000.00
    conn.close()


# ── 5. _import_ar_snapshot entity='SD' (default backwards compat) ─────────────

def test_import_ar_snapshot_default_sd(tmp_db, ar_fixture_file):
    import import_express as ie
    conn = sqlite3.connect(tmp_db)
    conn.execute('PRAGMA foreign_keys = OFF')
    company_id = conn.execute('SELECT id FROM companies LIMIT 1').fetchone()[0]
    cur = conn.execute(
        "INSERT INTO express_import_log (file_type, source_filename, company_id, status) "
        "VALUES ('ar_snapshot', 'test', ?, 'imported')", (company_id,))
    batch_id = cur.lastrowid
    conn.commit()

    # Call without entity kwarg — should default to 'SD'
    ie._import_ar_snapshot(conn, ar_fixture_file, batch_id, company_id)
    conn.commit()

    rows = conn.execute(
        "SELECT entity FROM express_ar_outstanding WHERE batch_id=? LIMIT 1",
        (batch_id,)
    ).fetchall()
    assert rows[0][0] == 'SD'
    conn.close()


# ── 6. _import_ap_snapshot inserts BSN AP rows ───────────────────────────────

def test_import_ap_snapshot_inserts_correctly(tmp_db, ap_fixture_file):
    import import_express as ie
    conn = sqlite3.connect(tmp_db)
    conn.execute('PRAGMA foreign_keys = OFF')
    company_id = conn.execute('SELECT id FROM companies LIMIT 1').fetchone()[0]
    cur = conn.execute(
        "INSERT INTO express_import_log (file_type, source_filename, company_id, status) "
        "VALUES ('ap_snapshot', 'test', ?, 'imported')", (company_id,))
    batch_id = cur.lastrowid
    conn.commit()

    count, _ = ie._import_ap_snapshot(conn, ap_fixture_file, batch_id, company_id,
                                       entity='BSN')
    conn.commit()

    assert count == 2

    rows = conn.execute(
        "SELECT entity, COUNT(*), ROUND(SUM(outstanding_amount),2) "
        "FROM express_ap_outstanding WHERE batch_id=? GROUP BY entity",
        (batch_id,)
    ).fetchall()
    assert len(rows) == 1
    entity, n, total = rows[0]
    assert entity == 'BSN'
    assert n == 2
    assert total == 7000.00
    conn.close()


# ── 7. SD AR rows untouched when BSN AR snapshot is imported ─────────────────

def test_sd_ar_rows_untouched_by_bsn_import(tmp_db, ar_fixture_file):
    import import_express as ie
    conn = sqlite3.connect(tmp_db)
    conn.execute('PRAGMA foreign_keys = OFF')
    company_id = conn.execute('SELECT id FROM companies LIMIT 1').fetchone()[0]

    # Baseline: how many SD rows exist already
    sd_before = conn.execute(
        "SELECT COUNT(*) FROM express_ar_outstanding WHERE entity='SD'"
    ).fetchone()[0]

    # Import BSN AR snapshot
    cur = conn.execute(
        "INSERT INTO express_import_log (file_type, source_filename, company_id, status) "
        "VALUES ('ar_snapshot', 'test', ?, 'imported')", (company_id,))
    batch_id = cur.lastrowid
    conn.commit()
    ie._import_ar_snapshot(conn, ar_fixture_file, batch_id, company_id, entity='BSN')
    conn.commit()

    sd_after = conn.execute(
        "SELECT COUNT(*) FROM express_ar_outstanding WHERE entity='SD'"
    ).fetchone()[0]

    assert sd_after == sd_before, (
        f"SD row count changed: {sd_before} → {sd_after}")
    conn.close()


# ── 8+9. Real file golden-total assertions ────────────────────────────────────

@pytest.mark.skipif(
    not os.path.exists(_AR_FILE),
    reason="Flash drive not mounted — skip live AR file test"
)
def test_real_bsn_ar_golden_totals():
    """BSN AR file: 72 customers / 200 docs / ฿1,299,335.94."""
    records = list(p_ar.parse_ar_snapshot(_AR_FILE))
    customers = {r.customer_code for r in records}
    total = round(sum(r.outstanding_amount for r in records), 2)
    assert len(records) == 200, f"Expected 200 docs, got {len(records)}"
    assert len(customers) == 72, f"Expected 72 customers, got {len(customers)}"
    assert abs(total - 1_299_335.94) < 0.01, f"Expected ฿1,299,335.94, got {total}"


@pytest.mark.skipif(
    not os.path.exists(_AP_FILE),
    reason="Flash drive not mounted — skip live AP file test"
)
def test_real_bsn_ap_golden_totals():
    """BSN AP file: 2 suppliers / 7 invoices / ฿43,640.72."""
    records, grand_total, subtotals = p_ap.parse_ap_snapshot(_AP_FILE)
    p_ap._validate(records, grand_total, subtotals)
    suppliers = {r.supplier_code for r in records}
    total = round(sum(r.outstanding_amount for r in records), 2)
    assert len(records) == 7, f"Expected 7 docs, got {len(records)}"
    assert len(suppliers) == 2, f"Expected 2 suppliers, got {len(suppliers)}"
    assert abs(total - 43_640.72) < 0.01, f"Expected ฿43,640.72, got {total}"


# ── report_asof_date: snapshot date comes from the report header, not max(doc) ──

_HEADER = (
    '"บริษัท บุญสวัสดิ์ นำชัย จำกัด                     หน้า : 1"\n'
    '"  เจ้าหนี้คงค้างแบบละเอียด"\n'
    '"ณ วันที่  29 พ.ค. 2569                            วันที่ : 29/05/69"\n'
)


@pytest.mark.parametrize("mod", [p_ar, p_ap])
def test_report_asof_date_from_header(mod, tmp_path):
    """as-of date is parsed from the 'วันที่ : DD/MM/YY' header (BE→CE)."""
    p = tmp_path / "snap.csv"
    p.write_text(_HEADER, encoding='cp874')
    assert mod.report_asof_date(str(p)) == '2026-05-29'


@pytest.mark.parametrize("mod", [p_ar, p_ap])
def test_report_asof_date_absent_returns_none(mod, tmp_path):
    """No header date → None (importer then falls back to max doc date)."""
    p = tmp_path / "nohdr.csv"
    p.write_text('  ประเภทลูกค้า : ลูกค้าประจำ\n', encoding='cp874')
    assert mod.report_asof_date(str(p)) is None


# ── Idempotency: re-importing the same report must not double-count ──────────

def _new_batch(conn, file_type):
    company_id = conn.execute('SELECT id FROM companies LIMIT 1').fetchone()[0]
    cur = conn.execute(
        "INSERT INTO express_import_log (file_type, source_filename, company_id, status) "
        "VALUES (?, 'test', ?, 'imported')", (file_type, company_id))
    return cur.lastrowid, company_id


def test_ar_import_idempotent_on_reupload(tmp_db, ar_fixture_file):
    """Importing the same AR snapshot twice (different batches, same entity+date)
    leaves one set of rows, not double — Codex finding: retry must not inflate."""
    import import_express as ie
    conn = sqlite3.connect(tmp_db)
    conn.execute('PRAGMA foreign_keys = OFF')
    conn.execute("DELETE FROM express_ar_outstanding")  # tmp_db is a live-DB copy
    for _ in range(2):
        batch_id, company_id = _new_batch(conn, 'ar_snapshot')
        ie._import_ar_snapshot(conn, ar_fixture_file, batch_id, company_id, entity='BSN')
        conn.commit()
    n, total = conn.execute(
        "SELECT COUNT(*), ROUND(SUM(outstanding_amount),2) FROM express_ar_outstanding "
        "WHERE entity='BSN'").fetchone()
    assert n == 4, f"re-upload doubled rows: {n}"
    assert total == 9000.00
    conn.close()


def test_ap_import_idempotent_on_reupload(tmp_db, ap_fixture_file):
    import import_express as ie
    conn = sqlite3.connect(tmp_db)
    conn.execute('PRAGMA foreign_keys = OFF')
    conn.execute("DELETE FROM express_ap_outstanding")  # tmp_db is a live-DB copy
    for _ in range(2):
        batch_id, company_id = _new_batch(conn, 'ap_snapshot')
        ie._import_ap_snapshot(conn, ap_fixture_file, batch_id, company_id, entity='BSN')
        conn.commit()
    n, total = conn.execute(
        "SELECT COUNT(*), ROUND(SUM(outstanding_amount),2) FROM express_ap_outstanding "
        "WHERE entity='BSN'").fetchone()
    assert n == 2, f"re-upload doubled rows: {n}"
    assert total == 7000.00
    conn.close()


# ── Validation gate: footer mismatch aborts the import, commits nothing ──────

def _drop_last_detail_keep_footer(lines, footer_marker):
    """Remove the last detail row but keep the (now-inconsistent) footer, so the
    parsed sum no longer matches the footer total — simulates format drift."""
    detail_idxs = [i for i, ln in enumerate(lines)
                   if ln.lstrip().startswith(('0', '1', '2', '3'))]  # date-led rows
    out = [ln for i, ln in enumerate(lines) if i != detail_idxs[-1]]
    return out


def test_ar_import_aborts_on_footer_mismatch(tmp_db, tmp_path):
    """A partial AR parse (footer total > sum of parsed rows) must raise and
    write nothing — Codex finding: drift must not become authoritative."""
    import import_express as ie
    bad = _drop_last_detail_keep_footer(_AR_FIXTURE_LINES, 'รวมทั้งสิ้น')
    p = tmp_path / "bad_ar.csv"
    p.write_text('\n'.join(bad) + '\n', encoding='cp874')
    conn = sqlite3.connect(tmp_db)
    conn.execute('PRAGMA foreign_keys = OFF')
    conn.execute("DELETE FROM express_ar_outstanding")  # tmp_db is a live-DB copy
    batch_id, company_id = _new_batch(conn, 'ar_snapshot')
    conn.commit()
    with pytest.raises((ValueError, AssertionError)):
        ie._import_ar_snapshot(conn, str(p), batch_id, company_id, entity='BSN')
    n = conn.execute("SELECT COUNT(*) FROM express_ar_outstanding").fetchone()[0]
    assert n == 0, f"partial AR import committed {n} rows"
    conn.close()


def test_ap_import_aborts_on_footer_mismatch(tmp_db, tmp_path):
    import import_express as ie
    bad = _drop_last_detail_keep_footer(_AP_FIXTURE_LINES, 'รวมทั้งสิ้น')
    p = tmp_path / "bad_ap.csv"
    p.write_text('\n'.join(bad) + '\n', encoding='cp874')
    conn = sqlite3.connect(tmp_db)
    conn.execute('PRAGMA foreign_keys = OFF')
    conn.execute("DELETE FROM express_ap_outstanding")  # tmp_db is a live-DB copy
    batch_id, company_id = _new_batch(conn, 'ap_snapshot')
    conn.commit()
    with pytest.raises((ValueError, AssertionError)):
        ie._import_ap_snapshot(conn, str(p), batch_id, company_id, entity='BSN')
    n = conn.execute("SELECT COUNT(*) FROM express_ap_outstanding").fetchone()[0]
    assert n == 0, f"partial AP import committed {n} rows"
    conn.close()
