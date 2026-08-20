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


def test_run_import_records_identical_replay_is_a_no_op_in_business_state(tmp_db):
    """Replaying an UNCHANGED daily zip must leave the book exactly as it was.

    This used to assert `imported == 0, skipped == 1`, which pinned the wrong
    thing: the DBF path skipped by doc_no, so a document Express had CORRECTED
    was skipped just as permanently as an unchanged one (Codex Express
    Integration review 2026-08-20, P1). The refresh now rewrites the document,
    so the contract that matters is the resulting business state — same row
    count, same values, same child set — not whether a write was skipped."""
    _forget(tmp_db)
    rec = _payment_out_record(doc_no=_RPS)

    r1 = ie.run_import_records('payments_out', [rec], db_path=tmp_db)
    before_header, before_refs = _pout_business_state(tmp_db), _pout_refs(tmp_db)
    assert r1['imported'] == 1
    assert len(before_header) == 1 and len(before_refs) == 1, 'setup'

    ie.run_import_records('payments_out', [rec], db_path=tmp_db)

    assert _pout_business_state(tmp_db) == before_header, \
        'an identical replay must not change the document'
    assert _pout_refs(tmp_db) == before_refs, \
        'an identical replay must not change or duplicate its children'


def test_run_import_records_unknown_file_type_raises(tmp_db):
    # 'sales' is a real run_import() file_type with NO records-first writer —
    # it flows through models.py. (This used to use 'ar_snapshot', which gained
    # a records importer on 2026-08-17 when the daily DBF zip started carrying
    # the outstanding snapshots.)
    with pytest.raises(SystemExit):
        ie.run_import_records('sales', [], db_path=tmp_db)


def test_run_import_records_snapshot_without_a_date_raises(tmp_db):
    """A snapshot with no as-of date would land under snapshot_date_iso NULL,
    where MAX(snapshot_date_iso) can never see it — the rows would import
    'successfully' and be invisible to every AR/AP reader."""
    with pytest.raises(ValueError, match='snapshot_date'):
        ie.run_import_records('ar_snapshot', [], db_path=tmp_db)


def test_run_import_records_rolls_back_whole_batch_on_error(tmp_db):
    """A bad record mid-list must roll back the WHOLE batch (no express_import_log
    row left behind either), matching run_import()'s all-or-nothing contract —
    unlike the per-record SAVEPOINT isolation used elsewhere in this codebase,
    run_import_records has no per-record recovery, by design (mirrors run_import)."""
    good = _payment_out_record(doc_no='PS0009999')
    bad = dict(good)
    bad['doc_no'] = None   # NOT NULL violation on express_payments_out.doc_no

    # tmp_db CLONES THE LIVE DEV DB *WITH ITS DATA* (tests/conftest.py does a
    # shutil.copy2, there is no wipe), so this DB already carries every
    # express_dbf batch row the machine has ever imported — 26 of them today.
    # Asserting a global COUNT == 0 therefore failed on any developer machine
    # that had run a daily import, permanently, for a reason having nothing to
    # do with rollback. Force the state instead of inheriting it.
    seed = sqlite3.connect(tmp_db)
    seed.execute("DELETE FROM express_import_log "
                 "WHERE source_filename='express_dbf' AND file_type='payments_out'")
    seed.commit()
    seed.close()

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


# ── 5. DBF-direct path refreshes an existing document (Codex P1, 2026-08-20) ──
#
# run_import_records IS the Express DBF-direct entry point, and the daily zip is
# authoritative for the documents it carries: `cutoff` filters HEADERS, never the
# child rows within one, so every document it includes arrives with its COMPLETE
# child set (same property import_router relies on for payments_in). That is what
# makes replacement safe here and wrong on the text-report path, where a printed
# report can legitimately be partial — so the writers keep their incremental skip
# by default and only replace when the DBF caller asks.

_RPS = 'PS9922001'
_RGR = 'GR9922001'


