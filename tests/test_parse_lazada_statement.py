import os
os.environ.setdefault('SKIP_DB_INIT', '1')
import pandas as pd
import pytest
from parse_lazada_statement import (parse_lazada_statement, load_lazada_statement_csv,
                                     LazadaStatementError)

COLS = ['Statement Period','Statement Number','Transaction Date','Fee Name',
        'Amount(Include Tax)','VAT Amount','Release Status','Release Date','Comment',
        'Order Creation Date','Order Number','Order Line ID','Seller SKU','Lazada SKU',
        'WHT Amount','WHT included in Amount','Order Status','Product Name','Short Code']

def _row(stmt, fee, amt, order, rel='16 Jun 2026', ocd='11 Jun 2026', prod='สินค้า A', sku='SK1'):
    return {'Statement Period':'', 'Statement Number':stmt, 'Transaction Date':'15 Jun 2026',
            'Fee Name':fee, 'Amount(Include Tax)':amt, 'VAT Amount':'0', 'Release Status':'Released to My Balance',
            'Release Date':rel, 'Comment':'', 'Order Creation Date':ocd, 'Order Number':order,
            'Order Line ID':'1', 'Seller SKU':sku, 'Lazada SKU':'L1', 'WHT Amount':'0',
            'WHT included in Amount':'NO', 'Order Status':'Confirmed', 'Product Name':prod, 'Short Code':'SC'}

def _df(rows): return pd.DataFrame(rows, columns=COLS)

def test_aggregates_order_net_and_fees():
    # one order, gross 100, commission -8, payment fee -2 → net 90
    df = _df([
        _row('S-0615','Item Price Credit','100','ORD1'),
        _row('S-0615','Commission','-8','ORD1'),
        _row('S-0615','Payment Fee','-2','ORD1'),
    ])
    out = parse_lazada_statement(df)
    s = {x['order_sn']: x for x in out['settlements']}['ORD1']
    assert s['actual_payout'] == 90.0
    assert s['settled_at'] == '2026-06-16'
    f = {x['order_sn']: x for x in out['fee_rows']}['ORD1']
    assert f['item_value'] == 100.0
    assert f['fee_commission'] == -8.0
    assert f['fee_transaction'] == -2.0          # Payment Fee → fee_transaction
    assert f['net_payout'] == 90.0
    assert f['fee_total'] == 10.0                # item_value - net_payout
    # invariant: item_value + Σ(fee buckets) == net_payout
    buckets = sum(f[k] for k in ('fee_commission','fee_service','fee_transaction',
                  'fee_platform','fee_ads_escrow','fee_tax','shipping_net','fee_saver'))
    assert round(f['item_value'] + buckets, 2) == f['net_payout']

def test_income_rows_for_reconcile():
    df = _df([_row('S-0615','Item Price Credit','100','ORD1'),
              _row('S-0615','Commission','-10','ORD1')])
    out = parse_lazada_statement(df)
    inc = out['income_rows']
    assert len(inc) == 1
    assert inc[0] == {'txn_time':'2026-06-16','txn_type':'income','order_sn':'ORD1',
                      'amount':90.0,'description':'S-0615'}

def test_unmapped_fee_name_goes_to_platform_and_is_reported():
    df = _df([_row('S-0615','Item Price Credit','100','ORD1'),
              _row('S-0615','Mystery New Fee','-5','ORD1')])
    out = parse_lazada_statement(df)
    f = {x['order_sn']: x for x in out['fee_rows']}['ORD1']
    assert f['fee_platform'] == -5.0
    assert 'Mystery New Fee' in out['unmapped_fee_names']

def test_missing_required_column_raises():
    df = pd.DataFrame([{'Foo':'bar'}])
    with pytest.raises(LazadaStatementError):
        parse_lazada_statement(df)

def test_extended_fee_names_bucketed():
    # fee names that appear in the fuller statement export must map, not fall to catch-all
    df = _df([
        _row('S-1','Item Price Credit','200','O1'),
        _row('S-1','Campaign Fee','-5','O1'),
        _row('S-1','Promotional Charges Vouchers','-3','O1'),
        _row('S-1','Reversal of Free Shipping Max Fee','-2','O1'),
        _row('S-1','Lost Claim','4','O1'),
    ])
    out = parse_lazada_statement(df)
    f = {x['order_sn']: x for x in out['fee_rows']}['O1']
    assert f['fee_ads_escrow'] == -8.0          # Campaign Fee + Promotional Charges Vouchers
    assert f['shipping_net'] == -2.0            # Reversal of Free Shipping Max Fee
    assert f['fee_platform'] == 4.0             # Lost Claim → explicit catch-all
    assert f['net_payout'] == 194.0             # 200 -5 -3 -2 +4
    assert out['unmapped_fee_names'] == []      # all now known → no false "unknown fee" warning
