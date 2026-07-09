"""TDD for the matcher rebuild (global / product-first / nearest-date).

Replaces the greedy 3-pass ``run_automatch`` (see test_marketplace_match.py for
the old behavior it superseded). These 6 cases pin the spec at
``projects/marketplace-iv-matching/matcher-rebuild-spec.md`` — they must FAIL on
the old greedy matcher and PASS on the new global-assignment one.

Fixture isolates the matcher exactly like test_marketplace_match.py: deletes
every real order (cascades to their items) and the real Zหน้าร้าน/Lหน้าร้าน sales
rows from the tmp clone, then seeds synthetic data. Deleting (not just
un-settling) is required since the matcher's gate is STATUS-based, not
settled-only (2026-07-10) — a merely un-settled real order can still be
status-matchable and pollute the pool. product_ids 686/687/500/440/458/777/999
are real live-DB products (reused from test_marketplace_match.py) so FK checks
(PRAGMA foreign_keys=ON) don't trip; 686/687 are the actual ถุงหิ้ว pids from the
real incident this rebuild fixes.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

import marketplace_match as mm


@pytest.fixture
def mm_conn(tmp_db_conn):
    c = tmp_db_conn
    c.execute("DELETE FROM marketplace_orders")
    c.execute("DELETE FROM marketplace_order_invoice")
    c.execute("DELETE FROM sales_transactions WHERE customer_code IN ('Zหน้าร้าน','Lหน้าร้าน')")
    c.commit()
    return c


def _add_order(c, order_sn, payout, order_date, platform='shopee', product_ids=None):
    """Settled order. product_ids: None (unmapped), a single id, or a list of ids
    (multi-item order — one marketplace_order_items line per id)."""
    cur = c.execute(
        """INSERT INTO marketplace_orders
           (platform, order_sn, status, actual_payout, settled_at, order_date, currency)
           VALUES (?,?, 'สำเร็จแล้ว', ?, ?, ?, 'THB')""",
        (platform, order_sn, payout, order_date, order_date + ' 10:00'))
    order_id = cur.lastrowid
    if product_ids is not None:
        ids = product_ids if isinstance(product_ids, (list, tuple)) else [product_ids]
        for i, pid in enumerate(ids):
            c.execute(
                """INSERT INTO marketplace_order_items
                   (order_id, platform, order_sn, line_key, internal_product_id, qty)
                   VALUES (?,?,?,?,?,1)""",
                (order_id, platform, order_sn, f'{pid}-{i}', pid))
    c.commit()


def _add_iv(c, doc_base, date_iso, lines, customer_code='Zหน้าร้าน', vat_type=1):
    """lines: [(product_id, net), ...] — one Express sales_transactions line per
    tuple; iv_net (aggregated by marketplace_match._ivs_for) = sum of the nets."""
    for i, (pid, net) in enumerate(lines, start=1):
        c.execute(
            """INSERT INTO sales_transactions
               (date_iso, doc_no, doc_base, customer, customer_code, qty, unit_price,
                vat_type, total, net, product_id, created_at, synced_to_stock)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, '2026-06-01 00:00:00', 1)""",
            (date_iso, f"{doc_base}-{i}", doc_base, 'หน้าร้านS', customer_code,
             1, net, vat_type, net, net, pid))
    c.commit()


def test_stranding_fixed(mm_conn):
    """The real ถุงหิ้ว incident (pid 686/687, matcher-rebuild-spec.md). The old
    greedy exact-amount-first + oldest-first matcher strands 260622/260624VR and
    lets 260624VR grab a wrong-product IV via the amount-only fallback. The new
    global matcher must place all 7 orders, with 260624VR taking the single-item
    IV (NOT the bundle) and 260624VQ taking the bundle."""
    c = mm_conn
    _add_order(c, '260619',   29.0, '2026-06-19', product_ids=687)
    _add_order(c, '260620',   29.0, '2026-06-20', product_ids=687)
    _add_order(c, '260622',   29.0, '2026-06-22', product_ids=687)
    _add_order(c, '260624VR', 29.0, '2026-06-24', product_ids=687)
    _add_order(c, '260624VQ', 57.0, '2026-06-24', product_ids=[686, 687])
    _add_order(c, '260628',   29.0, '2026-06-28', product_ids=687)
    _add_order(c, '260629',   29.0, '2026-06-29', product_ids=687)

    # Invoice nets are DELIBERATELY not all equal: 969/978/1012/1037/1040 net ฿30
    # (a plausible fee-wobble, never exactly 29), 985 nets exactly ฿29. This is
    # the real bug trigger — the old exact-amount-first pass has exactly ONE
    # amount-exact candidate for the early ฿29 orders (985, dated 06-22) and
    # grabs it for 260619 regardless of date, skipping 260619's own same-day
    # (but non-exact) IV969 and stranding 260622's rightful 985.
    _add_iv(c, 'IV6900969', '2026-06-19', [(687, 30.0)])
    _add_iv(c, 'IV6900978', '2026-06-20', [(687, 30.0)])
    _add_iv(c, 'IV6900985', '2026-06-22', [(687, 29.0)])
    _add_iv(c, 'IV6901011', '2026-06-24', [(686, 28.0), (687, 29.0)])   # bundle, 57 total
    _add_iv(c, 'IV6901012', '2026-06-24', [(687, 30.0)])
    _add_iv(c, 'IV6901037', '2026-06-29', [(687, 30.0)])
    _add_iv(c, 'IV6901040', '2026-06-29', [(687, 30.0)])

    stats = mm.run_automatch(c, 'shopee')
    assert stats['matched'] == 7 and stats['unmatched'] == 0

    links = {r['order_sn']: r['doc_base'] for r in c.execute(
        "SELECT order_sn, doc_base FROM marketplace_order_invoice WHERE platform='shopee'")}
    assert links['260619'] == 'IV6900969'
    assert links['260620'] == 'IV6900978'
    assert links['260622'] == 'IV6900985'
    assert links['260624VR'] == 'IV6901012'          # NOT the bundle
    assert links['260624VQ'] == 'IV6901011'          # the bundle
    # 260628/260629 split the two 06-29 invoices (both are date-equidistant, so
    # either split is optimal) — but never a non-687 invoice.
    assert {links['260628'], links['260629']} == {'IV6901037', 'IV6901040'}
    assert set(links.values()) == {
        'IV6900969', 'IV6900978', 'IV6900985', 'IV6901011', 'IV6901012',
        'IV6901037', 'IV6901040'}


def test_bundle_preserved(mm_conn):
    """A ฿57 2-item order (686+687) keeps the matching bundle IV; a plain ฿29
    1-item order sharing ONE of the products must never grab it — multi-item
    edges need amount corroboration, not just product overlap.

    O29 is dated a day BEFORE OQ, and the bundle's net (55) is exact for
    neither, so the old oldest-first 'product' pass (no corroboration at all)
    processes O29 first and steals the bundle on naive product overlap,
    leaving OQ — the order that actually needs it — unmatched."""
    c = mm_conn
    _add_order(c, 'O29', 29.0, '2026-06-23', product_ids=687)
    _add_order(c, 'OQ', 57.0, '2026-06-24', product_ids=[686, 687])
    _add_iv(c, 'IV_BUNDLE', '2026-06-24', [(686, 26.0), (687, 29.0)])   # net 55, exact for neither
    stats = mm.run_automatch(c, 'shopee')
    links = {r['order_sn']: r['doc_base'] for r in c.execute(
        "SELECT order_sn, doc_base FROM marketplace_order_invoice WHERE platform='shopee'")}
    assert links.get('OQ') == 'IV_BUNDLE'
    assert 'O29' not in links                 # never grabbed the bundle
    assert stats['unmatched'] == 1             # O29 has nothing else to match


def test_nearest_date_beats_exact_amount(mm_conn):
    """Product-matched: a same-day near-amount IV beats a later EXACT-amount IV.
    The old exact-amount-first pass reached past the same-day invoice for an
    exact-฿ one further out; the new matcher must not."""
    c = mm_conn
    _add_order(c, 'OD', 50.0, '2026-06-10', product_ids=500)
    _add_iv(c, 'IV_NEAR', '2026-06-10', [(500, 48.0)])    # gap 0, off by 2
    _add_iv(c, 'IV_FAR',  '2026-06-15', [(500, 50.0)])    # gap 5, exact amount
    mm.run_automatch(c, 'shopee')
    row = c.execute(
        "SELECT doc_base, confidence FROM marketplace_order_invoice WHERE order_sn='OD'").fetchone()
    assert row['doc_base'] == 'IV_NEAR'
    assert row['confidence'] == 'confident'      # product + date match: ฿ gap doesn't downgrade it


def test_floor_no_fabrication(mm_conn):
    """No product info AND no plausible amount candidate → stays unmatched, no
    fabricated link (D14 floor)."""
    c = mm_conn
    _add_order(c, 'OU', 348.0, '2026-06-10')                 # unmapped, no product_ids
    _add_iv(c, 'IV_LOW', '2026-06-10', [(500, 30.0)])        # same-day but wildly off (318)
    stats = mm.run_automatch(c, 'shopee')
    assert stats['unmatched'] == 1
    assert c.execute("SELECT COUNT(*) FROM marketplace_order_invoice").fetchone()[0] == 0


def test_amount_only_guess_labeled(mm_conn):
    """Unmapped-product order with a near-amount IV auto-links as a distinct
    'review' guess — never 'confident' (D13)."""
    c = mm_conn
    _add_order(c, 'OG', 40.0, '2026-06-10')                  # unmapped
    _add_iv(c, 'IV_G', '2026-06-11', [(500, 45.0)])          # gap 1, off by 5 (within FUZZY=15)
    stats = mm.run_automatch(c, 'shopee')
    assert stats['matched'] == 1 and stats['review'] == 1 and stats['confident'] == 0
    row = c.execute(
        "SELECT doc_base, confidence FROM marketplace_order_invoice WHERE order_sn='OG'").fetchone()
    assert row['doc_base'] == 'IV_G' and row['confidence'] == 'review'


def test_manual_preserved(mm_conn):
    """A manual link + its invoice are untouched by a re-run of the new matcher."""
    c = mm_conn
    _add_order(c, 'O-M', 60.0, '2026-06-04', product_ids=500)
    _add_order(c, 'O-A', 60.0, '2026-06-05', product_ids=500)
    _add_iv(c, 'IV_MANUAL', '2026-06-05', [(500, 60.0)])
    mm.link_manual(c, 'shopee', 'O-M', 'IV_MANUAL', confirmed_by='put')
    mm.run_automatch(c, 'shopee')
    rows = [dict(r) for r in c.execute(
        "SELECT order_sn, match_method FROM marketplace_order_invoice WHERE doc_base='IV_MANUAL'")]
    assert rows == [{'order_sn': 'O-M', 'match_method': 'manual'}]
    assert c.execute(
        "SELECT COUNT(*) FROM marketplace_order_invoice WHERE order_sn='O-A'").fetchone()[0] == 0
