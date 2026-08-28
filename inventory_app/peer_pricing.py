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
from typing import List, Dict, Any, Optional, Tuple

import price_lookup as pl


def product_peer_prices(
    conn, customer_code: str, *,
    window_from_by_pair: Optional[Dict[Tuple[int, str], Optional[str]]] = None,
) -> List[Dict[str, Any]]:
    """Return peer-price comparison for every (product_id, unit) the customer bought.

    Args:
        conn: sqlite3 connection with sales_transactions table.
              Row factory can be sqlite3.Row or None.
        customer_code: the identity key to compare (must match sales_transactions.customer_code).
        window_from_by_pair: task-2-brief.md PR C / 2e, C3/C4. {(product_id, unit):
              'YYYY-MM-DD' | None}, or the default `None`.
              `None` (the default) → legacy behaviour: the population is
              ALL rows (marketplace/write-offs/dummy invoices included, no
              date floor) — UNCHANGED from before this parameter existed,
              so tests/test_peer_pricing.py's minimal fixture (which lacks
              doc_base/customer/ar_writeoffs — see its own tests) keeps
              passing untouched.
              Given a dict → the population is restricted to
              price_lookup.evidence_filter('st') (excludes SR/HS returns,
              write-offs, รายการหน้าร้าน marketplace rows, and the
              cost-basis dummy invoices — the SAME predicate the resolver's
              own evidence lookups use, so peers and resolve_price can never
              silently disagree about which rows count), AND, for each
              (product_id, unit) pair PRESENT in the map with a non-None
              value, further restricted to date_iso >= that pair's date.
              A pair absent from the map, or mapped to None, still gets
              evidence_filter but is NOT date-restricted (there is no
              price-regime change to anchor it to) — it is never silently
              dropped and never treated as "today". Call-card callers pass
              this keyed on the (product_id, unit) PAIR (never just
              product_id — see price_lookup.epochs_for_pairs, C2) so one
              product bought at two units gets two independent epochs.

    Returns:
        List of dicts, one per (product_id, unit) pair the customer bought, each with:
          product_id     - int
          unit           - str
          customer_median - float (rounded int) — median cash across all the customer's buys
          customer_latest - float (rounded int) — cash from the customer's most-recent buy
          customer_latest_list - float or None — gross unit_price of that most-recent line
          customer_latest_disc - str or None  — raw discount text of that line ('20%','15+5%','28.00','')
          peer_median    - float or None
          peer_repr_list - float or None — gross unit_price of the representative median peer's line
          peer_repr_disc - str or None  — raw discount text of that representative peer line
          peer_n         - int (distinct peer customers)
          diff           - float or None  (customer_median - peer_median)
          flag           - 'cheaper' | 'same' | 'higher'
    """
    if window_from_by_pair is None:
        rows = conn.execute(
            "SELECT product_id, unit, customer_code, qty, net, vat_type, date_iso, "
            "unit_price, discount "
            "FROM sales_transactions "
            "WHERE product_id IS NOT NULL AND qty > 0 AND net > 0",
        ).fetchall()
    else:
        all_rows = conn.execute(
            "SELECT product_id, unit, customer_code, qty, net, vat_type, date_iso, "
            "unit_price, discount "
            "FROM sales_transactions st "
            f"WHERE {pl.evidence_filter('st')}",
        ).fetchall()
        rows = []
        for r in all_rows:
            pair = (r[0], r[1] or '')
            floor = window_from_by_pair.get(pair)
            if floor is not None and r[6] < floor:  # r[6] = date_iso
                continue
            rows.append(r)

    # Build per-(product_id, unit) per-customer line lists.
    # Structure: data[(pid, unit)][cust_code] = [(cash, unit_price, discount), ...]
    # unit_price = gross list price for that line's unit; discount = raw text
    # ('20%' / '15+5%' / '28.00' / '') so the call-card can show "ราคาตั้ง + ส่วนลด".
    data = defaultdict(lambda: defaultdict(list))
    # For the TARGET customer only: (date_iso, cash, unit_price, discount) per (pid, unit),
    # used to derive the most-recent price (customer_latest) + that line's list+discount.
    target_dated = defaultdict(list)

    for row in rows:
        pid = row[0]
        unit = row[1] or ''
        cust = row[2]
        if not cust:
            continue
        qty = float(row[3])
        net = float(row[4])
        vat_type = int(row[5]) if row[5] is not None else 0
        date_iso = row[6]
        unit_price = row[7]
        discount = row[8]

        cash = net / qty
        if vat_type == 2:
            cash = cash * 1.07
        cash = round(cash)  # round to int before counting (quoting rule)
        data[(pid, unit)][cust].append((cash, unit_price, discount))
        if cust == customer_code:
            target_dated[(pid, unit)].append((date_iso, cash, unit_price, discount))

    result = []
    for (pid, unit), cust_map in data.items():
        if customer_code not in cust_map:
            continue

        # Customer's own cash list -> median
        cust_entries = cust_map[customer_code]
        cust_med = statistics.median([e[0] for e in cust_entries])

        # Most-recent price the customer paid for this (product, unit). If several
        # rows share the latest date, take their median cash. Falls back to the overall
        # median when no usable date is present. The gross list price + discount come
        # from the (latest-date) line whose cash is closest to that median.
        dated = target_dated.get((pid, unit), [])
        dates = [d for d, c, up, disc in dated if d]
        if dates:
            latest_date = max(dates)
            latest_entries = [(c, up, disc) for d, c, up, disc in dated if d == latest_date]
            cust_latest = statistics.median([c for c, up, disc in latest_entries])
            rep = min(latest_entries, key=lambda e: abs(e[0] - cust_latest))
        else:
            cust_latest = cust_med
            rep = min(cust_entries, key=lambda e: abs(e[0] - cust_med))
        customer_latest_list = rep[1]
        customer_latest_disc = rep[2]

        # Peer set: all OTHER customer_codes -> (cust_code, their median cash)
        peer_meds = []
        for c, entries in cust_map.items():
            if c == customer_code:
                continue
            peer_meds.append((c, statistics.median([e[0] for e in entries])))

        peer_n = len(peer_meds)

        if peer_n == 0:
            peer_med = None
            diff = None
            flag = 'same'
            peer_repr_list = None
            peer_repr_disc = None
            peer_min = None
            peer_max = None
            peer_cheaper_pct = None
            peers = []
        else:
            peer_med = statistics.median([pm for _, pm in peer_meds])
            diff = cust_med - peer_med
            if diff < 0:
                flag = 'cheaper'
            elif diff > 0:
                flag = 'higher'
            else:
                flag = 'same'

            # Representative median peer: the peer whose median cash is closest to
            # peer_med (ties -> the lower-median peer). From that peer's lines, take
            # the one whose cash is closest to peer_med for the displayed list+discount.
            rep_cust = min(peer_meds, key=lambda t: (abs(t[1] - peer_med), t[1]))[0]
            rep_peer_line = min(cust_map[rep_cust], key=lambda e: abs(e[0] - peer_med))
            peer_repr_list = rep_peer_line[1]
            peer_repr_disc = rep_peer_line[2]

            # Position in the peer group: range + the % of peers the customer is
            # cheaper than (their median > the customer's latest cash), plus the
            # per-peer breakdown (sorted by price, representative flagged) for the
            # "where does this price come from" modal.
            peer_vals = sorted(pm for _, pm in peer_meds)
            peer_min = peer_vals[0]
            peer_max = peer_vals[-1]
            # Midpoint percentile rank (tie-aware): peers strictly more expensive
            # count full, equal-priced peers count half. This keeps a customer
            # priced EQUAL to the group at ~50 (mid) instead of mislabelling them
            # as cheapest/most-expensive (ties are common with standardized pricing).
            n_more = sum(1 for pm in peer_vals if pm > cust_latest)
            n_equal = sum(1 for pm in peer_vals if pm == cust_latest)
            peer_cheaper_pct = round(100 * (n_more + 0.5 * n_equal) / peer_n)
            peers = sorted(
                ({'code': c, 'price': pm, 'is_repr': c == rep_cust} for c, pm in peer_meds),
                key=lambda d: d['price'],
            )

        result.append({
            'product_id': pid,
            'unit': unit,
            'customer_median': cust_med,
            'customer_latest': cust_latest,
            'customer_latest_list': customer_latest_list,
            'customer_latest_disc': customer_latest_disc,
            'peer_median': peer_med,
            'peer_repr_list': peer_repr_list,
            'peer_repr_disc': peer_repr_disc,
            'peer_min': peer_min,
            'peer_max': peer_max,
            'peer_cheaper_pct': peer_cheaper_pct,
            'peers': peers,
            'peer_n': peer_n,
            'diff': diff,
            'flag': flag,
        })

    return result
