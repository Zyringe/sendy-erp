"""Promotions + price tiers — extracted verbatim from models.py
(behavior-preserving split, Phase 11) — see models/__init__.py's module
docstring for the overall file-split rationale. No behavior changes.
"""
import bsn_units
from database import get_connection
from datetime import date


def get_promotions(product_id: int, active_only=False, conn=None):
    owned = conn is None
    if owned:
        conn = get_connection()
    try:
        cond = "WHERE product_id = ?"
        params = [product_id]
        if active_only:
            cond += " AND is_active = 1"
        return conn.execute(
            f"SELECT * FROM promotions {cond} ORDER BY created_at DESC", params
        ).fetchall()
    finally:
        if owned:
            conn.close()


def get_active_promotion(product_id: int, conn=None):
    today = date.today().isoformat()
    owned = conn is None
    if owned:
        conn = get_connection()
    try:
        return conn.execute("""
            SELECT * FROM promotions
            WHERE product_id = ? AND is_active = 1
              AND (date_start IS NULL OR date_start <= ?)
              AND (date_end IS NULL OR date_end >= ?)
            ORDER BY created_at DESC
            LIMIT 1
        """, (product_id, today, today)).fetchone()
    finally:
        if owned:
            conn.close()


def affects_price(promo) -> bool:
    """Does this promo change the per-unit price? The Python twin of
    promo_slot_sql()'s price_expr.

    `percent` and `fixed` always do. A `mixed` row does exactly when it carries
    a discount_value: the promotions CHECK forbids one on `bundle`/`gift` and
    allows any combination on `mixed` ("At least one structured field
    populated"), so a discount_value on a mixed row is a real percent — the
    same reading migration 177 renders into a DB trigger and the promo form
    offers as "ผสม (% + แถม / + ของแถม)".

    Callers that need the price itself want promo_price(); this predicate is
    for callers that must distinguish "no price effect" from "price unchanged"
    (review_rules' R5 skips the former). tests/test_promo_price_owner.py pins
    it against the SQL spelling over every row in the table, because two
    spellings of one rule is the defect this pair of functions exists to end.
    """
    if promo is None:
        return False
    promo_type = promo['promo_type']
    if promo_type in ('percent', 'fixed'):
        return True
    if promo_type == 'mixed':
        return promo['discount_value'] is not None
    return False


# promo_gate statuses under which a price promo's effect applies (#673).
GATE_APPLIES = ('none', 'met')


def promo_gate(promo, *, qty_pieces, min_pieces):
    """Does `promo`'s minimum quantity (#673, mig 199) let its price effect
    apply to this ask? Pure — the caller measures both sides first with
    price_lookup.promo_min_measure (price_lookup.measure_ratio, the one
    measurement contract), so `qty_pieces` and `min_pieces` are in ONE measure.
    Sites with a connection call price_lookup.gate_for_ask.

      'none'          no minimum (or no price effect at all): applies
      'met'           asked >= minimum, compared at 4 dp: applies to the WHOLE line
      'not_met'       asked < minimum
      'unconvertible' the minimum's unit has no ratio for this product
      'qty_unknown'   no quantity, or the asked unit has no ratio
      'missing'       a ยกลัง/ยกล่อง label with no number (Put 2026-10-02: never a
                      valid gated promo; fail closed)

    Only GATE_APPLIES lets promo_price apply the promo; every other status
    answers the list price. Never degrades an unknown ratio to 1."""
    if promo is None or not affects_price(promo):
        return 'none'
    if promo['min_qty'] is None:
        return 'missing' if promo['bundle_condition'] else 'none'
    if min_pieces is None:
        return 'unconvertible'
    if qty_pieces is None:
        return 'qty_unknown'
    return 'met' if round(qty_pieces, 4) >= round(min_pieces, 4) else 'not_met'


