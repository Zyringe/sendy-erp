# Shopee Marketplace Data Hub — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Implement on Sonnet.** Plan authored on Opus. Spec: `docs/superpowers/specs/2026-06-16-shopee-marketplace-data-hub-design.md`. Mockup (approved look): `docs/superpowers/specs/2026-06-16-shopee-marketplace-data-hub-mockup.html`.

**Goal:** Capture Shopee's full per-order fee breakdown and reconcile real bank deposits (weekly auto-withdrawals) to their exact orders, behind one auto-detecting upload box in Sendy.

**Architecture:** Three data layers — Order (what sold, already imported) → การเงิน/Income (fees → net) → Balance (net → bank deposit). New parsers feed two new tables (`marketplace_order_fees`, `marketplace_wallet_txns`) + a derived `marketplace_payouts`; a reconciliation engine segments wallet income by withdrawal cycle. Shopee-only now; parser interface is pluggable for Lazada/TikTok later.

**Tech Stack:** Flask 3 (Python 3.9, no ORM), SQLite, pandas/openpyxl, Bootstrap 5.3.3 + bootstrap-icons, pytest.

---

## Implementer context (read once, do not re-explore)

**Run / test / migrate**
- Venv python: `~/.virtualenvs/erp/bin/python` (system python3 lacks deps).
- Run server: `sendy-up` (logs `/tmp/sendy.log`), stop `sendy-down`. Or `~/.virtualenvs/erp/bin/python inventory_app/app.py` (port 5001).
- Tests from `sendy_erp/`: `~/.virtualenvs/erp/bin/pytest tests/<file>.py -v` (pytest.ini sets `pythonpath=inventory_app .`).
- Migration: drop `data/migrations/NNN_name.sql` + `NNN_name.rollback.sql`, then restart server — `database.py::init_db()` auto-applies on boot and records SHA in `applied_migrations`. Latest existing mig = **106**, so new = **107**.
- After ANY new/changed Flask route: `sendy-down && sendy-up` in the same step (werkzeug URL map is built at startup; the reloader does NOT rebuild it → `url_for(new endpoint)` 500s). Then `curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5001/<route>`.

