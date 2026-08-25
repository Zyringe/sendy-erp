"""Dictionary→product_name cascade engine for the Master Naming page.

A token in a dictionary (a color's Thai word, or a brand's in-name token)
changes → every ACTIVE product whose NAME contains that token AND whose
structured field matches gets the token substring-replaced. This is NOT a
rebuild from columns: ~42% of decomposed names diverge from build() (dirty
`series`, descriptors that live in no column), so a blanket rebuild would
mass-overwrite hand-tuned names. We only swap the one changed token.

Scope-by-field is load-bearing: colors AB and JBB both mean 'สีทองแดงรมดำ',
so scoping an AB cascade by the literal Thai word would wrongly rename JBB
products. We scope by `color_code` / `brand_id` and skip rows where the old
token isn't actually present (code present, word absent — e.g. model `#118AB`).

Every apply: WAL-safe backup first, then `BEGIN IMMEDIATE`, recompute the
affected set inside the txn (guard concurrent edits), apply, assert integrity
invariants (sku_code untouched + unique, no orphaned mappings, exact row
count), commit or rollback. Pure functions take an explicit conn / db_path so
they unit-test without the Flask app.
"""
from __future__ import annotations

import difflib
import sqlite3
import unicodedata

import db_backup
import name_builder
from sku_code_utils import PACKAGING_SHORT

KIND_COLOR = "color"
KIND_BRAND = "brand"


class CascadeConflict(Exception):
    """Affected set changed between preview and apply (concurrent edit)."""


class CascadeInvariantError(Exception):
    """A post-apply integrity invariant failed — the cascade was rolled back."""


class ProductNotFound(Exception):
    """save_product was asked to edit a product id that doesn't exist."""


def replace_token(name, old, new):
    """Replace every occurrence of `old` with `new` in `name`.

    Returns (new_name, changed). `changed` is False when `old` is empty,
    equals `new`, or is absent from `name` (code present but word absent).
    """
    if not old or old == new or old not in name:
        return name, False
    return name.replace(old, new), True


# ── preview (read-only) ───────────────────────────────────────────────────────

def _preview_color(conn, code, target):
    row = conn.execute(
        "SELECT name_th FROM color_finish_codes WHERE code=?", (code,)
    ).fetchone()
    old = row[0] if row else None
    rows = conn.execute(
        "SELECT id, product_name FROM products "
        "WHERE color_code=? AND is_active=1 ORDER BY id",
        (code,),
    ).fetchall()
    affected, skipped = [], []
    for r in rows:
        new_name, changed = replace_token(r["product_name"], old, target)
        if changed:
            affected.append({"id": r["id"], "old_name": r["product_name"],
                             "new_name": new_name})
        else:
            skipped.append({"id": r["id"], "name": r["product_name"],
                            "reason": "word_absent"})
    return {"kind": KIND_COLOR, "key": code, "old": old, "new": target,
            "affected": affected, "skipped": skipped}


def _preview_brand(conn, brand_id, target):
    row = conn.execute(
        "SELECT name, name_th FROM brands WHERE id=?", (brand_id,)
    ).fetchone()
    olds = []
    if row:
        for tok in (row["name"], row["name_th"]):
            if tok and tok != target and tok not in olds:
                olds.append(tok)
    rows = conn.execute(
        "SELECT id, product_name FROM products "
        "WHERE brand_id=? AND is_active=1 ORDER BY id",
        (brand_id,),
    ).fetchall()
    affected, skipped = [], []
    for r in rows:
        name = r["product_name"]
        if target and target in name:
            skipped.append({"id": r["id"], "name": name, "reason": "already_target"})
            continue
        new_name = None
        for old in olds:
            cand, changed = replace_token(name, old, target)
            if changed:
                new_name = cand
                break
        if new_name is not None:
            affected.append({"id": r["id"], "old_name": name, "new_name": new_name})
        else:
            skipped.append({"id": r["id"], "name": name, "reason": "brand_absent"})
    return {"kind": KIND_BRAND, "key": brand_id, "old": None, "new": target,
            "affected": affected, "skipped": skipped}


def preview(conn, kind, key, target):
    """Read-only. Return {kind, key, old, new, affected[], skipped[]}.

    affected: [{id, old_name, new_name}]; skipped: [{id, name, reason}].
    """
    if kind == KIND_COLOR:
        return _preview_color(conn, key, target)
    if kind == KIND_BRAND:
        return _preview_brand(conn, int(key), target)
    raise ValueError(f"unknown cascade kind: {kind!r}")


