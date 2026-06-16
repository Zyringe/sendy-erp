import os, pytest, models, marketplace_reconcile
from parse_balance import load_balance_sheet, parse_shopee_balance

F = os.path.expanduser("~/Downloads/my_balance_transaction_report.shopee.20260101_20260616.xlsx")

@pytest.mark.skipif(not os.path.exists(F), reason="real balance file not present")
def test_7689_and_5890_cycles(tmp_db_conn):
    c = tmp_db_conn
    c.execute("DELETE FROM marketplace_wallet_txns"); c.commit()
    models.import_wallet_txns(c, parse_shopee_balance(load_balance_sheet(F)), "real.xlsx")
    marketplace_reconcile.reconcile_payouts(c, 'shopee')
    def n(amt): return c.execute("SELECT n_orders FROM marketplace_payouts WHERE ABS(amount-?)<0.01",(amt,)).fetchone()['n_orders']
    assert n(7689.0) == 39
    assert n(5890.0) == 32
