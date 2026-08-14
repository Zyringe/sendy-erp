"""Master Naming blueprint — /naming.

The single place to manage product naming. Two tabs:

  Tab 1 (workbench): every active product, decomposed into its structured parts
    (brand, series, model, size, color + Thai color word, packaging, condition,
    pack_variant) with a "decomposed?" flag. Browse/search. (Inline editing +
    rebuilt-name diff land in Phase 2.)

  Tab 2 (dictionaries): edit the shared color & brand tokens. A change previews
    every affected product name (old→new) and, on confirm, cascades via
    naming_cascade — a substring replace scoped by the structured field, backed
    up first, with invariant asserts. NOT a rebuild from columns.

Routes
------
GET  /naming?tab=workbench|dictionaries
POST /naming/dict/<kind>/preview   (JSON, read-only) — {affected[], skipped[]}
POST /naming/dict/<kind>/apply     (JSON) — confirmed cascade → {ok, applied, backup}

Manager/admin only (staff blocked for the whole `naming.` prefix in
app.before_request; the POST endpoints are in _MANAGER_POST_OK).
"""
import database
import db_backup
import name_builder
import naming_cascade as nc
from flask import (Blueprint, render_template, request, jsonify)

from database import get_connection
from sku_code_utils import PACKAGING_SHORT, CONDITION_SHORT


def _db_path():
    """The live DB path — read from the same module as get_connection so the
    cascade/edit connection always matches what the rest of the app reads
    (database.DATABASE_PATH is what get_connection uses)."""
    return database.DATABASE_PATH

bp_naming = Blueprint('naming', __name__)

_VALID_KINDS = (nc.KIND_COLOR, nc.KIND_BRAND)
_PER_PAGE = 100


def _condition_opts(conn):
    """Canonical conditions PLUS every value already stored on a product.

    The dropdown used to be `CONDITION_SHORT` alone. That list is closed (10 values)
    and doubles as the sku-segment map, so any condition outside it could not be
    represented in the editor — and the editor does not fail safe:
    master_naming.html's `set()` leaves the <select> blank for an unknown value, the
    save payload always carries `condition: <value> || null`, and `_clean_updates`
    treats a present key as an instruction to write. So opening such a product and
    saving NULLED the column and rebuilt product_name without the word, in one
    transaction.

    44 rows on prod were exposed (swept 2026-08-14), in two severities:
      แบบหุล 19 · แบบมิล 8 · มียอด 7 — unmapped, so the sku survives; the word is lost.
      EXP:* 10 rows            — `_condition_segment` DOES emit a segment for these, so
                                 the regenerated sku loses it too:
                                 `DSC-CUT-AS-S5048-14in-GRN-EXP0727` → `…-14in-GRN`.
                                 That SKU is a live variant in a TikTok listing file, and
                                 sku_code is the join key three other consumers look up by.

    ⛔ Do NOT close the gap by adding those words to CONDITION_SHORT instead. That map
    also generates the sku segment, so it would rename every affected product —
    and `sku_code` is a cross-system join key (ERP ↔ photo folders ↔ batch.json ↔
    listing Seller SKU). `_condition_segment()` returns "" for an unmapped word, which
    is exactly why widening the dropdown alone leaves every sku_code untouched.

    Canonical values keep their defined order (it is a meaningful ordering, not
    alphabetical); DB-only extras are appended, sorted, de-duplicated.
    """
    stored = [r[0] for r in conn.execute(
        "SELECT DISTINCT condition FROM products "
        "WHERE TRIM(COALESCE(condition, '')) <> '' ORDER BY condition")]
    known = list(CONDITION_SHORT)
    return known + [c for c in stored if c not in CONDITION_SHORT]


def _editor_options(conn):
    """Option lists for the Tab 1 inline editor's dropdowns."""
    return {
        'brand_opts': conn.execute(
            "SELECT id, name, name_th FROM brands "
            "ORDER BY is_own_brand DESC, sort_order, name"
        ).fetchall(),
        'color_opts': conn.execute(
            "SELECT code, name_th FROM color_finish_codes ORDER BY sort_order, code"
        ).fetchall(),
        # EXEMPT, deliberately: packaging_th has the identical closed-list shape, but
        # (a) 0 products currently carry a value outside PACKAGING_SHORT (swept on prod
        # 2026-08-14) and (b) unlike condition, an unmapped packaging_th is NOT sku-safe
        # — `_clean_updates` derives packaging_short from PACKAGING_SHORT, so offering an
        # unknown value would let a save blank packaging_short and change the sku_code.
        # Widening this one needs that decision first; it is not the same fix.
        'packaging_opts': list(PACKAGING_SHORT.keys()),
        'condition_opts': _condition_opts(conn),
    }


