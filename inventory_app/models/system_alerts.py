"""Durable operational alerts (mig 149).

WHY THIS EXISTS
    PR2 makes a WACC cost-identity failure raise rather than silently writing a
    wrong cost basis, and the caller surfaces it as a flash message. But Put is
    the only person in the business who acts on digital problems, and the flash
    reaches whoever ran the import — who will not reliably relay it. A
    flash-only failure is effectively SILENT to the one person who can fix it.

OWNERSHIP RULE — read before wiring a new caller
    recalculate_product_wacc NEVER writes an alert. Writing one is the CALLER's
    job, and only AFTER it has rolled back and CLOSED the failed transaction:

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
