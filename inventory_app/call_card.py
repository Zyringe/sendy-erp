"""call_card.py — Call-log + CRM helpers + data assemblers for the Sendy call-card feature.

Dual-key design
---------------
Sales data (sales_transactions) is keyed on the customer NAME column
(`sales_transactions.customer`).  The call_log and crm tables are keyed on the
CANONICAL customer key — `customers.code` when a master row exists, else the
customer name for orphans.  This matches the ar_followup resolver so the AR and
call-card agree on who is who.

`get_card` reconciles the two keys:
  canonical_key → arf_mod._resolve_target → names_list → query sales by name.

Public surface
--------------
Pure helpers:
  call_status(last_called_iso, target_days, today=None)  → (status, days|None)
  elapsed_th(days)                                       → Thai elapsed string
  STATUS_LABEL                                           → dict
  DEFAULT_CALL_TARGET_DAYS                               → int

Log/CRM helpers (all accept a live sqlite3 connection):
  add_log(conn, customer_code, kind, body, user)
  mark_called(conn, customer_code, user, body=None)
  get_log(conn, customer_code)                → list[Row]
  last_called_at(conn, customer_code)         → ISO str | None
  soft_delete_log(conn, log_id, user)
  get_crm(conn, customer_code)                → Row | None
  upsert_crm(conn, customer_code, user, **fields)
  target_days_for(crm_row)                    → int

Assemblers:
  get_call_list(conn, *, q, region, call, spend_window, sort, sp) → list[dict]
  get_card(conn, customer_code)               → dict | None
"""
import datetime as dt
import statistics
from typing import Optional

import customer_geo as geo

# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_CALL_TARGET_DAYS = 365   # Put's global default (1 ปี); override per customer via CRM

STATUS_LABEL = {
    'recent': 'ยังไม่ถึงกำหนด',
    'due':    'ถึงกำหนดโทร',
    'never':  'ยังไม่เคยโทร',
}

# ── Pure helpers ──────────────────────────────────────────────────────────────

def _today():
    return dt.date.today()


def call_status(last_called_iso, target_days, today=None):
    """Return (status, days_since) where status ∈ {'never','recent','due'}.

    - 'never'  → last_called_iso is None/empty (days = None)
    - 'recent' → days since last call < target_days
    - 'due'    → days since last call >= target_days
    """
    today = today or _today()
    if not last_called_iso:
        return ('never', None)
    last = dt.date.fromisoformat(last_called_iso[:10])
    days = (today - last).days
    return (('due' if days >= target_days else 'recent'), days)


def elapsed_th(days):
    """Human-readable Thai elapsed string for a number of days."""
    if days < 30:
        return f"{days} วันก่อน"
    if days < 365:
        return f"{days // 30} เดือนก่อน"
    y, rem = divmod(days, 365)
    m = rem // 30
    return f"{y} ปี {m} เดือนก่อน" if m else f"{y} ปีก่อน"


# ── Log helpers ───────────────────────────────────────────────────────────────

def add_log(conn, customer_code, kind, body, user):
    """Append one entry to customer_call_log. kind ∈ {'note','call','data_flag'}."""
    conn.execute(
        "INSERT INTO customer_call_log(customer_code, kind, body, created_by) "
        "VALUES (?,?,?,?)",
        (customer_code, kind, body, user),
    )
    conn.commit()


def mark_called(conn, customer_code, user, body=None):
    """Record that the customer was called today (appends kind='call' row)."""
    add_log(conn, customer_code, 'call', body, user)


def get_log(conn, customer_code):
    """Return non-deleted log entries for this customer, newest first."""
    return conn.execute(
        "SELECT * FROM customer_call_log "
        "WHERE customer_code=? AND deleted_at IS NULL "
        "ORDER BY created_at DESC",
        (customer_code,),
    ).fetchall()


def last_called_at(conn, customer_code):
    """Return the ISO datetime of the most recent 'call' log entry, or None."""
    row = conn.execute(
        "SELECT MAX(created_at) AS m FROM customer_call_log "
        "WHERE customer_code=? AND kind='call' AND deleted_at IS NULL",
        (customer_code,),
    ).fetchone()
    return row['m'] if row and row['m'] else None


