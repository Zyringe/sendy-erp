"""Task 1.8 (order-driven-platform-deduction plan) — the `/alerts`
staleness warning.

WHY THIS EXISTS: D5 accepts a real gap — once a หน้าร้าน sale is excluded
from `sold_since`, the mirror (`platform_skus.stock`) only catches up at
the NEXT marketplace order import. Sized against weekly cadence + slack
(D8: >10 days), but if the operator ever just stops importing order files,
nothing else tells Put the mirror has gone stale. This alert is that
containment — "the phase that creates the exposure ships its own
containment" (Codex).

Computed live on every `/alerts` page load (`models.get_stock_alerts()`'s
pattern), not a durable `system_alerts` row: the condition self-clears the
moment a fresh order file lands, so nothing here needs a human dismiss.

`last_synced_at` verified as the right signal: grep-confirmed (and pinned
below by `test_last_synced_at_not_bumped_by_settlement_or_payout_writers`)
that ONLY `import_marketplace_orders`'s header upsert ever writes it —
`upsert_marketplace_settlements` / `assign_orders_to_batch` /
`assign_orders_manual` / `create_baseline_batch` / `unassign_batch` all
touch OTHER `marketplace_orders` columns (`settled_at`, `actual_payout`,
`payout_batch_id`) and never this one, so a settlement/payout action can
never fake order freshness.
"""
import inspect

import models.marketplace as marketplace_mod
from models.marketplace import get_order_staleness_alerts, ORDER_STALENESS_DAYS


def _seed_listing(conn, platform, variation_id, is_ignored=0):
    conn.execute(
        "INSERT INTO platform_skus (platform, variation_id, product_name, stock, is_ignored) "
        "VALUES (?, ?, 'p', 5, ?)", (platform, variation_id, is_ignored))
    conn.commit()


def _seed_order(conn, platform, order_sn, days_ago):
    conn.execute(
        "INSERT INTO marketplace_orders (platform, order_sn, last_synced_at) "
        "VALUES (?, ?, datetime('now','localtime', ?))",
        (platform, order_sn, f'-{days_ago} days'))
    conn.commit()


def test_fires_at_11_days(empty_db_conn):
    conn = empty_db_conn
    _seed_listing(conn, 'shopee', 'V1')
    _seed_order(conn, 'shopee', 'ORD-11', 11)

    alerts = get_order_staleness_alerts(conn)

    assert len(alerts) == 1
    assert alerts[0]['platform'] == 'shopee'
    assert alerts[0]['days_old'] == 11
    assert 'Shopee' in alerts[0]['message']
    assert 'สต็อกหน้าร้านใน Sendy อาจสูงกว่าจริง' in alerts[0]['message']


def test_does_not_fire_at_9_days(empty_db_conn):
    conn = empty_db_conn
    _seed_listing(conn, 'shopee', 'V1')
    _seed_order(conn, 'shopee', 'ORD-9', 9)

    assert get_order_staleness_alerts(conn) == []


def test_boundary_exactly_10_days_does_not_fire(empty_db_conn):
    conn = empty_db_conn
    _seed_listing(conn, 'shopee', 'V1')
    _seed_order(conn, 'shopee', 'ORD-10', ORDER_STALENESS_DAYS)

    assert get_order_staleness_alerts(conn) == []


def test_never_imported_warns(empty_db_conn):
    conn = empty_db_conn
    _seed_listing(conn, 'lazada', 'V-LZ')  # active listing, zero rows in marketplace_orders

    alerts = get_order_staleness_alerts(conn)

    assert len(alerts) == 1
    assert alerts[0]['platform'] == 'lazada'
    assert alerts[0]['days_old'] is None
    assert 'Lazada' in alerts[0]['message']


def test_fresh_import_is_silent_control(empty_db_conn):
    """CONTROL: a platform with a recent import produces no alert."""
    conn = empty_db_conn
    _seed_listing(conn, 'shopee', 'V1')
    _seed_order(conn, 'shopee', 'ORD-FRESH', 1)

    assert get_order_staleness_alerts(conn) == []


def test_no_active_listing_no_alert(empty_db_conn):
    """A platform with only an IGNORED listing has nothing live selling on
    it -- staleness of its order import doesn't matter."""
    conn = empty_db_conn
    _seed_listing(conn, 'shopee', 'V-IGN', is_ignored=1)
    # No orders at all either -- would otherwise be the "never imported" case.

    assert get_order_staleness_alerts(conn) == []


def test_tiktok_never_alerts_even_with_active_listing_and_no_orders(empty_db_conn):
    """D9: TikTok is fully out of scope -- no TikTok orders exist in the
    ERP, so warning about a TikTok order-import gap would be nonsense."""
    conn = empty_db_conn
    _seed_listing(conn, 'tiktok', 'V-TT')

    assert get_order_staleness_alerts(conn) == []


def test_names_the_right_platform_among_two(empty_db_conn):
    conn = empty_db_conn
    _seed_listing(conn, 'shopee', 'V-S')
    _seed_listing(conn, 'lazada', 'V-L')
    _seed_order(conn, 'shopee', 'ORD-S', 1)     # fresh
    _seed_order(conn, 'lazada', 'ORD-L', 15)    # stale

    alerts = get_order_staleness_alerts(conn)

    assert len(alerts) == 1
    assert alerts[0]['platform'] == 'lazada'


def test_last_synced_at_not_bumped_by_settlement_or_payout_writers():
    """Contamination check from the brief: none of the settlement/payout
    write paths may touch `last_synced_at`, or a settlement/payout upload
    would fake order freshness."""
    for fn in (marketplace_mod.upsert_marketplace_settlements,
              marketplace_mod.assign_orders_to_batch,
              marketplace_mod.assign_orders_manual,
              marketplace_mod.create_baseline_batch,
              marketplace_mod.unassign_batch):
        src = inspect.getsource(fn)
        assert 'last_synced_at' not in src, (
            f"{fn.__name__} touches last_synced_at -- staleness signal contaminated")

    # CONTROL: the ONE function that IS supposed to touch it does.
    assert 'last_synced_at' in inspect.getsource(marketplace_mod.import_marketplace_orders)
