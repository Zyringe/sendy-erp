"""Supplier-catalogue mapping blueprint.

Phase 3 of the supplier-catalogue intelligence project. Provides UI for
mapping purchased items (purchase_transactions.product_name_raw) to
catalogue items (supplier_catalogue_items) imported from price-list files.

Primary view is purchased-first: the user only cares about catalogue items
they have actually bought, since auto-match yields ~3%.
"""
import json
import re
from collections import Counter

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, session, jsonify, abort)

from database import get_connection

bp_supplier_catalogue = Blueprint('supplier_catalogue', __name__,
                                  url_prefix='/supplier-catalogue')


# ── Role gate ────────────────────────────────────────────────────────────────
# Every route in this blueprint exposes procurement-cost-sensitive data
# (list_price, net_cash_price). The role model says staff must not see
# cost/GP. The app-level _before_request middleware checks POSTs against
# _STAFF_POST_OK / _MANAGER_POST_OK and gates hr.*/cashbook.* GETs, but it
# does NOT gate supplier_catalogue.* GETs — those would leak prices.
# Block all access here so the gate lives next to the data.

@bp_supplier_catalogue.before_request
def _require_admin_or_manager():
    role = session.get('role')
    if role not in ('admin', 'manager'):
        flash('ไม่มีสิทธิ์เข้าถึงข้อมูลราคาผู้ผลิต', 'danger')
        return redirect(url_for('dashboard'))


# ── Helpers ───────────────────────────────────────────────────────────────────

_BRACKET_RE = re.compile(r"[\[\(].*?[\]\)]")
# Token separators inside a "word": dash, comma, hash, slash, plus, asterisk.
# Splitting on these unblocks matches like 'BRAVO-101' ↔ 'BRAVO' + '101'.
_TOKEN_SPLIT_RE = re.compile(r"[\s,\-#/+*]+")
# Split on Latin↔Thai script boundary (but not digit↔Thai, since '6นิ้ว' is a
# useful joined size-token). BSN data frequently glues Latin to Thai with no
# separator — e.g., 'STLสีฟ้า' must split into ['stl', 'สีฟ้า'] to align with
# catalogue's 'STL ด้ามสีฟ้า'.
_SCRIPT_BOUNDARY_RE = re.compile(
    r'(?<=[A-Za-z])(?=[฀-๿])|(?<=[฀-๿])(?=[A-Za-z])'
)


def _normalize(s):
    if s is None:
        return ""
    s = str(s).strip()
    s = _BRACKET_RE.sub(" ", s)
    s = s.replace('"', "นิ้ว").replace("”", "นิ้ว").replace("“", "นิ้ว")
    s = s.replace(" ", " ").replace("　", " ")
    s = " ".join(s.split())
    s = s.lower()
    return s


def _tokenize(s):
    out = []
    for raw in _TOKEN_SPLIT_RE.split(_normalize(s)):
        if not raw:
            continue
        for part in _SCRIPT_BOUNDARY_RE.split(raw):
            if part:
                out.append(part)
    return out


# BSN abbreviates units to 2-char tokens (first letter + last consonant of the
# canonical Thai word). The catalogue uses the full word. We expand BSN abbrevs
# to canonical so they can be compared directly.
# Source: hand-curated from `unit_conversions` ratio=1.0 most-common pairings,
# plus the rest derived by hardware-domain knowledge.
# Many BSN abbreviations are unambiguous (หด → หลอด).
# A few (หล) collide between multiple canonical units. List all plausible
# expansions; _unit_score takes the best match.
_BSN_UNIT_TO_CANONICAL = {
    'หด': ['หลอด'],
    'มน': ['ม้วน'],
    'อน': ['อัน'],
    'ชด': ['ชุด'],
    'ลก': ['ลูก'],
    'ตว': ['ตัว'],
    'ดก': ['ดอก'],
    'ผน': ['ผืน'],
    'คน': ['คัน'],
    'ขด': ['ขีด'],
    'ผง': ['แผง'],
    'สน': ['เส้น'],
    'แพ': ['แผ่น', 'ห่อ', 'แพ็ค'],   # ambiguous: pack, sheet, or bundle depending on product
    'กป': ['กระป๋อง'],
    'กล': ['กล.เล็ก', 'กล.ใหญ่'],
    'กก': ['กก.'],
    'หล': ['หลอด', 'โหล', 'อัน'],   # ambiguous in BSN data
    'กร': ['กุรุส', 'กระป๋อง'],
}


def _canonical_units(u):
    """Return a list of plausible canonical units for a (possibly abbreviated)
    unit string. Order: explicit-alias hits first, then the original input as
    fallback (handles already-canonical words like 'หลอด')."""
    if not u:
        return []
    u = str(u).strip().lstrip('!').strip()
    if not u:
        return []
    aliases = _BSN_UNIT_TO_CANONICAL.get(u, [])
    return list(aliases) + [u] if aliases else [u]


