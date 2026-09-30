"""TikTok orders -> their Express IV (PR-3 of the TikTok order import).

Plan: projects/tiktok-order-import/tiktok-order-import-plan.md, PR-3.
- Customer code Tหน้าร้าน; billed basis = what the buyer paid = o.item_total
  (Put Q2: the team keys the IV at the buyer-paid amount, 405 not 435 or 309.05).
- The three basis CASE copies each get a tiktok branch; they are NOT unified
  (they differ for Shopee on purpose).
- Automatch runs after the income file, never after the order file (Shopee and
  Lazada behave the same). Never reconcile, never cashbook.
- The settlement page stays Shopee/Lazada: it is built on bank deposits.

Fixtures: the real order CSV and income file of 2026-09-30 on empty_db with
migration 197, the 4 TikTok SKUs mapped to products, and two Express IVs keyed
the way prod has them: IV6901503 (฿405, order …379) and IV6901501 (฿119, …543).
"""
import io
import os
import re

import pytest

os.environ.setdefault('SKIP_DB_INIT', '1')

import marketplace_match
import marketplace_reconcile
import models
import models.marketplace as mm
import parse_tiktok_income as pti
from marketplace_files import load_order_export
from models.marketplace import import_marketplace_orders

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
APP = os.path.join(REPO, 'inventory_app')
FIX = os.path.join(REPO, 'tests', 'fixtures', 'tiktok')
ORDERS_CSV = os.path.join(FIX, 'tiktok_orders_sample.csv')
INCOME_XLSX = os.path.join(FIX, 'tiktok_income_sample.xlsx')
MIG_197 = os.path.join(REPO, 'data', 'migrations', '197_marketplace_orders_tiktok.sql')

O379, O543, O817 = '585884671861360379', '585884215723460543', '585883444661159817'
SKU_PID = {'1737136796219442872': 9401, '1737136999694436024': 9402,
           '1736984084285458104': 9403, '1736984084285654712': 9404}


def _read(p):
    with open(p, 'rb') as f:
        return f.read()


def _add_iv(c, doc_base, net, date_iso, product_id, customer_code='Tหน้าร้าน'):
    c.execute(
        """INSERT INTO sales_transactions
           (date_iso, doc_no, doc_base, customer, customer_code, qty, unit_price,
            vat_type, total, net, product_id, created_at, synced_to_stock)
           VALUES (?,?,?,?,?,1,?,1,?,?,?, '2026-09-05 00:00:00', 1)""",
        (date_iso, f'{doc_base}-1', doc_base, 'หน้าร้านT', customer_code,
         net, net, net, product_id))
    c.commit()


def _import_income(c):
    p = pti.parse_tiktok_income(*pti.load_tiktok_income(io.BytesIO(_read(INCOME_XLSX))))
    models.upsert_marketplace_settlements(c, p['settlements'], 'income.xlsx', platform='tiktok')
    models.upsert_marketplace_fees(c, p['fee_rows'], 'income.xlsx', platform='tiktok')


@pytest.fixture
def conn(empty_db_conn):
    c = empty_db_conn
    with open(MIG_197, encoding='utf-8') as f:
        c.executescript(f.read())
    for vid, pid in SKU_PID.items():
        c.execute("INSERT INTO products (id, product_name) VALUES (?, ?)", (pid, f'p{pid}'))
        c.execute("INSERT INTO platform_skus (platform, product_name, variation_id, stock, "
                  "internal_product_id) VALUES ('tiktok', 'x', ?, 5, ?)", (vid, pid))
    c.commit()
    import_marketplace_orders(c, load_order_export(_read(ORDERS_CSV))[1], 'orders.csv')
    _import_income(c)
    _add_iv(c, 'IV6901503', 405.0, '2026-09-04', 9401)
    _add_iv(c, 'IV6901501', 119.0, '2026-09-04', 9402)
    return c


def _links(c, platform='tiktok'):
    return [tuple(r) for r in c.execute(
        "SELECT order_sn, doc_base, customer_code, match_method, confidence "
        "FROM marketplace_order_invoice WHERE platform=? ORDER BY order_sn", (platform,))]


def _oid(c, sn):
    return c.execute("SELECT id FROM marketplace_orders WHERE platform='tiktok' AND order_sn=?",
                     (sn,)).fetchone()[0]


# ── automatch oracle ─────────────────────────────────────────────────────────