**DB facts**
- DB path via `config.DATABASE_PATH`; open with `from database import get_connection` (returns sqlite3 conn, `row_factory = sqlite3.Row`).
- `marketplace_orders` columns: `id, platform, order_sn, status, buyer_name, order_date, paid_date, item_total, marketplace_fee, payout, currency, source_file, raw_json, first_synced_at, last_synced_at, actual_payout, settled_at, settlement_source, payout_batch_id`. We ADD `payout_id` in T-mig.
- `marketplace_order_items` columns include: `order_id, item_name, variation_name, seller_sku, qty, unit_price, item_subtotal, internal_product_id`.
- Existing `payout_batches` + `marketplace_orders.payout_batch_id` (mig 105, the manual matcher PR #147): **leave in place, deprecated.** Do NOT drop them (avoid data loss / rollback risk). The new UI uses `marketplace_payouts`/`payout_id` instead.

**Existing functions to build on** (`inventory_app/`):
- `parse_income_transfer.py`: `find_income_header_row(raw_df, max_scan=15)`, `load_income_sheet(source)` (reads sheet `'Income'`, header-aware), `parse_shopee_income(df)` (→ `[{order_sn, actual_payout, settled_at}]`), `IncomeTransferError`, `_to_float`. We ADD `parse_shopee_income_fees(df)`.
- `parse_orders.py`: `parse_shopee_orders(df)`, `parse_lazada_orders(df)`.
- `marketplace.py` blueprint (`bp_marketplace`): routes `/marketplace`, `/marketplace/import` (POST, order file → `models.import_marketplace_orders`), `/marketplace/settlement-import` (POST, income → `models.upsert_marketplace_settlements` then `marketplace_match.run_automatch`), `/marketplace/settlement` (GET), `/marketplace/api/order/<id>` (→ `models.get_marketplace_order_detail`).
- `models.py`: `upsert_marketplace_settlements(conn, settlements, source_file, platform)`, `get_settlement_report(conn, platform)`, `get_marketplace_order_detail(conn, order_id)`, `get_deposit_batch_report(conn)`.
- `db_backup.safe_create_backup(tag, db_path, backup_dir)` + `db_backup.default_backup_dir(path)` — call before any importer writes (pattern already in `import_orders`).

**Test fixtures** (`tests/conftest.py`): `tmp_db` (copies live DB to tmp, monkeypatches `config.DATABASE_PATH`), `tmp_db_conn` (sqlite3 conn on it, `row_factory=Row`). Use `tmp_db_conn` for DB tests. Pure-parser tests need no DB.

**Sample files for fixtures / manual checks**
- Balance: `~/Downloads/my_balance_transaction_report.shopee.20260101_20260616.xlsx`
- การเงิน/Income: `~/Downloads/Income.โอนเงินสำเร็จ.th.20260101_20260616.xlsx`
- Orders: `data/source/Ecommerce_Order/Order.all.*.xlsx`
- **Reconciliation oracle (verified):** withdrawal 2026-06-16 = ฿7,689 = **39 orders**; 2026-06-09 = ฿5,890 = **32 orders**.

**Repo discipline:** `sendy_erp` is its own git repo; `main` auto-deploys to Railway (merge = deploy). Work on a branch `feat/marketplace-data-hub`. Commit per task. **Do NOT merge/push to main without Put** — pre-merge gate = boot real app, curl routes, exercise the real upload POST.

**Column-name constants (verified from real files)**

การเงิน "Income" sheet headers:
```
order_sn       = 'หมายเลขคำสั่งซื้อ'
order_date     = 'วันที่ทำการสั่งซื้อ'
buyer          = 'ชื่อผู้ใช้ (ผู้ซื้อ)'
fee_pct        = 'ค่าธรรมเนียม (%)'
settled_at     = 'วันที่โอนชำระเงินสำเร็จ'
item_normal    = 'สินค้าราคาปกติ'
seller_disc    = 'ส่วนลดสินค้าจากผู้ขาย'
comm_ams       = 'ค่าคอมมิชชั่น AMS'
comm           = 'ค่าคอมมิชชั่น'
service        = 'ค่าบริการ'
platform_infra = 'ค่าธรรมเนียมโครงสร้างพื้นฐานแพลตฟอร์ม'
payment_txn    = 'ค่าธุรกรรมการชำระเงิน'
ads_escrow     = 'ค่าธรรมเนียมเติมเงินโฆษณาจากเงิน Escrow'
tax            = 'ภาษี'
ship_buyer     = 'ค่าจัดส่งที่ชำระโดยผู้ซื้อ'
ship_shopee    = 'ค่าจัดส่งสินค้าที่ออกโดย Shopee'
ship_by_you    = 'ค่าจัดส่งที่ Shopee ชำระโดยชื่อของคุณ'
saver_fee      = 'ค่าธรรมเนียม ของโปรแกรมประหยัดค่าจัดส่ง'
net            = 'จำนวนเงินทั้งหมดที่โอนแล้ว (฿)'
```

Balance "Transaction Report" sheet headers (banner above; header row ~index 17):
```
time     = 'วันที่'
type     = 'ประเภทการทำธุรกรรม'   # values: รายรับจากคำสั่งซื้อ | การถอนเงิน | รายการปรับปรุง
desc     = 'คำอธิบาย'
order_sn = 'รหัสคำสั่งซื้อ'        # '-' on withdrawals
flow     = 'รูปแบบธุรกรรม'         # เงินเข้า | เงินออก
amount   = 'จำนวนเงิน'             # signed
status   = 'สถานะ'
balance  = 'ยอดเงินหลังทำธุรกรรมเสร็จสิ้น'
```

---

## Phase 0 — Parsers + file detector (pure, TDD)

### Task 1: Income fee parser (`parse_shopee_income_fees`)

**Files:**
- Modify: `inventory_app/parse_income_transfer.py`
- Test: `tests/test_parse_income_fees.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_parse_income_fees.py
import pandas as pd
from parse_income_transfer import parse_shopee_income_fees

def _df(**over):
    base = {
        'หมายเลขคำสั่งซื้อ': '260610Q41E2471', 'วันที่ทำการสั่งซื้อ': '2026-06-10',
        'ชื่อผู้ใช้ (ผู้ซื้อ)': 'mbz0nna9ii', 'ค่าธรรมเนียม (%)': '3.21%',
        'วันที่โอนชำระเงินสำเร็จ': '2026-06-15', 'สินค้าราคาปกติ': '55',
        'ส่วนลดสินค้าจากผู้ขาย': '-6', 'ค่าคอมมิชชั่น AMS': '-6', 'ค่าคอมมิชชั่น': '-4',
        'ค่าบริการ': '-1', 'ค่าธรรมเนียมโครงสร้างพื้นฐานแพลตฟอร์ม': '0',
        'ค่าธุรกรรมการชำระเงิน': '-1', 'ค่าธรรมเนียมเติมเงินโฆษณาจากเงิน Escrow': '0',
        'ภาษี': '0', 'ค่าจัดส่งที่ชำระโดยผู้ซื้อ': '29', 'ค่าจัดส่งสินค้าที่ออกโดย Shopee': '-29',
        'ค่าจัดส่งที่ Shopee ชำระโดยชื่อของคุณ': '0',
        'ค่าธรรมเนียม ของโปรแกรมประหยัดค่าจัดส่ง': '-2',
        'จำนวนเงินทั้งหมดที่โอนแล้ว (฿)': '35',
    }
    base.update(over)
    return pd.DataFrame([base])

def test_fee_buckets_and_net():
    rows = parse_shopee_income_fees(_df())
    assert len(rows) == 1
    r = rows[0]
    assert r['order_sn'] == '260610Q41E2471'
    assert r['net_payout'] == 35.0
    assert r['item_value'] == 49.0            # 55 + (-6)
    assert r['fee_commission'] == -10.0       # AMS -6 + comm -4
    assert r['fee_transaction'] == -1.0
    assert r['shipping_net'] == 0.0           # 29 - 29 + 0
    assert r['fee_saver'] == -2.0
    assert r['fee_pct'] == '3.21%'
    # fee_total = item_value - net_payout (the satang-true identity)
    assert r['fee_total'] == round(49.0 - 35.0, 2)
    assert 'fee_raw_json' in r and '260610Q41E2471' in r['fee_raw_json']

def test_blank_order_sn_skipped():
    assert parse_shopee_income_fees(_df(**{'หมายเลขคำสั่งซื้อ': ''})) == []
```

- [ ] **Step 2: Run test — expect FAIL**

Run: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_parse_income_fees.py -v`
Expected: FAIL — `ImportError: cannot import name 'parse_shopee_income_fees'`.

- [ ] **Step 3: Implement** (append to `parse_income_transfer.py`)

```python
import json

# Fee buckets → list of source column names summed into each bucket.
_FEE_BUCKETS = {
    'fee_commission':  ['ค่าคอมมิชชั่น AMS', 'ค่าคอมมิชชั่น'],
    'fee_service':     ['ค่าบริการ'],
    'fee_transaction': ['ค่าธุรกรรมการชำระเงิน'],
    'fee_platform':    ['ค่าธรรมเนียมโครงสร้างพื้นฐานแพลตฟอร์ม'],
    'fee_ads_escrow':  ['ค่าธรรมเนียมเติมเงินโฆษณาจากเงิน Escrow'],
    'fee_tax':         ['ภาษี'],
    'shipping_net':    ['ค่าจัดส่งที่ชำระโดยผู้ซื้อ', 'ค่าจัดส่งสินค้าที่ออกโดย Shopee',
                        'ค่าจัดส่งที่ Shopee ชำระโดยชื่อของคุณ'],
    'fee_saver':       ['ค่าธรรมเนียม ของโปรแกรมประหยัดค่าจัดส่ง'],
}
_ITEM_COLS = ['สินค้าราคาปกติ', 'ส่วนลดสินค้าจากผู้ขาย']
_COL_FEE_PCT = 'ค่าธรรมเนียม (%)'


def _sum_cols(row, cols):
    total = 0.0
    for c in cols:
        v = _to_float(row.get(c)) if c in row else None
        if v is not None:
            total += v
    return round(total, 2)


def parse_shopee_income_fees(df):
    """Per-order fee breakdown from the Income sheet (full columns).

    Returns a list of dicts: order_sn, order_date, buyer, item_value, the
    fee_* buckets, shipping_net, fee_total, net_payout, fee_pct, fee_raw_json.
    Rows with a blank order id are skipped. Amounts keep Shopee's sign
    (fees are negative). fee_total = item_value - net_payout (the satang-true
    identity), independent of bucket completeness.
    """
    out = []
    for _, row in df.iterrows():
        sn = '' if pd.isna(row.get(_COL_ORDER_SN)) else str(row.get(_COL_ORDER_SN)).strip()
        if not sn or sn.lower() == 'nan':
            continue
        item_value = _sum_cols(row, _ITEM_COLS)
        net = _to_float(row.get(_COL_PAYOUT)) or 0.0
        rec = {
            'order_sn':   sn,
            'order_date': ('' if pd.isna(row.get('วันที่ทำการสั่งซื้อ'))
                           else str(row.get('วันที่ทำการสั่งซื้อ')).strip()[:10]),
            'buyer':      ('' if pd.isna(row.get('ชื่อผู้ใช้ (ผู้ซื้อ)'))
                           else str(row.get('ชื่อผู้ใช้ (ผู้ซื้อ)')).strip()),
            'item_value': item_value,
            'net_payout': round(net, 2),
            'fee_pct':    ('' if pd.isna(row.get(_COL_FEE_PCT))
                           else str(row.get(_COL_FEE_PCT)).strip()),
            'fee_total':  round(item_value - net, 2),
            'fee_raw_json': json.dumps(
                {k: (None if pd.isna(v) else str(v)) for k, v in row.items()},
                ensure_ascii=False),
        }
        for bucket, cols in _FEE_BUCKETS.items():
            rec[bucket] = _sum_cols(row, cols)
        out.append(rec)
    return out
```

- [ ] **Step 4: Run test — expect PASS**

Run: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_parse_income_fees.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add inventory_app/parse_income_transfer.py tests/test_parse_income_fees.py
git commit -m "feat(marketplace): parse full per-order Shopee fee breakdown"
```

---

### Task 2: Balance report parser (`parse_balance.py`)

**Files:**
- Create: `inventory_app/parse_balance.py`
- Test: `tests/test_parse_balance.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_parse_balance.py
import pandas as pd
import pytest
from parse_balance import parse_shopee_balance, find_balance_header_row, BalanceError

HEADER = ['วันที่','ประเภทการทำธุรกรรม','คำอธิบาย','รหัสคำสั่งซื้อ',
          'รูปแบบธุรกรรม','จำนวนเงิน','สถานะ','ยอดเงินหลังทำธุรกรรมเสร็จสิ้น']

def _df(rows):
    return pd.DataFrame(rows, columns=HEADER)

def test_classifies_types_and_sign():
    df = _df([
        ['2026-06-15 13:55:39','รายรับจากคำสั่งซื้อ','#A','260610Q41E2471','เงินเข้า','35','สำเร็จ','7689'],
        ['2026-06-16 01:17:03','การถอนเงิน','อัตโนมัติ','-','เงินออก','-7689','สำเร็จ','0'],
        ['2026-02-05 15:42:13','รายการปรับปรุง','ชดเชย','-','เงินเข้า','55','สำเร็จ','856'],
    ])
    rows = parse_shopee_balance(df)
    assert [r['txn_type'] for r in rows] == ['income','withdrawal','adjustment']
    assert rows[0]['order_sn'] == '260610Q41E2471' and rows[0]['amount'] == 35.0
    assert rows[1]['order_sn'] is None and rows[1]['amount'] == -7689.0
    assert rows[1]['running_balance'] == 0.0

def test_find_header_row_past_banner():
    raw = pd.DataFrame([['รายงาน',None,None,None,None,None,None,None],
                        [None]*8, HEADER, ['2026-06-16','การถอนเงิน','x','-','เงินออก','-1','ok','0']])
    assert find_balance_header_row(raw) == 2

def test_bad_amount_raises():
    with pytest.raises(BalanceError):
        parse_shopee_balance(_df([['t','การถอนเงิน','x','-','เงินออก','notnum','ok','0']]))
```

- [ ] **Step 2: Run test — expect FAIL**

Run: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_parse_balance.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'parse_balance'`.

- [ ] **Step 3: Implement**

```python
# inventory_app/parse_balance.py
"""Parser for Shopee Seller Balance transaction reports
(my_balance_transaction_report.*.xlsx). One sheet 'Transaction Report' with a
metadata banner above the real header row. Each row is a wallet event:
รายรับจากคำสั่งซื้อ (order income), การถอนเงิน (bank withdrawal = a real bank
deposit), or รายการปรับปรุง (adjustment). The withdrawal rows + the running
balance are the ground truth for which orders make up each bank deposit.
"""
import pandas as pd

SHEET = 'Transaction Report'
_C_TIME='วันที่'; _C_TYPE='ประเภทการทำธุรกรรม'; _C_DESC='คำอธิบาย'
_C_SN='รหัสคำสั่งซื้อ'; _C_AMT='จำนวนเงิน'; _C_BAL='ยอดเงินหลังทำธุรกรรมเสร็จสิ้น'

_TYPE_MAP = {'รายรับจากคำสั่งซื้อ':'income', 'การถอนเงิน':'withdrawal',
             'รายการปรับปรุง':'adjustment'}


class BalanceError(Exception):
    pass


def _to_float(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).replace(',', '').strip()
    if s == '' or s.lower() == 'nan':
        return None
    try:
        return float(s)
    except ValueError:
        return None


def find_balance_header_row(raw_df, max_scan=25):
    """0-based index of the header row (cells include _C_TIME and _C_TYPE)."""
    limit = min(max_scan, len(raw_df))
    for i in range(limit):
        cells = {str(v).strip() for v in raw_df.iloc[i].tolist() if not pd.isna(v)}
        if _C_TIME in cells and _C_TYPE in cells:
            return i
    raise BalanceError(
        f'ไม่พบแถวหัวตาราง ("{_C_TIME}"/"{_C_TYPE}") ใน {limit} แถวแรก '
        '— ต้องเป็นไฟล์ Seller Balance (my_balance_transaction_report) จาก Shopee ค่ะ')


def load_balance_sheet(source):
    """Read the 'Transaction Report' sheet header-aware → str DataFrame."""
    try:
        raw = pd.read_excel(source, sheet_name=SHEET, header=None, dtype=str)
    except Exception as e:
        raise BalanceError(f'อ่านชีต "{SHEET}" ไม่ได้: {e}')
    hdr = find_balance_header_row(raw)
    df = raw.iloc[hdr + 1:].reset_index(drop=True)
    df.columns = [str(c).strip() for c in raw.iloc[hdr].tolist()]
    return df


def parse_shopee_balance(df):
    """List of wallet rows: {txn_time, txn_type, order_sn, amount,
    running_balance, description}. Rows with no amount are skipped (blank
    spacer rows). Raises BalanceError on an unknown txn type or unparseable
    amount on a real (typed) row."""
    out = []
    for _, r in df.iterrows():
        typ_raw = r.get(_C_TYPE)
        if typ_raw is None or pd.isna(typ_raw) or str(typ_raw).strip() == '':
            continue
        typ_raw = str(typ_raw).strip()
        if typ_raw not in _TYPE_MAP:
            raise BalanceError(f'ประเภทธุรกรรมไม่รู้จัก: {typ_raw!r}')
        amt = _to_float(r.get(_C_AMT))
        if amt is None:
            raise BalanceError(f'จำนวนเงินอ่านไม่ได้ในแถว {typ_raw!r}: {r.get(_C_AMT)!r}')
        sn = r.get(_C_SN)
        sn = None if (sn is None or pd.isna(sn) or str(sn).strip() in ('', '-')) else str(sn).strip()
        out.append({
            'txn_time':        '' if pd.isna(r.get(_C_TIME)) else str(r.get(_C_TIME)).strip(),
            'txn_type':        _TYPE_MAP[typ_raw],
            'order_sn':        sn,
            'amount':          round(amt, 2),
            'running_balance': _to_float(r.get(_C_BAL)),
            'description':     '' if pd.isna(r.get(_C_DESC)) else str(r.get(_C_DESC)).strip(),
        })
    return out
```

- [ ] **Step 4: Run test — expect PASS**

Run: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_parse_balance.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add inventory_app/parse_balance.py tests/test_parse_balance.py
git commit -m "feat(marketplace): Shopee seller-balance ledger parser"
```

---

### Task 3: File-type detector (`marketplace_files.py`)

**Files:**
- Create: `inventory_app/marketplace_files.py`
- Test: `tests/test_marketplace_files.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_marketplace_files.py
import io
import pandas as pd
from marketplace_files import detect_file

def _xlsx(sheets):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as w:
        for name, df in sheets.items():
            df.to_excel(w, sheet_name=name, index=False, header=False)
    buf.seek(0); return buf

def test_detect_balance():
    df = pd.DataFrame([['รายงาน'], ['วันที่','ประเภทการทำธุรกรรม']])
    assert detect_file(_xlsx({'Transaction Report': df})) == ('balance', 'shopee')

def test_detect_income():
    df = pd.DataFrame([['x']])
    sheets = {'Summary': df, 'Income': df, 'Service Fee Details': df, 'Adjustment': df}
    assert detect_file(_xlsx(sheets)) == ('income', 'shopee')

def test_detect_shopee_order():
    df = pd.DataFrame([['หมายเลขคำสั่งซื้อ','ชื่อสินค้า','จำนวน']])
    assert detect_file(_xlsx({'orders': df})) == ('order', 'shopee')

def test_detect_unknown():
    df = pd.DataFrame([['foo','bar']])
    assert detect_file(_xlsx({'sheet1': df})) == (None, None)
```

- [ ] **Step 2: Run test — expect FAIL**

Run: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_marketplace_files.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marketplace_files'`.

- [ ] **Step 3: Implement**

```python
# inventory_app/marketplace_files.py
"""Detect which Shopee/Lazada export a file is, so one upload box can route it.

Returns (kind, platform):
  kind ∈ {'balance','income','order', None}; platform ∈ {'shopee','lazada', None}.
Detection order: sheet-name signatures first (Balance/Income are unambiguous),
then sheet-0 column signatures for the flat Order export.
"""
import pandas as pd


def detect_file(source):
    try:
        xl = pd.ExcelFile(source)
    except Exception:
        return (None, None)
    sheets = set(xl.sheet_names)
    if 'Transaction Report' in sheets:
        return ('balance', 'shopee')
    if 'Income' in sheets and 'Service Fee Details' in sheets:
        return ('income', 'shopee')
    # Order export: read sheet 0 header (no banner) and sniff columns.
    try:
        cols = set(pd.read_excel(xl, sheet_name=0, header=0, nrows=0, dtype=str).columns)
    except Exception:
        cols = set()
    if 'orderItemId' in cols and 'orderNumber' in cols:
        return ('order', 'lazada')
    if 'หมายเลขคำสั่งซื้อ' in cols:
        return ('order', 'shopee')
    return (None, None)
```

- [ ] **Step 4: Run test — expect PASS**

Run: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_marketplace_files.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add inventory_app/marketplace_files.py tests/test_marketplace_files.py
git commit -m "feat(marketplace): file-type/platform detector for unified upload"
```

---

## Phase 1 — Migration (schema)

### Task 4: Migration 107 (fees + wallet + payouts tables)

**Files:**
- Create: `data/migrations/107_marketplace_fees_wallet.sql`
- Create: `data/migrations/107_marketplace_fees_wallet.rollback.sql`
- Test: `tests/test_mig107_schema.py`

- [ ] **Step 1: Write the migration**

```sql
-- 107_marketplace_fees_wallet.sql
-- Per-order Shopee fee breakdown + seller-balance wallet ledger + derived
-- bank-deposit (payout) table. Supersedes the manual payout_batches matcher
-- (mig 105) for reconciliation; payout_batches is left in place (deprecated).
PRAGMA busy_timeout=10000;
BEGIN IMMEDIATE;

CREATE TABLE marketplace_order_fees (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT NOT NULL,
    order_sn        TEXT NOT NULL,
    item_value      REAL,
    fee_commission  REAL DEFAULT 0,
    fee_service     REAL DEFAULT 0,
    fee_transaction REAL DEFAULT 0,
    fee_platform    REAL DEFAULT 0,
    fee_ads_escrow  REAL DEFAULT 0,
    fee_tax         REAL DEFAULT 0,
    shipping_net    REAL DEFAULT 0,
    fee_saver       REAL DEFAULT 0,
    fee_total       REAL,
    net_payout      REAL,
    fee_pct         TEXT,
    fee_raw_json    TEXT,
    source_file     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(platform, order_sn)
);

CREATE TABLE marketplace_wallet_txns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT NOT NULL,
    txn_time        TEXT NOT NULL,
    txn_type        TEXT NOT NULL,          -- income | withdrawal | adjustment
    order_sn        TEXT,
    amount          REAL NOT NULL,
    running_balance REAL,
    description     TEXT,
    source_file     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(platform, txn_time, txn_type, order_sn, amount)
);