# ── invariant helpers ─────────────────────────────────────────────────────────

def _sku_map(conn, ids):
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, sku_code FROM products WHERE id IN ({placeholders})", ids
    ).fetchall()
    return {r["id"]: r["sku_code"] for r in rows}


def _sku_unique_ok(conn):
    row = conn.execute(
        "SELECT COUNT(*) AS n, COUNT(DISTINCT sku_code) AS d "
        "FROM products WHERE sku_code IS NOT NULL"
    ).fetchone()
    return row["n"] == row["d"]


def _orphan_mappings(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM product_code_mapping m "
        "LEFT JOIN products p ON p.id = m.product_id "
        "WHERE m.product_id IS NOT NULL AND p.id IS NULL"
    ).fetchone()[0]


def _active_count(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM products WHERE is_active=1"
    ).fetchone()[0]


# ── apply (the one write path) ────────────────────────────────────────────────

def apply(db_path, kind, key, target, expected_count, *,
          backup_dir=None, reason="master_naming_cascade"):
    """Cascade `kind`/`key` → `target` across product names.

    Backs up first (WAL-safe), recomputes the affected set inside a write txn,
    requires it to equal `expected_count` (else CascadeConflict + rollback),
    applies the dict + name updates, asserts integrity invariants (else
    CascadeInvariantError + rollback), commits. Returns
    {"applied": n, "backup": <name or None>}.
    """
    if backup_dir is None:
        backup_dir = db_backup.default_backup_dir(db_path)

    info, err = db_backup.safe_create_backup(reason, db_path=db_path,
                                             backup_dir=backup_dir)
    if err:
        raise RuntimeError(f"backup failed, cascade aborted: {err}")

    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # manual transaction control
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")

        pv = preview(conn, kind, key, target)
        affected = pv["affected"]
        if len(affected) != expected_count:
            conn.execute("ROLLBACK")
            raise CascadeConflict(
                f"affected count {len(affected)} != expected {expected_count}")

        ids = [a["id"] for a in affected]
        before_sku = _sku_map(conn, ids)
        before_active = _active_count(conn)

        if kind == KIND_COLOR:
            conn.execute("UPDATE color_finish_codes SET name_th=? WHERE code=?",
                         (target, key))
        # brand standardize is name-only — the brands table is left unchanged.

        changed = 0
        for a in affected:
            conn.execute("UPDATE products SET product_name=? WHERE id=?",
                         (a["new_name"], a["id"]))
            changed += 1

        problems = []
        if changed != expected_count:
            problems.append(f"changed {changed} != expected {expected_count}")
        if _sku_map(conn, ids) != before_sku:
            problems.append("sku_code changed")
        if not _sku_unique_ok(conn):
            problems.append("sku_code uniqueness broken")
        if _orphan_mappings(conn):
            problems.append("orphaned product_code_mapping rows")
        if _active_count(conn) != before_active:
            problems.append("active product count changed")
        if problems:
            conn.execute("ROLLBACK")
            raise CascadeInvariantError("; ".join(problems))

        conn.execute("COMMIT")
        return {"applied": changed, "backup": info["name"] if info else None}
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


# ── single-product edit (Tab 1 workbench inline save) ─────────────────────────

_EDITABLE_TEXT = ("series", "model", "size", "color_code", "packaging_th",
                  "condition", "pack_variant", "sub_category")


# ZERO WIDTH JOINER / NON-JOINER. Explicitly, NOT the whole Cf category: RLM/LRM and
# SOFT HYPHEN really are formatting, but a dropped ZWJ/ZWNJ changes shaping.
_ZW = ("\u200d", "\u200c")


def _norm_name(s):
    """NFC, `_` -> space, collapse runs of whitespace.

    `sub_category` legitimately stores `แผ่นตัดเหล็กบาง_Super_Thin` while the stored name
    spells it with spaces; 137 active products differ ONLY that way, losing nothing.

    NFC matters now that category M counts as content: without it, two names that are
    canonically EQUIVALENT but order their combining marks differently compare unequal
    by position, SequenceMatcher reports a mark deleted, and the operator is refused —
    then pushed toward the DESTRUCTIVE override to save something that was never going
    to lose anything (Codex, 2026-08-24). Measured on the prod snapshot, NFC changes
    0 of 2,044 stored names, so it is pure insurance for imported text.
    """
    return " ".join(
        unicodedata.normalize("NFC", (s or "")).replace("_", " ").split())


