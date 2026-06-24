"""Shared, dependency-free marketplace fee-bucket vocabulary.

Kept out of the parser modules (which import pandas) so models.py can import it
cheaply — the fee-breakdown display needs the raw-line → bucket map to decide,
per order, whether a bucket came from a single fee type (show its real name) or
several (show the generic category).

- LAZADA_BUCKET: Lazada raw statement label → bucket column. SOURCE OF TRUTH,
  imported by parse_lazada_statement (which owns the SUMMING into buckets).
- GRANULAR_LABEL: raw label → clean Thai display name, used only when a bucket is
  single-source (the "smart label", e.g. a LazCoins-only ค่าโฆษณา/โปรโมชั่น bucket
  reads "ส่วนลด LazCoins"). Labels absent here fall back to the generic bucket name.
"""

# Lazada ชื่อรายการธุรกรรม → bucket column. Unmapped names fall to fee_platform
# (the catch-all) at parse time.
LAZADA_BUCKET = {
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
    # --- Thai-language export: same transactions, Thai ชื่อรายการธุรกรรม → same
    # buckets as the English names above. ('Premium Package' stays English even in
    # the Thai file, so it is already covered.) ---
    'ยอดรวมค่าสินค้า': 'item_value',                       # = Item Price Credit (gross)
    'หักค่าธรรมเนียมการขายสินค้า': 'fee_commission',        # = Commission
    'ค่าธรรมเนียมการชำระเงิน': 'fee_transaction',           # = Payment Fee
    'ค่าธรรมเนียมโปรแกรมส่วนลด LazCoins': 'fee_ads_escrow',  # = LazCoins Discount Promotion Fee
    'ส่วนลด LazCoins': 'fee_ads_escrow',                    # = LazCoins Discount
    'รางวัลรีวิวสำหรับผู้ซื้อ': 'fee_ads_escrow',           # = Buyer Review Incentive
}

# Raw label → clean Thai name for the single-source "smart label". Only labels that
# benefit from a specific name need an entry; anything else falls back to the bucket's
# generic label (which is already its real meaning, e.g. Commission → ค่าคอมมิชชั่น).
GRANULAR_LABEL = {
    'LazCoins Discount': 'ส่วนลด LazCoins',
    'LazCoins Discount Promotion Fee': 'ส่วนลด LazCoins',
    'ค่าธรรมเนียมโปรแกรมส่วนลด LazCoins': 'ส่วนลด LazCoins',
    'ส่วนลด LazCoins': 'ส่วนลด LazCoins',
    'Reversal of LazCoins Discount': 'คืนส่วนลด LazCoins',
    'Reversal of LazCoins Discount Promotion Fee': 'คืนส่วนลด LazCoins',
    'Campaign Fee': 'ค่าแคมเปญ',
    'Promotional Charges Vouchers': 'ค่าโค้ดส่วนลด',
    'Buyer Review Incentive': 'รางวัลรีวิวผู้ซื้อ',
    'รางวัลรีวิวสำหรับผู้ซื้อ': 'รางวัลรีวิวผู้ซื้อ',
    'Lost Claim': 'ค่าชดเชยพัสดุหาย',
}