# ── Tab data builders ─────────────────────────────────────────────────────────

def _workbench_products(conn, q, scope, page):
    """Active products with their decomposed parts + Thai color word, paginated.

    scope: 'all' | 'decomposed' | 'freeform'. A product is "decomposed" when it
    carries at least one structured naming field.
    """
    decomposed_expr = (
        "(p.series IS NOT NULL OR p.model IS NOT NULL OR p.size IS NOT NULL "
        " OR p.color_code IS NOT NULL OR p.packaging_th IS NOT NULL "
        " OR p.condition IS NOT NULL)"
    )
    where = ["p.is_active = 1"]
    params = []
    if q:
        where.append("p.product_name LIKE ?")
        params.append(f"%{q}%")
    if scope == 'decomposed':
        where.append(decomposed_expr)
    elif scope == 'freeform':
        where.append("NOT " + decomposed_expr)
    where_sql = " AND ".join(where)

    total = conn.execute(
        f"SELECT COUNT(*) FROM products p WHERE {where_sql}", params
    ).fetchone()[0]

    rows = conn.execute(
        f"""
        SELECT p.id, p.product_name, p.brand_id, b.name AS brand_name,
               p.sub_category, p.series, p.model, p.size,
               p.color_code, cf.name_th AS color_th,
               p.packaging_th, p.condition, p.pack_variant,
               p.sku_code, p.sku_code_locked,
               {decomposed_expr} AS is_decomposed
          FROM products p
          LEFT JOIN brands b              ON b.id = p.brand_id
          LEFT JOIN color_finish_codes cf ON cf.code = p.color_code
         WHERE {where_sql}
         ORDER BY p.id
         LIMIT ? OFFSET ?
        """,
        params + [_PER_PAGE, (page - 1) * _PER_PAGE],
    ).fetchall()
    pages = (total + _PER_PAGE - 1) // _PER_PAGE
    return rows, total, pages


def _color_dictionary(conn):
    """color_finish_codes + how many active products use each code."""
    return conn.execute(
        """
        SELECT c.code, c.name_th, c.sort_order,
               COUNT(p.id) AS n_products
          FROM color_finish_codes c
          LEFT JOIN products p ON p.color_code = c.code AND p.is_active = 1
         GROUP BY c.code
         ORDER BY c.sort_order, c.code
        """
    ).fetchall()


def _brand_dictionary(conn):
    """Brands with active product count + how many names currently show the
    English token vs the Thai token (so Put can pick the canonical form)."""
    return conn.execute(
        """
        SELECT b.id, b.code, b.name, b.name_th, b.short_code,
               COUNT(p.id) AS n_products,
               SUM(CASE WHEN b.name IS NOT NULL AND b.name <> ''
                         AND p.product_name LIKE '%' || b.name || '%'
                        THEN 1 ELSE 0 END) AS n_en,
               SUM(CASE WHEN b.name_th IS NOT NULL AND b.name_th <> ''
                         AND p.product_name LIKE '%' || b.name_th || '%'
                        THEN 1 ELSE 0 END) AS n_th
          FROM brands b
          LEFT JOIN products p ON p.brand_id = b.id AND p.is_active = 1
         GROUP BY b.id
        HAVING n_products > 0
         ORDER BY n_products DESC, b.name
        """
    ).fetchall()


# ── Page ──────────────────────────────────────────────────────────────────────

