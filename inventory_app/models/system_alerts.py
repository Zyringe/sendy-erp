"""Durable operational alerts (mig 149).

WHY THIS EXISTS
    PR2 makes a WACC cost-identity failure raise rather than silently writing a
    wrong cost basis, and the caller surfaces it as a flash message. But Put is
    the only person in the business who acts on digital problems, and the flash
    reaches whoever ran the import — who will not reliably relay it. A
    flash-only failure is effectively SILENT to the one person who can fix it.

OWNERSHIP RULE — read before wiring a new caller
    The rule is "whoever OWNS the failed connection records the alert", exactly
    once:

      * recalculate_product_wacc(pid)            → it owns the connection, so
                                                    IT alerts. Pass `operation`
                                                    so the alert carries your
                                                    business action, and do NOT
                                                    record a second one after
                                                    catching — `operation` is
                                                    part of the dedupe key, so
                                                    two alerts would survive.
      * recalculate_product_wacc(pid, conn)      → YOU own conn, so YOU alert,
                                                    after rolling back and
                                                    closing it.

    (This used to read "recalculate_product_wacc NEVER writes an alert". That
    stopped being true once the owning wrapper gained its own cleanup, and the
    stale wording is what produced duplicate ratio-replay alerts.)

    The owner's sequence is always:

        WACC pre-flight fails
          → raise WaccIdentityError (carrying structured context)
          → caller rolls back + closes its connection
          → caller calls record_wacc_identity_alert(...)   ← fresh connection
          → caller re-raises so the operation cannot report success

    Both naive alternatives are broken. Writing on the SAME connection means
    the alert is rolled back with the failed work — the exact silence this
    exists to prevent. Opening a second connection while the first still holds
    a write transaction risks `database is locked`.

    Alert persistence is BEST-EFFORT and must never mask the original error: if
    the insert fails we log and let the WaccIdentityError propagate. An
    alert-table problem must not replace an actionable money-path error, and
    must never turn a failure into a success.
"""
import json
import sys

from database import get_connection

KIND_WACC_IDENTITY = 'wacc_identity'
KIND_IMPORT_IGNORED_LINES = 'import_ignored_lines'
KIND_SLOW_REQUEST = 'slow_request'
KIND_ORPHAN_BSN_LEDGER = 'orphan_bsn_ledger'
KIND_CONVERSION_ROLE_ERROR = 'conversion_role_error'

# Prod runs `gunicorn --timeout 60` (Procfile / railway.toml). A request that
# exceeds it is SIGABRT'd mid-flight, so it cannot report itself — the warning
# has to come from the slow-but-successful runs BELOW the line. Keep this in
# step with the Procfile if the timeout ever changes.
REQUEST_TIMEOUT_SECONDS = 60
SLOW_REQUEST_WARN_SECONDS = 20        # a third of the budget spent: worth a look
SLOW_REQUEST_CRITICAL_SECONDS = 40    # two thirds: will start 500ing on its own


def _dedupe_key(parts):
    """Incident identity — deliberately NOT the whole context.

    If the key included diagnostics (filename, batch id), retrying the same
    underlying breakage in a NEW batch would change the key and raise a second
    unresolved alert for one incident. Those fields live in context_json.
    """
    return '|'.join('' if p is None else str(p) for p in parts)


def create_system_alert(kind, message, *, dedupe_key, severity='error',
                        context=None, conn=None):
    """Insert one unresolved alert, or leave the existing one alone.

    The partial unique index (kind, dedupe_key) WHERE resolved_at IS NULL means
    a repeat of an already-open incident is a no-op. Once resolved, the same
    incident CAN alert again — a recurrence is news.

    Returns the alert id, or None if an open one already existed.
    """
    own = conn is None
    if own:
        conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO system_alerts"
            " (kind, severity, message, dedupe_key, context_json)"
            " VALUES (?, ?, ?, ?, ?)",
            (kind, severity, message, dedupe_key,
             json.dumps(context, ensure_ascii=False) if context else None),
        )
        if own:
            conn.commit()
        return cur.lastrowid if cur.rowcount else None
    finally:
        if own:
            conn.close()