def _forget(tmp_db, *, file_type=None):
    """Force the state instead of inheriting it — tmp_db clones the live dev DB
    WITH its data (see the rollback test above)."""
    conn = sqlite3.connect(tmp_db)
    conn.execute('PRAGMA foreign_keys = OFF')
    conn.execute("DELETE FROM express_payment_out_receive_refs WHERE payment_out_id IN"
                 " (SELECT id FROM express_payments_out WHERE doc_no = ?)", (_RPS,))
    conn.execute("DELETE FROM express_payments_out WHERE doc_no = ?", (_RPS,))
    conn.execute("DELETE FROM express_credit_note_lines WHERE credit_note_id IN"
                 " (SELECT id FROM express_credit_notes WHERE doc_no = ?)", (_RGR,))
    conn.execute("DELETE FROM express_credit_notes WHERE doc_no = ?", (_RGR,))
    if file_type:
        conn.execute("DELETE FROM express_import_log WHERE source_filename='express_dbf'"
                     " AND file_type = ?", (file_type,))
    conn.commit()
    conn.close()


def _pout_row(tmp_db, doc_no=_RPS):
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT id, invoice_amount, supplier_name, date_iso, company_id"
        "  FROM express_payments_out WHERE doc_no = ?", (doc_no,)).fetchall()
    conn.close()
    return row


def _pout_business_state(tmp_db, doc_no=_RPS):
    """Everything about the document that the business cares about — id and
    batch_id deliberately excluded, since a refresh legitimately rewrites both."""
    conn = sqlite3.connect(tmp_db)
    rows = conn.execute(
        "SELECT doc_no, date_iso, company_id, supplier_name, is_void, deposit_applied,"
        "       invoice_amount, cash_amount, cheque_amount, interest_amount,"
        "       discount_amount, vat_amount, note"
        "  FROM express_payments_out WHERE doc_no = ? ORDER BY company_id",
        (doc_no,)).fetchall()
    conn.close()
    return rows


def _pout_refs(tmp_db, doc_no=_RPS):
    conn = sqlite3.connect(tmp_db)
    refs = conn.execute(
        "SELECT receive_doc, amount FROM express_payment_out_receive_refs"
        "  WHERE payment_out_id IN (SELECT id FROM express_payments_out WHERE doc_no = ?)"
        "  ORDER BY receive_doc", (doc_no,)).fetchall()
    conn.close()
    return refs


def _cn_lines(tmp_db, doc_no=_RGR):
    conn = sqlite3.connect(tmp_db)
    lines = conn.execute(
        "SELECT line_no, product_code, line_total FROM express_credit_note_lines"
        "  WHERE credit_note_id IN (SELECT id FROM express_credit_notes WHERE doc_no = ?)"
        "  ORDER BY line_no", (doc_no,)).fetchall()
    conn.close()
    return lines


def test_dbf_replay_refreshes_a_corrected_payment_out(tmp_db):
    """Express corrected PS9922001's amount, supplier and allocation. The next
    daily zip must make Sendy match — header AND children become exactly B."""
    _forget(tmp_db)
    a = _payment_out_record(doc_no=_RPS, supplier_name='ผู้ขายเดิม', invoice_amount=8540.00)
    ie.run_import_records('payments_out', [a], db_path=tmp_db)
    assert _pout_row(tmp_db)[0][1] == pytest.approx(8540.00), 'setup'

    b = _payment_out_record(doc_no=_RPS, supplier_name='ผู้ขายที่แก้แล้ว', invoice_amount=9999.99)
    b['receive_refs'] = [{'receive_doc': 'RR7700777', 'receive_date_iso': None,
                          'invoice_ref': None, 'amount': 9999.99}]
    ie.run_import_records('payments_out', [b], db_path=tmp_db)

    rows = _pout_row(tmp_db)
    assert len(rows) == 1, 'refresh must replace, never duplicate'
    assert rows[0][1] == pytest.approx(9999.99), 'corrected amount must land'
    assert rows[0][2] == 'ผู้ขายที่แก้แล้ว', 'corrected supplier must land'
    assert _pout_refs(tmp_db) == [('RR7700777', pytest.approx(9999.99))], \
        'the child set must be exactly B, with A\'s allocation gone'


