"""ตรวจบิล detection engine for Sendy ERP (v2 — whole-dataset, read-only).

Pure module — no Flask routes, no UI, no module-global mutable state.
All review state lives in DB tables txn_review_docs + txn_review_flags
(migration 099 — doc-keyed, suspicious-only, no decision lifecycle).

Detection rules (R1–R5, R7):
  R1_UNMAPPED         — product_id IS NULL
  R2_BELOW_COST       — effective unit price < cost × ratio × R2_TOLERANCE
  R3_PRICE_DEVIATION  — unit_price deviates > R3_PCT from historical median
                        AND absolute deviation >= R3_MIN_BAHT
  R4_UNUSUAL_UNIT     — mapped product, unit != unit_type, no unit_conversions row
  R5_PROMO_MISMATCH   — active promo on date_iso but price doesn't match
  R7_ALL_FREE         — whole document has zero-revenue lines and no paid lines

Free lines (qty > 0 AND abs(net) < 0.005) skip price rules R2/R3/R5.
SR rows (ref_invoice non-empty OR qty <= 0) also skip price rules.
R1/R4 apply to all rows.

Public API:
  default_since()                                     → ISO date ~183 days back
  scan_all(conn=None, db_path=None)                   → dict summary
  scan_docs(doc_bases, conn=None, db_path=None)       → dict summary
  scan_after_import(batch_id, conn=None, db_path=None) → dict summary
  get_review_feed(since_date=None, limit=None, conn=None, db_path=None) → list
  suspicious_count(since_date=None, conn=None, db_path=None) → int
"""
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

R7_ALL_FREE = 'R7_ALL_FREE'


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


def _r3_history(conn, doc_base: str, product_id: int, unit: str,
                customer_code: Optional[str], date_iso: str):
    """Return (median, source_label) for R3, or (None, None) if insufficient history.

    Customer-specific first: >= 2 prior lines for this customer_code.
    Global fallback: >= 3 prior lines across >= 2 distinct doc_bases.
    History excludes the current doc_base and uses LOOKBACK_DAYS from date_iso.
    """
    from_date = _subtract_days(date_iso, LOOKBACK_DAYS)

    if customer_code:
        rows = conn.execute("""
            SELECT unit_price
            FROM sales_transactions
            WHERE product_id=? AND unit=? AND customer_code=?
              AND doc_base != ?
              AND date_iso >= ? AND date_iso <= ?
              AND (ref_invoice IS NULL OR ref_invoice = '')
              AND qty > 0
        """, (product_id, unit, customer_code, doc_base, from_date, date_iso)).fetchall()
        prices_cust = [float(r['unit_price']) for r in rows if r['unit_price'] is not None]
        if len(prices_cust) >= 2:
            return _median(prices_cust), 'ร้านนี้'

    # Global fallback
    rows = conn.execute("""
        SELECT unit_price, doc_base
        FROM sales_transactions
        WHERE product_id=? AND unit=?
          AND doc_base != ?
          AND date_iso >= ? AND date_iso <= ?
          AND (ref_invoice IS NULL OR ref_invoice = '')
          AND qty > 0
    """, (product_id, unit, doc_base, from_date, date_iso)).fetchall()
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


# ── Doc-level helpers ────────────────────────────────────────────────────────

def _fmt_qty(q) -> str:
    return f'{float(q or 0):g}'   # 25.0→'25', 1.5→'1.5'


def _evaluate_doc(conn, lines: List[dict]) -> dict:
    """Evaluate one document (all sales_transactions rows sharing a doc_base).

    Returns:
      flags          — list of (line_dict, flag_dict) tuples (R1..R5 + R7)
      free_goods_note — '; '-joined แถม descriptions, or None
      max_severity   — 'high'/'medium'/'low'/None
      line_count     — total lines
      real_flag_count — len(flags)
    """
    real_flags = []
    free_notes = []
    paid_count = 0
    free_count = 0
    for line in lines:
        qty = float(line.get('qty') or 0)
        net = float(line.get('net') or 0)
        is_sr = bool((line.get('ref_invoice') or '').strip()) or qty <= 0
        is_free = qty > 0 and abs(net) < 0.005
        if is_free:
            free_count += 1
            name = line.get('product_name_raw') or line.get('bsn_code') or ''
            unit = line.get('unit') or ''
            free_notes.append(f'แถม {_fmt_qty(qty)} {unit} {name}'.strip())
        elif not is_sr:
            paid_count += 1
        for fl in _check_row_rules(conn, line):
            real_flags.append((line, fl))
    # All-free doc: free lines present, no paid lines → R7 (low)
    if free_count > 0 and paid_count == 0:
        real_flags.append((lines[0], {
            'rule_code': R7_ALL_FREE, 'severity': 'low',
            'message_th': 'ทั้งบิลไม่มีราคา — เช็คหน่อย', 'details_json': None,
        }))
    max_sev = None
    for _, fl in real_flags:
        s = fl['severity']
        if max_sev is None or _SEVERITY_ORDER[s] > _SEVERITY_ORDER[max_sev]:
            max_sev = s
    return {
        'flags': real_flags,
        'free_goods_note': '; '.join(free_notes) if free_notes else None,
        'max_severity': max_sev,
        'line_count': len(lines),
        'real_flag_count': len(real_flags),
    }


