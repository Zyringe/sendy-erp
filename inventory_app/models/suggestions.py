"""Pending product-suggestion review/approval helpers — extracted verbatim
from models.py (behavior-preserving split, Phase 12) — see
models/__init__.py's module docstring for the overall file-split rationale.
No behavior changes.

`approve_pending_suggestion` calls `create_structured_product` (`.products`)
and `resolve_pending_mappings` (`.mapping`) bare — both on the brief's
expected suggestions->{mapping, products} edge list. The `resolve_pending_mappings`
binding here is load-bearing: a test patches `models.suggestions.resolve_pending_mappings`
(not `models.mapping...`) to intercept this exact call — see the Phase 12
report's monkeypatch-retarget section.
"""

import sqlite3

import sku_code_utils
from database import get_connection

from .products import create_structured_product
from .mapping import resolve_pending_mappings
from .bsn_sync import cross_unit_hazard


class DuplicateSkuError(Exception):
    """Raised by `create_now` when the proposed sku_code already belongs to
    another product (active or inactive — sku_code is unique regardless of
    is_active). `duplicate_of` is a dict of the colliding row for the
    caller/client to show and confirm past."""
    def __init__(self, duplicate_of: dict):
        self.duplicate_of = duplicate_of
        super().__init__(
            f"sku_code collision with product #{duplicate_of['id']}"
        )


class SuggestionAlreadyStagedError(Exception):
    """Raised by `create_now` when another request already holds
    `pending_product_suggestions.bsn_code`'s UNIQUE slot for this code —
    either a genuinely concurrent create_now (the case this guards) or an
    unrelated pre-existing stage. Either way, create_now must never silently
    clobber it (see `create_now`'s docstring)."""
    def __init__(self, bsn_code: str, existing_status: str):
        self.bsn_code = bsn_code
        self.existing_status = existing_status
        super().__init__(
            f"{bsn_code} already has a {existing_status} suggestion"
        )


def count_pending_suggestions() -> int:
    conn = get_connection()
    n = conn.execute(
        "SELECT COUNT(*) FROM pending_product_suggestions WHERE status='pending'"
    ).fetchone()[0]
    conn.close()
    return n


def get_pending_suggestions():
    """List of suggestions awaiting manager/admin review, oldest first."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT pps.*, u.display_name AS suggested_by_name, b.name AS brand_name
          FROM pending_product_suggestions pps
          LEFT JOIN users u ON u.id = pps.suggested_by_user_id
          LEFT JOIN brands b ON b.id = pps.brand_id
         WHERE pps.status = 'pending'
         ORDER BY pps.created_at ASC
    """).fetchall()
    conn.close()
    return rows


def get_pending_suggestion(suggestion_id: int):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM pending_product_suggestions WHERE id = ?",
        (suggestion_id,),
    ).fetchone()
    conn.close()
    return row


