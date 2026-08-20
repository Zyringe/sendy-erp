"""/ap must count a DBF-sourced supplier payment at its VERIFIED amount
(Codex Express Integration review 2026-08-20, P1).

inventory_app/express_dbf_source.py stores APTRN.RCVAMT — the one money field
Phase 0's reconciliation actually ties — in `invoice_amount`, and deliberately
leaves `cash_amount`/`cheque_amount` at 0 because the APRCPCQ-based cash-vs-
cheque split is unverified. Every /ap paid query summed `cash_amount +
cheque_amount`, so each new daily payment landed on the page worth ฿0.

The fallback is scoped to DBF-sourced rows ON PURPOSE. A manually imported row
whose split is genuinely 0/0 is NOT the same situation — the local snapshot has
one (PS0000E02: invoice_amount −2,052.00 offset by interest_amount 2,052.00),
and reinterpreting it would change a number nobody asked us to touch.

Everything here drives the REAL route, in a date window of its own so the
assertions are about seeded rows rather than whatever history the cloned dev DB
happens to carry.
"""
import os
import sqlite3
from contextlib import contextmanager

import pytest

os.environ.setdefault('SKIP_DB_INIT', '1')

FROM, TO = '2030-01-01', '2030-12-31'
_DATE = '2030-06-01'

DBF_DOC, DBF_AMOUNT = 'PS9933001', 83075.67           # verified RCVAMT, no split
MANUAL_DOC, MANUAL_PAID = 'PS9933002', 1000.00        # 600 cash + 400 cheque
MANUAL_INVOICE = 1234.00                              # deliberately != MANUAL_PAID
ZERO_DOC, ZERO_INVOICE = 'PS9933003', -2052.00        # the PS0000E02 shape


def _client(role='admin'):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = role
        s['role'] = role
    return c


@contextmanager
def _capture():
    """Assert on the values the route computed, not on a localized string."""
    from flask import template_rendered
    from app import app as a
    recorded = []

    def record(sender, template, context, **extra):
        recorded.append(context)

    template_rendered.connect(record, a)
    try:
        yield recorded
    finally:
        template_rendered.disconnect(record, a)


def _batch(conn, source_filename):
    cur = conn.execute(
        "INSERT INTO express_import_log (file_type, source_filename, company_id, status)"
        " VALUES ('payments_out', ?, 1, 'imported')", (source_filename,))
    return cur.lastrowid


@pytest.fixture
def seeded(tmp_db):
    """Force the window empty, then seed exactly the three shapes under test."""
    conn = sqlite3.connect(tmp_db)
    conn.execute('PRAGMA foreign_keys = OFF')
    conn.execute("DELETE FROM express_payments_out WHERE date_iso BETWEEN ? AND ?",
                 (FROM, TO))
    assert conn.execute(
        "SELECT COUNT(*) FROM express_payments_out WHERE date_iso BETWEEN ? AND ?",
        (FROM, TO)).fetchone()[0] == 0, 'setup: the window must start empty'

    dbf_batch = _batch(conn, 'express_dbf')
    manual_batch = _batch(conn, '/Volumes/ZYRINGE/express_data/จ่ายชำระหนี้.csv')
    conn.executemany(
        "INSERT INTO express_payments_out"
        " (batch_id, doc_no, date_iso, company_id, supplier_name, is_void,"
        "  invoice_amount, cash_amount, cheque_amount, interest_amount)"
        " VALUES (?, ?, ?, 1, ?, 0, ?, ?, ?, ?)",
        [
            (dbf_batch,    DBF_DOC,    _DATE, 'ผู้ขาย DBF',   DBF_AMOUNT,     0.0,   0.0, 0.0),
            (manual_batch, MANUAL_DOC, _DATE, 'ผู้ขายมือ',     MANUAL_INVOICE, 600.0, 400.0, 0.0),
            (manual_batch, ZERO_DOC,   _DATE, 'ผู้ขายดอกเบี้ย', ZERO_INVOICE,   0.0,   0.0, 2052.0),
        ])
    conn.commit()
    conn.close()
    return tmp_db


def _get(tab=None):
    url = f'/ap?from={FROM}&to={TO}' + (f'&tab={tab}' if tab else '')
    with _capture() as ctx:
        r = _client().get(url)
    assert r.status_code == 200, f'{url} returned {r.status_code}'
    assert ctx, 'no template rendered — the route did not reach render_template'
    return ctx[-1]


def test_dbf_row_with_no_split_contributes_its_verified_amount(seeded):
    """The whole finding: ฿83,075.67 of real payment was showing as ฿0."""
    recent = {r['doc_no']: r for r in _get('payments')['recent']}
    assert DBF_DOC in recent, 'control: the seeded DBF payment must be in range'
    assert recent[DBF_DOC]['paid'] == pytest.approx(DBF_AMOUNT), \
        'a DBF payment with no cash/cheque split must count at its verified RCVAMT'


def test_manual_row_keeps_its_populated_split(seeded):
    """MANUAL_INVOICE != MANUAL_PAID on purpose — a blanket switch to
    invoice_amount would show 1,234.00 here instead of the real 1,000.00."""
    recent = {r['doc_no']: r for r in _get('payments')['recent']}
    assert MANUAL_DOC in recent, 'control: the seeded manual payment must be in range'
    assert recent[MANUAL_DOC]['paid'] == pytest.approx(MANUAL_PAID)


def test_manual_row_with_no_split_is_left_alone(seeded):
    """The PS0000E02 shape. Its split is 0/0 but it is NOT DBF-sourced, so the
    verified-amount fallback must not reach it and flip the page to −2,052."""
    recent = {r['doc_no']: r for r in _get('payments')['recent']}
    assert ZERO_DOC in recent, 'control: the seeded interest row must be in range'
    assert recent[ZERO_DOC]['paid'] == pytest.approx(0.0), \
        'a manually imported zero-split row must keep its existing paid value'


def test_overview_total_is_the_sum_of_the_three_paid_values(seeded):
    assert _get()['summary']['total_paid'] == pytest.approx(
        DBF_AMOUNT + MANUAL_PAID + 0.0)


def test_the_three_ap_surfaces_agree(seeded):
    """Overview total, supplier grouping and the payment rows are three separate
    queries; the finding existed because they shared a WRONG expression, and the
    fix is only safe if they still share the SAME one."""
    overview = _get()['summary']['total_paid']
    suppliers = sum(r['paid_total'] for r in _get('suppliers')['pay_rows'])
    rows = sum(r['paid'] for r in _get('payments')['recent'])

    assert len(_get('payments')['recent']) == 3, 'control: all three rows in range'
    assert overview == pytest.approx(suppliers)
    assert overview == pytest.approx(rows)


def test_supplier_tab_shows_the_dbf_payment_against_its_supplier(seeded):
    paid = {r['supplier_name']: r['paid'] for r in _get('suppliers')['supplier_rows']}
    assert paid.get('ผู้ขาย DBF') == pytest.approx(DBF_AMOUNT)