def test_automatch_links_the_two_sales_and_leaves_the_cancel(conn):
    r = marketplace_match.run_automatch(conn, 'tiktok')
    assert _links(conn) == [
        (O543, 'IV6901501', 'Tหน้าร้าน', 'auto', 'confident'),
        (O379, 'IV6901503', 'Tหน้าร้าน', 'auto', 'confident'),
    ]
    assert (r['confident'], r['review'], r['returns_matched']) == (2, 0, 0)
    # O817 is cancelled and settled at ฿0: not a returns-pass candidate at all.
    assert marketplace_match._settled_cancel_return_orders(conn, 'tiktok') == []


def test_automatch_rerun_is_idempotent(conn):
    marketplace_match.run_automatch(conn, 'tiktok')
    first = _links(conn)
    marketplace_match.run_automatch(conn, 'tiktok')
    assert _links(conn) == first and len(first) == 2


def test_automatch_breaks_a_tie_on_what_the_buyer_paid(conn):
    """Two same-product IVs on the same day: ฿405 (buyer paid) and ฿309.05 (the
    payout). The basis decides, and TikTok's basis is item_total."""
    _add_iv(conn, 'IV6901599', 309.05, '2026-09-04', 9401)
    marketplace_match.run_automatch(conn, 'tiktok')
    assert dict((sn, doc) for sn, doc, *_ in _links(conn))[O379] == 'IV6901503'


def test_completed_tiktok_status_is_matchable_even_unsettled():
    assert marketplace_match._is_matchable_status('เสร็จสมบูรณ์', settled=False) is True
    assert marketplace_match._is_matchable_status('ยกเลิกแล้ว', settled=True) is False


# ── the three basis copies ───────────────────────────────────────────────────

_BASIS_CASE = re.compile(r"CASE\s+WHEN\s+(\w+)\.platform\s*=\s*'lazada'(.*?)\bEND\b", re.S)


def _basis_cases():
    hits = []
    for root, _, files in os.walk(APP):
        for fn in files:
            if fn.endswith('.py'):
                p = os.path.join(root, fn)
                with open(p, encoding='utf-8') as f:
                    for m in _BASIS_CASE.finditer(f.read()):
                        hits.append((os.path.relpath(p, APP), m.group(0)))
    return hits


def test_every_billed_basis_case_has_a_tiktok_branch():
    hits = _basis_cases()
    # Control: the three known copies, and no fourth appeared unnoticed.
    assert sorted(f for f, _ in hits) == ['marketplace_match.py', 'models/marketplace.py',
                                          'models/marketplace.py']
    for f, case in hits:
        assert re.search(r"WHEN\s+\w+\.platform\s*=\s*'tiktok'\s+THEN\s+\w+\.item_total\b", case), (f, case)


def test_the_three_copies_agree_for_a_tiktok_order(conn):
    oid = _oid(conn, O379)
    matchable = {o['order_sn']: o['billed_basis'] for o in marketplace_match._matchable_orders(conn, 'tiktok')}
    picker = models.get_marketplace_order(conn, oid)['billed_basis']
    recon = conn.execute(
        f"SELECT {mm._BILLED_BASIS_SQL} FROM marketplace_orders mo "
        "LEFT JOIN marketplace_order_fees f ON f.platform=mo.platform AND f.order_sn=mo.order_sn "
        "WHERE mo.id=?", (oid,)).fetchone()[0]
    # Control: the payout differs, so a copy that fell to actual_payout would show.
    assert conn.execute("SELECT actual_payout FROM marketplace_orders WHERE id=?", (oid,)).fetchone()[0] == 309.05
    assert matchable[O379] == picker == recon == 405.0


# ── picker, manual pick, worklist ────────────────────────────────────────────

def test_picker_ranks_the_right_iv_first_at_zero_difference(conn):
    cands = marketplace_match.iv_candidates(conn, models.get_marketplace_order(conn, _oid(conn, O379)))
    assert cands[0]['doc_base'] == 'IV6901503'
    assert cands[0]['product_match'] is True and cands[0]['amount_diff'] == 0.0


def _client():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 4
        sess['username'] = 'staffer'
        sess['role'] = 'staff'
    return c


def test_manual_pick_links_a_tiktok_order(conn):
    oid = _oid(conn, O379)
    resp = _client().post(f'/marketplace/order/{oid}/link-iv?platform=tiktok',
                          data={'doc_base': 'IV6901503',
                                'next': '/marketplace/review?platform=tiktok'})
    assert resp.status_code == 302
    assert _links(conn) == [(O379, 'IV6901503', 'Tหน้าร้าน', 'manual', 'manual')]