def promo_price(list_for_unit, ratio, promo, *, qty_pieces, min_pieces):
    """THE promo→price application for the whole app. Given a list price for
    some unit and that unit's piece-ratio, return the price after `promo`.

    `qty_pieces` / `min_pieces` are REQUIRED keywords (#673): a call site that
    never thought about quantity is a TypeError, not a silent discount. They go
    to promo_gate; a caller with no quantity passes None and a gated promo
    answers the list.

      - a gated promo whose gate is not GATE_APPLIES → list_for_unit unchanged
      - no promo, or a promo with no price effect (bundle / gift, and a mixed
        row carrying only deal terms) → list_for_unit unchanged
      - 'fixed' → discount_value × ratio (fixed IS the final per-PIECE price).
        When `ratio is None` — a tier answers the price but no piece-ratio is
        derivable — that conversion cannot be computed, so the promo is left
        unapplied rather than guessed.
      - 'percent', and a 'mixed' row carrying a discount_value → list ×
        (1 − d/100), rounded 2dp. This branch never needs `ratio`.

    Pure — no DB. models.effective_price is the one caller that cannot import
    price_lookup (models sits under it); every other caller reaches it
    directly (#673 removed price_lookup.apply_price_promo, a pass-through).
    """
    if promo is None:
        return list_for_unit
    if promo_gate(promo, qty_pieces=qty_pieces, min_pieces=min_pieces) not in GATE_APPLIES:
        return list_for_unit
    return promo_price_if_met(list_for_unit, ratio, promo)


def promo_price_if_met(list_for_unit, ratio, promo):
    """The price `promo` WOULD give if its minimum were met — the ungated math
    promo_price applies once the gate allows it. For detection/display only
    (R5's "discount given below the minimum"); never a quote."""
    if promo is None:
        return list_for_unit
    if promo['promo_type'] == 'fixed':
        if ratio is None:
            return list_for_unit
        return round(promo['discount_value'] * ratio, 2)
    d = promo['discount_value']
    if d is None:
        return list_for_unit
    return round(list_for_unit * (1 - d / 100), 2)


def effective_price(product, conn=None) -> float:
    """Return the effective per-unit selling price for this product, i.e. the
    catalog price after whatever promo occupies its PRICE slot today.

    Two defects lived here until 2026-09-09 (card 3 of the 2026-09-08
    architecture review), both because this function answered "what does a
    promo do to the price?" itself instead of asking the owner:

    1. it returned base_sell_price for every `mixed` row, so 27 own-brand
       products on prod rendered a price 10-20% above the one the quote
       resolver charged the customer for the same SKU;
    2. it picked its promo with get_active_promotion() — newest row across ALL
       types — so a later bundle/gift promo shadowed an earlier percent one and
       the discount vanished. Measured 0 products affected on prod 2026-09-08,
       but migration 177 permits one promo per slot, i.e. both at once.

    Now: select the price-slot promo, then hand it to promo_price(). Both
    steps are the app's single definition, shared with price_lookup and the
    mig-177 trigger.

    No quantity here (#673): a promo with a minimum is not this price.
    """
    owned = conn is None
    if owned:
        conn = get_connection()
    try:
        price_promo, _qty_promo = get_active_promos_by_class(
            product['id'], date.today().isoformat(), conn)
        return promo_price(product['base_sell_price'], 1.0, price_promo,
                           qty_pieces=None, min_pieces=None)
    finally:
        if owned:
            conn.close()