def _unit_score(purchase_unit, catalogue_unit):
    """1.0 = any canonical interpretation matches exactly;
    0.5 = substring relation; 0.0 = no overlap."""
    if not purchase_unit or not catalogue_unit:
        return 0.0
    pus = _canonical_units(purchase_unit)
    cus = _canonical_units(catalogue_unit)
    if set(pus) & set(cus):
        return 1.0
    for pu in pus:
        for cu in cus:
            if pu and cu and (pu in cu or cu in pu):
                return 0.5
    return 0.0


def _price_score(purchase_price, list_price, net_cash_price):
    """Compares purchase_price against list_price and net_cash_price (the two
    most likely matches in BSN data) and returns the closest fit on 0..1.
    Tolerance: 1% → score 1.0, 5% → 0.6, 15% → 0.0 (linear in between)."""
    if not purchase_price:
        return 0.0
    best = 0.0
    for ref in (list_price, net_cash_price):
        if not ref or ref <= 0:
            continue
        delta = abs(purchase_price - ref) / ref
        if delta <= 0.01:
            s = 1.0
        elif delta <= 0.05:
            s = 1.0 - (delta - 0.01) / 0.04 * 0.4   # 1.0 → 0.6
        elif delta <= 0.15:
            s = 0.6 - (delta - 0.05) / 0.10 * 0.6   # 0.6 → 0.0
        else:
            s = 0.0
        if s > best:
            best = s
    return best


def _suggest_candidates(conn, supplier_id, query_text,
                        purchase_unit=None, purchase_price=None, limit=15):
    """Rank catalogue items by combined name + unit + price similarity.

    Filter rule: include any candidate where name overlaps OR unit matches
    OR price is close — even with zero-token overlap a strong unit+price hit
    is enough to surface (this is what unblocks compound-word names like
    'กาวซิลิโคน BROVO' vs catalogue 'ซิลิโคน BRAVO').
    """
    q_tokens = set(_tokenize(query_text))
    if not q_tokens and not purchase_unit and not purchase_price:
        return []

    rows = conn.execute(
        """
        SELECT id, name_raw, name_normalized, name_tokens, sheet_name, unit,
               list_price, net_cash_price, price_change_flag
        FROM supplier_catalogue_items
        WHERE supplier_id = ? AND is_active = 1
        """,
        (supplier_id,),
    ).fetchall()

    scored = []
    for r in rows:
        # Re-tokenize from name_raw so query and candidates use the same rules
        # (DB-stored name_tokens may be from an older tokenizer).
        tokens = set(_tokenize(r['name_raw']))

        # Name (Jaccard)
        name_score = 0.0
        if q_tokens and tokens:
            inter = len(tokens & q_tokens)
            uni = len(tokens | q_tokens)
            if uni:
                name_score = inter / uni

        u_score = _unit_score(purchase_unit, r['unit'])
        p_score = _price_score(purchase_price, r['list_price'], r['net_cash_price'])

        # Filter: require *some* signal. Pure 0/0/0 → drop.
        if name_score == 0 and u_score == 0 and p_score == 0:
            continue

        # Combined: name carries weight but unit+price together can rescue a
        # zero-token candidate (max combined of unit+price alone = 0.5).
        combined = 0.5 * name_score + 0.25 * u_score + 0.25 * p_score

        scored.append({
            'id': r['id'],
            'name_raw': r['name_raw'],
            'sheet_name': r['sheet_name'],
            'unit': r['unit'],
            'list_price': r['list_price'],
            'net_cash_price': r['net_cash_price'],
            'price_change_flag': r['price_change_flag'],
            'score': round(combined, 3),
            'name_score': round(name_score, 3),
            'unit_score': round(u_score, 3),
            'price_score': round(p_score, 3),
        })
    scored.sort(key=lambda x: (-x['score'], -x['name_score'], -len(x['name_raw'])))
    return scored[:limit]


