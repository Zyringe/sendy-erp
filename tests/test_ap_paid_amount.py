"""/ap must count a DBF-sourced supplier payment at what was actually paid.

History: the Codex Express Integration review (2026-08-20, P1) found every /ap
paid query summing `cash_amount + cheque_amount` while the DBF adapter left both
at 0, so each new daily payment landed on the page worth ฿0. The first fix was a
`_AP_PAID_AMOUNT` CASE that fell back to `invoice_amount` for DBF-sourced rows.

That premise was false at the source. APTRN headers DO carry the split —
CSHPAY + CHQPAY + DISCAMT − INTPAY == RCVAMT on 1985/1985 of BSN5657's PS
headers (2026-08-20) — so the adapter now reads it and the CASE is gone. The
population that made the fallback look necessary is the 17 headers with no
split, and reading them straight is not merely acceptable but *more* correct:
7 are interest offsets whose RCVAMT is negative (PS0000E02: −2,052.00 against
INTPAY 2,052.00) and 10 are ฿0.00 documents. The fallback would have rendered
those 7 as money paid.

Unlike the version this replaces, every row here arrives through the REAL
adapter and the REAL importer — hand-seeded rows cannot observe adapter-level
facts, which is exactly how the false premise survived a green suite.
"""
import datetime
import os
import sys
from contextlib import contextmanager

import pytest

os.environ.setdefault('SKIP_DB_INIT', '1')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import import_express as ie                                        # noqa: E402
from express_dbf_source import build_payments_out_records          # noqa: E402

FROM, TO = '2030-01-01', '2030-12-31'
_DOCDAT = datetime.date(2030, 6, 1)

SPLIT_DOC,  SPLIT_PAID  = 'PS9933001', 83075.67    # 80,000.00 cash + 3,075.67 cheque
CHEQUE_DOC, CHEQUE_PAID = 'PS9933002', 12000.00    # cheque only
OFFSET_DOC, OFFSET_RCV  = 'PS9933003', -2052.00    # the PS0000E02 shape
VOID_DOC,   VOID_PAID   = 'PS9933004', 9999.99     # DOCSTAT 'C'


def _aptrn_ps(docnum, *, rcvamt, cshpay=0.0, chqpay=0.0, discamt=0.0, intpay=0.0,
              docstat='N', supcod='S900'):
    return {
        'DOCNUM': docnum, 'RECTYP': '9', 'SUPCOD': supcod, 'DOCDAT': _DOCDAT,
        'RCVAMT': rcvamt, 'CSHPAY': cshpay, 'CHQPAY': chqpay,
        'DISCAMT': discamt, 'INTPAY': intpay, 'DOCSTAT': docstat, 'YOUREF': '',
    }


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


@pytest.fixture
def seeded(tmp_db):
    """Force the window empty (tmp_db clones the live dev DB WITH its data),
    then import the four shapes through the adapter + importer."""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.execute('PRAGMA foreign_keys = OFF')
    conn.execute("DELETE FROM express_payment_out_receive_refs WHERE payment_out_id IN"
                 " (SELECT id FROM express_payments_out WHERE date_iso BETWEEN ? AND ?)",
                 (FROM, TO))
    conn.execute("DELETE FROM express_payments_out WHERE date_iso BETWEEN ? AND ?",
                 (FROM, TO))
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM express_payments_out WHERE date_iso BETWEEN ? AND ?",
        (FROM, TO)).fetchone()[0] == 0, 'setup: the window must start empty'
    conn.close()

    aptrn = [
        _aptrn_ps(SPLIT_DOC,  rcvamt=SPLIT_PAID,  cshpay=80000.00, chqpay=3075.67),
        _aptrn_ps(CHEQUE_DOC, rcvamt=CHEQUE_PAID, chqpay=CHEQUE_PAID),
        _aptrn_ps(OFFSET_DOC, rcvamt=OFFSET_RCV,  intpay=2052.00),
        _aptrn_ps(VOID_DOC,   rcvamt=VOID_PAID,   cshpay=VOID_PAID, docstat='C'),
    ]
    records = build_payments_out_records(aptrn, [], [])
    assert len(records) == 4, 'setup: the adapter must emit all four headers'
    ie.run_import_records('payments_out', records, db_path=tmp_db)
    return tmp_db


def _get(tab=None):
    url = f'/ap?from={FROM}&to={TO}' + (f'&tab={tab}' if tab else '')
    with _capture() as ctx:
        r = _client().get(url)
    assert r.status_code == 200, f'{url} returned {r.status_code}'
    assert ctx, 'no template rendered — the route did not reach render_template'
    return ctx[-1]


def _recent():
    return {r['doc_no']: r for r in _get('payments')['recent']}


def test_a_dbf_payment_is_worth_its_cash_and_cheque_split(seeded):
    """The original finding: ฿83,075.67 of real payment showed as ฿0."""
    recent = _recent()
    assert SPLIT_DOC in recent, 'control: the seeded payment must be in range'
    assert recent[SPLIT_DOC]['paid'] == pytest.approx(SPLIT_PAID)


def test_a_cheque_only_payment_is_not_lost(seeded):
    recent = _recent()
    assert CHEQUE_DOC in recent, 'control: the seeded cheque payment must be in range'
    assert recent[CHEQUE_DOC]['paid'] == pytest.approx(CHEQUE_PAID)


def test_an_interest_offset_is_worth_nothing_paid(seeded):
    """No money left the bank: RCVAMT is negative because the invoice was netted
    against interest. Falling back to invoice_amount would render −฿2,052.00 as
    an amount paid — across BSN5657 that shape totals −฿224,567.22."""
    recent = _recent()
    assert OFFSET_DOC in recent, 'control: the seeded offset row must be in range'
    assert recent[OFFSET_DOC]['paid'] == pytest.approx(0.0)


def test_a_cancelled_payment_is_off_the_page(seeded):
    """DOCSTAT 'C' now reaches is_void, and /ap filters is_void = 0."""
    assert VOID_DOC not in _recent()


def test_overview_total_is_the_sum_of_the_live_payments(seeded):
    assert _get()['summary']['total_paid'] == pytest.approx(
        SPLIT_PAID + CHEQUE_PAID + 0.0)


def test_the_three_ap_surfaces_agree(seeded):
    """Overview total, supplier grouping and the payment rows are three separate
    queries; the finding existed because they shared a WRONG expression, and any
    fix is only safe if they still share the SAME one."""
    overview = _get()['summary']['total_paid']
    suppliers = sum(r['paid_total'] for r in _get('suppliers')['pay_rows'])
    rows = sum(r['paid'] for r in _get('payments')['recent'])

    assert len(_get('payments')['recent']) == 3, 'control: three live rows in range'
    assert overview == pytest.approx(suppliers)
    assert overview == pytest.approx(rows)
