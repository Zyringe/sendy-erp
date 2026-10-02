"""Confirmed #711 flow, through HTTP and the transactional conversion boundary."""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest
from lxml import html
import models


@pytest.fixture
def desk(empty_db_conn):
    from app import app
    c = empty_db_conn
    c.executemany("INSERT INTO products(id,product_name,unit_type) VALUES (?,?,?)",
                  [(608, 'ฆ้อนยางเล็ก+ด้ามไม้', 'อัน'),
                   (609, 'ฆ้อนยางด้ามไม้เล็ก (แพ็ค)', 'อัน'),
                   (610, 'อีกแหล่ง', 'อัน')])
    c.executemany("INSERT INTO stock_levels(product_id,quantity) VALUES (?,?)",
                  [(608, 2), (609, -5), (610, 3)])
    c.commit()
    app.config['TESTING'] = True
    client = app.test_client()
    with client.session_transaction() as s:
        s.update(user_id=1, username='test-admin', role='admin')
    return client, c


def formula(c, source=608, qty=6):
    fid = c.execute("INSERT INTO conversion_formulas(name,output_product_id,output_qty)"
                    " VALUES ('test conversion',609,?)", (qty,)).lastrowid
    c.execute("INSERT INTO conversion_formula_inputs(formula_id,product_id,quantity)"
              " VALUES (?,?,1)", (fid, source))
    c.commit()
    return fid


def page(response):
    assert response.status_code == 200
    return html.fromstring(response.data)


def balances(c):
    return list(map(tuple, c.execute('SELECT product_id,quantity FROM stock_levels ORDER BY product_id')))


def test_single_formula_prefills_smallest_whole_run_without_writes(desk):
    client, c = desk
    fid = formula(c)
    before = balances(c)
    response = client.get('/alerts/609/conversion')
    assert response.status_code == 302
    assert response.location == f'/conversions/{fid}/run?alert_product_id=609'
    doc = page(client.get(response.location))
    assert doc.xpath('string(//input[@name="multiplier"]/@value)') == '1'
    assert balances(c) == before
    c.execute('UPDATE stock_levels SET quantity=-7 WHERE product_id=609')
    c.commit()
    doc = page(client.get(response.location))
    assert doc.xpath('string(//input[@name="multiplier"]/@value)') == '2'


def test_multiple_formulas_choose_sources_even_when_stock_is_zero(desk):
    client, c = desk
    first, second = formula(c), formula(c, 610)
    c.execute('UPDATE stock_levels SET quantity=0 WHERE product_id=608')
    c.commit()
    doc = page(client.get('/alerts/609/conversion'))
    links = doc.xpath('//a[contains(@href,"/run?")]/@href')
    assert len(links) == 2
    assert {f'/conversions/{f}/run?alert_product_id=609' for f in (first, second)} == set(links)
    assert 'มี 0 อัน' in doc.xpath('string(//a[contains(@href,"/conversions/%s/run")])' % first)


def test_missing_formula_saves_reciprocal_one_to_one_without_stock(desk):
    client, c = desk
    c.execute('UPDATE stock_levels SET quantity=0 WHERE product_id=608')
    c.commit()
    before = balances(c)
    response = client.get('/alerts/609/conversion')
    assert response.location == '/conversions/pair?alert_product_id=609'
    doc = page(client.get(response.location))
    assert doc.xpath('string(//input[@name="alert_product_id"]/@value)') == '609'
    response = client.post(response.location, data={'alert_product_id': '609', 'source_id': '608',
                           'ratio': '1', 'alert_direction': 'unpack'})
    assert response.status_code == 302
    rows = c.execute('SELECT id,output_product_id,output_qty FROM conversion_formulas ORDER BY id').fetchall()
    assert len(rows) == 2
    assert {(r['output_product_id'], r['output_qty']) for r in rows} == {(608, 1), (609, 1)}
    target = next(r['id'] for r in rows if r['output_product_id'] == 609)
    assert response.location == f'/conversions/{target}/run?alert_product_id=609'
    assert balances(c) == before
    assert c.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 0


def run_data(doc, multiplier):
    return {'multiplier': str(multiplier), 'alert_product_id': '609',
            'run_token': doc.xpath('string(//input[@name="run_token"]/@value)'),
            'recipe': doc.xpath('string(//input[@name="recipe"]/@value)')}


def test_shortage_partial_run_and_replay_leave_remaining_alert(desk):
    client, c = desk
    fid = formula(c, qty=1)
    url = f'/conversions/{fid}/run?alert_product_id=609'
    doc = page(client.get(url))
    assert doc.xpath('string(//input[@name="multiplier"]/@value)') == '5'
    before = balances(c)
    response = client.post(url, data=run_data(doc, 5))
    assert response.status_code == 200
    assert balances(c) == before
    data = run_data(page(response), 2)
    response = client.post(url, data=data)
    assert response.location == '/alerts'
    assert balances(c) == [(608, 0), (609, -3), (610, 3)]
    assert len(page(client.get('/alerts')).xpath('//a[@href="/alerts/609/conversion"]')) == 1
    # One confirmation, even when the browser repeats its POST.
    client.post(url, data=data)
    assert balances(c) == [(608, 0), (609, -3), (610, 3)]
    assert c.execute('SELECT COUNT(*) FROM conversion_cost_log').fetchone()[0] == 1


