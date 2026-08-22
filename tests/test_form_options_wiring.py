"""The CALL SITES actually use form_options — not just that the module works.

`tests/test_form_options.py` exercises the builders in isolation, which leaves the
wiring unpinned: flipping `drop_dated` in `bsn.py::mapping()`, or dropping
`packaging_options` from the render context, keeps every one of those tests green.
The pre-existing `test_mapping_category_picker.py` only asserts the option-group
KEYS are present (`'conditions:' in html`) — a substring check that cannot see a
wrong VALUE.

So each test here compares what a page RENDERS against what `form_options` says it
should, and FORCES the DB state it depends on (a dated condition must exist) rather
than inheriting whatever the cloned dev DB happens to hold — `tmp_db` copies the
live DB with its data.
"""
import json
import os
import re
import sqlite3

import pytest

os.environ.setdefault('SKIP_DB_INIT', '1')

DATED = 'EXP:07/2031'          # forced by the fixture; far-future so it is unmistakably ours
UNDATED_EXTRA = 'ZZไม่มีในลิสต์'  # forced non-canonical, non-dated extra


@pytest.fixture
def seeded_client(tmp_db):
    """A manager client whose DB is guaranteed to contain one dated and one
    undated non-canonical condition, plus the rows that make both /mapping
    forms render."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("DELETE FROM product_code_mapping WHERE bsn_code = 'ZZWIRE01'")
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id, is_ignored) "
        "VALUES ('ZZWIRE01', 'wiring render test', NULL, 0)"
    )
    for sku, cond in (('ZZ-WIRE-DATED', DATED), ('ZZ-WIRE-PLAIN', UNDATED_EXTRA)):
        conn.execute("DELETE FROM products WHERE sku_code = ?", (sku,))
        conn.execute(
            "INSERT INTO products (product_name, sku_code, condition, is_active) "
            "VALUES (?, ?, ?, 1)", (f'wiring fixture {sku}', sku, cond)
        )
    conn.commit()
    conn.close()

    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-manager'
        sess['role'] = 'manager'
    return c


def _combo_opts(html, key):
    """Pull one option group's canonical values out of the page's COMBO_OPTS."""
    m = re.search(r'\b%s:\s*\[(.*?)\],?\s*\n' % re.escape(key), html, re.S)
    assert m, f"COMBO_OPTS group {key!r} not found in the rendered page"
    return [json.loads(v) for v in re.findall(r'v:("(?:[^"\\]|\\.)*")', m.group(1))]


def test_mapping_renders_the_create_side_condition_list(seeded_client):
    """/mapping must render conditions(drop_dated=True): the forced undated extra
    is offered, the forced dated one is not."""
    rendered = _combo_opts(seeded_client.get('/mapping').get_data(as_text=True),
                           'conditions')
    # CONTROL: the non-dated extra proves the DB-extras path ran at all. Without
    # it, "DATED not in rendered" would also pass if extras were dropped wholesale.
    assert UNDATED_EXTRA in rendered, \
        "DB-sourced extras are missing entirely — the list is not coming from form_options"
    assert DATED not in rendered, \
        "/mapping is offering a dated EXP condition — drop_dated is not wired through"


def test_naming_keeps_the_dated_condition_mapping_drops(seeded_client):
    """/naming is an EDIT form: an unofferable stored value gets blanked on save
    (the 44-row incident of 2026-08-14), so it must keep what /mapping drops."""
    html = seeded_client.get('/naming').get_data(as_text=True)
    assert UNDATED_EXTRA in html, "control: /naming is not rendering DB extras at all"
    assert DATED in html, \
        "/naming dropped a dated condition — saving such a product would blank it"


def test_mapping_packaging_comes_from_the_route_not_a_template_literal(seeded_client):
    """The 11 packaging values used to be a hardcoded Jinja list in mapping.html."""
    import form_options
    rendered = _combo_opts(seeded_client.get('/mapping').get_data(as_text=True),
                           'packaging')
    assert len(rendered) == 11, rendered   # control: the group parsed at all
    assert rendered == form_options.packaging()
