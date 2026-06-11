"""ตรวจบิล detection engine for Sendy ERP.

Pure module — no Flask routes, no UI, no module-global mutable state.
All review state lives in DB tables txn_review_docs + txn_review_flags
(migration 098).

Detection rules (R1–R5; R6 deferred to v1.1):
  R1_UNMAPPED    — product_id IS NULL
  R2_BELOW_COST  — effective unit price < cost × ratio × R2_TOLERANCE
  R3_PRICE_DEVIATION — unit_price deviates > R3_PCT from historical median
                       AND absolute deviation >= R3_MIN_BAHT
  R4_UNUSUAL_UNIT — mapped product, unit != unit_type, no unit_conversions row
  R5_PROMO_MISMATCH — active promo on date_iso but price doesn't match
                      (percent/fixed only; bundle/gift/mixed skipped)

SR rows (ref_invoice non-empty OR qty <= 0) skip price rules R2/R3/R5.
R1 still applies to SR rows.

Public API (mirrors ar_followup.py connection style):
  scan_batch(batch_id, conn=None, db_path=None)   → dict summary
  get_batch_review(batch_id, conn=None, db_path=None) → {date_iso: [doc+flags]}
  get_sales_batches(limit=20, conn=None, db_path=None) → list
  mark_doc(doc_review_id, status, note, reviewed_by, conn=None, db_path=None)
  pending_review_count(conn=None, db_path=None)   → int
"""
import hashlib
import json
import sqlite3
import statistics
from typing import Optional, List

import config

# ── Detection thresholds ─────────────────────────────────────────────────────

R3_PCT = 0.20          # 20% deviation threshold for price history check
R3_MIN_BAHT = 2.0      # abs deviation must also be >= ฿2
R2_TOLERANCE = 0.99    # sell eff < cost * ratio * 0.99 → flag
R5_TOLERANCE = 0.01    # promo expected price within 1% → pass
LOOKBACK_DAYS = 365    # how many days of history to consider for R3

_SEVERITY_ORDER = {'high': 3, 'medium': 2, 'low': 1}


# ── Connection helpers (mirror ar_followup.py) ────────────────────────────────

def _connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or config.DATABASE_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class _ConnCtx:
    def __init__(self, conn, db_path):
        self._given, self._db_path, self._owned = conn, db_path, None

    def __enter__(self):
        if self._given is not None:
            return self._given
        self._owned = _connect(self._db_path)
        return self._owned

    def __exit__(self, *exc):
        if self._owned is not None:
            self._owned.close()
        return False


# ── Product cache helpers ─────────────────────────────────────────────────────

def _get_product(conn, product_id: int):
    """Return a products row dict or None."""
    return conn.execute(
        "SELECT id, unit_type, cost_price, base_sell_price FROM products WHERE id=?",
        (product_id,)
    ).fetchone()


def _get_ratio(conn, product_id: int, bsn_unit: str, unit_type: str):
    """Return (ratio, found) where ratio=1.0 and found=True when unit==unit_type,
    or the unit_conversions ratio when found, or (None, False) when missing."""
    if bsn_unit == unit_type:
        return 1.0, True
    row = conn.execute(
        "SELECT ratio FROM unit_conversions WHERE product_id=? AND bsn_unit=?",
        (product_id, bsn_unit)
    ).fetchone()
    if row:
        return float(row['ratio']), True
    return None, False


# ── Promo helper (date-parameterized, NOT get_active_promotion()) ─────────────

def _get_active_promo_on_date(conn, product_id: int, date_iso: str):
    """Return the most recent active promo for product on date_iso.

    Mirrors get_active_promotion() semantics but uses date_iso instead of
    today — R5 must check historical data, not the current catalog state.
    """
    return conn.execute("""
        SELECT id, promo_name, promo_type, discount_value
        FROM promotions
        WHERE product_id = ? AND is_active = 1
          AND (date_start IS NULL OR date_start <= ?)
          AND (date_end   IS NULL OR date_end   >= ?)
        ORDER BY id DESC
        LIMIT 1
    """, (product_id, date_iso, date_iso)).fetchone()