def test_review_page_offers_tiktok(conn):
    marketplace_match.run_automatch(conn, 'tiktok')
    html = _client().get('/marketplace/review?platform=tiktok').get_data(as_text=True)
    assert '<h4 class="mb-0">ต้องตรวจการจับคู่ใบกำกับ — TikTok</h4>' in html
    assert "platform=tiktok" in html               # the switcher button
    # Control: the worklist was built for tiktok, not the Shopee fallback.
    assert models.get_iv_match_worklist(conn, platform='tiktok')['platform'] == 'tiktok'


def test_settlement_still_falls_back_to_shopee(conn):
    html = _client().get('/marketplace/settlement?platform=tiktok').get_data(as_text=True)
    assert '<h4 class="mb-0">Settlement — Shopee</h4>' in html


def test_recon_customer_is_a_hard_lookup(conn):
    assert mm._RECON_CUSTOMER['tiktok'] == 'หน้าร้านT'
    with pytest.raises(KeyError):
        models.get_marketplace_reconciliation(conn, platform='thaimart')


# ── upload wiring ────────────────────────────────────────────────────────────

def _upload(path, name):
    return _client().post('/marketplace/upload',
                          data={'files': [(io.BytesIO(_read(path)), name)]},
                          content_type='multipart/form-data', follow_redirects=True)


def test_income_upload_runs_automatch_for_tiktok_and_never_reconcile(conn, monkeypatch):
    calls = []
    real = marketplace_match.run_automatch
    monkeypatch.setattr(marketplace_match, 'run_automatch',
                        lambda c, p: calls.append(('match', p)) or real(c, p))
    monkeypatch.setattr(marketplace_reconcile, 'reconcile_payouts',
                        lambda c, p: calls.append(('reconcile', p)) or {})
    _upload(INCOME_XLSX, 'income.xlsx')
    assert calls == [('match', 'tiktok')]
    assert [doc for _, doc, *_ in _links(conn)] == ['IV6901501', 'IV6901503']


