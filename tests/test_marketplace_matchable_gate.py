"""TDD for the status-based matchable gate (2026-07-10 relax).

Pre-relax, ``run_automatch`` only pooled orders with
``settled_at IS NOT NULL AND actual_payout IS NOT NULL`` — so a large 2024
Shopee tranche (สำเร็จแล้ว, keyed by the team long before Sendy ever imported a
settlement file) was invisible to the matcher forever. This file pins the new
STATUS-based gate: completed/in-transit statuses are matchable regardless of
settlement; not-yet-shipped is skipped (no IV likely exists yet); the
cancel/return family never matches (unchanged); any unrecognized status is a
fail-safe skip (never a guess). See
projects/express-integration/marketplace-iv-mapping-plan.md §A.

Fixture matches test_marketplace_match.py: deletes every real order (cascades
to their items) and clears the real Zหน้าร้าน/Lหน้าร้าน sales rows from the tmp
clone, then seeds synthetic data. Deleting (not just un-settling) is required
BECAUSE this file tests the new STATUS-based gate — a merely un-settled real
order can still be status-matchable and pollute the pool (that's exactly the
bug this gate fixes, so the fixture must not accidentally rely on the old
settled-only isolation). product_ids 300/301/302 are real live-DB products
reused across tests here (FK checks are ON) — distinct from the ids
test_marketplace_match.py already uses (440/458/500/686/687/777/999) purely so
the two files don't need to coordinate, not because reuse would be unsafe
(each test gets its own tmp DB).
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


def _add_order(c, order_sn, order_date, status, platform='shopee',
                item_total=None, actual_payout=None, settled_at=None, product_id=None):
    cur = c.execute(
        """INSERT INTO marketplace_orders
           (platform, order_sn, status, item_total, actual_payout, settled_at, order_date, currency)
           VALUES (?,?,?,?,?,?,?, 'THB')""",
        (platform, order_sn, status, item_total, actual_payout, settled_at, order_date + ' 10:00'))
    if product_id is not None:
        c.execute(
            """INSERT INTO marketplace_order_items
               (order_id, platform, order_sn, line_key, internal_product_id, qty)
               VALUES (?,?,?,?,?,1)""",
            (cur.lastrowid, platform, order_sn, str(product_id), product_id))
    c.commit()


def _add_iv(c, doc_base, net, date_iso, customer_code='Zหน้าร้าน', vat_type=1, product_id=None):
    c.execute(
        """INSERT INTO sales_transactions
           (date_iso, doc_no, doc_base, customer, customer_code, qty, unit_price,
            vat_type, total, net, product_id, created_at, synced_to_stock)
           VALUES (?,?,?,?,?,?,?,?,?,?,?, '2026-06-01 00:00:00', 1)""",
        (date_iso, f"{doc_base}-1", doc_base, 'หน้าร้านS', customer_code,
         1, net, vat_type, net, net, product_id))
    c.commit()


# ── status classification (unit-level, direct) ──────────────────────────────

@pytest.mark.parametrize('status', [
    'สำเร็จแล้ว', 'จัดส่งสำเร็จแล้ว', 'delivered', 'confirmed',
])
def test_completed_statuses_are_matchable(status):
    assert mm._is_matchable_status(status) is True


def test_completed_prefix_with_dynamic_suffix_is_matchable():
    """Shopee appends a dynamic return-window deadline to this status — must be
    a prefix match, not an exact-string match."""
    s = 'ผู้ซื้อได้รับสินค้าแล้ว โปรดทราบว่าผู้ซื้อสามารถยื่นคำขอคืนเงิน/คืนสินค้าได้จนถึง 2026-07-04'
    assert mm._is_matchable_status(s) is True


@pytest.mark.parametrize('status', ['การจัดส่ง', 'shipped'])
def test_in_transit_statuses_are_matchable(status):
    assert mm._is_matchable_status(status) is True


def test_not_shipped_yet_is_not_matchable():
    assert mm._is_matchable_status('ที่ต้องจัดส่ง') is False


@pytest.mark.parametrize('status', [
    'ยกเลิกแล้ว', 'canceled', 'returned', 'Package Returned',
    'Package scrapped', 'Lost by 3PL', 'In Transit: Returning to seller',
])
def test_cancel_return_family_never_matchable(status):
    assert mm._is_matchable_status(status) is False


def test_unknown_status_is_fail_safe_skip(caplog):
    """A brand-new/unrecognized status must be SKIPPED, never guessed, and must
    log a warning so someone notices and classifies it."""
    with caplog.at_level('WARNING'):
        assert mm._is_matchable_status('some_new_status_shopee_added') is False
    assert any('some_new_status_shopee_added' in r.message for r in caplog.records)


def test_none_status_is_fail_safe_skip():
    assert mm._is_matchable_status(None) is False


# ── integration: run_automatch honours the gate + basis fallback ───────────

def test_unsettled_completed_order_now_enters_pool(mm_conn):
    """The core fix: a สำเร็จแล้ว order with NO settlement (actual_payout/settled_at
    both NULL — the 2024 Shopee tranche) must now product-match, using item_total
    as the billed basis (single-product-vs-single-product needs no amount check
    at all — see _product_compatible D12)."""
    c = mm_conn
    _add_order(c, 'O-UNSETTLED', '2026-06-04', 'สำเร็จแล้ว',
               item_total=99.0, actual_payout=None, settled_at=None, product_id=300)
    _add_iv(c, 'IV9100001', 99.0, '2026-06-05', product_id=300)
    stats = mm.run_automatch(c, 'shopee')
    assert stats['matched'] == 1 and stats['confident'] == 1
    row = c.execute(
        "SELECT doc_base FROM marketplace_order_invoice WHERE order_sn='O-UNSETTLED'").fetchone()
    assert row['doc_base'] == 'IV9100001'


def test_unsettled_order_uses_item_total_as_amount_basis(mm_conn):
    """No product on either side, so this is an amount-only guess — proves the
    ITEM_TOTAL fallback (not actual_payout, which is NULL) feeds the amount
    corroboration: item_total=77 must match an IV net of 77, not silently fail
    because actual_payout is null."""
    c = mm_conn
    _add_order(c, 'O-ITEMTOTAL', '2026-06-04', 'สำเร็จแล้ว',
               item_total=77.0, actual_payout=None, settled_at=None)
    _add_iv(c, 'IV9100002', 77.0, '2026-06-05')
    stats = mm.run_automatch(c, 'shopee')
    assert stats['matched'] == 1 and stats['review'] == 1


def test_settled_order_still_prefers_actual_payout_over_item_total(mm_conn):
    """When settled, the real settlement figure (actual_payout) wins over
    item_total, even though both are present and differ — 'keep settlement
    payout when settled' per the plan."""
    c = mm_conn
    _add_order(c, 'O-SETTLED', '2026-06-04', 'สำเร็จแล้ว',
               item_total=999.0, actual_payout=88.0, settled_at='2026-06-05')
    _add_iv(c, 'IV9100003', 88.0, '2026-06-05')     # matches actual_payout, not item_total
    stats = mm.run_automatch(c, 'shopee')
    assert stats['matched'] == 1
    row = c.execute(
        "SELECT doc_base FROM marketplace_order_invoice WHERE order_sn='O-SETTLED'").fetchone()
    assert row['doc_base'] == 'IV9100003'


