"""Parser for the Lazada Wallet / Transactions CSV.

Two row Types: 'Deposit/Settlement' (+, per statement — money into the Lazada
wallet) and 'Withdrawal/Auto Withdrawal' (-, the actual bank deposit, refs
'Bank Ref. ...'). Only the Withdrawal rows are imported (as txn_type='withdrawal')
— they close the reconcile cycles into bank deposits. The per-order income side is
synthesized from the Account Statement (see parse_lazada_statement). Deposit totals
per statement are returned only for the Σ-per-statement == deposit cross-check.
"""
import re
from datetime import datetime

import pandas as pd


class LazadaWalletError(ValueError):
    pass


_C_TIME = 'Transaction Time'
_C_TYPE = 'Type'
_C_AMT = 'Amount'
_C_REMARK = 'Remarks'
_REQUIRED = (_C_TIME, _C_TYPE, _C_AMT, _C_REMARK)
_STMT_RE = re.compile(r'(THJ\w*-?\d{4}-?\d{4})')


def _num(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).replace(',', '').replace('+', '').strip()
    if s == '' or s.lower() == 'nan':
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _iso_dt(s):
    return datetime.strptime(str(s).strip(), '%d %b %Y %H:%M:%S').strftime('%Y-%m-%d %H:%M:%S')


def load_lazada_wallet_csv(source):
    try:
        return pd.read_csv(source, sep=';', encoding='utf-8-sig', dtype=str).fillna('')
    except Exception as e:
        raise LazadaWalletError(f'อ่านไฟล์ Wallet ไม่ได้: {e}')


def parse_lazada_wallet(df):
    for col in _REQUIRED:
        if col not in df.columns:
            raise LazadaWalletError(
                f'ไม่พบคอลัมน์ "{col}" — ต้องเป็นไฟล์ Wallet/Transactions จาก Lazada ค่ะ')
    withdrawals = []
    deposits = {}
    settle = {}   # statement -> {settled_at (precise), amount}
    for _, r in df.iterrows():
        typ = str(r.get(_C_TYPE, '')).strip()
        amt = _num(r.get(_C_AMT))
        if amt is None:
            continue
        remark = str(r.get(_C_REMARK, '')).strip()
        if typ == 'Withdrawal':
            withdrawals.append({'txn_time': _iso_dt(r.get(_C_TIME)),
                                'txn_type': 'withdrawal', 'order_sn': None,
                                'amount': round(amt, 2), 'running_balance': None,
                                'description': remark})
        elif typ == 'Deposit':
            m = _STMT_RE.search(remark)
            if m:
                stmt = m.group(1)
                deposits[stmt] = round(deposits.get(stmt, 0.0) + amt, 2)
                cur = settle.setdefault(stmt, {'settled_at': _iso_dt(r.get(_C_TIME)),
                                               'amount': 0.0})
                cur['amount'] = round(cur['amount'] + amt, 2)
    settlements = [{'statement': k, 'settled_at': v['settled_at'], 'amount': v['amount']}
                   for k, v in settle.items()]
    return {'withdrawals': withdrawals, 'deposits_by_statement': deposits,
            'settlements': settlements}