def test_dbf_replay_refreshes_a_corrected_credit_note(tmp_db):
    """Same contract on the GR side: header total and the whole line set."""
    _forget(tmp_db)
    a = _credit_note_ap_record(doc_no=_RGR, supplier_name='ผู้ขายเดิม', total=390.00)
    ie.run_import_records('credit_notes', [a], db_path=tmp_db)
    conn = sqlite3.connect(tmp_db)
    assert conn.execute("SELECT total_amount FROM express_credit_notes WHERE doc_no=?",
                        (_RGR,)).fetchone()[0] == pytest.approx(390.00), 'setup'
    conn.close()

    b = _credit_note_ap_record(
        doc_no=_RGR, supplier_name='ผู้ขายที่แก้แล้ว', total=1250.00,
        lines=[{'line_no': 1, 'product_code': '999x9999', 'product_name': 'ของใหม่',
                'qty': 5.0, 'unit': 'ตัว', 'unit_price': 250.0, 'discount': '',
                'line_total': 1250.00, 'is_cleared': False}])
    ie.run_import_records('credit_notes', [b], db_path=tmp_db)

    conn = sqlite3.connect(tmp_db)
    rows = conn.execute("SELECT total_amount, supplier_name FROM express_credit_notes"
                        " WHERE doc_no=?", (_RGR,)).fetchall()
    conn.close()
    assert len(rows) == 1, 'refresh must replace, never duplicate'
    assert rows[0][0] == pytest.approx(1250.00)
    assert rows[0][1] == 'ผู้ขายที่แก้แล้ว'
    assert _cn_lines(tmp_db) == [(1, '999x9999', pytest.approx(1250.00))], \
        'the line set must be exactly B'


def test_dbf_replay_drops_a_child_that_vanished(tmp_db):
    """The hard half of 'exactly B': a child Express REMOVED must disappear.
    An upsert that only writes the incoming rows would leave it behind."""
    _forget(tmp_db)
    a = _payment_out_record(doc_no=_RPS, invoice_amount=500.0)
    a['receive_refs'] = [
        {'receive_doc': 'RR001', 'receive_date_iso': None, 'invoice_ref': None, 'amount': 300.0},
        {'receive_doc': 'RR002', 'receive_date_iso': None, 'invoice_ref': None, 'amount': 200.0},
    ]
    ie.run_import_records('payments_out', [a], db_path=tmp_db)
    assert len(_pout_refs(tmp_db)) == 2, 'setup: two allocations'

    b = _payment_out_record(doc_no=_RPS, invoice_amount=300.0)
    b['receive_refs'] = [
        {'receive_doc': 'RR001', 'receive_date_iso': None, 'invoice_ref': None, 'amount': 300.0},
    ]
    ie.run_import_records('payments_out', [b], db_path=tmp_db)

    assert _pout_refs(tmp_db) == [('RR001', pytest.approx(300.0))], \
        'RR002 was removed in Express and must be gone from Sendy'


def test_dbf_replay_drops_a_credit_note_line_that_vanished(tmp_db):
    _forget(tmp_db)
    a = _credit_note_ap_record(doc_no=_RGR, total=300.0, lines=[
        {'line_no': 1, 'product_code': 'A1', 'product_name': 'ก', 'qty': 1.0, 'unit': 'ตัว',
         'unit_price': 100.0, 'discount': '', 'line_total': 100.0, 'is_cleared': False},
        {'line_no': 2, 'product_code': 'B2', 'product_name': 'ข', 'qty': 1.0, 'unit': 'ตัว',
         'unit_price': 200.0, 'discount': '', 'line_total': 200.0, 'is_cleared': False},
    ])
    ie.run_import_records('credit_notes', [a], db_path=tmp_db)
    assert len(_cn_lines(tmp_db)) == 2, 'setup: two lines'

    b = _credit_note_ap_record(doc_no=_RGR, total=100.0, lines=[
        {'line_no': 1, 'product_code': 'A1', 'product_name': 'ก', 'qty': 1.0, 'unit': 'ตัว',
         'unit_price': 100.0, 'discount': '', 'line_total': 100.0, 'is_cleared': False},
    ])
    ie.run_import_records('credit_notes', [b], db_path=tmp_db)

    assert _cn_lines(tmp_db) == [(1, 'A1', pytest.approx(100.0))], \
        'line 2 was removed in Express and must be gone from Sendy'


