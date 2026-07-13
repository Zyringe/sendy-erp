"""TDD for the /marketplace/review dismiss ("รับทราบ — ไม่มีใบกำกับ") flow.

A bucket-D order whose sale was verified never-keyed in Express can be
acknowledged so the worklist stops nagging: it leaves rows_d, appears in the
auditable rows_dismissed list, and the acknowledgement is reversible (undo).
Table: marketplace_review_dismissals (mig 135). Routes are manager-gated.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest


@pytest.fixture
def d_order(tmp_db_conn):
    """One bucket-D order (settled + mapped item, no IV link) under a unique
    order_sn so assertions never collide with cloned live data."""
    c = tmp_db_conn
    c.execute("DELETE FROM marketplace_orders WHERE order_sn = 'DISMISS1'")
    c.execute("DELETE FROM marketplace_review_dismissals WHERE order_sn = 'DISMISS1'")
    px = c.execute("INSERT INTO products (product_name) VALUES ('Dismiss Test Product')").lastrowid
    cur = c.execute(
        """INSERT INTO marketplace_orders
           (platform, order_sn, status, order_date, actual_payout, settled_at, currency)
           VALUES ('shopee', 'DISMISS1', 'สำเร็จแล้ว', '2026-05-10 10:00', 42.0, '2026-05-11', 'THB')""")
    oid = cur.lastrowid
    c.execute(
        """INSERT INTO marketplace_order_items
           (order_id, platform, order_sn, line_key, item_name, qty, internal_product_id)
           VALUES (?, 'shopee', 'DISMISS1', 'L1', 'สินค้าทดสอบรับทราบ', 1, ?)""",
        (oid, px))
    c.commit()
    return c, oid


def _client(role='manager'):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 4
        sess['username'] = 'tester'
        sess['role'] = role
    return c


def _rows_d_sns(conn):
    from models.marketplace import get_iv_match_worklist
    w = get_iv_match_worklist(conn, 'shopee')
    return {r['order_sn'] for r in w['rows_d']}, w


def test_worklist_excludes_dismissed_and_lists_them(d_order):
    conn, oid = d_order
    from models.marketplace import dismiss_review_order, undismiss_review_order

    sns, _ = _rows_d_sns(conn)
    assert 'DISMISS1' in sns  # baseline: it IS a bucket-D row

    assert dismiss_review_order(conn, oid, reason='ทีมไม่ได้คีย์', by='tester') == 'DISMISS1'
    sns, w = _rows_d_sns(conn)
    assert 'DISMISS1' not in sns
    dis = {r['order_sn']: r for r in w['rows_dismissed']}
    assert 'DISMISS1' in dis
    assert dis['DISMISS1']['reason'] == 'ทีมไม่ได้คีย์'
    assert w['count_dismissed'] >= 1
    # dismissed rows never count toward the B/C/D attention badge
    assert all(r['order_sn'] != 'DISMISS1' for r in w['rows_b'] + w['rows_c'])

    assert undismiss_review_order(conn, oid) == 1
    sns, w = _rows_d_sns(conn)
    assert 'DISMISS1' in sns
    assert all(r['order_sn'] != 'DISMISS1' for r in w['rows_dismissed'])


def test_dismiss_is_idempotent(d_order):
    conn, oid = d_order
    from models.marketplace import dismiss_review_order
    dismiss_review_order(conn, oid, reason='x', by='t')
    dismiss_review_order(conn, oid, reason='x', by='t')  # second call: no crash, no dup
    n = conn.execute(
        "SELECT COUNT(*) FROM marketplace_review_dismissals WHERE order_sn='DISMISS1'"
    ).fetchone()[0]
    assert n == 1


def test_dismiss_route_roundtrip(d_order):
    _conn, oid = d_order
    from database import get_connection
    client = _client('manager')

    resp = client.post(f'/marketplace/order/{oid}/review-dismiss',
                       data={'action': 'dismiss', 'reason': 'ทีมไม่ได้คีย์'},
                       follow_redirects=False)
    assert resp.status_code == 302
    c = get_connection()
    row = c.execute(
        "SELECT reason, dismissed_by FROM marketplace_review_dismissals "
        "WHERE platform='shopee' AND order_sn='DISMISS1'").fetchone()
    assert row is not None and row['reason'] == 'ทีมไม่ได้คีย์' and row['dismissed_by'] == 'tester'

    # page shows the acknowledged section with the row + undo control
    html = client.get('/marketplace/review?platform=shopee').get_data(as_text=True)
    assert 'รับทราบแล้ว' in html and 'DISMISS1' in html

    resp = client.post(f'/marketplace/order/{oid}/review-dismiss',
                       data={'action': 'undo'}, follow_redirects=False)
    assert resp.status_code == 302
    n = c.execute(
        "SELECT COUNT(*) FROM marketplace_review_dismissals WHERE order_sn='DISMISS1'"
    ).fetchone()[0]
    c.close()
    assert n == 0


def test_dismiss_route_blocks_staff(d_order):
    _conn, oid = d_order
    resp = _client('staff').post(f'/marketplace/order/{oid}/review-dismiss',
                                 data={'action': 'dismiss'}, follow_redirects=False)
    assert resp.status_code in (302, 403)
    from database import get_connection
    c = get_connection()
    n = c.execute(
        "SELECT COUNT(*) FROM marketplace_review_dismissals WHERE order_sn='DISMISS1'"
    ).fetchone()[0]
    c.close()
    assert n == 0
