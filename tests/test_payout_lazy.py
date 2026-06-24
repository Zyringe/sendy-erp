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


def test_fee_lines_from_raw_orders_parses_and_keeps_invariant():
    import json
    raw = json.dumps({'ค่าธรรมเนียมการชำระเงิน': -1.54, 'ยอดรวมค่าสินค้า': 49.0,
                      'หักค่าธรรมเนียมการขายสินค้า': -6.16, 'ส่วนลด LazCoins': -0.98,
                      'Premium Package': -3.08, 'รางวัลรีวิวสำหรับผู้ซื้อ': -5.35},
                     ensure_ascii=False)
    lines = models._fee_lines_from_raw(raw)
    assert lines[0] == {'label': 'ยอดรวมค่าสินค้า', 'amount': 49.0}   # positive item line first
    negs = [x['amount'] for x in lines[1:]]
    assert negs == sorted(negs)                                       # biggest deduction first
    assert {'label': 'ส่วนลด LazCoins', 'amount': -0.98} in lines      # raw label kept verbatim
    assert round(sum(x['amount'] for x in lines), 2) == 31.89         # Σ == net (popover footer)


def test_fee_lines_from_raw_none_for_missing_or_bad():
    assert models._fee_lines_from_raw(None) is None
    assert models._fee_lines_from_raw('') is None
    assert models._fee_lines_from_raw('{}') is None
    assert models._fee_lines_from_raw('not json') is None


def test_shopee_payout_orders_build_fee_lines_from_buckets(tmp_db_conn):
    """Shopee's breakdown comes from the typed fee_* bucket columns (item first,
    each non-zero fee, then a reconciling residual so Σ == net_payout footer).
    Shopee's fee_raw_json is the full raw statement row, NOT a {label:number}
    fee dict — it must never feed the popover."""
    c = tmp_db_conn
    _seed_one_deposit(c)
    # LZ1 is settled (item_value 60, net_payout 50): seed real buckets summing to −10.
    c.execute("UPDATE marketplace_order_fees SET fee_commission=-6.0, fee_service=-4.0 "
              "WHERE platform='shopee' AND order_sn='LZ1'")
    c.commit()
    pid = models.get_payout_summaries(c, 'shopee')[0]['id']
    by_sn = {o['order_sn']: o for o in models.get_payout_orders(c, 'shopee', pid)}
    lines = by_sn['LZ1']['fee_lines']
    assert lines[0] == {'label': 'มูลค่าสินค้า', 'amount': 60.0}     # positive item line first
    assert {'label': 'ค่าคอมมิชชั่น', 'amount': -6.0} in lines
    assert {'label': 'ค่าบริการ', 'amount': -4.0} in lines
    assert round(sum(x['amount'] for x in lines), 2) == 50.0        # Σ == net_payout footer
    assert by_sn['LZ2']['fee_lines'] is None       # no Income breakdown → estimate, no lines
    assert 'fee_raw_json' not in by_sn['LZ1']      # raw string not leaked to the client
    assert 'fee_commission' not in by_sn['LZ1']    # raw bucket columns not leaked either


def test_shopee_raw_statement_row_does_not_crash_payout_orders(tmp_db_conn):
    """Regression for PR #195: every Shopee deposit card showed 'โหลดไม่สำเร็จ'
    because get_payout_orders ran round() over fee_raw_json — but Shopee stores
    the FULL raw statement row there (string values, buyer name, order id, …),
    so round('1381', 2) raised TypeError → 500 → card load failed. The expand
    must build from buckets and must not touch the raw row for Shopee."""
    import json
    c = tmp_db_conn
    _seed_one_deposit(c)
    real_row = json.dumps({
        'ลำดับที่': '1381', 'หมายเลขคำสั่งซื้อ': 'LZ1',
        'ชื่อผู้ใช้ (ผู้ซื้อ)': 'pooysukan', 'ค่าธรรมเนียม (%)': '17.0',
        'สินค้าราคาปกติ': '60.0', 'รหัสคืนสินค้า': None,
    }, ensure_ascii=False)
    c.execute("UPDATE marketplace_order_fees SET fee_raw_json=? "
              "WHERE platform='shopee' AND order_sn='LZ1'", (real_row,))
    c.commit()
    pid = models.get_payout_summaries(c, 'shopee')[0]['id']
    by_sn = {o['order_sn']: o for o in models.get_payout_orders(c, 'shopee', pid)}  # must NOT raise
    labels = {x['label'] for x in by_sn['LZ1']['fee_lines']}
    assert 'ชื่อผู้ใช้ (ผู้ซื้อ)' not in labels and 'ลำดับที่' not in labels  # no raw-row garbage


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