def test_order_upload_does_not_run_automatch_like_shopee(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(marketplace_match, 'run_automatch', lambda c, p: calls.append(p))
    _upload(ORDERS_CSV, 'orders.csv')
    assert calls == []


# ── review fixes (Fable, PR-3) ───────────────────────────────────────────────

def _unsettle(c, sn, platform='tiktok'):
    c.execute("UPDATE marketplace_orders SET actual_payout=NULL, settled_at=NULL "
              "WHERE platform=? AND order_sn=?", (platform, sn))
    c.commit()


def test_worklist_counts_an_unsettled_completed_tiktok_order_in_bucket_a(conn):
    _unsettle(conn, O379)
    w = models.get_iv_match_worklist(conn, platform='tiktok')
    assert w['total_a'] == 1


def test_worklist_bucket_a_unchanged_for_shopee(conn):
    """Control: Shopee's gate is still 'สำเร็จแล้ว' only — จัดส่งสำเร็จแล้ว (also in
    _STATUS_COMPLETED) stays out of bucket A, and TikTok's word does nothing there."""
    for sn, status in (('SPA1', 'สำเร็จแล้ว'), ('SPA2', 'จัดส่งสำเร็จแล้ว'), ('SPA3', 'เสร็จสมบูรณ์')):
        conn.execute("INSERT INTO marketplace_orders (platform, order_sn, status, order_date) "
                     "VALUES ('shopee', ?, ?, '2026-09-01 10:00')", (sn, status))
    conn.commit()
    assert models.get_iv_match_worklist(conn, platform='shopee')['total_a'] == 1


def _add_two_line_iv(c, doc_base, date_iso, pids, net_each=69.52):
    for i, pid in enumerate(pids, 1):
        c.execute(
            """INSERT INTO sales_transactions
               (date_iso, doc_no, doc_base, customer, customer_code, qty, unit_price,
                vat_type, total, net, product_id, created_at, synced_to_stock)
               VALUES (?,?,?,'หน้าร้านT','Tหน้าร้าน',1,?,1,?,?,?, '2026-09-20 00:00:00', 1)""",
            (date_iso, f'{doc_base}-{i}', doc_base, net_each, net_each, net_each, pid))
    c.commit()


def _o817_link(c):
    return c.execute("SELECT doc_base FROM marketplace_order_invoice WHERE platform='tiktok' "
                     "AND order_sn=?", (O817,)).fetchone()


def test_a_tiktok_cancel_settled_at_zero_never_takes_a_later_iv(conn):
    """TikTok settles a cancel at ฿0 with a settled_at, so the returns pass saw it.
    A same-product IV 16 days later belongs to some other sale."""
    _add_two_line_iv(conn, 'IV6901520', '2026-09-20', [9403, 9404])
    r = marketplace_match.run_automatch(conn, 'tiktok')
    assert _o817_link(conn) is None and r['returns_matched'] == 0
    # Control: the IV is a real candidate — the two sales still link as before.
    assert len(_links(conn)) == 2


def test_settled_override_needs_money_for_tiktok_only():
    assert marketplace_match._is_matchable_status(
        'Canceled', settled=True, platform='tiktok', actual_payout=0.0) is False
    assert marketplace_match._is_matchable_status(
        'Canceled', settled=True, platform='tiktok', actual_payout=12.0) is True
    # Control: Shopee/Lazada keep the plain settled override.
    for p in ('shopee', 'lazada'):
        assert marketplace_match._is_matchable_status(
            'Canceled', settled=True, platform=p, actual_payout=0.0) is True


def test_unknown_tiktok_cancel_spelling_settled_at_zero_is_not_matched(conn):
    conn.execute("UPDATE marketplace_orders SET status='Canceled' WHERE platform='tiktok' "
                 "AND order_sn=?", (O817,))
    conn.commit()
    _add_two_line_iv(conn, 'IV6901521', '2026-09-06', [9403, 9404])
    marketplace_match.run_automatch(conn, 'tiktok')
    assert _o817_link(conn) is None
    assert len(_links(conn)) == 2                       # control: the two sales still link


def test_unknown_status_warning_names_the_platform(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger='marketplace_match'):
        marketplace_match._is_matchable_status('Mystery', settled=False, platform='tiktok')
    assert 'Mystery' in caplog.text and 'tiktok' in caplog.text


def test_a_failed_automatch_is_rolled_back_not_committed_by_the_next_platform(conn, monkeypatch):
    """run_automatch DELETEs a platform's auto rows before re-deriving them. If it
    raises after that DELETE, the next platform's commit must not make the
    deletion durable: the failed platform's links survive the batch."""
    from tests.test_marketplace_upload_batch import _income_xlsx
    conn.execute("INSERT INTO marketplace_orders (platform, order_sn, status, order_date) "
                 "VALUES ('shopee', 'SPKEEP1', 'สำเร็จแล้ว', '2026-07-10 09:00')")
    conn.execute("INSERT INTO marketplace_order_invoice (platform, order_sn, doc_base, "
                 "customer_code, match_method, confidence) "
                 "VALUES ('shopee', 'SPKEEP1', 'IV6800001', 'Zหน้าร้าน', 'auto', 'confident')")
    conn.commit()
    real = marketplace_match._build_edges

    def boom(orders, *a, **kw):
        if any(o['platform'] == 'shopee' for o in orders):
            raise RuntimeError('injected after the DELETE')
        return real(orders, *a, **kw)
    monkeypatch.setattr(marketplace_match, '_build_edges', boom)

    resp = _client().post('/marketplace/upload', data={'files': [
        (_income_xlsx(order_sn='SPKEEP1'), 'Income.shopee.xlsx'),
        (io.BytesIO(_read(INCOME_XLSX)), 'income_tiktok.xlsx')]},
        content_type='multipart/form-data', follow_redirects=True)
    html = resp.get_data(as_text=True)
    assert 'จับคู่ใบกำกับอัตโนมัติไม่สำเร็จ' in html and 'injected' in html
    assert _links(conn, 'shopee') == [('SPKEEP1', 'IV6800001', 'Zหน้าร้าน', 'auto', 'confident')]
    assert len(_links(conn)) == 2                      # control: tiktok's automatch ran and committed


def test_confirm_page_names_tiktok_properly(conn):
    """A typed IV always goes through the confirm page; its platform label is
    PLAT_LABEL's 'TikTok', not capitalize()'s 'Tiktok'."""
    resp = _client().post(f'/marketplace/order/{_oid(conn, O379)}/link-iv?platform=tiktok',
                          data={'doc_base_manual': 'IV6901503'})
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200 and 'IV6901503' in html      # control: the confirm page
    assert '· TikTok' in html and 'Tiktok' not in html
