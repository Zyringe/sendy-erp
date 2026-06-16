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

def test_imbalanced_cycle_is_flagged_not_raised(tmp_db_conn):
    # Income != withdrawal (here income > withdrawal, a boundary straddle) must
    # NOT abort the whole reconcile — the cycle is recorded status='unbalanced'.
    c = tmp_db_conn
    _seed_orders(c, ['A','B'])
    models.import_wallet_txns(c, [
      {'txn_time':'2026-06-02 10:00','txn_type':'income','order_sn':'A','amount':50.0,'running_balance':50.0,'description':''},
      {'txn_time':'2026-06-02 11:00','txn_type':'income','order_sn':'B','amount':50.0,'running_balance':100.0,'description':''},
      {'txn_time':'2026-06-09 01:19','txn_type':'withdrawal','order_sn':None,'amount':-10.0,'running_balance':90.0,'description':'w'},
    ], 'b.xlsx')
    res = marketplace_reconcile.reconcile_payouts(c, 'shopee')   # no raise
    assert res['payouts'] == 1 and res['unbalanced'] == 1
    row = c.execute("SELECT amount, status FROM marketplace_payouts").fetchone()
    assert (row['amount'], row['status']) == (10.0, 'unbalanced')


def test_withdrawal_reversal_is_inflow_not_a_deposit(tmp_db_conn):
    # A positive-amount 'withdrawal' = a reversed/failed transfer (money back).
    # It must NOT create a deposit; its value rolls into the next real deposit.
    c = tmp_db_conn
    _seed_orders(c, ['A'])
    models.import_wallet_txns(c, [
      {'txn_time':'2026-06-02 10:00','txn_type':'income','order_sn':'A','amount':100.0,'running_balance':100.0,'description':''},
      {'txn_time':'2026-06-03 01:00','txn_type':'withdrawal','order_sn':None,'amount':40.0,'running_balance':140.0,'description':'reversal'},
      {'txn_time':'2026-06-09 01:19','txn_type':'withdrawal','order_sn':None,'amount':-140.0,'running_balance':0.0,'description':'real deposit'},
    ], 'b.xlsx')
    res = marketplace_reconcile.reconcile_payouts(c, 'shopee')
    assert res['payouts'] == 1            # only the negative withdrawal is a deposit
    row = c.execute("SELECT amount, n_orders, status FROM marketplace_payouts").fetchone()
    assert (row['amount'], row['n_orders'], row['status']) == (140.0, 1, 'reconciled')


def test_same_day_same_amount_deposits_both_recorded(tmp_db_conn):
    # Two DISTINCT bank deposits on the same day with the same amount must both
    # be recorded — no UNIQUE(date,amount) collision (prod 2025-04-01: 2× ฿3,596).
    c = tmp_db_conn
    _seed_orders(c, ['A','B'])
    models.import_wallet_txns(c, [
      {'txn_time':'2025-04-01 01:00','txn_type':'income','order_sn':'A','amount':50.0,'running_balance':50.0,'description':''},
      {'txn_time':'2025-04-01 02:00','txn_type':'withdrawal','order_sn':None,'amount':-50.0,'running_balance':0.0,'description':'d1'},
      {'txn_time':'2025-04-01 03:00','txn_type':'income','order_sn':'B','amount':50.0,'running_balance':50.0,'description':''},
      {'txn_time':'2025-04-01 04:00','txn_type':'withdrawal','order_sn':None,'amount':-50.0,'running_balance':0.0,'description':'d2'},
    ], 'b.xlsx')
    res = marketplace_reconcile.reconcile_payouts(c, 'shopee')   # no UNIQUE error
    assert res['payouts'] == 2
    rows = c.execute("SELECT deposit_date, amount FROM marketplace_payouts").fetchall()
    assert [(r['deposit_date'], r['amount']) for r in rows] == [('2025-04-01', 50.0), ('2025-04-01', 50.0)]


def test_partial_leading_cycle_flagged_unbalanced(tmp_db_conn):
    # File starts mid-cycle: the first deposit includes income from before the
    # file (income < withdrawal). Recorded, flagged unbalanced, later clean
    # cycles still reconcile.
    c = tmp_db_conn
    _seed_orders(c, ['A','B'])
    models.import_wallet_txns(c, [
      {'txn_time':'2026-01-03 10:00','txn_type':'income','order_sn':'A','amount':60.0,'running_balance':60.0,'description':''},
      {'txn_time':'2026-01-07 01:00','txn_type':'withdrawal','order_sn':None,'amount':-100.0,'running_balance':0.0,'description':'partial'},
      {'txn_time':'2026-01-10 10:00','txn_type':'income','order_sn':'B','amount':25.0,'running_balance':25.0,'description':''},
      {'txn_time':'2026-01-14 01:00','txn_type':'withdrawal','order_sn':None,'amount':-25.0,'running_balance':0.0,'description':'clean'},
    ], 'b.xlsx')
    res = marketplace_reconcile.reconcile_payouts(c, 'shopee')
    assert res['payouts'] == 2 and res['unbalanced'] == 1
    statuses = {r['amount']: r['status'] for r in
                c.execute("SELECT amount, status FROM marketplace_payouts")}
    assert statuses == {100.0: 'unbalanced', 25.0: 'reconciled'}
