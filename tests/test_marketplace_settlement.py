"""Tests for marketplace settlement upsert and report functions.

Uses the shared `tmp_db_conn` fixture (a tmp clone of the live DB, schema already
at migration 099 so the settlement columns exist) and seeds two synthetic shopee
orders. The assertions (updated/not_found counts, batches grouped by date, totals)
are what matter; the fixture only provides an isolated DB to act on.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')  # don't init the live DB when importing app

import pytest

import models


@pytest.fixture
def conn(tmp_db_conn):
    """Clone of the live DB seeded with exactly two settle-able shopee orders.

    The clone already carries real marketplace rows, but none have settled_at /
    actual_payout set (verified at write time), so stamping only ORDER001/ORDER002
    keeps the settlement-report assertions deterministic.
    """
    c = tmp_db_conn
    # Clear any pre-existing settlement state so the report groups deterministically,
    # and remove name collisions for the synthetic order_sns.
    c.execute("UPDATE marketplace_orders SET actual_payout=NULL, settled_at=NULL, settlement_source=NULL")
    c.execute("DELETE FROM marketplace_orders WHERE order_sn IN ('ORDER001','ORDER002','ORDER003','LAZ001')")
    c.execute("""INSERT INTO marketplace_orders
        (platform, order_sn, status, item_total, marketplace_fee, payout, currency)
        VALUES
        ('shopee', 'ORDER001', 'สำเร็จแล้ว',  275.0, 64.0, 211.0, 'THB'),
        ('shopee', 'ORDER002', 'สำเร็จแล้ว',  100.0, 22.0,  78.0, 'THB'),
        ('shopee', 'ORDER003', 'ยกเลิกแล้ว',  100.0, NULL, NULL, 'THB'),
        ('lazada', 'LAZ001',   'สำเร็จแล้ว',  500.0, NULL, NULL, 'THB')
    """)
    c.commit()
    return c


def test_upsert_updates_matched_orders(conn):
    settlements = [
        {'order_sn': 'ORDER001', 'actual_payout': 211.0, 'settled_at': '2026-05-10'},
    ]
    stats = models.upsert_marketplace_settlements(conn, settlements, 'Income.test.xlsx')
    assert stats['updated'] == 1
    assert stats['not_found'] == 0
    row = conn.execute(
        "SELECT actual_payout, settled_at, settlement_source FROM marketplace_orders WHERE order_sn='ORDER001'"
    ).fetchone()
    assert row[0] == pytest.approx(211.0)
    assert row[1] == '2026-05-10'
    assert row[2] == 'Income.test.xlsx'


def test_upsert_counts_not_found(conn):
    settlements = [
        {'order_sn': 'NONEXISTENT', 'actual_payout': 50.0, 'settled_at': '2026-06-01'},
    ]
    stats = models.upsert_marketplace_settlements(conn, settlements, 'test.xlsx')
    assert stats['updated'] == 0
    assert stats['not_found'] == 1


def test_upsert_handles_multiple(conn):
    settlements = [
        {'order_sn': 'ORDER001', 'actual_payout': 211.0, 'settled_at': '2026-05-10'},
        {'order_sn': 'ORDER002', 'actual_payout': 78.0,  'settled_at': '2026-05-10'},
        {'order_sn': 'GHOST',    'actual_payout': 0.0,   'settled_at': '2026-05-10'},
    ]
    stats = models.upsert_marketplace_settlements(conn, settlements, 'test.xlsx')
    assert stats['updated'] == 2
    assert stats['not_found'] == 1


def test_get_settlement_report_groups_by_date(conn):
    conn.execute("""UPDATE marketplace_orders SET actual_payout=211.0, settled_at='2026-05-10'
                    WHERE order_sn='ORDER001'""")
    conn.execute("""UPDATE marketplace_orders SET actual_payout=78.0, settled_at='2026-05-10'
                    WHERE order_sn='ORDER002'""")
    conn.commit()
    report = models.get_settlement_report(conn)
    assert len(report['batches']) == 1
    batch = report['batches'][0]
    assert batch['settled_at'] == '2026-05-10'
    assert batch['order_count'] == 2
    assert batch['total_payout'] == pytest.approx(289.0)


def _pending_sns(report):
    return {p['order_sn'] for p in report['pending']}


def test_pending_lists_unsettled_and_drops_settled(conn):
    """A settled order leaves pending; an unsettled one stays."""
    models.upsert_marketplace_settlements(
        conn, [{'order_sn': 'ORDER001', 'actual_payout': 211.0, 'settled_at': '2026-05-10'}],
        'Income.test.xlsx')
    report = models.get_settlement_report(conn)
    sns = _pending_sns(report)
    assert 'ORDER001' not in sns      # got stamped → out of pending
    assert 'ORDER002' in sns          # still unsettled → in pending


def test_pending_excludes_cancelled(conn):
    """ยกเลิกแล้ว orders never count as collectable AR → excluded from pending."""
    report = models.get_settlement_report(conn)
    assert 'ORDER003' not in _pending_sns(report)


def test_report_is_platform_scoped(conn):
    """Shopee report ignores lazada rows entirely (batches + pending)."""
    report = models.get_settlement_report(conn, platform='shopee')
    assert 'LAZ001' not in _pending_sns(report)


def test_fee_diff_is_rounded(conn):
    """fee_diff = item_total - actual_payout, rounded to 2 dp."""
    # 275.0 - 211.123 = 63.877 → rounds to 63.88
    models.upsert_marketplace_settlements(
        conn, [{'order_sn': 'ORDER001', 'actual_payout': 211.123, 'settled_at': '2026-05-10'}],
        'Income.test.xlsx')
    report = models.get_settlement_report(conn)
    order = next(o for b in report['batches'] for o in b['orders']
                 if o['order_sn'] == 'ORDER001')
    assert order['fee_diff'] == pytest.approx(63.88)


def test_blank_settled_at_is_not_settled(conn):
    """A blank settled_at (parser emits '') must NOT settle the order:
    no phantom batch keyed on '', and the order stays in pending."""
    stats = models.upsert_marketplace_settlements(
        conn, [{'order_sn': 'ORDER001', 'actual_payout': 211.0, 'settled_at': ''}],
        'Income.test.xlsx')
    assert stats['updated'] == 0
    assert stats['skipped_no_date'] == 1
    report = models.get_settlement_report(conn)
    # No batch should carry an empty settled_at key.
    assert all(b['settled_at'] not in ('', None) for b in report['batches'])
    # The order was NOT stamped, so it remains pending.
    assert 'ORDER001' in _pending_sns(report)
    # And the DB row is untouched (still NULL).
    row = conn.execute(
        "SELECT actual_payout, settled_at FROM marketplace_orders WHERE platform='shopee' AND order_sn='ORDER001'"
    ).fetchone()
    assert row[0] is None and row[1] is None


def test_upsert_does_not_cross_platforms(conn):
    """A shopee Income file must not stamp a lazada row sharing an order_sn."""
    # Force a cross-platform order_sn collision.
    conn.execute("UPDATE marketplace_orders SET order_sn='ORDER001' WHERE platform='lazada' AND order_sn='LAZ001'")
    conn.commit()
    models.upsert_marketplace_settlements(
        conn, [{'order_sn': 'ORDER001', 'actual_payout': 211.0, 'settled_at': '2026-05-10'}],
        'Income.test.xlsx', platform='shopee')
    laz = conn.execute(
        "SELECT actual_payout, settled_at FROM marketplace_orders WHERE platform='lazada' AND order_sn='ORDER001'"
    ).fetchone()
    assert laz[0] is None and laz[1] is None   # lazada row untouched
