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

# Lazada Fee Name → existing marketplace_order_fees bucket (single source of truth
# in marketplace_fee_buckets, also used by the display layer). Unmapped → fee_platform.
from marketplace_fee_buckets import LAZADA_BUCKET as _BUCKET
_BUCKETS = ('fee_commission', 'fee_service', 'fee_transaction', 'fee_platform',
            'fee_ads_escrow', 'fee_tax', 'shipping_net', 'fee_saver')

# Thai-export headers → the English column names the parser uses. Lazada Seller
# Center exports in the UI language; the files are otherwise identical. Renaming
# is a no-op on an English file (no matching keys). 'วันที่ปรับปรุงเข้ายอดของฉัน'
# ("credited to my balance") is the Release Date — verified to equal statement
# date +1, matching the existing English-imported income txn_time convention.
_TH_COL = {
    'รหัสรอบบิล': _C_STMT,
    'ชื่อรายการธุรกรรม': _C_FEE,
    'จำนวนเงิน(รวมภาษี)': _C_AMT,
    'หมายเลขคำสั่งซื้อ': _C_ORDER,
    'วันที่ปรับปรุงเข้ายอดของฉัน': _C_REL,
}


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
    df = df.rename(columns=_TH_COL)   # normalize Thai-export headers (no-op for English)
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