CREATE TABLE marketplace_payouts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    platform     TEXT NOT NULL,
    deposit_date TEXT NOT NULL,
    amount       REAL NOT NULL,
    n_orders     INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'reconciled',
    source_file  TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(platform, deposit_date, amount)
);

ALTER TABLE marketplace_orders ADD COLUMN payout_id INTEGER;
CREATE INDEX idx_marketplace_orders_payout_id ON marketplace_orders(payout_id);
CREATE INDEX idx_wallet_txns_platform_time ON marketplace_wallet_txns(platform, txn_time, id);

COMMIT;
```

- [ ] **Step 2: Write the rollback**

```sql
-- 107_marketplace_fees_wallet.rollback.sql
PRAGMA busy_timeout=10000;
BEGIN IMMEDIATE;
DROP INDEX IF EXISTS idx_wallet_txns_platform_time;
DROP INDEX IF EXISTS idx_marketplace_orders_payout_id;
DROP TABLE IF EXISTS marketplace_payouts;
DROP TABLE IF EXISTS marketplace_wallet_txns;
DROP TABLE IF EXISTS marketplace_order_fees;
-- marketplace_orders.payout_id is an additive nullable column; left in place
-- (dropping a column needs a table rebuild and isn't worth the risk).
COMMIT;
```

- [ ] **Step 3: Write the schema test**

```python
# tests/test_mig107_schema.py
def test_new_tables_and_column(tmp_db_conn):
    c = tmp_db_conn
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'marketplace_order_fees','marketplace_wallet_txns','marketplace_payouts'} <= names
    cols = {r[1] for r in c.execute("PRAGMA table_info(marketplace_orders)")}
    assert 'payout_id' in cols
