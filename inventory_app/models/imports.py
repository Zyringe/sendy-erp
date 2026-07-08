"""Weekly BSN import (sales/purchase) — extracted verbatim from models.py
(behavior-preserving split, Phase 12) — see models/__init__.py's module
docstring for the overall file-split rationale. No behavior changes.

Imports `_resolve_mapping` from `.mapping` and `_sync_bsn_to_stock` from
`.bsn_sync` (both on the brief's expected edge list: imports->{bsn_sync,
mapping, wacc}) plus `recalculate_product_wacc` from `.wacc`.
"""

from database import get_connection
import bsn_units

from .mapping import _resolve_mapping
from .bsn_sync import _sync_bsn_to_stock
from .wacc import recalculate_product_wacc


def _detect_removed_lines(conn, table: str, file_type: str, entries: list) -> list:
    """Stored lines that belong to a doc PRESENT in this file but are NO LONGER
    in the file = deleted at source. Returns the stored rows to reverse.

    Scoped to doc_base present in the file: a partial slice that doesn't mention
    a doc never reverses that doc (so partial imports stay safe). Sales line key
    = (doc_no, bsn_code) — doc_no already carries the printed "-N". Purchase has
    no suffix, so the key adds line_seq. Chunked IN-clause avoids the SQL
    variable limit on a full-history re-import.
    """
    if not entries:
        return []
    if file_type == 'purchase':
        file_keys = {(e['doc_no'], e['product_code_raw'], e.get('line_seq', 1)) for e in entries}
    else:
        file_keys = {(e['doc_no'], e['product_code_raw']) for e in entries}

    def _base(dn):
        return dn.rsplit('-', 1)[0] if '-' in dn else dn

    docs = sorted({_base(e['doc_no']) for e in entries})
    extra = ", line_seq" if file_type == 'purchase' else ""
    found = []
    CHUNK = 800
    for i in range(0, len(docs), CHUNK):
        batch = docs[i:i + CHUNK]
        ph = ",".join("?" * len(batch))
        rows = conn.execute(
            f"SELECT id, doc_no, bsn_code, product_id, product_name_raw{extra} "
            f"FROM {table} WHERE doc_base IN ({ph})", batch
        ).fetchall()
        for r in rows:
            key = ((r['doc_no'], r['bsn_code'], r['line_seq']) if file_type == 'purchase'
                   else (r['doc_no'], r['bsn_code']))
            if key not in file_keys:
                found.append(r)
    return found