def save_pending_suggestion(data: dict, user_id: int, *, upsert: bool = True) -> int:
    """Insert a new staged SKU suggestion. Returns new suggestion id.
    UPSERT on bsn_code by default, so re-submitting overwrites the prior
    staged version (the regular "ส่งให้ manager review" flow).
    `data` may include free-text overrides (brand_other_name, color_code_other,
    packaging_other) and unit-conversion hints (bsn_unit, unit_conversion_ratio).

    `upsert=False` (used by `create_now`'s claim step) drops the ON CONFLICT
    clause: a bsn_code that already holds a row (pending OR approved) raises
    `sqlite3.IntegrityError` instead of silently overwriting it. That's the
    difference between "re-submitting a stage overwrites the prior draft"
    (fine, single human iterating) and "two concurrent create_now calls for
    the same bsn_code" (must not let one clobber the other's payload before
    it gets approved) — see `create_now`.
    """
    # Default any missing extras to None so SQL params bind cleanly
    for k in ('brand_other_name', 'color_code_other', 'packaging_other',
              'bsn_unit', 'unit_conversion_ratio',
              'sub_category', 'sub_category_short_code', 'category_id',
              'clone_source_pid'):
        data.setdefault(k, None)
    conflict_clause = """
        ON CONFLICT(bsn_code) DO UPDATE SET
            bsn_name = excluded.bsn_name,
            suggested_name = excluded.suggested_name,
            category = excluded.category,
            series = excluded.series,
            brand_id = excluded.brand_id,
            model = excluded.model,
            size = excluded.size,
            color_th = excluded.color_th,
            color_code = excluded.color_code,
            packaging = excluded.packaging,
            condition = excluded.condition,
            pack_variant = excluded.pack_variant,
            suggested_cost = excluded.suggested_cost,
            suggested_unit_type = excluded.suggested_unit_type,
            units_per_carton = excluded.units_per_carton,
            units_per_box = excluded.units_per_box,
            brand_other_name = excluded.brand_other_name,
            color_code_other = excluded.color_code_other,
            packaging_other = excluded.packaging_other,
            bsn_unit = excluded.bsn_unit,
            unit_conversion_ratio = excluded.unit_conversion_ratio,
            sub_category = excluded.sub_category,
            sub_category_short_code = excluded.sub_category_short_code,
            category_id = excluded.category_id,
            clone_source_pid = excluded.clone_source_pid,
            suggested_by_user_id = excluded.suggested_by_user_id,
            status = 'pending'
    """ if upsert else ""
    conn = get_connection()
    try:
        cur = conn.execute(f"""
            INSERT INTO pending_product_suggestions
              (bsn_code, bsn_name, suggested_name, category, series, brand_id,
               model, size, color_th, color_code, packaging, condition, pack_variant,
               suggested_cost, suggested_unit_type, units_per_carton, units_per_box,
               brand_other_name, color_code_other, packaging_other,
               bsn_unit, unit_conversion_ratio,
               sub_category, sub_category_short_code, category_id, clone_source_pid,
               suggested_by_user_id, status)
            VALUES
              (:bsn_code, :bsn_name, :suggested_name, :category, :series, :brand_id,
               :model, :size, :color_th, :color_code, :packaging, :condition, :pack_variant,
               :suggested_cost, :suggested_unit_type, :units_per_carton, :units_per_box,
               :brand_other_name, :color_code_other, :packaging_other,
               :bsn_unit, :unit_conversion_ratio,
               :sub_category, :sub_category_short_code, :category_id, :clone_source_pid,
               :suggested_by_user_id, 'pending'){conflict_clause}
        """, {**data, 'suggested_by_user_id': user_id})
        conn.commit()
        sid = cur.lastrowid or conn.execute(
            "SELECT id FROM pending_product_suggestions WHERE bsn_code = ?",
            (data['bsn_code'],)
        ).fetchone()[0]
    except sqlite3.IntegrityError:
        conn.rollback()
        raise
    finally:
        conn.close()
    return sid


# Keys the Tab-2 approve form submits as `<value> || null`, so an explicit null
# is a deliberate CLEAR rather than "not edited". See the merge in
# approve_pending_suggestion.
_CLEARABLE_EDIT_KEYS = frozenset({
    'category_id', 'brand_id', 'color_code', 'packaging',
})


