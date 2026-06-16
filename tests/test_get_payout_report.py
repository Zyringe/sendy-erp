import models, marketplace_reconcile

def test_payout_report_groups_orders_with_fees(tmp_db_conn):
    c = tmp_db_conn
    c.execute("DELETE FROM marketplace_wallet_txns"); c.commit()
    c.execute("INSERT INTO marketplace_orders (platform, order_sn) VALUES ('shopee','A')")
    c.execute("INSERT INTO marketplace_orders (platform, order_sn) VALUES ('shopee','B')")
    models.upsert_marketplace_fees(c, [
      {'order_sn':'A','item_value':60.0,'net_payout':50.0,'fee_total':10.0},
      {'order_sn':'B','item_value':30.0,'net_payout':25.0,'fee_total':5.0}], 'f.xlsx')
    models.import_wallet_txns(c, [
      {'txn_time':'2026-06-02 10:00','txn_type':'income','order_sn':'A','amount':50.0,'running_balance':50.0,'description':''},
      {'txn_time':'2026-06-02 11:00','txn_type':'income','order_sn':'B','amount':25.0,'running_balance':75.0,'description':''},
      {'txn_time':'2026-06-09 01:19','txn_type':'withdrawal','order_sn':None,'amount':-75.0,'running_balance':0.0,'description':'w'}], 'b.xlsx')
    marketplace_reconcile.reconcile_payouts(c, 'shopee')
    rep = models.get_payout_report(c, 'shopee')
    assert len(rep) == 1
    d = rep[0]
    assert d['amount'] == 75.0 and d['n_orders'] == 2
    assert round(d['fee_total'], 2) == 15.0
    assert {o['order_sn'] for o in d['orders']} == {'A','B'}
