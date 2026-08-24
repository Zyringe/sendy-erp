"""D2 (projects/products-new-clone-provenance/plan.md): the detail page's
ที่มา badge gets a second prefix branch — created_via='manual_clone_<pid>'
must render 'เพิ่มเอง (คัดลอกจาก #<pid>)', alongside the pre-existing
'smart_mapping_clone_' branch (untouched, D8).

OWNED FIXTURES — deliberately NOT an extension of
tests/test_bp_products_routes.py::test_product_detail_created_via_badge.
That test grabs the first 5 `is_active` rows straight from tmp_db's
INHERITED live-DB-clone data and `pytest.skip()`s if fewer than 5 exist
(Codex R1 #8) — fine for its own pre-existing 3-label + NULL assertions,
but piggybacking a NEW assertion on arbitrary inherited rows would make
this test's outcome depend on live catalog contents, and a skip would
silently stop pinning the new label at all. This file instead DELETEs by
unique test product names then INSERTs its own row for every created_via
shape under test, asserts the row count it expects, and never skips
(tests/conftest.py::tmp_db clones the live dev DB WITH its data — force
every state, inherit none).
"""
import os
import sqlite3

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

_NAME_SOURCE = 'ทดสอบ badge PR5 — clone source — ห้ามลบมือ'
_NAME_MANUAL = 'ทดสอบ badge PR5 — manual — ห้ามลบมือ'
_NAME_MANUAL_CLONE = 'ทดสอบ badge PR5 — manual_clone — ห้ามลบมือ'
_NAME_SMART_MAPPING = 'ทดสอบ badge PR5 — smart_mapping — ห้ามลบมือ'
_NAME_LEGACY = 'ทดสอบ badge PR5 — legacy — ห้ามลบมือ'
_NAME_NULL = 'ทดสอบ badge PR5 — null — ห้ามลบมือ'
_NAME_SMART_MAPPING_CLONE = 'ทดสอบ badge PR5 — smart_mapping_clone — ห้ามลบมือ'

# Review finding 3 / plan.md:158: the OWNED fixture must cover all SIX
# created_via shapes the badge renders — manual / smart_mapping / legacy /
# NULL / smart_mapping_clone_ / manual_clone_ — not just the two new-token
# rows. Before this, deleting the pre-existing smart_mapping_clone_ branch
# from detail.html left this file's own test green; only the inherited,
# skip-if-<5-rows test in test_bp_products_routes.py still covered it.
_ALL_NAMES = [
    _NAME_SOURCE, _NAME_MANUAL, _NAME_MANUAL_CLONE,
    _NAME_SMART_MAPPING, _NAME_LEGACY, _NAME_NULL, _NAME_SMART_MAPPING_CLONE,
]


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


@pytest.fixture
def badge_rows(tmp_db):
    """Owned rows for all SIX created_via shapes the badge renders (plan.md
    :158), keyed by unique test product names — DELETE-then-INSERT, never
    inherited."""
    conn = sqlite3.connect(tmp_db)
    for name in _ALL_NAMES:
        conn.execute("DELETE FROM products WHERE product_name = ?", (name,))

    def _insert(name, created_via):
        conn.execute(
            "INSERT INTO products (product_name, unit_type, is_active, created_via) "
            "VALUES (?, 'ตัว', 1, ?)",
            (name, created_via),
        )
        return conn.execute(
            "SELECT id FROM products WHERE product_name = ?", (name,)
        ).fetchone()[0]

    pid_source = _insert(_NAME_SOURCE, 'manual')
    pid_manual = _insert(_NAME_MANUAL, 'manual')
    pid_manual_clone = _insert(_NAME_MANUAL_CLONE, f'manual_clone_{pid_source}')
    pid_smart_mapping = _insert(_NAME_SMART_MAPPING, 'smart_mapping')
    pid_legacy = _insert(_NAME_LEGACY, 'legacy')
    pid_null = _insert(_NAME_NULL, None)
    pid_smart_mapping_clone = _insert(_NAME_SMART_MAPPING_CLONE, f'smart_mapping_clone_{pid_source}')
    conn.commit()

    # Assert the row COUNT before asserting anything about the rows
    # (verification-discipline.md) — a dedup/insert bug in the fixture
    # itself must fail loudly here, not surface as a confusing mismatch
    # three assertions later in the test body.
    count = conn.execute(
        "SELECT COUNT(*) FROM products WHERE product_name IN ({})".format(
            ','.join('?' * len(_ALL_NAMES))
        ),
        _ALL_NAMES,
    ).fetchone()[0]
    conn.close()
    assert count == len(_ALL_NAMES), (
        f"expected {len(_ALL_NAMES)} owned fixture rows, found {count} — "
        "insert/dedup bug in the fixture itself, not the feature under test"
    )

    return {
        'source': pid_source,
        'manual': pid_manual,
        'manual_clone': pid_manual_clone,
        'smart_mapping': pid_smart_mapping,
        'legacy': pid_legacy,
        'null': pid_null,
        'smart_mapping_clone': pid_smart_mapping_clone,
    }