def _promo_expected_per_base_unit(product, promo) -> Optional[float]:
    """Compute expected per-base-unit price from promo.

    Returns None for bundle/gift/mixed (skip).
    Mirrors effective_price() semantics from models.py:
      percent → base * (1 - disc/100), rounded 2dp
      fixed   → discount_value (IS the final selling price)
    """
    ptype = promo['promo_type']
    if ptype in ('bundle', 'gift', 'mixed'):
        return None
    if ptype == 'percent':
        return round(product['base_sell_price'] * (1 - promo['discount_value'] / 100), 2)
    if ptype == 'fixed':
        return float(promo['discount_value'])
    return None


# ── R3 median helpers ─────────────────────────────────────────────────────────

def _median(values: List[float]) -> float:
    n = len(values)
    if n == 0:
        return 0.0
    sorted_v = sorted(values)
    mid = n // 2
    if n % 2 == 0:
        return (sorted_v[mid - 1] + sorted_v[mid]) / 2.0
    return sorted_v[mid]


def _r3_history(conn, batch_id: int, product_id: int, unit: str,
                customer_code: Optional[str], date_iso: str):
    """Return (median, source_label) for R3, or (None, None) if insufficient history.

    Customer-specific first: >= 2 prior lines for this customer_code.
    Global fallback: >= 3 prior lines across >= 2 distinct doc_bases.
    History excludes the current batch_id and uses LOOKBACK_DAYS from date_iso.
    """
    from_date = _subtract_days(date_iso, LOOKBACK_DAYS)

    if customer_code:
        rows = conn.execute("""
            SELECT unit_price
            FROM sales_transactions
            WHERE product_id=? AND unit=? AND customer_code=?
              AND batch_id != ?
              AND date_iso >= ? AND date_iso <= ?
              AND (ref_invoice IS NULL OR ref_invoice = '')
              AND qty > 0
        """, (product_id, unit, customer_code, batch_id, from_date, date_iso)).fetchall()
        prices_cust = [float(r['unit_price']) for r in rows if r['unit_price'] is not None]
        if len(prices_cust) >= 2:
            return _median(prices_cust), 'ร้านนี้'

    # Global fallback
    rows = conn.execute("""
        SELECT unit_price, doc_base
        FROM sales_transactions
        WHERE product_id=? AND unit=?
          AND batch_id != ?
          AND date_iso >= ? AND date_iso <= ?
          AND (ref_invoice IS NULL OR ref_invoice = '')
          AND qty > 0
    """, (product_id, unit, batch_id, from_date, date_iso)).fetchall()
    prices_global = [float(r['unit_price']) for r in rows if r['unit_price'] is not None]
    doc_bases = {r['doc_base'] for r in rows}
    if len(prices_global) >= 3 and len(doc_bases) >= 2:
        return _median(prices_global), 'ทั่วไป'

    return None, None


def _subtract_days(date_iso: str, days: int) -> str:
    """Return date_iso - days as an ISO string (simple math, no imports needed)."""
    from datetime import date, timedelta
    d = date.fromisoformat(date_iso)
    return (d - timedelta(days=days)).isoformat()


# ── Row-level rule checks ─────────────────────────────────────────────────────

