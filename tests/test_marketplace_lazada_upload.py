import os
os.environ.setdefault('SKIP_DB_INIT', '1')
import models, marketplace_reconcile
from parse_lazada_statement import parse_lazada_statement
from parse_lazada_wallet import parse_lazada_wallet
import pandas as pd

SCOLS = ['Statement Period','Statement Number','Transaction Date','Fee Name',
        'Amount(Include Tax)','VAT Amount','Release Status','Release Date','Comment',
        'Order Creation Date','Order Number','Order Line ID','Seller SKU','Lazada SKU',
        'WHT Amount','WHT included in Amount','Order Status','Product Name','Short Code']
WCOLS = ['Transaction Number','Transaction Time','Type','Sub Type','Amount','Remarks']

def _srow(stmt, fee, amt, order, rel):
    d = {c:'' for c in SCOLS}
    d.update({'Statement Number':stmt,'Fee Name':fee,'Amount(Include Tax)':amt,
              'Release Date':rel,'Order Number':order,'Order Creation Date':rel,
              'Order Status':'Confirmed','Product Name':'P','Seller SKU':'SK'})
    return d

def test_full_lazada_import_links_orders_to_deposit(tmp_db_conn):
    c = tmp_db_conn
    c.execute("DELETE FROM marketplace_wallet_txns WHERE platform='lazada'")
    c.execute("DELETE FROM marketplace_payouts WHERE platform='lazada'")
    # seed an order row so settlement can stamp it
    c.execute("""INSERT INTO marketplace_orders (platform, order_sn, status, order_date, currency)
                 VALUES ('lazada','ORDX','Confirmed','2026-06-10 10:00','THB')""")
    c.commit()
    sdf = pd.DataFrame([_srow('S-0615','Item Price Credit','100','ORDX','16 Jun 2026'),
                        _srow('S-0615','Commission','-10','ORDX','16 Jun 2026')], columns=SCOLS)
    p = parse_lazada_statement(sdf)
    models.upsert_marketplace_settlements(c, p['settlements'], 'stmt.csv', platform='lazada')
    models.upsert_marketplace_fees(c, p['fee_rows'], 'stmt.csv', platform='lazada')
    models.import_wallet_txns(c, p['income_rows'], 'stmt.csv', platform='lazada')
    wdf = pd.DataFrame([['1','17 Jun 2026 10:21:07','Withdrawal','Auto Withdrawal','-90','Bank Ref. X']], columns=WCOLS)
    w = parse_lazada_wallet(wdf)
    models.import_wallet_txns(c, w['withdrawals'], 'wal.csv', platform='lazada')
    rec = marketplace_reconcile.reconcile_payouts(c, 'lazada')
    assert rec['payouts'] == 1
    assert rec['orders_linked'] == 1
    row = c.execute("SELECT actual_payout, payout_id FROM marketplace_orders WHERE order_sn='ORDX'").fetchone()
    assert row['actual_payout'] == 90.0 and row['payout_id'] is not None
