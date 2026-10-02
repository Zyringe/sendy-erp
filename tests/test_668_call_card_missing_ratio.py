"""#668: the call card's ราคาตั้ง for a line bought in a unit with no ratio.

Before: the card looked the ratio up by the bill's exact spelling and, on a
miss, showed the base-unit list price (฿100 per ตัว) beside a line bought in
กล่อง. Put's decisions (2026-10-02): such a row shows "ไม่มีอัตราแปลง" instead
of a price (option A), and the card finds ratios by unit word the way the
resolver does, so `หล` reads the `โหล` row.

Every product: unit_type ตัว, base_sell_price 100 (tests/_purchase_history_fixture).
"""
import os
import re

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

import call_card
from tests._purchase_history_fixture import add_line, mk_product

CODE = 'Z668'
NAME = 'ร้านทดสอบหกหกแปด'


@pytest.fixture
def conn(empty_db_conn):
    c = empty_db_conn
    c.execute("INSERT INTO customers (code, name) VALUES (?, ?)", (CODE, NAME))
    c.execute("INSERT INTO unit_map (book, spelling, word) VALUES ('BSN5657', 'หล', 'โหล')")
    c.commit()
    return c


def _row(conn, unit, ratios=(), promo=None):
    pid = mk_product(conn, f'สินค้าหกหกแปด {unit!r}')
    conn.executemany("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?, ?, ?)",
                     [(pid, u, r) for u, r in ratios])
    if promo is not None:
        conn.execute("INSERT INTO promotions (product_id, promo_name, promo_type, discount_value, "
                     "is_active) VALUES (?, 'fixed 8', 'fixed', ?, 1)", (pid, promo))
    add_line(conn, doc_base='IV668', date_iso='2026-09-01', pid=pid, qty=1, net=500,
             customer=NAME, code=CODE, unit=unit)
    conn.commit()
    rows = [p for p in call_card._assemble_products(conn, CODE, CODE, today='2026-10-02')
            if p['product_id'] == pid]
    assert len(rows) == 1, 'CONTROL: the line must reach the card'
    return rows[0]


@pytest.mark.parametrize('ratios', [(), (('กล่อง', 0.0),)], ids=['no row', 'ratio 0'])
def test_unit_with_no_usable_ratio_has_no_price(conn, ratios):
    p = _row(conn, 'กล่อง', ratios, promo=8)
    assert p['base'] is None
    assert p['ratio_missing'] is True


def test_variant_spelling_reads_its_word_row(conn):
    p = _row(conn, 'หล', [('โหล', 12.0)], promo=8)
    assert (p['base'], p['ratio_missing']) == (1200.0, False)


def test_base_unit_with_surrounding_spaces_is_the_base_unit(conn):
    p = _row(conn, ' ตัว ', [], promo=8)
    assert (p['base'], p['ratio_missing']) == (100.0, False)


@pytest.mark.parametrize('unit,want', [('โหล', (1200.0,)), ('', (100.0,)), ('ตัว', (100.0,))])
def test_converted_rows_are_unchanged(conn, unit, want):
    p = _row(conn, unit, [('โหล', 12.0)], promo=8)
    assert (p['base'], p['ratio_missing']) == want + (False,)


def test_rendered_card_says_no_ratio_instead_of_the_base_price(conn):
    _row(conn, 'กล่อง')
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess['user_id'] = 4
        sess['username'] = 'staffer'
        sess['role'] = 'staff'
    resp = client.get('/call/' + CODE)
    assert resp.status_code == 200, 'CONTROL: get_card returned None and the route redirected'
    html = resp.get_data(as_text=True)

    cell = re.search(r'<td[^>]*data-tpl="tpl-promo-1"[^>]*>(.*?)</td>', html, re.S)
    modal = re.search(r'<template id="tpl-promo-1">(.*?)</template>', html, re.S)
    assert cell and modal, 'CONTROL: the ราคาตั้ง cell and its modal rendered'
    for part in (cell.group(1), modal.group(1)):
        assert 'ไม่มีอัตราแปลง' in part
        assert '฿100.00' not in part
    # The cell opens the modal on click, so the link to fix it lives in the modal.
    assert 'href="/unit-conversions?q=' in modal.group(1)
    assert 'ราคาตั้ง (กล่อง)' in modal.group(1), 'CONTROL: this is the กล่อง row'