def _check_row_rules(conn, row: dict) -> List[dict]:
    """Apply R1–R5 to one sales_transactions row.

    row must have keys matching sales_transactions columns.
    Returns a list of flag dicts (may be empty if no flags).
    Each flag: {rule_code, severity, message_th, details_json}
    """
    flags = []
    product_id = row.get('product_id')
    unit = row.get('unit') or ''
    unit_price = float(row.get('unit_price') or 0)
    qty = float(row.get('qty') or 0)
    net = float(row.get('net') or 0)
    bsn_code = row.get('bsn_code') or ''
    raw_name = row.get('product_name_raw') or ''
    ref_invoice = row.get('ref_invoice') or ''
    date_iso = row.get('date_iso') or ''
    customer_code = row.get('customer_code') or ''
    doc_no = row.get('doc_no') or ''
    batch_id = row.get('batch_id', 0)

    # Determine if this is an SR / return row (skip price rules)
    is_sr = bool(ref_invoice.strip()) or qty <= 0

    # ── R1_UNMAPPED ──────────────────────────────────────────────────────────
    if product_id is None:
        flags.append({
            'rule_code': 'R1_UNMAPPED',
            'severity': 'high',
            'message_th': f'ยังไม่ได้ผูกสินค้า — รหัส BSN {bsn_code} ({raw_name})',
            'details_json': json.dumps({'bsn_code': bsn_code, 'raw_name': raw_name},
                                       ensure_ascii=False),
        })
        # No product → R2/R3/R4/R5 cannot run (no unit_type to compare)
        return flags

    product = _get_product(conn, product_id)
    if product is None:
        # Mapped but product row gone (data quality) — treat as unmapped
        flags.append({
            'rule_code': 'R1_UNMAPPED',
            'severity': 'high',
            'message_th': f'ยังไม่ได้ผูกสินค้า — รหัส BSN {bsn_code} ({raw_name})',
            'details_json': json.dumps({'bsn_code': bsn_code, 'raw_name': raw_name},
                                       ensure_ascii=False),
        })
        return flags

    unit_type = product['unit_type'] or 'ตัว'
    cost_price = float(product['cost_price'] or 0)
    base_sell_price = float(product['base_sell_price'] or 0)

    # ── R4_UNUSUAL_UNIT ──────────────────────────────────────────────────────
    # Check before R2 so we know whether ratio is available
    ratio, ratio_found = _get_ratio(conn, product_id, unit, unit_type)

    if unit != unit_type and not ratio_found:
        flags.append({
            'rule_code': 'R4_UNUSUAL_UNIT',
            'severity': 'high',
            'message_th': (
                f'หน่วย "{unit}" ไม่มีอัตราแปลง — สต๊อกจะไม่ตัด '
                f'(หน่วยหลัก: {unit_type})'
            ),
            'details_json': json.dumps({
                'unit_sold': unit, 'unit_type': unit_type,
            }, ensure_ascii=False),
        })
        # No ratio → R2/R5 cannot compute accurate per-base-unit price
        # Still run R3 (uses unit_price directly, no ratio)
        if not is_sr:
            med, src = _r3_history(conn, batch_id, product_id, unit,
                                   customer_code or None, date_iso)
            if med is not None and med > 0:
                pct_dev = abs(unit_price - med) / med
                abs_dev = abs(unit_price - med)
                if pct_dev > R3_PCT and abs_dev >= R3_MIN_BAHT:
                    flags.append({
                        'rule_code': 'R3_PRICE_DEVIATION',
                        'severity': 'medium',
                        'message_th': (
                            f'ราคาขาย {unit_price}/{unit} '
                            f'ต่างจากที่เคยขาย{src} {med:.2f} '
                            f'({_pct_str(unit_price, med)})'
                        ),
                        'details_json': json.dumps({
                            'unit_price': unit_price, 'median': med,
                            'pct_dev': round(pct_dev * 100, 1),
                            'source': src,
                        }, ensure_ascii=False),
                    })
        return flags

    # ratio is now valid (1.0 if unit == unit_type, or from unit_conversions)
    if ratio is None:
        ratio = 1.0

    # eff = effective per-base-unit selling price (ex-VAT)
    eff = (net / qty) if qty != 0 else unit_price
    # per-base-unit: if ratio>1, unit_price is per "derived" unit (e.g. per โหล)
    # The per-base-unit price to compare vs cost_price is unit_price / ratio
    per_base = unit_price / ratio if ratio else unit_price

    # ── R2_BELOW_COST ────────────────────────────────────────────────────────
    if not is_sr and cost_price > 0:
        # eff per-base-unit should be >= cost_price * R2_TOLERANCE
        # But the test for R2 uses eff = net/qty (per sold unit = per derived unit)
        # and compares against cost * ratio
        cost_per_sold_unit = cost_price * ratio
        if (eff < cost_per_sold_unit * R2_TOLERANCE):
            flags.append({
                'rule_code': 'R2_BELOW_COST',
                'severity': 'high',
                'message_th': (
                    f'ขาย {eff:.2f}/{unit} ต่ำกว่าทุน {cost_per_sold_unit:.2f} '
                    f'(ทุน {cost_price:.2f}/{unit_type} × {ratio})'
                ),
                'details_json': json.dumps({
                    'eff': round(eff, 4),
                    'cost_price': cost_price,
                    'ratio': ratio,
                    'cost_per_sold_unit': round(cost_per_sold_unit, 4),
                    'unit': unit,
                    'unit_type': unit_type,
                }, ensure_ascii=False),
            })

    # ── R3_PRICE_DEVIATION ───────────────────────────────────────────────────
    if not is_sr:
        med, src = _r3_history(conn, batch_id, product_id, unit,
                               customer_code or None, date_iso)
        if med is not None and med > 0:
            pct_dev = abs(unit_price - med) / med
            abs_dev = abs(unit_price - med)
            if pct_dev > R3_PCT and abs_dev >= R3_MIN_BAHT:
                flags.append({
                    'rule_code': 'R3_PRICE_DEVIATION',
                    'severity': 'medium',
                    'message_th': (
                        f'ราคาขาย {unit_price}/{unit} '
                        f'ต่างจากที่เคยขาย{src} {med:.2f} '
                        f'({_pct_str(unit_price, med)})'
                    ),
                    'details_json': json.dumps({
                        'unit_price': unit_price, 'median': med,
                        'pct_dev': round(pct_dev * 100, 1),
                        'source': src,
                    }, ensure_ascii=False),
                })

    # ── R5_PROMO_MISMATCH ────────────────────────────────────────────────────
    if not is_sr:
        promo = _get_active_promo_on_date(conn, product_id, date_iso)
        if promo is not None:
            expected_per_base = _promo_expected_per_base_unit(
                {'base_sell_price': base_sell_price}, promo
            )
            if expected_per_base is not None:
                # Scale expected to the sold unit
                expected_per_sold = expected_per_base * ratio
                # Check if sold at base_sell_price × ratio (not applying promo)
                base_per_sold = base_sell_price * ratio
                sold_at_full_price = abs(unit_price - base_per_sold) / (base_per_sold or 1) <= R5_TOLERANCE

                # Pass if within R5_TOLERANCE of expected promo price
                within_promo = abs(unit_price - expected_per_sold) / (expected_per_sold or 1) <= R5_TOLERANCE

                # Pass if matches any price tier
                tier_match = _matches_tier(conn, product_id, unit_price)

                if not within_promo and not tier_match:
                    if sold_at_full_price:
                        msg = (
                            f'มีโปร "{promo["promo_name"]}" คาดราคา {expected_per_sold:.2f} '
                            f'แต่ขาย {unit_price} — ไม่ได้ใช้โปร?'
                        )
                    else:
                        msg = (
                            f'มีโปร "{promo["promo_name"]}" คาดราคา {expected_per_sold:.2f} '
                            f'แต่ขาย {unit_price}'
                        )
                    flags.append({
                        'rule_code': 'R5_PROMO_MISMATCH',
                        'severity': 'medium',
                        'message_th': msg,
                        'details_json': json.dumps({
                            'promo_name': promo['promo_name'],
                            'promo_type': promo['promo_type'],
                            'expected': round(expected_per_sold, 2),
                            'observed': unit_price,
                        }, ensure_ascii=False),
                    })

    return flags


