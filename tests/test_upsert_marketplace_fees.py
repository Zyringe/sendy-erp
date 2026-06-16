import models

def test_upsert_inserts_and_updates(tmp_db_conn):
    c = tmp_db_conn
    rows = [{'order_sn':'SN1','item_value':49.0,'net_payout':35.0,'fee_total':14.0,
             'fee_commission':-10.0,'fee_service':-1.0,'fee_transaction':-1.0,
             'fee_platform':0.0,'fee_ads_escrow':0.0,'fee_tax':0.0,'shipping_net':0.0,
             'fee_saver':-2.0,'fee_pct':'3.21%','fee_raw_json':'{}'}]
    n = models.upsert_marketplace_fees(c, rows, 'f.xlsx')
    assert n == 1
    got = c.execute("SELECT net_payout, fee_commission FROM marketplace_order_fees WHERE order_sn='SN1'").fetchone()
    assert got['net_payout'] == 35.0 and got['fee_commission'] == -10.0
    # re-run with changed net = update, not duplicate
    rows[0]['net_payout'] = 30.0
    models.upsert_marketplace_fees(c, rows, 'f2.xlsx')
    rows_db = c.execute("SELECT net_payout FROM marketplace_order_fees WHERE order_sn='SN1'").fetchall()
    assert len(rows_db) == 1 and rows_db[0]['net_payout'] == 30.0
