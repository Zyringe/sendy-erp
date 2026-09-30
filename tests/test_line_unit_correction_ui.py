"""แก้หน่วยบรรทัด (#692) PR-2: the page, its three POSTs and the two entry points.

The line under test is IV6900001-1, 2 โหล of a product whose base unit is
หลอด (โหล = 12): the ledger holds -24, stock is 176, and correcting the line
to หลอด makes the ledger row -2.

Every page assertion is scoped to an element (`_Page`) and every number is
parsed before it is compared. A redirect is never taken as evidence of a
write or of a refusal: the DB is read each time.
"""
import datetime
import fcntl
import os
import sqlite3
from collections import defaultdict
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest

from tests import unit_correction_scenario as sc

DOC = 'IV6900001'
LINE = 'IV6900001-1'
KEY = {'doc_no': LINE, 'bsn_code': sc.CODE}
PAGE = '/sales/unit-correction'
CANCEL_REASON = 'ยกเลิกเพราะแก้ผิดบรรทัด'
IMPORT_BUSY = 'กำลังนำเข้าข้อมูลจาก Express อยู่ ลองใหม่อีกครั้งในอีกสักครู่'

_VOID = {'input', 'br', 'hr', 'img', 'meta', 'link'}


class _Page(HTMLParser):
    """The rendered page, read per element id:
    `text[id]`   the element's text,
    `tags[id]`   every (tag, attrs) inside it,
    `values[id]` {data-uc name: [text of each element carrying it]},
    `links[id]`  [(href, title, text)] of the anchors inside it.
    """

    def __init__(self, html):
        super().__init__()
        self._open = []
        self.text = defaultdict(str)
        self.tags = defaultdict(list)
        self.values = defaultdict(lambda: defaultdict(list))
        self.links = defaultdict(list)
        self.feed(html)

    def _scopes(self, upto=None):
        return [e['id'] for e in self._open[:upto] if e['id']]

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for scope in self._scopes():
            self.tags[scope].append((tag, attrs))
        if tag not in _VOID:
            self._open.append({'tag': tag, 'id': attrs.get('id'),
                               'uc': attrs.get('data-uc'), 'attrs': attrs, 'buf': ''})

    def handle_endtag(self, tag):
        for i in range(len(self._open) - 1, -1, -1):
            if self._open[i]['tag'] == tag:
                for depth in range(len(self._open) - 1, i - 1, -1):
                    self._close(self._open[depth], depth)
                del self._open[i:]
                return

    def _close(self, e, depth):
        text = ' '.join(e['buf'].split())
        for scope in self._scopes(depth):
            if e['uc']:
                self.values[scope][e['uc']].append(text)
            if e['tag'] == 'a':
                self.links[scope].append(
                    (e['attrs'].get('href', ''), e['attrs'].get('title', ''), text))

    def handle_data(self, data):
        for e in self._open:
            e['buf'] += data
        for scope in self._scopes():
            self.text[scope] += data


def _num(text):
    return float(text.replace(',', ''))


def _one(values, name):
    found = values[name]
    assert len(found) == 1, (name, found)
    return found[0]


def _client(role, username='put-admin'):
    from app import app
    app.config['TESTING'] = True
    c = app.test_client()
    if role:
        with c.session_transaction() as s:
            s['user_id'], s['username'], s['role'] = 1, username, role
    return c


def _flashes(c):
    with c.session_transaction() as s:
        return [tuple(f) for f in s.get('_flashes', [])]


def _location(resp):
    parts = urlsplit(resp.headers['Location'])
    return parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}


def _luc(path, fn, *args):
    import line_unit_correction as luc
    c = sc.conn(path)
    try:
        return getattr(luc, fn)(c, *args)
    finally:
        c.close()


def _everything(path):
    """What a refused request must leave alone, audit trail included."""
    return sc.written_state(path), sc.audit_count(path)