def preview_import(entries: list, file_type: str) -> dict:
    """Read-only DRY-RUN of import_weekly — the diff shown on the confirm page.

    Classifies each line against the stored row using the SAME line-identity
    keys and change comparison as import_weekly, but writes NOTHING. Lets the
    user see (and confirm) exactly what a full/partial re-upload will change
    before it touches the ledger.

    Returns counts (new/changed/unchanged/ignored/unmapped) + the per-row diffs
    for changed lines + the unmapped bsn_codes.
    """
    assert file_type in ('sales', 'purchase')
    table = 'sales_transactions' if file_type == 'sales' else 'purchase_transactions'
    conn = get_connection()
    counts = {'new': 0, 'changed': 0, 'unchanged': 0, 'ignored': 0, 'unmapped': 0}
    new_codes = {}
    changes = []
    try:
        for e in entries:
            unit = bsn_units.normalize_unit(e.get('unit'))
            doc_no = e['doc_no']
            line_seq = e.get('line_seq', 1)
            pid, is_ignored, mapped = _resolve_mapping(conn, e['product_code_raw'], unit)
            if is_ignored:
                counts['ignored'] += 1
                continue
            if file_type == 'purchase':
                old = conn.execute(
                    f"SELECT * FROM {table} WHERE doc_no=? AND bsn_code=? AND line_seq=?",
                    (doc_no, e['product_code_raw'], line_seq)).fetchone()
            else:
                old = conn.execute(
                    f"SELECT * FROM {table} WHERE doc_no=? AND bsn_code=?",
                    (doc_no, e['product_code_raw'])).fetchone()
            # Mirror import_weekly: an unmapped code whose stored row is already
            # linked keeps that link (so it is NOT a benign "won't affect stock"
            # unmapped row — it stays mapped and is compared normally).
            if pid is None and old is not None and old['product_id']:
                pid = old['product_id']
                mapped = True
            if not mapped:
                counts['unmapped'] += 1
                if e['product_code_raw']:
                    new_codes[e['product_code_raw']] = e['product_name_raw']
                continue
            if old is None:
                counts['new'] += 1
                continue
            diffs = []
            if abs((old['qty'] or 0) - (e['qty'] or 0)) >= 1e-9:
                diffs.append(('qty', old['qty'], e['qty']))
            if bsn_units.normalize_unit(old['unit'] or '') != (unit or ''):
                diffs.append(('unit', old['unit'], unit))
            if abs((old['unit_price'] or 0) - (e['unit_price'] or 0)) >= 1e-9:
                diffs.append(('unit_price', old['unit_price'], e['unit_price']))
            if abs((old['net'] or 0) - (e['net'] or 0)) >= 1e-9:
                diffs.append(('net', old['net'], e['net']))
            if (old['product_id'] or 0) != (pid or 0):
                diffs.append(('product_id', old['product_id'], pid))
            if not diffs:
                counts['unchanged'] += 1
            else:
                counts['changed'] += 1
                if len(changes) < 500:
                    changes.append({
                        'doc_no': doc_no, 'bsn_code': e['product_code_raw'],
                        'name': e['product_name_raw'], 'diffs': diffs,
                    })
        removed = _detect_removed_lines(conn, table, file_type, entries)
    finally:
        conn.close()
    counts['removed'] = len(removed)
    return {**counts, 'new_codes': sorted(new_codes.keys()),
            'new_code_names': new_codes, 'changes': changes,
            'removed_rows': [{'doc_no': r['doc_no'], 'bsn_code': r['bsn_code'],
                              'name': r['product_name_raw'], 'product_id': r['product_id']}
                             for r in removed[:500]]}