def record_ignored_import_lines_alert(ignored_detail, *, file_type, filename):
    """A weekly import SKIPPED lines whose รหัส is marked ไม่นำเข้า.

    Not an error — the import succeeded — but it silently drops the REVENUE of
    those lines along with their stock. ฿592 of ค่าขนส่ง (888ค8888) disappeared
    that way over two years before anyone noticed, ฿480 of it billed to named
    B2B customers.

    PR #364 already shows this on the import results page. That page is seen by
    whoever ran the import, which is the precise failure this module exists to
    fix (see the docstring above): the team clicks past it and Put never hears.
    An alert on /alerts stays until someone acknowledges it.

    Dedupe key is the bsn_code ALONE, per _dedupe_key's rule: filename and batch
    are diagnostics and live in context. So a code that keeps being skipped week
    after week holds ONE open alert rather than stacking, and a recurrence after
    acknowledgement is allowed to alert again.

    Best-effort. Callers invoke this AFTER their own connection is closed, and
    must not let an alert failure sink an import that really succeeded.
    """
    if not ignored_detail:
        return []
    ids = []
    for d in ignored_detail:
        msg = ("นำเข้า{ft} ข้าม {lines} บรรทัด รหัส {code} ({name}) "
               "รวม {net:,.2f} บาท — รหัสนี้ตั้งเป็น \"ไม่นำเข้า\" "
               "ยอดขายส่วนนี้จึงไม่ถูกบันทึก. ถ้าเป็นค่าบริการที่เก็บเงินลูกค้าจริง "
               "(เช่น ค่าขนส่ง) ต้องแก้วิธีบันทึกก่อน ไม่ใช่แค่เปิดรหัสกลับ"
               ).format(ft='ขาย' if file_type == 'sales' else 'ซื้อ',
                        lines=d.get('lines'), code=d.get('bsn_code'),
                        name=d.get('name') or '-', net=d.get('net') or 0)
        aid = create_system_alert(
            KIND_IMPORT_IGNORED_LINES, msg,
            dedupe_key=_dedupe_key([d.get('bsn_code')]),
            severity='warning',
            context={'file_type': file_type, 'filename': filename,
                     'ignored_detail': ignored_detail})
        if aid:
            ids.append(aid)
    return ids


