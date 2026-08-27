"""Phase 2d — the product page must stop calling a date-closed promo "ใช้งาน".

`blueprints/products.py::product_detail` used to hand the template the raw
`models.get_promotions()` rows, and `templates/products/detail.html` judged
each one by a bare `is_active`. After 2a a promo can be closed by DATE while
`is_active` is still 1 (`deactivate_promotion` stamps `date_end`, but nothing
flips `is_active` when a window simply expires), so such a row rendered a
green ใช้งาน badge and still offered the ยกเลิก button. Wrong on prod today.

The fix classifies in PYTHON with the shared vocabulary
(`models.classify_promotions` → `is_current`, shipped and tested in #436) and
renders three states: ใช้อยู่ / ตั้งเวลาไว้ / ปิดแล้ว. The predicate is never
re-implemented in Jinja.

⚠ DEVIATION FROM THE PLAN, flagged for Put: the plan said "gate ยกเลิก on
`is_current`". That would remove the only UI path to cancel a SCHEDULED promo
— a row 2a can now create on purpose. This gates on "not closed" instead, so
ยกเลิก stays on current AND scheduled rows and disappears only on closed ones
(which is the actual bug). Both halves are pinned below; flipping to
current-only is a one-line template change plus deleting one assertion.

FIXTURE DISCIPLINE: `tmp_db` clones the live dev DB *with its data*, so every
promo read here is INSERTed by this file after deleting the product's existing
promos, and every count is asserted before any property. The closed and the
current promo share a product on purpose (they are the primary test's own
control, in one render); their windows do NOT overlap, so the fixture stays
well-formed if migration 177's one-per-slot trigger later lands. The scheduled
promo sits on a SECOND product because its open-ended window WOULD overlap the
current one under 177.

Assertions are on the rendered ELEMENT (`>ปิดแล้ว<`) scoped to the promo's own
<tr>, never a bare Thai substring on the whole page.
"""
import sqlite3
from datetime import date, timedelta

import pytest

CLOSED = '2D-CLOSED-BYDATE'
CURRENT = '2D-CURRENT'
SCHEDULED = '2D-SCHEDULED'

TODAY = date.today()
D = lambda n: (TODAY + timedelta(days=n)).isoformat()   # noqa: E731


@pytest.fixture
def admin_client(tmp_db):
    """Authed admin test client. Session injection, NOT a real login: this
    machine's Python has no hashlib.scrypt, so a real HTTP login against a
    scrypt-hashed user throws (CLAUDE.local.md). Same pattern as
    tests/test_nonstock_doc_badge.py."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


@pytest.fixture
def seeded(tmp_db):
    """(pid_a, pid_b, {promo_name: promo_id}) — forced, never inherited."""
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    pids = [r['id'] for r in conn.execute(
        "SELECT id FROM products WHERE is_active = 1 ORDER BY id LIMIT 2").fetchall()]
    assert len(pids) == 2, f"need 2 active products in the clone, got {len(pids)}"
    pid_a, pid_b = pids
    conn.execute("DELETE FROM promotions WHERE product_id IN (?, ?)", (pid_a, pid_b))

    def add(pid, name, start, end, source):
        conn.execute(
            "INSERT INTO promotions (product_id, promo_name, promo_type, "
            "discount_value, date_start, date_end, is_active, source) "
            "VALUES (?, ?, 'percent', 10, ?, ?, 1, ?)",
            (pid, name, start, end, source))
        return conn.execute("SELECT last_insert_rowid() AS id").fetchone()['id']

    ids = {
        # is_active is 1 on ALL THREE — that is the whole point. Only the dates
        # differ, so a bare `is_active` check cannot tell them apart.
        CLOSED:    add(pid_a, CLOSED,    D(-60), D(-1), 'catalog-import'),
        CURRENT:   add(pid_a, CURRENT,   D(-1),  None,  'catalog-import'),
        SCHEDULED: add(pid_b, SCHEDULED, D(30),  None,  'manual'),
    }
    conn.commit()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM promotions WHERE product_id IN (?, ?) "
        "AND is_active = 1", (pid_a, pid_b)).fetchone()['n']
    conn.close()
    assert n == 3, f"fixture must leave exactly 3 active promos, found {n}"
    return pid_a, pid_b, ids


def _promo_section(body: str) -> str:
    """Everything after the promotions card-header icon — keeps the top
    info-card's active-promo badge out of the assertions."""
    parts = body.split('bi-percent')
    assert len(parts) == 2, f"expected 1 'bi-percent' marker, found {len(parts) - 1}"
    return parts[1]


