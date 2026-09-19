"""#589 — the per-invoice tick-list on /commission/sp/<code> names a `settled`
invoice as settled instead of falling through to "รอจ่าย".

Today the route asks for only_unpaid=True, which drops every settled row
(remaining is forced to 0), so the branch cannot be reached through real data.
The test feeds the template through the real route with the engine call
stubbed, and asserts on each row's own status cell, with a pending row as the
control.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import lxml.html

SETTLED, PENDING = 'IV_SETTLED_589', 'IV_PENDING_589'


def _row(doc, status, remaining):
    return {
        'invoice_no': doc, 'invoice_date': '2026-01-10', 'receipt_date': '2026-04-20',
        'receipt_no': 'RE_' + doc, 'customer_name': 'ลูกค้าทดสอบ 589',
        'own_net': 1000.0, 'third_net': 0.0, 'total_net': 1000.0,
        'commission_due': 100.0, 'paid_amount': 0.0,
        'remaining': remaining, 'paid_status': status,
    }


def _status_badge(html, doc):
    tree = lxml.html.fromstring(html)
    rows = tree.xpath(f"//tr[.//strong[normalize-space()='{doc}']]")
    assert len(rows) == 1, f'{doc} must render exactly one row in the tick-list'
    cell = rows[0].xpath('./td[last()]')[0]
    badges = cell.xpath(".//span[contains(concat(' ', @class, ' '), ' badge ')]")
    assert len(badges) == 1
    return badges[0].text_content().strip()


def test_settled_invoice_shows_settled_badge_not_pending(tmp_db, monkeypatch):
    import commission
    monkeypatch.setattr(commission, 'get_invoice_commission_for_sp',
                        lambda *a, **k: [_row(SETTLED, 'settled', 0.0),
                                         _row(PENDING, 'pending', 100.0)])
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1; s['username'] = 'admin'; s['role'] = 'admin'

    resp = c.get('/commission/sp/03?month=2026-09')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert _status_badge(html, PENDING) == 'รอจ่าย', 'control: a pending row keeps its badge'
    assert _status_badge(html, SETTLED) == 'ปิดยอดแล้ว'
