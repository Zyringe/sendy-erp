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

_ALL_NAMES = [_NAME_SOURCE, _NAME_MANUAL, _NAME_MANUAL_CLONE]


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
    """Owned rows for the two created_via shapes this test needs (a plain
    'manual' CONTROL row + a 'manual_clone_<source pid>' row), keyed by
    unique test product names — DELETE-then-INSERT, never inherited."""
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

    return {'source': pid_source, 'manual': pid_manual, 'manual_clone': pid_manual_clone}


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