# ── Scan / persist (doc-level writes) ─────────────────────────────────────────

_SALES_COLS = """s.id, s.batch_id, s.date_iso, s.doc_no, s.doc_base,
    s.product_id, s.bsn_code, s.product_name_raw, s.customer, s.customer_code,
    s.qty, s.unit, s.unit_price, s.vat_type, s.net, s.ref_invoice"""


def _persist_doc(c, doc_base: str, lines: List[dict]) -> Optional[int]:
    """Upsert suspicious doc + flags; delete the row if doc is now clean.

    Returns flag count when suspicious, None when clean.
    """
    ev = _evaluate_doc(c, lines)
    if ev['real_flag_count'] == 0:
        c.execute("DELETE FROM txn_review_docs WHERE doc_base=?", (doc_base,))
        return None
    head = lines[0]
    c.execute("""
        INSERT INTO txn_review_docs
            (doc_base, date_iso, customer, customer_code, line_count,
             flag_count, max_severity, free_goods_note, scanned_at)
        VALUES (?,?,?,?,?,?,?,?,datetime('now','localtime'))
        ON CONFLICT(doc_base) DO UPDATE SET
            date_iso=excluded.date_iso, customer=excluded.customer,
            customer_code=excluded.customer_code, line_count=excluded.line_count,
            flag_count=excluded.flag_count, max_severity=excluded.max_severity,
            free_goods_note=excluded.free_goods_note, scanned_at=excluded.scanned_at
    """, (doc_base, head.get('date_iso') or '', head.get('customer'),
          head.get('customer_code'), ev['line_count'], ev['real_flag_count'],
          ev['max_severity'], ev['free_goods_note']))
    c.execute("DELETE FROM txn_review_flags WHERE doc_base=?", (doc_base,))
    for line, fl in ev['flags']:
        c.execute("""INSERT INTO txn_review_flags
            (doc_base, txn_id, doc_no, rule_code, severity, message_th, details_json)
            VALUES (?,?,?,?,?,?,?)""",
            (doc_base, line.get('id'), line.get('doc_no') or '', fl['rule_code'],
             fl['severity'], fl['message_th'], fl.get('details_json')))
    return ev['real_flag_count']


def _group_by_docbase(rows) -> dict:
    docs = {}
    for r in rows:
        d = dict(r)
        docs.setdefault(d['doc_base'], []).append(d)
    return docs


def default_since() -> str:
    """ISO date ~6 months back — default feed window and dashboard badge window."""
    from datetime import date, timedelta
    return (date.today() - timedelta(days=183)).isoformat()


def get_review_feed(since_date=None, include_medium=False, limit=None,
                    conn=None, db_path=None) -> List[dict]:
    """Return suspicious docs newest-first, each with a 'flags' list attached.

    By default hides docs whose worst issue is only 'medium' (R3 price-deviation /
    R5 promo-mismatch) — those fire heavily on normal negotiated B2B prices and
    bury the actionable 'high' bills. include_medium=True shows every severity.
    """
    with _ConnCtx(conn, db_path) as c:
        sql = (
            "SELECT doc_base, date_iso, customer, customer_code, line_count,"
            " flag_count, max_severity, free_goods_note, scanned_at"
            " FROM txn_review_docs"
        )
        conds: List = []
        params: List = []
        if since_date:
            conds.append("date_iso >= ?")
            params.append(since_date)
        if not include_medium:
            conds.append("max_severity != 'medium'")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY date_iso DESC, doc_base DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        docs = c.execute(sql, params).fetchall()
        out = []
        for d in docs:
            flags = c.execute("""
                SELECT doc_no, rule_code, severity, message_th, details_json
                FROM txn_review_flags WHERE doc_base=?
                ORDER BY CASE severity
                    WHEN 'high'   THEN 3
                    WHEN 'medium' THEN 2
                    ELSE 1
                END DESC, id
            """, (d['doc_base'],)).fetchall()
            row = dict(d)
            row['flags'] = [dict(f) for f in flags]
            out.append(row)
        return out


