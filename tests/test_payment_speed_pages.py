"""#499 — the payment-speed figure on the customer page and `/m/customer`.

Route-level renders over `tmp_db`, which clones the live dev DB WITH its data:
every row asserted on here is FORCED (deleted by key first, then inserted),
never inherited.

Assertions are scoped to the ELEMENT carrying the figure, never a page-wide
substring — the same words can sit in a `<script>`, a comment or a modal. Every
"no figure" test carries a CONTROL proving the page rendered the region the
figure would have sat in, so an absent element means "not shown", not "the
page took another branch".
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import re
import sqlite3
from html.parser import HTMLParser
from urllib.parse import quote

import payments_alloc as pa


FAST = ('ZZPS01', 'ร้านทดสอบจ่ายไว')        # 3 settled bills → a figure
THIN = ('ZZPS02', 'ร้านทดสอบบิลน้อย')        # 2 settled bills → no figure
NOROW = ('ZZPS03', 'ร้านทดสอบไม่มีทะเบียน')  # 3 settled bills, NO customers row
CODES = [FAST[0], THIN[0], NOROW[0]]

# (doc_base, invoice date, receipt no, receipt date). Days: 20, 40, 30 → median 30.
# ZZPSRE12 pays two bills in one collection round → 3 bills / 2 receipts.
FAST_BILLS = [('ZZPSIV11', '2026-01-01', 'ZZPSRE11', '2026-01-21'),
              ('ZZPSIV12', '2026-01-10', 'ZZPSRE12', '2026-02-19'),
              ('ZZPSIV13', '2026-01-20', 'ZZPSRE12', '2026-02-19')]
THIN_BILLS = [('ZZPSIV21', '2026-01-01', 'ZZPSRE21', '2026-01-11'),
              ('ZZPSIV22', '2026-01-02', 'ZZPSRE22', '2026-01-12')]
NOROW_BILLS = [('ZZPSIV31', '2026-01-01', 'ZZPSRE31', '2026-01-11'),
               ('ZZPSIV32', '2026-01-02', 'ZZPSRE32', '2026-01-12'),
               ('ZZPSIV33', '2026-01-03', 'ZZPSRE33', '2026-01-13')]


def _seed(db_path):
    conn = sqlite3.connect(db_path)
    try:
        docs = [b[0] for b in FAST_BILLS + THIN_BILLS + NOROW_BILLS]
        res = sorted({b[2] for b in FAST_BILLS + THIN_BILLS + NOROW_BILLS})
        q = lambda xs: ','.join('?' * len(xs))
        conn.execute(f"DELETE FROM paid_invoices WHERE doc_no IN ({q(docs)})", docs)
        conn.execute(
            f"DELETE FROM paid_invoices WHERE re_id IN "
            f"(SELECT id FROM received_payments WHERE re_no IN ({q(res)}))", res)
        conn.execute(f"DELETE FROM received_payments WHERE re_no IN ({q(res)})", res)
        conn.execute(f"DELETE FROM sales_transactions WHERE customer_code IN ({q(CODES)})", CODES)
        conn.execute(f"DELETE FROM customers WHERE code IN ({q(CODES)})", CODES)

        for code, name in (FAST, THIN):
            conn.execute("INSERT INTO customers (code, name, credit_days) VALUES (?,?,30)",
                         (code, name))

        receipts = {}
        for (code, name), bills in ((FAST, FAST_BILLS), (THIN, THIN_BILLS),
                                    (NOROW, NOROW_BILLS)):
            for doc, inv_date, re_no, re_date in bills:
                conn.execute(
                    """INSERT INTO sales_transactions
                       (date_iso, doc_no, doc_base, customer, customer_code,
                        qty, unit, unit_price, vat_type, total, net)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (inv_date, f'{doc}-1', doc, name, code, 1, 'ตัว', 1000, 1, 1000, 1000))
                if re_no not in receipts:
                    receipts[re_no] = conn.execute(
                        """INSERT INTO received_payments
                           (re_no, date_iso, customer, salesperson, cancelled, total)
                           VALUES (?,?,?,?,0,NULL)""",
                        (re_no, re_date, name, 'S1')).lastrowid
                conn.execute(
                    "INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount) "
                    "VALUES (?,?,'IV',1000)", (receipts[re_no], doc))
        conn.commit()
    finally:
        conn.close()


def _client(role='admin'):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = role
        s['role'] = role
    return c


class _ById(HTMLParser):
    """Collects the visible text of every element carrying `id=element_id`,
    nested children included (a regex stops at the first inner close tag)."""

    def __init__(self, element_id):
        super().__init__()
        self.element_id = element_id
        self.found = []      # one text list per matching element
        self._depth = 0      # open tags inside the current match

    _VOID = {'br', 'img', 'input', 'hr', 'meta', 'link', 'wbr'}

    def handle_starttag(self, tag, attrs):
        if tag in self._VOID:
            return
        if self._depth:
            self._depth += 1
        elif dict(attrs).get('id') == self.element_id:
            self.found.append([])
            self._depth = 1

    def handle_endtag(self, tag):
        if self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if self._depth:
            self.found[-1].append(data)