def test_changed_recipe_requires_new_confirmation(desk):
    client, c = desk
    fid = formula(c)
    url = f'/conversions/{fid}/run?alert_product_id=609'
    doc = page(client.get(url))
    c.execute('UPDATE conversion_formulas SET output_qty=12 WHERE id=?', (fid,))
    c.commit()
    before = balances(c)
    response = client.post(url, data=run_data(doc, 1))
    assert response.status_code == 200
    assert balances(c) == before
    assert c.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 0
    assert 'สูตรเปลี่ยน' in response.data.decode()


def test_deactivated_between_route_read_and_transaction_is_refused(desk, monkeypatch):
    client, c = desk
    fid = formula(c)
    url = f'/conversions/{fid}/run?alert_product_id=609'
    doc = page(client.get(url))
    original = models.run_conversion
    def deactivate(*args, **kwargs):
        c.execute('UPDATE conversion_formulas SET is_active=0 WHERE id=?', (fid,))
        c.commit()
        return original(*args, **kwargs)
    monkeypatch.setattr(models, 'run_conversion', deactivate)
    before = balances(c)
    client.post(url, data=run_data(doc, 1))
    assert balances(c) == before
    assert c.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 0


@pytest.mark.parametrize('direction,expected', [('unpack', (1, 6)), ('pack', (6, 1))])
def test_alert_pair_ratio_orientation_and_reciprocal_enforced(desk, direction, expected):
    client, c = desk
    response = client.post('/conversions/pair?alert_product_id=609', data={
        'source_id': '608', 'ratio': '6', 'alert_direction': direction,
        'direction': 'pack', 'packaging_id': '610'})
    assert response.status_code == 302
    rows = models.get_producing_formulas(609)
    assert len(rows) == 1
    assert (rows[0]['inputs'][0]['quantity'], rows[0]['output_qty']) == expected
    assert c.execute('SELECT COUNT(*) FROM conversion_formulas WHERE is_active=1').fetchone()[0] == 2
    assert c.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 0


def test_cleared_deficit_disappears_and_cannot_restart_from_alert(desk):
    client, c = desk
    fid = formula(c)
    url = f'/conversions/{fid}/run?alert_product_id=609'
    doc = page(client.get(url))
    assert client.post(url, data=run_data(doc, 1)).location == '/alerts'
    assert balances(c) == [(608, 1), (609, 1), (610, 3)]
    assert len(page(client.get('/alerts')).xpath('//a[@href="/alerts/609/conversion"]')) == 0
    assert client.get('/alerts/609/conversion').location == '/alerts'


@pytest.mark.parametrize('role,allowed', [('admin', True), ('manager', True), ('staff', True),
                                        ('shareholder', False), ('general', False)])
def test_existing_write_roles_preserved(desk, role, allowed):
    client, c = desk
    fid = formula(c)
    with client.session_transaction() as s:
        s['role'] = role
    url = f'/conversions/{fid}/run?alert_product_id=609'
    if role == 'general':
        assert client.get('/alerts/609/conversion').status_code == 302
        doc = None
    else:
        doc = page(client.get(url))
    before = balances(c)
    response = client.post(url, data=run_data(doc, 1) if doc is not None else {})
    if allowed:
        assert response.location == '/alerts'
        assert balances(c) == [(608, 1), (609, 1), (610, 3)]
    else:
        assert response.status_code == 302
        assert balances(c) == before
        assert c.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 0


def test_vat_book_and_mismatched_output_do_not_write(desk):
    client, c = desk
    fid = formula(c)
    assert client.get(f'/conversions/{fid}/run?alert_product_id=608').status_code == 400
    with client.session_transaction() as s:
        s['active_book'] = 'vat'
    before = balances(c)
    response = client.post(f'/conversions/{fid}/run?alert_product_id=609',
                           data={'multiplier': 1, 'run_token': 'vat'})
    assert response.status_code == 302
    assert response.location == "/sales"
    with client.session_transaction() as session:
        assert any("สมุด VAT อ่านอย่างเดียว" in msg for _, msg in session["_flashes"])
    assert balances(c) == before


def test_invalid_output_quantity_refuses_stale_preview(desk):
    client, c = desk
    fid = formula(c)
    url = f'/conversions/{fid}/run?alert_product_id=609'
    doc = page(client.get(url))
    c.execute('UPDATE conversion_formulas SET output_qty=0 WHERE id=?', (fid,))
    c.commit()
    before = balances(c)
    response = client.post(url, data=run_data(doc, 1))
    assert response.status_code == 302
    assert response.location == '/alerts'
    assert balances(c) == before


def test_alert_builder_cannot_remove_existing_packaging(desk):
    client, c = desk
    models.upsert_pack_unpack_pair(608, 609, 1, packaging_id=610)
    before = list(map(tuple, c.execute('SELECT * FROM conversion_formula_inputs')))
    response = client.post('/conversions/pair?alert_product_id=609', data={
        'source_id': '608', 'ratio': '1', 'alert_direction': 'unpack'})
    assert response.status_code == 200
    assert list(map(tuple, c.execute('SELECT * FROM conversion_formula_inputs'))) == before
    assert c.execute('SELECT COUNT(*) FROM conversion_formulas WHERE is_active=1').fetchone()[0] == 1