def import_weekly(entries: list, file_type: str, filename: str,
                  apply_removals: bool = True) -> dict:
    """
    Insert sales or purchase entries; skip duplicates by doc_no.

    apply_removals: when True, lines that vanished from the source (for docs the
    file mentions) are reversed (see _detect_removed_lines). The route defaults
    this to the user's explicit opt-in checkbox — a product-code/salesperson
    FILTERED Express export yields partial invoices, and blindly reversing the
    filtered-out lines would mass-delete real stock. Detection always runs so we
    can report the count either way.
    Returns stats dict.
    """
    assert file_type in ('sales', 'purchase')
    table = 'sales_transactions' if file_type == 'sales' else 'purchase_transactions'
    party_col = 'customer' if file_type == 'sales' else 'supplier'
    party_code_col = 'customer_code' if file_type == 'sales' else 'supplier_code'

    conn = get_connection()

    # Log the batch
    cur = conn.execute(
        "INSERT INTO import_log (filename, rows_imported, rows_skipped, notes) VALUES (?,0,0,?)",
        (filename, file_type)
    )
    batch_id = cur.lastrowid

    imported = skipped_dup = overwritten = unchanged = removed = removed_skipped = 0
    new_bsn_codes = {}        # code → name for codes not yet in mapping table
    affected_pids = set()     # products whose ledger must be rebuilt in pass 2

    # Ledger notes this file_type owns. The pass-2 re-sync deletes ONLY these
    # for affected products, so a sales re-import never wipes a product's
    # purchase movements (or vice versa).
    bsn_notes = (('BSN ขาย', 'BSN ขาย-คืน') if file_type == 'sales'
                 else ('BSN ซื้อ', 'BSN ซื้อ-คืน'))

    # ── Pass 1: diff each line vs the stored row; upsert only REAL changes ──
    # Line identity: sales doc_no already carries the printed "-N" line suffix
    # (line-unique), so (doc_no, bsn_code) is enough. Purchase doc_no has no
    # suffix, so line_seq (from the parser, formalised by mig 091) disambiguates
    # multiple lines of one product in one document. Re-uploading an identical
    # line is a true no-op (counted as `unchanged`) → idempotent.
    for e in entries:
        # Auto-normalise the BSN unit acronym → full Thai so it matches the
        # (already-normalised) unit_conversions table → far fewer pending.
        e['unit'] = bsn_units.normalize_unit(e.get('unit'))
        doc_no   = e['doc_no']
        doc_base = doc_no.rsplit('-', 1)[0] if '-' in doc_no else doc_no
        line_seq = e.get('line_seq', 1)

        product_id, is_ignored, mapped = _resolve_mapping(conn, e['product_code_raw'], e['unit'])
        if is_ignored:
            skipped_dup += 1
            continue

        if file_type == 'purchase':
            old = conn.execute(
                f"SELECT * FROM {table} WHERE doc_no=? AND bsn_code=? AND line_seq=?",
                (doc_no, e['product_code_raw'], line_seq)
            ).fetchone()
        else:
            old = conn.execute(
                f"SELECT * FROM {table} WHERE doc_no=? AND bsn_code=?",
                (doc_no, e['product_code_raw'])
            ).fetchone()

        # Preserve an existing product link: if this code is no longer in the
        # mapping but the stored row was already linked to a product, KEEP that
        # product_id instead of nulling it. Nulling would orphan the row's stock
        # movement (_sync only posts non-null product_id) and float stock UP —
        # the opposite of the "won't affect stock" the preview shows for a truly
        # unmapped code.
        if product_id is None and old is not None and old['product_id']:
            product_id = old['product_id']
            mapped = True

        if not mapped and e['product_code_raw']:
            new_bsn_codes[e['product_code_raw']] = e['product_name_raw']

        if old is not None:
            # Normalise the STORED unit before comparing: legacy/rebuild rows
            # were saved with raw acronym units (หล/ตว/กก…) while e['unit'] was
            # normalised at the top of the loop. Without this, re-importing the
            # identical file flags ~95% of purchase rows as "changed" (cosmetic
            # only — same base_qty), churning the ledger needlessly. Comparing
            # normalize(old) == new makes a true re-upload a genuine no-op.
            same = (
                abs((old['qty'] or 0) - (e['qty'] or 0)) < 1e-9
                and bsn_units.normalize_unit(old['unit'] or '') == (e['unit'] or '')
                and abs((old['unit_price'] or 0) - (e['unit_price'] or 0)) < 1e-9
                and abs((old['net'] or 0) - (e['net'] or 0)) < 1e-9
                and (old['product_id'] or 0) == (product_id or 0)
            )
            if same:
                unchanged += 1
                continue
            # Real change → replace the source row; pass 2 rebuilds its ledger.
            if old['product_id']:
                affected_pids.add(old['product_id'])
            conn.execute(f"DELETE FROM {table} WHERE id=?", (old['id'],))
            overwritten += 1

        if file_type == 'purchase':
            conn.execute(f"""
                INSERT INTO {table}
                    (batch_id, date_iso, doc_no, doc_base, product_id, bsn_code,
                     product_name_raw, {party_col}, {party_code_col}, qty, unit,
                     unit_price, vat_type, discount, total, net, line_seq)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                batch_id, e['date_iso'], doc_no, doc_base, product_id,
                e['product_code_raw'], e['product_name_raw'], e['party'],
                e['party_code'], e['qty'], e['unit'], e['unit_price'],
                e['vat_type'], e['discount'], e['total'], e['net'], line_seq
            ))
        else:
            conn.execute(f"""
                INSERT INTO {table}
                    (batch_id, date_iso, doc_no, doc_base, product_id, bsn_code,
                     product_name_raw, {party_col}, {party_code_col}, qty, unit,
                     unit_price, vat_type, discount, total, net)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                batch_id, e['date_iso'], doc_no, doc_base, product_id,
                e['product_code_raw'], e['product_name_raw'], e['party'],
                e['party_code'], e['qty'], e['unit'], e['unit_price'],
                e['vat_type'], e['discount'], e['total'], e['net']
            ))
        imported += 1
        if product_id:
            affected_pids.add(product_id)

    # ── Deletion detection: lines removed from the source within docs that ARE
    # in this file. Scoped to doc_base present in the file, so a PARTIAL slice
    # never reverses docs it doesn't mention. Drop the orphan source row and mark
    # its product affected — Pass 2's rebuild reverses the stock movement.
    # Gated by apply_removals: a FILTERED export (narrowed รหัสสินค้า/พนักงานขาย)
    # produces partial invoices whose filtered-out lines look "deleted" — only
    # reverse when the user explicitly opted in (the route's checkbox).
    to_remove = _detect_removed_lines(conn, table, file_type, entries)
    if apply_removals:
        for r in to_remove:
            if r['product_id']:
                affected_pids.add(r['product_id'])
            conn.execute(f"DELETE FROM {table} WHERE id=?", (r['id'],))
            removed += 1
    else:
        removed_skipped = len(to_remove)

    # ── Pass 2: rebuild the ledger for affected products ONCE ──
    # Delete only this file_type's BSN movements for the affected products (the
    # mig-080 triggers auto-reconcile stock_levels — no manual stock surgery),
    # reset their source rows, then a SINGLE _sync_bsn_to_stock re-posts. An
    # all-unchanged re-import touches 0 products → genuine no-op.
    if affected_pids:
        pids = list(affected_pids)
        p_ph = ",".join("?" * len(pids))
        n_ph = ",".join("?" * len(bsn_notes))
        conn.execute(
            f"DELETE FROM transactions WHERE product_id IN ({p_ph}) "
            f"AND note IN ({n_ph})",
            pids + list(bsn_notes)
        )
        conn.execute(
            f"UPDATE {table} SET synced_to_stock=0 WHERE product_id IN ({p_ph})",
            pids
        )
        _sync_bsn_to_stock(conn, table, file_type)

    # Register new BSN codes in mapping table (unmapped)
    for code, name in new_bsn_codes.items():
        conn.execute("""
            INSERT OR IGNORE INTO product_code_mapping (bsn_code, bsn_name)
            VALUES (?, ?)
        """, (code, name))

    # Update batch log
    conn.execute(
        "UPDATE import_log SET rows_imported=?, rows_skipped=? WHERE id=?",
        (imported, skipped_dup, batch_id)
    )
    conn.commit()

    # WACC: recalculate for the products whose ledger actually changed.
    if affected_pids:
        for pid in affected_pids:
            recalculate_product_wacc(pid, conn)
        conn.commit()

    conn.close()

    return {
        'imported': imported,
        'skipped_dup': skipped_dup,
        'overwritten': overwritten,
        'unchanged': unchanged,
        'removed': removed,
        'removed_skipped': removed_skipped,
        'new_unmapped': len(new_bsn_codes),
        'affected_products': len(affected_pids),
        'batch_id': batch_id,
    }


