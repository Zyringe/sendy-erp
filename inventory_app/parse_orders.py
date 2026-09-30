"""Marketplace ORDER-export parsers (Shopee, Lazada, TikTok).

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

import re
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


# --- TikTok Shop order-export column headers (CSV, one row per SKU line) ---
class _TT:
    ORDER         = 'Order ID'
    STATUS        = 'Order Status'
    RETURN_TYPE   = 'Cancelation/Return Type'
    SKU_ID        = 'SKU ID'                       # = platform_skus.variation_id
    SELLER_SKU    = 'Seller SKU'
    ITEM_NAME     = 'Product Name'
    VARIATION     = 'Variation'
    QTY           = 'Quantity'
    QTY_RETURNED  = 'Sku Quantity of return'
    UNIT_PRICE    = 'SKU Unit Original Price'
    PLATFORM_DISC = 'SKU Platform Discount'
    SELLER_DISC   = 'SKU Seller Discount'
    SUBTOTAL      = 'SKU Subtotal After Discount'  # what the buyer paid for the line
    SHIPPING      = 'Shipping Fee After Discount'
    ORDER_AMOUNT  = 'Order Amount'
    REFUND        = 'Order Refund Amount'
    CREATED       = 'Created Time'
    PAID          = 'Paid Time'
    CANCELLED     = 'Cancelled Time'
    RECIPIENT     = 'Recipient'
    PHONE         = 'Phone #'
    ADDR          = ('Detail Address', 'Districts', 'District', 'Province', 'Zipcode')
    PAYMENT       = 'Payment Method'
    CHANNEL       = 'Order Channel'
    CREATOR       = 'Creator Handle'


_TT_REQUIRED = {_TT.ORDER, _TT.STATUS, _TT.SKU_ID, _TT.ITEM_NAME, _TT.QTY,
                _TT.UNIT_PRICE, _TT.SUBTOTAL, _TT.CREATED, _TT.PAID}


def _tt_id(val, col):
    """Order ID / SKU ID: ASCII digits only once the trailing TAB is stripped
    (str.isdigit() also accepts Thai and superscript digits)."""
    s = _s(val)
    if not re.fullmatch(r'[0-9]+', s):
        raise ValueError(f"TikTok order export: {col} is not numeric: {s!r}")
    return s


def _tt_dt(val, col):
    """'DD/MM/YYYY HH:MM:SS' -> 'YYYY-MM-DD HH:MM:SS' (seconds kept). Blank -> None;
    anything else that won't parse raises (never returned raw)."""
    s = _s(val)
    if s == '':
        return None
    try:
        return datetime.strptime(s, '%d/%m/%Y %H:%M:%S').strftime('%Y-%m-%d %H:%M:%S')
    except ValueError:
        raise ValueError(f"TikTok order export: {col} is not DD/MM/YYYY HH:MM:SS: {s!r}")


def _unmasked(val):
    """TikTok masks buyer data with '*'; a masked value carries nothing -> None."""
    s = _s(val)
    return None if s == '' or '*' in s else s


