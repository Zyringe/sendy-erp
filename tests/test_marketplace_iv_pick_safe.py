"""#545 — a manual IV pick is checked on the server before anything is written.

Seam: the real authed POST /marketplace/order/<id>/link-iv (and the confirm POST
it renders), observed through the response (confirm page / redirect / flash) and
the `marketplace_order_invoice` + `audit_log` rows it leaves behind.

Every test seeds its own orders and IVs on the tmp clone of the live DB and
deletes its keys first: the clone carries real rows, so no state is inherited.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

TEST_DOCS = ('IV9500001', 'IV9500002', 'IV9500003', 'HS9500004', 'SR9500005',
             'IV9500006', 'IV9500007', 'IV9500008')
TEST_ORDERS = ('PICKS1', 'PICKS2', 'PICKS3', 'PICKL1')


def _order(c, platform, order_sn, payout, order_date='2026-06-08'):
    return c.execute(
        """INSERT INTO marketplace_orders
           (platform, order_sn, status, actual_payout, settled_at, order_date, currency)
           VALUES (?,?, 'สำเร็จแล้ว', ?, ?, ?, 'THB')""",
        (platform, order_sn, payout, order_date, order_date + ' 10:00')).lastrowid


def _doc(c, doc_base, net, customer_code, date_iso='2026-06-09'):
    c.execute(
        """INSERT INTO sales_transactions
           (date_iso, doc_no, doc_base, customer, customer_code, qty, unit_price,
            vat_type, total, net, created_at, synced_to_stock)
           VALUES (?,?,?,?,?, 1, ?, 1, ?, ?, '2026-06-09 00:00:00', 1)""",
        (date_iso, f'{doc_base}-1', doc_base, 'ทดสอบ', customer_code, net, net, net))


def _link(c, platform, order_sn, doc_base, method='auto', confirmed_by=None):
    c.execute(
        """INSERT INTO marketplace_order_invoice
           (platform, order_sn, doc_base, customer_code, match_method, confidence, confirmed_by)
           VALUES (?,?,?,?,?,?,?)""",
        (platform, order_sn, doc_base, 'Zหน้าร้าน', method,
         'manual' if method == 'manual' else 'confident', confirmed_by))


@pytest.fixture
def pick(tmp_db_conn):
    """Shopee orders PICKS1..3 + Lazada PICKL1, and one Express doc per case."""
    c = tmp_db_conn
    marks = ','.join('?' * len(TEST_DOCS))
    c.execute(f"DELETE FROM sales_transactions WHERE doc_base IN ({marks})", TEST_DOCS)
    c.execute(f"DELETE FROM marketplace_order_invoice WHERE doc_base IN ({marks})", TEST_DOCS)
    c.execute(f"DELETE FROM audit_log WHERE table_name='marketplace_order_invoice' "
              f"AND row_key IN ({marks})", TEST_DOCS)
    omarks = ','.join('?' * len(TEST_ORDERS))
    c.execute(f"DELETE FROM marketplace_order_invoice WHERE order_sn IN ({omarks})", TEST_ORDERS)
    c.execute(f"DELETE FROM marketplace_orders WHERE order_sn IN ({omarks})", TEST_ORDERS)
    ids = {
        'S1': _order(c, 'shopee', 'PICKS1', 132.0),
        'S2': _order(c, 'shopee', 'PICKS2', 140.0),
        'S3': _order(c, 'shopee', 'PICKS3', 150.0),
        'L1': _order(c, 'lazada', 'PICKL1', 200.0),
    }
    _doc(c, 'IV9500001', 132.0, 'Zหน้าร้าน')   # free, Shopee's own code
    _doc(c, 'IV9500002', 140.0, 'Zหน้าร้าน')   # held by PICKS2 (auto)
    _doc(c, 'IV9500003', 200.0, 'Lหน้าร้าน')   # free, Lazada's own code
    _doc(c, 'HS9500004', 132.0, 'Zหน้าร้าน')   # a cash bill, not an invoice
    _doc(c, 'SR9500005', -50.0, 'Zหน้าร้าน')   # a credit note
    _doc(c, 'IV9500006', 132.0, 'Gหน้าร้าน')   # walk-in counter, not a marketplace
    _doc(c, 'IV9500007', 132.0, 'PICKB2B')     # a B2B customer
    _doc(c, 'IV9500008', 132.0, 'Bหน้าร้าน')   # old Shopee shop B
    _link(c, 'shopee', 'PICKS2', 'IV9500002')
    c.commit()
    return c, ids


def _client():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    cl = flask_app.test_client()
    with cl.session_transaction() as s:
        s.update(user_id=4, username='staffer', role='staff')
    return cl


def _flashes(cl):
    with cl.session_transaction() as s:
        return [m for _cat, m in s.get('_flashes', [])]


def _links(c, doc_base):
    return [dict(r) for r in c.execute(
        "SELECT platform, order_sn, match_method FROM marketplace_order_invoice WHERE doc_base=?",
        (doc_base,))]


def _order_link(c, order_sn):
    r = c.execute("SELECT doc_base FROM marketplace_order_invoice WHERE order_sn=?",
                  (order_sn,)).fetchone()
    return r['doc_base'] if r else None


def _audit(c):
    marks = ','.join('?' * len(TEST_DOCS))
    return [dict(r) for r in c.execute(
        f"""SELECT row_id, action, row_key, changed_fields, user, change_source
            FROM audit_log WHERE table_name='marketplace_order_invoice'
            AND row_key IN ({marks}) ORDER BY id""", TEST_DOCS)]


def _row(c, platform, order_sn):
    return dict(c.execute(
        """SELECT id, doc_base, match_method, confidence, customer_code, confirmed_by
           FROM marketplace_order_invoice WHERE platform=? AND order_sn=?""",
        (platform, order_sn)).fetchone())


def _press_confirm(cl, resp):
    """Submit the confirm page's form exactly as the browser would."""
    action, fields = _confirm_form(resp.get_data(as_text=True))
    fields.pop('_submit_label')
    return cl.post(action, data=fields)