```

> Note: `tmp_db` copies the live DB, which is already at mig ≥107 after you restart the server below. If running this test BEFORE a server boot applied 107, instead run the migration manually first (Step 4).

- [ ] **Step 4: Apply by restarting the server, then verify**

Run:
```bash
sendy-down && sendy-up && sleep 2
grep -i "107_marketplace" /tmp/sendy.log
sqlite3 inventory_app/instance/inventory.db "SELECT filename FROM applied_migrations WHERE filename LIKE '107%';"
```
Expected: log shows `[migration] applied 107_marketplace_fees_wallet`; the SELECT returns the filename.

- [ ] **Step 5: Run schema test — expect PASS**

Run: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_mig107_schema.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add data/migrations/107_marketplace_fees_wallet.sql data/migrations/107_marketplace_fees_wallet.rollback.sql tests/test_mig107_schema.py
git commit -m "feat(marketplace): mig 107 — fees + wallet ledger + payouts tables"
```

---

## Phase 2 — Income fees: store + backfill

### Task 5: `upsert_marketplace_fees` (models)

**Files:**
- Modify: `inventory_app/models.py` (add function near `upsert_marketplace_settlements`, ~line 5236)
- Test: `tests/test_upsert_marketplace_fees.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_upsert_marketplace_fees.py
import models

def test_upsert_inserts_and_updates(tmp_db_conn):
    c = tmp_db_conn
    rows = [{'order_sn':'SN1','item_value':49.0,'net_payout':35.0,'fee_total':14.0,
             'fee_commission':-10.0,'fee_service':-1.0,'fee_transaction':-1.0,
             'fee_platform':0.0,'fee_ads_escrow':0.0,'fee_tax':0.0,'shipping_net':0.0,
             'fee_saver':-2.0,'fee_pct':'3.21%','fee_raw_json':'{}'}]
    n = models.upsert_marketplace_fees(c, rows, 'f.xlsx')
    assert n == 1
    got = c.execute("SELECT net_payout, fee_commission FROM marketplace_order_fees WHERE order_sn='SN1'").fetchone()
    assert got['net_payout'] == 35.0 and got['fee_commission'] == -10.0
    # re-run with changed net = update, not duplicate
    rows[0]['net_payout'] = 30.0
    models.upsert_marketplace_fees(c, rows, 'f2.xlsx')
    rows_db = c.execute("SELECT net_payout FROM marketplace_order_fees WHERE order_sn='SN1'").fetchall()
    assert len(rows_db) == 1 and rows_db[0]['net_payout'] == 30.0
```

- [ ] **Step 2: Run test — expect FAIL** (`AttributeError: ... 'upsert_marketplace_fees'`)

Run: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_upsert_marketplace_fees.py -v`

- [ ] **Step 3: Implement** (add to `models.py`)

```python
def upsert_marketplace_fees(conn, fee_rows, source_file=None, platform='shopee'):
    """Insert/replace per-order fee rows into marketplace_order_fees.
    Keyed UNIQUE(platform, order_sn). Returns count upserted."""
    n = 0
    for f in fee_rows:
        conn.execute(
            """INSERT INTO marketplace_order_fees
                 (platform, order_sn, item_value, fee_commission, fee_service,
                  fee_transaction, fee_platform, fee_ads_escrow, fee_tax,
                  shipping_net, fee_saver, fee_total, net_payout, fee_pct,
                  fee_raw_json, source_file)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(platform, order_sn) DO UPDATE SET
                  item_value=excluded.item_value, fee_commission=excluded.fee_commission,
                  fee_service=excluded.fee_service, fee_transaction=excluded.fee_transaction,
                  fee_platform=excluded.fee_platform, fee_ads_escrow=excluded.fee_ads_escrow,
                  fee_tax=excluded.fee_tax, shipping_net=excluded.shipping_net,
                  fee_saver=excluded.fee_saver, fee_total=excluded.fee_total,
                  net_payout=excluded.net_payout, fee_pct=excluded.fee_pct,
                  fee_raw_json=excluded.fee_raw_json, source_file=excluded.source_file""",
            (platform, f['order_sn'], f.get('item_value'), f.get('fee_commission', 0),
             f.get('fee_service', 0), f.get('fee_transaction', 0), f.get('fee_platform', 0),
             f.get('fee_ads_escrow', 0), f.get('fee_tax', 0), f.get('shipping_net', 0),
             f.get('fee_saver', 0), f.get('fee_total'), f.get('net_payout'),
             f.get('fee_pct'), f.get('fee_raw_json'), source_file))
        n += 1
    conn.commit()
    return n
```

- [ ] **Step 4: Run test — expect PASS**

Run: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_upsert_marketplace_fees.py -v`

- [ ] **Step 5: Commit**

```bash
git add inventory_app/models.py tests/test_upsert_marketplace_fees.py
git commit -m "feat(marketplace): upsert_marketplace_fees model helper"
```

---

### Task 6: Wire fees into the Income import + backfill history

**Files:**
- Modify: `inventory_app/blueprints/marketplace.py` (`settlement_import`, ~line 128-137)
- Backfill (one-off): run inline python (no file)

- [ ] **Step 1: Extend `settlement_import` to also store fees**

In `settlement_import`, the same parsed `df` feeds both helpers (no re-read). After the existing `settlements = parse_shopee_income(df)` line, add one line:

```python
        df = load_income_sheet(io.BytesIO(f.read()))
        settlements = parse_shopee_income(df)
        fee_rows = parse_shopee_income_fees(df)   # <-- add (reuses the same df)
```
Then inside the `conn` block, after `upsert_marketplace_settlements(...)`:

```python
            stats = models.upsert_marketplace_settlements(conn, settlements, f.filename)
            fee_n = models.upsert_marketplace_fees(conn, fee_rows, f.filename)
            match = marketplace_match.run_automatch(conn, 'shopee')
```
Add `parse_shopee_income_fees` to the import at top of file:
```python
from parse_income_transfer import (parse_shopee_income, IncomeTransferError,
                                   load_income_sheet, parse_shopee_income_fees)
```
And append to the flash message: `+ f' · ค่าธรรมเนียม {fee_n} ออเดอร์'`.

- [ ] **Step 2: Restart + smoke the route**

Run:
```bash
sendy-down && sendy-up && sleep 2
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5001/marketplace/settlement
```
Expected: `200`.

- [ ] **Step 3: Backfill historical fees from the full-year Income file**

