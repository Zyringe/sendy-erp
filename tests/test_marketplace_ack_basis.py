"""The acknowledgement a manager gives on /marketplace/settlement must survive
the next page load.

THE DEFECT THIS PINS
    `reconcile_orders()` decides whether an order still shows as a discrepancy by
    recomputing `d_bill = billed - billed_basis`, where `billed_basis` is
    Lazada's `COALESCE(item_value, item_total, actual_payout)` but Shopee's plain
    `actual_payout`. `set_amount_review()` stored `billed - actual_payout` for
    BOTH platforms. The read path then requires the stored and recomputed values
    to agree within a satang before it treats the order as reviewed, so on Lazada
    the acknowledgement could never match and the row re-flagged forever.

    It stayed invisible because Shopee's basis IS actual_payout, so the two
    agreed there by accident — and every one of the 29 acknowledgements on prod
    (2026-08-24) is Shopee. Measured on the same snapshot: 1,706 of 1,707 settled
    Lazada orders have a basis that differs from actual_payout, so the bug was
    latent on essentially the whole platform, waiting for the first Lazada
    acknowledgement.

WHY THE TEST IS SHAPED THIS WAY
    Asserting a hardcoded d_bill would pass while both sides drift together. The
    assertion that matters is the ROUND TRIP: acknowledge, then re-read through
    the real reconcile path and require `reviewed is True`. That is the property
    the operator actually depends on, and it cannot be satisfied by two
    independently-wrong formulas.

    Both platforms are exercised in the same run: Shopee is the CONTROL. It
    passed before the fix and must still pass after, which is what proves the
    fix did not simply move the divergence to the other platform.
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest


PREFIX = 'ACKBASIS'


@pytest.fixture
def ack_conn(tmp_db_conn):
    """One settled order per platform, each linked to an IV whose billed amount
    differs from BOTH the payout and the Lazada item_value — so the order is a
    genuine discrepancy on either basis and the two bases are distinguishable.

    ⚠ Rows are force-deleted first: tmp_db clones the live DB WITH its data, so a
    fixture that only inserts is sitting on real history (conftest.py::tmp_db).
    """
    c = tmp_db_conn
    for sql in (
        "DELETE FROM marketplace_amount_review WHERE order_sn LIKE ?",
        "DELETE FROM marketplace_order_invoice WHERE order_sn LIKE ?",
        "DELETE FROM marketplace_order_fees WHERE order_sn LIKE ?",
        "DELETE FROM marketplace_orders WHERE order_sn LIKE ?",
    ):
        c.execute(sql, (PREFIX + '%',))
    c.execute("DELETE FROM sales_transactions WHERE doc_base LIKE 'IV94%'")

    pid = c.execute(
        "INSERT INTO products (product_name) VALUES ('ACK basis test product')").lastrowid

    def _sale(doc_base, net):
        # vat_type 1 keeps billed == net, so the arithmetic in the assertions is
        # visible rather than hidden behind a 1.07 the test would have to mirror.
        c.execute(
            """INSERT INTO sales_transactions
                   (date_iso, doc_no, doc_base, product_id, customer, customer_code,
                    qty, unit_price, vat_type, total, net, synced_to_stock)
               VALUES ('2026-06-01', ?, ?, ?, ?, ?, 1, ?, 1, ?, ?, 0)""",
            (doc_base + '-1', doc_base, pid,
             'หน้าร้านL' if doc_base.endswith('L') else 'หน้าร้านS',
             'L01' if doc_base.endswith('L') else 'S01', net, net, net))

    def _order(platform, order_sn, doc_base, payout, item_value=None):
        oid = c.execute(
            """INSERT INTO marketplace_orders
                   (platform, order_sn, status, order_date, actual_payout,
                    settled_at, currency)
               VALUES (?, ?, 'completed', '2026-06-01', ?, '2026-06-05', 'THB')""",
            (platform, order_sn, payout)).lastrowid
        c.execute(
            """INSERT INTO marketplace_order_invoice
                   (platform, order_sn, doc_base, match_method, confidence)
               VALUES (?, ?, ?, 'manual', 'confident')""",
            (platform, order_sn, doc_base))
        if item_value is not None:
            c.execute(
                """INSERT INTO marketplace_order_fees (platform, order_sn, item_value)
                   VALUES (?, ?, ?)""", (platform, order_sn, item_value))
        return oid

    # Lazada: payout 90, item_value 100 -> the two bases disagree by 10.
    #         billed 120 -> a discrepancy on either basis (30 vs 20), so the row
    #         is flagged regardless and only the STORED value can differ.
    _sale('IV9400001L', 120.0)
    laz = _order('lazada', PREFIX + 'LAZ', 'IV9400001L', payout=90.0, item_value=100.0)

    # Shopee CONTROL: basis IS actual_payout, so both paths already agreed.
    _sale('IV9400002S', 120.0)
    shp = _order('shopee', PREFIX + 'SHP', 'IV9400002S', payout=90.0)

    c.commit()
    return c, laz, shp


def _row(conn, platform, order_sn):
    """The order as the settlement page actually renders it — through the real
    reconciliation, not a re-implementation of its arithmetic."""
    import models.marketplace as mp
    rep = mp.get_marketplace_reconciliation(conn, platform)
    for month in rep['months']:
        for r in month['orders']:
            if r.get('order_sn') == order_sn:
                return r
    return None


def test_lazada_acknowledgement_survives_a_reload(ack_conn):
    """THE BUG: acknowledge a Lazada discrepancy, reload, and it must stay
    acknowledged. Before the fix the stored d_bill was 120-90=30 while the page
    recomputed 120-100=20, so `reviewed` came back False forever."""
    import models.marketplace as mp
    conn, laz, _shp = ack_conn

    before = _row(conn, 'lazada', PREFIX + 'LAZ')
    assert before is not None, 'CONTROL: the fixture order must reach the page at all'
    assert before['amount_mismatch'] is True, 'CONTROL: it must be a discrepancy to review'
    assert before['reviewed'] is False, 'CONTROL: nothing acknowledged yet'

    res = mp.set_amount_review(conn, laz, accept=True, reviewed_by='test')
    assert res and res.get('accepted') is True

    after = _row(conn, 'lazada', PREFIX + 'LAZ')
    assert after['reviewed'] is True, (
        'acknowledged Lazada order re-flagged: stored d_bill=%r vs page d_bill=%r'
        % (res.get('d_bill'), after['d_bill']))


def test_shopee_acknowledgement_still_survives(ack_conn):
    """CONTROL for the fix, not for the bug. Shopee agreed before because its
    basis IS actual_payout; if the fix broke this, it merely moved the defect."""
    import models.marketplace as mp
    conn, _laz, shp = ack_conn

    before = _row(conn, 'shopee', PREFIX + 'SHP')
    assert before is not None and before['amount_mismatch'] is True
    mp.set_amount_review(conn, shp, accept=True, reviewed_by='test')
    after = _row(conn, 'shopee', PREFIX + 'SHP')
    assert after['reviewed'] is True


def test_stored_d_bill_equals_the_page_value_on_both_platforms(ack_conn):
    """Pins the NUMBER as well as the outcome.

    `reviewed` is a tolerance comparison (<0.01), so a future change could keep
    it True while the stored value drifts. Asserting equality of the two d_bills
    is what stops the write path from re-inventing either half of the formula —
    the basis OR the VAT-aware billed sum.
    """
    import models.marketplace as mp
    conn, laz, shp = ack_conn

    for order_id, platform, order_sn in ((laz, 'lazada', PREFIX + 'LAZ'),
                                         (shp, 'shopee', PREFIX + 'SHP')):
        res = mp.set_amount_review(conn, order_id, accept=True, reviewed_by='test')
        page = _row(conn, platform, order_sn)
        assert abs(res['d_bill'] - page['d_bill']) < 0.005, (
            '%s: stored %r != page %r' % (platform, res['d_bill'], page['d_bill']))

    # And they must not be the SAME number on both platforms — if they were, the
    # fixture would not be distinguishing the two bases and every assertion above
    # would pass for the wrong reason.
    laz_row = _row(conn, 'lazada', PREFIX + 'LAZ')
    shp_row = _row(conn, 'shopee', PREFIX + 'SHP')
    assert abs(laz_row['d_bill'] - shp_row['d_bill']) > 0.005, (
        'CONTROL: the two platforms must differ, else the test cannot see a basis bug')