def suspicious_count(since_date=None, include_medium=False, conn=None, db_path=None) -> int:
    """Count suspicious docs (read-fresh — safe under gunicorn -w 2).

    Mirrors get_review_feed's default: hides 'medium'-only docs unless
    include_medium=True, so the dashboard badge reflects actionable bills.
    """
    with _ConnCtx(conn, db_path) as c:
        try:
            conds: List = []
            params: List = []
            if since_date:
                conds.append("date_iso >= ?")
                params.append(since_date)
            if not include_medium:
                conds.append("max_severity != 'medium'")
            sql = "SELECT COUNT(*) FROM txn_review_docs"
            if conds:
                sql += " WHERE " + " AND ".join(conds)
            r = c.execute(sql, params).fetchone()
            return int(r[0]) if r else 0
        except sqlite3.OperationalError:
            return 0  # table missing (pre-migration) — degrade badge, never 500


def scan_all(conn=None, db_path=None) -> dict:
    """Full re-scan of the whole dataset. Writes suspicious-only rows."""
    with _ConnCtx(conn, db_path) as c:
        rows = c.execute(
            f"SELECT {_SALES_COLS} FROM sales_transactions s"
            " WHERE s.doc_base IS NOT NULL ORDER BY s.doc_base"
        ).fetchall()
        docs = _group_by_docbase(rows)
        c.execute("DELETE FROM txn_review_docs")
        flagged = 0
        flags_total = 0
        for doc_base, lines in docs.items():
            n = _persist_doc(c, doc_base, lines)
            if n is not None:
                flagged += 1
                flags_total += n
        if conn is None:
            c.commit()
        return {'docs_scanned': len(docs), 'docs_flagged': flagged, 'flags_total': flags_total}


def scan_docs(doc_bases, conn=None, db_path=None) -> dict:
    """Incremental re-scan of specific doc_bases (upsert or remove each)."""
    doc_bases = [d for d in (doc_bases or []) if d]
    with _ConnCtx(conn, db_path) as c:
        flagged = 0
        flags_total = 0
        for doc_base in doc_bases:
            rows = c.execute(
                f"SELECT {_SALES_COLS} FROM sales_transactions s"
                " WHERE s.doc_base=? ORDER BY s.doc_no", (doc_base,)
            ).fetchall()
            if not rows:
                c.execute("DELETE FROM txn_review_docs WHERE doc_base=?", (doc_base,))
                continue
            n = _persist_doc(c, doc_base, [dict(r) for r in rows])
            if n is not None:
                flagged += 1
                flags_total += n
        if conn is None:
            c.commit()
        return {'docs_scanned': len(doc_bases), 'docs_flagged': flagged, 'flags_total': flags_total}


def scan_after_import(batch_id: int, conn=None, db_path=None) -> dict:
    """Re-scan documents touched by an import batch (used by the import hooks)."""
    with _ConnCtx(conn, db_path) as c:
        rows = c.execute(
            "SELECT DISTINCT doc_base FROM sales_transactions"
            " WHERE batch_id=? AND doc_base IS NOT NULL", (batch_id,)
        ).fetchall()
        result = scan_docs([r['doc_base'] for r in rows], conn=c)
        if conn is None:
            c.commit()
        return result


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
    doc_base = row.get('doc_base') or ''

    # Determine if this is an SR / return row (skip price rules)
    is_sr = bool(ref_invoice.strip()) or qty <= 0
    is_free = qty > 0 and abs(net) < 0.005       # แถม — earned zero revenue
    skip_price = is_sr or is_free

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
        if not skip_price:
            med, src = _r3_history(conn, doc_base, product_id, unit,
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
    if not skip_price and cost_price > 0:
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
    if not skip_price:
        med, src = _r3_history(conn, doc_base, product_id, unit,
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
    if not skip_price:
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

