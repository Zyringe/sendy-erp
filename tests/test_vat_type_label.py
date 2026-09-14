"""#495: vat_type 1 reads "ไม่บวก VAT", from ONE definition in macros.html.

`vat_type` is Express's per-document VAT mode (CONTEXT.md). Type 1 used to be
labelled "รวม VAT" (VAT included) at nine places, each typed by hand. Measured
on prod (triage, 2026-09-13) that is literally true for only 2 of 15,823 type-1
sales rows and 131 of 820 type-1 purchase documents, where Express carved 7%
out of the price. On every other type-1 sale the customer paid `net` with no
VAT in it. "ไม่บวก VAT" (no VAT added on top) is true of all of them. Put's
ruling: #495, 2026-09-13.

Two kinds of test:
  * one render test per surface, asserting on the ELEMENT (a badge's text, an
    <option>'s text, a stat-card label, a badge's `title`), found through a
    small stdlib DOM, never a page-wide substring (the same words live in
    scripts, comments and modals). Each asserts the element COUNT first, and
    reads elements this test seeded, so a removed element cannot pass.
  * a template sweep that fails when a template re-types the old label.

FIXTURE DISCIPLINE: `tmp_db` clones the live dev DB with its data. Every row a
test reads is inserted here under a key asserted absent first.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import re  # noqa: E402
import sqlite3  # noqa: E402
from html.parser import HTMLParser  # noqa: E402

import pytest  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(REPO, 'inventory_app', 'templates')

LABEL = {1: 'ไม่บวก VAT', 2: 'แยก VAT', 0: 'ยกเว้น VAT'}

# One sales document per vat_type, one purchase document of type 1.
SALES_DOCS = {1: 'IVTEST495A', 2: 'IVTEST495B', 0: 'IVTEST495C'}
SALES_SEARCH = 'IVTEST495'
PURCH_DOC = 'RRTEST495A'
CUSTOMER = 'ลูกค้าทดสอบ 495'


# ── a small DOM over html.parser (no bs4/lxml in requirements) ───────────────

_VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
         'meta', 'source', 'track', 'wbr'}


class _Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs), parent, []

    def iter(self):
        yield self
        for ch in self.children:
            if isinstance(ch, _Node):
                yield from ch.iter()

    def text(self):
        raw = ''.join(ch.text() if isinstance(ch, _Node) else ch for ch in self.children)
        return ' '.join(raw.split())          # also folds &nbsp; (\xa0)

    def has_class(self, cls):
        return cls in (self.attrs.get('class') or '').split()


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__()
        self.root = self.cur = _Node('#root', [], None)

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs, self.cur)
        self.cur.children.append(node)
        if tag not in _VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(_Node(tag, attrs, self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def _dom(html):
    t = _Tree()
    t.feed(html)
    t.close()
    return t.root


def _els(root, tag, cls=None, **attrs):
    """Every `tag` under root carrying class `cls` and the given attributes
    (pass data-* names with underscores: data_label='VAT')."""
    want = {k.replace('_', '-'): v for k, v in attrs.items()}
    return [n for n in root.iter()
            if n.tag == tag
            and (cls is None or n.has_class(cls))
            and all(n.attrs.get(k) == v for k, v in want.items())]


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def admin_client(tmp_db):
    """Session injection, not a real login (this Python has no hashlib.scrypt)."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


def _assert_absent(conn, table, like):
    n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE doc_no LIKE ?",
                     (like + '%',)).fetchone()[0]
    assert n == 0, f'{table} already holds {like}* rows: the fixture would inherit them'


@pytest.fixture
def seeded(tmp_db):
    """Sales docs A/B/C (vat_type 1/2/0) + purchase doc RRTEST495A (vat_type 1),
    all on one fresh product with a distinct unit_price per vat_type."""
    conn = sqlite3.connect(tmp_db, timeout=10)
    _assert_absent(conn, 'sales_transactions', SALES_SEARCH)
    _assert_absent(conn, 'purchase_transactions', PURCH_DOC)
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type, is_active)"
        " VALUES ('สินค้าทดสอบป้าย VAT 495', 'ตัว', 1)").lastrowid
    for vt, doc in SALES_DOCS.items():
        price = {1: 123.45, 2: 234.56, 0: 345.67}[vt]
        conn.execute(
            "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id,"
            " bsn_code, product_name_raw, customer, customer_code, qty, unit,"
            " unit_price, vat_type, total, net, synced_to_stock)"
            " VALUES ('2026-08-01', ?, ?, ?, 'T495', 'สินค้าทดสอบ', ?, 'T495',"
            " 1, 'ตัว', ?, ?, ?, ?, 0)",
            (doc + '-1', doc, pid, CUSTOMER, price, vt, price, price))
    conn.execute(
        "INSERT INTO purchase_transactions (date_iso, doc_no, doc_base, product_id,"
        " bsn_code, product_name_raw, supplier, supplier_code, qty, unit,"
        " unit_price, vat_type, total, net, synced_to_stock)"
        " VALUES ('2026-08-01', ?, ?, ?, 'T495', 'สินค้าทดสอบ', 'ผู้จำหน่ายทดสอบ',"
        " 'T495', 1, 'ตัว', 100, 1, 100, 100, 0)",
        (PURCH_DOC + '-1', PURCH_DOC, pid))
    conn.commit()
    conn.close()
    return pid