def soft_delete_log(conn, log_id, user):
    """Soft-delete a log entry.  Only the original author can delete their own row."""
    conn.execute(
        "UPDATE customer_call_log "
        "SET deleted_at=datetime('now','localtime'), deleted_by=? "
        "WHERE id=? AND created_by=? AND deleted_at IS NULL",
        (user, log_id, user),
    )
    conn.commit()


# ── CRM helpers ───────────────────────────────────────────────────────────────

def get_crm(conn, customer_code):
    """Return the customer_crm row (sqlite3.Row) or None if absent."""
    return conn.execute(
        "SELECT * FROM customer_crm WHERE customer_code=?",
        (customer_code,),
    ).fetchone()


def upsert_crm(conn, customer_code, user, **fields):
    """Insert or update a customer_crm row.

    fields ⊆ {tags, next_call_date, call_target_days}
    On conflict the named fields + updated_by/updated_at are overwritten.
    """
    if not fields:
        return
    cols = ",".join(fields)
    ph = ",".join("?" * len(fields))
    sets = ",".join(f"{k}=excluded.{k}" for k in fields)
    conn.execute(
        f"INSERT INTO customer_crm(customer_code, updated_by, {cols}) "
        f"VALUES (?,?,{ph}) "
        f"ON CONFLICT(customer_code) DO UPDATE SET {sets}, "
        f"updated_by=excluded.updated_by, "
        f"updated_at=datetime('now','localtime')",
        (customer_code, user, *fields.values()),
    )
    conn.commit()


def target_days_for(crm_row):
    """Return per-customer call-target days, falling back to DEFAULT_CALL_TARGET_DAYS."""
    if crm_row and crm_row['call_target_days']:
        return crm_row['call_target_days']
    return DEFAULT_CALL_TARGET_DAYS


# ── Spend-window helper ───────────────────────────────────────────────────────

def _spend_cutoff(window):
    """Return ISO date string cutoff for the spend window, or None for 'all'."""
    today = _today()
    if window == '6m':
        # approximately 6 months back
        cutoff = today - dt.timedelta(days=183)
    elif window == '1y':
        cutoff = today - dt.timedelta(days=365)
    elif window == '2y':
        cutoff = today - dt.timedelta(days=730)
    else:  # 'all'
        return None
    return cutoff.isoformat()


# ── get_call_list ─────────────────────────────────────────────────────────────