def min_qty_problem(conn, product_id, data):
    """Why `data`'s minimum quantity (#673, mig 199) cannot be saved on this
    product, or None. Every promotion writer asks this first (#673 fix round:
    the route was the only gate). The DB CHECK is the final gate; this names
    the problem and refuses what the CHECK cannot see: a unit with no ratio
    for this product, and a ยกลัง/ยกล่อง label with no number (Put 2026-10-02).
    """
    import math
    import price_lookup   # here, not at module level: price_lookup imports this module
    # validate the word create/replace will STORE, not the raw spelling typed:
    # 'หล' with its own row could pass while the stored 'โหล' cannot be measured
    raw_unit = (data.get('min_qty_unit') or '').strip()
    min_qty = data.get('min_qty')
    min_unit = bsn_units.normalize_unit(raw_unit, conn=conn) if raw_unit else None
    shaped = {'promo_type': data.get('promo_type'), 'discount_value': data.get('discount_value')}
    if min_qty is None and min_unit is None:
        if data.get('bundle_condition') and affects_price(shaped):
            return (f'เงื่อนไข "{data["bundle_condition"]}" ต้องระบุจำนวนขั้นต่ำ '
                    f'(จำนวน + หน่วย) ไม่อย่างนั้นระบบใช้โปรนี้ไม่ได้')
        return None
    if min_qty is None or min_unit is None:
        return 'ระบุจำนวนขั้นต่ำและหน่วยให้ครบทั้งคู่ (หรือเว้นว่างทั้งคู่)'
    try:
        min_qty = float(min_qty)
    except (TypeError, ValueError):
        return 'จำนวนขั้นต่ำต้องเป็นตัวเลข'
    if not (math.isfinite(min_qty) and min_qty > 0):
        return 'จำนวนขั้นต่ำต้องมากกว่า 0'
    if not affects_price(shaped) or data.get('bundle_buy') is not None or data.get('gift_desc'):
        return 'จำนวนขั้นต่ำใช้ได้กับโปรลดราคาเท่านั้น (ลด % / ราคาพิเศษ) ไม่ใช้กับโปรแถม'
    row = conn.execute("SELECT unit_type FROM products WHERE id = ?", (product_id,)).fetchone()
    if row is None:
        return None   # the products FK refuses the insert anyway
    return price_lookup.min_qty_unit_problem(conn, product_id, row['unit_type'], min_unit)


def create_promotion(data: dict) -> int:
    """Insert a promotions row. Accepts any subset of the extended fields
    introduced in mig 086 (bundle_*, gift_*). Missing keys default to None
    so the DB's CHECK constraint enforces shape per promo_type.

    Required keys: product_id, promo_name, promo_type.
    Optional: discount_value, date_start, date_end, bundle_buy, bundle_free,
              bundle_unit, bundle_condition, bundle_tiers_json,
              gift_desc, gift_qty, min_qty, min_qty_unit (#673; the unit is
              stored as its หน่วย word, like bundle_unit). An invalid
              minimum raises ValueError (min_qty_problem) before any write.
    """
    conn = get_connection()
    problem = min_qty_problem(conn, data["product_id"], data)
    if problem:
        conn.close()
        raise ValueError(problem)
    full = {
        "product_id":        data["product_id"],
        "promo_name":        data["promo_name"],
        "promo_type":        data["promo_type"],
        "discount_value":    data.get("discount_value"),
        "date_start":        data.get("date_start"),
        "date_end":          data.get("date_end"),
        "bundle_buy":        data.get("bundle_buy"),
        "bundle_free":       data.get("bundle_free"),
        "bundle_unit":       bsn_units.normalize_unit(
            (data.get("bundle_unit") or "").strip(), conn=conn) or None,
        "bundle_condition":  data.get("bundle_condition"),
        "bundle_tiers_json": data.get("bundle_tiers_json"),
        "gift_desc":         data.get("gift_desc"),
        "gift_qty":          data.get("gift_qty"),
        "min_qty":           data.get("min_qty"),
        "min_qty_unit":      bsn_units.normalize_unit(
            (data.get("min_qty_unit") or "").strip(), conn=conn) or None,
    }
    try:
        cur = conn.execute("""
            INSERT INTO promotions (
                product_id, promo_name, promo_type, discount_value,
                date_start, date_end,
                bundle_buy, bundle_free, bundle_unit, bundle_condition,
                bundle_tiers_json, gift_desc, gift_qty, min_qty, min_qty_unit
            ) VALUES (
                :product_id, :promo_name, :promo_type, :discount_value,
                :date_start, :date_end,
                :bundle_buy, :bundle_free, :bundle_unit, :bundle_condition,
                :bundle_tiers_json, :gift_desc, :gift_qty, :min_qty, :min_qty_unit
            )
        """, full)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def deactivate_promotion(promo_id: int, today=None, conn=None):
    """The 'ปิดเลย' button — the only path that sets is_active = 0. Also
    stamps date_end = today when it was NULL (task-2-brief.md 2a), so a
    deactivated promo has a real closing date for history/is_current
    purposes instead of relying on is_active alone."""
    today = today or date.today().isoformat()
    owned = conn is None
    if owned:
        conn = get_connection()
    try:
        conn.execute(
            "UPDATE promotions SET is_active = 0, "
            "date_end = COALESCE(date_end, ?) WHERE id = ?",
            (today, promo_id),
        )
        conn.commit()
    finally:
        if owned:
            conn.close()