def _loss_spans(old_name, new_name):
    """Indices of `_norm_name(old_name)` whose characters `new_name` does not carry.

    POSITIONS, not fragments. Two loss sets must be comparable across different edits,
    and SequenceMatcher's fragment BOUNDARIES move when the candidate changes: with an
    orphan `TAYITA`, the pre-edit rebuild reports the fragment `TAYITA` while a
    candidate that preserved only `TAYI` reports `TA`. Comparing those two strings for
    equality finds no overlap and lets the save through, destroying the leftover — the
    exact bypass this function exists to close (Codex, 2026-08-24). Character positions
    intersect correctly no matter where the boundaries fall.
    """
    a, b = _norm_name(old_name), _norm_name(new_name)
    out = set()
    for tag, i1, i2, _j1, _j2 in difflib.SequenceMatcher(
            None, a, b, autojunk=False).get_opcodes():
        if tag in ("delete", "replace"):
            out.update(range(i1, i2))
    return out


def _spans_to_fragments(old_name, spans):
    """Contiguous runs of `spans` rendered as readable text, keeping only runs that
    carry a letter or digit. A hand-picked punctuation allowlist missed '#', ':', '+',
    quotes and Thai punctuation, so formatting-only round-trips were refused for no
    reason."""
    a = _norm_name(old_name)
    frags, run = [], []
    for i in sorted(spans):
        if run and i == run[-1] + 1:
            run.append(i)
        else:
            if run:
                frags.append("".join(a[k] for k in run))
            run = [i]
    if run:
        frags.append("".join(a[k] for k in run))
    # L, M and N. **M is not optional for Thai**: สระ and วรรณยุกต์ (ิ ่ ้ ็ ...) are
    # combining marks, category Mn — dropping one changes the word, but an L/N-only
    # test reads it as punctuation and reports "nothing lost". `กิ` -> `ก` produced a
    # loss span and an EMPTY fragment list, so both the probe and the authoritative
    # check waved the save through (Codex, 2026-08-24).
    return [f.strip() for f in frags
            if any(unicodedata.category(ch)[0] in ("L", "M", "N") or ch in _ZW
                   for ch in f)]


def _name_loss(old_name, new_name):
    """Readable fragments of `old_name` that `new_name` drops."""
    return _spans_to_fragments(old_name, _loss_spans(old_name, new_name))


def _clean_updates(fields):
    """Whitelist + normalize the editable structured columns from `fields`.

    Empty strings → NULL; brand_id coerced to int or NULL; packaging_short
    derived from packaging_th so the sku stays consistent with the name.
    Non-whitelisted keys (e.g. product_name, sku_code) are ignored — the name
    is rebuilt and the sku regenerated, never set directly here.
    """
    updates = {}
    for k in _EDITABLE_TEXT:
        if k in fields:
            v = fields[k]
            updates[k] = (v.strip() or None) if isinstance(v, str) else v
    if "brand_id" in fields:
        b = fields["brand_id"]
        updates["brand_id"] = int(b) if b not in (None, "", 0, "0") else None
    if "packaging_th" in updates:
        pth = updates["packaging_th"]
        updates["packaging_short"] = PACKAGING_SHORT.get(pth) if pth else None
    return updates


