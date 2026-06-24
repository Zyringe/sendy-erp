import models, marketplace_reconcile


def test_lazada_income_reanchored_to_statement_settlement_time(tmp_db_conn):
    # Lazada income carries a DATE-ONLY release date that is occasionally off-by-one
    # vs the actual ~2am settlement, mis-grouping it across deposit cycles. Order B's
    # release date (06-08) would group it with A into the first deposit (→ 150 vs 100,
    # unbalanced), but its statement S2 actually settled 06-09 (after the 06-08
    # withdrawal). Re-anchoring by lazada_statement_settlement.settled_at must move B
    # into the second deposit so BOTH cycles reconcile.
    c = tmp_db_conn
    c.execute("DELETE FROM marketplace_wallet_txns")
    c.execute("DELETE FROM marketplace_payouts")
    c.execute("DELETE FROM lazada_statement_settlement")
    for sn in ('A', 'B'):
        c.execute("INSERT INTO marketplace_orders (platform, order_sn) VALUES ('lazada', ?)", (sn,))
    models.import_wallet_txns(c, [
      {'txn_time':'2026-06-08','txn_type':'income','order_sn':'A','amount':100.0,'running_balance':None,'description':'S1'},
      {'txn_time':'2026-06-08','txn_type':'income','order_sn':'B','amount':50.0,'running_balance':None,'description':'S2'},
      {'txn_time':'2026-06-08 10:00:00','txn_type':'withdrawal','order_sn':None,'amount':-100.0,'running_balance':None,'description':'Bank Ref. X-0608'},
      {'txn_time':'2026-06-15 10:00:00','txn_type':'withdrawal','order_sn':None,'amount':-50.0,'running_balance':None,'description':'Bank Ref. X-0615'},
    ], 'bal.csv', platform='lazada')
    c.executemany("INSERT INTO lazada_statement_settlement (statement, settled_at, amount) VALUES (?,?,?)",
                  [('S1','2026-06-08 02:00:00',100.0), ('S2','2026-06-09 02:00:00',50.0)])
    c.commit()
    res = marketplace_reconcile.reconcile_payouts(c, 'lazada')
    assert res['payouts'] == 2
    assert res['unbalanced'] == 0          # both cycles balance after re-anchoring
    rows = c.execute("""SELECT deposit_date, amount, n_orders, status FROM marketplace_payouts
                        WHERE platform='lazada' ORDER BY deposit_date""").fetchall()
    assert [(r['amount'], r['n_orders'], r['status']) for r in rows] == [
        (100.0, 1, 'reconciled'), (50.0, 1, 'reconciled')]
    p2 = c.execute("SELECT id FROM marketplace_payouts WHERE deposit_date='2026-06-15'").fetchone()['id']
    linked = [r['order_sn'] for r in c.execute("SELECT order_sn FROM marketplace_orders WHERE payout_id=?", (p2,))]
    assert linked == ['B']

def _seed_orders(c, sns):
    # The fixture copies the live DB, which carries real lazada+shopee
    # marketplace data. These tests assert on UNSCOPED marketplace_payouts
    # queries (COUNT(*), full SELECTs ORDER BY date), so the table must be
    # globally empty before each shopee reconcile — clearing only
    # platform='shopee' leaks the 128 real lazada payout rows into assertions.
    c.execute("DELETE FROM marketplace_wallet_txns")
    c.execute("DELETE FROM marketplace_payouts")
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

def test_lazada_cycle_total_from_settlement_not_order_income(tmp_db_conn):
    # The statement per-order income carries refund-timing noise vs the wallet, so
    # the cycle must total from the authoritative SETTLEMENT amount. รอบบิล S1
    # settled 200 but only 100 of order income is tagged to it (a refund landed in a
    # different รอบบิล in the statement) — settlement-driven reconcile must still
    # balance the 200 deposit and link the order via its รอบบิล.
    c = tmp_db_conn
    c.execute("DELETE FROM marketplace_wallet_txns")
    c.execute("DELETE FROM marketplace_payouts")
    c.execute("DELETE FROM lazada_statement_settlement")
    c.execute("INSERT INTO marketplace_orders (platform, order_sn) VALUES ('lazada','A')")
    models.import_wallet_txns(c, [
      {'txn_time':'2026-06-09','txn_type':'income','order_sn':'A','amount':100.0,'running_balance':None,'description':'S1'},
      {'txn_time':'2026-06-15 10:00:00','txn_type':'withdrawal','order_sn':None,'amount':-200.0,'running_balance':None,'description':'Bank Ref. X'},
    ], 'bal.csv', platform='lazada')
    c.execute("INSERT INTO lazada_statement_settlement (statement, settled_at, amount) VALUES ('S1','2026-06-09 02:00:00',200.0)")
    c.commit()
    res = marketplace_reconcile.reconcile_payouts(c, 'lazada')
    assert res['unbalanced'] == 0
    p = c.execute("SELECT amount, n_orders, status FROM marketplace_payouts WHERE platform='lazada'").fetchone()
    assert (p['amount'], p['status']) == (200.0, 'reconciled')
    assert c.execute("SELECT payout_id FROM marketplace_orders WHERE order_sn='A'").fetchone()['payout_id'] is not None


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