def _section(html, section_id):
    """The inner HTML of one confirm-page section; fails loudly when it is absent,
    so no assertion scoped to it can pass on a page that never rendered it."""
    import re
    m = re.search(rf'<section[^>]*id="{section_id}"[^>]*>(.*?)</section>', html, re.S)
    assert m, f'section #{section_id} not rendered'
    return m.group(1)


def _confirm_form(html):
    """The confirm form's action + every <input> name→value, read with a real
    parser the way the browser will submit it."""
    from html.parser import HTMLParser

    class P(HTMLParser):
        inside = in_button = False
        action, label = None, ''

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == 'form' and a.get('id') == 'ivConfirmForm':
                self.inside, self.action = True, a.get('action')
            elif tag == 'input' and self.inside and a.get('name'):
                self.fields[a['name']] = a.get('value', '')
            elif tag == 'button' and self.inside and a.get('type') == 'submit':
                self.in_button = True

        def handle_data(self, data):
            if self.in_button:
                self.label += data

        def handle_endtag(self, tag):
            if tag == 'form':
                self.inside = False
            elif tag == 'button':
                self.in_button = False

    p = P()
    p.fields = {}
    p.feed(html)
    assert p.action, 'confirm form not rendered'
    p.fields['_submit_label'] = p.label.strip()
    return p.action, p.fields


def test_typed_number_not_in_sendy_is_refused(pick):
    c, ids = pick
    cl = _client()
    resp = cl.post(f"/marketplace/order/{ids['S1']}/link-iv", data={'doc_base_manual': 'IV9599999'})
    assert resp.status_code == 302
    assert _flashes(cl) == ['ไม่พบ IV9599999 ในระบบ ตรวจเลขอีกครั้ง (ถ้าเพิ่งคีย์ใน Express รอข้อมูลรอบถัดไป)']
    assert _order_link(c, 'PICKS1') is None


@pytest.mark.parametrize('doc, message', [
    ('HS9500004', 'HS9500004 เป็นบิลเงินสด ไม่ใช่ใบกำกับ'),
    ('SR9500005', 'SR9500005 เป็นใบลดหนี้ ไม่ใช่ใบกำกับ'),
])
def test_a_doc_that_is_not_an_iv_is_refused_naming_its_kind(pick, doc, message):
    c, ids = pick
    cl = _client()
    resp = cl.post(f"/marketplace/order/{ids['S1']}/link-iv", data={'doc_base_manual': doc})
    assert resp.status_code == 302
    assert _flashes(cl) == [message]
    assert _order_link(c, 'PICKS1') is None


