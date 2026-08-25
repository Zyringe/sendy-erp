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
