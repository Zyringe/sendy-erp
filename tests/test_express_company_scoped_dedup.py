"""Legacy Express import dedup must be scoped to company_id (Codex Express
Integration review 2026-08-20, P2).

scripts/import_express.py advertises `--company BSN|SD`, but its
read-before-write keys ignored company_id: `_existing_doc_nos()` selected
`doc_no` from the whole table, and the sales writer built its skip set from
every `(doc_no, line_no)` pair regardless of owner. So the SECOND company to
import a document number Express had reused was silently skipped, and its
rows never landed.

Every test here pairs the cross-company assertion with a SAME-company CONTROL.
Without the control these tests would also pass if dedup were deleted outright,
which is the opposite of the fix — so the control is what makes them able to
fail in both directions.
"""
import dataclasses
import os
import sqlite3
import sys

import pytest

_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts'))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import import_express as ie


# tmp_db clones the LIVE dev DB *with its data* (tests/conftest.py shutil.copy2,
# no wipe), so these doc numbers must be forced clear rather than assumed absent.
_PS = 'PS9911001'
_GR = 'GR9911001'
_RE = 'RE9911001'
_IV = 'IV9911001'


def _forced_conn(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute('PRAGMA foreign_keys = OFF')
    conn.execute("DELETE FROM express_payment_out_receive_refs WHERE payment_out_id IN"
                 " (SELECT id FROM express_payments_out WHERE doc_no = ?)", (_PS,))
    conn.execute("DELETE FROM express_payments_out WHERE doc_no = ?", (_PS,))
    conn.execute("DELETE FROM express_credit_note_lines WHERE credit_note_id IN"
                 " (SELECT id FROM express_credit_notes WHERE doc_no = ?)", (_GR,))
    conn.execute("DELETE FROM express_credit_notes WHERE doc_no = ?", (_GR,))
    conn.execute("DELETE FROM express_payment_in_invoice_refs WHERE payment_in_id IN"
                 " (SELECT id FROM express_payments_in WHERE doc_no = ?)", (_RE,))
    conn.execute("DELETE FROM express_payments_in WHERE doc_no = ?", (_RE,))
    conn.execute("DELETE FROM express_sales WHERE doc_no = ?", (_IV,))
    conn.commit()
    return conn


def _batch(conn, file_type, code):
    row = conn.execute('SELECT id FROM companies WHERE code = ?', (code,)).fetchone()
    company_id = row[0]
    cur = conn.execute(
        "INSERT INTO express_import_log (file_type, source_filename, company_id, status)"
        " VALUES (?, 'test', ?, 'imported')", (file_type, company_id))
    conn.commit()
    return cur.lastrowid, company_id


def _owners(conn, table, doc_no):
    return sorted(r[0] for r in conn.execute(
        f'SELECT company_id FROM {table} WHERE doc_no = ?', (doc_no,)))


# ── payments_out ────────────────────────────────────────────────────────────

def _payment_out(doc_no=_PS, invoice_amount=100.0):
    return {
        'doc_no': doc_no, 'date_iso': '2026-05-01', 'supplier_name': 'ผู้ขาย ก',
        'is_void': False, 'deposit_applied': 0.0, 'invoice_amount': invoice_amount,
        'cash_amount': 0.0, 'cheque_amount': 0.0, 'interest_amount': 0.0,
        'discount_amount': 0.0, 'vat_amount': 0.0, 'cheque_no': '',
        'cheque_date_iso': '', 'bank': '', 'cheque_status': '', 'note': '',
        'receive_refs': [{'receive_doc': 'RR001', 'receive_date_iso': None,
                          'invoice_ref': None, 'amount': invoice_amount}],
    }


def test_payments_out_dedup_is_scoped_to_company(tmp_db):
    conn = _forced_conn(tmp_db)

    b_bsn, id_bsn = _batch(conn, 'payments_out', 'BSN')
    n_bsn, _ = ie._import_payments_out_records(conn, [_payment_out()], b_bsn, id_bsn)
    conn.commit()
    assert n_bsn == 1, 'setup: the first company must import'

    b_sd, id_sd = _batch(conn, 'payments_out', 'SD')
    n_sd, _ = ie._import_payments_out_records(conn, [_payment_out()], b_sd, id_sd)
    conn.commit()
    assert n_sd == 1, "SD's own PS document must not be skipped because BSN has one"

    # CONTROL: same company, same doc_no is still deduped.
    b_again, _ = _batch(conn, 'payments_out', 'BSN')
    n_again, _ = ie._import_payments_out_records(conn, [_payment_out()], b_again, id_bsn)
    conn.commit()
    assert n_again == 0, 'same-company re-import must still be skipped'

    assert _owners(conn, 'express_payments_out', _PS) == sorted([id_bsn, id_sd])
    conn.close()


# ── credit_notes ────────────────────────────────────────────────────────────

def _credit_note(doc_no=_GR, total=390.0):
    return {
        'doc_no': doc_no, 'date_iso': '2026-05-01', 'supplier_name': 'ผู้ขาย ข',
        'ref_doc': 'RR9911001', 'v_flag': 0, 'discount': 0.0, 'vat': 0.0,
        'total': total, 'is_cleared': False, 'is_void': False, 'type_code': None,
        'note': '',
        'lines': [{'line_no': 1, 'product_code': '532ด6515', 'product_name': 'ดอกสว่าน',
                   'qty': 3.0, 'unit': 'ดก', 'unit_price': 130.0, 'discount': '',
                   'line_total': total, 'is_cleared': False}],
    }


def test_credit_notes_dedup_is_scoped_to_company(tmp_db):
    conn = _forced_conn(tmp_db)

    b_bsn, id_bsn = _batch(conn, 'credit_notes', 'BSN')
    n_bsn, _ = ie._import_credit_notes_records(conn, [_credit_note()], b_bsn, id_bsn)
    conn.commit()
    assert n_bsn == 1, 'setup: the first company must import'

    b_sd, id_sd = _batch(conn, 'credit_notes', 'SD')
    n_sd, _ = ie._import_credit_notes_records(conn, [_credit_note()], b_sd, id_sd)
    conn.commit()
    assert n_sd == 1, "SD's own GR document must not be skipped because BSN has one"

    b_again, _ = _batch(conn, 'credit_notes', 'BSN')
    n_again, _ = ie._import_credit_notes_records(conn, [_credit_note()], b_again, id_bsn)
    conn.commit()
    assert n_again == 0, 'same-company re-import must still be skipped'

    assert _owners(conn, 'express_credit_notes', _GR) == sorted([id_bsn, id_sd])
    conn.close()


# ── payments_in (file-path writer; parser monkeypatched) ────────────────────

@dataclasses.dataclass
class _Ref:
    invoice_no: str
    invoice_date_iso: str
    amount: float


@dataclasses.dataclass
class _PaymentIn:
    doc_no: str
    date_iso: str
    customer_name: str
    salesperson_code: str = '01'
    is_void: bool = False
    deposit_applied: float = 0.0
    invoice_amount: float = 500.0
    cash_amount: float = 500.0
    cheque_amount: float = 0.0
    interest_amount: float = 0.0
    discount_amount: float = 0.0
    vat_amount: float = 0.0
    cheque_no: str = ''
    cheque_date_iso: str = ''
    bank: str = ''
    cheque_status: str = ''
    note: str = ''
    invoice_refs: list = dataclasses.field(default_factory=list)


def test_payments_in_dedup_is_scoped_to_company(tmp_db, monkeypatch):
    rec = _PaymentIn(doc_no=_RE, date_iso='2026-05-01', customer_name='ลูกค้า ก',
                     invoice_refs=[_Ref('IV0001', '2026-04-30', 500.0)])
    monkeypatch.setattr(ie.p_pin, 'parse_payments_in', lambda path: [rec])
    conn = _forced_conn(tmp_db)

    b_bsn, id_bsn = _batch(conn, 'payments_in', 'BSN')
    n_bsn, _ = ie._import_payments_in(conn, '/fake.csv', b_bsn, id_bsn)
    conn.commit()
    assert n_bsn == 1, 'setup: the first company must import'

    b_sd, id_sd = _batch(conn, 'payments_in', 'SD')
    n_sd, _ = ie._import_payments_in(conn, '/fake.csv', b_sd, id_sd)
    conn.commit()
    assert n_sd == 1, "SD's own RE document must not be skipped because BSN has one"

    b_again, _ = _batch(conn, 'payments_in', 'BSN')
    n_again, _ = ie._import_payments_in(conn, '/fake.csv', b_again, id_bsn)
    conn.commit()
    assert n_again == 0, 'same-company re-import must still be skipped'

    assert _owners(conn, 'express_payments_in', _RE) == sorted([id_bsn, id_sd])
    conn.close()


# ── sales (file-path writer; parser monkeypatched) ──────────────────────────

@dataclasses.dataclass
class _SaleLine:
    doc_no: str
    line_no: int
    date_iso: str
    customer_code: str = ''
    customer_name: str = 'ลูกค้า ข'
    product_code: str = '532ด6515'
    product_name: str = 'ดอกสว่าน'
    qty: float = 2.0
    unit: str = 'ดก'
    return_flag: str = ''
    unit_price: float = 50.0
    vat_type: int = 0
    discount: str = ''
    total: float = 100.0
    total_discount: float = 0.0
    net: float = 100.0
    ref_doc: str = ''
    is_warning: bool = False


def test_sales_dedup_is_scoped_to_company(tmp_db, monkeypatch):
    rec = _SaleLine(doc_no=_IV, line_no=1, date_iso='2026-05-01')
    monkeypatch.setattr(ie.p_sales, 'parse_sales', lambda path: [rec])
    conn = _forced_conn(tmp_db)

    b_bsn, id_bsn = _batch(conn, 'sales', 'BSN')
    n_bsn, _ = ie._import_sales(conn, '/fake.csv', b_bsn, id_bsn)
    conn.commit()
    assert n_bsn == 1, 'setup: the first company must import'

    b_sd, id_sd = _batch(conn, 'sales', 'SD')
    n_sd, _ = ie._import_sales(conn, '/fake.csv', b_sd, id_sd)
    conn.commit()
    assert n_sd == 1, "SD's own IV line must not be skipped because BSN has one"

    b_again, _ = _batch(conn, 'sales', 'BSN')
    n_again, _ = ie._import_sales(conn, '/fake.csv', b_again, id_bsn)
    conn.commit()
    assert n_again == 0, 'same-company re-import must still be skipped'

    assert _owners(conn, 'express_sales', _IV) == sorted([id_bsn, id_sd])
    conn.close()
