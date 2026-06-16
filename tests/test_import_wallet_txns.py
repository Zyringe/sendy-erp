import models

def _rows():
    return [
      {'txn_time':'2026-06-15 13:55:39','txn_type':'income','order_sn':'SN1','amount':35.0,'running_balance':35.0,'description':'#A'},
      {'txn_time':'2026-06-16 01:17:03','txn_type':'withdrawal','order_sn':None,'amount':-35.0,'running_balance':0.0,'description':'auto'},
    ]

def test_insert_is_idempotent(tmp_db_conn):
    c = tmp_db_conn
    c.execute("DELETE FROM marketplace_wallet_txns"); c.commit()
    a = models.import_wallet_txns(c, _rows(), 'bal.xlsx')
    b = models.import_wallet_txns(c, _rows(), 'bal.xlsx')   # re-import same file
    assert a == 2 and b == 0
    total = c.execute("SELECT COUNT(*) FROM marketplace_wallet_txns").fetchone()[0]
    assert total == 2