def is_current(row, day):
    """The shared vocabulary (task-2-brief.md 2a): is_active = 1 AND
    (date_start IS NULL OR date_start <= day) AND (date_end IS NULL OR
    date_end >= day). Used by the route (2d), the resolver (R2, already
    inline in get_active_promotion / get_active_promos_by_class above),
    the stale flag (R6) and the DB trigger (2b, same predicate rendered
    in SQL) — never bare is_active.

    `row` is a sqlite3.Row or dict carrying is_active/date_start/date_end.
    """
    if not row['is_active']:
        return False
    if row['date_start'] is not None and row['date_start'] > day:
        return False
    if row['date_end'] is not None and row['date_end'] < day:
        return False
    return True


def classify_promotions(rows, day):
    """Partition `rows` (e.g. from get_promotions) into (current, scheduled,
    closed) per is_current(row, day) — task-2-brief.md 2d. scheduled = not
    yet current because date_start is in the future (is_active still 1);
    closed = everything else (is_active = 0, or date_end has passed)."""
    current, scheduled, closed = [], [], []
    for row in rows:
        if is_current(row, day):
            current.append(row)
        elif row['is_active'] and row['date_start'] is not None and row['date_start'] > day:
            scheduled.append(row)
        else:
            closed.append(row)
    return current, scheduled, closed


def promo_slots_for(conn, promo_type, discount_value, bundle_buy, gift_desc):
    """Which slot(s) a (not-yet-inserted) row with these fields would
    occupy — evaluated by the SAME promo_slot_sql() expression the
    resolver (R2) and the mig-177 trigger use, against a throwaway 1-row
    derived table, so this can never drift from them. Needed by
    replace_promotion (2a) and the catalog importer (2c), which must know
    slot membership BEFORE the row exists (replace_promotion closes the
    old occupant of a slot BEFORE inserting the new row, or mig 177's
    overlap trigger sees a transient overlap and aborts).

    Returns (occupies_price: bool, occupies_qty: bool).
    """
    price_expr, qty_expr = promo_slot_sql('')
    row = conn.execute(f"""
        SELECT ({price_expr}) AS price_slot, ({qty_expr}) AS qty_slot
        FROM (SELECT ? AS promo_type, ? AS discount_value,
                     ? AS bundle_buy, ? AS gift_desc)
    """, (promo_type, discount_value, bundle_buy, gift_desc)).fetchone()
    return bool(row['price_slot']), bool(row['qty_slot'])


def _overlapping_promos(conn, product_id, new_start, new_end,
                        occupies_price, occupies_qty):
    """Active promos on `product_id` whose date window OVERLAPS
    [new_start, new_end] in a slot the incoming row occupies.

    The overlap predicate is byte-identical in intent to migration 177's
    trigger (`COALESCE` to open-ended sentinels), so the model and the DB
    guard can never disagree about what "already occupied" means:
        p.date_start <= new_end  AND  new_start <= p.date_end
    Asking `get_active_promos_by_class(pid, today)` instead — "what is
    current TODAY" — is the bug this replaced: a row whose window starts
    AFTER today is invisible to it, so a scheduled promo survived and
    stacked (task-2a-review.md, BLOCKER 1).
    """
    if not (occupies_price or occupies_qty):
        return []
    price_expr, qty_expr = promo_slot_sql('')
    if occupies_price and occupies_qty:
        slot_clause = f"({price_expr} OR {qty_expr})"
    else:
        slot_clause = price_expr if occupies_price else qty_expr
    return conn.execute(f"""
        SELECT * FROM promotions
         WHERE product_id = ? AND is_active = 1
           AND COALESCE(date_start, '0000-01-01') <= COALESCE(?, '9999-12-31')
           AND ? <= COALESCE(date_end, '9999-12-31')
           AND {slot_clause}
         ORDER BY id
    """, (product_id, new_end, new_start)).fetchall()