def get_call_list(conn, *, q=None, region=None, call=None,
                  spend_window='1y', sort='spend', sp=None):
    """Return the call worklist — one dict per active customer.

    Parameters
    ----------
    conn         : sqlite3 connection (row_factory=sqlite3.Row)
    q            : search string matched against customer name/code (case-insensitive)
    region       : ภาค filter (matches customer_geo.region_of(address))
    call         : 'never'|'recent'|'due' — filter by call_status
    spend_window : '6m'|'1y'|'2y'|'all' — window for the displayed spend value ONLY.
                   Does NOT affect which customers appear in the list.
    sort         : 'spend'|'last_buy'|'name'|'call'
    sp           : salesperson code filter

    Returns
    -------
    list of dicts, each with keys:
      customer_code, name, province, region,
      last_buy, spend, call_status, call_days,
      last_called, badges {ar, quiet, special}

    Design: NO N+1 queries.
      - universe: ALL customers with >=1 sales row (excl. หน้าร้าน marketplace accounts)
      - spend: one GROUP BY over spend_window (0 for customers outside window)
      - last_buy: separate all-time MAX query (independent of spend_window)
      - last_called: one GROUP BY over customer_call_log
      - customers master join for name/address/salesperson
      - all assembled in Python
    """
    cutoff = _spend_cutoff(spend_window)

    # ── 1. Customer universe: ALL customers with any sales row ────────────────
    # Marketplace pseudo-customers (หน้าร้านS/B/L) are NOT call targets — exclude them.
    # Pull canonical key: customers.code when available, else name for orphans.
    customer_rows = conn.execute("""
        SELECT
            COALESCE(NULLIF(TRIM(st.customer_code),''), st.customer) AS canonical_code,
            COALESCE(c.name, st.customer)                            AS name,
            COALESCE(c.address, '')                                   AS address,
            st.customer_code                                          AS raw_code,
            c.salesperson                                             AS salesperson_code
        FROM (
            SELECT DISTINCT
                customer,
                customer_code
            FROM sales_transactions
            WHERE customer IS NOT NULL AND customer != ''
              AND customer NOT LIKE 'หน้าร้าน%'
        ) st
        LEFT JOIN customers c ON c.code = TRIM(st.customer_code)
    """).fetchall()

    # Deduplicate by canonical_code (multiple name variants can map to same code)
    seen_codes = {}
    for row in customer_rows:
        code = row['canonical_code']
        if not code:
            continue
        if code not in seen_codes:
            seen_codes[code] = {
                'canonical_code': code,
                'name': row['name'],
                'address': row['address'],
                'salesperson_code': row['salesperson_code'],
            }

    # ── 2. Spend aggregate — window-filtered (shows ฿0 for quiet customers) ──
    spend_params = []
    spend_where = "WHERE customer NOT LIKE 'หน้าร้าน%'"
    if cutoff:
        spend_where += " AND date_iso >= ?"
        spend_params.append(cutoff)

    spend_rows = conn.execute(f"""
        SELECT
            COALESCE(NULLIF(TRIM(customer_code),''), customer) AS canonical_code,
            SUM(CASE WHEN vat_type=2 THEN net*1.07 ELSE net END) AS spend
        FROM sales_transactions
        {spend_where}
        GROUP BY canonical_code
    """, spend_params).fetchall()

    spend_map = {row['canonical_code']: row['spend'] or 0.0
                 for row in spend_rows if row['canonical_code']}

    # ── 3. last_buy — all-time MAX (independent of spend_window) ─────────────
    last_buy_rows = conn.execute("""
        SELECT
            COALESCE(NULLIF(TRIM(customer_code),''), customer) AS canonical_code,
            MAX(date_iso) AS last_buy
        FROM sales_transactions
        WHERE customer NOT LIKE 'หน้าร้าน%'
        GROUP BY canonical_code
    """).fetchall()

    last_buy_map = {row['canonical_code']: row['last_buy']
                    for row in last_buy_rows if row['canonical_code']}

    # ── 4. last_called aggregate (one query) ──────────────────────────────────
    last_called_rows = conn.execute("""
        SELECT customer_code, MAX(created_at) AS last_called
        FROM customer_call_log
        WHERE kind='call' AND deleted_at IS NULL
        GROUP BY customer_code
    """).fetchall()

    last_called_map = {}
    for row in last_called_rows:
        last_called_map[row['customer_code']] = row['last_called']

    # ── 5. CRM target_days (one query) ───────────────────────────────────────
    crm_rows = conn.execute(
        "SELECT customer_code, call_target_days FROM customer_crm"
    ).fetchall()
    crm_target_map = {r['customer_code']: r['call_target_days'] for r in crm_rows}

    # ── 6. Assemble + filter ──────────────────────────────────────────────────
    result = []
    for code, info in seen_codes.items():
        # Apply salesperson filter
        if sp and info['salesperson_code'] != sp:
            continue

        address = info['address']
        province = geo.province_of(address)
        row_region = geo.region_of(address)

        # Apply region filter
        if region and row_region != region:
            continue

        # Apply name/code search
        if q:
            q_lower = q.lower()
            if (q_lower not in info['name'].lower() and
                    q_lower not in code.lower()):
                continue

        # Spend (window-scoped, 0 if no sales in window) and all-time last_buy
        spend = spend_map.get(code, 0.0)
        last_buy = last_buy_map.get(code)

        # Call status
        last_called = last_called_map.get(code)
        target_days = crm_target_map.get(code) or DEFAULT_CALL_TARGET_DAYS
        cs, call_days = call_status(
            last_called[:10] if last_called else None,
            target_days,
        )

        # Apply call-status filter
        if call and cs != call:
            continue

        # Badges — ar and quiet are cheap; special is expensive so OMITTED from list
        # (kept on the card). See deviations.
        quiet_badge = False
        if last_buy:
            days_since_buy = (dt.date.today() - dt.date.fromisoformat(last_buy)).days
            quiet_badge = days_since_buy > 180

        result.append({
            'customer_code': code,
            'name':          info['name'],
            'province':      province,
            'region':        row_region,
            'last_buy':      last_buy,
            'spend':         spend,
            'call_status':   cs,
            'call_days':     call_days,
            'last_called':   last_called[:10] if last_called else None,
            'badges': {
                'ar':      False,   # populated cheaply in route via cf_mod.ar_aging when needed
                'quiet':   quiet_badge,
                'special': False,   # omitted from list — see deviations in call_card docstring
            },
        })

    # ── 6. Sort ───────────────────────────────────────────────────────────────
    if sort == 'spend':
        result.sort(key=lambda r: -(r['spend'] or 0))
    elif sort == 'last_buy':
        result.sort(key=lambda r: (r['last_buy'] or ''), reverse=True)
    elif sort == 'name':
        result.sort(key=lambda r: r['name'])
    elif sort == 'call':
        # due first, never second, recent last
        _order = {'due': 0, 'never': 1, 'recent': 2}
        result.sort(key=lambda r: _order.get(r['call_status'], 9))

    return result


