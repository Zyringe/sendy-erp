"""Shared cross-cutting helpers used by 2+ models submodules.

Extracted verbatim from models.py (behavior-preserving split, Phase 11) —
see models/__init__.py's module docstring for the overall file-split
rationale. No behavior changes.
"""
from database import get_connection
import re as _re_mod


def _set_price_change_source(conn, source):
    """Tell the product_price_history trigger WHY the next price change on
    `products` happened. The trigger (mig 130) reads the single-row
    price_change_source table in the SAME transaction and stamps
    product_price_history.source with it. UPSERT so it also works on a
    fresh DB where schema.sql created the table but seeded no row. Callers
    reset to None after the UPDATE so unrelated price writes default to NULL."""
    conn.execute(
        "INSERT INTO price_change_source (id, source) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET source = excluded.source",
        (source,)
    )


# audit_log TTL: low-value `transactions` import churn older than this many days
# is pruned by prune_audit_log(), which the import-confirm flow calls once per
# import session. The table is the largest in the DB (a one-time historical BSN
# import churned ~390k rows in a single day); this keeps it self-limiting so it
# can never bloat the volume. This is the single policy lever — change retention
# here only.
AUDIT_LOG_RETENTION_DAYS = 90

# Retention predicate (option B — `transactions`-only). We prune ONLY old
# `transactions` import churn and keep EVERYTHING else forever:
#   - PRUNE old `transactions` INSERT + DELETE → the import delete-then-reinsert
#                                                rebuild (each delete has a
#                                                matching reinsert moments later
#                                                → no forensic value). This pair
#                                                is ~95% of the table.
#   - KEEP  all audit on every OTHER table, forever — including every finance
#           INSERT (a created payout / receipt / invoice), UPDATE (price/cost/
#           note edit), and DELETE (a hand-void) on commission_payouts /
#           received_payments / paid_invoices / products / etc.
#   - KEEP  `transactions` UPDATE (a real ledger-row edit) forever too — only
#           INSERT/DELETE churn on `transactions` is dropped.
# Pruning only `transactions` INSERT+DELETE reclaims the one-time bulk and stops
# weekly-import regrowth without touching any money-table or human-edit trail.
# Trade-off (accepted by Put): a genuine hand-void of a stock-ledger row is also
# a `transactions` DELETE and is indistinguishable from import churn in the
# current schema (trigger writes leave `user` NULL), so it is pruned after the
# window too.
_AUDIT_PRUNE_PREDICATE = (
    "(table_name = 'transactions' AND action IN ('INSERT','DELETE'))"
)


def prune_audit_log(conn=None):
    """Prune old `transactions` import churn from audit_log.

    Option B (see _AUDIT_PRUNE_PREDICATE): prunes ONLY `transactions`
    INSERT+DELETE older than AUDIT_LOG_RETENTION_DAYS; keeps all other audit
    (every finance INSERT/UPDATE/DELETE, all UPDATEs, all non-`transactions`
    DELETEs) FOREVER. Idempotent. Returns the number of rows deleted. The age
    test is strict `<` cutoff, so a row at the boundary day is kept (see below).

    Note on cost: the DELETE is a full table SCAN of audit_log — the only
    created_at index (idx_audit_log_table_time) leads with table_name, so SQLite
    won't seek on created_at alone for a DELETE. We deliberately DON'T add a
    dedicated created_at index: it would cost ~10MB on a volume-constrained DB to
    optimise a bounded delete that runs at most once per import flow (steady-state
    it removes ~one day of rows). Measured sub-second on a prod-size snapshot.
    """
    own = conn is None
    if own:
        conn = get_connection()
    try:
        cur = conn.execute(
            # Cutoff is DATE-ONLY by design: created_at carries a time component
            # ('YYYY-MM-DD HH:MM:SS'), and a same-day timestamp sorts AFTER the
            # bare cutoff date string, so the boundary day is retained. Do NOT
            # "fix" this to datetime(...) — that would drop the boundary day.
            "DELETE FROM audit_log "
            "WHERE created_at < date('now','localtime',?) "
            f"AND {_AUDIT_PRUNE_PREDICATE}",
            (f"-{AUDIT_LOG_RETENTION_DAYS} day",),
        )
        deleted = cur.rowcount
        if own:
            conn.commit()
        return deleted
    finally:
        if own:
            conn.close()


# Noise words to strip before matching (brands, filler marketing words)
_NOISE_WORDS = _re_mod.compile(
    r'\b(sendai|golden\s*lion|ม้าลอดห่วง|สิงห์|คุณภาพดี|อย่างดี|ราคาถูก'
    r'|ของแท้|สินค้าดี|มีให้เลือก|เกรดa|เกรด\s*a|ฟรี|ส่งฟรี|แพ็ค|pack'
    r'|แถมฟรี|โปรโมชั่น|ราคาพิเศษ)\b',
    _re_mod.IGNORECASE
)
_QTY_PREFIX = _re_mod.compile(r'[\(\[【]\s*[\d,./]+\s*[^\)\]】]*[\)\]】]')


def _clean_for_match(text):
    """Strip brand noise & qty-prefixes, return lowercase normalized string."""
    text = _QTY_PREFIX.sub(' ', text or '')
    text = _NOISE_WORDS.sub(' ', text)
    text = text.lower()
    text = _re_mod.sub(r'[()（）【】\[\]\'""]', ' ', text)
    text = _re_mod.sub(r'\s+', ' ', text).strip()
    return text
