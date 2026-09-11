"""A contact edit made on the call card must survive the next customer import.

`import_customers_from_bsn` (what POST /customers/import-bsn runs) rewrites
`phone` / `contact` from the Express file on every row whose
`contact_normalized_at` is NULL. The customer page's edit modal stamps that
column when a contact field changes, which is what protects a hand edit. The
call card's "บันทึกข้อมูลติดต่อ" form wrote the same fields without stamping,
so the next import put Express's old values back and nothing said so.

Observed through the rendered call card, the way a person reads it: the
header's `ผู้ติดต่อ:` line. The same page also echoes the value inside the edit
form's <input name="contact" value=...>, so every assertion is scoped to the
header's `<div class="who">` element.
"""
import os
import re
os.environ.setdefault('SKIP_DB_INIT', '1')

CODE = 'CC-S1-TEST'
NAME = 'ร้านทดสอบ call card'
OLD_PHONE = '038-111111'
OLD_CONTACT = 'คุณเดิม'
NEW_CONTACT = 'พี่ทดสอบ (จัดซื้อ) ไลน์ได้'

_WHO = re.compile(r'<div class="who">(.*?)</div>', re.S)
_CONTACT = re.compile(r'ผู้ติดต่อ:\s*(.*?)\s*&nbsp;')


def _client():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 4
        sess['username'] = 'sanchai'
        sess['role'] = 'staff'
    return c


def _seed(conn):
    """A customer Express knows, never hand-edited (contact_normalized_at NULL),
    with one sale so /call/<code> resolves the real master row."""
    conn.execute("DELETE FROM customers WHERE code = ?", (CODE,))
    conn.execute("DELETE FROM sales_transactions WHERE customer_code = ?", (CODE,))
    conn.execute(
        "INSERT INTO customers (code, name, zone, customer_type, credit_days,"
        " phone, contact, contact_normalized_at) VALUES (?,?,?,?,?,?,?,NULL)",
        (CODE, NAME, 'ตอ', '01', 30, OLD_PHONE, OLD_CONTACT))
    conn.execute(
        "INSERT INTO sales_transactions (date_iso, doc_no, customer, customer_code,"
        " qty, unit, unit_price, net) VALUES ('2026-09-01','IV-S1-TEST',?,?,1,'ตัว',100,100)",
        (NAME, CODE))
    conn.commit()


def _express_row():
    """The row as the next Express customer file still carries it."""
    return {'code': CODE, 'name': NAME, 'salesperson': None, 'zone': 'ตอ',
            'customer_type': '01', 'credit_days': 30, 'tax_id': '',
            'phone': OLD_PHONE, 'contact': OLD_CONTACT, 'address': '', 'fax': ''}


def _contact_line(client):
    r = client.get(f'/call/{CODE}')
    assert r.status_code == 200, r.status_code
    html = r.get_data(as_text=True)
    who = _WHO.findall(html)
    assert len(who) == 1, f'expected one header .who block, found {len(who)}'
    m = _CONTACT.search(who[0])
    return m.group(1) if m else None


def test_call_card_contact_edit_survives_customer_import(empty_db, empty_db_conn):
    import models

    _seed(empty_db_conn)
    c = _client()

    r = c.post(f'/call/{CODE}/contact', data={
        'nickname': '', 'phone': OLD_PHONE, 'fax': '',
        'contact': NEW_CONTACT, 'address': '', 'contact_note': '',
    })
    assert r.status_code in (302, 303)

    # CONTROL: the edit landed, and the page resolved the real master row (a
    # synthetic master for an unresolved key prints no ผู้ติดต่อ line at all).
    assert _contact_line(c) == NEW_CONTACT

    models.import_customers_from_bsn([_express_row()])

    after = _contact_line(c)
    assert after == NEW_CONTACT, (
        f'the customer import reverted the call-card edit: header now says {after!r}')
