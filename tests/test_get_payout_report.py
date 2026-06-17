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


def test_payout_report_year_filter(tmp_db_conn):
    # Deposits across 2025 + 2026 must all be reachable — no silent cap. The
    # year filter scopes the view; get_payout_years lists the available years.
    c = tmp_db_conn
    c.execute("DELETE FROM marketplace_wallet_txns WHERE platform='shopee'")
    c.execute("DELETE FROM marketplace_payouts WHERE platform='shopee'")
    c.execute("UPDATE marketplace_orders SET payout_id=NULL WHERE platform='shopee'")
    c.commit()
    models.import_wallet_txns(c, [
      {'txn_time':'2025-03-01 10:00','txn_type':'income','order_sn':None,'amount':500.0,'running_balance':500.0,'description':''},
      {'txn_time':'2025-03-07 01:00','txn_type':'withdrawal','order_sn':None,'amount':-500.0,'running_balance':0.0,'description':'w25'},
      {'txn_time':'2026-03-01 10:00','txn_type':'income','order_sn':None,'amount':300.0,'running_balance':300.0,'description':''},
      {'txn_time':'2026-03-07 01:00','txn_type':'withdrawal','order_sn':None,'amount':-300.0,'running_balance':0.0,'description':'w26'},
    ], 'b.xlsx')
    marketplace_reconcile.reconcile_payouts(c, 'shopee')

    assert models.get_payout_years(c, 'shopee') == ['2026', '2025']          # newest first
    assert len(models.get_payout_report(c, 'shopee')) == 2                    # all years
    only25 = models.get_payout_report(c, 'shopee', year='2025')
    assert len(only25) == 1 and only25[0]['amount'] == 500.0                  # 2025 reachable
    only26 = models.get_payout_report(c, 'shopee', year='2026')
    assert len(only26) == 1 and only26[0]['amount'] == 300.0


def _reset_shopee(c):
    c.execute("DELETE FROM marketplace_wallet_txns WHERE platform='shopee'")
    c.execute("DELETE FROM marketplace_payouts WHERE platform='shopee'")
    c.execute("DELETE FROM marketplace_order_fees WHERE platform='shopee'")
    c.execute("UPDATE marketplace_orders SET payout_id=NULL WHERE platform='shopee'")
    c.commit()


def test_payout_report_order_carries_id_for_drilldown(tmp_db_conn):
    # Every order row must expose its `id` so the deposits tab can wire the
    # fee-breakdown drill-down modal (data-order-id).
    c = tmp_db_conn
    _reset_shopee(c)
    c.execute("INSERT INTO marketplace_orders (platform, order_sn) VALUES ('shopee','ID1')")
    models.upsert_marketplace_fees(c, [
      {'order_sn':'ID1','item_value':60.0,'net_payout':50.0,'fee_total':10.0}], 'f.xlsx')
    models.import_wallet_txns(c, [
      {'txn_time':'2026-06-02 10:00','txn_type':'income','order_sn':'ID1','amount':50.0,'running_balance':50.0,'description':''},
      {'txn_time':'2026-06-09 01:19','txn_type':'withdrawal','order_sn':None,'amount':-50.0,'running_balance':0.0,'description':'w'}], 'b.xlsx')
    marketplace_reconcile.reconcile_payouts(c, 'shopee')
    o = models.get_payout_report(c, 'shopee')[0]['orders'][0]
    assert o['id'] == c.execute("SELECT id FROM marketplace_orders WHERE order_sn='ID1'").fetchone()['id']
    assert o['fee_source'] == 'settled'      # has an Income fees row


def test_payout_report_falls_back_to_wallet_net_when_no_income(tmp_db_conn):
    # User uploaded ONLY Order + Balance (no Income file). The order has NO
    # marketplace_order_fees row, but the wallet ledger carries the true net.
    # The 3 columns must still fill: net = wallet net, item_value = order item_total,
    # fee_total = item_total - net (estimate), tagged 'wallet'.
    c = tmp_db_conn
    _reset_shopee(c)
    c.execute("INSERT INTO marketplace_orders (platform, order_sn, item_total) VALUES ('shopee','W1', 100.0)")
    models.import_wallet_txns(c, [
      {'txn_time':'2026-06-02 10:00','txn_type':'income','order_sn':'W1','amount':82.0,'running_balance':82.0,'description':''},
      {'txn_time':'2026-06-09 01:19','txn_type':'withdrawal','order_sn':None,'amount':-82.0,'running_balance':0.0,'description':'w'}], 'b.xlsx')
    marketplace_reconcile.reconcile_payouts(c, 'shopee')
    o = models.get_payout_report(c, 'shopee')[0]['orders'][0]
    assert o['order_sn'] == 'W1'
    assert o['net_payout'] == 82.0           # wallet net (actual_payout is NULL without Income)
    assert o['item_value'] == 100.0          # from Order export item_total
    assert o['fee_total'] == 18.0            # estimate = item_total - wallet net
    assert o['fee_source'] == 'wallet'
    assert 'id' in o