def replace_promotion(product_id, data, today, conn=None, cancel_conflicts=False):
    """Create promotion `data` for product_id, closing whatever occupies the
    same slot(s) over the same dates — task-2-brief.md 2a. Change = close old
    (by DATE, keeping is_active = 1) + open new; never flips is_active early
    (that would leave a gap where every quote falls back to list price when
    new_start is in the future).

    Two kinds of occupant, split by whether closing them by date is even
    expressible:
      * starts BEFORE new_start → closed with `date_end = new_start - 1`,
        `is_active` untouched. This is the ordinary replace.
      * starts ON or AFTER new_start (a SCHEDULED promo) → its whole window
        sits inside the new one, so there is no date to close it at. Refused
        by default, naming the conflict, writing NOTHING — unless the caller
        passes `cancel_conflicts=True` (the operator ticked
        "ยกเลิกโปรที่ตั้งเวลาไว้"), which deactivates it the same way the
        "ปิดเลย" button does. Ruling: Put, 2026-08-27.

    Returns (ok: bool, message: str, new_id: int | None). `ok is False`
    means refused and NOTHING was written.

    `today` must be passed by the caller (ISO date string) — this
    function does no wall-clock read, so callers/tests can pin it.

    ⚠ Opens its own `BEGIN IMMEDIATE` and commits: it CANNOT be called with a
    `conn` that is already inside a transaction (raises OperationalError), and
    it commits whatever else that connection had pending. See task-2a-review.md
    MINOR 5 before wiring it into the 2c importer.
    """
    owned = conn is None
    if owned:
        conn = get_connection()
    try:
        new_start = data.get('date_start') or today
        if new_start < today:
            return False, 'ไม่สามารถตั้งวันเริ่มโปรย้อนหลังได้ (การย้อนวันจะเขียนทับหลักฐานราคาที่ผ่านมา)', None
        new_end = data.get('date_end')

        problem = min_qty_problem(conn, product_id, data)
        if problem:
            return False, problem, None

        occupies_price, occupies_qty = promo_slots_for(
            conn, data['promo_type'], data.get('discount_value'),
            data.get('bundle_buy'), data.get('gift_desc'))

        conn.execute("BEGIN IMMEDIATE")
        try:
            overlapping = _overlapping_promos(
                conn, product_id, new_start, new_end, occupies_price, occupies_qty)
            closeable, conflicts = [], []
            for row in overlapping:
                start = row['date_start'] or '0000-01-01'
                (closeable if start < new_start else conflicts).append(row)

            if conflicts and not cancel_conflicts:
                conn.rollback()
                names = ', '.join(
                    f'"{r["promo_name"]}" เริ่ม {r["date_start"]}' for r in conflicts)
                return False, (
                    f'มีโปรโมชันที่ตั้งเวลาไว้ทับช่วงนี้อยู่แล้ว: {names} — '
                    f'ติ๊ก "ยกเลิกโปรที่ตั้งเวลาไว้" เพื่อยกเลิกใบนั้นแล้วบันทึกใบนี้แทน'
                ), None

            # Order matters: cancel first, then date-close, then insert — so
            # mig 177's trigger never sees a transient overlap mid-statement.
            for row in conflicts:
                conn.execute(
                    "UPDATE promotions SET is_active = 0, "
                    "date_end = COALESCE(date_end, ?) WHERE id = ?",
                    (today, row['id']))

            day_before = _add_days(new_start, -1)
            for row in closeable:
                conn.execute(
                    "UPDATE promotions SET date_end = ? WHERE id = ?", (day_before, row['id']))

            full = {
                "product_id":        product_id,
                "promo_name":        data["promo_name"],
                "promo_type":        data["promo_type"],
                "discount_value":    data.get("discount_value"),
                "date_start":        new_start,
                "date_end":          new_end,
                "bundle_buy":        data.get("bundle_buy"),
                "bundle_free":       data.get("bundle_free"),
                "bundle_unit":       bsn_units.normalize_unit(
                    (data.get("bundle_unit") or "").strip(), conn=conn) or None,
                "bundle_condition":  data.get("bundle_condition"),
                "bundle_tiers_json": data.get("bundle_tiers_json"),
                "gift_desc":         data.get("gift_desc"),
                "gift_qty":          data.get("gift_qty"),
                "min_qty":           data.get("min_qty"),
                "min_qty_unit":      bsn_units.normalize_unit(
                    (data.get("min_qty_unit") or "").strip(), conn=conn) or None,
                "source":            "manual",
            }
            cur = conn.execute("""
                INSERT INTO promotions (
                    product_id, promo_name, promo_type, discount_value,
                    date_start, date_end,
                    bundle_buy, bundle_free, bundle_unit, bundle_condition,
                    bundle_tiers_json, gift_desc, gift_qty, min_qty, min_qty_unit, source
                ) VALUES (
                    :product_id, :promo_name, :promo_type, :discount_value,
                    :date_start, :date_end,
                    :bundle_buy, :bundle_free, :bundle_unit, :bundle_condition,
                    :bundle_tiers_json, :gift_desc, :gift_qty, :min_qty, :min_qty_unit,
                    :source
                )
            """, full)
            new_id = cur.lastrowid
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        cancelled = ' (ยกเลิกโปรที่ตั้งเวลาไว้แล้ว)' if (cancel_conflicts and conflicts) else ''
        return True, f'บันทึกโปรโมชันเรียบร้อย{cancelled}', new_id
    finally:
        if owned:
            conn.close()