@pytest.fixture
def line(empty_db, monkeypatch):
    sc.seed_company(empty_db)
    pid = sc.seed_product(empty_db)
    sc.run_zip(monkeypatch, sc.standard_book())
    assert sc.sale_ledger(empty_db, LINE, pid)[0][1] == -24
    assert sc.stock(empty_db, pid) == 176
    return empty_db, pid


def _count_stock(path, pid, qty=5, note='นับสต๊อก', at='2026-04-15 10:00:00'):
    c = sc.conn(path)
    c.execute("INSERT INTO transactions (product_id, txn_type, quantity_change,"
              " unit_mode, note, created_at) VALUES (?, 'ADJUST', ?, 'unit', ?, ?)",
              (pid, qty, note, at))
    c.commit()
    c.close()


APPLY = dict(KEY, corrected_unit='หลอด', stock_mode='hold', reason=sc.REASON)


# ── the page ─────────────────────────────────────────────────────────────────

def test_the_page_shows_an_admin_the_line_and_the_units_it_may_become(line):
    resp = _client('admin').get(PAGE, query_string=KEY)

    assert resp.status_code == 200
    page = _Page(resp.get_data(as_text=True))
    card = page.text['uc-line']
    for shown in (LINE, '2026-04-01', 'ลูกค้าทดสอบ', 'ใบมีดคัตเตอร์'):
        assert shown in card, shown
    values = page.values['uc-line']
    assert _num(_one(values, 'qty')) == 2
    assert _one(values, 'unit') == 'โหล'
    assert _num(_one(values, 'unit-price')) == 49
    assert _num(_one(values, 'net')) == 98
    back = [urlsplit(href).path for href, _t, _x in page.links['uc-line']]
    assert '/sales/doc/' + DOC in back
    options = [a.get('value') for tag, a in page.tags['uc-correct'] if tag == 'option']
    assert options == ['หลอด']
    # No preview asked for yet, no correction made: neither panel is there.
    assert 'uc-confirm' not in page.text
    assert 'uc-active' not in page.text


@pytest.mark.parametrize('role', ['manager', 'staff', 'shareholder'])
def test_the_page_is_forbidden_to_everyone_but_the_admin(line, role):
    assert _client('admin').get(PAGE, query_string=KEY).status_code == 200
    assert _client(role).get(PAGE, query_string=KEY).status_code == 403


def test_the_page_sends_a_logged_out_visitor_to_login(line):
    resp = _client(None).get(PAGE, query_string=KEY)
    assert resp.status_code == 302
    assert _location(resp)[0] == '/login'


def test_the_page_of_a_line_that_does_not_exist_is_a_404(line):
    admin = _client('admin')
    assert admin.get(PAGE, query_string=KEY).status_code == 200
    assert admin.get(PAGE, query_string=dict(KEY, doc_no='IV6900001-9')).status_code == 404
    assert admin.get(PAGE).status_code == 404


def test_the_page_of_a_corrected_line_shows_the_correction_and_only_the_cancel_form(line):
    path, _pid = line
    cid = _luc(path, 'apply', LINE, sc.CODE, 'หลอด', 'hold', sc.REASON, 'put')

    page = _Page(_client('admin').get(PAGE, query_string=KEY).get_data(as_text=True))

    badge = page.text['uc-active']
    for shown in ('โหล', 'หลอด', 'put', sc.REASON, 'คงยอดคงเหลือ'):
        assert shown in badge, shown
    hidden = {a.get('name'): a.get('value') for tag, a in page.tags['uc-active']
              if tag == 'input' and a.get('type') == 'hidden'}
    assert hidden['correction_id'] == str(cid)
    assert 'csrf_token' in hidden
    assert 'ยกเลิกการแก้หน่วย' in badge
    assert 'uc-correct' not in page.text
    assert 'uc-confirm' not in page.text


