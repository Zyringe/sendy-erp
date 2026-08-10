"""I2: the two document pages must not label a non-stock line 'รอ' forever.

`sales_doc.html` / `purchases_doc.html` render the sync column as a green
check when `synced_to_stock` is truthy and a grey **รอ** ("waiting") badge
otherwise. A billable non-stock line is permanently `synced_to_stock = 0` by
design, so 'รอ' would read as an unfinished job to chase on every ค่าขนส่ง
line and — per design.md §1 — every bill on the active 14" clearance promo.
These two pages are read daily.

The third state is `ไม่นับสต็อก`, driven by `models.NON_STOCK_BSN_CODES`
passed in from the route (never a hard-coded list in Jinja — design.md §3.3).

FIXTURE DISCIPLINE (Task 3's STANDING RULE): every row these tests read is
INSERTed here, under a doc_base asserted absent first. Nothing is inherited
from the live-DB clone, so the assertions cannot be satisfied by state the
test did not create.

Assertions are on the rendered ELEMENT (`>ไม่นับสต็อก<`), never a bare Thai
substring, and every count is asserted before any property.
"""
import sqlite3

import pytest

SALES_DOC = 'IVNONSTOCKBADGE1'
PURCH_DOC = 'RRNONSTOCKBADGE1'
ORDINARY_CODE = 'NSBADGE-ORDINARY'      # deliberately NOT in NON_STOCK_BSN_CODES


@pytest.fixture
def admin_client(tmp_db):
    """Authed admin test client. Session injection, NOT a real login: this
    machine's Python has no hashlib.scrypt, so a real HTTP login against a
    scrypt-hashed user throws. Same pattern as tests/test_nonstock_precedence.py."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


def _seed(tmp_db, table, doc, code_col_extra, rows):
    """Insert three lines under `doc` covering all three badge states.

    Returns nothing; raises if the doc_base is not clean, so a colliding
    fixture fails loudly instead of asserting against inherited rows.
    """
    conn = sqlite3.connect(tmp_db, timeout=10)
    try:
        existing = conn.execute(
            "SELECT COUNT(*) FROM {} WHERE doc_no LIKE ?".format(table),
            (doc + '-%',)).fetchone()[0]
        assert existing == 0, (
            '{} already holds rows for {} — fixture would be inheriting state'
            .format(table, doc))
        conn.executemany(
            "INSERT INTO {} (date_iso, doc_no, product_id, bsn_code,"
            " product_name_raw, {}, qty, unit, unit_price, vat_type, total,"
            " net, synced_to_stock)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)".format(table, code_col_extra),
            rows)
        conn.commit()
        seeded = conn.execute(
            "SELECT COUNT(*) FROM {} WHERE doc_no LIKE ?".format(table),
            (doc + '-%',)).fetchone()[0]
        assert seeded == 3, 'seeded {} rows, expected 3'.format(seeded)
    finally:
        conn.close()


@pytest.fixture
def sales_doc_seeded(tmp_db):
    _seed(tmp_db, 'sales_transactions', SALES_DOC, 'customer', [
        # (date, doc_no, pid, bsn_code, raw_name, party, qty, unit,
        #  price, vat_type, total, net, synced)
        ('2026-08-01', SALES_DOC + '-1', None, ORDINARY_CODE, 'สินค้าปกติ',
         'ลูกค้าทดสอบ', 1, 'ตัว', 100.0, 1, 100.0, 100.0, 1),
        ('2026-08-01', SALES_DOC + '-2', None, 'ZZZ', 'ส่วนลดพิเศษ',
         'ลูกค้าทดสอบ', 1, 'ครั้ง', -50.0, 1, -50.0, -50.0, 0),
        ('2026-08-01', SALES_DOC + '-3', None, ORDINARY_CODE, 'สินค้ายังไม่ sync',
         'ลูกค้าทดสอบ', 1, 'ตัว', 100.0, 1, 100.0, 100.0, 0),
    ])
    return SALES_DOC


@pytest.fixture
def purchases_doc_seeded(tmp_db):
    _seed(tmp_db, 'purchase_transactions', PURCH_DOC, 'supplier', [
        ('2026-08-01', PURCH_DOC + '-1', None, ORDINARY_CODE, 'สินค้าปกติ',
         'ผู้ขายทดสอบ', 1, 'ตัว', 100.0, 1, 100.0, 100.0, 1),
        ('2026-08-01', PURCH_DOC + '-2', None, '888ค8888', 'ค่าขนส่ง',
         'ผู้ขายทดสอบ', 1, 'ครั้ง', 30.0, 1, 30.0, 30.0, 0),
        ('2026-08-01', PURCH_DOC + '-3', None, ORDINARY_CODE, 'สินค้ายังไม่ sync',
         'ผู้ขายทดสอบ', 1, 'ตัว', 100.0, 1, 100.0, 100.0, 0),
    ])
    return PURCH_DOC


def _assert_three_states(html):
    """One line per badge state, on the rendered element.

    Counts first, so nothing can pass on an empty render. `>รอ<` and
    `>ไม่นับสต็อก<` are matched with their tag boundaries: a bare Thai
    substring test would be satisfied by unrelated page chrome.
    """
    assert '<table' in html, 'page did not render a table at all'
    assert html.count('>ไม่นับสต็อก<') == 1, (
        'expected exactly 1 non-stock badge, got {}'
        .format(html.count('>ไม่นับสต็อก<')))
    assert html.count('>รอ<') == 1, (
        'the ORDINARY unsynced line must still say รอ — got {}'
        .format(html.count('>รอ<')))
    assert html.count('bi-check2') == 1, (
        'the ordinary synced line must still show the green check — got {}'
        .format(html.count('bi-check2')))


def test_sales_doc_shows_non_stock_instead_of_pending(admin_client,
                                                      sales_doc_seeded):
    resp = admin_client.get('/sales/doc/' + sales_doc_seeded)
    assert resp.status_code == 200, resp.status_code
    _assert_three_states(resp.get_data(as_text=True))


def test_purchases_doc_shows_non_stock_instead_of_pending(admin_client,
                                                          purchases_doc_seeded):
    resp = admin_client.get('/purchases/doc/' + purchases_doc_seeded)
    assert resp.status_code == 200, resp.status_code
    _assert_three_states(resp.get_data(as_text=True))


def test_badge_list_comes_from_the_constant_not_a_jinja_literal(admin_client,
                                                                sales_doc_seeded):
    """The route must pass models.NON_STOCK_BSN_CODES through, so adding a
    code to the constant reaches these pages with no template edit.

    Proved by monkeypatching nothing and instead checking the negative: the
    ORDINARY code is not treated as non-stock (count 1, asserted above) while
    ZZZ is — which can only be true if the template consults the passed list.
    The positive half is the grep below: neither template may contain a code
    literal.
    """
    import os
    tpl_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'inventory_app', 'templates')
    checked = 0
    for name in ('sales_doc.html', 'purchases_doc.html'):
        with open(os.path.join(tpl_dir, name), encoding='utf-8') as f:
            body = f.read()
        checked += 1
        # control: the template really is the one we think it is
        assert 'ไม่นับสต็อก' in body, name + ' lost the non-stock badge'
        assert 'ZZZ' not in body, name + ' hard-codes a non-stock code'
        assert '888' not in body, name + ' hard-codes a non-stock code'
        assert 'non_stock_codes' in body, name + ' does not read the passed list'
    assert checked == 2