def test_dbf_replace_failure_restores_the_whole_prior_document(tmp_db):
    """A failure AFTER the old document was cleared must restore it complete —
    header, children and batch state. This is what makes the refresh atomic
    rather than a delete that can strand a document with nothing in its place."""
    _forget(tmp_db, file_type='payments_out')
    a = _payment_out_record(doc_no=_RPS, supplier_name='ผู้ขายเดิม', invoice_amount=8540.00)
    a['receive_refs'] = [
        {'receive_doc': 'RR001', 'receive_date_iso': None, 'invoice_ref': None, 'amount': 8540.00},
    ]
    ie.run_import_records('payments_out', [a], db_path=tmp_db)
    before_rows, before_refs = _pout_row(tmp_db), _pout_refs(tmp_db)
    assert len(before_rows) == 1 and len(before_refs) == 1, 'setup'

    changed = _payment_out_record(doc_no=_RPS, supplier_name='ผู้ขายใหม่', invoice_amount=1.0)
    bad = _payment_out_record(doc_no='PS9922002')
    bad['doc_no'] = None            # NOT NULL violation, raised after the replace
    with pytest.raises(sqlite3.IntegrityError):
        ie.run_import_records('payments_out', [changed, bad], db_path=tmp_db)

    assert _pout_row(tmp_db) == before_rows, \
        'the prior document must survive intact, same row and same id'
    assert _pout_refs(tmp_db) == before_refs, 'its children must survive too'
    conn = sqlite3.connect(tmp_db)
    n_log = conn.execute(
        "SELECT COUNT(*) FROM express_import_log WHERE source_filename='express_dbf'"
        " AND file_type='payments_out'").fetchone()[0]
    conn.close()
    assert n_log == 1, 'only the successful first batch may remain'


def test_dbf_replace_is_scoped_to_company(tmp_db):
    """Refreshing BSN's PS9922001 must not touch SD's document of the same
    number — identity is (company_id, doc_no), never doc_no alone."""
    _forget(tmp_db)
    sd = _payment_out_record(doc_no=_RPS, supplier_name='ของ SD', invoice_amount=111.0)
    ie.run_import_records('payments_out', [sd], company_code='SD', db_path=tmp_db)
    bsn = _payment_out_record(doc_no=_RPS, supplier_name='ของ BSN', invoice_amount=222.0)
    ie.run_import_records('payments_out', [bsn], company_code='BSN', db_path=tmp_db)
    assert len(_pout_row(tmp_db)) == 2, 'setup: one document per company'

    corrected = _payment_out_record(doc_no=_RPS, supplier_name='BSN แก้แล้ว', invoice_amount=333.0)
    ie.run_import_records('payments_out', [corrected], company_code='BSN', db_path=tmp_db)

    conn = sqlite3.connect(tmp_db)
    got = dict(conn.execute(
        "SELECT c.code, p.invoice_amount FROM express_payments_out p"
        "  JOIN companies c ON c.id = p.company_id WHERE p.doc_no = ?", (_RPS,)).fetchall())
    conn.close()
    assert got == {'SD': pytest.approx(111.0), 'BSN': pytest.approx(333.0)}, \
        "SD's document must be untouched by BSN's refresh"


# ── 6. what the refresh must NOT weaken (independent review, 2026-08-20) ─────

def _orphan_refs(tmp_db):
    conn = sqlite3.connect(tmp_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM express_payment_out_receive_refs r"
        " WHERE NOT EXISTS (SELECT 1 FROM express_payments_out p WHERE p.id = r.payment_out_id)"
    ).fetchone()[0]
    conn.close()
    return n


def _orphan_cn_lines(tmp_db):
    conn = sqlite3.connect(tmp_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM express_credit_note_lines l"
        " WHERE NOT EXISTS (SELECT 1 FROM express_credit_notes c WHERE c.id = l.credit_note_id)"
    ).fetchone()[0]
    conn.close()
    return n