def test_the_page_lists_the_lines_ended_corrections_newest_first(line):
    path, _pid = line
    first = _luc(path, 'apply', LINE, sc.CODE, 'หลอด', 'hold', sc.REASON, 'put')
    _luc(path, 'cancel', first, CANCEL_REASON, 'put')
    second = _luc(path, 'apply', LINE, sc.CODE, 'หลอด', 'move', 'รอบสองแก้เป็นหลอดอีกครั้ง', 'put')
    _luc(path, 'cancel', second, 'ยกเลิกรอบสองอีกครั้งหนึ่ง', 'put')

    page = _Page(_client('admin').get(PAGE, query_string=KEY).get_data(as_text=True))

    rows = page.values['uc-history']['history-row']
    assert len(rows) == 2
    assert 'ยกเลิกรอบสองอีกครั้งหนึ่ง' in rows[0] and CANCEL_REASON in rows[1]
    # Both are ended, so the line can be corrected again.
    assert 'uc-correct' in page.text


# ── preview ──────────────────────────────────────────────────────────────────

def _preview(client, **over):
    return client.post(PAGE + '/preview', data=dict(dict(KEY, corrected_unit='หลอด'), **over))


def _radios(page):
    return [(a.get('value'), 'checked' in a) for tag, a in page.tags['uc-confirm']
            if tag == 'input' and a.get('type') == 'radio']


def test_preview_shows_both_stock_outcomes_and_proposes_hold_after_a_stock_adjust(line):
    path, pid = line
    _count_stock(path, pid)
    before = _everything(path)

    resp = _preview(_client('admin'))

    assert resp.status_code == 200
    page = _Page(resp.get_data(as_text=True))
    values = page.values['uc-confirm']
    assert _num(_one(values, 'old-effect')) == -24
    assert _num(_one(values, 'new-effect')) == -2
    assert [_num(v) for v in values['stock-now']] == [181, 181]
    assert _num(_one(values, 'stock-after-hold')) == 181
    assert _num(_one(values, 'stock-after-move')) == 203
    assert _num(_one(values, 'hold-offset')) == -22
    assert _radios(page) == [('hold', True), ('move', False)]
    assert '(ระบบแนะนำ)' in _one(values, 'mode-hold')
    assert '(ระบบแนะนำ)' not in _one(values, 'mode-move')
    panel = page.text['uc-confirm']
    assert 'พบรายการปรับสต็อกหลังวันขาย: "นับสต๊อก" เมื่อ 2026-04-15 10:00:00' in ' '.join(panel.split())
    assert 'ระบบไม่รู้ว่ารายการนี้เป็นการนับสต็อกจริงหรือไม่ โปรดเลือกเอง' in panel
    assert 'ไม่พบรายการปรับสต็อกหลังวันขาย' not in panel
    hidden = {a.get('name'): a.get('value') for tag, a in page.tags['uc-confirm']
              if tag == 'input' and a.get('type') == 'hidden'}
    assert (hidden['doc_no'], hidden['bsn_code'], hidden['corrected_unit']) == \
        (LINE, sc.CODE, 'หลอด')
    assert 'csrf_token' in hidden
    assert _everything(path) == before


def test_preview_proposes_move_when_no_adjust_follows_the_sale(line):
    page = _Page(_preview(_client('admin')).get_data(as_text=True))

    values = page.values['uc-confirm']
    assert [_num(v) for v in values['stock-now']] == [176, 176]
    assert _num(_one(values, 'stock-after-hold')) == 176
    assert _num(_one(values, 'stock-after-move')) == 198
    assert _radios(page) == [('hold', False), ('move', True)]
    assert '(ระบบแนะนำ)' in _one(values, 'mode-move')
    assert '(ระบบแนะนำ)' not in _one(values, 'mode-hold')
    panel = page.text['uc-confirm']
    assert 'ไม่พบรายการปรับสต็อกหลังวันขาย' in panel
    assert 'ระบบไม่รู้ว่ารายการนี้' not in panel
    # Nothing about cost moves for this line, so neither warning is shown.
    assert 'ต้นทุนเฉลี่ย' not in panel


