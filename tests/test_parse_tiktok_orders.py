"""TikTok Shop order export (ทั้งหมด คำสั่งซื้อ-*.csv) -> parse_orders.parse_tiktok_orders.

Plan: projects/tiktok-order-import/tiktok-order-import-plan.md, PR-1 step 2.
The fixture is the real 2026-09-30 export (3 orders on 4 lines, buyer fields
already masked by TikTok; Buyer Username and Tracking ID scrubbed further).
Money oracle = the plan's "Money facts" table: buyer paid 405 / 119 / 139.04.
"""
import io
import os

import pandas as pd
import pytest

from parse_orders import parse_tiktok_orders

FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'tiktok', 'tiktok_orders_sample.csv')

COMPLETED = '585884671861360379'
COMPLETED_2 = '585884215723460543'
CANCELLED = '585883444661159817'


def _df():
    return pd.read_csv(FIXTURE, dtype=str, keep_default_na=False, encoding='utf-8-sig')


@pytest.fixture
def orders():
    return {o['order_sn']: o for o in parse_tiktok_orders(_df())}


def test_three_orders_four_lines(orders):
    assert sorted(orders) == sorted([COMPLETED, COMPLETED_2, CANCELLED])
    assert [len(orders[sn]['items']) for sn in (COMPLETED, COMPLETED_2, CANCELLED)] == [1, 1, 2]
    assert all(o['platform'] == 'tiktok' for o in orders.values())


def test_ids_lose_the_trailing_tab(orders):
    o = orders[CANCELLED]
    assert [it['variation_id'] for it in o['items']] == ['1736984084285458104', '1736984084285654712']
    assert [it['line_key'] for it in o['items']] == ['1736984084285458104', '1736984084285654712']


def test_money_is_buyer_paid(orders):
    assert orders[COMPLETED]['item_total'] == 405
    assert orders[COMPLETED_2]['item_total'] == 119
    assert orders[CANCELLED]['item_total'] == pytest.approx(139.04, abs=0.001)
    it = orders[COMPLETED]['items'][0]
    assert (it['qty'], it['unit_price'], it['item_subtotal']) == (1.0, 460.0, 405.0)
    for o in orders.values():
        assert o['marketplace_fee'] is None and o['payout'] is None


def test_dates_keep_the_seconds(orders):
    o = orders[COMPLETED]
    assert o['order_date'] == '2026-09-04 12:45:05'
    assert o['paid_date'] == '2026-09-05 12:00:27'


def test_status_stored_as_is(orders):
    assert orders[COMPLETED]['status'] == 'เสร็จสมบูรณ์'
    assert orders[CANCELLED]['status'] == 'ยกเลิกแล้ว'


def test_masked_buyer_fields_become_none(orders):
    o = orders[COMPLETED]
    assert o['buyer_name'] is None
    assert o['buyer_phone'] is None
    # Only the masked parts drop out; the district/province TikTok leaves readable stay.
    assert o['ship_address'] == 'Kamphaeng Saen Nakhon Pathom'


def test_unmasked_name_is_kept():
    df = _df()
    df.loc[0, 'Recipient'] = 'สมชาย ใจดี'
    o = {x['order_sn']: x for x in parse_tiktok_orders(df)}[COMPLETED]
    assert o['buyer_name'] == 'สมชาย ใจดี'


def test_carried_fields_for_later_prs(orders):
    it = orders[COMPLETED]['items'][0]
    assert (it['platform_discount'], it['seller_discount']) == (30.0, 25.0)
    c = orders[CANCELLED]
    assert c['items'][0]['qty_returned'] == 1.0
    assert c['items'][0]['return_type'] == 'Cancel'
    assert c['order_amount'] == pytest.approx(139.04)
    # Order Refund Amount is per SKU line (69.52 on each of the 2 lines); the
    # order's refund is their sum = the income file's full refund, 139.04.
    assert [it['refund_amount'] for it in c['items']] == [69.52, 69.52]
    assert c['refund_amount'] == pytest.approx(139.04)
    assert orders[COMPLETED]['refund_amount'] is None
    assert c['cancelled_time'] == '2026-09-04 11:56:50'
    assert c['payment_method'] == 'PayLater + TikTok Shop Balance'
    assert c['order_channel'] == 'Product cards'
    assert c['creator_handle'] is None
    assert [it['creator_handle'] for it in c['items']] == [None, None]
    assert orders[COMPLETED]['cancelled_time'] is None


def test_repeated_sku_in_one_order_aggregates_into_one_line():
    """UNIQUE(platform, order_sn, line_key) on marketplace_order_items: two rows
    for the same SKU ID must become one line, qty and subtotal summed."""
    df = _df()
    dup = df.iloc[[0]].copy()
    df = pd.concat([df, dup], ignore_index=True)
    o = {x['order_sn']: x for x in parse_tiktok_orders(df)}[COMPLETED]
    assert len(o['items']) == 1
    it = o['items'][0]
    assert (it['qty'], it['item_subtotal']) == (2.0, 810.0)
    assert o['item_total'] == 810.0
    assert it['platform_discount'] == 60.0


def test_creator_handle_is_carried_per_line():
    """Affiliate attribution can differ per SKU line; the order-level value is
    only the first one seen."""
    df = _df()
    df.loc[2, 'Creator Handle'] = 'creator_a'
    df.loc[3, 'Creator Handle'] = 'creator_b'
    c = {x['order_sn']: x for x in parse_tiktok_orders(df)}[CANCELLED]
    assert [it['creator_handle'] for it in c['items']] == ['creator_a', 'creator_b']
    assert c['creator_handle'] == 'creator_a'


def test_missing_required_column_names_what_it_saw():
    df = _df().drop(columns=['SKU ID'])
    with pytest.raises(ValueError) as e:
        parse_tiktok_orders(df)
    assert 'SKU ID' in str(e.value) and 'Order Status' in str(e.value)


@pytest.mark.parametrize('col,bad', [('Order ID', '58588467186136037X\t'),
                                     ('SKU ID', '1737 136796\t'),
                                     ('SKU ID', '๑๗๓๗\t'),          # Thai digits: isdigit() says yes
                                     ('Order ID', '585884²\t')])     # superscript: isdigit() says yes
def test_non_numeric_ids_refused(col, bad):
    df = _df()
    df.loc[0, col] = bad
    with pytest.raises(ValueError):
        parse_tiktok_orders(df)


@pytest.mark.parametrize('col', ['Created Time', 'Paid Time', 'Cancelled Time'])
def test_unparseable_date_refused_never_returned_raw(col):
    df = _df()
    df.loc[0, col] = '2026-09-04T12:45\t'
    with pytest.raises(ValueError):
        parse_tiktok_orders(df)


def test_blank_date_is_none():
    df = _df()
    df.loc[0, 'Paid Time'] = '\t'
    o = {x['order_sn']: x for x in parse_tiktok_orders(df)}[COMPLETED]
    assert o['paid_date'] is None