def approve_pending_suggestion(suggestion_id: int, edits: dict, reviewer_id: int) -> int:
    """Apply manager/admin edits → create product → map BSN code → mark approved.
    Returns the new product id. Single transaction (on `conn`) — the product
    row itself (spec cols + derived/override name + sku_code) is created by
    `create_structured_product` (P3 of the product-creation-consolidation
    plan; stamps `created_via='smart_mapping'`), called WITH this function's
    `conn` so it participates in the same transaction rather than committing
    on its own. That plus the surrounding BSN-mapping upsert, unit_conversion
    insert, and suggestion status update all commit or roll back together —
    a failure anywhere leaves no orphan product/mapping row. `edits` dict
    overrides any field on the staged suggestion."""
    conn = get_connection()
    try:
        sug = conn.execute(
            "SELECT * FROM pending_product_suggestions WHERE id = ? AND status='pending'",
            (suggestion_id,)
        ).fetchone()
        if not sug:
            raise ValueError(f'suggestion {suggestion_id} not found or already approved')

        # Merge: edits overrides suggestion.
        #
        # `is not None` means an omitted key preserves the staged value — right
        # for a partial edit payload. But the approve form sends its FK pickers
        # as `v('cat-id') || null`, so CLEARING a picker submits an explicit
        # null, which this filter would drop: the staged value survives and the
        # product is created with the very category/brand the manager just
        # removed (Codex review, 2026-08-22). Same membership-vs-truthiness trap
        # as the payroll note-clearing bug — a key that is PRESENT is an
        # instruction to write, even when its value is null.
        #
        # Scoped to the FK pickers the approve form can actually clear rather
        # than applied to every key, so an omitted-vs-null distinction still
        # protects the free-text fields (which submit '' for a clear, and ''
        # already passes the filter below).
        d = dict(sug)
        d.update({
            k: v for k, v in edits.items()
            if v is not None or k in _CLEARABLE_EDIT_KEYS
        })

        # packaging: free-text override is stored if dropdown empty
        # (may fail CHECK trigger on products INSERT — admin must extend trigger first)
        packaging_th = d.get('packaging') or None
        if not packaging_th and d.get('packaging_other'):
            packaging_th = d['packaging_other'].strip() or None

        # Clone provenance: durable in created_via itself rather than a
        # dedicated flag, matching how the rest of the app already stamps
        # source ids into this free-text column (claude:split-...,
        # script alias-round1 ...). clone_source_pid rode along on the
        # STAGED row (models.suggestions.save_pending_suggestion) so it
        # survives a stage-now/approve-later gap, not just the one-request
        # create_now path.
        created_via = ('smart_mapping_clone_' + str(d['clone_source_pid'])
                       if d.get('clone_source_pid') else 'smart_mapping')

        # Row-insert + name + sku_code all go through the canonical create
        # path. It re-resolves brand_other_name/color_code_other into new FK
        # rows and free-text `category` into `category_id` itself (same
        # logic this function used to inline). Passing OUR conn keeps it
        # inside this function's own transaction — no separate commit, so
        # the mapping/status writes below can still roll everything back
        # together on failure (no orphan product).
        new_pid = create_structured_product({
            'product_name': d.get('suggested_name') or d.get('bsn_name'),
            'brand_id': d.get('brand_id'),
            'brand_other_name': d.get('brand_other_name'),
            'color_code': d.get('color_code'),
            'color_code_other': d.get('color_code_other'),
            'color_th': d.get('color_th'),
            'category_id': d.get('category_id'),
            'category': d.get('category'),
            'sub_category': d.get('sub_category'),
            'sub_category_short_code': d.get('sub_category_short_code'),
            'series': d.get('series'),
            'model': d.get('model'),
            'size': d.get('size'),
            'condition': d.get('condition'),
            'pack_variant': d.get('pack_variant'),
            'packaging_th': packaging_th,
            'unit_type': d.get('suggested_unit_type') or 'ตัว',
            'cost_price': d.get('suggested_cost') or 0.0,
            'units_per_carton': d.get('units_per_carton') or 1,
            'units_per_box': d.get('units_per_box') or 1,
        }, created_via, conn=conn)

        # Upsert mapping (bsn_code → new product) — the non-split catch-all row
        # (bsn_unit='', mig 124 restore). UPDATE-then-INSERT mirrors
        # upsert_mapping() (boundary-safe; reuses the existing pending row, so
        # no separate placeholder cleanup is needed). Filtering/inserting on
        # bsn_unit='' means this never clobbers a unit-specific split row that
        # may already exist for this code (PR #178 regression class: mig 112
        # once made this INSERT omit bsn_unit entirely and 500 on a NOT NULL
        # column with no default — restored here explicitly, not relying on
        # the column DEFAULT, to keep intent obvious).
        updated = conn.execute(
            "UPDATE product_code_mapping SET bsn_name=?, product_id=?, is_ignored=0 "
            "WHERE bsn_code=? AND bsn_unit=''",
            (sug['bsn_name'], new_pid, sug['bsn_code'])
        ).rowcount
        if not updated:
            conn.execute(
                "INSERT OR IGNORE INTO product_code_mapping "
                "(bsn_code, bsn_name, product_id, is_ignored, bsn_unit) "
                "VALUES (?, ?, ?, 0, '')",
                (sug['bsn_code'], sug['bsn_name'], new_pid)
            )

        # Mark suggestion approved
        conn.execute("""
            UPDATE pending_product_suggestions
               SET status = 'approved',
                   reviewed_by_user_id = ?,
                   approved_product_id = ?,
                   reviewed_at = datetime('now','localtime')
             WHERE id = ?
        """, (reviewer_id, new_pid, suggestion_id))

        # Auto-create unit_conversion if BSN ships in different unit than product
        bsn_unit = d.get('bsn_unit')
        ratio = d.get('unit_conversion_ratio')
        product_unit = d.get('suggested_unit_type') or 'ตัว'
        if bsn_unit and ratio and float(ratio) > 0 and bsn_unit != product_unit:
            hz = cross_unit_hazard(conn, new_pid, bsn_unit)
            # Allowlist, not a blocklist: only a clean None or a ratio-1
            # pack_piece alias inserts. Any OTHER kind — 'pair',
            # 'configuration_error' (cross_unit_hazard never raises this past
            # its own boundary, see its docstring), or a kind added later —
            # already falls through to "do not insert" without needing to be
            # named here.
            if hz is None or (hz['kind'] == 'pack_piece' and float(ratio) == 1):
                conn.execute("""
                    INSERT INTO unit_conversions (product_id, bsn_unit, ratio)
                    VALUES (?, ?, ?)
                    ON CONFLICT(product_id, bsn_unit) DO UPDATE SET
                        ratio = excluded.ratio
                """, (new_pid, bsn_unit, float(ratio)))

        # Backfill product_id on existing unlinked transaction rows
        resolve_pending_mappings(conn)

        conn.commit()
        return new_pid
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_now(payload: dict, user_id: int, confirm_duplicate: bool = False) -> tuple:
    """'สร้างเลย' (mapping-suggest-clone plan, PR3, decision Q7): stage then
    immediately approve, SERVER-SIDE, in one call — not two client requests.
    `bsn.py::mapping_save` used to discard the sid `save_pending_suggestion`
    returns and re-find the row by `bsn_code` before approving; that races
    the UPSERT on `bsn_code`'s unique key (a second tab or a double-click
    could approve data it never staged). Doing both here removes the window.

    Still TWO commits under the hood: `save_pending_suggestion` commits, then
    `approve_pending_suggestion` opens its own connection and commits again.
    Deliberately NOT merged into one transaction — a failure between them
    (e.g. the packaging_th CHECK trigger) leaves a pending row, visible and
    recoverable on the Tab-2 review list, rather than reimplementing
    approve's own all-or-nothing block here.

    Duplicate guard (Q8/Q12): before staging anything, compute the would-be
    sku_code the same way `create_structured_product`/`regenerate_for_product`
    would, and check it against ALL products — active AND inactive.
    `idx_products_sku_code` has no is_active filter and 49 inactive rows
    hold sku_codes; an active-only check would pass here and then the new
    row would silently take a `-<id>` collision suffix instead — the exact
    event this guard exists to catch, whose collision partner is usually
    something deliberately merged away. Raises DuplicateSkuError unless
    `confirm_duplicate=True` (the client's confirmed retry) or there's
    genuinely no collision. Never a hard block — `-<id>` suffixes exist
    because genuine look-alikes are real; confirm_duplicate lets the caller
    create anyway and take that suffix.

    Concurrency guard (the "two create_now for the same bsn_code" case):
    the CLAIM step below uses `save_pending_suggestion(..., upsert=False)` —
    a plain INSERT, not the usual UPSERT — so `pending_product_suggestions
    .bsn_code`'s own UNIQUE constraint is what serializes two overlapping
    create_now calls: SQLite guarantees only one of two concurrent INSERTs
    on the same key can ever commit, so the second raises IntegrityError
    (surfaced here as SuggestionAlreadyStagedError) instead of silently
    UPSERT-overwriting the first call's row out from under its own
    approve_pending_suggestion read. This closes the gap the discarded-sid
    bug (see docstring above) used to open even after fixing the sid itself:
    passing the right sid means nothing if a second writer can still repoint
    what that sid's row CONTAINS between save and approve.
    """
    if not confirm_duplicate:
        conn = get_connection()
        try:
            proposed_sku = sku_code_utils.preview_sku_code(conn, payload)
            if proposed_sku:
                dup = conn.execute(
                    "SELECT id, product_name, sku_code, is_active FROM products "
                    "WHERE sku_code = ?",
                    (proposed_sku,),
                ).fetchone()
                if dup:
                    raise DuplicateSkuError(dict(dup))
        finally:
            conn.close()

    try:
        sid = save_pending_suggestion(payload, user_id, upsert=False)
    except sqlite3.IntegrityError:
        conn = get_connection()
        existing = conn.execute(
            "SELECT status FROM pending_product_suggestions WHERE bsn_code=?",
            (payload['bsn_code'],),
        ).fetchone()
        conn.close()
        raise SuggestionAlreadyStagedError(
            payload['bsn_code'], existing['status'] if existing else 'unknown'
        )
    new_pid = approve_pending_suggestion(sid, {}, user_id)

    # Post-create collision detection (Codex review, 2026-08-22).
    #
    # The guard above is a check-then-write across two connections, so two
    # requests for DIFFERENT bsn_codes that resolve to the SAME sku_code can
    # both pass it before either inserts. `UNIQUE(bsn_code)` serializes only
    # same-code races, not this one. The loser then gets a `-<id>` suffix from
    # regenerate_for_product without ever showing the operator a confirmation.
    #
    # Deliberately NOT fixed by wrapping the check and the insert in one
    # transaction: the insert lives inside approve_pending_suggestion's own
    # all-or-nothing block, and re-implementing that here is exactly what the
    # plan (decision Q7) rules out. The suffix itself is the designed fallback
    # and is not corruption — the real harm is that it happens SILENTLY. So we
    # detect it after the fact and hand the caller a warning to surface.
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT sku_code FROM products WHERE id = ?", (new_pid,)
        ).fetchone()
    finally:
        conn.close()
    sku = (row['sku_code'] if row else '') or ''
    if not confirm_duplicate and sku.endswith(f'-{new_pid}'):
        return new_pid, (
            f'สร้างสำเร็จ แต่ sku_code ชนกับสินค้าเดิม จึงได้ต่อท้ายเป็น {sku} '
            '— ตรวจสอบว่าซ้ำกับตัวที่มีอยู่หรือไม่'
        )
    return new_pid, None
