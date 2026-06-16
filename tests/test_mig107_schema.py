def test_new_tables_and_column(tmp_db_conn):
    c = tmp_db_conn
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'marketplace_order_fees','marketplace_wallet_txns','marketplace_payouts'} <= names
    cols = {r[1] for r in c.execute("PRAGMA table_info(marketplace_orders)")}
    assert 'payout_id' in cols
