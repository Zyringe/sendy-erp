"""vat-substitute — real Flask-test-client render checks.

The unit-test suites (test_vat_sub_*.py) exercise the Python business logic
directly and never render Jinja — so a template bug like accessing a dict's
'items' KEY via `.items` (which Jinja resolves to the dict's bound `.items()`
METHOD instead, per its attribute-then-subscript lookup) is invisible to
them. Caught only by an actual GET through the real app during the manual
port-5003 smoke test; these pin it so it can't regress silently again."""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest


def _login(client, role='admin'):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = f'test-{role}'
        sess['role'] = role


@pytest.fixture
def route_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    with flask_app.test_client() as c:
        yield c


def _seed_identity_mapped_product(conn):
    """A product with an xp5 identity mapping — exercises the own-stock
    card + the STKGRP-bridge guess path (the exact page that 500'd)."""
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES ('สินค้าทดสอบ render', 'ตัว')"
    ).lastrowid
    conn.execute(
        "INSERT INTO xp5_product_mapping (xp5_code, product_id, status, match_layer) "
        "VALUES ('RENDER1', ?, 'reviewed', 'manual')", (pid,))
    conn.commit()
    return pid


def test_index_renders(route_client, tmp_db):
    _login(route_client)
    r = route_client.get('/vat-sub')
    assert r.status_code == 200


def test_product_view_renders_with_identity_mapping_and_no_vat_book(route_client, tmp_db):
    """No vat_book.db exists in this tmp env -> own_card/candidates/guesses
    all take their 'book unavailable' branches. Exactly this combination
    (guesses = {'empty': True, 'reason': ...}) is what originally 500'd
    (unrelated bug: 'items' key vs dict.items() method) once the book WAS
    present and guesses.items() no longer took the empty branch."""
    import sqlite3
    import config
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    pid = _seed_identity_mapped_product(conn)
    conn.close()

    _login(route_client)
    r = route_client.get(f'/vat-sub/product/{pid}')
    assert r.status_code == 200
    assert 'สินค้าทดสอบ render' in r.get_data(as_text=True)