def get_recent_imports(limit=5):
    conn = get_connection()
    rows = conn.execute(
        "SELECT filename, rows_imported, rows_skipped, imported_at, notes "
        "FROM import_log ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return rows


def get_express_dbf_freshness(stale_after_hours=26):
    """Last full Express-DBF-direct import + staleness, for the dashboard
    freshness badge (projects/express-integration/plan.md Phase 2).

    import_router.commit_express_dbf() always runs payments_out and
    credit_notes_ap through import_express.run_import_records(), which
    INSERTs an express_import_log row (source_filename='express_dbf')
    unconditionally at the start of each call — even when that particular
    batch has zero of those records. So MAX(imported_at) filtered to
    source_filename='express_dbf' is a reliable "last full DBF commit"
    marker, not just a payments/credit-note-specific one. (Column is
    `imported_at`, not `created_at`.)
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT MAX(imported_at) AS last_at, "
        "(julianday('now','localtime') - julianday(MAX(imported_at))) * 24.0 AS hours_stale "
        "FROM express_import_log WHERE source_filename = 'express_dbf'"
    ).fetchone()
    conn.close()
    last_at = row['last_at']
    hours_stale = row['hours_stale']
    return {
        'last_at': last_at,
        'hours_stale': hours_stale,
        'is_stale': last_at is None or hours_stale > stale_after_hours,
    }
