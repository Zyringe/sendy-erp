"""Shared cross-cutting helpers used by 2+ models submodules.

Extracted verbatim from models.py (behavior-preserving split, Phase 11) —
see models/__init__.py's module docstring for the overall file-split
rationale. No behavior changes.
"""
from database import get_connection
import re as _re_mod

# The marketplaces the schema accepts — CHECK(platform IN (...)) on
# platform_skus / platform_products / ecommerce_listings (mig 140).
# One definition so a fourth platform is one edit, not a hunt: a list
# hardcoded in a second place is how TikTok listings were silently
# dropped from the product page (get_marketplace_listings_with_history).
PLATFORMS = ('shopee', 'lazada', 'tiktok')


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


# ── source-document provenance (mig 172) ─────────────────────────────────────

SOURCE_DOC_TABLES = ('sales_transactions', 'purchase_transactions')


def declared_update(conn, table, row_id, changes, *, actor,
                    source='manual', reason=None):
    """The only supported way to change a sales/purchase document row.

    mig 172 refuses an UPDATE that touches a meaningful column without saying who
    made it and, for a human edit, why. The reason travels ON THE ROW rather than
    through a side table on purpose: `_set_price_change_source` above uses the
    side-table shape, and it is only safe while the set and the UPDATE share one
    transaction. Set it, commit, then update, and a second connection can stamp
    ITS source onto YOUR row — reproduced in
    projects/express-integration/spike/provenance-2026-08-25/. Both existing
    price callers happen to hold one transaction, so that hazard is latent there,
    but it is not a shape to copy into a new feature.

    `token` is generated per call and must differ from the row's current one. An
    UPDATE that simply omits these columns inherits the PREVIOUS change's reason
    and would read as fully explained, which is worse than no reason at all.
    """
    import uuid
    if table not in SOURCE_DOC_TABLES:
        raise ValueError(f'declared_update is for {SOURCE_DOC_TABLES}, not {table!r}')
    if not (actor or '').strip():
        raise ValueError('actor is required — an unattributed change is the thing '
                         'mig 172 exists to prevent')
    if source == 'manual' and len((reason or '').strip()) < 12:
        raise ValueError('a manual change needs a reason that explains it; '
                         f'got {reason!r}. The DB will refuse it anyway.')
    if not changes:
        raise ValueError('no changes given')
    sets = ', '.join(f'{c} = ?' for c in changes)
    conn.execute(
        f"UPDATE {table} SET {sets}, change_source = ?, change_actor = ?, "
        f"change_reason = ?, change_token = ? WHERE id = ?",
        (*changes.values(), source, actor, reason, uuid.uuid4().hex, row_id))


SOURCE_DOC_FIELD_LABELS = {
    'date_iso': 'วันที่', 'doc_no': 'เลขที่บรรทัด', 'doc_base': 'เลขที่เอกสาร',
    'product_id': 'สินค้าที่ผูก', 'bsn_code': 'รหัส BSN', 'customer': 'ลูกค้า',
    'customer_code': 'รหัสลูกค้า', 'supplier': 'ผู้ขาย', 'supplier_code': 'รหัสผู้ขาย',
    'qty': 'จำนวน', 'unit': 'หน่วย', 'unit_price': 'ราคา/หน่วย', 'vat_type': 'ชนิด VAT',
    'discount': 'ส่วนลด', 'total': 'ยอดรวม', 'net': 'ยอดสุทธิ',
    'ref_invoice': 'อ้างอิงใบกำกับ', 'line_seq': 'ลำดับบรรทัด',
}


def get_source_doc_audit_history(doc_base, table, limit=30):
    """Every recorded change to one document's lines, newest first.

    Unlike `get_customer_audit_history`, this DOES answer "who" and "why":
    mig 172 stamps `change_actor` / `change_source` / `change_reason` onto the
    row, and the trigger copies them into the audit row it writes. A panel that
    only a SQL prompt can read is not provenance anyone has — Put is the sole
    digital operator and does not run SQL.

    Matched on `row_key`, not `row_id`: the importer replaces a changed line
    with DELETE+INSERT, so the numeric id changes at exactly the moment the
    history matters. Sales keys are `<doc_no>|<bsn_code>` where doc_no carries
    the printed `-N` suffix, so a document's lines are matched by prefix — with
    the separator included, otherwise `IV0001` would also collect `IV00010`.
    """
    if table not in SOURCE_DOC_TABLES:
        raise ValueError(f'unknown source table {table!r}')
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT created_at, action, changed_fields, user, change_source, change_reason
                 FROM audit_log
                WHERE table_name = ?
                  AND (row_key LIKE ? ESCAPE '\\' OR row_key LIKE ? ESCAPE '\\')
                ORDER BY id DESC LIMIT ?""",
            (table, _like_prefix(doc_base) + '-%', _like_prefix(doc_base) + '|%', limit)
        ).fetchall()
    finally:
        conn.close()
    import json
    out = []
    for r in rows:
        try:
            raw = json.loads(r['changed_fields'] or '{}')
        except ValueError:
            raw = {}
        changes = []
        for field, val in raw.items():
            old, new = (val + [None, None])[:2] if isinstance(val, list) else (None, val)
            changes.append({'field': field,
                            'label': SOURCE_DOC_FIELD_LABELS.get(field, field),
                            'old': old, 'new': new})
        out.append({'created_at': r['created_at'], 'action': r['action'],
                    'actor': r['user'], 'source': r['change_source'],
                    'reason': r['change_reason'], 'changes': changes})
    return out


def _like_prefix(value):
    """LIKE metacharacters in a document number would silently widen the match."""
    return (value or '').replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


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
    "("
    "  (table_name = 'transactions' AND action IN ('INSERT','DELETE'))"
    "  OR (table_name IN ('sales_transactions','purchase_transactions')"
    "      AND action IN ('INSERT','DELETE')"
    "      AND change_source = 'import')"
    ")"
)
# ⚠ The second clause is only safe because mig 172 made "who wrote this" a
# recorded fact instead of a guess. The comment above this block explains why the
# `transactions` hand-void is pruned along with the churn: it was
# "indistinguishable from import churn". That is exactly the mistake this clause
# must not repeat — so it prunes ONLY rows that positively declare
# change_source='import', and a row with a NULL or 'manual' source is kept
# forever. Every guarded UPDATE is kept regardless of source: those are the
# meaningful changes the whole feature exists to preserve.


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
