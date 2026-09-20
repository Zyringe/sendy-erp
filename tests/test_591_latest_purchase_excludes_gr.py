"""#591 — GR (ใบลดหนี้ซื้อ, purchase return) rows must never be picked as the
"latest purchase" a new-SKU cost basis prefills from.

`bsn_suggest._latest_purchase` and the matching `suggestion_cost_basis` window
in `blueprints/bsn.py::mapping` both `ORDER BY date_iso DESC, id DESC LIMIT 1`
over purchase_transactions — before #591 neither excluded GR, so a return
posted after the real purchase would win the window and its unit_price/net
(what came BACK, not what was paid) would prefill `opening_cost`. Both call
sites must exclude GR via the SAME predicate
(`sales_filters.not_a_purchase_return_clause`) or they can pick different rows
and disagree, the way #591's investigation found `_latest_purchase` and the
Tab-2 cost-basis window already do for the SAME bsn_code today (a live prod
mismatch, harmless only because every GR code happens to be mapped already).

tmp_db clones the live dev DB WITH its data, so each fixture forces its own
state for a code that cannot collide with anything real.
"""
import json
import os
import re
import sqlite3

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

_BSN_CODE = 'ZZ591GR'
_BSN_CODE_GR_ONLY = 'ZZ591GRONLY'


def _reset(conn, code):
    conn.execute("DELETE FROM purchase_transactions WHERE bsn_code=?", (code,))
    conn.execute("DELETE FROM pending_product_suggestions WHERE bsn_code=?", (code,))
    conn.execute("DELETE FROM product_code_mapping WHERE bsn_code=?", (code,))


def _purchase(conn, code, doc_base, *, net, qty, unit_price, date_iso, unit='โหล'):
    conn.execute(
        "INSERT INTO purchase_transactions "
        "(bsn_code, doc_no, doc_base, product_name_raw, unit, qty, unit_price, "
        " net, date_iso) VALUES (?,?,?,?,?,?,?,?,?)",
        (code, doc_base, doc_base, 'ของทดสอบ 591', unit, qty, unit_price, net, date_iso))


# ── _latest_purchase itself ──────────────────────────────────────────────────

def test_latest_purchase_skips_a_newer_gr_row(tmp_db_conn):
    """A GR posted AFTER the real purchase must not win the ORDER BY window."""
    conn = tmp_db_conn
    _reset(conn, _BSN_CODE)
    _purchase(conn, _BSN_CODE, 'RR591001', net=1810.0, qty=1, unit_price=1810.0,
              date_iso='2026-06-01')
    # The return: posted LATER, would win a naive "latest" window.
    _purchase(conn, _BSN_CODE, 'GR591002', net=1810.0, qty=1, unit_price=1810.0,
              date_iso='2026-07-01')
    conn.commit()

    import bsn_suggest
    result = bsn_suggest._latest_purchase(conn, _BSN_CODE)

    assert result, 'a real purchase exists; the basis must not come back empty'
    assert result['last_date'] == '2026-06-01', (
        f"must resolve to the real purchase (2026-06-01), not the GR "
        f"(2026-07-01): got {result['last_date']}")


def test_latest_purchase_prefers_an_older_gr_over_nothing(tmp_db_conn):
    """A GR OLDER than the real purchase must not matter either way — this
    pins the exclusion is a WHERE filter, not an accidental ORDER BY tiebreak
    that only happens to work when the return is newer."""
    conn = tmp_db_conn
    _reset(conn, _BSN_CODE)
    _purchase(conn, _BSN_CODE, 'GR591000', net=999.0, qty=1, unit_price=999.0,
              date_iso='2026-01-01')
    _purchase(conn, _BSN_CODE, 'RR591001', net=1810.0, qty=1, unit_price=1810.0,
              date_iso='2026-06-01')
    conn.commit()

    import bsn_suggest
    result = bsn_suggest._latest_purchase(conn, _BSN_CODE)
    assert result['last_date'] == '2026-06-01'
    assert result['line_net'] == pytest.approx(1810.0)


def test_latest_purchase_returns_empty_when_only_a_return_exists(tmp_db_conn):
    """A code with NOTHING but a return has no real cost basis — {} (no
    basis), never the GR's own figures. Regression shape: before #591 this
    returned the GR row instead of the correct 'no basis' signal."""
    conn = tmp_db_conn
    _reset(conn, _BSN_CODE_GR_ONLY)
    _purchase(conn, _BSN_CODE_GR_ONLY, 'GR591099', net=500.0, qty=2, unit_price=250.0,
              date_iso='2026-08-01')
    conn.commit()

    import bsn_suggest
    result = bsn_suggest._latest_purchase(conn, _BSN_CODE_GR_ONLY)
    assert result == {}, f'a GR-only code must have no basis, got {result}'