def save_product(db_path, pid, fields, *, backup_dir=None,
                 reason="master_naming_edit", rebuild_name=False,
                 expected_product_name=None):
    """Update a product's structured naming columns. **`product_name` is NOT rebuilt.**

    ⚠ This function used to recompose product_name from the columns on EVERY save and
    UPDATE it unconditionally. That is what destroyed hand-tuned names: measured on the
    2026-08-24 prod snapshot, 342 of 1,994 active products carry text no column holds,
    so saving any unrelated field silently dropped it.

    The stored name is now the source of truth, which is what the rest of the system
    already assumed. `naming_cascade.apply()` — the bulk dictionary cascade in this very
    module — has always refused to rebuild ("~42% of decomposed names diverge from
    build()"), and the audit log shows all 2,594 recorded name changes were direct
    writes, none of them accompanied by a naming-column edit: the naming standard has
    been enforced by scripts, never by this rebuild. `save_product` was the odd one out.

    Two ways the name can still change, both explicit:
      * `fields["product_name"]` — the operator typed it, or adopted the workbench's
        suggested name by pressing the button (which shows what adopting would drop).
      * `rebuild_name=True` — recompose from the columns. Only
        `scripts/hammer_bundle_datafix.py` asks for this, and it asserts the exact
        name it expects afterwards.

    `expected_product_name` is optimistic concurrency for the rename: the workbench
    seeds its name box when the page LOADS, and the cascade and the mass-rename scripts
    write names continuously (223 changes in July, 42 in August). Without the check, a
    page left open and then saved would quietly restore an obsolete name over a newer
    one — the same undetectable loss this redesign exists to remove, arriving through a
    different door (Codex, 2026-08-25). Mismatch raises `CascadeConflict`, matching what
    `apply()` already does when its affected set moves between preview and apply.

    ⚠ **sku_code is deliberately NOT regenerated here** (issue #383). It used to
    be, lock-aware. The two fields are different kinds of thing:

        product_name  derived display text — rebuilding it IS this function's job
        sku_code      a stable EXTERNAL identity key, joining the ERP to
                      photo-library folder names, reshoot batch.json, and the
                      Seller SKU typed into TikTok

    Because stored codes and the generator had drifted, saving an unrelated typo
    silently moved that key for 14 products, 6 of them live TikTok SKUs — and
    nothing errored, because a stale code does not fail, it just stops resolving
    (`plan_all_batches.py` then drops the product as out-of-stock).

    Warning-then-confirming could not fix it: this function COMMITs before its
    caller can serialise a response, so any confirmation is after the fact.
    Lock-by-default could not either — `/products/<id>/regen-sku-code` sets
    `sku_code_locked = 0`, so one legitimate rename re-arms the hazard.

    Moving a code is now only ever explicit: `/products/<id>/regen-sku-code`.
    `apply()` above already took this position (it rolls back on "sku_code
    changed"); this function was the inconsistent one.

    Returns {old_name, new_name, sku_code}. Backs up first, then BEGIN IMMEDIATE
    + invariant asserts → commit or rollback. Raises ProductNotFound if `pid`
    doesn't exist.
    """
    if backup_dir is None:
        backup_dir = db_backup.default_backup_dir(db_path)
    info, err = db_backup.safe_create_backup(reason, db_path=db_path,
                                             backup_dir=backup_dir)
    if err:
        raise RuntimeError(f"backup failed, edit aborted: {err}")

    updates = _clean_updates(fields)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")

        cur = conn.execute(
            "SELECT product_name, sku_code FROM products WHERE id=?",
            (pid,)).fetchone()
        if cur is None:
            conn.execute("ROLLBACK")
            raise ProductNotFound(f"product {pid} not found")
        old_name, old_sku = cur["product_name"], cur["sku_code"]
        before_active = _active_count(conn)

        if updates:
            set_clause = ", ".join(f"{k}=?" for k in updates)
            conn.execute(f"UPDATE products SET {set_clause} WHERE id=?",
                         list(updates.values()) + [pid])

        # The name moves only when someone says so. `_clean_updates` still ignores
        # product_name (it is not a spec column), so it is read straight from `fields`.
        typed = fields.get("product_name")
        typed = typed.strip() if isinstance(typed, str) else None
        if (expected_product_name is not None
                and typed and typed != old_name
                and expected_product_name != old_name):
            conn.execute("ROLLBACK")
            raise CascadeConflict(
                f"ชื่อสินค้าถูกแก้จากที่อื่นระหว่างที่เปิดหน้านี้ค้างไว้ "
                f"(ตอนเปิด: {expected_product_name!r} · ตอนนี้: {old_name!r}) "
                f"— โหลดหน้าใหม่แล้วแก้อีกครั้ง")
        if rebuild_name:
            new_name = name_builder.rebuild_product_name(conn, pid)
        elif typed:
            new_name = typed
        else:
            new_name = old_name
        if new_name != old_name:
            conn.execute("UPDATE products SET product_name=? WHERE id=?",
                         (new_name, pid))

        problems = []
        # Structural, not merely "we don't call the generator any more": if any
        # future edit re-introduces a mutation path, the transaction rolls back
        # instead of silently moving the key. Same stance, and the same phrasing,
        # as apply()'s _sku_map check above.
        after_sku = conn.execute("SELECT sku_code FROM products WHERE id=?",
                                 (pid,)).fetchone()["sku_code"]
        if after_sku != old_sku:
            problems.append("sku_code changed")
        if not _sku_unique_ok(conn):
            problems.append("sku_code uniqueness broken")
        if _orphan_mappings(conn):
            problems.append("orphaned product_code_mapping rows")
        if _active_count(conn) != before_active:
            problems.append("active product count changed")
        if problems:
            conn.execute("ROLLBACK")
            raise CascadeInvariantError("; ".join(problems))

        conn.execute("COMMIT")
        return {"old_name": old_name, "new_name": new_name, "sku_code": old_sku}
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
