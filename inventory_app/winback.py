"""winback.py — one win-back computation, shared by the call card and the
customer page (#497).

Replaces call_card.py's own `_compute_winback`, which was name-keyed with no
evidence filter: a bill name shared by two customer codes merged both
companies' history, and freebie-only invoices / credit notes / not-a-sale
documents all counted as purchases.

Population = `price_lookup.purchase_population_filter`, restricted to whatever
scope the caller passes — the SAME population
`models.customers._customer_product_cards` counts as ครั้งที่ซื้อ, so a product's
win-back flag and its purchase count on the customer page can never disagree
about what counts as a real sale. Deliberately the PURCHASE population and not
`price_evidence_filter`: a bill that was written off is still a purchase this
shop made (Put, 2026-09-17, #554), and win-back asks when it last bought.
Keyed by (product_id, unit). Eligible at >=3 distinct INVOICES (doc_base —
the same ครั้งที่ซื้อ definition the product card counts,
`COUNT(DISTINCT s.doc_base)`); two invoices dated the SAME day are still two
separate ครั้ง. Gaps, however, are measured between distinct purchase DATES
(a product bought twice on one day has nothing to compute a within-day gap
from), so a product needs >=2 distinct dates before it can be flagged at
all — 3 invoices on one date is eligible but has no gap. Flagged when days
since the last date exceeds the median inter-purchase gap.

`where` / `params`: a scope built the way
`models.customers._customer_sales_scope` builds one. The CALLER decides which
customer key to scope by — customer_code (preferred; never a bill name that
could be shared by >1 company) or, for a true orphan with no code anywhere,
the bill name itself (see call_card.get_card). Win-back must always be
computed over the customer's FULL history: pass a scope built with
date_from=date_to=None. This module never applies a date bound of its own —
that independence is the caller's responsibility (there is nothing here to
get wrong either way, since the scope is opaque to this function; the
customer page's route is where that discipline actually lives).
"""
import datetime as dt
import statistics
from collections import defaultdict

import price_lookup


def compute_winback(conn, where, params, today=None):
    """Return win-back rows for one customer scope, longest-lapse first.

    Each row: {product_id, product_name, unit, last_buy, median_gap_days,
    days_since}.

    `today` (ISO-comparable date, default: real wall-clock date) — injectable
    so callers/tests can pin "now" without waiting on the calendar.
    """
    today = today or dt.date.today()
    rows = conn.execute(f"""
        SELECT
            s.product_id,
            COALESCE(p.product_name, s.product_name_raw) AS product_name,
            s.unit,
            s.date_iso,
            s.doc_base
        FROM sales_transactions s
        LEFT JOIN products p ON p.id = s.product_id
        WHERE {where}
          AND {price_lookup.purchase_population_filter('s')}
          AND s.product_id IS NOT NULL
        ORDER BY s.product_id, s.unit, s.date_iso
    """, params).fetchall()

    groups = defaultdict(lambda: {'product_name': None, 'dates': [], 'doc_bases': set()})
    for row in rows:
        key = (row['product_id'], row['unit'])
        groups[key]['product_name'] = row['product_name']
        groups[key]['dates'].append(row['date_iso'])
        groups[key]['doc_bases'].add(row['doc_base'])

    out = []
    for (pid, unit), info in groups.items():
        # Eligibility = ครั้งที่ซื้อ >= 3 (distinct INVOICES), same definition
        # the product card counts — NOT distinct dates. Two invoices on one
        # day are two ครั้ง.
        if len(info['doc_bases']) < 3:
            continue

        dates = sorted(set(info['dates']))
        # A gap needs two ENDPOINTS. 3 invoices dated the same single day are
        # eligible (>=3 ครั้ง) but there is no gap to measure yet — never flag.
        if len(dates) < 2:
            continue

        date_objs = [dt.date.fromisoformat(d) for d in dates]
        gaps = [(date_objs[i + 1] - date_objs[i]).days for i in range(len(date_objs) - 1)]
        med_gap = statistics.median(gaps)

        last_date = date_objs[-1]
        days_since = (today - last_date).days

        if days_since > med_gap:
            out.append({
                'product_id':      pid,
                'product_name':    info['product_name'],
                'unit':            unit,
                'last_buy':        dates[-1],
                'median_gap_days': int(med_gap),
                'days_since':      days_since,
            })

    out.sort(key=lambda r: -r['days_since'])
    return out