def test_latest_purchase_finds_a_row_with_null_doc_base(tmp_db_conn):
    """not_a_purchase_return_clause() is NULL-safe on purpose (#626 review):
    a bare `doc_base NOT LIKE 'GR%'` evaluates NULL, not TRUE, for a NULL
    doc_base, so a real purchase whose doc_base happens to be unknown would
    be silently dropped from the window instead of just failing to be
    recognized as GR. Explicit here, seeding doc_base=NULL directly, so a
    future fixture change can't quietly remove this guard the way
    test_bsn_suggest_unit_helpers.py's missing-column fixture already did
    once (caught only by a full-suite run, not by this file's own tests)."""
    conn = tmp_db_conn
    _reset(conn, _BSN_CODE)
    conn.execute(
        "INSERT INTO purchase_transactions "
        "(bsn_code, doc_no, doc_base, product_name_raw, unit, qty, unit_price, "
        " net, date_iso) VALUES (?,?,NULL,?,?,?,?,?,?)",
        (_BSN_CODE, 'RRNULL01', 'ของทดสอบ 591', 'โหล', 1, 1810.0, 1810.0, '2026-06-01'))
    conn.commit()

    import bsn_suggest
    result = bsn_suggest._latest_purchase(conn, _BSN_CODE)
    assert result, ('a real purchase with NULL doc_base must still be found, '
                     f'not silently dropped: got {result}')
    assert result['last_date'] == '2026-06-01'
    assert result['line_net'] == pytest.approx(1810.0)


# ── the matching window in blueprints/bsn.py::mapping ────────────────────────

@pytest.fixture
def staged_client_with_gr(tmp_db):
    """One staged suggestion whose purchase history is a real purchase
    FOLLOWED by a GR — the exact shape that made the pre-#591 window disagree
    with _latest_purchase for the same code."""
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _reset(conn, _BSN_CODE)
    _purchase(conn, _BSN_CODE, 'RR591001', net=1044.55, qty=1, unit_price=1810.0,
              date_iso='2026-06-01')
    _purchase(conn, _BSN_CODE, 'GR591002', net=9999.99, qty=99, unit_price=9999.99,
              date_iso='2026-07-01')
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id, is_ignored)"
        " VALUES (?, 'ของทดสอบ 591', NULL, 0)", (_BSN_CODE,))
    cur = conn.execute(
        "INSERT INTO pending_product_suggestions "
        "(bsn_code, bsn_name, suggested_name, suggested_cost, suggested_unit_type, "
        " bsn_unit, unit_conversion_ratio, status) "
        "VALUES (?,?,?,?,?,?,?,'pending')",
        (_BSN_CODE, 'ของทดสอบ 591', 'ของทดสอบ 591', 87.0458, 'ตัว', 'โหล', 12))
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


def test_mapping_page_cost_basis_skips_the_gr(staged_client_with_gr):
    client, sid = staged_client_with_gr
    html = client.get('/mapping?tab=suggestions').get_data(as_text=True)

    m = re.search(r'const SUG_COST_BASIS = (\{.*?\});', html, re.S)
    assert m, 'SUG_COST_BASIS is not rendered at all'
    basis = json.loads(m.group(1))
    assert str(sid) in basis, f'staged row {sid} has a real purchase; must have a basis'
    # The GR's net/qty (9999.99 / 99) must NOT appear here — only the real
    # purchase (1044.55 / 1) may.
    assert basis[str(sid)]['net'] == pytest.approx(1044.55), (
        f"the window must skip GR591002 (net 9999.99) and resolve to the real "
        f"purchase RR591001 (net 1044.55): got {basis[str(sid)]}")
    assert basis[str(sid)]['qty'] == pytest.approx(1)


def test_mapping_suggest_endpoint_skips_the_gr(staged_client_with_gr):
    """/mapping/suggest/<bsn_code> calls bsn_suggest.suggest_for_bsn directly,
    which embeds _latest_purchase's result as 'latest_purchase' — the same
    exclusion must hold on this second consumer."""
    client, _sid = staged_client_with_gr
    resp = client.get(f'/mapping/suggest/{_BSN_CODE}')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['latest_purchase'], 'a real purchase exists; must not be empty'
    assert data['latest_purchase']['line_net'] == pytest.approx(1044.55), (
        f"expected the real purchase's net, got {data['latest_purchase']}")