def test_replacing_a_payment_out_strands_no_orphan_children(tmp_db):
    """The child DELETE in _delete_existing_doc is the whole reason that helper
    exists (`PRAGMA foreign_keys = OFF` means ON DELETE CASCADE never fires).
    Nothing pinned it: every other child assertion reads THROUGH the header
    (`WHERE payment_out_id IN (SELECT id ... WHERE doc_no = ?)`), and an orphan's
    FK matches no header, so those queries are structurally incapable of seeing
    a leak. Assert on the orphan set directly."""
    _forget(tmp_db)
    assert _orphan_refs(tmp_db) == 0, 'setup control: the clone must start clean'

    a = _payment_out_record(doc_no=_RPS)
    a['receive_refs'] = [
        {'receive_doc': 'RR001', 'receive_date_iso': None, 'invoice_ref': None, 'amount': 1.0},
        {'receive_doc': 'RR002', 'receive_date_iso': None, 'invoice_ref': None, 'amount': 2.0},
    ]
    ie.run_import_records('payments_out', [a], db_path=tmp_db)
    b = _payment_out_record(doc_no=_RPS)
    b['receive_refs'] = [
        {'receive_doc': 'RR001', 'receive_date_iso': None, 'invoice_ref': None, 'amount': 1.0},
    ]
    ie.run_import_records('payments_out', [b], db_path=tmp_db)
    ie.run_import_records('payments_out', [b], db_path=tmp_db)

    assert _orphan_refs(tmp_db) == 0, \
        'the superseded allocations must be DELETED, not left pointing at a dead header id'


def test_replacing_a_credit_note_strands_no_orphan_lines(tmp_db):
    _forget(tmp_db)
    assert _orphan_cn_lines(tmp_db) == 0, 'setup control: the clone must start clean'

    a = _credit_note_ap_record(doc_no=_RGR, lines=[
        {'line_no': 1, 'product_code': 'A1', 'product_name': 'ก', 'qty': 1.0, 'unit': 'ตัว',
         'unit_price': 1.0, 'discount': '', 'line_total': 1.0, 'is_cleared': False},
        {'line_no': 2, 'product_code': 'B2', 'product_name': 'ข', 'qty': 1.0, 'unit': 'ตัว',
         'unit_price': 2.0, 'discount': '', 'line_total': 2.0, 'is_cleared': False},
    ])
    ie.run_import_records('credit_notes', [a], db_path=tmp_db)
    b = _credit_note_ap_record(doc_no=_RGR, lines=[
        {'line_no': 1, 'product_code': 'A1', 'product_name': 'ก', 'qty': 1.0, 'unit': 'ตัว',
         'unit_price': 1.0, 'discount': '', 'line_total': 1.0, 'is_cleared': False},
    ])
    ie.run_import_records('credit_notes', [b], db_path=tmp_db)
    ie.run_import_records('credit_notes', [b], db_path=tmp_db)

    assert _orphan_cn_lines(tmp_db) == 0, \
        'the superseded lines must be DELETED, not left pointing at a dead header id'


def test_a_duplicate_doc_no_inside_one_batch_still_fails_loudly(tmp_db):
    """`UNIQUE(batch_id, doc_no)` is the only structural duplicate guard these
    tables have. The refresh must clear PRIOR batches, never the row this same
    batch just wrote — otherwise two records for one doc_no in a single import
    silently become last-write-wins instead of rolling the batch back, and the
    reported record_count over-counts what actually landed."""
    _forget(tmp_db, file_type='payments_out')
    first = _payment_out_record(doc_no=_RPS, invoice_amount=100.0)
    second = _payment_out_record(doc_no=_RPS, invoice_amount=999.0)

    with pytest.raises(sqlite3.IntegrityError):
        ie.run_import_records('payments_out', [first, second], db_path=tmp_db)

    assert _pout_row(tmp_db) == [], 'the whole batch must roll back, leaving nothing'
    conn = sqlite3.connect(tmp_db)
    n_log = conn.execute(
        "SELECT COUNT(*) FROM express_import_log WHERE source_filename='express_dbf'"
        " AND file_type='payments_out'").fetchone()[0]
    conn.close()
    assert n_log == 0, 'the batch log row must roll back too'