# ── the one definition ───────────────────────────────────────────────────────

@pytest.mark.parametrize('vat_type, expected', [
    (1, 'ไม่บวก VAT'), (2, 'แยก VAT'), (0, 'ยกเว้น VAT'), (None, 'ยกเว้น VAT')])
def test_the_shared_label_macro(tmp_db, vat_type, expected):
    from app import app as flask_app
    macros = flask_app.jinja_env.get_template('macros.html').module
    assert macros.vat_label(vat_type) == expected


# ── surface 1: the invoice page header badge (sales.sales_doc) ───────────────

@pytest.mark.parametrize('vat_type', [1, 2, 0])
def test_invoice_header_badge(admin_client, seeded, vat_type):
    r = admin_client.get(f'/sales/doc/{SALES_DOCS[vat_type]}')
    assert r.status_code == 200
    root = _dom(r.data.decode())
    # The info bar's VAT column: the col whose caption span reads "VAT".
    cols = [c for c in _els(root, 'div', 'col-auto')
            if (caps := _els(c, 'span', 'text-subtle')) and caps[0].text() == 'VAT']
    assert len(cols) == 1
    badges = _els(cols[0], 'span', 'badge')
    assert len(badges) == 1
    assert badges[0].text() == LABEL[vat_type]


# ── surfaces 2-4: the sales list (sales.sales_view) ──────────────────────────

def _stat_cards(root):
    """{'Type N' badge text: (label text, sub text)} for the three VAT cards."""
    cards = _els(root, 'div', 'stat-card')
    assert len(cards) == 3
    out = {}
    for card in cards:
        (label,) = _els(card, 'div', 'stat-card-label')
        (type_badge,) = _els(label, 'span', 'badge')
        (sub,) = _els(card, 'div', 'stat-card-sub')
        out[type_badge.text()] = (label.text(), sub.text())
    return out


def _vat_options(root):
    (select,) = _els(root, 'select', name='vat_type')
    opts = _els(select, 'option')
    assert len(opts) == 4
    return {o.attrs.get('value'): (o.text(), 'selected' in o.attrs) for o in opts}


def test_sales_list_stat_cards(admin_client, seeded):
    root = _dom(admin_client.get(f'/sales?doc_no={SALES_SEARCH}').data.decode())
    cards = _stat_cards(root)
    # CONTROL: each card counts the one seeded line of its type, so the label
    # sits on the card that summarises vat_type N, not merely on some card.
    assert cards == {
        'Type 1': ('ไม่บวก VAT Type 1', '1 รายการ'),
        'Type 2': ('แยก VAT Type 2', '1 รายการ'),
        'Type 0': ('ยกเว้น VAT Type 0', '1 รายการ'),
    }


def test_sales_list_filter_options_and_filter(admin_client, seeded):
    root = _dom(admin_client.get(f'/sales?doc_no={SALES_SEARCH}&vat_type=1').data.decode())
    assert _vat_options(root) == {
        '': ('ทั้งหมด', False),
        '1': ('ไม่บวก VAT (1)', True),
        '2': ('แยก VAT (2)', False),
        '0': ('ยกเว้น VAT (0)', False),
    }
    # Filtering is unchanged: ?vat_type=1 keeps exactly the type-1 document.
    docs = [td.text() for td in _els(root, 'td', data_label='เอกสาร')]
    assert docs == [SALES_DOCS[1]]


def test_sales_list_row_badge(admin_client, seeded):
    root = _dom(admin_client.get(f'/sales?doc_no={SALES_SEARCH}').data.decode())
    rows = [tr for tr in _els(root, 'tr') if _els(tr, 'td', data_label='VAT')]
    assert len(rows) == 3
    got = {}
    for tr in rows:
        (doc,) = _els(tr, 'td', data_label='เอกสาร')
        (vat,) = _els(tr, 'td', data_label='VAT')
        (badge,) = _els(vat, 'span', 'badge')
        got[doc.text()] = badge.text()
    assert got == {SALES_DOCS[vt]: LABEL[vt] for vt in (1, 2, 0)}