def record_orphan_bsn_ledger_alerts(*, file_type, filename):
    """A ledger row that LOOKS like a BSN sync row but no sync will ever manage it.

    THE FAILURE THIS CATCHES
        models/imports.py deletes `note IN (<this file_type's two notes>)` before
        re-posting — exact-match, and right to be (see BSN_SYNC_EXACT_NOTES in
        bsn_sync.py). So a hand-written `BSN …` row is immortal: the genuine row
        beside it is deleted and recreated by every import, the orphan is not,
        and one bill deducts twice from then on.

        Nothing else in the app can see this. The ledger stays internally
        consistent — SUM(quantity_change) still equals stock_levels — so every
        drift check passes. Only a comparison against the SOURCE tables shows the
        gap, and nothing runs that comparison. Found on 2026-08-13 only because
        Put noticed his ถุงหิ้ว count was short; by then it had been wrong for two
        months across three products.

    WHY THE WHOLE LEDGER, NOT JUST THIS IMPORT'S PRODUCTS
        The first version scanned only `affected_pids`, reasoning that the
        re-post is when the damage happens. Both halves of that were wrong,
        measured on the real 26,848-row ledger:

          * It was SLOWER — 6.0ms scoped vs 3.8ms unscoped (best of 5). The
            `product_id IN (…)` list on a full-history import is 1,755
            parameters, which costs more than the scan it was meant to avoid.
          * It missed the case that matters. An ad-hoc script that writes an
            orphan onto a product whose canonical row ALREADY exists does its
            damage immediately, with no import involved. Scoped, that product
            only ever alerts if some later import happens to touch it — which
            is exactly what saved us in June, by luck, not by design.

        So: sweep everything, every import. It is cheap, and "cheap and total"
        beats "cheap and conditional" for a check whose whole purpose is that
        nobody is looking.

    SEVERITY
        error   — a canonical row shares its reference_no, so stock is wrong NOW.
        warning — no collision yet; the row is still immortal and will collide
                  the day that document is re-imported.

    Dedupe key is the ORPHAN ROW'S transactions.id, and that choice is
    load-bearing (Codex review, 2026-08-13). The obvious key, (product_id,
    reference_no), collapses two rogue rows on the same document into ONE alert
    — whose message names a single id to delete. Put deletes that one, marks the
    alert acknowledged, and the second orphan is still there, still double-
    deducting, with nothing open to say so. Each orphan row IS the incident.

    That does not contradict _dedupe_key's "identity, not diagnostics" rule: the
    row id is this incident's identity, and it is stable precisely because
    nothing deletes these rows — the immortality that makes them a bug is what
    makes them a reliable key. Filename/batch stay in context.

    Best-effort, like every caller here: never let a monitoring problem sink an
    import that actually succeeded. Call it AFTER your connection is closed.
    """
    # Local import ON PURPOSE — do not hoist. bsn_sync imports wacc, and wacc
    # imports this module (record_wacc_identity_alert), so a module-level
    # import here closes the cycle and breaks `import models` outright.
    from .bsn_sync import BSN_SYNC_EXACT_NOTES, BSN_SYNC_HISTORY_NOTE_PREFIX
    ids = []
    conn = get_connection()
    try:
        n_ph = ','.join('?' * len(BSN_SYNC_EXACT_NOTES))
        # LEFT JOIN, not JOIN: an inner join would silently DROP an orphan
        # sitting on a product_id with no products row. Zero such rows today,
        # but a check that exists because nobody is looking must not have a
        # path where it quietly returns less than it found.
        rows = conn.execute(
            f"""SELECT t.id, t.product_id, t.reference_no, t.quantity_change,
                       t.note, COALESCE(p.product_name, '(ไม่พบสินค้า)') AS product_name,
                       (SELECT COUNT(*) FROM transactions c
                         WHERE c.product_id = t.product_id
                           AND t.reference_no IS NOT NULL
                           AND c.reference_no = t.reference_no
                           AND c.note IN ({n_ph})) AS canonical_siblings
                  FROM transactions t
                  LEFT JOIN products p ON p.id = t.product_id
                 WHERE (t.note LIKE 'BSN%' OR t.note LIKE 'ประวัติขาย%')
                   AND t.note NOT IN ({n_ph})
                   AND t.note NOT LIKE ?
                 ORDER BY t.product_id, t.id""",
            (*BSN_SYNC_EXACT_NOTES, *BSN_SYNC_EXACT_NOTES,
             BSN_SYNC_HISTORY_NOTE_PREFIX + '%'),
        ).fetchall()
    finally:
        conn.close()

    for r in rows:
        doubled = r['canonical_siblings'] > 0
        if doubled:
            msg = (f"สต็อกถูกตัดซ้ำ: {r['product_name']} (#{r['product_id']}) "
                   f"เอกสาร {r['reference_no']} มีรายการสต็อก 2 แถวสำหรับบิลใบเดียว "
                   f"({r['quantity_change']:+g}) แถวหนึ่งเขียนใส่ไว้เองด้วยโน้ต "
                   f"\"{r['note']}\" ซึ่งการนำเข้ารายสัปดาห์ลบไม่ได้ "
                   f"ยอดคงเหลือจึงน้อยกว่าความจริง "
                   f"แก้โดยลบแถวที่เขียนเอง (id {r['id']}) แล้วสต็อกจะกลับมาเอง")
        else:
            msg = (f"พบรายการสต็อกที่เขียนใส่ไว้เอง: {r['product_name']} "
                   f"(#{r['product_id']}) เอกสาร {r['reference_no']} โน้ต "
                   f"\"{r['note']}\" ระบบนำเข้าไม่รู้จักแถวนี้และลบไม่ได้ "
                   f"ถ้าเอกสารใบนี้ถูกนำเข้าอีกครั้งจะกลายเป็นตัดสต็อกซ้ำ "
                   f"ควรตรวจว่าควรลบทิ้ง (id {r['id']}) หรือไม่")
        aid = create_system_alert(
            KIND_ORPHAN_BSN_LEDGER, msg,
            dedupe_key=_dedupe_key([r['id']]),
            severity='error' if doubled else 'warning',
            context={'transaction_id': r['id'], 'product_id': r['product_id'],
                     'reference_no': r['reference_no'], 'note': r['note'],
                     'quantity_change': r['quantity_change'],
                     'canonical_siblings': r['canonical_siblings'],
                     # The import that RAN the sweep, not necessarily the one
                     # that caused the row — the scan is unscoped (see above).
                     'found_during_file_type': file_type,
                     'found_during_filename': filename})
        if aid:
            ids.append(aid)
    return ids