def _add_days(iso_date, n):
    from datetime import timedelta
    return (date.fromisoformat(iso_date) + timedelta(days=n)).isoformat()


def promo_slot_sql(alias):
    """Return (price_expr, qty_expr) — the two independent promo-class
    predicates, every column prefixed with `alias.` (pass '' for an
    unaliased FROM, 'NEW' for a trigger body).

    A `mixed` row can satisfy BOTH slots at once (discount_value set AND
    bundle_buy/gift_desc set) — the two slots are selected independently
    by the caller, not mutually exclusive. See price_lookup.py R2.
    """
    p = f'{alias}.' if alias else ''
    price_expr = (
        f"({p}promo_type IN ('percent','fixed') "
        f"OR ({p}promo_type = 'mixed' AND {p}discount_value IS NOT NULL))"
    )
    qty_expr = (
        f"({p}promo_type IN ('bundle','gift') "
        f"OR ({p}promo_type = 'mixed' AND ({p}bundle_buy IS NOT NULL OR {p}gift_desc IS NOT NULL)))"
    )
    return price_expr, qty_expr


def get_active_promos_by_class(product_id: int, on_date: str, conn):
    """Return (price_promo_row, qty_promo_row) for product_id, active on
    on_date — each slot selected independently (ORDER BY id DESC LIMIT 1
    among rows whose date window contains on_date), so a later-created
    bundle/gift promo can never shadow an earlier price promo (or vice
    versa) the way a single ORDER BY id DESC LIMIT 1 over all promo_types
    would. A `mixed` row satisfying both predicates is returned in both
    slots (see promo_slot_sql). Caller (price_lookup.resolve_price) owns
    the conn — this never opens/closes its own.
    """
    price_expr, qty_expr = promo_slot_sql('')
    price_promo = conn.execute(f"""
        SELECT * FROM promotions
        WHERE product_id = ? AND is_active = 1
          AND (date_start IS NULL OR date_start <= ?)
          AND (date_end IS NULL OR date_end >= ?)
          AND {price_expr}
        ORDER BY id DESC LIMIT 1
    """, (product_id, on_date, on_date)).fetchone()
    qty_promo = conn.execute(f"""
        SELECT * FROM promotions
        WHERE product_id = ? AND is_active = 1
          AND (date_start IS NULL OR date_start <= ?)
          AND (date_end IS NULL OR date_end >= ?)
          AND {qty_expr}
        ORDER BY id DESC LIMIT 1
    """, (product_id, on_date, on_date)).fetchone()
    return price_promo, qty_promo


def get_product_price_tiers(product_id: int, conn=None):
    """Return all tier rows for this product, ordered by sort_order then price."""
    owned = conn is None
    if owned:
        conn = get_connection()
    try:
        return conn.execute(
            "SELECT id, qty_label, price, note, sort_order "
            "FROM product_price_tiers "
            "WHERE product_id = ? "
            "ORDER BY sort_order, price",
            (product_id,)
        ).fetchall()
    finally:
        if owned:
            conn.close()