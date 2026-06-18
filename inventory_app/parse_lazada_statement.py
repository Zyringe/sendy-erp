"""Parser for Lazada Account Statement CSV (Seacd….csv).

Unlike Shopee's Income Transfer (one payout row per order), Lazada gives many
per-fee rows. We aggregate by Order Number → per-order net payout + fee buckets,
and emit synthesized per-order wallet 'income' rows (txn_time = statement Release
Date) so the generic reconcile_payouts links orders→bank-deposits unchanged.

CSV: ';' delimited, utf-8-sig (BOM). Dates 'DD Mon YYYY'. Amounts may carry
'+'/'-' and thousands commas. Fees are negative; Item Price Credit is the gross.
"""
import json
from datetime import datetime

import pandas as pd


class LazadaStatementError(ValueError):
    pass


_C_STMT = 'Statement Number'
_C_FEE = 'Fee Name'
_C_AMT = 'Amount(Include Tax)'
_C_REL = 'Release Date'
_C_ORDER = 'Order Number'
_C_OCD = 'Order Creation Date'
_REQUIRED = (_C_STMT, _C_FEE, _C_AMT, _C_ORDER)

# Lazada Fee Name → existing marketplace_order_fees bucket. Unmapped → fee_platform.
_BUCKET = {
    'Item Price Credit': 'item_value', 'Reversal Item Price': 'item_value',
    'Commission': 'fee_commission', 'Reversal Commission': 'fee_commission',
    'Commission fee - correction for undercharge': 'fee_commission',
    'Payment Fee': 'fee_transaction', 'Payment Fee Credit': 'fee_transaction',
    'Payment fee - correction for undercharge': 'fee_transaction',
    'Premium Package': 'fee_service', 'Reverse - Premium Package': 'fee_service',
    'Free Shipping Max Fee': 'shipping_net',
    'Shipping Fee Voucher Refund to Laz': 'shipping_net',
    'Wrong Shipping Fee Adjustment': 'shipping_net',
    'Reversal of Free Shipping Max Fee': 'shipping_net',
    'LazCoins Discount': 'fee_ads_escrow',
    'LazCoins Discount Promotion Fee': 'fee_ads_escrow',
    'Reversal of LazCoins Discount': 'fee_ads_escrow',
    'Reversal of LazCoins Discount Promotion Fee': 'fee_ads_escrow',
    'Buyer Review Incentive': 'fee_ads_escrow',
    'Campaign Fee': 'fee_ads_escrow',
    'Promotional Charges Vouchers': 'fee_ads_escrow',
    # Lost Claim = Lazada reimbursement for parcels lost by 3PL (a credit, not a
    # fee); no dedicated bucket → parked in the platform catch-all, but mapped
    # explicitly so it stops being reported as an "unknown fee" on every import.
    'Lost Claim': 'fee_platform',
}
_BUCKETS = ('fee_commission', 'fee_service', 'fee_transaction', 'fee_platform',
            'fee_ads_escrow', 'fee_tax', 'shipping_net', 'fee_saver')


def _num(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return 0.0
    s = str(v).replace(',', '').replace('+', '').strip()
    if s == '' or s.lower() == 'nan':
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _iso_date(s):
    s = (str(s) or '').strip()
    if not s or s.lower() == 'nan':
        return ''
    return datetime.strptime(s, '%d %b %Y').strftime('%Y-%m-%d')


def load_lazada_statement_csv(source):
    """Read the Lazada Account Statement CSV → str DataFrame."""
    try:
        return pd.read_csv(source, sep=';', encoding='utf-8-sig', dtype=str).fillna('')
    except Exception as e:
        raise LazadaStatementError(f'อ่านไฟล์ Account Statement ไม่ได้: {e}')


def parse_lazada_statement(df):
    """Aggregate the Account Statement by Order Number. See module docstring."""
    for col in _REQUIRED:
        if col not in df.columns:
            raise LazadaStatementError(
                f'ไม่พบคอลัมน์ "{col}" — ต้องเป็นไฟล์ Account Statement จาก Lazada ค่ะ')

    agg = {}            # order_sn -> accumulator
    order_order = []    # preserve first-seen order
    unmapped = set()
    for _, r in df.iterrows():
        sn = str(r.get(_C_ORDER, '')).strip()
        if not sn or sn.lower() == 'nan':
            continue
        if sn not in agg:
            agg[sn] = {'item_value': 0.0, 'net': 0.0, 'settled_at': '',
                       'statement': str(r.get(_C_STMT, '')).strip(),
                       'raw': {}}
            order_order.append(sn)
        a = agg[sn]
        amt = _num(r.get(_C_AMT))
        fee_name = str(r.get(_C_FEE, '')).strip()
        bucket = _BUCKET.get(fee_name)
        if bucket is None:
            bucket = 'fee_platform'
            unmapped.add(fee_name)
        a['net'] += amt
        if bucket == 'item_value':
            a['item_value'] += amt
        else:
            a.setdefault(bucket, 0.0)
            a[bucket] = a.get(bucket, 0.0) + amt
        a['raw'][fee_name] = round(a['raw'].get(fee_name, 0.0) + amt, 2)
        rel = _iso_date(r.get(_C_REL))
        if rel:
            a['settled_at'] = rel               # latest release date wins
        a['statement'] = str(r.get(_C_STMT, '')).strip()

    settlements, fee_rows, income_rows = [], [], []
    for sn in order_order:
        a = agg[sn]
        item_value = round(a['item_value'], 2)
        net = round(a['net'], 2)
        settlements.append({'order_sn': sn, 'actual_payout': net,
                            'settled_at': a['settled_at']})
        rec = {'order_sn': sn, 'item_value': item_value, 'net_payout': net,
               'fee_total': round(item_value - net, 2),
               'fee_pct': (f"{round((item_value - net) / item_value * 100, 1)}%"
                           if item_value else ''),
               'fee_raw_json': json.dumps(a['raw'], ensure_ascii=False)}
        for b in _BUCKETS:
            rec[b] = round(a.get(b, 0.0), 2)
        fee_rows.append(rec)
        if a['settled_at']:
            income_rows.append({'txn_time': a['settled_at'], 'txn_type': 'income',
                                'order_sn': sn, 'amount': net,
                                'description': a['statement']})
    return {'settlements': settlements, 'fee_rows': fee_rows,
            'income_rows': income_rows, 'unmapped_fee_names': sorted(unmapped)}
