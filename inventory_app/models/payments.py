"""Payments / AR helpers — extracted verbatim from models.py
(behavior-preserving split, Phase 12) — see models/__init__.py's module
docstring for the overall file-split rationale. No behavior changes.
"""

from database import get_connection
from cashflow import BSN_AR_PREDICATE


def parse_payment_csv(filepath):
    """Parse การรับชำระหนี้ CSV (cp874). Returns list of RE dicts with iv_list.

    iv_list shape: list of dicts, each
        {'iv_no': str, 'amount': float, 'kind': 'IV' | 'SR'}.

    A receipt may apply a credit note against the invoices it settles.
    Express emits these as an "SR…" sub-row carrying a NEGATIVE amount
    (optional leading '-'), e.g.
        "                             SR6900009    27/03/69         -2293.20"
    These SR(-) lines ARE captured (kind='SR', amount negative). Dropping
    them — as the old IV-only regex did — made the receipt look like it
    applied only the +IV total, so the invoice read as fantasy "overpaid".

    total (per RE record): Σ of the POSITIVE IV amounts ONLY (kind=='IV').
    SR(-) lines are netting links, NOT extra collected cash, and the
    header total / existing total-based tests are Σ IV(+). The netting of
    SR(-) against the invoice happens downstream in payments_alloc via the
    persisted receipt links, not in this header total.
    """
    import re as _re
    records = []
    current = None
    with open(filepath, encoding='cp874') as f:
        for line in f:
            text = line.strip().strip('"').replace('\xa0', ' ')
            if not text:
                continue
            # RE header row.  Salesperson can be "06" (digits) or "06-L" (with branch
            # suffix), so allow non-space chars rather than requiring \w+.
            m = _re.match(r'^(\d{2}/\d{2}/\d{2})\s+(\*?RE\S+)\s+(.+?)\s{2,}(\S+)\s', text)
            if m:
                if current:
                    current['total'] = sum(
                        iv['amount'] for iv in current['iv_list']
                        if iv['kind'] == 'IV')
                    records.append(current)
                d, re_no, customer, sp = m.groups()
                cancelled = re_no.startswith('*')
                re_no_clean = re_no.lstrip('*')
                dd, mm, yy = d.split('/')
                year_ce = int(yy) + 2500 - 543
                date_iso = f"{year_ce}-{mm}-{dd}"
                current = {
                    're_no': re_no_clean,
                    'cancelled': cancelled,
                    'date_iso': date_iso,
                    'customer': customer.strip(),
                    'salesperson': sp.strip(),
                    'iv_list': []
                }
                continue
            # Sub-row — IV (settled invoice, positive) or SR (credit-note
            # receipt link, NEGATIVE with an optional leading '-').
            # group1 = doc no (IV…/SR…), group2 = optional '-', group3 =
            # amount which may carry thousands commas (e.g. "1,234.56").
            m2 = _re.match(
                r'\s*((?:IV|SR)\S+)\s+\d{2}/\d{2}/\d{2}\s+(-?)([\d,]+\.\d{2})',
                text)
            if m2 and current:
                doc_no = m2.group(1)
                sign = -1.0 if m2.group(2) == '-' else 1.0
                amount = sign * float(m2.group(3).replace(',', ''))
                # Fail-loud on unknown prefixes. The regex above limits matches
                # to IV|SR, so the else branch is dead code today — but if the
                # regex is ever widened (new BSN doc kind), this catches the
                # parser/CHECK-constraint coordination gap before paid_invoices
                # receives a misclassified row.
                if doc_no.startswith('SR'):
                    kind = 'SR'
                elif doc_no.startswith('IV'):
                    kind = 'IV'
                else:
                    raise ValueError(
                        f"parse_payment_csv: unexpected doc prefix in {doc_no!r} "
                        f"(supported: IV, SR). Update the regex AND the "
                        f"paid_invoices.doc_kind CHECK constraint together."
                    )
                current['iv_list'].append(
                    {'iv_no': doc_no, 'amount': amount, 'kind': kind})
    if current:
        current['total'] = sum(
            iv['amount'] for iv in current['iv_list']
            if iv['kind'] == 'IV')
        records.append(current)
    return records