def test_product_view_renders_with_real_guess_items(route_client, tmp_db, tmp_path, monkeypatch):
    """Build a real (lightweight) vat_book.db so guesses['items'] is a
    non-empty list — the exact branch that 500'd before the fix (Jinja
    resolved `guesses.items` to the dict's bound .items() method)."""
    import sqlite3
    import config
    import book_registry

    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    pid = _seed_identity_mapped_product(conn)
    conn.close()

    book_path = book_registry.book_db_path('vat')
    bc = sqlite3.connect(book_path)
    bc.executescript("""
        CREATE TABLE products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT NOT NULL,
            unit_type TEXT NOT NULL DEFAULT 'ตัว',
            cost_price REAL NOT NULL DEFAULT 0.0);
        CREATE TABLE product_code_mapping (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bsn_code TEXT NOT NULL, bsn_name TEXT NOT NULL, product_id INTEGER,
            bsn_unit TEXT NOT NULL DEFAULT '');
        CREATE TABLE stock_levels (
            product_id INTEGER PRIMARY KEY, quantity REAL NOT NULL DEFAULT 0);
        CREATE TABLE stmas_meta (
            stkcod TEXT PRIMARY KEY, stkgrp TEXT NOT NULL, vatcod TEXT NOT NULL);
        CREATE TABLE book_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    own_pid = bc.execute("INSERT INTO products (product_name) VALUES ('ของตัวเอง')").lastrowid
    bc.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id) "
              "VALUES ('RENDER1', 'ของตัวเอง', ?)", (own_pid,))
    bc.execute("INSERT INTO stock_levels (product_id, quantity) VALUES (?, 0)", (own_pid,))
    bc.execute("INSERT INTO stmas_meta (stkcod, stkgrp, vatcod) VALUES ('RENDER1', '57', '1')")
    guess_pid = bc.execute("INSERT INTO products (product_name) VALUES ('ตัวแทนเดา')").lastrowid
    bc.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id) "
              "VALUES ('GUESS1', 'ตัวแทนเดา', ?)", (guess_pid,))
    bc.execute("INSERT INTO stock_levels (product_id, quantity) VALUES (?, 3)", (guess_pid,))
    bc.execute("INSERT INTO stmas_meta (stkcod, stkgrp, vatcod) VALUES ('GUESS1', '57', '1')")
    bc.execute("INSERT INTO book_meta VALUES ('built_at', '2026-08-05T00:00:00')")
    bc.commit()
    bc.close()

    _login(route_client)
    r = route_client.get(f'/vat-sub/product/{pid}')
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'GUESS1' in html
    assert own_pid or guess_pid  # keep linters quiet about unused


def test_planning_renders(route_client, tmp_db):
    _login(route_client)
    r = route_client.get('/vat-sub/planning')
    assert r.status_code == 200


def test_group_detail_renders(route_client, tmp_db):
    import sqlite3
    import config
    conn = sqlite3.connect(config.DATABASE_PATH)
    gid = conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('กลุ่มทดสอบ render')").lastrowid
    conn.commit()
    conn.close()

    _login(route_client)
    r = route_client.get(f'/vat-sub/group/{gid}')
    assert r.status_code == 200
    assert 'กลุ่มทดสอบ render' in r.get_data(as_text=True)


def test_group_detail_renders_move_member_form_when_another_group_exists(route_client, tmp_db):
    """The move-member route (models.move_member) has a UI trigger — a
    group page with >=1 other group and a member must render a form
    posting to vat_sub.group_move_member (caught missing during the manual
    port-5003 smoke test: the route existed but no template linked to it)."""
    import sqlite3
    import config
    conn = sqlite3.connect(config.DATABASE_PATH)
    g1 = conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('กลุ่ม 1')").lastrowid
    conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('กลุ่ม 2')")
    conn.execute(
        "INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, 'MV1', 'manual')", (g1,))
    conn.commit()
    conn.close()

    _login(route_client)
    r = route_client.get(f'/vat-sub/group/{g1}')
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert f'action="/vat-sub/group/{g1}/move-member"' in html
    assert 'กลุ่ม 2' in html


def test_staff_cannot_post_promote(route_client, tmp_db):
    _login(route_client, role='staff')
    r = route_client.post('/vat-sub/promote', data={'product_id': '1', 'xp5_code': 'X1'})
    # access_control redirects (not 403) for a POST outside the role whitelist
    assert r.status_code in (302, 403)


def test_badge_js_compares_base_unit_not_selected_unit(route_client, tmp_db):
    """Codex r1 finding 1 (decision 10): the price is divided back to X's
    BASE unit before comparing, so the unit-compatibility check must use
    products.unit_type — the SELECTED deal unit only supplies the ratio.
    Selecting โหล (ratio 12) against a ตัว candidate must NOT flip the badge
    to "เทียบไม่ได้"."""
    import sqlite3
    import config
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    pid = _seed_identity_mapped_product(conn)
    conn.close()
    _login(route_client)
    html = route_client.get(f'/vat-sub/product/{pid}').get_data(as_text=True)
    # tojson escapes Thai to \uXXXX (Flask ensure_ascii) — assert the WIRING:
    # a base-unit constant exists, feeds computeBadge, and the old
    # selected-unit variable is gone entirely.
    assert 'const X_BASE_UNIT = ' in html
    assert 'computeBadge(price, ratio, X_BASE_UNIT,' in html
    assert 'opt.value' not in html


def test_product_view_renders_200_when_book_predates_stmas_meta(route_client, tmp_db, tmp_path, monkeypatch):
    """A vat_book built by the pre-#368 builder has no stmas_meta table —
    exactly prod's state between the merge and the next team upload. Every
    read path LEFT JOINs stmas_meta, so such a book must be treated as
    NOT READY (open_vat_book -> None => graceful 'ยังไม่ถูกสร้าง/ยังไม่พร้อม'
    states), never a 500 on a nav-reachable page."""
    import sqlite3
    import config
    import models.vat_sub as vs
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    pid = _seed_identity_mapped_product(conn)
    conn.close()

    old_book = tmp_path / 'old_book.db'
    c = sqlite3.connect(old_book)
    c.executescript("""
        CREATE TABLE products (id INTEGER PRIMARY KEY, product_name TEXT, unit_type TEXT, cost_price REAL);
        CREATE TABLE product_code_mapping (id INTEGER PRIMARY KEY, bsn_code TEXT, bsn_name TEXT, product_id INTEGER);
        CREATE TABLE stock_levels (product_id INTEGER PRIMARY KEY, quantity REAL);
    """)
    c.close()
    monkeypatch.setattr(vs.book_registry, 'book_db_path', lambda kind: str(old_book))

    _login(route_client)
    r = route_client.get(f'/vat-sub/product/{pid}')
    assert r.status_code == 200