def test_manual_clone_badge_renders_label_with_source_pid(admin_client, badge_rows):
    """D2: created_via='manual_clone_<pid>' must render
    'เพิ่มเอง (คัดลอกจาก #<pid>)' on the detail page — target the rendered
    ELEMENT, never a bare Thai substring: 'คัดลอกจาก' already appears in
    the create form's own label 'คัดลอกจาก SKU เดิม' AND in the
    'smart_mapping_clone_' branch's label. Includes a CONTROL in the SAME
    test (verification-discipline.md, "A CONTROL in the same test"): a
    plain 'manual' row must render the ordinary 'เพิ่มเอง' badge and must
    NOT show any clone wording — proving the assertion below isn't a false
    match that would fire on every manual row regardless of the new code."""
    # CONTROL first — the plain-manual row renders the *ordinary* label and
    # nothing that looks like a clone.
    control_resp = admin_client.get(f"/products/{badge_rows['manual']}")
    assert control_resp.status_code == 200, control_resp.data[:500]
    control_html = control_resp.data.decode('utf-8')
    assert '>เพิ่มเอง<' in control_html
    assert 'คัดลอกจาก' not in control_html

    resp = admin_client.get(f"/products/{badge_rows['manual_clone']}")
    assert resp.status_code == 200, resp.data[:500]
    html = resp.data.decode('utf-8')
    assert f">เพิ่มเอง (คัดลอกจาก #{badge_rows['source']})<" in html


def test_created_via_badge_covers_the_remaining_four_states(admin_client, badge_rows):
    """Review finding 3 / plan.md:158: the two pre-existing exact-dict
    labels (smart_mapping, legacy), the NULL-renders-nothing case, and the
    pre-existing smart_mapping_clone_ prefix branch must ALSO be owned
    here, not left to the inherited/skippable test in
    test_bp_products_routes.py. Without this, deleting the
    smart_mapping_clone_ branch from detail.html left this file's own test
    green — the only thing that would have caught it was a test that can
    itself be skipped (fewer than 5 active rows in whatever DB CI happens
    to run against)."""
    resp = admin_client.get(f"/products/{badge_rows['smart_mapping']}")
    assert resp.status_code == 200, resp.data[:500]
    assert '>จาก Smart Mapping<' in resp.data.decode('utf-8')

    resp = admin_client.get(f"/products/{badge_rows['legacy']}")
    assert resp.status_code == 200, resp.data[:500]
    assert '>เดิม<' in resp.data.decode('utf-8')

    resp = admin_client.get(f"/products/{badge_rows['null']}")
    assert resp.status_code == 200, resp.data[:500]
    assert 'ที่มา</td>' not in resp.data.decode('utf-8'), "NULL created_via must render no ที่มา row at all"

    resp = admin_client.get(f"/products/{badge_rows['smart_mapping_clone']}")
    assert resp.status_code == 200, resp.data[:500]
    html = resp.data.decode('utf-8')
    assert f">จาก Smart Mapping (คัดลอกจาก #{badge_rows['source']})<" in html
