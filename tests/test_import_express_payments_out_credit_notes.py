"""Tests for scripts/import_express.py's payments_out + credit_notes (AP
side) importers — payments_out (จ่ายชำระหนี้) and credit_notes_ap
(ใบลดหนี้ — ส่งคืน) had ZERO prior test coverage; these two functions are
also the ones refactored for Phase 1 slice B (Express DBF-direct import,
projects/express-integration/plan.md) to expose a records-first entry
point (run_import_records) alongside the existing file-path entry point.

Coverage:
  1. _import_payments_out_records: dict input inserts express_payments_out
     + express_payment_out_receive_refs correctly; idempotent (skip on
     re-run, incremental=True dedup by doc_no).
  2. _import_credit_notes_records: dict input inserts express_credit_notes
     + express_credit_note_lines correctly; idempotent.
  3. run_import_records: full path (express_import_log batch row +
     company_id resolution + commit), for both file_types; unknown
     file_type raises; a mid-batch exception rolls back the whole batch.
  4. _import_payments_out / _import_credit_notes (thin file-path wrappers):
     still delegate correctly after the refactor — proven by monkeypatching
     the real parser function to return canned dataclass instances (locks
     in the dataclasses.asdict() conversion without re-testing the regex
     parsers themselves, which are untouched, out-of-scope, pre-existing
     code).
"""
import dataclasses
import os
import sqlite3
import sys

import pytest

# Make scripts/ importable (import_express.py lives there).
_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts'))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import import_express as ie


def _company_id(conn, code='BSN'):
    row = conn.execute('SELECT id FROM companies WHERE code = ?', (code,)).fetchone()
    return row[0]


def _new_batch(conn, file_type, code='BSN'):
    company_id = _company_id(conn, code)
    cur = conn.execute(
        "INSERT INTO express_import_log (file_type, source_filename, company_id, status) "
        "VALUES (?, 'test', ?, 'imported')", (file_type, company_id))
    return cur.lastrowid, company_id


# ── 1. _import_payments_out_records ─────────────────────────────────────────

def _payment_out_record(doc_no='PS9999001', supplier_name='ซัพพลายเออร์ A',
                         invoice_amount=8540.00, receive_refs=None):
    return {
        'doc_no': doc_no, 'date_iso': '2026-05-01', 'supplier_name': supplier_name,
        'is_void': False, 'deposit_applied': 0.0, 'invoice_amount': invoice_amount,
        'cash_amount': 0.0, 'cheque_amount': 0.0, 'interest_amount': 0.0,
        'discount_amount': 0.0, 'vat_amount': 0.0, 'cheque_no': '',
        'cheque_date_iso': '', 'bank': '', 'cheque_status': '', 'note': '',
        'receive_refs': receive_refs or [
            {'receive_doc': 'RR6600291', 'receive_date_iso': None,
             'invoice_ref': None, 'amount': invoice_amount},
        ],
    }


