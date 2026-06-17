import models

def test_detail_includes_fees_and_payout(tmp_db_conn):
    c = tmp_db_conn
    c.execute("INSERT INTO marketplace_orders (platform, order_sn) VALUES ('shopee','Z1')")
    oid = c.execute("SELECT id FROM marketplace_orders WHERE order_sn='Z1'").fetchone()['id']
    models.upsert_marketplace_fees(c, [{'order_sn':'Z1','item_value':100.0,'net_payout':80.0,
        'fee_total':20.0,'fee_commission':-12.0,'fee_service':-3.0,'fee_transaction':-2.0,
        'fee_platform':-1.0,'fee_ads_escrow':-2.0,'fee_tax':0.0,'shipping_net':0.0,
        'fee_saver':0.0,'fee_pct':'20%'}], 'f.xlsx')
    pid = c.execute("INSERT INTO marketplace_payouts (platform,deposit_date,amount,n_orders) VALUES ('shopee','2026-06-16',80.0,1)").lastrowid
    c.execute("UPDATE marketplace_orders SET payout_id=? WHERE id=?",(pid,oid)); c.commit()
    d = models.get_marketplace_order_detail(c, oid)
    assert d['fees']['fee_commission'] == -12.0
    assert d['fees']['net_payout'] == 80.0
    assert d['payout']['deposit_date'] == '2026-06-16'


def test_detail_includes_adjustments(tmp_db_conn):
    # Refunds / Seller-Balance adjustments (txn_type='adjustment') carry an
    # order_sn — surface them on the order so a refund/ปรับปรุง is visible in the
    # drill-down (income rows are NOT adjustments and must not leak in).
    c = tmp_db_conn
    c.execute("INSERT INTO marketplace_orders (platform, order_sn) VALUES ('shopee','ADJ1')")
    oid = c.execute("SELECT id FROM marketplace_orders WHERE order_sn='ADJ1'").fetchone()['id']
    models.import_wallet_txns(c, [
      {'txn_time':'2026-04-02 10:00','txn_type':'income','order_sn':'ADJ1','amount':120.0,'running_balance':120.0,'description':'income'},
      {'txn_time':'2026-04-05 23:47','txn_type':'adjustment','order_sn':'ADJ1','amount':-500.0,'running_balance':0.0,'description':'คืนเงิน/คืนสินค้าสำเร็จ'}], 'b.xlsx')
    d = models.get_marketplace_order_detail(c, oid)
    assert d['adjustments'] == [
      {'txn_time':'2026-04-05 23:47','amount':-500.0,'description':'คืนเงิน/คืนสินค้าสำเร็จ'}]


def test_detail_adjustments_empty_when_none(tmp_db_conn):
    c = tmp_db_conn
    c.execute("INSERT INTO marketplace_orders (platform, order_sn) VALUES ('shopee','NOADJ')")
    oid = c.execute("SELECT id FROM marketplace_orders WHERE order_sn='NOADJ'").fetchone()['id']
    d = models.get_marketplace_order_detail(c, oid)
    assert d['adjustments'] == []