def _supplier_or_404(conn, supplier_id):
    row = conn.execute("SELECT * FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
    if not row:
        abort(404)
    return row


# ── Routes ────────────────────────────────────────────────────────────────────

@bp_supplier_catalogue.route('/')
def supplier_catalogue_list():
    conn = get_connection()
    suppliers = conn.execute(
        """
        SELECT s.id, s.name, s.display_name, s.is_active,
          (SELECT COUNT(*) FROM supplier_catalogue_items i WHERE i.supplier_id = s.id) AS n_items,
          (SELECT COUNT(*) FROM supplier_catalogue_items i WHERE i.supplier_id = s.id AND i.price_change_flag = 'changed') AS n_changed,
          (SELECT COUNT(*) FROM supplier_catalogue_items i WHERE i.supplier_id = s.id AND i.price_change_flag = 'new') AS n_new,
          (SELECT COUNT(DISTINCT product_name_raw) FROM purchase_transactions WHERE supplier = s.name) AS n_purchased,
          (SELECT COUNT(*) FROM supplier_product_mapping m WHERE m.supplier_id = s.id) AS n_mapped
        FROM suppliers s
        ORDER BY s.is_active DESC, s.name
        """
    ).fetchall()
    conn.close()
    return render_template('supplier_catalogue/list.html', suppliers=suppliers)


@bp_supplier_catalogue.route('/<int:supplier_id>/purchased')
def supplier_catalogue_purchased(supplier_id):
    """Primary view: list distinct purchased product_name_raw values from this
    supplier, joined with their existing mapping (if any)."""
    conn = get_connection()
    supplier = _supplier_or_404(conn, supplier_id)

    search = (request.args.get('q') or '').strip()
    show = request.args.get('show', 'all')  # all|unmapped|mapped

    sql_filters = ["pt.supplier = ?"]
    params = [supplier['name']]
    if search:
        sql_filters.append("LOWER(pt.product_name_raw) LIKE ?")
        params.append(f"%{search.lower()}%")

    rows = conn.execute(
        f"""
        SELECT
          pt.product_name_raw AS name,
          COUNT(*) AS n_lines,
          MAX(pt.date_iso) AS last_seen,
          MAX(pt.unit_price) AS last_unit_price,
          MAX(pt.unit) AS last_unit,
          (
            SELECT m.id
              FROM supplier_product_mapping m
              WHERE m.supplier_id = ?
                AND m.purchase_name_raw = pt.product_name_raw
                AND m.is_ignored = 0
              LIMIT 1
          ) AS mapping_id,
          (
            SELECT m.catalogue_item_id
              FROM supplier_product_mapping m
              WHERE m.supplier_id = ?
                AND m.purchase_name_raw = pt.product_name_raw
                AND m.is_ignored = 0
              LIMIT 1
          ) AS mapped_catalogue_item_id,
          (
            SELECT m.product_id
              FROM supplier_product_mapping m
              WHERE m.supplier_id = ?
                AND m.purchase_name_raw = pt.product_name_raw
                AND m.is_ignored = 0
              LIMIT 1
          ) AS mapped_product_id,
          (
            SELECT i.name_raw
              FROM supplier_product_mapping m
              JOIN supplier_catalogue_items i ON i.id = m.catalogue_item_id
              WHERE m.supplier_id = ?
                AND m.purchase_name_raw = pt.product_name_raw
                AND m.is_ignored = 0
              LIMIT 1
          ) AS mapped_catalogue_name
        FROM purchase_transactions pt
        WHERE {' AND '.join(sql_filters)}
        GROUP BY pt.product_name_raw
        ORDER BY n_lines DESC, last_seen DESC
        """,
        [supplier_id, supplier_id, supplier_id, supplier_id] + params,
    ).fetchall()

    if show == 'unmapped':
        rows = [r for r in rows if r['mapping_id'] is None]
    elif show == 'mapped':
        rows = [r for r in rows if r['mapping_id'] is not None]

    conn.close()
    return render_template(
        'supplier_catalogue/purchased.html',
        supplier=supplier, rows=rows, search=search, show=show,
    )


@bp_supplier_catalogue.route('/<int:supplier_id>/match')
def supplier_catalogue_match(supplier_id):
    """Per-item match view: takes one purchase_name_raw and shows top-N
    catalogue suggestions ranked by token overlap. User picks one + submits."""
    purchase_name = (request.args.get('name') or '').strip()
    back_q = (request.args.get('back_q') or '').strip()
    if not purchase_name:
        return redirect(url_for('supplier_catalogue.supplier_catalogue_purchased',
                                supplier_id=supplier_id))

    conn = get_connection()
    supplier = _supplier_or_404(conn, supplier_id)

    summary = conn.execute(
        """
        SELECT COUNT(*) AS n_lines, MAX(date_iso) AS last_seen,
               MAX(unit_price) AS last_unit_price, MAX(unit) AS last_unit,
               MIN(date_iso) AS first_seen
        FROM purchase_transactions
        WHERE supplier = ? AND product_name_raw = ?
        """,
        (supplier['name'], purchase_name),
    ).fetchone()

    suggestions = _suggest_candidates(
        conn, supplier_id, purchase_name,
        purchase_unit=summary['last_unit'] if summary else None,
        purchase_price=summary['last_unit_price'] if summary else None,
        limit=15,
    )

    existing = conn.execute(
        """
        SELECT m.id AS mapping_id, m.catalogue_item_id, m.is_ignored, m.note,
               i.name_raw AS catalogue_name
        FROM supplier_product_mapping m
        LEFT JOIN supplier_catalogue_items i ON i.id = m.catalogue_item_id
        WHERE m.supplier_id = ? AND m.purchase_name_raw = ?
        """,
        (supplier_id, purchase_name),
    ).fetchone()

    conn.close()
    return render_template(
        'supplier_catalogue/match.html',
        supplier=supplier, purchase_name=purchase_name, summary=summary,
        suggestions=suggestions, existing=existing, back_q=back_q,
    )


@bp_supplier_catalogue.route('/<int:supplier_id>/suggest')
def supplier_catalogue_suggest(supplier_id):
    """JSON endpoint for live mapping suggestions."""
    q = (request.args.get('q') or '').strip()
    unit = (request.args.get('unit') or '').strip() or None
    price = request.args.get('price', type=float)
    if not q and not unit and not price:
        return jsonify([])
    conn = get_connection()
    suggestions = _suggest_candidates(
        conn, supplier_id, q,
        purchase_unit=unit, purchase_price=price, limit=10,
    )
    conn.close()
    return jsonify(suggestions)


@bp_supplier_catalogue.route('/<int:supplier_id>/mapping/save', methods=['POST'])
def supplier_catalogue_mapping_save(supplier_id):
    """Save (or replace) the mapping for one purchase_name_raw."""
    purchase_name = (request.form.get('purchase_name_raw') or '').strip()
    catalogue_item_id = request.form.get('catalogue_item_id', type=int)
    is_ignored = 1 if request.form.get('is_ignored') == '1' else 0
    note = (request.form.get('note') or '').strip() or None

    if not purchase_name:
        flash('ไม่พบรายการที่ต้องการผูก', 'danger')
        return redirect(url_for('supplier_catalogue.supplier_catalogue_purchased', supplier_id=supplier_id))

    if not is_ignored and not catalogue_item_id:
        flash('กรุณาเลือกรายการในแคตตาล็อกก่อน', 'danger')
        return redirect(url_for('supplier_catalogue.supplier_catalogue_purchased',
                                supplier_id=supplier_id, q=purchase_name))

    conn = get_connection()
    _supplier_or_404(conn, supplier_id)

    # Validate catalogue_item_id belongs to this supplier (if provided)
    if catalogue_item_id:
        chk = conn.execute(
            "SELECT id FROM supplier_catalogue_items WHERE id = ? AND supplier_id = ?",
            (catalogue_item_id, supplier_id),
        ).fetchone()
        if not chk:
            conn.close()
            flash('รายการในแคตตาล็อกไม่ถูกต้อง', 'danger')
            return redirect(url_for('supplier_catalogue.supplier_catalogue_purchased',
                                    supplier_id=supplier_id, q=purchase_name))

    # Replace any existing mapping for this (supplier, purchase_name)
    existing = conn.execute(
        """
        SELECT id FROM supplier_product_mapping
        WHERE supplier_id = ? AND purchase_name_raw = ?
        """,
        (supplier_id, purchase_name),
    ).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE supplier_product_mapping SET
              catalogue_item_id = ?, is_ignored = ?, note = ?,
              confidence = 'manual',
              updated_at = datetime('now','localtime')
            WHERE id = ?
            """,
            (catalogue_item_id, is_ignored, note, existing['id']),
        )
    else:
        conn.execute(
            """
            INSERT INTO supplier_product_mapping
              (supplier_id, catalogue_item_id, purchase_name_raw, is_ignored,
               confidence, note, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'manual', ?,
                    datetime('now','localtime'), datetime('now','localtime'))
            """,
            (supplier_id, catalogue_item_id, purchase_name, is_ignored, note),
        )
    conn.commit()
    conn.close()

    if is_ignored:
        flash(f'ข้ามการผูก: {purchase_name}', 'info')
    else:
        flash(f'ผูกแล้ว: {purchase_name}', 'success')
    return redirect(url_for('supplier_catalogue.supplier_catalogue_purchased',
                            supplier_id=supplier_id, q=request.form.get('back_q', '')))


@bp_supplier_catalogue.route('/<int:supplier_id>/mapping/<int:mapping_id>/delete', methods=['POST'])
def supplier_catalogue_mapping_delete(supplier_id, mapping_id):
    conn = get_connection()
    row = conn.execute(
        "SELECT purchase_name_raw FROM supplier_product_mapping WHERE id = ? AND supplier_id = ?",
        (mapping_id, supplier_id),
    ).fetchone()
    if not row:
        conn.close()
        abort(404)
    conn.execute("DELETE FROM supplier_product_mapping WHERE id = ?", (mapping_id,))
    conn.commit()
    conn.close()
    flash(f'ยกเลิกการผูก: {row["purchase_name_raw"]}', 'success')
    return redirect(url_for('supplier_catalogue.supplier_catalogue_purchased',
                            supplier_id=supplier_id, q=request.form.get('back_q', '')))