def record_wacc_identity_alert(exc, *, operation=None, extra=None):
    """Persist a WaccIdentityError on a FRESH connection. Best-effort.

    Call this only AFTER the failed transaction has been rolled back and
    closed. Never raises: a failure here is logged and swallowed so the
    original WaccIdentityError still reaches the user.
    """
    try:
        op = operation or getattr(exc, 'operation', None)
        context = dict(getattr(exc, 'context', lambda: {})() or {})
        context['operation'] = op
        if extra:
            # Diagnostics only — these must NOT enter the dedupe key.
            context.update(extra)
        key = _dedupe_key([
            exc.product_id, exc.reference_no,
            exc.source_bsn_code, exc.source_line_seq, op,
        ])
        msg = (f"คำนวณต้นทุน (WACC) ไม่สำเร็จ: {exc.reason} "
               f"— สินค้า #{exc.product_id}, เอกสาร {exc.reference_no}")
        return create_system_alert(KIND_WACC_IDENTITY, msg, dedupe_key=key,
                                   context=context)
    except Exception as alert_exc:            # noqa: BLE001
        # Never let alerting replace the money-path error it was reporting.
        print(f"[system_alerts] failed to record alert: {alert_exc}",
              file=sys.stderr)
        return None


def record_conversion_role_alert(formula_id, exc):
    """Persist a ConversionRoleError caught inside cross_unit_hazard.

    cross_unit_hazard itself never raises this past its own boundary (see its
    docstring) — a malformed multi-input [แพ็ค] formula (no roles, or roles
    that don't match the contract) is failed closed as a returned
    {'kind': 'configuration_error', ...} hazard instead, because that
    function has 6 call sites spanning WRITE paths (must block unconditionally)
    and READ/LIST paths (a raise there would 500 a whole page over one bad
    row). This module is the one durable place the operator actually sees it.

    Best-effort, on a FRESH connection — cross_unit_hazard is called mid
    read-only query loops on the caller's own connection, and this must never
    interfere with (or be rolled back by) whatever that caller does next.

    Dedupe key is the formula id ALONE. Load-bearing: cross_unit_hazard can
    be called many times for the SAME malformed formula within one request
    (once per pending row that happens to partner with it, from a list-
    builder loop) — without this every one of those calls would try to file
    a fresh open alert for what is really one incident.
    """
    try:
        return create_system_alert(
            KIND_CONVERSION_ROLE_ERROR, str(exc),
            dedupe_key=_dedupe_key([formula_id]),
            severity='error',
            context={'formula_id': formula_id})
    except Exception as alert_exc:            # noqa: BLE001
        print(f"[system_alerts] failed to record conversion-role alert: {alert_exc}",
              file=sys.stderr)
        return None


