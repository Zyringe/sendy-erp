"""Tests for the durable AR write-off ledger (migration 095).

A doc_no recorded in `ar_writeoffs` must drop from the canonical collectable-AR
surfaces (customer_ranking / ar_aging) and STAY dropped after a fresh ลูกหนี้คงค้าง
import replaces express_ar_outstanding. Write-backs (credit balances) are excluded
the same way (they're cleared off the AR, just booked as รายได้อื่น by the accountant).
"""
import sqlite3
import pytest

import ar_followup
import cashflow
from tests.test_ar_followup import _ins_express


def _ensure_writeoffs_table(conn):
    """The schema-clone fixture predates mig 095 until it's applied to the live
    DB; create the table here so the test is self-contained. Mirrors mig 095."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ar_writeoffs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_no         TEXT    NOT NULL,
            customer_code  TEXT,
            customer_name  TEXT,
            amount         REAL    NOT NULL DEFAULT 0,
            type           TEXT    NOT NULL CHECK(type IN ('expense','writeback')),
            writeoff_date  TEXT    NOT NULL,
            reason         TEXT,
            created_at     TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
            UNIQUE(doc_no)
        )""")
    conn.commit()


def _writeoff(conn, doc_no, amount, typ='expense', code='01อ35', when='2026-06-05'):
    conn.execute(
        "INSERT INTO ar_writeoffs (doc_no, customer_code, amount, type, writeoff_date) "
        "VALUES (?,?,?,?,?)", (doc_no, code, amount, typ, when))
    conn.commit()


def test_writeoff_drops_doc_from_customer_ranking(empty_db_conn):
    conn = empty_db_conn
    _ensure_writeoffs_table(conn)
    # Two collectable docs for the same customer.
    _ins_express(conn, 'IV6900401', '01อ35', 'วรสวัสดิ์', '2026-03-11', 95704.35)
    _ins_express(conn, 'IV6800934', '02ส10', 'สมเด็จโมเดิร์นโฮม', '2025-03-31', 24999.25)
    conn.commit()

    before = {r['customer_code']: r['outstanding'] for r in ar_followup.customer_ranking(conn=conn)}
    assert before.get('02ส10') == pytest.approx(24999.25)

    # Write off สมเด็จ.
    _writeoff(conn, 'IV6800934', 24999.25)
    after = {r['customer_code']: r['outstanding'] for r in ar_followup.customer_ranking(conn=conn)}
    assert '02ส10' not in after, "written-off customer should drop from ranking"
    assert after['01อ35'] == pytest.approx(95704.35), "other debt unchanged"


def test_writeoff_reduces_collectable_by_exact_amount(empty_db_conn):
    conn = empty_db_conn
    _ensure_writeoffs_table(conn)
    _ins_express(conn, 'IVA', 'C1', 'A', '2026-01-10', 10000.00)
    _ins_express(conn, 'IVB', 'C2', 'B', '2026-02-10', 25000.00)
    _ins_express(conn, 'IVC', 'C3', 'C', '2026-03-10', 5000.00)
    conn.commit()

    total_before = cashflow.ar_aging(conn=conn)['total_outstanding']
    assert total_before == pytest.approx(40000.00)

    _writeoff(conn, 'IVB', 25000.00)
    total_after = cashflow.ar_aging(conn=conn)['total_outstanding']
    assert total_after == pytest.approx(15000.00), "collectable drops by exactly the write-off"


def test_writeback_credit_also_excluded(empty_db_conn):
    """A negative (credit) row marked write-back is excluded too."""
    conn = empty_db_conn
    _ensure_writeoffs_table(conn)
    _ins_express(conn, 'IVPOS', 'C1', 'A', '2026-01-10', 10000.00)
    _ins_express(conn, 'RECRED', 'C1', 'A', '2026-01-15', -3000.00)
    conn.commit()
    # net before = 7000
    assert cashflow.ar_aging(conn=conn)['total_outstanding'] == pytest.approx(7000.00)
    _writeoff(conn, 'RECRED', -3000.00, typ='writeback')
    # credit excluded → only the positive remains
    assert cashflow.ar_aging(conn=conn)['total_outstanding'] == pytest.approx(10000.00)


def test_writeoff_survives_reimport_of_snapshot(empty_db_conn):
    """The killer property: a fresh snapshot (new snapshot_date) that re-lists
    the same doc_no must STILL exclude it, because ar_writeoffs is keyed on doc_no
    and the queries use MAX(snapshot_date_iso)."""
    conn = empty_db_conn
    _ensure_writeoffs_table(conn)
    # Old snapshot.
    _ins_express(conn, 'IVX', 'C1', 'A', '2026-01-10', 8000.00, snapshot='2026-05-29')
    conn.commit()
    _writeoff(conn, 'IVX', 8000.00)
    assert cashflow.ar_aging(conn=conn)['total_outstanding'] == pytest.approx(0.0)

    # Simulate a re-import: a NEW snapshot date still lists IVX as outstanding.
    _ins_express(conn, 'IVX', 'C1', 'A', '2026-01-10', 8000.00, snapshot='2026-06-05')
    _ins_express(conn, 'IVNEW', 'C2', 'B', '2026-06-04', 1000.00, snapshot='2026-06-05')
    conn.commit()
    aging = cashflow.ar_aging(conn=conn)
    # latest snapshot is 06-05; IVX still excluded, only the new doc counts.
    assert aging['as_of'] == '2026-06-05'
    assert aging['total_outstanding'] == pytest.approx(1000.00), \
        "written-off doc stays excluded across re-import; only the genuinely-new doc counts"


