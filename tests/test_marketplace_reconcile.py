import models, marketplace_reconcile

def _seed_orders(c, sns):
    c.execute("DELETE FROM marketplace_wallet_txns WHERE platform='shopee'")
    c.execute("DELETE FROM marketplace_payouts WHERE platform='shopee'")
    for sn in sns:
        c.execute("INSERT INTO marketplace_orders (platform, order_sn) VALUES ('shopee', ?)", (sn,))
    c.commit()

def test_two_cycles_assign_payouts(tmp_db_conn):
    c = tmp_db_conn
    _seed_orders(c, ['A','B','C','D'])
    wallet = [
      {'txn_time':'2026-06-02 10:00','txn_type':'income','order_sn':'A','amount':100.0,'running_balance':100.0,'description':''},
      {'txn_time':'2026-06-02 11:00','txn_type':'income','order_sn':'B','amount':50.0,'running_balance':150.0,'description':''},
      {'txn_time':'2026-06-09 01:19','txn_type':'withdrawal','order_sn':None,'amount':-150.0,'running_balance':0.0,'description':'w1'},
      {'txn_time':'2026-06-10 10:00','txn_type':'income','order_sn':'C','amount':70.0,'running_balance':70.0,'description':''},
      {'txn_time':'2026-06-12 10:00','txn_type':'income','order_sn':'D','amount':30.0,'running_balance':100.0,'description':''},
      {'txn_time':'2026-06-16 01:17','txn_type':'withdrawal','order_sn':None,'amount':-100.0,'running_balance':0.0,'description':'w2'},
    ]
    models.import_wallet_txns(c, wallet, 'bal.xlsx')
    res = marketplace_reconcile.reconcile_payouts(c, 'shopee')
    assert res['payouts'] == 2
    payouts = c.execute("SELECT deposit_date, amount, n_orders FROM marketplace_payouts ORDER BY deposit_date").fetchall()
    assert (payouts[0]['amount'], payouts[0]['n_orders']) == (150.0, 2)
    assert (payouts[1]['amount'], payouts[1]['n_orders']) == (100.0, 2)
    # orders A,B linked to payout 1; C,D to payout 2
    p2 = c.execute("SELECT id FROM marketplace_payouts WHERE deposit_date='2026-06-16'").fetchone()['id']
    linked = [r['order_sn'] for r in c.execute("SELECT order_sn FROM marketplace_orders WHERE payout_id=? ORDER BY order_sn",(p2,))]
    assert linked == ['C','D']

def test_idempotent_rerun(tmp_db_conn):
    c = tmp_db_conn
    _seed_orders(c, ['A'])
    models.import_wallet_txns(c, [
      {'txn_time':'2026-06-02 10:00','txn_type':'income','order_sn':'A','amount':10.0,'running_balance':10.0,'description':''},
      {'txn_time':'2026-06-09 01:19','txn_type':'withdrawal','order_sn':None,'amount':-10.0,'running_balance':0.0,'description':'w'},
    ], 'b.xlsx')
    marketplace_reconcile.reconcile_payouts(c, 'shopee')
    marketplace_reconcile.reconcile_payouts(c, 'shopee')
    assert c.execute("SELECT COUNT(*) FROM marketplace_payouts").fetchone()[0] == 1

def test_mismatch_raises_when_income_exceeds_withdrawal(tmp_db_conn):
    # income > withdrawal is the genuine corruption case (duplicate rows etc.)
    # income < withdrawal is valid partial-historical (pre-range carry-over) — not an error
    c = tmp_db_conn
    _seed_orders(c, ['A','B'])
    models.import_wallet_txns(c, [
      {'txn_time':'2026-06-02 10:00','txn_type':'income','order_sn':'A','amount':50.0,'running_balance':50.0,'description':''},
      {'txn_time':'2026-06-02 11:00','txn_type':'income','order_sn':'B','amount':50.0,'running_balance':100.0,'description':''},
      {'txn_time':'2026-06-09 01:19','txn_type':'withdrawal','order_sn':None,'amount':-10.0,'running_balance':90.0,'description':'w'},
    ], 'b.xlsx')
    import pytest
    with pytest.raises(marketplace_reconcile.ReconcileError):
        marketplace_reconcile.reconcile_payouts(c, 'shopee')