def test_import_payments_out_records_inserts_correctly(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute('PRAGMA foreign_keys = OFF')
    batch_id, company_id = _new_batch(conn, 'payments_out')
    conn.commit()

    count, line_count = ie._import_payments_out_records(
        conn, [_payment_out_record()], batch_id, company_id)
    conn.commit()

    assert count == 1
    assert line_count == 1
    row = conn.execute(
        "SELECT invoice_amount, supplier_name FROM express_payments_out WHERE doc_no='PS9999001'"
    ).fetchone()
    assert row[0] == pytest.approx(8540.00)
    assert row[1] == 'ซัพพลายเออร์ A'
    ref = conn.execute(
        "SELECT receive_doc, amount FROM express_payment_out_receive_refs "
        "WHERE payment_out_id = (SELECT id FROM express_payments_out WHERE doc_no='PS9999001')"
    ).fetchone()
    assert ref[0] == 'RR6600291'
    assert ref[1] == pytest.approx(8540.00)
    conn.close()


def test_import_payments_out_records_skips_existing_doc_no(tmp_db):
    """Dedup key = doc_no (matches _existing_doc_nos, not the per-batch
    UNIQUE(batch_id, doc_no) constraint) — re-importing the same doc_no in
    a later batch is skipped, not duplicated."""
    conn = sqlite3.connect(tmp_db)
    conn.execute('PRAGMA foreign_keys = OFF')

    batch1, company_id = _new_batch(conn, 'payments_out')
    conn.commit()
    ie._import_payments_out_records(conn, [_payment_out_record()], batch1, company_id)
    conn.commit()

    batch2, _ = _new_batch(conn, 'payments_out')
    conn.commit()
    count2, _ = ie._import_payments_out_records(
        conn, [_payment_out_record()], batch2, company_id)
    conn.commit()

    assert count2 == 0, "re-import of the same doc_no must be skipped"
    n = conn.execute(
        "SELECT COUNT(*) FROM express_payments_out WHERE doc_no='PS9999001'"
    ).fetchone()[0]
    assert n == 1, "must not duplicate"
    conn.close()


# ── 2. _import_credit_notes_records (AP side: ใบลดหนี้ — ส่งคืน) ────────────

def _credit_note_ap_record(doc_no='GR9999001', supplier_name='ซัพพลายเออร์ B',
                            total=390.00, lines=None):
    return {
        'doc_no': doc_no, 'date_iso': '2024-06-01', 'supplier_name': supplier_name,
        'ref_doc': 'RR9999001', 'v_flag': 0, 'discount': 0.0, 'vat': 0.0,
        'total': total, 'is_cleared': False, 'is_void': False, 'type_code': None,
        'note': '',
        'lines': lines if lines is not None else [
            {'line_no': 1, 'product_code': '532ด6515', 'product_name': 'ดอกสว่าน',
             'qty': 3.0, 'unit': 'ดก', 'unit_price': 235.0, 'discount': '',
             'line_total': 390.00, 'is_cleared': False},
        ],
    }


def test_import_credit_notes_records_inserts_correctly(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute('PRAGMA foreign_keys = OFF')
    batch_id, company_id = _new_batch(conn, 'credit_notes')
    conn.commit()

    count, line_count = ie._import_credit_notes_records(
        conn, [_credit_note_ap_record()], batch_id, company_id)
    conn.commit()

    assert count == 1
    assert line_count == 1
    row = conn.execute(
        "SELECT total_amount, supplier_name, ref_doc FROM express_credit_notes "
        "WHERE doc_no='GR9999001'"
    ).fetchone()
    assert row[0] == pytest.approx(390.00), "must be TRNVAL-sourced total, not VAT-stripped"
    assert row[1] == 'ซัพพลายเออร์ B'
    assert row[2] == 'RR9999001'
    ln = conn.execute(
        "SELECT product_code, qty, line_total FROM express_credit_note_lines "
        "WHERE credit_note_id = (SELECT id FROM express_credit_notes WHERE doc_no='GR9999001')"
    ).fetchone()
    assert ln[0] == '532ด6515'
    assert ln[1] == pytest.approx(3.0)
    assert ln[2] == pytest.approx(390.00)
    conn.close()


def test_import_credit_notes_records_skips_existing_doc_no(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute('PRAGMA foreign_keys = OFF')

    batch1, company_id = _new_batch(conn, 'credit_notes')
    conn.commit()
    ie._import_credit_notes_records(conn, [_credit_note_ap_record()], batch1, company_id)
    conn.commit()

    batch2, _ = _new_batch(conn, 'credit_notes')
    conn.commit()
    count2, _ = ie._import_credit_notes_records(
        conn, [_credit_note_ap_record()], batch2, company_id)
    conn.commit()

    assert count2 == 0
    n = conn.execute(
        "SELECT COUNT(*) FROM express_credit_notes WHERE doc_no='GR9999001'"
    ).fetchone()[0]
    assert n == 1
    conn.close()


def test_import_credit_notes_records_zero_line_gr_ref_doc_null(tmp_db):
    """Zero-line GR master with ref_doc=None (MAPPING.md §6 open edge case) —
    inserts a header row with no lines, ref_doc stored as NULL."""
    conn = sqlite3.connect(tmp_db)
    conn.execute('PRAGMA foreign_keys = OFF')
    batch_id, company_id = _new_batch(conn, 'credit_notes')
    conn.commit()

    rec = _credit_note_ap_record(doc_no='GR9999003', total=0.0, lines=[])
    rec['ref_doc'] = None
    count, line_count = ie._import_credit_notes_records(conn, [rec], batch_id, company_id)
    conn.commit()

    assert count == 1
    assert line_count == 0
    row = conn.execute(
        "SELECT ref_doc, total_amount FROM express_credit_notes WHERE doc_no='GR9999003'"
    ).fetchone()
    assert row[0] is None
    assert row[1] == pytest.approx(0.0)
    conn.close()


# ── 3. run_import_records — the records-first entry point ──────────────────

def test_run_import_records_payments_out_end_to_end(tmp_db):
    result = ie.run_import_records('payments_out', [_payment_out_record()], db_path=tmp_db)

    assert result == {'imported': 1, 'skipped': 0, 'total': 1, 'lines': 1}
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT invoice_amount FROM express_payments_out WHERE doc_no='PS9999001'"
    ).fetchone()
    conn.close()
    assert row[0] == pytest.approx(8540.00)


def test_run_import_records_credit_notes_end_to_end(tmp_db):
    result = ie.run_import_records('credit_notes', [_credit_note_ap_record()], db_path=tmp_db)

    assert result == {'imported': 1, 'skipped': 0, 'total': 1, 'lines': 1}
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT total_amount FROM express_credit_notes WHERE doc_no='GR9999001'"
    ).fetchone()
    conn.close()
    assert row[0] == pytest.approx(390.00)


def test_run_import_records_idempotent_across_calls(tmp_db):
    """Two separate run_import_records calls (each its own batch) with the
    same doc_no → second call reports 0 imported, 1 skipped; no duplicate row."""
    r1 = ie.run_import_records('payments_out', [_payment_out_record()], db_path=tmp_db)
    r2 = ie.run_import_records('payments_out', [_payment_out_record()], db_path=tmp_db)

    assert r1['imported'] == 1
    assert r2['imported'] == 0 and r2['skipped'] == 1

    conn = sqlite3.connect(tmp_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM express_payments_out WHERE doc_no='PS9999001'"
    ).fetchone()[0]
    conn.close()
    assert n == 1


def test_run_import_records_unknown_file_type_raises(tmp_db):
    with pytest.raises(SystemExit):
        ie.run_import_records('ar_snapshot', [], db_path=tmp_db)


def test_run_import_records_rolls_back_whole_batch_on_error(tmp_db):
    """A bad record mid-list must roll back the WHOLE batch (no express_import_log
    row left behind either), matching run_import()'s all-or-nothing contract —
    unlike the per-record SAVEPOINT isolation used elsewhere in this codebase,
    run_import_records has no per-record recovery, by design (mirrors run_import)."""
    good = _payment_out_record(doc_no='PS0009999')
    bad = dict(good)
    bad['doc_no'] = None   # NOT NULL violation on express_payments_out.doc_no

    with pytest.raises(sqlite3.IntegrityError):
        ie.run_import_records('payments_out', [good, bad], db_path=tmp_db)

    conn = sqlite3.connect(tmp_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM express_payments_out WHERE doc_no='PS0009999'"
    ).fetchone()[0]
    log_n = conn.execute(
        "SELECT COUNT(*) FROM express_import_log WHERE source_filename='express_dbf' "
        "AND file_type='payments_out'"
    ).fetchone()[0]
    conn.close()
    assert n == 0, "the good record must NOT survive a batch that later fails"
    assert log_n == 0, "the batch log row must roll back too"


# ── 4. thin file-path wrappers still delegate correctly post-refactor ──────

import dataclasses


@dataclasses.dataclass
class _FakeAPPaymentDC:
    doc_no: str
    date_iso: str
    supplier_name: str
    is_void: bool = False
    deposit_applied: float = 0.0
    invoice_amount: float = 0.0
    cash_amount: float = 0.0
    cheque_amount: float = 0.0
    interest_amount: float = 0.0
    discount_amount: float = 0.0
    vat_amount: float = 0.0
    cheque_no: str = ''
    cheque_date_iso: str = ''
    bank: str = ''
    cheque_status: str = ''
    note: str = ''
    receive_refs: list = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class _FakeReceiveRefDC:
    receive_doc: str
    receive_date_iso: str
    invoice_ref: str
    amount: float


def test_import_payments_out_path_wrapper_still_delegates(tmp_db, monkeypatch):
    """_import_payments_out(conn, path, ...) — the file-path wrapper — must
    still convert parser output via dataclasses.asdict() and reach the same
    write logic as the records-first seam. Monkeypatch the real parser
    function (untouched by this refactor) to isolate the wrapper itself."""
    fake_record = _FakeAPPaymentDC(
        doc_no='PS9999002', date_iso='2026-05-05', supplier_name='ซัพพลายเออร์ C',
        invoice_amount=1234.56,
        receive_refs=[_FakeReceiveRefDC('RR6600300', '2026-05-04', None, 1234.56)],
    )
    monkeypatch.setattr(ie.p_pout, 'parse_payments_out', lambda path: [fake_record])

    conn = sqlite3.connect(tmp_db)
    conn.execute('PRAGMA foreign_keys = OFF')
    batch_id, company_id = _new_batch(conn, 'payments_out')
    conn.commit()

    count, line_count = ie._import_payments_out(conn, '/fake/path.csv', batch_id, company_id)
    conn.commit()

    assert count == 1
    assert line_count == 1
    row = conn.execute(
        "SELECT invoice_amount FROM express_payments_out WHERE doc_no='PS9999002'"
    ).fetchone()
    assert row[0] == pytest.approx(1234.56)
    conn.close()


@dataclasses.dataclass
class _FakeCreditNoteLineDC:
    line_no: int
    product_code: str
    product_name: str
    qty: float
    unit: str
    unit_price: float
    discount: str
    line_total: float
    is_cleared: bool


@dataclasses.dataclass
class _FakeCreditNoteDC:
    doc_no: str
    date_iso: str
    supplier_name: str
    ref_doc: str
    v_flag: int
    discount: float
    vat: float
    total: float
    is_cleared: bool
    is_void: bool
    type_code: int
    note: str = ''
    lines: list = dataclasses.field(default_factory=list)


def test_import_credit_notes_path_wrapper_still_delegates(tmp_db, monkeypatch):
    """_import_credit_notes(conn, path, ...) — the file-path wrapper — must
    still convert parser output via dataclasses.asdict() and reach the same
    write logic as the records-first seam."""
    fake_record = _FakeCreditNoteDC(
        doc_no='GR9999002', date_iso='2024-06-02', supplier_name='ซัพพลายเออร์ D',
        ref_doc='RR6700099', v_flag=0, discount=0.0, vat=0.0, total=777.0,
        is_cleared=False, is_void=False, type_code=2,
        lines=[_FakeCreditNoteLineDC(1, '999ก9999', 'สินค้าทดสอบ', 1.0, 'ชิ้น',
                                      777.0, '', 777.0, False)],
    )
    monkeypatch.setattr(ie.p_cn, 'parse_credit_notes', lambda path: [fake_record])

    conn = sqlite3.connect(tmp_db)
    conn.execute('PRAGMA foreign_keys = OFF')
    batch_id, company_id = _new_batch(conn, 'credit_notes')
    conn.commit()

    count, line_count = ie._import_credit_notes(conn, '/fake/path.csv', batch_id, company_id)
    conn.commit()

    assert count == 1
    assert line_count == 1
    row = conn.execute(
        "SELECT total_amount FROM express_credit_notes WHERE doc_no='GR9999002'"
    ).fetchone()
    assert row[0] == pytest.approx(777.0)
    conn.close()
