"""Marketplace ORDER-export parsers (Shopee, Lazada).

Distinct from parse_platform.py, which parses the Mass-Update LISTING file.
These parse the per-order export downloaded from the Seller Center and return a
list of normalized order dicts:

    {
        'platform': 'shopee',
        'order_sn': '260530R46WR25G',
        'status': 'ที่ต้องจัดส่ง',
        'order_date': '2026-05-30 15:32',
        'paid_date': '2026-05-30 16:02',
        'buyer_name': '...', 'buyer_phone': '...', 'ship_address': '...',
        'item_total': 9.0,            # sum of line subtotals (pre-fee)
        'marketplace_fee': 2.0,       # commission + transaction fee + service fee
        'payout': 9.0,                # seller net (จำนวนเงินทั้งหมด) — see note
        'currency': 'THB',
        'items': [
            {'line_key': 'name|var', 'seller_sku': '', 'variation_id': None,
             'item_name': '...', 'variation_name': '...',
             'qty': 1.0, 'unit_price': 9.0, 'item_subtotal': 9.0},
            ...
        ],
    }

Resolution of item -> internal product happens in the importer (models.py),
not here: this layer is pure and DB-free so it is unit-testable on a DataFrame.

NOTE on money: `payout` is Shopee's จำนวนเงินทั้งหมด and `marketplace_fee` is the
sum of the charged-fee columns. These are the figures Shopee prints on the order
export; the *settled* payout can differ slightly and is confirmed only on the
finance/settlement report. Treat them as indicative until reconciled.
"""

from collections import OrderedDict
from datetime import datetime
from typing import Optional


# --- Shopee order-export column headers (Thai) ---
class _SP:
    ORDER      = 'หมายเลขคำสั่งซื้อ'
    STATUS     = 'สถานะการสั่งซื้อ'
    ORDER_DATE = 'วันที่ทำการสั่งซื้อ'
    PAID_TIME  = 'เวลาการชำระสินค้า'
    ITEM_NAME  = 'ชื่อสินค้า'
    SKU_REF    = 'เลขอ้างอิง SKU (SKU Reference No.)'
    VAR_NAME   = 'ชื่อตัวเลือก'
    SELL_PRICE = 'ราคาขาย'
    QTY        = 'จำนวน'
    NET_SELL   = 'ราคาขายสุทธิ'
    COMMISSION = 'ค่าคอมมิชชั่น'
    TXN_FEE    = 'Transaction Fee'
    SVC_FEE    = 'ค่าบริการ'
    TOTAL      = 'จำนวนเงินทั้งหมด'
    RECIPIENT  = 'ชื่อผู้รับ'
    PHONE      = 'หมายเลขโทรศัพท์'
    ADDR       = 'ที่อยู่ในการจัดส่ง'
    PROVINCE   = 'จังหวัด'
    DISTRICT   = 'เขต/อำเภอ'
    ZIP        = 'รหัสไปรษณีย์'


def _s(val):
    """Cell -> trimmed str ('' for NaN/None)."""
    if val is None:
        return ''
    s = str(val).strip()
    return '' if s.lower() in ('nan', 'none') else s


def _num(val) -> Optional[float]:
    """Cell -> float, tolerant of commas/blank. '' -> None."""
    s = _s(val).replace(',', '')
    if s == '':
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _first_nonempty(rows, key):
    for r in rows:
        v = _s(r.get(key, ''))
        if v != '':
            return v
    return ''


def _iso_dt(s, fmt):
    """Normalize a date string to 'YYYY-MM-DD HH:MM' (sortable); raw on failure."""
    s = _s(s)
    if s == '':
        return None
    try:
        return datetime.strptime(s, fmt).strftime('%Y-%m-%d %H:%M')
    except ValueError:
        return s


def parse_shopee_orders(df):
    """Parse a Shopee order-export DataFrame (read with header=0, dtype=str).

    Groups the flat one-row-per-line sheet by order number. Order-level fields
    (status, dates, buyer, fees, total) repeat on every line, so they are taken
    from the first non-empty value; qty/price/subtotal are per line.
    Returns a list of order dicts (see module docstring).
    """
    required = {_SP.ORDER, _SP.ITEM_NAME, _SP.QTY}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Shopee order export missing columns: {sorted(missing)}")

    records = df.to_dict('records')

    # Preserve first-seen order; group lines under each order number.
    groups = OrderedDict()
    for r in records:
        osn = _s(r.get(_SP.ORDER, ''))
        if osn == '':
            continue
        groups.setdefault(osn, []).append(r)

    orders = []
    for osn, rows in groups.items():
        commission = _num(_first_nonempty(rows, _SP.COMMISSION)) or 0.0
        txn_fee    = _num(_first_nonempty(rows, _SP.TXN_FEE)) or 0.0
        svc_fee    = _num(_first_nonempty(rows, _SP.SVC_FEE)) or 0.0

        addr_parts = [_first_nonempty(rows, k) for k in
                      (_SP.ADDR, _SP.DISTRICT, _SP.PROVINCE, _SP.ZIP)]
        ship_address = ' '.join(p for p in addr_parts if p)

        items = []
        seen_keys = {}
        item_total = 0.0
        for r in rows:
            name = _s(r.get(_SP.ITEM_NAME, ''))
            var  = _s(r.get(_SP.VAR_NAME, ''))
            base_key = f"{name}|{var}"
            n = seen_keys.get(base_key, 0) + 1
            seen_keys[base_key] = n
            line_key = base_key if n == 1 else f"{base_key}#{n}"

            subtotal = _num(r.get(_SP.NET_SELL))
            if subtotal is not None:
                item_total += subtotal

            items.append({
                'line_key': line_key,
                'seller_sku': _s(r.get(_SP.SKU_REF, '')) or None,
                'variation_id': None,           # Shopee order export carries none
                'item_name': name,
                'variation_name': var or None,
                'qty': _num(r.get(_SP.QTY)) or 0.0,
                'unit_price': _num(r.get(_SP.SELL_PRICE)),
                'item_subtotal': subtotal,
            })

        orders.append({
            'platform': 'shopee',
            'order_sn': osn,
            'status': _first_nonempty(rows, _SP.STATUS) or None,
            'order_date': _first_nonempty(rows, _SP.ORDER_DATE) or None,
            'paid_date': _first_nonempty(rows, _SP.PAID_TIME) or None,
            'buyer_name': _first_nonempty(rows, _SP.RECIPIENT) or None,
            'buyer_phone': _first_nonempty(rows, _SP.PHONE) or None,
            'ship_address': ship_address or None,
            'item_total': round(item_total, 2),
            'marketplace_fee': round(commission + txn_fee + svc_fee, 2),
            'payout': _num(_first_nonempty(rows, _SP.TOTAL)),
            'currency': 'THB',
            'items': items,
        })

    return orders


