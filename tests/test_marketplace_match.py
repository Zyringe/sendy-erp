"""Tests for marketplace_match — linking marketplace orders to Express IVs.

The matcher links a settled Shopee/Lazada order to the Express invoice the team
keyed shortly AFTER the platform order, using product overlap → date (IV on/after
the order date) → amount. 'confident' = the matched IV's amount equals the payout;
'review' = it differs (a billed≠payout discrepancy to fix in Express).

Fixture isolates the matcher: it un-settles every real order and deletes the real
Zหน้าร้าน/Lหน้าร้าน sales rows + items from the tmp clone, then seeds synthetic data.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

import marketplace_match as mm


@pytest.fixture
def mm_conn(tmp_db_conn):
    c = tmp_db_conn
    c.execute("UPDATE marketplace_orders SET actual_payout=NULL, settled_at=NULL, settlement_source=NULL")
    c.execute("DELETE FROM marketplace_order_invoice")
    c.execute("DELETE FROM sales_transactions WHERE customer_code IN ('Zหน้าร้าน','Lหน้าร้าน')")
    c.commit()
    return c


def _add_order(c, order_sn, payout, order_date, platform='shopee', product_id=None):
    """Settled order; order_date is the matcher anchor (settled_at only needs to be
    non-null so the order counts as settled)."""
    settled_at = order_date
    cur = c.execute(
        """INSERT INTO marketplace_orders
           (platform, order_sn, status, actual_payout, settled_at, order_date, currency)
           VALUES (?,?, 'สำเร็จแล้ว', ?, ?, ?, 'THB')""",
        (platform, order_sn, payout, settled_at, order_date + ' 10:00'))
    if product_id is not None:
        c.execute(
            """INSERT INTO marketplace_order_items
               (order_id, platform, order_sn, line_key, internal_product_id, qty)
               VALUES (?,?,?,?,?,1)""",
            (cur.lastrowid, platform, order_sn, str(product_id), product_id))
    c.commit()


def _add_iv(c, doc_base, net, date_iso, customer_code='Zหน้าร้าน', vat_type=1, lines=1, product_id=None):
    for i in range(1, lines + 1):
        line_net = net if i == 1 else 0.0
        c.execute(
            """INSERT INTO sales_transactions
               (date_iso, doc_no, doc_base, customer, customer_code, qty, unit_price,
                vat_type, total, net, product_id, created_at, synced_to_stock)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, '2026-06-01 00:00:00', 1)""",
            (date_iso, f"{doc_base}-{i}", doc_base, 'หน้าร้านS', customer_code,
             1, line_net, vat_type, line_net, line_net, product_id))
    c.commit()


def test_exact_amount_confident(mm_conn):
    c = mm_conn
    _add_order(c, 'O-UNIQ', 99.0, '2026-06-04')
    _add_iv(c, 'IV9000001', 99.0, '2026-06-05')           # 1 day after, amount matches
    stats = mm.run_automatch(c, 'shopee')
    assert stats['matched'] == 1 and stats['confident'] == 1
    row = c.execute(
        "SELECT doc_base, match_method, confidence FROM marketplace_order_invoice WHERE order_sn='O-UNIQ'"
    ).fetchone()
    assert row['doc_base'] == 'IV9000001'
    assert row['match_method'] == 'auto' and row['confidence'] == 'confident'


def test_iv_before_order_is_excluded(mm_conn):
    """An invoice dated BEFORE the platform order can't be that order's invoice."""
    c = mm_conn
    _add_order(c, 'O-AFTER', 50.0, '2026-06-10')
    _add_iv(c, 'IV9000002', 50.0, '2026-06-03')           # 7 days BEFORE the order
    stats = mm.run_automatch(c, 'shopee')
    assert stats['unmatched'] == 1
    assert c.execute("SELECT COUNT(*) FROM marketplace_order_invoice").fetchone()[0] == 0


def test_product_overlap_beats_nearer_date(mm_conn):
    """A product-matching invoice wins over a date-nearer one with no shared product
    (the 246/236 same-amount-sibling case)."""
    c = mm_conn
    _add_order(c, 'O-PROD', 50.0, '2026-06-04', product_id=999)
    _add_iv(c, 'IV_NEAR', 50.0, '2026-06-04')                       # gap 0, no product
    _add_iv(c, 'IV_PROD', 50.0, '2026-06-05', product_id=999)       # gap 1, shares product 999
    stats = mm.run_automatch(c, 'shopee')
    assert stats['matched'] == 1
    assert c.execute(
        "SELECT doc_base FROM marketplace_order_invoice WHERE order_sn='O-PROD'"
    ).fetchone()['doc_base'] == 'IV_PROD'


def test_amount_differs_is_review(mm_conn):
    """Product confirms the invoice but the amount differs (Shopee adjusted the
    payout) → labelled 'review' (a discrepancy to fix)."""
    c = mm_conn
    _add_order(c, 'O-ADJ', 236.0, '2026-06-05', product_id=440)
    _add_iv(c, 'IV9000010', 246.0, '2026-06-06', product_id=440)    # +10฿, shares product
    stats = mm.run_automatch(c, 'shopee')
    assert stats['matched'] == 1 and stats['review'] == 1 and stats['confident'] == 0
    row = c.execute(
        "SELECT doc_base, confidence FROM marketplace_order_invoice WHERE order_sn='O-ADJ'").fetchone()
    assert row['doc_base'] == 'IV9000010' and row['confidence'] == 'review'


def test_unconfirmed_large_amount_gap_unmatched(mm_conn):
    """No shared product AND a big amount gap → not a wild guess, left unmatched."""
    c = mm_conn
    _add_order(c, 'O-FAR', 100.0, '2026-06-08')
    _add_iv(c, 'IV9000011', 200.0, '2026-06-08')           # same day but 100฿ off, no product
    stats = mm.run_automatch(c, 'shopee')
    assert stats['unmatched'] == 1
    assert c.execute("SELECT COUNT(*) FROM marketplace_order_invoice").fetchone()[0] == 0


def test_oldest_order_takes_nearest(mm_conn):
    """Two same-amount orders + two invoices: each gets a distinct invoice."""
    c = mm_conn
    _add_order(c, 'O-1', 12.0, '2026-06-04')
    _add_order(c, 'O-2', 12.0, '2026-06-05')
    _add_iv(c, 'IV9000020', 12.0, '2026-06-04')
    _add_iv(c, 'IV9000021', 12.0, '2026-06-06')
    stats = mm.run_automatch(c, 'shopee')
    assert stats['matched'] == 2 and stats['unmatched'] == 0
    docs = {r['order_sn']: r['doc_base'] for r in c.execute(
        "SELECT order_sn, doc_base FROM marketplace_order_invoice").fetchall()}
    assert docs['O-1'] != docs['O-2']


def test_more_orders_than_ivs_leaves_remainder(mm_conn):
    c = mm_conn
    _add_order(c, 'O-A', 12.0, '2026-06-04')
    _add_order(c, 'O-B', 12.0, '2026-06-05')
    _add_iv(c, 'IV9000030', 12.0, '2026-06-05')
    stats = mm.run_automatch(c, 'shopee')
    assert stats['matched'] == 1 and stats['unmatched'] == 1
    assert c.execute("SELECT COUNT(*) FROM marketplace_order_invoice").fetchone()[0] == 1


def test_manual_not_clobbered_and_iv_not_reused(mm_conn):
    c = mm_conn
    _add_order(c, 'O-M', 60.0, '2026-06-04')
    _add_order(c, 'O-A', 60.0, '2026-06-05')
    _add_iv(c, 'IV9000040', 60.0, '2026-06-05')
    mm.link_manual(c, 'shopee', 'O-M', 'IV9000040', confirmed_by='put')
    mm.run_automatch(c, 'shopee')
    rows = c.execute(
        "SELECT order_sn FROM marketplace_order_invoice WHERE doc_base='IV9000040'").fetchall()
    assert [r['order_sn'] for r in rows] == ['O-M']                 # not stolen by auto
    assert c.execute("SELECT COUNT(*) FROM marketplace_order_invoice WHERE order_sn='O-A'").fetchone()[0] == 0


def test_idempotent(mm_conn):
    c = mm_conn
    _add_order(c, 'O-IDEM', 33.0, '2026-06-04')
    _add_iv(c, 'IV9000050', 33.0, '2026-06-05')
    mm.run_automatch(c, 'shopee')
    mm.run_automatch(c, 'shopee')
    assert c.execute(
        "SELECT COUNT(*) FROM marketplace_order_invoice WHERE order_sn='O-IDEM'").fetchone()[0] == 1


def test_vat_type2_amount_matches_payout(mm_conn):
    c = mm_conn
    _add_order(c, 'O-VAT', 107.0, '2026-06-09')
    _add_iv(c, 'IV9000060', 100.0, '2026-06-10', vat_type=2)        # 100*1.07 = 107
    stats = mm.run_automatch(c, 'shopee')
    assert stats['confident'] == 1
    assert c.execute(
        "SELECT doc_base FROM marketplace_order_invoice WHERE order_sn='O-VAT'"
    ).fetchone()['doc_base'] == 'IV9000060'


def test_lazada_uses_L_code(mm_conn):
    c = mm_conn
    _add_order(c, 'L-1', 88.0, '2026-06-04', platform='lazada')
    _add_iv(c, 'IV9000070', 88.0, '2026-06-05', customer_code='Lหน้าร้าน')
    stats = mm.run_automatch(c, 'lazada')
    assert stats['matched'] == 1
    assert c.execute(
        "SELECT doc_base FROM marketplace_order_invoice WHERE order_sn='L-1'"
    ).fetchone()['doc_base'] == 'IV9000070'


def test_shopee_ignores_lazada_iv(mm_conn):
    c = mm_conn
    _add_order(c, 'O-SHOP', 55.0, '2026-06-04', platform='shopee')
    _add_iv(c, 'IV9000080', 55.0, '2026-06-05', customer_code='Lหน้าร้าน')  # wrong book
    stats = mm.run_automatch(c, 'shopee')
    assert stats['unmatched'] == 1
    assert c.execute("SELECT COUNT(*) FROM marketplace_order_invoice").fetchone()[0] == 0


def test_manual_link_steals_iv(mm_conn):
    c = mm_conn
    _add_order(c, 'O-OLD', 50.0, '2026-06-04')
    _add_order(c, 'O-NEW', 50.0, '2026-06-05')
    _add_iv(c, 'IV9000200', 50.0, '2026-06-05')
    mm.run_automatch(c, 'shopee')
    holder = c.execute(
        "SELECT order_sn FROM marketplace_order_invoice WHERE doc_base='IV9000200'").fetchone()['order_sn']
    other = 'O-NEW' if holder == 'O-OLD' else 'O-OLD'
    stolen = mm.link_manual(c, 'shopee', other, 'IV9000200', confirmed_by='put')
    assert holder in stolen
    rows = c.execute(
        "SELECT order_sn, match_method FROM marketplace_order_invoice WHERE doc_base='IV9000200'").fetchall()
    assert len(rows) == 1 and rows[0]['order_sn'] == other and rows[0]['match_method'] == 'manual'


def test_picker_surfaces_near_amount_iv(mm_conn):
    """The picker shows a near-amount invoice (10฿ off) with its diff + days-after."""
    c = mm_conn
    _add_order(c, 'O-NOISE', 236.0, '2026-06-06')
    _add_iv(c, 'IV9000220', 246.0, '2026-06-08')           # +10฿, 2 days after the order
    order = c.execute("SELECT * FROM marketplace_orders WHERE order_sn='O-NOISE'").fetchone()
    cands = mm.iv_candidates(c, order)
    m = next(x for x in cands if x['doc_base'] == 'IV9000220')
    assert m['amount_diff'] == 10.0
    assert m['date_gap'] == 2


def test_picker_ranks_product_match_first(mm_conn):
    c = mm_conn
    _add_order(c, 'O-PK', 80.0, '2026-06-04', product_id=777)
    _add_iv(c, 'IV_A', 80.0, '2026-06-04')                          # gap 0, no product
    _add_iv(c, 'IV_B', 80.0, '2026-06-05', product_id=777)          # gap 1, product match
    order = c.execute("SELECT * FROM marketplace_orders WHERE order_sn='O-PK'").fetchone()
    cands = mm.iv_candidates(c, order)
    assert cands[0]['doc_base'] == 'IV_B' and cands[0]['product_match'] is True


def test_lazada_matches_iv_on_gross_not_net(mm_conn):
    c = mm_conn
    # Lazada order: gross 100, net payout 80 (20% fee). Team keyed the IV at GROSS=100.
    cur = c.execute("""INSERT INTO marketplace_orders
        (platform, order_sn, status, actual_payout, settled_at, order_date, item_total, currency)
        VALUES ('lazada','LZ1','สำเร็จ', 80, '2026-06-10', '2026-06-10 10:00', 100, 'THB')""")
    c.execute("""INSERT INTO marketplace_order_fees (platform, order_sn, item_value, net_payout, fee_total)
                 VALUES ('lazada','LZ1',100,80,20)""")
    c.commit()
    _add_iv(c, 'IV7000001', 100.0, '2026-06-11', customer_code='Lหน้าร้าน')
    res = mm.run_automatch(c, 'lazada')
    row = c.execute("SELECT doc_base, confidence FROM marketplace_order_invoice WHERE order_sn='LZ1'").fetchone()
    assert row['doc_base'] == 'IV7000001'
    assert row['confidence'] == 'confident'      # IV(100) == gross(100), not net(80)