Run:
```bash
cd sendy_erp && ~/.virtualenvs/erp/bin/python - <<'PY'
import io, models
from parse_income_transfer import load_income_sheet, parse_shopee_income_fees
from database import get_connection
f="/Users/putty/Downloads/Income.โอนเงินสำเร็จ.th.20260101_20260616.xlsx"
df=load_income_sheet(f)
rows=parse_shopee_income_fees(df)
c=get_connection()
n=models.upsert_marketplace_fees(c, rows, "Income.20260101_20260616.xlsx")
# verify the satang identity for orders that also have settlement net
bad=c.execute("""SELECT COUNT(*) FROM marketplace_order_fees f JOIN marketplace_orders o
  ON o.platform='shopee' AND o.order_sn=f.order_sn AND o.actual_payout IS NOT NULL
  WHERE ABS(f.net_payout - o.actual_payout) > 0.01""").fetchone()[0]
print("fees upserted:", n, "| net mismatches vs settlement:", bad)
c.close()
PY
```
Expected: `fees upserted: <hundreds>` and `net mismatches vs settlement: 0` (the Income net must equal the settlement actual_payout).

- [ ] **Step 4: Commit**

```bash
git add inventory_app/blueprints/marketplace.py
git commit -m "feat(marketplace): capture per-order fees on Income import + backfill"
```

---

## Phase 3 — Balance import + reconciliation

### Task 7: `import_wallet_txns` (models)

**Files:**
- Modify: `inventory_app/models.py`
- Test: `tests/test_import_wallet_txns.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_import_wallet_txns.py
import models

def _rows():
    return [
      {'txn_time':'2026-06-15 13:55:39','txn_type':'income','order_sn':'SN1','amount':35.0,'running_balance':35.0,'description':'#A'},
      {'txn_time':'2026-06-16 01:17:03','txn_type':'withdrawal','order_sn':None,'amount':-35.0,'running_balance':0.0,'description':'auto'},
    ]

def test_insert_is_idempotent(tmp_db_conn):
    c = tmp_db_conn
    a = models.import_wallet_txns(c, _rows(), 'bal.xlsx')
    b = models.import_wallet_txns(c, _rows(), 'bal.xlsx')   # re-import same file
    assert a == 2 and b == 0
    total = c.execute("SELECT COUNT(*) FROM marketplace_wallet_txns").fetchone()[0]
    assert total == 2
```

- [ ] **Step 2: Run test — expect FAIL**

Run: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_import_wallet_txns.py -v`

- [ ] **Step 3: Implement** (add to `models.py`)

```python
def import_wallet_txns(conn, wallet_rows, source_file=None, platform='shopee'):
    """Insert wallet ledger rows. Idempotent via UNIQUE(platform,txn_time,
    txn_type,order_sn,amount) + INSERT OR IGNORE. Returns count newly inserted."""
    n = 0
    for r in wallet_rows:
        cur = conn.execute(
            """INSERT OR IGNORE INTO marketplace_wallet_txns
                 (platform, txn_time, txn_type, order_sn, amount, running_balance,
                  description, source_file)
               VALUES (?,?,?,?,?,?,?,?)""",
            (platform, r['txn_time'], r['txn_type'], r.get('order_sn'),
             r['amount'], r.get('running_balance'), r.get('description'), source_file))
        n += cur.rowcount
    conn.commit()
    return n
```

- [ ] **Step 4: Run — expect PASS**; **Step 5: Commit**

```bash
cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_import_wallet_txns.py -v
git add inventory_app/models.py tests/test_import_wallet_txns.py
git commit -m "feat(marketplace): idempotent wallet-txn import"
```

---

### Task 8: Reconciliation engine (`marketplace_reconcile.py`)

**Files:**
- Create: `inventory_app/marketplace_reconcile.py`
- Test: `tests/test_marketplace_reconcile.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_marketplace_reconcile.py
import models, marketplace_reconcile

def _seed_orders(c, sns):
    for sn in sns:
        c.execute("INSERT INTO marketplace_orders (platform, order_sn) VALUES ('shopee', ?)", (sn,))
    c.commit()

def test_two_cycles_assign_payouts(tmp_db_conn):
    c = tmp_db_conn
    _seed_orders(c, ['A','B','C','D'])
    wallet = [
      {'txn_time':'2026-06-02 10:00','txn_type':'income','order_sn':'A','amount':100.0,'running_balance':100.0,'description':''},
      {'txn_time':'2026-06-02 11:00','txn_type':'income','order_sn':'B','amount':50.0,'running_balance':150.0,'description':''},
      {'txn_time':'2026-06-09 01:19','txn_type':'withdrawal','order_sn':None,'amount':-150.0,'running_balance':0.0,'description':'w1'},
      {'txn_time':'2026-06-10 10:00','txn_type':'income','order_sn':'C','amount':70.0,'running_balance':70.0,'description':''},
      {'txn_time':'2026-06-12 10:00','txn_type':'income','order_sn':'D','amount':30.0,'running_balance':100.0,'description':''},
      {'txn_time':'2026-06-16 01:17','txn_type':'withdrawal','order_sn':None,'amount':-100.0,'running_balance':0.0,'description':'w2'},
    ]
    models.import_wallet_txns(c, wallet, 'bal.xlsx')
    res = marketplace_reconcile.reconcile_payouts(c, 'shopee')
    assert res['payouts'] == 2
    payouts = c.execute("SELECT deposit_date, amount, n_orders FROM marketplace_payouts ORDER BY deposit_date").fetchall()
    assert (payouts[0]['amount'], payouts[0]['n_orders']) == (150.0, 2)
    assert (payouts[1]['amount'], payouts[1]['n_orders']) == (100.0, 2)
    # orders A,B linked to payout 1; C,D to payout 2
    p2 = c.execute("SELECT id FROM marketplace_payouts WHERE deposit_date='2026-06-16'").fetchone()['id']
    linked = [r['order_sn'] for r in c.execute("SELECT order_sn FROM marketplace_orders WHERE payout_id=? ORDER BY order_sn",(p2,))]
    assert linked == ['C','D']

def test_idempotent_rerun(tmp_db_conn):
    c = tmp_db_conn
    _seed_orders(c, ['A'])
    models.import_wallet_txns(c, [
      {'txn_time':'2026-06-02 10:00','txn_type':'income','order_sn':'A','amount':10.0,'running_balance':10.0,'description':''},
      {'txn_time':'2026-06-09 01:19','txn_type':'withdrawal','order_sn':None,'amount':-10.0,'running_balance':0.0,'description':'w'},
    ], 'b.xlsx')
    marketplace_reconcile.reconcile_payouts(c, 'shopee')
    marketplace_reconcile.reconcile_payouts(c, 'shopee')
    assert c.execute("SELECT COUNT(*) FROM marketplace_payouts").fetchone()[0] == 1

def test_mismatch_raises(tmp_db_conn):
    c = tmp_db_conn
    _seed_orders(c, ['A'])
    models.import_wallet_txns(c, [
      {'txn_time':'2026-06-02 10:00','txn_type':'income','order_sn':'A','amount':10.0,'running_balance':10.0,'description':''},
      {'txn_time':'2026-06-09 01:19','txn_type':'withdrawal','order_sn':None,'amount':-99.0,'running_balance':0.0,'description':'w'},
    ], 'b.xlsx')
    import pytest
    with pytest.raises(marketplace_reconcile.ReconcileError):
        marketplace_reconcile.reconcile_payouts(c, 'shopee')
```

- [ ] **Step 2: Run test — expect FAIL**

Run: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_marketplace_reconcile.py -v`

- [ ] **Step 3: Implement**