def parse_tiktok_orders(df):
    """Parse a TikTok Shop order-export DataFrame (CSV read with dtype=str,
    keep_default_na=False, encoding='utf-8-sig').

    One row per SKU line. Lines are grouped by Order ID and aggregated by SKU ID
    (line_key = variation_id = SKU ID), so a SKU repeated inside one order stays
    one line under UNIQUE(platform, order_sn, line_key). item_subtotal is
    `SKU Subtotal After Discount` = what the buyer paid, after TikTok's and the
    seller's discounts; item_total is its sum. Fees and payout come from the
    income file (a later phase), so marketplace_fee and payout stay None.

    Discounts, returns and order-level amounts are carried on the dict (and so
    stored in raw_json) for the settlement/IV phases; nothing acts on them here.
    Returns a list of order dicts (see module docstring).
    """
    missing = _TT_REQUIRED - set(df.columns)
    if missing:
        raise ValueError(f"TikTok order export missing columns: {sorted(missing)}; "
                         f"columns seen: {list(df.columns)}")

    groups = OrderedDict()
    for r in df.to_dict('records'):
        osn = _tt_id(r.get(_TT.ORDER), _TT.ORDER)
        groups.setdefault(osn, []).append(r)

    orders = []
    for osn, rows in groups.items():
        lines = OrderedDict()
        for r in rows:
            sku = _tt_id(r.get(_TT.SKU_ID), _TT.SKU_ID)
            line = lines.get(sku)
            if line is None:
                line = lines[sku] = {
                    'line_key': sku,
                    'seller_sku': _s(r.get(_TT.SELLER_SKU)) or None,
                    'variation_id': sku,
                    'item_name': _s(r.get(_TT.ITEM_NAME)),
                    'variation_name': _s(r.get(_TT.VARIATION)) or None,
                    'qty': 0.0,
                    'unit_price': _num(r.get(_TT.UNIT_PRICE)),
                    'item_subtotal': 0.0,
                    'platform_discount': 0.0,
                    'seller_discount': 0.0,
                    'qty_returned': 0.0,
                    'return_type': _s(r.get(_TT.RETURN_TYPE)) or None,
                    'refund_amount': None,
                    'creator_handle': _s(r.get(_TT.CREATOR)) or None,
                }
            line['qty'] += _num(r.get(_TT.QTY)) or 0.0
            line['item_subtotal'] = round(line['item_subtotal'] + (_num(r.get(_TT.SUBTOTAL)) or 0.0), 2)
            line['platform_discount'] = round(
                line['platform_discount'] + (_num(r.get(_TT.PLATFORM_DISC)) or 0.0), 2)
            line['seller_discount'] = round(
                line['seller_discount'] + (_num(r.get(_TT.SELLER_DISC)) or 0.0), 2)
            line['qty_returned'] += _num(r.get(_TT.QTY_RETURNED)) or 0.0
            refund = _num(r.get(_TT.REFUND))            # per SKU line, blank = none
            if refund is not None:
                line['refund_amount'] = round((line['refund_amount'] or 0.0) + refund, 2)

        items = list(lines.values())
        # Every date cell is checked, not only the first non-empty one.
        created = [_tt_dt(r.get(_TT.CREATED), _TT.CREATED) for r in rows]
        paid = [_tt_dt(r.get(_TT.PAID), _TT.PAID) for r in rows]
        cancelled = [_tt_dt(r.get(_TT.CANCELLED), _TT.CANCELLED) for r in rows]
        addr_parts = [_unmasked(_first_nonempty(rows, k)) for k in _TT.ADDR]
        refunds = [li['refund_amount'] for li in items if li['refund_amount'] is not None]

        orders.append({
            'platform': 'tiktok',
            'order_sn': osn,
            'status': _first_nonempty(rows, _TT.STATUS) or None,
            'order_date': next((d for d in created if d), None),
            'paid_date': next((d for d in paid if d), None),
            'buyer_name': _unmasked(_first_nonempty(rows, _TT.RECIPIENT)),
            'buyer_phone': _unmasked(_first_nonempty(rows, _TT.PHONE)),
            'ship_address': ' '.join(p for p in addr_parts if p) or None,
            'item_total': round(sum(li['item_subtotal'] for li in items), 2),
            'marketplace_fee': None,                    # income file (PR-2)
            'payout': None,
            'currency': 'THB',
            'items': items,
            'order_amount': _num(_first_nonempty(rows, _TT.ORDER_AMOUNT)),
            'refund_amount': round(sum(refunds), 2) if refunds else None,
            'shipping_fee_after_discount': _num(_first_nonempty(rows, _TT.SHIPPING)),
            'payment_method': _first_nonempty(rows, _TT.PAYMENT) or None,
            'order_channel': _first_nonempty(rows, _TT.CHANNEL) or None,
            'creator_handle': _first_nonempty(rows, _TT.CREATOR) or None,
            'cancelled_time': next((d for d in cancelled if d), None),
        })

    return orders
