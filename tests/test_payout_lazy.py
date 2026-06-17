"""Lazy-load split of the deposits tab: deposit summaries (no order rows) +
per-deposit order fetch. The page renders only summaries; orders load on expand.
get_payout_report still composes both so its contract is unchanged.
"""
import models, marketplace_reconcile


def _seed_one_deposit(c):
    c.execute("DELETE FROM marketplace_wallet_txns WHERE platform='shopee'")
    c.execute("DELETE FROM marketplace_payouts WHERE platform='shopee'")
    c.execute("DELETE FROM marketplace_order_fees WHERE platform='shopee'")
    c.execute("UPDATE marketplace_orders SET payout_id=NULL WHERE platform='shopee'")
    c.execute("DELETE FROM marketplace_orders WHERE order_sn IN ('LZ1','LZ2')")
    c.execute("INSERT INTO marketplace_orders (platform, order_sn) VALUES ('shopee','LZ1')")
    c.execute("INSERT INTO marketplace_orders (platform, order_sn, item_total) VALUES ('shopee','LZ2', 100.0)")
    models.upsert_marketplace_fees(c, [{'order_sn':'LZ1','item_value':60.0,'net_payout':50.0,'fee_total':10.0}], 'f.xlsx')
    models.import_wallet_txns(c, [
      {'txn_time':'2026-06-02 10:00','txn_type':'income','order_sn':'LZ1','amount':50.0,'running_balance':50.0,'description':''},
      {'txn_time':'2026-06-02 11:00','txn_type':'income','order_sn':'LZ2','amount':82.0,'running_balance':132.0,'description':''},
      {'txn_time':'2026-06-09 01:19','txn_type':'withdrawal','order_sn':None,'amount':-132.0,'running_balance':0.0,'description':'w'}], 'b.xlsx')
    marketplace_reconcile.reconcile_payouts(c, 'shopee')
    c.commit()


def test_summaries_have_fee_total_but_no_order_rows(tmp_db_conn):
    c = tmp_db_conn
    _seed_one_deposit(c)
    s = models.get_payout_summaries(c, 'shopee')
    assert len(s) == 1
    d = s[0]
    assert d['n_orders'] == 2 and d['amount'] == 132.0
    # fee_total = LZ1 settled 10 + LZ2 estimate (item_total 100 − wallet 82) = 18 → 28
    assert d['fee_total'] == 28.0
    assert 'orders' not in d            # the whole point: no per-order rows in the summary


def test_payout_orders_returns_rows_with_id_and_source(tmp_db_conn):
    c = tmp_db_conn
    _seed_one_deposit(c)
    pid = models.get_payout_summaries(c, 'shopee')[0]['id']
    orders = models.get_payout_orders(c, 'shopee', pid)
    by_sn = {o['order_sn']: o for o in orders}
    assert set(by_sn) == {'LZ1', 'LZ2'}
    assert by_sn['LZ1']['fee_source'] == 'settled' and 'id' in by_sn['LZ1']
    assert by_sn['LZ2']['fee_source'] == 'wallet' and by_sn['LZ2']['net_payout'] == 82.0


def test_get_payout_report_still_composes_summary_plus_orders(tmp_db_conn):
    c = tmp_db_conn
    _seed_one_deposit(c)
    rep = models.get_payout_report(c, 'shopee')
    assert len(rep) == 1
    assert rep[0]['fee_total'] == 28.0
    assert {o['order_sn'] for o in rep[0]['orders']} == {'LZ1', 'LZ2'}


def test_deposits_page_lazy_and_api_serves_rows(tmp_db_conn):
    """Deposits tab renders deposit cards but NOT the order rows (lazy); the new
    API serves a deposit's rows on demand."""
    import os
    os.environ.setdefault('SKIP_DB_INIT', '1')
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = tmp_db_conn
    _seed_one_deposit(c)
    pid = models.get_payout_summaries(c, 'shopee')[0]['id']
    cl = flask_app.test_client()
    with cl.session_transaction() as s:
        s['user_id'] = 4; s['username'] = 'x'; s['role'] = 'staff'
    page = cl.get('/marketplace/settlement?platform=shopee&tab=deposits&year=all').get_data(as_text=True)
    assert f'data-payout-id="{pid}"' in page        # deposit card rendered
    assert 'LZ1' not in page and 'LZ2' not in page   # order rows NOT inlined (lazy)
    api = cl.get(f'/marketplace/api/payout/{pid}/orders?platform=shopee').get_json()
    assert {o['order_sn'] for o in api['orders']} == {'LZ1', 'LZ2'}