```python
# inventory_app/marketplace_reconcile.py
"""Segment the wallet ledger into bank-deposit cycles.

Each 'withdrawal' row closes a cycle: it is a real bank deposit equal to the
sum of every income + adjustment row since the previous withdrawal. We write
one marketplace_payouts row per cycle and link its orders via
marketplace_orders.payout_id. Rebuilds from scratch each run (idempotent).
"""
_TOL = 0.01


class ReconcileError(Exception):
    pass


def reconcile_payouts(conn, platform='shopee'):
    """Rebuild marketplace_payouts + order links from marketplace_wallet_txns.
    Returns {'payouts': int, 'orders_linked': int}. Raises ReconcileError if a
    cycle's order+adjustment sum != withdrawal amount (incomplete ledger)."""
    # clear prior links + payouts for this platform
    conn.execute("UPDATE marketplace_orders SET payout_id = NULL WHERE platform = ?", (platform,))
    conn.execute("DELETE FROM marketplace_payouts WHERE platform = ?", (platform,))

    rows = conn.execute(
        """SELECT txn_time, txn_type, order_sn, amount FROM marketplace_wallet_txns
           WHERE platform = ? ORDER BY txn_time ASC, id ASC""", (platform,)).fetchall()

    n_payouts = 0
    n_linked = 0
    cur_orders = []      # order_sns in the open cycle
    cur_income = 0.0     # income + adjustment total in the open cycle
    for r in rows:
        if r['txn_type'] == 'withdrawal':
            wd = round(-r['amount'], 2)   # withdrawal amount stored negative
            if abs(round(cur_income, 2) - wd) > _TOL:
                raise ReconcileError(
                    f"cycle ending {r['txn_time']}: income {round(cur_income,2)} "
                    f"!= withdrawal {wd}")
            pid = conn.execute(
                """INSERT INTO marketplace_payouts
                     (platform, deposit_date, amount, n_orders, status)
                   VALUES (?,?,?,?, 'reconciled')""",
                (platform, str(r['txn_time'])[:10], wd, len(cur_orders))).lastrowid
            if cur_orders:
                qs = ','.join('?' * len(cur_orders))
                conn.execute(
                    f"""UPDATE marketplace_orders SET payout_id = ?
                        WHERE platform = ? AND order_sn IN ({qs})""",
                    [pid, platform, *cur_orders])
                n_linked += len(cur_orders)
            n_payouts += 1
            cur_orders, cur_income = [], 0.0
        else:  # income | adjustment
            cur_income += (r['amount'] or 0)
            if r['order_sn']:
                cur_orders.append(r['order_sn'])
    conn.commit()
    return {'payouts': n_payouts, 'orders_linked': n_linked}
```

- [ ] **Step 4: Run — expect PASS** (3 passed); **Step 5: Commit**

```bash
cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_marketplace_reconcile.py -v
git add inventory_app/marketplace_reconcile.py tests/test_marketplace_reconcile.py
git commit -m "feat(marketplace): withdrawal-cycle reconciliation engine"
```

---

### Task 9: Balance import route + backfill + the real-data oracle check

**Files:**
- Modify: `inventory_app/blueprints/marketplace.py` (add `/marketplace/balance-import` POST)
- Test: `tests/test_balance_reconcile_real.py` (integration, skips if file absent)

- [ ] **Step 1: Add the balance-import route**

Add imports at top of `marketplace.py`:
```python
import marketplace_reconcile
from parse_balance import parse_shopee_balance, load_balance_sheet, BalanceError
```
Add route:
```python
@bp_marketplace.route('/marketplace/balance-import', methods=['POST'])
def balance_import():
    f = request.files.get('balance_file')
    if not f or f.filename == '':
        flash('กรุณาเลือกไฟล์ Seller Balance (.xlsx)', 'warning')
        return redirect(url_for('marketplace.settlement'))
    try:
        df = load_balance_sheet(io.BytesIO(f.read()))
        wallet = parse_shopee_balance(df)
    except BalanceError as e:
        flash(str(e), 'danger')
        return redirect(url_for('marketplace.settlement'))
    except Exception as e:
        flash(f'อ่านไฟล์ไม่ได้: {e}', 'danger')
        return redirect(url_for('marketplace.settlement'))
    _info, _err = db_backup.safe_create_backup(
        'marketplace_balance', db_path=config.DATABASE_PATH,
        backup_dir=db_backup.default_backup_dir(config.DATABASE_PATH))
    conn = get_connection()
    try:
        ins = models.import_wallet_txns(conn, wallet, f.filename)
        try:
            rec = marketplace_reconcile.reconcile_payouts(conn, 'shopee')
        except marketplace_reconcile.ReconcileError as e:
            flash(f'นำเข้าแล้ว {ins} รายการ แต่กระทบยอดไม่ลงตัว: {e} '
                  '(ไฟล์ Balance อาจไม่ครบช่วง) — ตรวจดูยอดโอนค่ะ', 'warning')
            return redirect(url_for('marketplace.settlement'))
    finally:
        conn.close()
    flash(f'นำเข้า Balance สำเร็จ: เพิ่ม {ins} รายการ · ยอดโอนเข้าบัญชี '
          f'{rec["payouts"]} ก้อน ({rec["orders_linked"]} ออเดอร์)', 'success')
    return redirect(url_for('marketplace.settlement'))
```

- [ ] **Step 2: Restart + smoke**

Run:
```bash
sendy-down && sendy-up && sleep 2
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5001/marketplace/settlement
```
Expected: `200`.

- [ ] **Step 3: Backfill the real balance file + assert the oracle**

```bash
cd sendy_erp && ~/.virtualenvs/erp/bin/python - <<'PY'
import models, marketplace_reconcile
from parse_balance import load_balance_sheet, parse_shopee_balance
from database import get_connection
f="/Users/putty/Downloads/my_balance_transaction_report.shopee.20260101_20260616.xlsx"
c=get_connection()
ins=models.import_wallet_txns(c, parse_shopee_balance(load_balance_sheet(f)), "balance.20260101_20260616.xlsx")
rec=marketplace_reconcile.reconcile_payouts(c, 'shopee')
print("wallet inserted:", ins, "| payouts:", rec)
for amt in (7689.0, 5890.0):
    row=c.execute("SELECT id,deposit_date,n_orders FROM marketplace_payouts WHERE ABS(amount-?)<0.01",(amt,)).fetchone()
    print(f"  ฿{amt:.0f} -> deposit {row['deposit_date']} n_orders={row['n_orders']}")
c.close()
PY
```
Expected: `฿7689 -> ... n_orders=39` and `฿5890 -> ... n_orders=32` (the verified oracle).

- [ ] **Step 4: Write the integration test** (guards the oracle for CI)

```python
# tests/test_balance_reconcile_real.py
import os, pytest, models, marketplace_reconcile
from parse_balance import load_balance_sheet, parse_shopee_balance

F = os.path.expanduser("~/Downloads/my_balance_transaction_report.shopee.20260101_20260616.xlsx")

@pytest.mark.skipif(not os.path.exists(F), reason="real balance file not present")
def test_7689_and_5890_cycles(tmp_db_conn):
    c = tmp_db_conn
    models.import_wallet_txns(c, parse_shopee_balance(load_balance_sheet(F)), "real.xlsx")
    marketplace_reconcile.reconcile_payouts(c, 'shopee')
    def n(amt): return c.execute("SELECT n_orders FROM marketplace_payouts WHERE ABS(amount-?)<0.01",(amt,)).fetchone()['n_orders']
    assert n(7689.0) == 39
    assert n(5890.0) == 32
```

- [ ] **Step 5: Run + commit**

```bash
cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_balance_reconcile_real.py -v
git add inventory_app/blueprints/marketplace.py tests/test_balance_reconcile_real.py
git commit -m "feat(marketplace): balance import + reconcile route + real-data oracle test"
```

---

## Phase 4 — Unified auto-detecting upload box

### Task 10: One upload route that detects + routes

**Files:**
- Modify: `inventory_app/blueprints/marketplace.py` (add `/marketplace/upload` POST)

- [ ] **Step 1: Implement the unified route** (reuses the three importers above)

```python
from marketplace_files import detect_file

@bp_marketplace.route('/marketplace/upload', methods=['POST'])
def upload():
    """One box for all marketplace files; detects kind+platform and routes."""
    files = request.files.getlist('files')
    files = [f for f in files if f and f.filename]
    if not files:
        flash('กรุณาเลือกไฟล์ค่ะ', 'warning')
        return redirect(url_for('marketplace.settlement'))
    _info, _err = db_backup.safe_create_backup(
        'marketplace_upload', db_path=config.DATABASE_PATH,
        backup_dir=db_backup.default_backup_dir(config.DATABASE_PATH))
    msgs, need_reconcile = [], False
    conn = get_connection()
    try:
        for f in files:
            data = f.read()
            kind, platform = detect_file(io.BytesIO(data))
            if kind is None:
                msgs.append(f'⚠️ {f.filename}: ไม่รู้จักชนิดไฟล์'); continue
            if kind == 'order':
                df = pd.read_excel(io.BytesIO(data), sheet_name=0, header=0, dtype=str)
                orders = (parse_shopee_orders(df) if platform == 'shopee'
                          else parse_lazada_orders(df))
                s = models.import_marketplace_orders(conn, orders, f.filename)
                msgs.append(f'📦 {f.filename}: ออเดอร์ {s["orders"]} (ใหม่), จับคู่ {s["lines_resolved"]}')
            elif kind == 'income':
                df = load_income_sheet(io.BytesIO(data))
                models.upsert_marketplace_settlements(conn, parse_shopee_income(df), f.filename)
                fn = models.upsert_marketplace_fees(conn, parse_shopee_income_fees(df), f.filename)
                marketplace_match.run_automatch(conn, 'shopee')
                msgs.append(f'💰 {f.filename}: ค่าธรรมเนียม+ยอดโอน {fn} ออเดอร์')
            elif kind == 'balance':
                ins = models.import_wallet_txns(conn, parse_shopee_balance(load_balance_sheet(io.BytesIO(data))), f.filename)
                need_reconcile = True
                msgs.append(f'🏦 {f.filename}: รายการกระเป๋าเงิน +{ins}')
        if need_reconcile:
            try:
                rec = marketplace_reconcile.reconcile_payouts(conn, 'shopee')
                msgs.append(f'↔ กระทบยอดโอน {rec["payouts"]} ก้อน ({rec["orders_linked"]} ออเดอร์)')
            except marketplace_reconcile.ReconcileError as e:
                msgs.append(f'⚠️ กระทบยอดไม่ลงตัว: {e}')
    except Exception as e:
        conn.close()
        flash(f'นำเข้าไม่สำเร็จ: {e}', 'danger')
        return redirect(url_for('marketplace.settlement'))
    conn.close()
    flash(' · '.join(msgs), 'success')
    return redirect(url_for('marketplace.settlement'))
```