def _row(section: str, promo_name: str) -> str:
    """The single <tr> that carries `promo_name`. The count assertion is the
    control: if the page stops rendering the row at all, this fails loudly
    instead of letting a `not in` assertion pass vacuously."""
    rows = [r for r in section.split('<tr') if promo_name in r]
    assert len(rows) == 1, f"expected exactly 1 row for {promo_name}, got {len(rows)}"
    return rows[0]


def test_date_closed_promo_is_not_labelled_current(admin_client, seeded):
    """THE BUG. date_end yesterday + is_active 1 → ปิดแล้ว, never ใช้อยู่.
    The current promo on the SAME page is the control."""
    pid_a, _, _ = seeded
    r = admin_client.get(f'/products/{pid_a}')
    assert r.status_code == 200
    section = _promo_section(r.data.decode('utf-8'))

    closed_row = _row(section, CLOSED)
    assert '>ปิดแล้ว<' in closed_row, closed_row
    assert '>ใช้อยู่<' not in closed_row, closed_row

    # Control, same render: the check CAN find ใช้อยู่ when it belongs.
    current_row = _row(section, CURRENT)
    assert '>ใช้อยู่<' in current_row, current_row
    assert '>ปิดแล้ว<' not in current_row, current_row


def test_scheduled_promo_shows_its_start_date(admin_client, seeded):
    """date_start in the future → ตั้งเวลาไว้ + the date, not ใช้อยู่."""
    _, pid_b, _ = seeded
    r = admin_client.get(f'/products/{pid_b}')
    assert r.status_code == 200
    row = _row(_promo_section(r.data.decode('utf-8')), SCHEDULED)
    assert '>ตั้งเวลาไว้<' in row, row
    assert '>ใช้อยู่<' not in row, row
    assert D(30) in row, row


def test_cancel_button_gone_on_a_closed_promo_kept_on_a_current_one(admin_client, seeded):
    """ยกเลิก must not be offered on a promo that is already dead by date."""
    pid_a, _, ids = seeded
    r = admin_client.get(f'/products/{pid_a}')
    section = _promo_section(r.data.decode('utf-8'))
    assert f"/promotions/{ids[CLOSED]}/deactivate" not in _row(section, CLOSED)
    # Control: the button still exists where it should.
    assert f"/promotions/{ids[CURRENT]}/deactivate" in _row(section, CURRENT)


def test_cancel_button_kept_on_a_scheduled_promo(admin_client, seeded):
    """⚠ The deviation, pinned. A scheduled promo has not started, but it is a
    live row that will start — cancelling it before then is the whole point of
    2a's `cancel_conflicts` option, and this page is the only UI that offers
    it. Delete this test if Put prefers the plan's current-only gate."""
    _, pid_b, ids = seeded
    r = admin_client.get(f'/products/{pid_b}')
    row = _row(_promo_section(r.data.decode('utf-8')), SCHEDULED)
    assert f"/promotions/{ids[SCHEDULED]}/deactivate" in row, row


def test_source_is_shown_on_a_current_promo(admin_client, seeded):
    """So Put can tell a catalog-import promo from one he typed."""
    pid_a, _, _ = seeded
    r = admin_client.get(f'/products/{pid_a}')
    assert 'จากแค็ตตาล็อก' in _row(_promo_section(r.data.decode('utf-8')), CURRENT)


def test_route_classifies_in_python_not_jinja(admin_client, seeded):
    """The route must hand the template the three classified lists. Pinning the
    seam is what makes the break-it-once cycle (revert to raw get_promotions)
    turn the tests above red rather than merely changing the markup."""
    import flask
    from app import app as flask_app
    pid_a, _, _ = seeded
    captured = {}

    def _grab(sender, template, context, **extra):
        if template.name == 'products/detail.html':
            captured.update(context)

    flask.template_rendered.connect(_grab, flask_app)
    try:
        admin_client.get(f'/products/{pid_a}')
    finally:
        flask.template_rendered.disconnect(_grab, flask_app)

    assert 'promos_current' in captured, sorted(captured)
    assert [p['promo_name'] for p in captured['promos_current']] == [CURRENT]
    assert [p['promo_name'] for p in captured['promos_closed']] == [CLOSED]
    assert captured['promos_scheduled'] == []
