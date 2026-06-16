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