- [ ] **Step 2: Restart + smoke each real file through the box**

Run:
```bash
sendy-down && sendy-up && sleep 2
for ff in \
  "$HOME/Downloads/my_balance_transaction_report.shopee.20260101_20260616.xlsx" \
  "$HOME/Downloads/Income.โอนเงินสำเร็จ.th.20260601_20260616.xlsx"; do
  curl -s -o /dev/null -w "%{http_code} " -F "files=@${ff}" \
    -F "csrf_token=$(curl -s -c /tmp/cj http://127.0.0.1:5001/marketplace/settlement | grep -o 'csrf-token\" content=\"[^\"]*' | sed 's/.*content=\"//')" \
    -b /tmp/cj http://127.0.0.1:5001/marketplace/upload; echo "$ff"
done
```
Expected: `302 <file>` for each (redirect after import). If CSRF blocks (400), test the upload via the browser instead and note it for Put.

- [ ] **Step 3: Commit**

```bash
git add inventory_app/blueprints/marketplace.py
git commit -m "feat(marketplace): unified auto-detecting upload route"
```

---

## Phase 5 — UI: deposit list + per-order fee section

### Task 11: `get_payout_report` + settlement page deposit list

**Files:**
- Modify: `inventory_app/models.py` (add `get_payout_report`)
- Modify: `inventory_app/blueprints/marketplace.py` (`settlement` passes payout report)
- Modify: `inventory_app/templates/marketplace/settlement.html` (add the deposit tab + the unified upload box)
- Test: `tests/test_get_payout_report.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_get_payout_report.py
import models, marketplace_reconcile

def test_payout_report_groups_orders_with_fees(tmp_db_conn):
    c = tmp_db_conn
    c.execute("INSERT INTO marketplace_orders (platform, order_sn) VALUES ('shopee','A')")
    c.execute("INSERT INTO marketplace_orders (platform, order_sn) VALUES ('shopee','B')")
    models.upsert_marketplace_fees(c, [
      {'order_sn':'A','item_value':60.0,'net_payout':50.0,'fee_total':10.0},
      {'order_sn':'B','item_value':30.0,'net_payout':25.0,'fee_total':5.0}], 'f.xlsx')
    models.import_wallet_txns(c, [
      {'txn_time':'2026-06-02 10:00','txn_type':'income','order_sn':'A','amount':50.0,'running_balance':50.0,'description':''},
      {'txn_time':'2026-06-02 11:00','txn_type':'income','order_sn':'B','amount':25.0,'running_balance':75.0,'description':''},
      {'txn_time':'2026-06-09 01:19','txn_type':'withdrawal','order_sn':None,'amount':-75.0,'running_balance':0.0,'description':'w'}], 'b.xlsx')
    marketplace_reconcile.reconcile_payouts(c, 'shopee')
    rep = models.get_payout_report(c, 'shopee')
    assert len(rep) == 1
    d = rep[0]
    assert d['amount'] == 75.0 and d['n_orders'] == 2
    assert round(d['fee_total'], 2) == 15.0
    assert {o['order_sn'] for o in d['orders']} == {'A','B'}
```

- [ ] **Step 2: Run — expect FAIL**

Run: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_get_payout_report.py -v`

- [ ] **Step 3: Implement `get_payout_report`** (add to `models.py`)

```python
def get_payout_report(conn, platform='shopee', limit=24):
    """Bank deposits (newest first) with their orders + fee join, for the
    settlement page. Each deposit: id, deposit_date, amount, n_orders,
    fee_total (Σ order fees), orders:[{order_sn, settled_at, item_value,
    fee_total, net_payout, fee_pct}]."""
    payouts = conn.execute(
        """SELECT id, deposit_date, amount, n_orders
           FROM marketplace_payouts WHERE platform = ?
           ORDER BY deposit_date DESC, id DESC LIMIT ?""", (platform, limit)).fetchall()
    out = []
    for p in payouts:
        orders = conn.execute(
            """SELECT o.order_sn, o.settled_at,
                      f.item_value, f.fee_total, f.net_payout, f.fee_pct
               FROM marketplace_orders o
               LEFT JOIN marketplace_order_fees f
                      ON f.platform = o.platform AND f.order_sn = o.order_sn
               WHERE o.platform = ? AND o.payout_id = ?
               ORDER BY o.settled_at, o.order_sn""", (platform, p['id'])).fetchall()
        out.append({
            'id': p['id'], 'deposit_date': p['deposit_date'], 'amount': p['amount'],
            'n_orders': p['n_orders'],
            'fee_total': round(sum((o['fee_total'] or 0) for o in orders), 2),
            'orders': [dict(o) for o in orders],
        })
    return out
```

- [ ] **Step 4: Pass it to the template** — in `settlement()` add `payout_report=models.get_payout_report(conn, platform)` to the `render_template` call.

- [ ] **Step 5: Add the deposit tab + unified upload box to `settlement.html`**

Replace the upload `<form>` (lines ~9-15) with the unified box, and add a deposit tab. Insert this tab item into the `<ul class="nav nav-tabs">` as the FIRST tab and make it active by default:

```html
    <li class="nav-item">
      <a class="nav-link {{ 'active' if tab == 'deposits' else '' }}"
         href="{{ url_for('marketplace.settlement', platform=platform, tab='deposits') }}">
        เงินเข้าบัญชี (ยอดโอน)
      </a>
    </li>
```

Replace the header upload form with:
```html
    <form method="post" action="{{ url_for('marketplace.upload') }}"
          enctype="multipart/form-data" class="d-flex gap-2 align-items-center">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <input type="file" name="files" accept=".xlsx" multiple class="form-control form-control-sm">
      <button type="submit" class="btn btn-sm btn-primary">นำเข้าไฟล์ (Order/การเงิน/Balance)</button>
    </form>
```

Add the deposit tab body (place before the `{% if tab == 'daily' %}` block):
```html
  {% if tab == 'deposits' %}
  {% if payout_report %}
    {% for d in payout_report %}
    <div class="card mb-3">
      <div class="card-header d-flex justify-content-between align-items-center"
           role="button" data-bs-toggle="collapse" data-bs-target="#dep{{ d.id }}">
        <strong>เงินเข้าบัญชี {{ d.deposit_date }}</strong>
        <span><span class="text-muted small me-3">ค่าธรรมเนียมรวม ฿{{ '{:,.2f}'.format(d.fee_total) }}</span>
          <span class="text-success fw-bold">฿{{ '{:,.2f}'.format(d.amount) }}</span>
          <span class="text-muted">· {{ d.n_orders }} ออเดอร์</span></span>
      </div>
      <div class="collapse" id="dep{{ d.id }}">
        <div class="card-body p-0">
          <table class="table table-sm table-hover mb-0">
            <thead class="table-light"><tr>
              <th>เลขออเดอร์</th><th>วันโอน</th><th class="text-end">มูลค่าสินค้า</th>
              <th class="text-end">ค่าธรรมเนียม</th><th class="text-end">ยอดสุทธิ</th><th class="text-end">%</th>
            </tr></thead>
            <tbody>
              {% for o in d.orders %}
              <tr>
                <td><code>{{ o.order_sn }}</code></td>
                <td class="small">{{ o.settled_at or '–' }}</td>
                <td class="text-end">{{ '{:,.2f}'.format(o.item_value or 0) }}</td>
                <td class="text-end text-danger">{{ '{:,.2f}'.format(o.fee_total or 0) }}</td>
                <td class="text-end fw-bold">{{ '{:,.2f}'.format(o.net_payout or 0) }}</td>
                <td class="text-end small">{{ o.fee_pct or '' }}</td>
              </tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </div>
    </div>
    {% endfor %}
  {% else %}
    <div class="alert alert-info">ยังไม่มีข้อมูลยอดโอน — อัปโหลดไฟล์ Seller Balance ด้านบนค่ะ</div>
  {% endif %}
  {% endif %}