def import_payments(filepath):
    """Import payment CSV into received_payments + paid_invoices tables.

    Uses idempotent upserts (ON CONFLICT DO UPDATE) so re-importing the same
    file any number of times leaves row counts and every amount/total identical
    after the first successful run.

    Re_id resolution
    ----------------
    We use ``INSERT ... ON CONFLICT(re_no) DO UPDATE ... RETURNING id`` (SQLite
    ≥3.35, available here as sqlite 3.35+).  RETURNING delivers the canonical
    row id for BOTH the INSERT path and the UPDATE path in a single statement,
    removing all dependence on ``cur.lastrowid`` which is unreliable for the
    conflict/UPDATE path (it retains a stale value from the most recent plain
    INSERT on that connection, not 0 as the old guard assumed).

    Per-record transactional boundary
    ----------------------------------
    Each RE record is wrapped in a SAVEPOINT so that a single bad record
    (malformed amount, FK violation, etc.) is fully rolled back without
    discarding the good work that came before.  After the loop a single
    ``conn.commit()`` flushes all survivors.

    Returns dict
    ------------
      imported  — brand-new RE rows (did not exist before this run)
      updated   — existing RE rows refreshed (upsert took the UPDATE path)
      skipped   — RE records that raised an exception (isolated, rolled back)
      total     — total RE records parsed from the file
      errors    — list of up to 5 distinct exception reprs from skipped records
                  (empty list when all records imported cleanly)

    Invariant: ``imported + updated + skipped == total`` always holds.

    Note: legacy rows imported before migration 058 have amount/total = NULL;
    they are updated to carry real amounts the first time that RE is re-imported.
    """
    records = parse_payment_csv(filepath)
    conn = get_connection()
    imported = 0
    updated = 0
    skipped = 0
    errors = []          # up to 5 distinct repr strings

    for i, r in enumerate(records):
        sp = f"sp_re_{i}"
        try:
            conn.execute(f"SAVEPOINT {sp}")

            # --- Classify as new vs existing BEFORE the upsert ---
            # (rowcount after an UPSERT is always 1 in SQLite regardless of path,
            # so we must pre-check existence to distinguish insert from update.)
            existing = conn.execute(
                "SELECT 1 FROM received_payments WHERE re_no=?", (r['re_no'],)
            ).fetchone()
            is_new = existing is None

            # --- Authoritative upsert with RETURNING id ---
            # RETURNING delivers the real id for BOTH the INSERT and the UPDATE
            # conflict path — no dependence on cur.lastrowid.
            row = conn.execute(
                """INSERT INTO received_payments
                       (re_no, date_iso, customer, salesperson, cancelled, total)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(re_no) DO UPDATE SET
                       date_iso=excluded.date_iso,
                       customer=excluded.customer,
                       salesperson=excluded.salesperson,
                       cancelled=excluded.cancelled,
                       total=excluded.total
                   RETURNING id""",
                (r['re_no'], r['date_iso'], r['customer'], r['salesperson'],
                 1 if r['cancelled'] else 0, r.get('total'))
            ).fetchone()

            assert row is not None, f"RETURNING id returned nothing for re_no={r['re_no']!r}"
            re_id = row[0]
            assert re_id, f"re_id resolved to falsy value for re_no={r['re_no']!r}"

            for iv in r['iv_list']:
                conn.execute(
                    """INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount)
                       VALUES (?,?,?,?)
                       ON CONFLICT(re_id, doc_no) DO UPDATE SET
                           doc_kind=excluded.doc_kind,
                           amount=excluded.amount""",
                    (re_id, iv['iv_no'], iv['kind'], iv['amount'])
                )

            conn.execute(f"RELEASE SAVEPOINT {sp}")

            if is_new:
                imported += 1
            else:
                updated += 1

        except Exception as exc:
            conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            conn.execute(f"RELEASE SAVEPOINT {sp}")
            skipped += 1
            if len(errors) < 5:
                errors.append(repr(exc))

    conn.commit()
    conn.close()
    return {
        'imported': imported,
        'updated': updated,
        'skipped': skipped,
        'total': len(records),
        'errors': errors,
    }