def _matches_tier(conn, product_id: int, unit_price: float) -> bool:
    """Return True if unit_price matches any product_price_tiers.price (within 0.5)."""
    rows = conn.execute(
        "SELECT price FROM product_price_tiers WHERE product_id=?", (product_id,)
    ).fetchall()
    for r in rows:
        if abs(float(r['price']) - unit_price) < 0.5:
            return True
    return False


def _pct_str(actual: float, median: float) -> str:
    if median == 0:
        return '—'
    pct = (actual - median) / median * 100
    sign = '+' if pct >= 0 else ''
    return f'{sign}{pct:.1f}%'


# ── Fingerprint ───────────────────────────────────────────────────────────────

def _make_fingerprint(flags_list: List[dict], doc_no_list: List[str],
                      qty_list: List[float], price_list: List[float]) -> Optional[str]:
    """SHA-1 of sorted (rule_code|doc_no|qty|unit_price) tuples."""
    if not flags_list:
        return None
    parts = []
    for f, doc_no, qty, price in zip(flags_list, doc_no_list, qty_list, price_list):
        parts.append(f"{f['rule_code']}|{doc_no}|{qty}|{price}")
    parts.sort()
    return hashlib.sha1('\n'.join(parts).encode('utf-8')).hexdigest()


# ── Public API ────────────────────────────────────────────────────────────────