def _element(html, element_id):
    """Text of each element with this id — the caller asserts the COUNT first,
    so zero or two matches is a failure, never a silent pass."""
    p = _ById(element_id)
    p.feed(html)
    return [re.sub(r'\s+', ' ', ''.join(parts)).strip() for parts in p.found]


def _get(client, url):
    r = client.get(url)
    assert r.status_code == 200, url
    return r.data.decode()


def _credit_cell(html):
    """CONTROL for the desktop header: the เครดิต cell rendered for THIS page."""
    m = re.search(r'<div class="text-subtle small mb-1">เครดิต</div>\s*'
                  r'<div class="fw-500">([^<]*)</div>', html)
    return m.group(1).strip() if m else None


# ── desktop /customer/code/<code> ────────────────────────────────────────────

def test_customer_page_shows_the_figure_beside_the_credit_term(tmp_db):
    _seed(tmp_db)
    html = _get(_client(), f'/customer/code/{quote(FAST[0])}')
    assert _credit_cell(html) == '30 วัน', 'CONTROL: the header cells never rendered'

    found = _element(html, 'pay-speed')
    assert len(found) == 1, found
    text = found[0]
    assert 'จ่ายจริง' in text
    assert '~30 วัน' in text
    assert '3 บิล / 2 ใบเสร็จ' in text
    assert '2026-01-01 ถึง 2026-01-20' in text


def test_customer_page_figure_is_not_gated_like_cost(tmp_db):
    """Not cost data: staff (who never sees cost) gets the same figure."""
    _seed(tmp_db)
    html = _get(_client('staff'), f'/customer/code/{quote(FAST[0])}')
    found = _element(html, 'pay-speed')
    assert len(found) == 1, found
    assert '~30 วัน' in found[0]


def test_customer_page_omits_the_figure_below_three_settled_bills(tmp_db):
    _seed(tmp_db)
    html = _get(_client(), f'/customer/code/{quote(THIN[0])}')
    assert _credit_cell(html) == '30 วัน', 'CONTROL: the header cells never rendered'
    assert _element(html, 'pay-speed') == []
    assert 'จ่ายจริง' not in html


def test_customer_page_without_a_customers_row_shows_no_figure(tmp_db):
    """Same rule as the mobile page: the header's detail cells all come from the
    customers row, and a code without one shows none of them."""
    _seed(tmp_db)
    assert pa.payment_speed(NOROW[0])['invoices'] == 3, 'CONTROL: enough data for a figure'
    html = _get(_client(), f'/customer/code/{quote(NOROW[0])}')
    assert 'ยังไม่มีใน master' in html, 'CONTROL: the no-master-row branch rendered'
    assert _element(html, 'pay-speed') == []


# ── mobile /m/customer/code/<code> ──────────────────────────────────────────

def test_mobile_page_shows_the_figure_beside_the_credit_badge(tmp_db):
    _seed(tmp_db)
    html = _get(_client(), f'/m/customer/code/{quote(FAST[0])}')
    assert 'เครดิต 30 วัน' in html, 'CONTROL: the header badges never rendered'

    found = _element(html, 'm-pay-speed')
    assert len(found) == 1, found
    assert found[0] == 'จ่ายจริง ~30 วัน · 3 บิล / 2 ใบเสร็จ'


def test_mobile_page_omits_the_figure_below_three_settled_bills(tmp_db):
    _seed(tmp_db)
    html = _get(_client(), f'/m/customer/code/{quote(THIN[0])}')
    assert 'เครดิต 30 วัน' in html, 'CONTROL: the header badges never rendered'
    assert _element(html, 'm-pay-speed') == []
    assert 'จ่ายจริง' not in html


def test_mobile_page_without_a_customers_row_uses_the_url_code_for_figure(tmp_db):
    """Payment speed is code-keyed even when no customers-master row exists."""
    _seed(tmp_db)
    assert pa.payment_speed(NOROW[0])['invoices'] == 3, 'CONTROL: enough data for a figure'
    html = _get(_client(), f'/m/customer/code/{quote(NOROW[0])}')
    assert 'ไม่พบข้อมูลลูกค้า' not in html, 'CONTROL: took the not-found branch'
    assert 'ขายล่าสุด' in html, 'CONTROL: the page rendered its bills'
    assert NOROW[1] in html, 'the no-master page did not fall back to its latest bill name'
    found = _element(html, 'm-pay-speed')
    assert len(found) == 1, found
    assert found[0] == 'จ่ายจริง ~10 วัน · 3 บิล / 3 ใบเสร็จ'
