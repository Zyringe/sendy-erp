"""/products condition badge (build ฉ, 2026-07-22).

'(ไม่สวย)' was stripped from 10 product names on 2026-07-23 — the tag now
lives ONLY in `products.condition`, so the badge restores at-a-glance
visibility on both the desktop table and the mobile card view.

Assertions anchor on the actual badge markup (`badge-warning` span), not
just the condition word appearing anywhere on the page — the same
false-pass trap `test_products_inactive_toggle.py` already documents for
'ปิดใช้งาน' (a filter label can contain the same text as a row badge).
`?q=` narrows each request to one distinctive product so the assertion
doesn't have to scope itself to a single row/card by hand.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest


@pytest.fixture
def admin_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


def _first_condition_product(tmp_db):
    """A real active product carrying a non-null condition, whose NAME text
    does NOT itself contain the condition word — the exact case that would
    otherwise make a naive 'condition text in html' assertion meaningless."""
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT id, product_name, condition FROM products"
        " WHERE is_active=1 AND condition IS NOT NULL AND condition != ''"
        " AND product_name NOT LIKE '%' || condition || '%'"
        " ORDER BY id LIMIT 1").fetchone()
    conn.close()
    if row is None:
        pytest.skip("no active product with a name-clean condition tag in this DB copy")
    return row


def test_get_products_selects_condition_column(tmp_db):
    """models.get_products() must actually select `condition` — the badge
    can't render from a column the query never fetched."""
    import models
    pid, name, condition = _first_condition_product(tmp_db)
    rows, _ = models.get_products(search=name, page=1, per_page=50)
    match = next(r for r in rows if r["id"] == pid)
    assert match["condition"] == condition


def test_condition_badge_renders_on_desktop_and_mobile(admin_client, tmp_db):
    pid, name, condition = _first_condition_product(tmp_db)
    html = admin_client.get(f"/products?q={name}").get_data(as_text=True)
    badge = f'<span class="badge badge-warning ms-1">{condition}</span>'
    assert html.count(badge) == 2  # once in the desktop <tr>, once in the mobile card


def test_condition_badge_absent_for_normal_product(admin_client, tmp_db):
    """A product with NO condition must show zero badge-warning spans on a
    page narrowed to just it — proves the badge is conditional, not always-on."""
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT id, product_name FROM products"
        " WHERE is_active=1 AND (condition IS NULL OR condition='')"
        " ORDER BY id LIMIT 1").fetchone()
    conn.close()
    pid, name = row
    html = admin_client.get(f"/products?q={name}").get_data(as_text=True)
    assert name in html  # sanity: the product itself did render
    assert "badge-warning" not in html