def test_preview_warns_that_move_recosts_and_prints_only_the_count_that_changed(
        empty_db, monkeypatch):
    """10 bought, 24 sold, 100 bought: the second lot is costed at stock -14
    today and at +8 once the line is 2 หลอด."""
    sc.seed_company(empty_db)
    sc.seed_product(empty_db)
    sc.run_zip(monkeypatch, sc.Book()
               .purchase('RR6900001', [(1, sc.CODE, 10.0, 'หลอด', 10.0)],
                         datetime.date(2026, 3, 10))
               .sale(DOC, [(1, sc.CODE, 2.0, 'โหล', 49.0)])
               .purchase('RR6900002', [(1, sc.CODE, 100.0, 'หลอด', 20.0)],
                         datetime.date(2026, 5, 1)))

    page = _Page(_preview(_client('admin')).get_data(as_text=True))

    panel = ' '.join(page.text['uc-confirm'].split())
    assert 'ถ้าเลือกขยับยอดคงเหลือ ต้นทุนเฉลี่ยย้อนหลังของสินค้านี้จะถูกคำนวณใหม่' in panel
    values = page.values['uc-confirm']
    assert [_num(v) for v in values['below-zero']] == [1, 0]
    assert values['at-zero'] == []
    assert 'ใบลดหนี้' not in panel


def test_preview_warns_when_hold_on_a_return_still_moves_cost(empty_db, monkeypatch):
    day = datetime.date(2026, 5, 1)
    sc.seed_company(empty_db)
    sc.seed_product(empty_db)
    sc.run_zip(monkeypatch, sc.Book()
               .purchase('RR6900001', [(1, sc.CODE, 100.0, 'หลอด', 10.0)],
                         datetime.date(2026, 3, 10))
               .sale('SR6900001', [(1, sc.CODE, 2.0, 'โหล', 49.0)], day)
               .purchase('RR6900002', [(1, sc.CODE, 100.0, 'หลอด', 20.0)], day))

    page = _Page(_preview(_client('admin'), doc_no='SR6900001-1').get_data(as_text=True))

    panel = ' '.join(page.text['uc-confirm'].split())
    assert ('บรรทัดนี้เป็นใบลดหนี้ และมีการรับเข้า 1 รายการในวันเดียวกัน '
            'ต้นทุนเฉลี่ยจะขยับแม้เลือกคงยอดคงเหลือ') in panel
    assert _num(_one(page.values['uc-confirm'], 'reweights')) == 1


def test_preview_of_a_refused_unit_shows_the_refusal_and_no_confirm_panel(line):
    resp = _preview(_client('admin'), corrected_unit='ลัง')

    assert resp.status_code == 200
    page = _Page(resp.get_data(as_text=True))
    assert 'หน่วย "ลัง" ไม่ใช่หน่วยหลักและไม่มีอัตราแปลงของสินค้านี้' in page.text['uc-refused']
    assert 'uc-confirm' not in page.text
    assert [t for t, a in page.tags['uc-correct'] if t == 'input' and a.get('type') == 'radio'] == []
    # The select is still there to try again.
    assert [a.get('value') for tag, a in page.tags['uc-correct'] if tag == 'option'] == ['หลอด']


# ── apply ────────────────────────────────────────────────────────────────────

def test_apply_hold_corrects_the_line_as_the_logged_in_admin(line):
    path, pid = line
    (ledger_id, _qty), = sc.sale_ledger(path, LINE, pid)
    admin = _client('admin')

    resp = admin.post(PAGE + '/apply', data=APPLY)

    assert resp.status_code == 302
    assert _location(resp)[0] == '/sales/doc/' + DOC
    correction, = sc.corrections(path)
    assert (correction['status'], correction['stock_mode'], correction['created_by'],
            correction['reason']) == ('active', 'hold', 'put-admin', sc.REASON)
    assert sc.sales_row(path, LINE)['unit'] == 'หลอด'
    assert sc.sale_ledger(path, LINE, pid) == [(ledger_id, -2)]
    offset, = sc.offsets(path, pid)
    assert offset['quantity_change'] == -22 and offset['id'] == correction['offset_txn_id']
    assert sc.stock(path, pid) == 176
    (category, message), = _flashes(admin)
    assert category == 'success' and LINE in message