def scan_batch(batch_id: int,
               conn: Optional[sqlite3.Connection] = None,
               db_path: Optional[str] = None) -> dict:
    """Idempotent: delete & recompute all flags for batch_id in ONE transaction.

    Review decisions (ok/wrong) survive if the flags_fingerprint is unchanged.
    Changed or newly flagged docs reset to 'pending' (old note prefixed '[เดิม]').
    Clean docs get review_status='auto_passed'.

    Returns summary dict:
      docs_total, docs_clean, docs_flagged, flags_total
    """
    with _ConnCtx(conn, db_path) as c:
        # Load all sales rows for this batch
        rows = c.execute("""
            SELECT s.id, s.batch_id, s.date_iso, s.doc_no, s.doc_base,
                   s.product_id, s.bsn_code, s.product_name_raw,
                   s.customer, s.customer_code, s.qty, s.unit,
                   s.unit_price, s.vat_type, s.net, s.ref_invoice
            FROM sales_transactions s
            WHERE s.batch_id = ?
            ORDER BY s.doc_base, s.doc_no
        """, (batch_id,)).fetchall()

        # Group by doc_base
        docs = {}
        for r in rows:
            db = r['doc_base']
            if db not in docs:
                docs[db] = {
                    'date_iso': r['date_iso'],
                    'customer': r['customer'],
                    'customer_code': r['customer_code'],
                    'lines': [],
                }
            docs[db]['lines'].append(dict(r))

        # Snapshot existing decisions keyed by doc_base
        old_docs = {}
        for row in c.execute(
            "SELECT doc_base, flags_fingerprint, review_status, reviewed_by, reviewed_at, note "
            "FROM txn_review_docs WHERE batch_id=?", (batch_id,)
        ).fetchall():
            old_docs[row['doc_base']] = dict(row)

        # Delete all existing flags for this batch (doc rows are UPSERTed below)
        c.execute("""
            DELETE FROM txn_review_flags
            WHERE doc_review_id IN (
                SELECT id FROM txn_review_docs WHERE batch_id=?
            )
        """, (batch_id,))

        total_flags = 0
        docs_clean = 0
        docs_flagged = 0

        for doc_base, doc_info in docs.items():
            all_flags = []
            doc_nos = []
            qtys = []
            prices = []

            for line in doc_info['lines']:
                line_flags = _check_row_rules(c, line)
                for fl in line_flags:
                    all_flags.append(fl)
                    doc_nos.append(line['doc_no'])
                    qtys.append(float(line['qty'] or 0))
                    prices.append(float(line['unit_price'] or 0))

            flag_count = len(all_flags)
            new_fp = _make_fingerprint(all_flags, doc_nos, qtys, prices)

            # Determine max severity
            max_sev = None
            for fl in all_flags:
                sev = fl['severity']
                if max_sev is None or _SEVERITY_ORDER[sev] > _SEVERITY_ORDER[max_sev]:
                    max_sev = sev

            # Decide review_status
            old = old_docs.get(doc_base)
            if flag_count == 0:
                new_status = 'auto_passed'
                docs_clean += 1
            else:
                docs_flagged += 1
                if old and old['flags_fingerprint'] == new_fp and old['review_status'] in ('ok', 'wrong'):
                    # Unchanged fingerprint → keep human decision
                    new_status = old['review_status']
                else:
                    new_status = 'pending'

            # Determine note carry-forward
            carry_note = None
            carry_by = None
            carry_at = None
            if old:
                if new_status == 'pending' and old.get('note'):
                    carry_note = f"[เดิม] {old['note']}"
                elif new_status in ('ok', 'wrong'):
                    carry_note = old.get('note')
                    carry_by = old.get('reviewed_by')
                    carry_at = old.get('reviewed_at')

            # UPSERT doc row
            c.execute("""
                INSERT INTO txn_review_docs
                    (batch_id, doc_base, date_iso, customer, customer_code,
                     line_count, flag_count, max_severity, flags_fingerprint,
                     review_status, reviewed_by, reviewed_at, note)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(batch_id, doc_base) DO UPDATE SET
                    date_iso          = excluded.date_iso,
                    customer          = excluded.customer,
                    customer_code     = excluded.customer_code,
                    line_count        = excluded.line_count,
                    flag_count        = excluded.flag_count,
                    max_severity      = excluded.max_severity,
                    flags_fingerprint = excluded.flags_fingerprint,
                    review_status     = excluded.review_status,
                    reviewed_by       = excluded.reviewed_by,
                    reviewed_at       = excluded.reviewed_at,
                    note              = excluded.note
            """, (
                batch_id, doc_base, doc_info['date_iso'],
                doc_info['customer'], doc_info['customer_code'],
                len(doc_info['lines']), flag_count, max_sev, new_fp,
                new_status, carry_by, carry_at, carry_note,
            ))

            # Get the doc_review_id
            doc_review_id = c.execute(
                "SELECT id FROM txn_review_docs WHERE batch_id=? AND doc_base=?",
                (batch_id, doc_base)
            ).fetchone()['id']

            # Insert flags
            for fl, doc_no, qty, price in zip(all_flags, doc_nos, qtys, prices):
                # Find the txn_id for this doc_no
                txn_row = c.execute(
                    "SELECT id FROM sales_transactions WHERE batch_id=? AND doc_no=?",
                    (batch_id, doc_no)
                ).fetchone()
                txn_id = txn_row['id'] if txn_row else None
                c.execute("""
                    INSERT INTO txn_review_flags
                        (doc_review_id, batch_id, txn_id, doc_no,
                         rule_code, severity, message_th, details_json)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (
                    doc_review_id, batch_id, txn_id, doc_no,
                    fl['rule_code'], fl['severity'], fl['message_th'],
                    fl.get('details_json'),
                ))
                total_flags += 1

        # Prune docs no longer present in this batch. The diff-based importer
        # DELETEs changed lines from an old batch (they re-enter under a new
        # batch_id), so a doc can vanish from batch N — without this prune its
        # stale txn_review_docs row would survive as a ghost 'pending' doc
        # inflating pending_review_count() and batch progress.
        # Subquery = current doc set; emptied batch → prunes everything.
        # (Flags were already deleted above while the stale doc rows existed.)
        c.execute("""
            DELETE FROM txn_review_docs
            WHERE batch_id = ?
              AND doc_base NOT IN (
                  SELECT DISTINCT doc_base FROM sales_transactions
                  WHERE batch_id = ? AND doc_base IS NOT NULL
              )
        """, (batch_id, batch_id))

        if conn is None:
            c.commit()

        return {
            'batch_id': batch_id,
            'docs_total': docs_clean + docs_flagged,
            'docs_clean': docs_clean,
            'docs_flagged': docs_flagged,
            'flags_total': total_flags,
        }


def get_batch_review(batch_id: int,
                     conn: Optional[sqlite3.Connection] = None,
                     db_path: Optional[str] = None) -> dict:
    """Return all docs + flags for batch_id, grouped by date_iso.

    Returns {date_iso: [doc_dict_with_flags_list, ...]}
    """
    with _ConnCtx(conn, db_path) as c:
        docs = c.execute("""
            SELECT id, batch_id, doc_base, date_iso, customer, customer_code,
                   line_count, flag_count, max_severity, flags_fingerprint,
                   review_status, reviewed_by, reviewed_at, note, created_at
            FROM txn_review_docs
            WHERE batch_id=?
            ORDER BY date_iso, doc_base
        """, (batch_id,)).fetchall()

        result = {}
        for d in docs:
            date_iso = d['date_iso'] or ''
            flags = c.execute("""
                SELECT id, txn_id, doc_no, rule_code, severity, message_th,
                       details_json, created_at
                FROM txn_review_flags
                WHERE doc_review_id=?
                ORDER BY CASE severity
                             WHEN 'high'   THEN 3
                             WHEN 'medium' THEN 2
                             ELSE 1
                         END DESC, id
            """, (d['id'],)).fetchall()
            doc_dict = dict(d)
            doc_dict['flags'] = [dict(f) for f in flags]
            result.setdefault(date_iso, []).append(doc_dict)

        return result


def get_sales_batches(limit: int = 20,
                      conn: Optional[sqlite3.Connection] = None,
                      db_path: Optional[str] = None) -> List[dict]:
    """Return import_log rows where notes='sales', newest first, with review progress.

    Each row includes: id, filename, rows_imported, rows_skipped, notes,
    imported_at, docs_total, docs_auto_passed, docs_pending, docs_ok, docs_wrong.
    """
    with _ConnCtx(conn, db_path) as c:
        batches = c.execute("""
            SELECT id, filename, rows_imported, rows_skipped, notes, imported_at
            FROM import_log
            WHERE notes = 'sales'
            ORDER BY id DESC
            LIMIT ?
        """, (limit,)).fetchall()

        result = []
        for b in batches:
            bid = b['id']
            counts = c.execute("""
                SELECT
                    COUNT(*) as docs_total,
                    SUM(CASE WHEN review_status='auto_passed' THEN 1 ELSE 0 END) as docs_auto_passed,
                    SUM(CASE WHEN review_status='pending'     THEN 1 ELSE 0 END) as docs_pending,
                    SUM(CASE WHEN review_status='ok'          THEN 1 ELSE 0 END) as docs_ok,
                    SUM(CASE WHEN review_status='wrong'       THEN 1 ELSE 0 END) as docs_wrong
                FROM txn_review_docs
                WHERE batch_id=?
            """, (bid,)).fetchone()
            row = dict(b)
            row['docs_total']       = counts['docs_total']       or 0
            row['docs_auto_passed'] = counts['docs_auto_passed'] or 0
            row['docs_pending']     = counts['docs_pending']     or 0
            row['docs_ok']          = counts['docs_ok']          or 0
            row['docs_wrong']       = counts['docs_wrong']       or 0
            result.append(row)

        return result


def mark_doc(doc_review_id: int,
             status: str,
             note: Optional[str],
             reviewed_by: str,
             conn: Optional[sqlite3.Connection] = None,
             db_path: Optional[str] = None) -> None:
    """Update review decision on a txn_review_docs row.

    status must be 'ok' or 'wrong'.
    Raises ValueError for invalid status.
    """
    if status not in ('ok', 'wrong'):
        raise ValueError(f"Invalid status '{status}'; must be 'ok' or 'wrong'")

    with _ConnCtx(conn, db_path) as c:
        c.execute("""
            UPDATE txn_review_docs
            SET review_status = ?,
                reviewed_by   = ?,
                reviewed_at   = datetime('now','localtime'),
                note          = ?
            WHERE id = ?
        """, (status, reviewed_by, note, doc_review_id))
        if conn is None:
            c.commit()


def pending_review_count(conn: Optional[sqlite3.Connection] = None,
                         db_path: Optional[str] = None) -> int:
    """Count txn_review_docs rows with review_status='pending'. Returns 0 on error."""
    with _ConnCtx(conn, db_path) as c:
        try:
            row = c.execute(
                "SELECT COUNT(*) FROM txn_review_docs WHERE review_status='pending'"
            ).fetchone()
            return int(row[0]) if row else 0
        except sqlite3.OperationalError:
            # Table doesn't exist yet (before migration applied)
            return 0
