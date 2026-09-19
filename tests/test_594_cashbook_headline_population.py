"""/cashbook headline totals read the SAME population as the breakdown under them (#594).

Before #594 the headline cards (รายรับรวม / รายจ่ายรวม / สุทธิเดือนนี้) were
summed from the per-account table, which lists ACTIVE accounts only, while
the category summary, สรุปรายเดือน and drill-down read every non-transfer
account. The two agreed in every month only because no inactive account
carried operating money. Migration 188 un-flags 904 and keeps it inactive, so
without this change 2026-02 would read ฿295,524.05 on the card and
฿326,702.96 in the table one line below it (measured on the prod snapshot of
2026-09-19 15:36Z). ADR 0017's premise is that no flag hides history, and
deactivation is a flag.

So: the totals move onto the non-transfer population; the per-account table
stays active-only, and a disclosure line names any closed account whose money
is in the totals but not in the table.

The guard: headline == category-summary totals for every month and for
all-time, on a fixture AND on the live-DB clone with the post-188 state
forced. Each run carries a CONTROL that an inactive account really
contributes money in some month — without one, every month would agree
vacuously (verification-discipline.md, "A test that cannot fail" #8).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import re
import sqlite3

import pytest
from lxml import html as lxml_html

TRANSFER = 'เงินทุน/เงินโอน'


def _client():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


def _money(text):
    return round(float(re.sub(r'[฿,\s]', '', text)), 2)


def _headline(client, month):
    """(income, expense, card3, card3_label, closed-account note or None, table codes) as RENDERED."""
    resp = client.get(f'/cashbook/?month={month}')
    assert resp.status_code == 200, resp.data[:500]
    t = lxml_html.fromstring(resp.data.decode('utf-8'))

    def card(label_xpath):
        vals = t.xpath(f"//div[contains(@class,'stat-card-label')][{label_xpath}]"
                       "/following-sibling::div[contains(@class,'stat-card-value')][1]")
        assert len(vals) == 1, (month, label_xpath, len(vals))
        return vals[0].text_content()

    income = _money(card("normalize-space(text())='รายรับรวม'"))
    expense = _money(card("contains(normalize-space(.),'รายจ่ายรวม')"))
    label = t.xpath("//div[contains(@class,'stat-card-label')]"
                    "[normalize-space(.)='สุทธิเดือนนี้' or normalize-space(.)='คงเหลือ']")
    assert len(label) == 1, month
    card3 = _money(card(f"normalize-space(.)='{label[0].text_content().strip()}'"))
    note = t.xpath("//li[@id='cb-closed-accounts-note']")
    table = [' '.join(td.text_content().split()) for td in t.xpath(
        "//div[contains(@class,'card')][div[contains(@class,'card-header')]"
        "[contains(normalize-space(.),'บัญชีดำเนินการ')]]//tbody/tr/td[1]")]
    return income, expense, card3, label[0].text_content().strip(), \
        (note[0].text_content() if note else None), table


def _category_totals(db, month):
    from blueprints.cashbook import _get_category_summary
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        inc, exp = _get_category_summary(conn, None if month == 'ทั้งหมด' else month)
    finally:
        conn.close()
    return round(sum(c['total'] for c in inc), 2), round(sum(c['total'] for c in exp), 2)


def _inactive_contribution(db, month):
    """Operating income + expense of INACTIVE non-transfer accounts in scope —
    an independent SQL path, the far side of the old `is_active = 1` filter."""
    conn = sqlite3.connect(db)
    where = '' if month == 'ทั้งหมด' else " AND strftime('%Y-%m', t.txn_date) = ?"
    args = () if month == 'ทั้งหมด' else (month,)
    try:
        return conn.execute(f"""
            SELECT COALESCE(SUM(t.amount), 0) FROM cashbook_transactions t
              JOIN cashbook_accounts a ON a.id = t.account_id
             WHERE a.is_transfer = 0 AND a.is_active = 0
               AND COALESCE(t.category, '') <> '{TRANSFER}'{where}""", args).fetchone()[0]
    finally:
        conn.close()


# ── fixture: one active, one closed, one transfer account ────────────────────

@pytest.fixture
def seeded(empty_db):
    conn = sqlite3.connect(empty_db)
    conn.execute("INSERT INTO cashbook_accounts (id, code, is_active, is_transfer, sort_order) VALUES (1, 'OP', 1, 0, 1)")
    conn.execute("INSERT INTO cashbook_accounts (id, code, is_active, is_transfer, sort_order) VALUES (2, 'RET', 0, 0, 2)")
    conn.execute("INSERT INTO cashbook_accounts (id, code, is_active, is_transfer, sort_order) VALUES (3, 'TR', 1, 1, 3)")
    rows = [
        # 2026-01: OP only
        (1, '2026-01-10', 'income', 'ยอดขายของ', 50.0),
        (1, '2026-01-11', 'expense', 'ค่าไฟ', 30.0),
        # 2026-02: OP + RET (operating and transfer-category) + TR
        (1, '2026-02-10', 'income', 'ยอดขายของ', 100.0),
        (1, '2026-02-11', 'expense', 'ค่าไฟ', 40.0),
        (2, '2026-02-12', 'income', 'ดอกเบี้ยเงินฝาก', 7.0),
        (2, '2026-02-13', 'expense', 'ค่าทำบัญชี', 900.0),
        (2, '2026-02-14', 'expense', TRANSFER, 5000.0),
        (3, '2026-02-15', 'expense', 'ค่าไฟ', 333.0),
        # 2026-03: RET only
        (2, '2026-03-10', 'expense', 'ภาษี/ค่าปรับ', 12.0),
    ]
    conn.executemany("INSERT INTO cashbook_transactions (account_id, txn_date, direction, category, amount)"
                     " VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return empty_db


def test_headline_counts_a_closed_account_and_matches_the_categories(seeded):
    c = _client()
    inc, exp, card3, label, note, _table = _headline(c, '2026-02')
    # CONTROL: the closed account is on the far side of the old filter here.
    assert _inactive_contribution(seeded, '2026-02') == 907.0
    assert (inc, exp) == (107.0, 940.0)            # OP 100/40 + RET 7/900; TR and the transfer row out
    assert (inc, exp) == _category_totals(seeded, '2026-02')
    assert label == 'สุทธิเดือนนี้' and card3 == round(inc - exp, 2)


@pytest.mark.parametrize('month', ['2026-01', '2026-02', '2026-03', 'ทั้งหมด'])
def test_headline_equals_category_totals_in_every_month(seeded, month):
    c = _client()
    inc, exp, card3, label, _note, _table = _headline(c, month)
    assert (inc, exp) == _category_totals(seeded, month), month
    if label == 'สุทธิเดือนนี้':
        assert card3 == round(inc - exp, 2)


def test_the_account_table_stays_active_only_and_the_note_names_the_closed_account(seeded):
    c = _client()
    _i, _e, _c, _l, note_feb, table = _headline(c, '2026-02')
    assert 'OP' in table and 'RET' not in table    # CONTROL + the table is unchanged
    assert note_feb is not None and 'RET' in note_feb
    _i, _e, _c, _l, note_jan, _t = _headline(c, '2026-01')
    assert note_jan is None                        # RET moved no money in January


def test_the_transfer_disclosure_reads_the_same_population(seeded):
    """The ฿ beside 'ไม่รวมหมวดเงินทุน/เงินโอน' is what the headline excludes by
    category — RET's ฿5,000 transfer row is excluded from the totals, so it is
    disclosed there too."""
    c = _client()
    resp = c.get('/cashbook/?month=2026-02')
    t = lxml_html.fromstring(resp.data.decode('utf-8'))
    li = t.xpath("//li[contains(normalize-space(.),'ไม่รวมหมวดเงินทุน/เงินโอน')]//span[@class='fw-semibold'][starts-with(normalize-space(.),'฿')]")
    assert len(li) == 1
    assert _money(li[0].text_content()) == 5000.0


# ── the live-DB clone, post-188 state forced ─────────────────────────────────

def test_headline_equals_category_totals_in_every_month_of_the_live_clone(tmp_db):
    conn = sqlite3.connect(tmp_db)
    # The state migration 188 produces on prod: 904 inactive and not a
    # transfer account. Forced, never inherited — a dev DB may still hold
    # 904 active, where 188 is a no-op.
    conn.execute("UPDATE cashbook_accounts SET is_transfer = 0, is_active = 0 WHERE code = '904'")
    conn.commit()
    months = [r[0] for r in conn.execute(
        "SELECT DISTINCT strftime('%Y-%m', txn_date) FROM cashbook_transactions ORDER BY 1")]
    conn.close()
    assert months, 'the live clone has no cashbook rows at all'
    contributing = [m for m in months if abs(_inactive_contribution(tmp_db, m)) > 0.005]
    if not contributing:
        pytest.skip('no inactive non-transfer account with money in this DB — nothing to guard')

    c = _client()
    for month in months + ['ทั้งหมด']:
        inc, exp, card3, label, note, _t = _headline(c, month)
        assert (inc, exp) == _category_totals(tmp_db, month), month
        if label == 'สุทธิเดือนนี้':
            assert card3 == round(inc - exp, 2), month
    # CONTROL: the months where a closed account moved money were rendered
    # with money on the page, and named in the note.
    for month in contributing:
        inc, exp, _c, _l, note, _t = _headline(c, month)
        assert inc + exp > 0 and note is not None, month