# --- Lazada order-export column headers ---
class _LZ:
    ORDER       = 'orderNumber'
    ITEM_ID     = 'orderItemId'
    SELLER_SKU  = 'sellerSku'
    LAZADA_SKU  = 'lazadaSku'      # = platform_skus.variation_id
    ITEM_NAME   = 'itemName'
    VARIATION   = 'variation'
    UNIT_PRICE  = 'unitPrice'
    PAID_PRICE  = 'paidPrice'
    STATUS      = 'status'
    CREATE      = 'createTime'
    SHIP_NAME   = 'shippingName'
    CUST_NAME   = 'customerName'
    PHONE       = 'shippingPhone'
    CITY        = 'shippingCity'
    POSTCODE    = 'shippingPostCode'
    REGION      = 'shippingRegion'
    ADDR        = ('shippingAddress', 'shippingAddress2', 'shippingAddress3',
                   'shippingAddress4', 'shippingAddress5')


def parse_lazada_orders(df):
    """Parse a Lazada order-export DataFrame (read with header=0, dtype=str).

    Lazada exports ONE ROW PER UNIT (each `orderItemId` is one sellable unit),
    so a product line's quantity is the count of rows sharing the same product
    within an order. Lines are grouped by lazadaSku (= variation_id) when present,
    else sellerSku, else itemName|variation. paidPrice (all-in buyer price per
    unit) is summed into item_subtotal; unitPrice is the per-unit base.

    The order export carries NO marketplace commission/fee, so marketplace_fee
    and payout stay None — those come from the Lazada finance/statement report
    (a later phase). Returns a list of order dicts (see module docstring).
    """
    required = {_LZ.ORDER, _LZ.ITEM_NAME}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Lazada order export missing columns: {sorted(missing)}")

    records = df.to_dict('records')

    groups = OrderedDict()
    for r in records:
        osn = _s(r.get(_LZ.ORDER, ''))
        if osn == '':
            continue
        groups.setdefault(osn, []).append(r)

    orders = []
    for osn, rows in groups.items():
        # Aggregate units into product lines, preserving first-seen order.
        lines = OrderedDict()
        for r in rows:
            seller_sku = _s(r.get(_LZ.SELLER_SKU, ''))
            variation_id = _s(r.get(_LZ.LAZADA_SKU, ''))
            name = _s(r.get(_LZ.ITEM_NAME, ''))
            var = _s(r.get(_LZ.VARIATION, ''))
            key = variation_id or seller_sku or f"{name}|{var}"
            line = lines.get(key)
            if line is None:
                line = {
                    'line_key': key,
                    'seller_sku': seller_sku or None,
                    'variation_id': variation_id or None,
                    'item_name': name,
                    'variation_name': var or None,
                    'qty': 0.0,
                    'unit_price': _num(r.get(_LZ.UNIT_PRICE)),
                    'item_subtotal': 0.0,
                }
                lines[key] = line
            line['qty'] += 1.0
            paid = _num(r.get(_LZ.PAID_PRICE))
            if paid is None:
                paid = _num(r.get(_LZ.UNIT_PRICE)) or 0.0
            line['item_subtotal'] = round(line['item_subtotal'] + paid, 2)

        items = list(lines.values())
        item_total = round(sum(li['item_subtotal'] for li in items), 2)

        addr_parts = [_first_nonempty(rows, k) for k in
                      (_LZ.ADDR + (_LZ.CITY, _LZ.POSTCODE, _LZ.REGION))]
        ship_address = ' '.join(p for p in addr_parts if p)

        orders.append({
            'platform': 'lazada',
            'order_sn': osn,
            'status': _first_nonempty(rows, _LZ.STATUS) or None,
            'order_date': _iso_dt(_first_nonempty(rows, _LZ.CREATE), '%d %b %Y %H:%M'),
            'paid_date': None,                          # not in the order export
            'buyer_name': (_first_nonempty(rows, _LZ.SHIP_NAME)
                           or _first_nonempty(rows, _LZ.CUST_NAME) or None),
            'buyer_phone': _first_nonempty(rows, _LZ.PHONE) or None,
            'ship_address': ship_address or None,
            'item_total': item_total,
            'marketplace_fee': None,                    # not in order export (finance report)
            'payout': None,
            'currency': 'THB',
            'items': items,
        })

    return orders