def test_apply_move_is_what_the_radio_said(line):
    path, pid = line

    resp = _client('admin').post(PAGE + '/apply', data=dict(APPLY, stock_mode='move'))

    assert resp.status_code == 302
    correction, = sc.corrections(path)
    assert correction['stock_mode'] == 'move'
    assert sc.offsets(path, pid) == []
    assert sc.stock(path, pid) == 198


@pytest.mark.parametrize('change, says', [
    ({'reason': 'สั้นไป'}, 'ต้องระบุเหตุผลอย่างน้อย 12 ตัวอักษร'),
    ({'corrected_unit': 'ลัง'}, 'ไม่ใช่หน่วยหลักและไม่มีอัตราแปลงของสินค้านี้'),
    ({'stock_mode': None}, 'ไม่รู้จักวิธีปรับสต็อก'),
    ({'reason': None}, 'ต้องระบุเหตุผลอย่างน้อย 12 ตัวอักษร'),
])
def test_a_refused_apply_writes_nothing_and_says_why(line, change, says):
    path, _pid = line
    before = _everything(path)
    data = {k: v for k, v in dict(APPLY, **change).items() if v is not None}
    admin = _client('admin')

    resp = admin.post(PAGE + '/apply', data=data)

    assert resp.status_code == 302
    assert _location(resp) == (PAGE, KEY)
    (category, message), = _flashes(admin)
    assert category == 'danger' and says in message
    assert _everything(path) == before


# ── cancel ───────────────────────────────────────────────────────────────────

@pytest.fixture
def corrected(line):
    path, pid = line
    cid = _luc(path, 'apply', LINE, sc.CODE, 'หลอด', 'hold', sc.REASON, 'put')
    assert len(sc.offsets(path, pid)) == 1
    return path, pid, cid


def test_cancel_puts_the_line_back_as_the_logged_in_admin(corrected):
    path, pid, cid = corrected
    admin = _client('admin')

    resp = admin.post(PAGE + '/cancel',
                      data={'correction_id': str(cid), 'reason': CANCEL_REASON})

    assert resp.status_code == 302
    assert _location(resp)[0] == '/sales/doc/' + DOC
    correction, = sc.corrections(path)
    assert (correction['status'], correction['ended_by'], correction['end_reason']) == \
        ('cancelled', 'put-admin', CANCEL_REASON)
    assert sc.sales_row(path, LINE)['unit'] == 'โหล'
    assert sc.sale_ledger(path, LINE, pid)[0][1] == -24
    assert sc.offsets(path, pid) == []
    assert sc.stock(path, pid) == 176
    (category, message), = _flashes(admin)
    assert category == 'success' and LINE in message


@pytest.mark.parametrize('data, says, back', [
    ({'reason': 'สั้นไป'}, 'ต้องระบุเหตุผลอย่างน้อย 12 ตัวอักษร', (PAGE, KEY)),
    ({'correction_id': '999999', 'reason': CANCEL_REASON},
     'การแก้หน่วยนี้ไม่ได้ค้างอยู่ ยกเลิกไม่ได้', ('/sales', {})),
    ({'correction_id': 'abc', 'reason': CANCEL_REASON},
     'การแก้หน่วยนี้ไม่ได้ค้างอยู่ ยกเลิกไม่ได้', ('/sales', {})),
])
def test_a_refused_cancel_writes_nothing_and_says_why(corrected, data, says, back):
    path, _pid, cid = corrected
    before = _everything(path)
    admin = _client('admin')

    resp = admin.post(PAGE + '/cancel', data=dict({'correction_id': str(cid)}, **data))

    assert resp.status_code == 302
    assert _location(resp) == back
    (category, message), = _flashes(admin)
    assert category == 'danger' and says in message
    assert _everything(path) == before