# ── get_card ──────────────────────────────────────────────────────────────────

def get_card(conn, customer_code):
    """Assemble the full call-card data dict for one customer.

    Parameters
    ----------
    conn          : sqlite3 connection
    customer_code : canonical customer key (code or orphan name)

    Returns
    -------
    dict with keys:
      master       - customers row or synthetic dict for orphans
      summary      - get_customer_summary result
      products     - top products with peer pricing + base price (unit-aware) + promo
      winback      - products with ≥3 prior buys whose last buy > median inter-purchase interval
      clearance    - hard_to_sell products in-stock that match customer's bought categories
      ar           - AR detail from express snapshot
      crm          - customer_crm row or None
      log          - call log rows (non-deleted)
      call_status  - (status, days) tuple
    or None if the customer has no sales history.
    """
    # Lazy imports to avoid circular deps at module load time
    import ar_followup as arf_mod
    import cashflow as cf_mod
    import models
    import peer_pricing as pp

    # ── 1. Resolve canonical key → names for sales queries ───────────────────
    canon_code, names = arf_mod._resolve_target(conn, customer_code)
    # Use the first resolved name for get_customer_summary (it accepts names)
    primary_name = names[0] if names else customer_code

    # ── 2. Master row ─────────────────────────────────────────────────────────
    if canon_code:
        master_row = conn.execute(
            "SELECT * FROM customers WHERE code=?", (canon_code,)
        ).fetchone()
        if master_row:
            master = dict(master_row)
            # Ensure fax, nickname, and contact_note keys are always present (added
            # across various migrations; older rows may lack them in sqlite3.Row).
            master.setdefault('fax', None)
            master.setdefault('nickname', None)
            master.setdefault('contact_note', None)
        else:
            master = {'code': canon_code, 'name': primary_name, 'fax': None, 'nickname': None,
                      'contact_note': None}
    else:
        master = {'code': customer_code, 'name': primary_name, 'fax': None, 'nickname': None,
                  'contact_note': None}

    # ── 3. Sales summary (via models — uses customer NAME) ───────────────────
    summary = models.get_customer_summary(primary_name)

    # ── 4. Top products: peer pricing + unit-aware base + promo + price tiers ──
    products = _assemble_products(conn, names, canon_code)

    # ── 5. Win-back: products with ≥3 prior buys whose last buy > median interval
    winback = _compute_winback(conn, names)

    # ── 6. Clearance: hard_to_sell=1 products in stock that overlap customer's categories
    clearance = _compute_clearance(conn, products)

    # ── 7. AR detail ─────────────────────────────────────────────────────────
    ar = []
    try:
        ar = arf_mod.get_customer_ar_detail(customer=customer_code, conn=conn)
    except Exception:
        pass  # AR data not critical for card rendering

    # ── 8. CRM + log ─────────────────────────────────────────────────────────
    crm = get_crm(conn, customer_code)
    log = get_log(conn, customer_code)

    # ── 9. Call status ────────────────────────────────────────────────────────
    last_c = last_called_at(conn, customer_code)
    target = target_days_for(crm)
    cs, call_days = call_status(last_c[:10] if last_c else None, target)

    return {
        'master':       master,
        'summary':      summary,
        'products':     products,
        'winback':      winback,
        'clearance':    clearance,
        'ar':           ar,
        'crm':          crm,
        'log':          log,
        'call_status':  (cs, call_days),
    }