def test_not_shipped_yet_order_excluded_even_with_exact_amount(mm_conn):
    """ที่ต้องจัดส่ง (not yet shipped) must stay OUT of the pool — an exact ฿ IV
    sitting nearby must NOT be claimed; the IV likely isn't for this order."""
    c = mm_conn
    _add_order(c, 'O-NOTSHIPPED', '2026-06-04', 'ที่ต้องจัดส่ง', item_total=55.0)
    _add_iv(c, 'IV9100004', 55.0, '2026-06-05')
    stats = mm.run_automatch(c, 'shopee')
    assert stats['unmatched'] == 0     # not even counted — never entered the pool
    assert c.execute("SELECT COUNT(*) FROM marketplace_order_invoice").fetchone()[0] == 0


def test_cancelled_order_never_enters_pool_even_unsettled(mm_conn):
    """ยกเลิกแล้ว must never match, exactly as before the relax — regression."""
    c = mm_conn
    _add_order(c, 'O-CANCELLED', '2026-06-04', 'ยกเลิกแล้ว', item_total=40.0)
    _add_iv(c, 'IV9100005', 40.0, '2026-06-05')
    stats = mm.run_automatch(c, 'shopee')
    assert stats['unmatched'] == 0
    assert c.execute("SELECT COUNT(*) FROM marketplace_order_invoice").fetchone()[0] == 0


def test_unknown_status_order_excluded_from_pool(mm_conn):
    """A brand-new status the matcher doesn't recognize must stay out of the
    automatch pool entirely (fail-safe), not just log-and-continue."""
    c = mm_conn
    _add_order(c, 'O-UNKNOWN', '2026-06-04', 'บางสถานะใหม่ที่ไม่รู้จัก', item_total=40.0)
    _add_iv(c, 'IV9100006', 40.0, '2026-06-05')
    stats = mm.run_automatch(c, 'shopee')
    assert stats['unmatched'] == 0
    assert c.execute("SELECT COUNT(*) FROM marketplace_order_invoice").fetchone()[0] == 0


def test_in_transit_order_matchable_before_settlement(mm_conn):
    """shipped/การจัดส่ง orders match too (team keys the IV at pack/ship time,
    per the plan) — Lazada 'shipped' case, unsettled."""
    c = mm_conn
    _add_order(c, 'L-SHIPPED', '2026-06-04', 'shipped', platform='lazada',
               item_total=66.0, actual_payout=None, settled_at=None, product_id=301)
    _add_iv(c, 'IV9100007', 66.0, '2026-06-05', customer_code='Lหน้าร้าน', product_id=301)
    stats = mm.run_automatch(c, 'lazada')
    assert stats['matched'] == 1 and stats['confident'] == 1