@pytest.mark.parametrize('doc, code', [
    ('IV9500006', 'Gหน้าร้าน'),   # walk-in counter: its name starts หน้าร้าน but it is not online
    ('IV9500007', 'PICKB2B'),
])
def test_an_iv_billed_to_a_non_marketplace_customer_is_refused(pick, doc, code):
    c, ids = pick
    cl = _client()
    resp = cl.post(f"/marketplace/order/{ids['S1']}/link-iv", data={'doc_base_manual': doc})
    assert resp.status_code == 302
    assert _flashes(cl) == [f'{doc} เป็นบิลของ {code} ไม่ใช่บิลขายออนไลน์ ผูกกับออเดอร์ไม่ได้']
    assert _order_link(c, 'PICKS1') is None


def test_a_typed_number_is_normalised_and_previewed_before_anything_is_written(pick):
    """Rules 3 + 5: '  iv9500001-1 ' (the /sales table prints line suffixes) is the
    free Shopee IV9500001, and a typed number is always previewed, never saved."""
    c, ids = pick
    cl = _client()
    resp = cl.post(f"/marketplace/order/{ids['S1']}/link-iv",
                   data={'doc_base_manual': '  iv9500001-1 '})
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert '>IV9500001<' in _section(html, 'ivConfirmIv')
    assert 'พิมพ์เอง' in _section(html, 'ivConfirmTyped')
    _action, fields = _confirm_form(html)
    assert fields['doc_base'] == 'IV9500001'
    assert _order_link(c, 'PICKS1') is None


def test_an_iv_held_by_another_order_shows_the_confirm_page_naming_the_holder(pick):
    """Rule 5: picked from the list, but PICKS2 holds it (auto). Nothing moves yet."""
    c, ids = pick
    cl = _client()
    resp = cl.post(f"/marketplace/order/{ids['S1']}/link-iv", data={'doc_base': 'IV9500002'})
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    order = _section(html, 'ivConfirmOrder')
    for text in ('PICKS1', 'Shopee', '132.00', '8 มิ.ย. 2026'):
        assert text in order
    iv = _section(html, 'ivConfirmIv')
    for text in ('>IV9500002<', '9 มิ.ย. 2026', '140.00', '+8.00', 'Shopee'):
        assert text in iv
    holder = _section(html, 'ivConfirmHolder')
    for text in ('PICKS2', 'Shopee', 'ผูกอัตโนมัติ', 'ออเดอร์ PICKS2 จะไม่มีใบกำกับ'):
        assert text in holder
    _action, fields = _confirm_form(html)
    assert fields['expected_holders'] == 'shopee:PICKS2'
    assert fields['_submit_label'] == 'ย้ายมาออเดอร์นี้'
    assert _links(c, 'IV9500002') == [
        {'platform': 'shopee', 'order_sn': 'PICKS2', 'match_method': 'auto'}]
    assert _order_link(c, 'PICKS1') is None


def test_a_holder_on_another_platform_is_named_with_who_confirmed_it(pick):
    """Rule 5, any platform: a Lazada order holds the Shopee IV by a manual link."""
    c, ids = pick
    _link(c, 'lazada', 'PICKL1', 'IV9500001', method='manual', confirmed_by='somchai')
    c.commit()
    cl = _client()
    resp = cl.post(f"/marketplace/order/{ids['S1']}/link-iv", data={'doc_base': 'IV9500001'})
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    holder = _section(html, 'ivConfirmHolder')
    for text in ('PICKL1', 'Lazada', 'ยืนยันเอง', 'somchai', 'ออเดอร์ PICKL1 จะไม่มีใบกำกับ'):
        assert text in holder
    _action, fields = _confirm_form(html)
    assert fields['expected_holders'] == 'lazada:PICKL1'
    assert _order_link(c, 'PICKS1') is None