def _assemble_products(conn, names, canon_code):
    """Build the 'ซื้อประจำ' product list: the customer's top-30 products by net
    revenue, each enriched with unit-aware base price, the full active-promotion
    dict, quantity price-tiers, and peer pricing (the customer's latest line + a
    representative-peer line, both carrying gross list price + raw discount text).

    Extracted from get_card so the pricing assembly is unit-testable without the
    models.get_customer_summary dependency (which opens its own connection).
    """
    import peer_pricing as pp

    # Pull customer's top products by net revenue
    product_rows = conn.execute("""
        SELECT
            st.product_id,
            COALESCE(p.product_name, st.product_name_raw) AS product_name,
            st.unit,
            SUM(st.qty)   AS total_qty,
            SUM(st.net)   AS total_net,
            COUNT(DISTINCT st.doc_no) AS doc_count,
            MAX(st.date_iso) AS last_buy,
            p.base_sell_price,
            p.unit_type
        FROM sales_transactions st
        LEFT JOIN products p ON p.id = st.product_id
        WHERE st.customer IN ({})
          AND st.product_id IS NOT NULL
        GROUP BY st.product_id, st.unit
        ORDER BY total_net DESC
        LIMIT 30
    """.format(",".join("?" * len(names))), names).fetchall()

    # Build peer pricing map for this customer_code
    peer_map = {}
    if canon_code:
        peer_rows = pp.product_peer_prices(conn, canon_code)
        peer_map = {(r['product_id'], r['unit']): r for r in peer_rows}
        # Enrich each per-peer breakdown with the peer's customer NAME (one query)
        # so the "where does this price come from" modal can name the peers.
        peer_codes = {pe['code'] for r in peer_rows for pe in (r.get('peers') or [])}
        if peer_codes:
            nph = ",".join("?" * len(peer_codes))
            name_map = {nr['code']: nr['name'] for nr in conn.execute(
                f"SELECT code, name FROM customers WHERE code IN ({nph})", list(peer_codes)
            ).fetchall()}
            for r in peer_rows:
                for pe in (r.get('peers') or []):
                    pe['name'] = name_map.get(pe['code']) or pe['code']

    # Batch-fetch unit_conversions for all product_ids (one query, no per-product churn)
    if product_rows:
        pid_list = list({row['product_id'] for row in product_rows if row['product_id']})
        ph = ",".join("?" * len(pid_list))
        uc_rows = conn.execute(
            f"SELECT product_id, bsn_unit, ratio FROM unit_conversions WHERE product_id IN ({ph})",
            pid_list,
        ).fetchall()
        uc_map = {(r['product_id'], r['bsn_unit']): float(r['ratio'])
                  for r in uc_rows if r['ratio']}

        # Batch-fetch active promotions for all product_ids (one query, no per-product
        # connection open/close — models.get_active_promotion opens its own conn each call).
        # We apply the same promo-price logic here (same 3-branch if/elif/else as
        # models.effective_price) rather than calling effective_price, because
        # effective_price also opens its own connection and expects a pre-fetched
        # product dict with key 'id'. Reusing that function would still require N round-trips.
        # The math is verbatim from models.effective_price so the two can't drift silently;
        # any change to promo math in models must be mirrored here.
        today_str = dt.date.today().isoformat()
        promo_rows = conn.execute(f"""
            SELECT p.*
            FROM promotions p
            INNER JOIN (
                SELECT product_id, MAX(created_at) AS latest
                FROM promotions
                WHERE product_id IN ({ph})
                  AND is_active = 1
                  AND (date_start IS NULL OR date_start <= ?)
                  AND (date_end IS NULL OR date_end >= ?)
                GROUP BY product_id
            ) sub ON p.product_id = sub.product_id AND p.created_at = sub.latest
        """, pid_list + [today_str, today_str]).fetchall()
        promo_map = {r['product_id']: r for r in promo_rows}

        # Batch-fetch quantity price-tiers (one query) — shown in the click-through modal.
        tier_rows = conn.execute(
            f"SELECT product_id, qty_label, price, note FROM product_price_tiers "
            f"WHERE product_id IN ({ph}) ORDER BY sort_order, price",
            pid_list,
        ).fetchall()
        tiers_map = {}
        for r in tier_rows:
            tiers_map.setdefault(r['product_id'], []).append(dict(r))

        # Batch-fetch this customer's full order history for the card's products
        # (the product-name click modal). One query, grouped by product_id.
        order_rows = conn.execute(
            f"SELECT product_id, date_iso, doc_no, qty, unit, unit_price, discount, net, vat_type "
            f"FROM sales_transactions "
            f"WHERE customer IN ({','.join('?' * len(names))}) AND product_id IN ({ph}) "
            f"ORDER BY product_id, date_iso DESC, doc_no DESC",
            list(names) + pid_list,
        ).fetchall()
        orders_map = {}
        for r in order_rows:
            orders_map.setdefault(r['product_id'], []).append(dict(r))
    else:
        uc_map = {}
        promo_map = {}
        tiers_map = {}
        orders_map = {}

    products = []
    for row in product_rows:
        pid = row['product_id']
        unit = row['unit'] or ''
        base = row['base_sell_price'] or 0.0
        unit_type = row['unit_type'] or ''

        # Unit-aware base price: multiply base by ratio when unit != unit_type
        if unit and unit_type and unit != unit_type:
            ratio = uc_map.get((pid, unit))
            if ratio:
                base = base * ratio

        # Promo-price from pre-fetched batch (same logic as models.effective_price)
        promo = promo_map.get(pid)
        if promo:
            promo_label = f"{promo['promo_name']} ({promo['promo_type']})"
            if promo['promo_type'] == 'percent':
                promo_price = round(base * (1 - promo['discount_value'] / 100), 2)
            elif promo['promo_type'] == 'fixed':
                promo_price = promo['discount_value']
            else:
                # bundle / gift / mixed — per-unit price unchanged (same as effective_price)
                promo_price = base
        else:
            promo_label = None
            promo_price = base

        peer_key = (pid, unit)
        peer = peer_map.get(peer_key, {})

        # The card's "ราคาล่าสุดที่ลูกค้าได้" column shows the customer's MOST-RECENT
        # price, so the ส่วนต่าง flag must compare THAT (not the median) vs the peer
        # median — otherwise the shown price and the cheaper/higher flag would disagree.
        cust_latest = peer.get('customer_latest')
        peer_med = peer.get('peer_median')
        if cust_latest is None or peer_med is None:
            card_flag = 'same'
        elif cust_latest < peer_med:
            card_flag = 'cheaper'
        elif cust_latest > peer_med:
            card_flag = 'higher'
        else:
            card_flag = 'same'

        products.append({
            'product_id':      pid,
            'product_name':    row['product_name'],
            'unit':            unit,
            'total_qty':       row['total_qty'],
            'total_net':       row['total_net'],
            'doc_count':       row['doc_count'],
            'last_buy':        row['last_buy'],
            'base':            round(base, 2),
            'promo_label':     promo_label,
            'promo':           dict(promo) if promo else None,
            'price_tiers':     tiers_map.get(pid, []),
            'customer_price':  round(promo_price, 2),
            'customer_median': peer.get('customer_median'),
            'customer_latest': cust_latest,
            'customer_latest_list': peer.get('customer_latest_list'),
            'customer_latest_disc': peer.get('customer_latest_disc'),
            'peer_median':     peer_med,
            'peer_repr_list':  peer.get('peer_repr_list'),
            'peer_repr_disc':  peer.get('peer_repr_disc'),
            'peer_min':        peer.get('peer_min'),
            'peer_max':        peer.get('peer_max'),
            'peer_cheaper_pct': peer.get('peer_cheaper_pct'),
            'peers':           peer.get('peers', []),
            'orders':          orders_map.get(pid, []),
            'peer_n':          peer.get('peer_n', 0),
            'flag':            card_flag,
        })

    return products