# ── surface 5: the product pricing page (products.product_pricing) ───────────

def test_pricing_list_price_badge(admin_client, seeded):
    r = admin_client.get(f'/products/{seeded}/pricing')
    assert r.status_code == 200
    root = _dom(r.data.decode())
    rows = [tr for tr in _els(root, 'tr')
            if (tds := _els(tr, 'td')) and tds[0].has_class('fw-600')
            and tds[0].text().startswith('฿')]
    assert len(rows) == 3
    got = {}
    for tr in rows:
        price_td, vat_td = _els(tr, 'td')[:2]
        (badge,) = _els(vat_td, 'span', 'badge')
        got[price_td.text()] = badge.text()
    assert got == {'฿123.45': LABEL[1], '฿234.56': LABEL[2], '฿345.67': LABEL[0]}


# ── surface 6: /ar transfer matcher, the bill badge's tooltip ────────────────

@pytest.fixture
def match_seeded(tmp_db):
    """ONLY the three seeded bills are outstanding, so a search for their sum
    has exactly one answer (same wipe as tests/test_payment_candidates.py)."""
    conn = sqlite3.connect(tmp_db, timeout=10)
    for trg in ('audit_sales_transactions_insert', 'audit_sales_transactions_delete'):
        conn.execute(f'DROP TRIGGER IF EXISTS {trg}')     # throwaway copy
    conn.execute('DELETE FROM sales_transactions')
    for vt, doc in SALES_DOCS.items():
        net = {1: 1000.0, 2: 1000.0, 0: 500.0}[vt]         # type 2 is owed net x 1.07
        conn.execute(
            "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, customer,"
            " customer_code, qty, unit_price, vat_type, net, synced_to_stock)"
            " VALUES ('2026-08-01', ?, ?, ?, 'T495', 1, ?, ?, ?, 0)",
            (doc + '-1', doc, CUSTOMER, net, vt, net))
    conn.commit()
    conn.close()


def _bill_titles(root):
    """{doc_base: title} for the matched-bill badges of the candidates table."""
    badges = [b for b in _els(root, 'span', 'badge') if b.text() in SALES_DOCS.values()]
    assert len(badges) == 3
    return {b.text(): b.attrs.get('title') for b in badges}


def test_ar_match_bill_tooltip(admin_client, match_seeded):
    # 1000.00 + 1070.00 + 500.00: one combination, all three bills.
    root = _dom(admin_client.get('/ar?tab=match&amount=2570&tol=0').data.decode())
    assert _bill_titles(root) == {SALES_DOCS[vt]: LABEL[vt] for vt in (1, 2, 0)}


# ── surface 7: payment_customers.html, the same tooltip ──────────────────────

def test_payment_customers_bill_tooltip(tmp_db):
    """No route renders this template today: `sales.payment_customers` has
    redirected to /ar?tab=customers since the AR consolidation, and the matcher
    moved to /ar?tab=match (surface 6). It still carried the old label, so it is
    rendered directly here to keep the ninth site pinned if it is ever revived."""
    from flask import render_template
    from app import app as flask_app
    bills = [{'doc_base': SALES_DOCS[vt], 'vat_type': vt} for vt in (1, 2, 0)]
    candidate = {'customer': CUSTOMER, 'matched_bills': bills, 'matched_sum': 2570.0,
                 'diff': 0.0, 'total_unpaid_bills': 3, 'total_outstanding': 2570.0}
    with flask_app.test_request_context('/'):
        html = render_template('payment_customers.html', rows=[], total_outstanding=0,
                               search='', match_str='2570', match_amount=2570.0,
                               candidates=[candidate])
    assert _bill_titles(_dom(html)) == {SALES_DOCS[vt]: LABEL[vt] for vt in (1, 2, 0)}


# ── surfaces 8-9: the purchases list (sales.purchases_view) ──────────────────

def test_purchases_list_stat_card_and_filter(admin_client, seeded):
    root = _dom(admin_client.get(f'/purchases?doc_no={PURCH_DOC}&vat_type=1').data.decode())
    cards = _stat_cards(root)
    # CONTROL: the Type 1 card is the one counting the seeded type-1 purchase.
    assert cards['Type 1'] == ('ไม่บวก VAT Type 1', '1 รายการ')
    assert cards['Type 2'][0] == 'แยก VAT Type 2'
    assert cards['Type 0'][0] == 'ยกเว้น VAT Type 0'
    assert _vat_options(root) == {
        '': ('ทั้งหมด', False),
        '1': ('ไม่บวก VAT (1)', True),
        '2': ('แยก VAT (2)', False),
        '0': ('ยกเว้น VAT (0)', False),
    }


# ── the sweep: nobody re-types the old label ─────────────────────────────────