def test_a_free_iv_under_another_channels_code_asks_first(pick):
    """Rule 5: Bหน้าร้าน is a marketplace code, but not Shopee's own (Z)."""
    c, ids = pick
    cl = _client()
    resp = cl.post(f"/marketplace/order/{ids['S1']}/link-iv", data={'doc_base': 'IV9500008'})
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'Shopee ร้าน B (ปิดแล้ว)' in _section(html, 'ivConfirmChannel')
    assert 'id="ivConfirmHolder"' not in html
    _action, fields = _confirm_form(html)
    assert fields['expected_holders'] == ''
    assert fields['_submit_label'] == 'ผูกกับออเดอร์นี้'
    assert _order_link(c, 'PICKS1') is None

    # Confirmed: the link records the code the IV is really billed to.
    assert _press_confirm(cl, resp).status_code == 302
    row = _row(c, 'shopee', 'PICKS1')
    assert (row['doc_base'], row['customer_code']) == ('IV9500008', 'Bหน้าร้าน')


def test_a_free_own_channel_pick_from_the_list_saves_straight_away(pick):
    """Rule 6, as before #545: no confirm page, and nothing moved so no audit row."""
    c, ids = pick
    cl = _client()
    resp = cl.post(f"/marketplace/order/{ids['S1']}/link-iv", data={'doc_base': 'IV9500001'})
    assert resp.status_code == 302
    row = _row(c, 'shopee', 'PICKS1')
    assert (row['doc_base'], row['match_method'], row['customer_code'], row['confirmed_by']) == \
        ('IV9500001', 'manual', 'Zหน้าร้าน', 'staffer')
    assert _audit(c) == []


def test_confirming_a_move_takes_the_iv_and_writes_one_audit_row(pick):
    """Rule 8: PICKS2's auto link is deleted, PICKS1 holds the IV, one audit row."""
    import json
    c, ids = pick
    loser_id = _row(c, 'shopee', 'PICKS2')['id']
    cl = _client()
    resp = cl.post(f"/marketplace/order/{ids['S1']}/link-iv", data={'doc_base': 'IV9500002'})
    assert resp.status_code == 200 and _audit(c) == []        # the page alone writes nothing
    assert _press_confirm(cl, resp).status_code == 302
    assert _links(c, 'IV9500002') == [
        {'platform': 'shopee', 'order_sn': 'PICKS1', 'match_method': 'manual'}]
    assert _order_link(c, 'PICKS2') is None
    audit = _audit(c)
    assert len(audit) == 1
    a = audit[0]
    assert (a['row_id'], a['action'], a['row_key'], a['user'], a['change_source']) == \
        (loser_id, 'DELETE', 'IV9500002', 'staffer', 'iv_picker_move')
    assert json.loads(a['changed_fields']) == {
        'order_sn': ['PICKS2', 'PICKS1'], 'platform': ['shopee', 'shopee'],
        'match_method': ['auto', 'manual'], 'confidence': ['confident', 'manual']}
    assert any('ย้าย IV9500002' in m and 'PICKS2' in m for m in _flashes(cl))


def test_a_shopee_iv_moved_to_a_lazada_order_leaves_exactly_one_holder(pick):
    """Acceptance: a Zหน้าร้าน IV held by a Shopee order, picked for a Lazada order."""
    c, ids = pick
    _link(c, 'shopee', 'PICKS3', 'IV9500001')
    c.commit()
    cl = _client()
    resp = cl.post(f"/marketplace/order/{ids['L1']}/link-iv",
                   data={'doc_base_manual': 'IV9500001'})
    assert resp.status_code == 200
    holder = _section(resp.get_data(as_text=True), 'ivConfirmHolder')
    assert 'PICKS3' in holder and 'Shopee' in holder
    assert _press_confirm(cl, resp).status_code == 302
    assert _links(c, 'IV9500001') == [
        {'platform': 'lazada', 'order_sn': 'PICKL1', 'match_method': 'manual'}]
    assert _row(c, 'lazada', 'PICKL1')['customer_code'] == 'Zหน้าร้าน'
    assert len(_audit(c)) == 1


def test_confirm_is_refused_when_a_different_order_took_the_iv_meanwhile(pick):
    """Rule 7: the page named PICKS2; by the time it is pressed PICKS3 holds it."""
    c, ids = pick
    cl = _client()
    resp = cl.post(f"/marketplace/order/{ids['S1']}/link-iv", data={'doc_base': 'IV9500002'})
    assert resp.status_code == 200
    c.execute("UPDATE marketplace_order_invoice SET order_sn='PICKS3' WHERE doc_base='IV9500002'")
    c.commit()
    assert _press_confirm(cl, resp).status_code == 302
    assert _flashes(cl) == ['มีการเปลี่ยนแปลง (IV9500002 ถูกผูกกับออเดอร์อื่นแล้ว) กรุณาเลือกใหม่']
    assert _links(c, 'IV9500002') == [
        {'platform': 'shopee', 'order_sn': 'PICKS3', 'match_method': 'auto'}]
    assert _order_link(c, 'PICKS1') is None
    assert _audit(c) == []