def _compute_winback(conn, names):
    """Win-back list: products with ≥3 distinct purchase dates whose last buy
    is older than the median inter-purchase interval for that product.

    Returns list of dicts {product_id, product_name, unit, last_buy, median_gap_days}.
    """
    if not names:
        return []

    # Pull per-product purchase dates for this customer
    rows = conn.execute("""
        SELECT
            st.product_id,
            COALESCE(p.product_name, st.product_name_raw) AS product_name,
            st.unit,
            st.date_iso
        FROM sales_transactions st
        LEFT JOIN products p ON p.id = st.product_id
        WHERE st.customer IN ({})
          AND st.product_id IS NOT NULL
        ORDER BY st.product_id, st.unit, st.date_iso
    """.format(",".join("?" * len(names))), names).fetchall()

    # Group by (product_id, unit) → sorted date list
    from collections import defaultdict
    groups = defaultdict(lambda: {'product_name': None, 'dates': []})
    for row in rows:
        key = (row['product_id'], row['unit'])
        groups[key]['product_name'] = row['product_name']
        groups[key]['dates'].append(row['date_iso'])

    today_str = _today().isoformat()
    winback = []
    for (pid, unit), info in groups.items():
        dates = sorted(set(info['dates']))
        if len(dates) < 3:
            continue

        # Compute inter-purchase gaps
        date_objs = [dt.date.fromisoformat(d) for d in dates]
        gaps = [(date_objs[i+1] - date_objs[i]).days for i in range(len(date_objs)-1)]
        med_gap = statistics.median(gaps)

        last_date = dt.date.fromisoformat(dates[-1])
        days_since = (dt.date.today() - last_date).days

        if days_since > med_gap:
            winback.append({
                'product_id':      pid,
                'product_name':    info['product_name'],
                'unit':            unit,
                'last_buy':        dates[-1],
                'median_gap_days': int(med_gap),
                'days_since':      days_since,
            })

    winback.sort(key=lambda r: -r['days_since'])
    return winback