def test_no_writeoffs_leaves_ar_unchanged(empty_db_conn):
    """Empty ar_writeoffs must not change any existing figure (no regression)."""
    conn = empty_db_conn
    _ensure_writeoffs_table(conn)
    _ins_express(conn, 'IV1', 'C1', 'A', '2026-01-10', 12345.67)
    conn.commit()
    assert cashflow.ar_aging(conn=conn)['total_outstanding'] == pytest.approx(12345.67)


def _ins_anomalous(conn, doc_no, code, name, date_iso, outstanding):
    """Insert an is_anomalous=1 (RE) row, re-using the snapshot batch."""
    bid = conn.execute(
        "SELECT id FROM express_import_log WHERE file_type='ar_snapshot' "
        "AND snapshot_date_iso='2026-05-29' LIMIT 1").fetchone()
    bid = bid[0] if bid else conn.execute(
        "INSERT INTO express_import_log (file_type, source_filename, record_count, "
        "line_count, snapshot_date_iso, status) VALUES "
        "('ar_snapshot','t.csv',0,0,'2026-05-29','imported')").lastrowid
    conn.execute(
        "INSERT INTO express_ar_outstanding (batch_id, snapshot_date_iso, customer_code, "
        "customer_name, doc_no, doc_date_iso, bill_amount, paid_amount, outstanding_amount, "
        "entity, is_anomalous) VALUES (?,?,?,?,?,?,?,?,?,'BSN',1)",
        (bid, '2026-05-29', code, name, doc_no, date_iso, outstanding, 0, outstanding))
    conn.commit()


def test_gross_reconciliation_holds_after_collectable_writeoff(empty_db_conn):
    """The MAJOR scrutinize fix: collectable + legacy + re + writeoff == gross.
    A collectable-window doc that is written off must land in the writeoff
    bucket (not vanish), so the disclosed gross still reconciles."""
    conn = empty_db_conn
    _ensure_writeoffs_table(conn)
    # collectable (recent, non-anomalous)
    _ins_express(conn, 'IVREC1', 'C1', 'A', '2026-03-11', 100000.00)
    _ins_express(conn, 'IVREC2', 'C2', 'B', '2026-04-01', 35201.25)  # → write off
    # legacy (non-anomalous, pre-2024)
    _ins_express(conn, 'IVOLD',  'C3', 'C', '2014-01-22', 50000.00)
    # RE (anomalous)
    _ins_anomalous(conn, 'RE001', 'C4', 'D', '2010-05-01', 20000.00)
    conn.commit()
    gross = 100000.00 + 35201.25 + 50000.00 + 20000.00

    # Write off the recent collectable doc.
    _writeoff(conn, 'IVREC2', 35201.25)

    collectable = cashflow.ar_aging(conn=conn)['total_outstanding']
    exc = cashflow.bsn_ar_excluded(conn=conn)

    assert collectable == pytest.approx(100000.00), "collectable drops the written-off recent"
    assert exc['writeoff_amount'] == pytest.approx(35201.25), "written-off recent lands in writeoff bucket"
    assert exc['writeoff_count'] == 1
    assert exc['legacy_amount'] == pytest.approx(50000.00)
    assert exc['re_amount'] == pytest.approx(20000.00)

    # THE reconciliation: nothing vanishes.
    recon = collectable + exc['legacy_amount'] + exc['re_amount'] + exc['writeoff_amount']
    assert recon == pytest.approx(gross), \
        f"gross must reconcile: got {recon}, expected {gross}"


def test_writeoff_buckets_are_disjoint_no_double_count(empty_db_conn):
    """A write-off that is itself RE or legacy stays in its RE/legacy bucket,
    NOT the writeoff bucket — so it's counted once and gross still reconciles."""
    conn = empty_db_conn
    _ensure_writeoffs_table(conn)
    _ins_express(conn, 'IVREC', 'C1', 'A', '2026-03-11', 80000.00)
    _ins_anomalous(conn, 'REWO', 'C2', 'B', '2010-05-01', 25000.00)  # RE that we also write off
    conn.commit()
    gross = 80000.00 + 25000.00
    _writeoff(conn, 'REWO', 25000.00)  # writing off an RE doc

    collectable = cashflow.ar_aging(conn=conn)['total_outstanding']
    exc = cashflow.bsn_ar_excluded(conn=conn)
    # REWO is anomalous → stays in RE bucket, NOT writeoff bucket (no double-count)
    assert exc['re_amount'] == pytest.approx(25000.00)
    assert exc['writeoff_amount'] == pytest.approx(0.0), "RE write-off stays in RE bucket"
    recon = collectable + exc['legacy_amount'] + exc['re_amount'] + exc['writeoff_amount']
    assert recon == pytest.approx(gross)


def test_excluded_by_customer_includes_writtenoff_recent(empty_db_conn):
    """The dunning 'not collectable' per-customer section must surface the
    written-off recent (it was previously dropped from BOTH collectable and
    excluded → invisible)."""
    conn = empty_db_conn
    _ensure_writeoffs_table(conn)
    _ins_express(conn, 'IVA', 'C1', 'CustA', '2026-03-11', 24999.25)
    conn.commit()
    _writeoff(conn, 'IVA', 24999.25)
    excl = cashflow.bsn_ar_excluded_by_customer(conn=conn)
    codes = {r['customer_code']: r for r in excl}
    assert 'C1' in codes, "written-off customer must appear in the excluded section"
    assert codes['C1']['has_writeoff'] == 1
    assert codes['C1']['outstanding'] == pytest.approx(24999.25)
