"""peer_pricing.py — Peer-median / special-price flag helper.

Ported from sendy_erp/data/exports/_gen_sales_playbook.py (the 'special price' flag logic).

The flag compares a customer's median unit-cash against the median-of-medians across
all OTHER customers who bought the SAME (product_id, unit). This is intentionally
independent of the catalog price — the catalog has known unit-coherence issues and
should only be shown as reference, not used to flag pricing anomalies.

Cash calculation:
  cash = net / qty
  vat_type=2 rows get cash = (net * 1.07) / qty  (apples-to-apples with non-VAT rows)
  Round cash to int before grouping/counting (per quoting rule: avoids float-dup miscounts
  where 90.00 vat_type=1 and 90.01 vat_type=2 would be counted as distinct tiers).

Identity column: customer_code (same as _gen_sales_playbook.py grouping key).
Peers = all rows where customer_code != target (excludes target from peer set).
peer_n = number of distinct peer customer_codes.
If peer_n == 0: peer_median = None, flag = 'same' (no comparison possible).
"""
import sqlite3
import statistics
from collections import defaultdict
from typing import List, Dict, Any


def product_peer_prices(conn, customer_code: str) -> List[Dict[str, Any]]:
    """Return peer-price comparison for every (product_id, unit) the customer bought.

    Args:
        conn: sqlite3 connection with sales_transactions table.
              Row factory can be sqlite3.Row or None.
        customer_code: the identity key to compare (must match sales_transactions.customer_code).

    Returns:
        List of dicts, one per (product_id, unit) pair the customer bought, each with:
          product_id     - int
          unit           - str
          customer_median - float (rounded int)
          peer_median    - float or None
          peer_n         - int (distinct peer customers)
          diff           - float or None  (customer_median - peer_median)
          flag           - 'cheaper' | 'same' | 'higher'
    """
    rows = conn.execute(
        "SELECT product_id, unit, customer_code, qty, net, vat_type "
        "FROM sales_transactions "
        "WHERE product_id IS NOT NULL AND qty > 0 AND net > 0",
    ).fetchall()

    # Build per-(product_id, unit) per-customer cash lists
    # Structure: data[(pid, unit)][cust_code] = [cash, ...]
    data = defaultdict(lambda: defaultdict(list))

    for row in rows:
        pid = row[0]
        unit = row[1] or ''
        cust = row[2]
        if not cust:
            continue
        qty = float(row[3])
        net = float(row[4])
        vat_type = int(row[5]) if row[5] is not None else 0

        cash = net / qty
        if vat_type == 2:
            cash = cash * 1.07
        cash = round(cash)  # round to int before counting (quoting rule)
        data[(pid, unit)][cust].append(cash)

    result = []
    for (pid, unit), cust_map in data.items():
        if customer_code not in cust_map:
            continue

        # Customer's own cash list -> median
        cust_vals = cust_map[customer_code]
        cust_med = statistics.median(cust_vals)

        # Peer set: all OTHER customer_codes
        peer_meds = []
        for c, vals in cust_map.items():
            if c == customer_code:
                continue
            peer_meds.append(statistics.median(vals))

        peer_n = len(peer_meds)

        if peer_n == 0:
            peer_med = None
            diff = None
            flag = 'same'
        else:
            peer_med = statistics.median(peer_meds)
            diff = cust_med - peer_med
            if diff < 0:
                flag = 'cheaper'
            elif diff > 0:
                flag = 'higher'
            else:
                flag = 'same'

        result.append({
            'product_id': pid,
            'unit': unit,
            'customer_median': cust_med,
            'peer_median': peer_med,
            'peer_n': peer_n,
            'diff': diff,
            'flag': flag,
        })

    return result