```

Also change the default tab in `settlement()` route: `tab = request.args.get('tab', 'deposits')`.

- [ ] **Step 6: Restart + verify both tabs render non-500**

Run:
```bash
sendy-down && sendy-up && sleep 2
for t in deposits daily batch; do
  echo -n "$t "; curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:5001/marketplace/settlement?tab=$t"
done
```
Expected: `deposits 200`, `daily 200`, `batch 200`.

- [ ] **Step 7: Run report test + commit**

```bash
cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_get_payout_report.py -v
git add inventory_app/models.py inventory_app/blueprints/marketplace.py inventory_app/templates/marketplace/settlement.html
git commit -m "feat(marketplace): deposit-list settlement tab + unified upload box"
```

---

### Task 12: Per-order fee section in the order modal

**Files:**
- Modify: `inventory_app/models.py` (`get_marketplace_order_detail` — add fees + payout)
- Modify: `inventory_app/templates/marketplace/_order_detail_modal.html` (render fees)
- Test: `tests/test_order_detail_fees.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_order_detail_fees.py
import models

def test_detail_includes_fees_and_payout(tmp_db_conn):
    c = tmp_db_conn
    c.execute("INSERT INTO marketplace_orders (platform, order_sn) VALUES ('shopee','Z1')")
    oid = c.execute("SELECT id FROM marketplace_orders WHERE order_sn='Z1'").fetchone()['id']
    models.upsert_marketplace_fees(c, [{'order_sn':'Z1','item_value':100.0,'net_payout':80.0,
        'fee_total':20.0,'fee_commission':-12.0,'fee_service':-3.0,'fee_transaction':-2.0,
        'fee_platform':-1.0,'fee_ads_escrow':-2.0,'fee_tax':0.0,'shipping_net':0.0,
        'fee_saver':0.0,'fee_pct':'20%'}], 'f.xlsx')
    pid = c.execute("INSERT INTO marketplace_payouts (platform,deposit_date,amount,n_orders) VALUES ('shopee','2026-06-16',80.0,1)").lastrowid
    c.execute("UPDATE marketplace_orders SET payout_id=? WHERE id=?",(pid,oid)); c.commit()
    d = models.get_marketplace_order_detail(c, oid)
    assert d['fees']['fee_commission'] == -12.0
    assert d['fees']['net_payout'] == 80.0
    assert d['payout']['deposit_date'] == '2026-06-16'
```

- [ ] **Step 2: Run — expect FAIL**

Run: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_order_detail_fees.py -v`

- [ ] **Step 3: Extend `get_marketplace_order_detail`** — before `return {'order':..., 'items':...}`, add:

```python
    fees = conn.execute(
        """SELECT item_value, fee_commission, fee_service, fee_transaction,
                  fee_platform, fee_ads_escrow, fee_tax, shipping_net, fee_saver,
                  fee_total, net_payout, fee_pct
           FROM marketplace_order_fees
           WHERE platform = ? AND order_sn = ?""",
        (o['platform'], o['order_sn'])).fetchone()
    payout = conn.execute(
        """SELECT p.deposit_date, p.amount FROM marketplace_payouts p
           JOIN marketplace_orders mo ON mo.payout_id = p.id
           WHERE mo.id = ?""", (order_id,)).fetchone()
    return {'order': dict(o), 'items': [dict(r) for r in items],
            'fees': dict(fees) if fees else None,
            'payout': dict(payout) if payout else None}
```

- [ ] **Step 4: Render fees in `_order_detail_modal.html`** — add a container after the `<dl>` (before the items table, ~line 25) :

```html
          <div id="odFees" class="mb-2"></div>
```
And in the `.then(d => {...})` JS block, after the `odIv` line, add:
```javascript
        const ff = d.fees, fl = (lbl, v) => (v == null || Math.abs(v) < 0.005) ? '' :
          `<div class="d-flex justify-content-between"><span class="text-muted">${lbl}</span><span class="${v<0?'text-danger':''}">฿${fmt(v)}</span></div>`;
        document.getElementById('odFees').innerHTML = !ff ? '' : `
          <div class="border rounded p-2 bg-light">
            <div class="fw-bold mb-1" style="color:#c41e2a"><i class="bi bi-cash-stack"></i> ค่าธรรมเนียม & เงินโอน</div>
            <div class="d-flex justify-content-between"><span class="text-muted">มูลค่าสินค้า</span><span>฿${fmt(ff.item_value)}</span></div>
            ${fl('ค่าคอมมิชชั่น', ff.fee_commission)}${fl('ค่าบริการ', ff.fee_service)}
            ${fl('ค่าธุรกรรมการชำระเงิน', ff.fee_transaction)}${fl('ค่าธรรมเนียมแพลตฟอร์ม', ff.fee_platform)}
            ${fl('ค่าโฆษณา (Escrow)', ff.fee_ads_escrow)}${fl('ภาษี', ff.fee_tax)}
            ${fl('ค่าจัดส่ง (สุทธิ)', ff.shipping_net)}${fl('ค่าประหยัดค่าส่ง', ff.fee_saver)}
            <div class="d-flex justify-content-between border-top mt-1 pt-1 fw-bold"><span>ยอดสุทธิที่ได้รับ</span><span class="text-success">฿${fmt(ff.net_payout)}</span></div>
            ${d.payout ? `<div class="small text-muted mt-1"><i class="bi bi-bank"></i> เข้าบัญชีในยอดโอน ${d.payout.deposit_date} (฿${fmt(d.payout.amount)})</div>` : ''}
          </div>`;
```

- [ ] **Step 5: Restart, run test, smoke the API**

Run:
```bash
sendy-down && sendy-up && sleep 2
cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_order_detail_fees.py -v
# pick a settled order id and confirm fees in the JSON:
OID=$(sqlite3 inventory_app/instance/inventory.db "SELECT o.id FROM marketplace_orders o JOIN marketplace_order_fees f ON f.order_sn=o.order_sn WHERE o.payout_id IS NOT NULL LIMIT 1;")
curl -s "http://127.0.0.1:5001/marketplace/api/order/$OID" | python3 -m json.tool | grep -E "fee_commission|net_payout|deposit_date"
```
Expected: test PASS; JSON shows `fee_commission`, `net_payout`, `deposit_date`.

- [ ] **Step 6: Commit**

```bash
git add inventory_app/models.py inventory_app/templates/marketplace/_order_detail_modal.html tests/test_order_detail_fees.py
git commit -m "feat(marketplace): per-order fee + payout section in order modal"
```

---

## Final verification (before handing to Put)

- [ ] **Full test suite green:** `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest -q` (no new failures).
- [ ] **Real app booted on the latest commit** (`sendy-down && sendy-up`), all routes 200:
  `/marketplace/settlement?tab=deposits`, `?tab=daily`, `/marketplace` , one `/marketplace/api/order/<id>`.
- [ ] **Browser check (Put, ~2 min):** open `/marketplace/settlement`, confirm the ฿7,689 deposit row expands to 39 orders with fees; open an order modal and confirm the fee section.
- [ ] **Do NOT merge to main without Put.** Merge = Railway deploy. When Put approves: `git fetch origin`, rebase if needed, open PR, after merge verify prod `/healthz` 200 + `applied_migrations` has 107.
- [ ] **Data → prod:** local-only data (fees/wallet/payouts) is NOT pushed by master-only upload. After deploy, re-import the same 3 files on prod through the new `/marketplace/upload` box (idempotent) OR follow the local→prod sync SOP.

## Out of scope (later — do not build now)
- P6: fee-analytics dashboard; Nami/cashbook posting of ads-from-escrow + commission as expenses; Lazada/TikTok parsers (interface is ready — add a `parse_lazada_balance` etc. and extend `detect_file`).
- Retiring `payout_batches`/`payout_batch_id` (mig 105) — left deprecated in place; remove in a future cleanup migration once the deposit list is confirmed in production use.