# ── who may post ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('role', ['manager', 'staff', 'shareholder'])
def test_a_non_admin_post_is_forbidden_and_writes_nothing(corrected, role):
    path, _pid, cid = corrected
    before = _everything(path)
    c = _client(role)

    refused = [
        c.post(PAGE + '/preview', data=dict(KEY, corrected_unit='หลอด')),
        c.post(PAGE + '/apply', data=APPLY),
        c.post(PAGE + '/cancel', data={'correction_id': str(cid), 'reason': CANCEL_REASON}),
    ]

    assert [r.status_code for r in refused] == [403, 403, 403]
    assert _everything(path) == before
    # Control: the same cancel from an admin does go through.
    _client('admin').post(PAGE + '/cancel',
                          data={'correction_id': str(cid), 'reason': CANCEL_REASON})
    assert sc.corrections(path)[0]['status'] == 'cancelled'


# ── refusals that are not the module's ───────────────────────────────────────

def _hold_import_lock(path):
    fd = os.open(os.path.join(os.path.dirname(path), 'express_import.lock'),
                 os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_apply_is_refused_while_an_express_import_holds_the_lock(line):
    path, _pid = line
    before = _everything(path)
    admin = _client('admin')
    fd = _hold_import_lock(path)
    try:
        resp = admin.post(PAGE + '/apply', data=APPLY)
    finally:
        os.close(fd)

    assert resp.status_code == 302 and _location(resp) == (PAGE, KEY)
    assert _flashes(admin) == [('danger', IMPORT_BUSY)]
    assert _everything(path) == before
    # Control: with the lock released the same request is written.
    admin.post(PAGE + '/apply', data=APPLY)
    assert [c['status'] for c in sc.corrections(path)] == ['active']


def test_cancel_is_refused_while_an_express_import_holds_the_lock(corrected):
    path, _pid, cid = corrected
    before = _everything(path)
    admin = _client('admin')
    data = {'correction_id': str(cid), 'reason': CANCEL_REASON}
    fd = _hold_import_lock(path)
    try:
        resp = admin.post(PAGE + '/cancel', data=data)
    finally:
        os.close(fd)

    assert resp.status_code == 302 and _location(resp) == (PAGE, KEY)
    assert _flashes(admin) == [('danger', IMPORT_BUSY)]
    assert _everything(path) == before
    admin.post(PAGE + '/cancel', data=data)
    assert [c['status'] for c in sc.corrections(path)] == ['cancelled']


def test_a_locked_database_is_answered_like_a_running_import(line, monkeypatch):
    import database
    path, _pid = line
    before = _everything(path)
    admin = _client('admin')

    def locked(conn):
        raise sqlite3.OperationalError('database is locked')
    with monkeypatch.context() as m:
        m.setattr(database, 'begin_immediate', locked)
        resp = admin.post(PAGE + '/apply', data=APPLY)

    assert resp.status_code == 302 and _location(resp) == (PAGE, KEY)
    assert _flashes(admin) == [('danger', IMPORT_BUSY)]
    assert _everything(path) == before


def test_a_cost_failure_is_a_flash_and_nothing_is_saved(line, monkeypatch):
    from models import wacc
    path, pid = line
    before = sc.written_state(path)
    admin = _client('admin')

    def broken(product_id, conn=None, **kwargs):
        raise wacc.WaccIdentityError('ทดสอบ', product_id=product_id,
                                     operation='unit_correction')
    with monkeypatch.context() as m:
        m.setattr(wacc, 'recalculate_product_wacc', broken)
        resp = admin.post(PAGE + '/apply', data=APPLY)

    assert resp.status_code == 302 and _location(resp) == (PAGE, KEY)
    assert _flashes(admin) == [
        ('danger', 'คำนวณต้นทุนไม่สำเร็จ ระบบจึงไม่บันทึกการแก้หน่วย (ดูหน้าแจ้งเตือน)')]
    assert sc.written_state(path) == before
    assert sc.sales_row(path, LINE)['unit'] == 'โหล'


def test_nothing_is_reachable_while_the_vat_book_is_active(corrected):
    """The book guard answers before the routes do: the VAT book is read-only
    and these pages are not part of it."""
    path, _pid, cid = corrected
    before = _everything(path)
    admin = _client('admin')
    assert admin.get(PAGE, query_string=KEY).status_code == 200
    with admin.session_transaction() as s:
        s['active_book'] = 'vat'

    responses = [
        admin.get(PAGE, query_string=KEY),
        admin.post(PAGE + '/preview', data=dict(KEY, corrected_unit='โหล')),
        admin.post(PAGE + '/apply', data=APPLY),
        admin.post(PAGE + '/cancel', data={'correction_id': str(cid), 'reason': CANCEL_REASON}),
    ]

    assert [r.status_code for r in responses] == [302, 302, 302, 302]
    assert {_location(r)[0] for r in responses} == {'/sales'}
    assert len(_flashes(admin)) == 4
    assert _everything(path) == before


# ── the invoice page ─────────────────────────────────────────────────────────

CODE2 = '031บ9501'


@pytest.fixture
def invoice(empty_db, monkeypatch):
    """One document, two lines, each its own product."""
    sc.seed_company(empty_db)
    sc.seed_product(empty_db)
    sc.seed_product(empty_db, CODE2, name='ใบมีดคัตเตอร์เล็ก')
    sc.run_zip(monkeypatch, sc.Book()
               .purchase('RR6900001', [(1, sc.CODE, 100.0, 'หลอด', 10.0),
                                       (2, CODE2, 100.0, 'หลอด', 10.0)],
                         datetime.date(2026, 3, 10))
               .sale(DOC, [(1, sc.CODE, 2.0, 'โหล', 49.0), (2, CODE2, 1.0, 'โหล', 49.0)]))
    return empty_db


def _cells(client):
    resp = client.get('/sales/doc/' + DOC)
    assert resp.status_code == 200
    page = _Page(resp.get_data(as_text=True))
    return page, [page.links['uc-cell-1'], page.links['uc-cell-2']]


def _target(href):
    parts = urlsplit(href)
    return parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}