def get_payment_status(status='all', search='', date_from='', date_to='', page=1, per_page=50):
    """Get IV invoices with payment status.
    Uses pre-computed doc_base column + index for performance.
    """
    conn = get_connection()

    conds = ["st.doc_base IS NOT NULL", "st.doc_base NOT LIKE 'SR%'", "st.doc_base NOT LIKE 'HS%'"]
    params = []

    if search:
        conds.append("(st.doc_base LIKE ? OR st.customer LIKE ?)")
        params += [f'%{search}%', f'%{search}%']
    if date_from:
        conds.append("st.date_iso >= ?"); params.append(date_from)
    if date_to:
        conds.append("st.date_iso <= ?"); params.append(date_to)

    paid_filter = ''
    if status == 'paid':
        paid_filter = 'HAVING is_paid = 1'
    elif status == 'unpaid':
        paid_filter = 'HAVING is_paid = 0 AND total_net > 0'
    else:
        paid_filter = 'HAVING total_net > 0'

    where = ' AND '.join(conds)

    sql = f"""
        SELECT
            st.doc_base,
            MIN(st.date_iso) AS bill_date,
            st.customer,
            SUM(CASE WHEN st.vat_type = 2 THEN st.net * 1.07 ELSE st.net END) AS total_net,
            MAX(CASE WHEN pi.doc_no IS NOT NULL THEN 1 ELSE 0 END) AS is_paid,
            MAX(rp.date_iso) AS paid_date,
            MAX(rp.re_no) AS re_no
        FROM sales_transactions st
        LEFT JOIN paid_invoices pi ON pi.doc_no = st.doc_base
        LEFT JOIN received_payments rp ON rp.id = pi.re_id AND rp.cancelled = 0
        WHERE {where}
        GROUP BY st.doc_base
        {paid_filter}
        ORDER BY bill_date DESC
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, params + [per_page, (page - 1) * per_page]).fetchall()

    count_sql = f"""
        SELECT COUNT(*) FROM (
            SELECT st.doc_base,
                MAX(CASE WHEN pi.doc_no IS NOT NULL THEN 1 ELSE 0 END) AS is_paid,
                SUM(CASE WHEN st.vat_type = 2 THEN st.net * 1.07 ELSE st.net END) AS total_net
            FROM sales_transactions st
            LEFT JOIN paid_invoices pi ON pi.doc_no = st.doc_base
            LEFT JOIN received_payments rp ON rp.id = pi.re_id AND rp.cancelled = 0
            WHERE {where}
            GROUP BY st.doc_base
            {paid_filter}
        )
    """
    total = conn.execute(count_sql, params).fetchone()[0]
    conn.close()
    return rows, total


def get_payment_summary():
    """Quick stats for payment status page."""
    conn = get_connection()
    row = conn.execute("""
        SELECT
            COUNT(DISTINCT st.doc_base) AS total_bills,
            SUM(CASE WHEN pi.doc_no IS NOT NULL THEN 1 ELSE 0 END) AS paid_count,
            SUM(CASE WHEN pi.doc_no IS NULL THEN 1 ELSE 0 END) AS unpaid_count,
            SUM(CASE WHEN pi.doc_no IS NOT NULL THEN st.net ELSE 0 END) AS paid_amount,
            SUM(CASE WHEN pi.doc_no IS NULL THEN st.net ELSE 0 END) AS unpaid_amount
        FROM (
            SELECT doc_base,
                   SUM(CASE WHEN vat_type = 2 THEN net * 1.07 ELSE net END) AS net
            FROM sales_transactions
            WHERE doc_base IS NOT NULL AND doc_base NOT LIKE 'SR%' AND doc_base NOT LIKE 'HS%'
            GROUP BY doc_base
            HAVING SUM(CASE WHEN vat_type = 2 THEN net * 1.07 ELSE net END) > 0
        ) st
        LEFT JOIN paid_invoices pi ON pi.doc_no = st.doc_base
        LEFT JOIN received_payments rp ON rp.id = pi.re_id AND rp.cancelled = 0
    """).fetchone()
    conn.close()
    return row


def get_customer_debt_summary(search=''):
    """สรุปหนี้ค้างชำระรายลูกค้า เรียงตามยอดค้างมากสุด.

    Sourced from express_ar_outstanding (latest snapshot) — same data as
    /express/ar — filtered to doc_date_iso >= 2024-01-01 (Sendy import
    window). Per Put 2026-05-02: BSN sync และ Express ใช้แหล่งเดียวกัน,
    ใช้ Express snapshot เป็น source of truth, จำกัดช่วงเดียวกับ Sendy
    import (2024-01-01 ถึงปัจจุบัน) เพื่อไม่นับ legacy debt ก่อนยุคนั้น.
    """
    conn = get_connection()
    cond = ""
    params = []
    if search:
        cond = "AND (ao.customer_name LIKE ? OR ao.customer_code LIKE ?)"
        params += [f'%{search}%', f'%{search}%']

    rows = conn.execute(f"""
        SELECT
            COALESCE(c.name, ao.customer_name) AS customer,
            ao.customer_code,
            COUNT(*)                           AS unpaid_bills,
            ROUND(SUM(ao.outstanding_amount), 2) AS outstanding_amount
        FROM express_ar_outstanding ao
        LEFT JOIN customers c ON c.code = ao.customer_code
        WHERE ao.entity = 'BSN'
          AND ao.snapshot_date_iso = (SELECT MAX(snapshot_date_iso) FROM express_ar_outstanding WHERE entity = 'BSN')
          AND {BSN_AR_PREDICATE}
          {cond}
        GROUP BY ao.customer_code
        HAVING outstanding_amount > 0
        ORDER BY outstanding_amount DESC
    """, params).fetchall()

    conn.close()
    return rows


def get_ar_reconciliation():
    """Per-customer reconcile: Express snapshot (get_customer_debt_summary) vs
    Sendy ledger unpaid (sales_transactions minus paid_invoices/received_payments).
    Read-only. Snapshot is the canonical AR; ledger is the live cross-check.

    Returns dict with keys:
      rows: list of {customer_code, customer_name, snapshot_amount, ledger_amount,
                     diff, status}  sorted by abs(diff) desc.
      snapshot_total, ledger_total, diff_total (floats).
    """
    # Snapshot side (canonical) — reuse the existing helper so totals match exactly.
    snap_rows = get_customer_debt_summary()
    snap = {r['customer_code']: {'name': r['customer'],
                                 'amount': r['outstanding_amount'] or 0.0}
            for r in snap_rows}

    # Ledger side — unpaid invoice balance per customer, mirroring get_payment_summary().
    # get_payment_summary groups by doc_base, sums vat-aware net, then marks unpaid
    # where paid_invoices has no matching row (rp.cancelled=0 check for received_payments).
    conn = get_connection()
    led_rows = conn.execute("""
        SELECT st.customer_code AS code,
               MAX(st.customer)  AS name,
               ROUND(SUM(bill_net), 2) AS unpaid
          FROM (
              SELECT customer_code, customer, doc_base,
                     SUM(CASE WHEN vat_type = 2 THEN net * 1.07 ELSE net END) AS bill_net
                FROM sales_transactions
               WHERE doc_base IS NOT NULL
                 AND doc_base NOT LIKE 'SR%'
                 AND doc_base NOT LIKE 'HS%'
               GROUP BY doc_base
              HAVING bill_net > 0
          ) st
          LEFT JOIN paid_invoices pi ON pi.doc_no = st.doc_base
          LEFT JOIN received_payments rp ON rp.id = pi.re_id AND rp.cancelled = 0
         WHERE pi.doc_no IS NULL
         GROUP BY st.customer_code
    """).fetchall()
    conn.close()

    led = {r['code']: {'name': r['name'], 'amount': r['unpaid'] or 0.0}
           for r in led_rows if r['code']}

    rows = []
    for code in set(snap) | set(led):
        s = snap.get(code, {}).get('amount', 0.0)
        l = led.get(code, {}).get('amount', 0.0)
        name = (snap.get(code, {}).get('name')
                or led.get(code, {}).get('name')
                or code)
        if code not in snap:
            status = 'ledger_only'
        elif code not in led:
            status = 'snapshot_only'
        elif abs(l - s) < 0.01:
            status = 'match'
        else:
            status = 'diff'
        rows.append({'customer_code': code, 'customer_name': name,
                     'snapshot_amount': round(s, 2), 'ledger_amount': round(l, 2),
                     'diff': round(l - s, 2), 'status': status})
    rows.sort(key=lambda r: abs(r['diff']), reverse=True)

    snap_total = round(sum(v['amount'] for v in snap.values()), 2)
    led_total = round(sum(v['amount'] for v in led.values()), 2)
    return {'rows': rows, 'snapshot_total': snap_total, 'ledger_total': led_total,
            'diff_total': round(led_total - snap_total, 2)}


def find_payment_candidates(amount, tolerance_pct=5):
    """คาดคะเนลูกค้าที่น่าจะโอนเงิน amount บาท
    ลองทุก subset ของบิลที่ค้างชำระของแต่ละลูกค้า
    คืนค่า list of dict เรียงตาม abs(diff) ASC
    """
    from itertools import combinations

    conn = get_connection()
    # ดึงบิลค้างชำระทั้งหมดแยกรายบิล (รวม vat_type ที่พบมากที่สุดในบิล)
    bill_rows = conn.execute("""
        SELECT st.customer, st.customer_code, st.doc_base,
               SUM(CASE WHEN st.vat_type=2 THEN st.net*1.07 ELSE st.net END) AS bill_net,
               MAX(st.vat_type) AS vat_type
        FROM sales_transactions st
        LEFT JOIN paid_invoices pi ON pi.doc_no = st.doc_base
        WHERE st.doc_base IS NOT NULL
          AND st.doc_base NOT LIKE 'SR%' AND st.doc_base NOT LIKE 'HS%'
          AND pi.doc_no IS NULL
        GROUP BY st.customer, st.customer_code, st.doc_base
        HAVING bill_net > 0
        ORDER BY st.customer, st.doc_base
    """).fetchall()
    conn.close()

    # จัดกลุ่มตามลูกค้า
    customers = {}
    for r in bill_rows:
        key = r['customer']
        if key not in customers:
            customers[key] = {'customer_code': r['customer_code'], 'bills': []}
        customers[key]['bills'].append({'doc_base': r['doc_base'], 'net': r['bill_net'], 'vat_type': r['vat_type']})

    tolerance = max(amount * tolerance_pct / 100, 200)
    results = []

    for customer, data in customers.items():
        bills = data['bills']
        if len(bills) > 15:
            # ถ้าบิลเยอะเกินไป ตรวจแค่ยอดรวมทั้งหมด
            total = sum(b['net'] for b in bills)
            if abs(total - amount) <= tolerance:
                results.append({
                    'customer': customer,
                    'customer_code': data['customer_code'],
                    'matched_bills': [{'doc_base': b['doc_base'], 'vat_type': b['vat_type']} for b in bills],
                    'matched_sum': total,
                    'diff': total - amount,
                    'total_unpaid_bills': len(bills),
                    'total_outstanding': total,
                })
            continue

        best_per_customer = []
        for r in range(1, len(bills) + 1):
            for combo in combinations(bills, r):
                combo_sum = sum(b['net'] for b in combo)
                diff = combo_sum - amount
                if abs(diff) <= tolerance:
                    best_per_customer.append({
                        'customer': customer,
                        'customer_code': data['customer_code'],
                        'matched_bills': [{'doc_base': b['doc_base'], 'vat_type': b['vat_type']} for b in combo],
                        'matched_sum': combo_sum,
                        'diff': diff,
                        'total_unpaid_bills': len(bills),
                        'total_outstanding': sum(b['net'] for b in bills),
                    })

        # เก็บแค่ 3 combo ที่ใกล้ที่สุดต่อลูกค้า
        best_per_customer.sort(key=lambda x: abs(x['diff']))
        results.extend(best_per_customer[:3])

    results.sort(key=lambda x: abs(x['diff']))
    return results[:20]


def get_customer_unpaid_bills(customer_name):
    """รายการบิลค้างชำระของลูกค้าคนนี้.

    Sourced from express_ar_outstanding (latest snapshot, doc_date >= 2024).
    Customer matched first by customers.name → customer_code, then falls
    back to ao.customer_name LIKE for legacy/typo cases.
    """
    conn = get_connection()
    rows = conn.execute("""
        SELECT
            ao.doc_no                    AS doc_base,
            ao.doc_date_iso              AS bill_date,
            COALESCE(c.name, ao.customer_name) AS customer,
            ao.customer_code,
            NULL                         AS vat_type,    -- placeholder; Express totals are already as-billed
            ao.outstanding_amount        AS total_net,
            ao.bill_amount,
            ao.paid_amount,
            ao.is_anomalous,
            ao.has_warning
        FROM express_ar_outstanding ao
        LEFT JOIN customers c ON c.code = ao.customer_code
        WHERE ao.entity = 'BSN'
          AND ao.snapshot_date_iso = (SELECT MAX(snapshot_date_iso) FROM express_ar_outstanding WHERE entity = 'BSN')
          AND ao.doc_date_iso >= '2024-01-01'
          AND (
                COALESCE(c.name, '') = ?
             OR ao.customer_name = ?
          )
          AND ao.outstanding_amount > 0
        ORDER BY ao.doc_date_iso DESC
    """, [customer_name, customer_name]).fetchall()
    conn.close()
    return rows