@bp_naming.route('/naming')
def index():
    tab = request.args.get('tab', 'workbench')
    conn = get_connection()
    try:
        if tab == 'dictionaries':
            return render_template(
                'master_naming.html', active_tab='dictionaries',
                colors=_color_dictionary(conn), brands=_brand_dictionary(conn),
            )
        # default: workbench
        q = request.args.get('q', '').strip()
        scope = request.args.get('scope', 'all')
        try:
            page = max(1, int(request.args.get('page', 1)))
        except (TypeError, ValueError):
            page = 1
        rows, total, pages = _workbench_products(conn, q, scope, page)
        return render_template(
            'master_naming.html', active_tab='workbench',
            products=rows, total=total, page=page, pages=pages,
            q=q, scope=scope, **_editor_options(conn),
        )
    finally:
        conn.close()


# ── Cascade JSON API ──────────────────────────────────────────────────────────

def _kind_or_400(kind):
    if kind not in _VALID_KINDS:
        return None
    return kind


@bp_naming.route('/naming/dict/<kind>/preview', methods=['POST'])
def dict_preview(kind):
    """Read-only: what would change if `key` → `target`. No writes."""
    if _kind_or_400(kind) is None:
        return jsonify({'ok': False, 'error': f'unknown kind: {kind}'}), 400
    data = request.get_json(silent=True) or {}
    key = data.get('key')
    target = (data.get('target') or '').strip()
    if key is None or not target:
        return jsonify({'ok': False, 'error': 'ต้องระบุ key และ target'}), 400
    conn = get_connection()
    try:
        pv = nc.preview(conn, kind, key, target)
    finally:
        conn.close()
    return jsonify({'ok': True, **pv})


@bp_naming.route('/naming/dict/<kind>/apply', methods=['POST'])
def dict_apply(kind):
    """Confirmed cascade. `expected_count` must match the previewed count."""
    if _kind_or_400(kind) is None:
        return jsonify({'ok': False, 'error': f'unknown kind: {kind}'}), 400
    data = request.get_json(silent=True) or {}
    key = data.get('key')
    target = (data.get('target') or '').strip()
    try:
        expected_count = int(data.get('expected_count'))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'expected_count ไม่ถูกต้อง'}), 400
    if key is None or not target:
        return jsonify({'ok': False, 'error': 'ต้องระบุ key และ target'}), 400

    db_path = _db_path()
    try:
        res = nc.apply(
            db_path, kind, key, target, expected_count,
            backup_dir=db_backup.default_backup_dir(db_path),
        )
    except nc.CascadeConflict as e:
        return jsonify({'ok': False, 'error':
                        f'ข้อมูลเปลี่ยนระหว่างตรวจสอบ กรุณาดูตัวอย่างใหม่ ({e})'}), 409
    except nc.CascadeInvariantError as e:
        return jsonify({'ok': False, 'error':
                        f'ยกเลิกแล้ว: การตรวจสอบความถูกต้องไม่ผ่าน ({e})'}), 500
    except Exception as e:  # backup failure, etc. — nothing was committed
        return jsonify({'ok': False, 'error': str(e)}), 500
    return jsonify({'ok': True, **res})


# ── Tab 1 single-product edit ─────────────────────────────────────────────────

@bp_naming.route('/naming/product/preview-name', methods=['POST'])
def product_preview_name():
    """Read-only: the canonical name that the submitted (not-yet-saved) field
    values would produce. Powers the live preview in the inline editor."""
    fields = request.get_json(silent=True) or {}
    conn = get_connection()
    try:
        name = name_builder.preview_name(conn, fields)
    finally:
        conn.close()
    return jsonify({'ok': True, 'name': name})


@bp_naming.route('/naming/product/<int:pid>/save', methods=['POST'])
def product_save(pid):
    """Apply edited structured columns to one product → rebuild name +
    regenerate sku_code (lock-aware). Returns old/new name + sku."""
    fields = request.get_json(silent=True) or {}
    db_path = _db_path()
    try:
        res = nc.save_product(db_path, pid, fields,
                              backup_dir=db_backup.default_backup_dir(db_path))
    except nc.ProductNotFound:
        return jsonify({'ok': False, 'error': f'ไม่พบสินค้า #{pid}'}), 404
    except nc.CascadeInvariantError as e:
        return jsonify({'ok': False, 'error':
                        f'ยกเลิกแล้ว: การตรวจสอบความถูกต้องไม่ผ่าน ({e})'}), 500
    except Exception as e:  # FK violation (bad color/brand), backup failure, etc.
        return jsonify({'ok': False, 'error': str(e)}), 400
    return jsonify({'ok': True, **res})