def record_slow_request_alert(endpoint, method, seconds, *, conn=None):
    """A request spent a dangerous share of the 60s budget. Best-effort.

    Returns the alert id, or None if it was fast enough, already open, or the
    write failed.

    Why the alert cannot come from the failure itself: gunicorn kills the
    worker, so a request that actually times out leaves no code running to
    report anything. Every warning therefore has to be raised by a run that
    still SUCCEEDED — which is precisely the month of 40-55s imports nobody
    noticed before the 2026-08-11 outage (see this file's module docstring for
    why /alerts, and not a flash, is the destination).

    Two levels, two dedupe keys: the failure mode being watched is gradual
    degradation, so "it got worse" must be able to alert while the first
    warning is still open. Within a level the endpoint alone is the key, per
    _dedupe_key's rule — the duration is a diagnostic and lives in context, so
    a weekly import that is slow every week holds one alert, not fifty-two.
    """
    try:
        if seconds is None or seconds < SLOW_REQUEST_WARN_SECONDS:
            return None
        critical = seconds >= SLOW_REQUEST_CRITICAL_SECONDS
        if critical:
            msg = (f"⚠ หน้า {endpoint} ({method}) ใช้เวลา {seconds:.0f} วินาที "
                   f"— ใกล้เพดาน {REQUEST_TIMEOUT_SECONDS} วินาทีของเซิร์ฟเวอร์มาก "
                   f"ถ้าเกินจะขึ้น Internal Server Error ทันที ควรแก้ก่อนที่จะพังจริง")
        else:
            msg = (f"หน้า {endpoint} ({method}) ใช้เวลา {seconds:.0f} วินาที "
                   f"— เพดานของเซิร์ฟเวอร์คือ {REQUEST_TIMEOUT_SECONDS} วินาที "
                   f"ถ้าเกินจะขึ้น Internal Server Error โดยไม่มีข้อความอธิบาย "
                   f"ปกติแปลว่าข้อมูลโตขึ้นจนโค้ดส่วนนี้เริ่มช้า")

        own = conn is None
        if own:
            conn = get_connection()
            # An already-slow request must not wait another 10s on a write lock
            # just to file its own warning — that would push it toward the very
            # timeout this is warning about. This line is the only protection
            # needed: nothing get_connection() does BEFORE it can block, which
            # was measured, not assumed. `sqlite3.connect` takes no lock, and
            # `PRAGMA journal_mode=WAL` under a held write lock RAISES
            # "database is locked" in 0.00s rather than waiting out the 10s
            # connect timeout (and on prod the DB is already WAL, so it is a
            # no-op read). A review flagged the ordering here as able to add
            # ~10s; reproducing it showed it cannot.
            conn.execute("PRAGMA busy_timeout=2000")
        try:
            aid = create_system_alert(
                KIND_SLOW_REQUEST, msg,
                dedupe_key=_dedupe_key([endpoint, 'critical' if critical else 'warn']),
                severity='error' if critical else 'warning',
                context={'endpoint': endpoint, 'method': method,
                         'seconds': round(seconds, 2),
                         'timeout_seconds': REQUEST_TIMEOUT_SECONDS},
                conn=conn)
            if own:
                conn.commit()
            return aid
        finally:
            if own:
                conn.close()
    except Exception as alert_exc:            # noqa: BLE001
        # A monitoring problem must never become the user's problem.
        print(f"[system_alerts] slow-request alert failed: {alert_exc}",
              file=sys.stderr)
        return None


def get_open_system_alerts():
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT id, kind, severity, message, context_json, created_at"
            " FROM system_alerts WHERE resolved_at IS NULL"
            " ORDER BY created_at DESC, id DESC"
        ).fetchall()
    finally:
        conn.close()


def count_open_system_alerts(conn=None):
    own = conn is None
    if own:
        conn = get_connection()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM system_alerts WHERE resolved_at IS NULL"
        ).fetchone()[0]
    finally:
        if own:
            conn.close()


def resolve_system_alert(alert_id, username):
    """Acknowledge an alert. Authorisation is enforced by the route — only
    admin/manager may call this, so the people who fail to relay a failure
    cannot clear it before Put sees it."""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE system_alerts"
            "   SET resolved_at = datetime('now','localtime'), resolved_by = ?"
            " WHERE id = ? AND resolved_at IS NULL",
            (username, alert_id),
        )
        conn.commit()
    finally:
        conn.close()