def _compute_clearance(conn, products):
    """Clearance: hard_to_sell=1 products with stock > 0 that share a category
    with any product the customer has bought.

    Returns list of dicts {product_id, product_name, unit_type, quantity, base_sell_price}.
    """
    # Gather category_ids from the customer's bought products
    if not products:
        return []

    bought_pids = [p['product_id'] for p in products if p['product_id']]
    if not bought_pids:
        return []

    ph = ",".join("?" * len(bought_pids))
    cat_rows = conn.execute(
        f"SELECT DISTINCT category_id FROM products WHERE id IN ({ph}) AND category_id IS NOT NULL",
        bought_pids,
    ).fetchall()
    cat_ids = [r['category_id'] for r in cat_rows]
    if not cat_ids:
        return []

    ph_cats = ",".join("?" * len(cat_ids))
    clearance_rows = conn.execute(
        f"""
        SELECT p.id AS product_id, p.product_name, p.unit_type, p.base_sell_price, sl.quantity
        FROM products p
        JOIN stock_levels sl ON sl.product_id = p.id
        WHERE p.hard_to_sell=1
          AND p.category_id IN ({ph_cats})
          AND sl.quantity > 0
          AND p.is_active=1
        ORDER BY sl.quantity DESC
        LIMIT 20
        """,
        cat_ids,
    ).fetchall()

    return [
        {
            'product_id':     r['product_id'],
            'product_name':   r['product_name'],
            'unit_type':      r['unit_type'],
            'base_sell_price': r['base_sell_price'],
            'quantity':       r['quantity'],
        }
        for r in clearance_rows
    ]
