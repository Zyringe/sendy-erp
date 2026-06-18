import os
os.environ.setdefault('SKIP_DB_INIT', '1')
import pandas as pd
import pytest
from parse_lazada_wallet import parse_lazada_wallet, LazadaWalletError

COLS = ['Transaction Number','Transaction Time','Type','Sub Type','Amount','Remarks']
def _df(rows): return pd.DataFrame(rows, columns=COLS)

def test_withdrawals_negative_iso_time():
    df = _df([
        ['1','18 Jun 2026 02:25:02','Deposit','Settlement','+84.42','Statement No. THJ-2026-0617'],
        ['2','15 Jun 2026 10:21:07','Withdrawal','Auto Withdrawal','-2,011.27','Bank Ref. THJ-20260615'],
    ])
    out = parse_lazada_wallet(df)
    assert len(out['withdrawals']) == 1
    w = out['withdrawals'][0]
    assert w['txn_type'] == 'withdrawal'
    assert w['amount'] == -2011.27
    assert w['txn_time'] == '2026-06-15 10:21:07'
    assert w['order_sn'] is None
    assert out['deposits_by_statement']['THJ-2026-0617'] == 84.42

def test_bad_columns_raise():
    with pytest.raises(LazadaWalletError):
        parse_lazada_wallet(pd.DataFrame([{'Foo':'bar'}]))
