"""#708: the desktop customer page and the mobile customer card show the
chaseable NET, the same total /ar and the dunning page show.

The bill list still lists positive bills only (ADR 0012). Chaseable credit rows
appear as one หักเครดิต line, so the listed bills plus that line equal the total.
Fixture: `test_ar_excluded_docs._seed`, whose CREDIT_CODE customer has a bill
of 1,200.00, a chaseable credit of -250.00 and an excluded RE row of 400.00.
"""
import re
import sqlite3

import pytest

import ar_statement
from tests.test_ar_excluded_docs import (
    CLEAN_CODE, CREDIT_CODE, CREDIT_NAME, _manager_client, _seed, _seed_master)


def _seed_credit_master(db_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM customers WHERE code = ? OR name = ?", (CREDIT_CODE, CREDIT_NAME))
        conn.execute("INSERT INTO customers (code, name) VALUES (?, ?)", (CREDIT_CODE, CREDIT_NAME))
        conn.commit()
    finally:
        conn.close()


def _card(html, start_marker, end_marker):
    assert start_marker in html, 'the unpaid card did not render'
    return html.split(start_marker, 1)[1].split(end_marker, 1)[0]


def _amounts(fragment):
    return [float(x.replace(',', '')) for x in re.findall(r'-?[\d,]+\.\d\d', fragment)]


def test_statement_splits_chaseable_into_bills_and_credits(tmp_db):
    _seed(tmp_db)
    st = ar_statement.customer_statement(CREDIT_CODE, db_path=tmp_db)
    assert [r['doc_no'] for r in st['bills']] == ['ZZEX-CR-BILL']
    assert [r['doc_no'] for r in st['credits']] == ['ZZEX-CR-CREDIT']
    assert st['total'] == pytest.approx(950.00)
    assert sum(r['outstanding'] for r in st['bills'] + st['credits']) \
        == pytest.approx(st['total'])


def test_desktop_customer_page_shows_the_net_and_a_credit_line(tmp_db):
    _seed(tmp_db)
    _seed_credit_master(tmp_db)
    html = _manager_client().get(f'/customer/code/{CREDIT_CODE}').get_data(as_text=True)
    card = _card(html, 'บิลค้างชำระ', '</table>')

    assert 'ZZEX-CR-BILL' in card
    assert 'ZZEX-CR-CREDIT' not in card, 'a credit is not a bill row'
    header = card.split('<table', 1)[0]
    assert _amounts(header) == [950.00], header
    credit = re.search(r'<tr data-row="credit">(.*?)</tr>', card, re.S)
    assert credit, 'no หักเครดิต line'
    assert 'หักเครดิต' in credit.group(1)
    assert _amounts(credit.group(1)) == [-250.00]
    foot = card.split('<tfoot', 1)[1]
    assert _amounts(foot) == [950.00]


def test_mobile_customer_card_shows_the_net_and_a_credit_line(tmp_db):
    _seed(tmp_db)
    _seed_credit_master(tmp_db)
    html = _manager_client().get(f'/m/customer/code/{CREDIT_CODE}').get_data(as_text=True)
    # The excluded-docs note follows the card; CREDIT_CODE has one (its RE row).
    card = _card(html, 'บิลค้างชำระ (', 'ยอดค้างด้านบนคือยอด')

    assert 'ZZEX-CR-BILL' in card
    assert 'ZZEX-CR-CREDIT' not in card
    assert 'จาก ฿950' in card, card[:300]
    credit = re.search(r'data-row="credit"(.*?)</div>\s*</div>', card, re.S)
    assert credit, 'no หักเครดิต line'
    assert 'หักเครดิต' in credit.group(1)
    assert _amounts(credit.group(1)) == [-250.00]


@pytest.mark.parametrize('url', ['/customer/code/{}', '/m/customer/code/{}'])
def test_customer_without_credits_has_no_credit_line(tmp_db, url):
    _seed(tmp_db)
    _seed_master(tmp_db)
    html = _manager_client().get(url.format(CLEAN_CODE)).get_data(as_text=True)
    assert 'ZZEX-ONLYCLEAN' in html, 'control: the clean customer rendered its bill'
    assert 'data-row="credit"' not in html
    assert 'หักเครดิต' not in html


def test_every_snapshot_code_total_is_the_independent_net(tmp_db):
    """Both pages render `customer_statement()['total']`; pin that number per
    code against SQL written here from the predicate's three clauses."""
    _seed(tmp_db)
    conn = sqlite3.connect(tmp_db)
    oracle = dict(conn.execute("""
        SELECT TRIM(customer_code), ROUND(SUM(outstanding_amount), 2)
          FROM express_ar_outstanding
         WHERE entity = 'BSN'
           AND snapshot_date_iso = (SELECT MAX(snapshot_date_iso)
                                      FROM express_ar_outstanding WHERE entity = 'BSN')
           AND is_anomalous = 0 AND doc_date_iso >= '2024-01-01'
           AND doc_no NOT IN (SELECT doc_no FROM ar_writeoffs)
           AND TRIM(customer_code) <> ''
         GROUP BY TRIM(customer_code)""").fetchall())
    conn.close()
    assert len(oracle) > 5 and CREDIT_CODE in oracle
    for code, net in oracle.items():
        st = ar_statement.customer_statement(code, db_path=tmp_db)
        assert round(st['total'], 2) == net, code