def test_the_invoice_offers_an_admin_the_link_on_every_line(invoice):
    page, (first, second) = _cells(_client('admin'))

    assert [len(first), len(second)] == [1, 1]
    assert _target(first[0][0]) == (PAGE, KEY)
    assert _target(second[0][0]) == (PAGE, {'doc_no': DOC + '-2', 'bsn_code': CODE2})
    assert first[0][2] == second[0][2] == 'แก้หน่วย'
    assert 'โหล' in page.text['uc-cell-1']


def test_the_invoice_offers_a_manager_no_link(invoice):
    page, cells = _cells(_client('manager'))

    assert cells == [[], []]
    assert 'แก้หน่วย' not in page.text['uc-cell-1']
    # Control: the cell is the quantity cell and it rendered.
    assert 'โหล' in page.text['uc-cell-1'] and 'โหล' in page.text['uc-cell-2']


def test_the_invoice_shows_the_active_correction_as_a_badge(invoice):
    _luc(invoice, 'apply', LINE, sc.CODE, 'หลอด', 'hold', sc.REASON, 'put')

    page, (first, second) = _cells(_client('admin'))

    (href, title, text), = first
    assert _target(href) == (PAGE, KEY)
    assert text == 'แก้หน่วย: โหล → หลอด'
    assert 'put' in title and sc.REASON in title
    assert [link[2] for link in second] == ['แก้หน่วย']

    page, (first, second) = _cells(_client('manager'))
    assert first == [] and second == []
    assert 'แก้หน่วย: โหล → หลอด' in ' '.join(page.text['uc-cell-1'].split())
    assert 'แก้หน่วย' not in page.text['uc-cell-2']