DEPOSITS = '/marketplace/settlement?platform=shopee&tab=deposits&year=2025'
REVIEW = '/marketplace/review?platform=lazada'


def _location(resp):
    from urllib.parse import urlsplit
    p = urlsplit(resp.headers['Location'])
    return p.path + ('?' + p.query if p.query else '')


def test_a_save_returns_to_the_page_the_picker_was_opened_from(pick):
    """Rule 9: settlement with its tab + year, not the bare settlement page."""
    _c, ids = pick
    resp = _client().post(f"/marketplace/order/{ids['S1']}/link-iv",
                          data={'doc_base': 'IV9500001', 'next': DEPOSITS})
    assert resp.status_code == 302 and _location(resp) == DEPOSITS


def test_a_refusal_returns_to_the_review_page_it_came_from(pick):
    _c, ids = pick
    resp = _client().post(f"/marketplace/order/{ids['S1']}/link-iv",
                          data={'doc_base_manual': 'IV9599999', 'next': REVIEW})
    assert resp.status_code == 302 and _location(resp) == REVIEW


def test_the_confirm_page_cancels_and_confirms_back_to_where_it_came_from(pick):
    import re
    c, ids = pick
    cl = _client()
    resp = cl.post(f"/marketplace/order/{ids['S1']}/link-iv",
                   data={'doc_base': 'IV9500002', 'next': DEPOSITS})
    html = resp.get_data(as_text=True)
    cancel = re.search(r'<a [^>]*id="ivConfirmCancel"[^>]*>', html)
    assert cancel, 'ยกเลิก link not rendered'
    assert 'href="/marketplace/settlement?platform=shopee&amp;tab=deposits&amp;year=2025"' in cancel.group(0)
    done = _press_confirm(cl, resp)
    assert done.status_code == 302 and _location(done) == DEPOSITS
    assert _order_link(c, 'PICKS1') == 'IV9500002'


@pytest.mark.parametrize('bad', ['https://evil.example/marketplace/settlement',
                                 '//evil.example/marketplace/settlement', '/admin/users'])
def test_a_next_that_is_not_a_picker_page_falls_back_to_settlement(pick, bad):
    _c, ids = pick
    resp = _client().post(f"/marketplace/order/{ids['S1']}/link-iv?platform=shopee",
                          data={'doc_base': 'IV9500001', 'next': bad})
    assert resp.status_code == 302
    assert _location(resp) == '/marketplace/settlement?platform=shopee'


def test_confirm_saves_when_the_holder_let_go_meanwhile(pick):
    """Rule 7: the page named PICKS2; by the time it is pressed nobody holds it."""
    c, ids = pick
    cl = _client()
    resp = cl.post(f"/marketplace/order/{ids['S1']}/link-iv", data={'doc_base': 'IV9500002'})
    assert resp.status_code == 200
    c.execute("DELETE FROM marketplace_order_invoice WHERE doc_base='IV9500002'")
    c.commit()
    assert _press_confirm(cl, resp).status_code == 302
    assert _links(c, 'IV9500002') == [
        {'platform': 'shopee', 'order_sn': 'PICKS1', 'match_method': 'manual'}]
    assert _audit(c) == []


def test_a_clicked_row_and_a_different_typed_number_are_refused(pick):
    """Rule 3: the picker used to let the typed box silently win over a clicked row.
    Both valid IVs here, so only the disagreement can be the reason."""
    c, ids = pick
    cl = _client()
    resp = cl.post(f"/marketplace/order/{ids['S1']}/link-iv",
                   data={'doc_base': 'IV9500001', 'doc_base_manual': 'IV9500008'})
    assert resp.status_code == 302
    assert _flashes(cl) == ['เลือกใบในรายการ (IV9500001) แต่พิมพ์เลข IV9500008 '
                            'ไม่ตรงกัน กรุณาเลือกอย่างใดอย่างหนึ่ง']
    assert _order_link(c, 'PICKS1') is None
