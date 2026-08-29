"""Tab-2 (manager review) must show the cost that will actually be written.

The server re-derives cost when the ratio moves
(`models.suggestions._cost_for_saved_ratio`) — that is the authority, and it
has its own tests. This file covers the VISIBLE half: if the form kept showing
a cost computed against the old ratio while the server wrote a different one,
the manager would approve a number they never saw.

Two traps this file is written against, both from
`.claude/rules/verification-discipline.md`:

  * a substring found in rendered HTML may sit inside a JS comment, so the
    whole feature can be dead with every test green. The script body is
    stripped of comments before the function assertions, with a CONTROL symbol
    that must survive the strip.
  * `SUG_COST_BASIS` could render as an empty object and every "is it there"
    assertion would still pass. The count and the actual net/qty values are
    asserted, not just the presence of the name.
"""
import json
import os
import re
import sqlite3

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest


_BSN_CODE = 'ZZTAB2COST'


@pytest.fixture
def staged_client(tmp_db):
    """One staged suggestion whose BSN code HAS a purchase line, so the basis
    is non-empty. Forces its own state — tmp_db clones the live dev DB with its
    data, so nothing here may be inherited."""
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    conn.execute("DELETE FROM pending_product_suggestions WHERE bsn_code=?", (_BSN_CODE,))
    conn.execute("DELETE FROM purchase_transactions WHERE bsn_code=?", (_BSN_CODE,))
    conn.execute(
        "INSERT INTO purchase_transactions "
        "(bsn_code, product_name_raw, unit, qty, unit_price, net, date_iso, doc_no) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (_BSN_CODE, 'ของทดสอบ', 'โหล', 1, 1810.0, 1044.55, '2026-08-15', 'RR-T2'))
    # Two of the assertions below read the tab-1 Suggest modal, which only
    # renders when a code is pending. That used to come free from whatever the
    # cloned dev DB happened to hold; /mapping now drops placeholders whose
    # bills were edited away at source, so the fixture forces its own -- which
    # is what this docstring asked for in the first place.
    conn.execute("DELETE FROM product_code_mapping WHERE bsn_code=?", (_BSN_CODE,))
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id, is_ignored)"
        " VALUES (?, 'ของทดสอบ', NULL, 0)", (_BSN_CODE,))
    cur = conn.execute(
        "INSERT INTO pending_product_suggestions "
        "(bsn_code, bsn_name, suggested_name, suggested_cost, suggested_unit_type, "
        " bsn_unit, unit_conversion_ratio, status) "
        "VALUES (?,?,?,?,?,?,?,'pending')",
        (_BSN_CODE, 'ของทดสอบ', 'ของทดสอบ', 87.0458, 'ตัว', 'โหล', 12))
    sid = cur.lastrowid
    conn.commit()
    conn.close()

    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-manager'
        sess['role'] = 'manager'
    return c, sid


def _script_without_comments(html):
    """Strip JS line and block comments so an assertion cannot pass on code
    that is commented out. Returns (stripped, had_unterminated_block)."""
    body = '\n'.join(re.findall(r'<script[^>]*>(.*?)</script>', html, re.S))
    body = re.sub(r'/\*.*?\*/', '', body, flags=re.S)
    body = re.sub(r'(?m)^\s*//.*$', '', body)
    return body


def test_cost_basis_is_rendered_with_real_numbers(staged_client):
    client, sid = staged_client
    html = client.get('/mapping?tab=suggestions').get_data(as_text=True)

    m = re.search(r'const SUG_COST_BASIS = (\{.*?\});', html, re.S)
    assert m, 'SUG_COST_BASIS is not rendered at all'
    basis = json.loads(m.group(1))
    assert str(sid) in basis, (
        f'the staged row {sid} has a purchase line, so it must have a basis; '
        f'got keys {list(basis)[:5]}'
    )
    # the VALUES, not just the key — an empty/zeroed entry would make
    # sugRatioChanged() a no-op while every presence check still passed
    assert basis[str(sid)]['net'] == pytest.approx(1044.55)
    assert basis[str(sid)]['qty'] == pytest.approx(1)


def test_ratio_recompute_function_is_live_code_not_a_comment(staged_client):
    client, sid = staged_client
    html = client.get('/mapping?tab=suggestions').get_data(as_text=True)
    stripped = _script_without_comments(html)

    # CONTROL: a symbol that must survive the strip. If the helper ate the
    # whole script, this fails instead of the assertions below passing/failing
    # for the wrong reason.
    assert 'function approveSuggestion(' in stripped, \
        'the comment stripper removed live code — the rest of this test is void'
    assert '/*' not in stripped, \
        'an unterminated block comment would make every later assertion vacuous'

    assert 'function sugRatioChanged(' in stripped
    assert 'SUG_COST_BASIS' in stripped
    # the ratio input for THIS row exists, so the listener has something to fire on
    assert f'id="sug-ratio-{sid}"' in html
    assert f'id="sug-cost-{sid}"' in html


def test_open_suggest_clears_the_cost_and_ratio_boxes(staged_client):
    """A cost left over from a previously-reviewed BSN code must not be
    submittable as the next code's cost_price/opening_cost.

    recomputeCostFromPurchase() returns early WITHOUT touching the box when
    there is no purchase line or no ratio yet, so the reset block is the only
    thing standing between "review code A, then open code B" and A's number
    being saved as B's cost. sm-ratio was never reset either (pre-existing) and
    now feeds the derivation, so a stale ratio yields a plausible wrong number
    rather than an obviously wrong one. Found by peer review 2026-08-25.
    """
    client, _sid = staged_client
    html = client.get('/mapping').get_data(as_text=True)
    stripped = _script_without_comments(html)

    # CONTROL: the function that must contain the reset actually survived the
    # comment strip — otherwise the two assertions below are void.
    assert 'function openSuggest(' in stripped, \
        'openSuggest was stripped away; this test cannot fail as written'

    start = stripped.index('function openSuggest(')
    body = stripped[start:start + 3000]
    assert "getElementById('sm-cost').value = ''" in body, \
        'openSuggest must clear the cost box on every open'
    assert "getElementById('sm-ratio').value = ''" in body, \
        'openSuggest must clear the ratio box — it feeds the cost derivation'


def test_new_brand_short_code_is_required_before_submitting(staged_client):
    """A blank short_code reproduces the SONAX defect (no brand segment in any
    sku_code of that brand), and a purely-Thai brand name gets NO auto-proposal
    — exactly the case that must be typed rather than waved through."""
    client, _sid = staged_client
    html = client.get('/mapping').get_data(as_text=True)
    stripped = _script_without_comments(html)
    assert 'function newBrandShortCodeMissing(' in stripped
    # both submit paths must consult it, not just one
    for fn in ('function confirmStageNew(', 'function confirmCreateNow('):
        start = stripped.index(fn)
        assert 'newBrandShortCodeMissing()' in stripped[start:start + 700], \
            f'{fn} does not check for a blank short_code'