def test_the_invoice_shows_an_ended_correction_muted(invoice):
    cid = _luc(invoice, 'apply', LINE, sc.CODE, 'หลอด', 'hold', sc.REASON, 'put')
    _luc(invoice, 'cancel', cid, CANCEL_REASON, 'put')

    page, (first, second) = _cells(_client('admin'))

    (href, _title, text), = first
    assert _target(href) == (PAGE, KEY) and text == 'เคยแก้หน่วย'
    assert [link[2] for link in second] == ['แก้หน่วย']

    page, (first, _second) = _cells(_client('manager'))
    assert first == []
    assert 'เคยแก้หน่วย' in page.text['uc-cell-1']


def test_the_invoice_in_the_vat_book_shows_nothing_about_corrections(invoice):
    """The VAT book here is a copy of the main one, correction row included:
    the page must not read it."""
    import book_registry
    _luc(invoice, 'apply', LINE, sc.CODE, 'หลอด', 'hold', sc.REASON, 'put')
    vat = sc.clone_db(invoice, book_registry.book_db_path('vat'))
    c = sqlite3.connect(vat)
    c.execute("PRAGMA journal_mode=DELETE")
    c.close()
    admin = _client('admin')
    _page, (first, _second) = _cells(admin)
    assert len(first) == 1
    with admin.session_transaction() as s:
        s['active_book'] = 'vat'

    page, cells = _cells(admin)

    assert cells == [[], []]
    assert 'แก้หน่วย' not in page.text['uc-cell-1']
    assert 'หลอด' in page.text['uc-cell-1']


# ── ตรวจบิล ──────────────────────────────────────────────────────────────────

def _flag(path, doc_no, txn_id, message):
    c = sc.raw(path)
    c.execute("INSERT OR IGNORE INTO txn_review_docs (doc_base, date_iso, line_count,"
              " flag_count, max_severity) VALUES (?, '2026-04-01', 2, 2, 'high')", (DOC,))
    c.execute("INSERT INTO txn_review_flags (doc_base, txn_id, doc_no, rule_code,"
              " severity, message_th) VALUES (?, ?, ?, 'R2_BELOW_COST', 'high', ?)",
              (DOC, txn_id, doc_no, message))
    c.commit()
    c.close()


def _review(client):
    resp = client.get('/review', query_string={'all': '1'})
    assert resp.status_code == 200
    return _Page(resp.get_data(as_text=True))


def test_the_review_page_links_an_admin_from_a_flagged_line_to_its_page(invoice):
    _flag(invoice, LINE, sc.sales_row(invoice, LINE)['id'], 'ธงของบรรทัดหนึ่ง')
    # A flag whose sales row is gone names no line any more, and neither does
    # one whose row id now belongs to another line.
    _flag(invoice, DOC + '-7', 999999, 'ธงของบรรทัดที่หายไป')
    _flag(invoice, DOC + '-8', sc.sales_row(invoice, DOC + '-2', CODE2)['id'],
          'ธงที่เลขแถวกลายเป็นของบรรทัดอื่น')

    page = _review(_client('admin'))

    flags = page.text['review-doc-' + DOC]
    for message in ('ธงของบรรทัดหนึ่ง', 'ธงของบรรทัดที่หายไป',
                    'ธงที่เลขแถวกลายเป็นของบรรทัดอื่น'):
        assert message in flags, message
    targets = [_target(href) for href, _t, text in page.links['review-doc-' + DOC]
               if text == 'แก้หน่วย']
    assert targets == [(PAGE, KEY)]


def test_the_review_page_offers_a_manager_no_link(invoice):
    _flag(invoice, LINE, sc.sales_row(invoice, LINE)['id'], 'ธงของบรรทัดหนึ่ง')

    page = _review(_client('manager'))

    assert 'ธงของบรรทัดหนึ่ง' in page.text['review-doc-' + DOC]
    assert 'แก้หน่วย' not in page.text['review-doc-' + DOC]
    assert [l for l in page.links['review-doc-' + DOC] if urlsplit(l[0]).path == PAGE] == []