# "รวม VAT" as a LABEL = the phrase starting a word. Thai runs words together,
# so a legitimate phrase that merely contains it is glued to the Thai letter
# before it: ไม่รวม VAT (ex-VAT, /accounting /cashflow /revenue), ยอดรวมรวม VAT
# (the แยก VAT grand total on the invoice page). "(รวม VAT)" qualifies a
# VAT-inclusive price (the vat-sub price box). Jinja comments never render.
# Cannot see: labels built in Python or static JS, and an exempt shape used AS a
# label ("(รวม VAT)", a glued "ราคารวม VAT"). On a false positive, e.g. a spaced
# <th>รวม VAT</th> VAT-total header, reword it ("ยอดรวม VAT") rather than loosen this.
_PHRASE = r'รวม(?:\s|&nbsp;|&#160;)*VAT'
_THAI = r'[\u0e00-\u0e7f]'
_OLD_LABEL = re.compile(r'(?<!' + _THAI + r')(?<!\()' + _PHRASE, re.I)
# The two exemptions as positive shapes, for the real-input control in the sweep.
_EXEMPT_SHAPES = (
    ('Thai-glued "…รวม VAT"', re.compile(_THAI + _PHRASE, re.I)),
    ('parenthesised "(รวม VAT)"', re.compile(r'\(' + _PHRASE, re.I)),
)
_JINJA_COMMENT = re.compile(r'\{#.*?#\}', re.S)


def _rendered_source(text):
    """Blank out Jinja comments, keeping newlines so line numbers survive."""
    return _JINJA_COMMENT.sub(lambda m: re.sub(r'[^\n]', ' ', m.group()), text)


def _old_label_lines(text):
    src = _rendered_source(text)
    return [src.count('\n', 0, m.start()) + 1 for m in _OLD_LABEL.finditer(src)]


@pytest.mark.parametrize('snippet', [
    '<span class="badge badge-success">รวม VAT</span>',                 # header / row / pricing badge
    '<span class="badge badge-success">\n    รวม VAT\n</span>',
    '<i class="bi bi-receipt me-1"></i>รวม VAT &nbsp;<span class="badge">Type 1</span>',  # stat card
    '<option value="1" {% if vat_type == 1 %}selected{% endif %}>รวม VAT (1)</option>',
    '<span class="badge me-1" title="รวม VAT">{{ b.doc_base }}</span>',  # tooltip
    "{{ 'รวม VAT' if vat_type == 1 else 'แยก VAT' }}",                    # a Jinja literal
    '<span class="badge">รวมVAT</span>',
])
def test_the_sweep_flags_every_shape_of_the_old_label(snippet):
    assert _old_label_lines(snippet) == [1 + snippet[:snippet.index('รวม')].count('\n')]


@pytest.mark.parametrize('snippet', [
    'BSN เท่านั้น &nbsp;·&nbsp; ไม่รวม VAT',
    '<td colspan="7" class="text-end">ยอดรวมรวม VAT</td>',
    'ราคาที่ลูกค้าจ่ายจริง (รวม VAT) ต่อ',
    '{# type 1 used to read รวม VAT #}',
])
def test_the_sweep_leaves_legitimate_uses_alone(snippet):
    assert _old_label_lines(snippet) == []


def test_no_template_retypes_the_old_type_1_label():
    bodies = {}
    for root, _dirs, files in os.walk(TEMPLATES):
        for fn in files:
            if fn.endswith('.html'):
                path = os.path.join(root, fn)
                with open(path, encoding='utf-8') as fh:
                    bodies[os.path.relpath(path, TEMPLATES)] = fh.read()
    # CONTROLS. The walk read the tree and went into subfolders, and each exempt
    # shape still occurs in some real template, so the clean result below is the
    # exemption at work on real input. Shapes, not pages: rewording one page cannot
    # turn this red unless that page held the last real example of a shape.
    assert len(bodies) > 100, f'the walk read only {len(bodies)} templates'
    assert any(os.sep in rel for rel in bodies), 'the walk never entered a subfolder'
    for name, shape in _EXEMPT_SHAPES:
        assert any(shape.search(_rendered_source(b)) for b in bodies.values()), (
            f'no template holds a {name} phrase any more, so that exemption is not '
            'exercised on real input. If the wording was retired on purpose, drop the '
            'shape here: test_the_sweep_leaves_legitimate_uses_alone still pins it.')

    flagged = [f'{rel}:{line}' for rel, body in sorted(bodies.items())
               for line in _old_label_lines(body)]
    assert flagged == [], (
        'vat_type 1 is "ไม่บวก VAT" (#495). Render it with vat_label / vat_badge '
        "from macros.html instead of typing a label: " + ', '.join(flagged))
