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


def test_detail_includes_fee_lines_for_tooltip(tmp_db_conn):
    """The drill-down modal shows the detailed ค่าธรรมเนียม as a hover tooltip, fed
    by fee_lines (item value first, each non-zero Thai-category fee, Σ == net_payout)
    — the SAME breakdown the settlement page tooltip uses."""
    c = tmp_db_conn
    c.execute("INSERT INTO marketplace_orders (platform, order_sn) VALUES ('shopee','FL1')")
    oid = c.execute("SELECT id FROM marketplace_orders WHERE order_sn='FL1'").fetchone()['id']
    models.upsert_marketplace_fees(c, [{'order_sn':'FL1','item_value':100.0,'net_payout':80.0,
        'fee_total':20.0,'fee_commission':-12.0,'fee_service':-3.0,'fee_transaction':-2.0,
        'fee_platform':-1.0,'fee_ads_escrow':-2.0,'fee_tax':0.0,'shipping_net':0.0,
        'fee_saver':0.0,'fee_pct':'20%'}], 'f.xlsx')
    c.commit()
    d = models.get_marketplace_order_detail(c, oid)
    fl = d['fee_lines']
    assert fl[0] == {'label':'มูลค่าสินค้า','amount':100.0}
    assert {'label':'ค่าคอมมิชชั่น','amount':-12.0} in fl
    assert round(sum(x['amount'] for x in fl), 2) == 80.0          # Σ == net_payout


def test_detail_lazada_smart_label_shows_lazcoins(tmp_db_conn):
    """The modal's fee tooltip uses the same smart label: a single-source LazCoins
    promo shows 'ส่วนลด LazCoins', not the generic bucket."""
    import json
    c = tmp_db_conn
    c.execute("INSERT INTO marketplace_orders (platform, order_sn) VALUES ('lazada','LZM1')")
    oid = c.execute("SELECT id FROM marketplace_orders WHERE order_sn='LZM1'").fetchone()['id']
    models.upsert_marketplace_fees(c, [{'order_sn':'LZM1','item_value':100.0,'net_payout':80.0,
        'fee_total':20.0,'fee_commission':-10.0,'fee_ads_escrow':-10.0,
        'fee_raw_json': json.dumps({'Item Price Credit':100.0,'Commission':-10.0,
                                    'LazCoins Discount':-10.0}, ensure_ascii=False)}],
        'lz.xlsx', platform='lazada')
    c.commit()
    d = models.get_marketplace_order_detail(c, oid)
    labels = {x['label'] for x in d['fee_lines']}
    assert 'ส่วนลด LazCoins' in labels
    assert 'fee_raw_json' not in d['fees']     # raw row must not leak to the client


def test_detail_fee_lines_none_without_settlement(tmp_db_conn):
    c = tmp_db_conn
    c.execute("INSERT INTO marketplace_orders (platform, order_sn) VALUES ('shopee','NOFEE')")
    oid = c.execute("SELECT id FROM marketplace_orders WHERE order_sn='NOFEE'").fetchone()['id']
    d = models.get_marketplace_order_detail(c, oid)
    assert d['fee_lines'] is None       # no Income breakdown → no tooltip, estimate badge instead


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
