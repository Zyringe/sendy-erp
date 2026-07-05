"""Pack/unpack conversion-formula helpers — extracted verbatim from
models.py (behavior-preserving split, Phase 12) — see models/__init__.py's
module docstring for the overall file-split rationale. No behavior changes.

Imports `get_current_wacc` + `recalculate_waccs_for_products` from `.wacc`
(the brief's expected conversions->wacc edge).
"""

from database import get_connection

from .wacc import get_current_wacc, recalculate_waccs_for_products


def get_conversion_formulas():
    conn = get_connection()
    rows = conn.execute("""
        SELECT cf.id, cf.name, cf.output_product_id, cf.output_qty,
               cf.note, cf.is_active, cf.created_at,
               p.product_name AS output_product_name,
               p.unit_type    AS output_unit_type,
               COUNT(cfi.id)  AS input_count
          FROM conversion_formulas cf
          JOIN products p ON p.id = cf.output_product_id
          LEFT JOIN conversion_formula_inputs cfi ON cfi.formula_id = cf.id
         GROUP BY cf.id
         ORDER BY cf.is_active DESC, cf.name
    """).fetchall()
    conn.close()
    return rows


def get_conversion_formula(formula_id):
    conn = get_connection()
    formula = conn.execute("""
        SELECT cf.*, p.product_name AS output_product_name,
               p.unit_type AS output_unit_type,
               COALESCE(sl.quantity, 0) AS output_stock
          FROM conversion_formulas cf
          JOIN products p ON p.id = cf.output_product_id
          LEFT JOIN stock_levels sl ON sl.product_id = cf.output_product_id
         WHERE cf.id = ?
    """, (formula_id,)).fetchone()
    if not formula:
        conn.close()
        return None, []
    inputs = conn.execute("""
        SELECT cfi.id, cfi.product_id, cfi.quantity,
               p.product_name, p.unit_type,
               COALESCE(sl.quantity, 0) AS current_stock
          FROM conversion_formula_inputs cfi
          JOIN products p ON p.id = cfi.product_id
          LEFT JOIN stock_levels sl ON sl.product_id = cfi.product_id
         WHERE cfi.formula_id = ?
         ORDER BY cfi.id
    """, (formula_id,)).fetchall()
    conn.close()
    return formula, inputs


def get_buildable(product_ids=None, conn=None):
    """Pack/unpack 'true availability'. For each product that is the OUTPUT of
    one or more ACTIVE conversion formulas, compute how many EXTRA output units
    could be produced from CURRENT input stock — one level deep, no recursion:

        buildable(P) = Σ over active formulas f with output=P of
                       (min over inputs i of floor(stock(i) / i.quantity)) * f.output_qty

    Returns {product_id: {'buildable': int, 'output_stock': num,
             'true_available': num (= output_stock + buildable),
             'sources': [{'formula_id', 'name', 'output_qty', 'qty'}]}}
    for every product that is such an output (buildable may be 0). When
    product_ids is given, the result is restricted to that set. A tiny epsilon
    absorbs IEEE noise in trigger-maintained stock (verification-discipline).
    """
    own = conn is None
    if own:
        conn = get_connection()
    try:
        params = []
        filt = ""
        if product_ids is not None:
            ids = list(product_ids)
            if not ids:
                return {}
            filt = " AND cf.output_product_id IN (%s)" % ",".join("?" * len(ids))
            params = ids
        rows = conn.execute(f"""
            SELECT cf.id AS formula_id, cf.name, cf.output_product_id AS out_pid,
                   cf.output_qty,
                   COALESCE(slo.quantity, 0) AS output_stock,
                   cfi.quantity AS input_qty,
                   COALESCE(sli.quantity, 0) AS input_stock
              FROM conversion_formulas cf
              JOIN conversion_formula_inputs cfi ON cfi.formula_id = cf.id
              LEFT JOIN stock_levels slo ON slo.product_id = cf.output_product_id
              LEFT JOIN stock_levels sli ON sli.product_id = cfi.product_id
             WHERE cf.is_active = 1{filt}
        """, params).fetchall()
    finally:
        if own:
            conn.close()

    per_formula = {}
    for r in rows:
        f = per_formula.setdefault(r["formula_id"], {
            "name": r["name"], "out_pid": r["out_pid"],
            "output_qty": r["output_qty"], "output_stock": r["output_stock"],
            "factors": [],
        })
        iq = r["input_qty"]
        # floor(input_stock / input_qty); +1e-9 absorbs IEEE noise (e.g. 5.9999999999999 → 6)
        factor = int((r["input_stock"] + 1e-9) // iq) if iq and iq > 0 else 0
        f["factors"].append(factor)

    result = {}
    for fid, f in per_formula.items():
        qty = (min(f["factors"]) if f["factors"] else 0) * f["output_qty"]
        entry = result.setdefault(f["out_pid"], {
            "buildable": 0, "output_stock": f["output_stock"],
            "true_available": 0, "sources": [],
        })
        entry["buildable"] += qty
        entry["sources"].append({
            "formula_id": fid, "name": f["name"],
            "output_qty": f["output_qty"], "qty": qty,
        })
    for e in result.values():
        e["true_available"] = e["output_stock"] + e["buildable"]
    return result


def upsert_pack_unpack_pair(pack_id, loose_id, ratio, direction='both', note='', conn=None):
    """Create or update the conversion formula(s) for a pack↔loose pair, in one
    call (the /conversions pair-mode form). Idempotent — re-running updates the
    matching formula instead of duplicating.

        PACK   : output=pack_id,  output_qty=1,     inputs=[(loose_id, ratio)]
        UNPACK : output=loose_id, output_qty=ratio, inputs=[(pack_id, 1)]

    direction: 'both' | 'pack' | 'unpack'. Dedup key = (output_product_id,
    frozenset(input_product_ids)) over ACTIVE formulas. Returns
    {'created': int, 'updated': int, 'formula_ids': [...]}.
    """
    ratio = int(ratio)
    own = conn is None
    if own:
        conn = get_connection()
    try:
        def _pinfo(pid):
            r = conn.execute("SELECT product_name, unit_type FROM products WHERE id=?", (pid,)).fetchone()
            return (r["product_name"], r["unit_type"]) if r else (str(pid), "")
        pack_name, _pack_unit = _pinfo(pack_id)
        _loose_name, loose_unit = _pinfo(loose_id)

        specs = []
        if direction in ('both', 'pack'):
            specs.append(dict(name=f"[แพ็ค] {pack_name} ⟵ {ratio} {loose_unit}",
                              output_pid=pack_id, output_qty=1, inputs=[(loose_id, ratio)]))
        if direction in ('both', 'unpack'):
            specs.append(dict(name=f"[แกะ] {pack_name} ⟶ {ratio} {loose_unit}",
                              output_pid=loose_id, output_qty=ratio, inputs=[(pack_id, 1)]))

        created = updated = 0
        formula_ids = []
        for spec in specs:
            want_inputs = frozenset(p for p, _ in spec['inputs'])
            existing = None
            for f in conn.execute("SELECT id FROM conversion_formulas WHERE output_product_id=? AND is_active=1",
                                  (spec['output_pid'],)).fetchall():
                ins = frozenset(r[0] for r in conn.execute(
                    "SELECT product_id FROM conversion_formula_inputs WHERE formula_id=?", (f["id"],)))
                if ins == want_inputs:
                    existing = f["id"]
                    break
            if existing is not None:
                conn.execute("UPDATE conversion_formulas SET name=?, output_qty=?, note=? WHERE id=?",
                             (spec['name'], spec['output_qty'], note or None, existing))
                conn.execute("DELETE FROM conversion_formula_inputs WHERE formula_id=?", (existing,))
                fid = existing
                updated += 1
            else:
                cur = conn.execute(
                    "INSERT INTO conversion_formulas(name, output_product_id, output_qty, note) VALUES (?,?,?,?)",
                    (spec['name'], spec['output_pid'], spec['output_qty'], note or None))
                fid = cur.lastrowid
                created += 1
            for ipid, iqty in spec['inputs']:
                conn.execute("INSERT INTO conversion_formula_inputs(formula_id, product_id, quantity) VALUES (?,?,?)",
                             (fid, ipid, iqty))
            formula_ids.append(fid)
        if own:
            conn.commit()
        return {'created': created, 'updated': updated, 'formula_ids': formula_ids}
    finally:
        if own:
            conn.close()


def delete_conversion_formula(formula_id, also_delete_id=None):
    """Delete a formula (+ its inputs via the explicit DELETE). When
    `also_delete_id` is given (the reciprocal pack/unpack partner), delete both
    in ONE transaction so a pair is never left half-deleted."""
    conn = get_connection()
    ids = [formula_id]
    if also_delete_id is not None and also_delete_id != formula_id:
        ids.append(also_delete_id)
    for fid in ids:
        conn.execute("DELETE FROM conversion_formula_inputs WHERE formula_id=?", (fid,))
        conn.execute("DELETE FROM conversion_formulas WHERE id=?", (fid,))
    conn.commit()
    conn.close()


def find_pair_partner(formula_id, conn=None):
    """Return the reciprocal pack/unpack partner row of `formula_id`, or None.

    A pair-half has exactly ONE output and ONE input. The partner P satisfies the
    FULL reciprocal: P.output_product_id == this formula's single input product,
    AND this formula's output_product_id is P's single input product; P active,
    single-input, P != self. Multi-input (general) formulas have no partner.
    Matching the full reciprocal (not output alone) disambiguates a loose product
    shared by several packs. Used so deleting one half of a [แพ็ค]/[แกะ] pair can
    offer to take the other half with it instead of silently orphaning it.
    """
    own = conn is None
    if own:
        conn = get_connection()
    try:
        f = conn.execute(
            "SELECT name, output_product_id FROM conversion_formulas WHERE id=?",
            (formula_id,)).fetchone()
        if f is None:
            return None
        # Only [แพ็ค]/[แกะ] pack-unpack formulas form a pair. A generic reciprocal
        # conversion from the advanced editor is NOT a deletable pair — gate on the
        # prefix so this stays consistent with the list's one-way detector.
        if not (f["name"].startswith('[แพ็ค]') or f["name"].startswith('[แกะ]')):
            return None
        ins = [r["product_id"] for r in conn.execute(
            "SELECT product_id FROM conversion_formula_inputs WHERE formula_id=?",
            (formula_id,)).fetchall()]
        if len(ins) != 1:                       # not a clean 1-input pair half
            return None
        my_input, my_output = ins[0], f["output_product_id"]
        for cand in conn.execute("""
            SELECT cf.id, cf.name, cf.output_product_id, cf.output_qty,
                   p.product_name AS output_product_name,
                   p.unit_type    AS output_unit_type
              FROM conversion_formulas cf
              JOIN products p ON p.id = cf.output_product_id
             WHERE cf.is_active=1 AND cf.output_product_id=? AND cf.id<>?
               AND (cf.name LIKE '[แพ็ค]%' OR cf.name LIKE '[แกะ]%')
        """, (my_input, formula_id)).fetchall():
            cins = [r["product_id"] for r in conn.execute(
                "SELECT product_id FROM conversion_formula_inputs WHERE formula_id=?",
                (cand["id"],)).fetchall()]
            # dedup key (output, single-input set) is unique among active formulas,
            # so the first full-reciprocal match is the only one.
            if len(cins) == 1 and cins[0] == my_output:
                return cand                     # full reciprocal match
        return None
    finally:
        if own:
            conn.close()


def derive_pair_from_formula(formula_id, conn=None):
    """Recover the (pack, loose, ratio, direction) that built a [แพ็ค]/[แกะ]
    pair-half, so the pair form can reopen it prefilled for editing.

        PACK   half: output=pack qty1, input=(loose, ratio)  → ratio = input qty
        UNPACK half: output=loose qty ratio, input=(pack, 1)  → ratio = output_qty

    Returns {'pack_id','loose_id','ratio','direction','pack_name','loose_name'},
    or None for anything that is NOT a clean pair-half (missing, no [แพ็ค]/[แกะ]
    prefix, or != 1 input — i.e. a generic/advanced formula has no pair form).
    `direction` is 'both' when the reciprocal partner is present, else the single
    side this formula represents ('pack' or 'unpack')."""
    own = conn is None
    if own:
        conn = get_connection()
    try:
        f = conn.execute(
            "SELECT id, name, output_product_id, output_qty, note FROM conversion_formulas WHERE id=?",
            (formula_id,)).fetchone()
        if f is None:
            return None
        name = f["name"] or ""
        is_pack, is_unpack = name.startswith('[แพ็ค]'), name.startswith('[แกะ]')
        if not (is_pack or is_unpack):
            return None                          # generic/advanced formula — no pair form
        ins = [(r["product_id"], r["quantity"]) for r in conn.execute(
            "SELECT product_id, quantity FROM conversion_formula_inputs WHERE formula_id=?",
            (formula_id,)).fetchall()]
        if len(ins) != 1:
            return None                          # not a clean 1-input pair half
        in_pid, in_qty = ins[0]
        if is_pack:
            pack_id, loose_id, ratio = f["output_product_id"], in_pid, in_qty
        else:                                    # [แกะ]
            loose_id, pack_id, ratio = f["output_product_id"], in_pid, f["output_qty"]
        direction = 'both' if find_pair_partner(formula_id, conn=conn) is not None \
                    else ('pack' if is_pack else 'unpack')

        def _name(pid):
            r = conn.execute("SELECT product_name FROM products WHERE id=?", (pid,)).fetchone()
            return r["product_name"] if r else str(pid)

        return {'pack_id': pack_id, 'loose_id': loose_id, 'ratio': int(ratio),
                'direction': direction, 'pack_name': _name(pack_id), 'loose_name': _name(loose_id),
                'note': f["note"] or ''}
    finally:
        if own:
            conn.close()


def get_recent_conversion_runs(limit=5):
    conn = get_connection()
    rows = conn.execute("""
        SELECT ccl.id, ccl.reference_no, ccl.event_date, ccl.created_at,
               ccl.output_qty, ccl.unit_cost, ccl.total_input_cost,
               p.product_name AS output_product_name,
               p.unit_type    AS output_unit_type
          FROM conversion_cost_log ccl
          JOIN products p ON p.id = ccl.output_product_id
         ORDER BY ccl.id DESC
         LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return rows


def run_conversion(formula_id, multiplier, reference_no='', extra_note='', writeoff_qty=0):
    """Run a conversion. `writeoff_qty` = output units scrapped during the run
    (ของเสีย, e.g. 10 แผง → 20 ตัว but 1 broke). Inputs are still fully consumed;
    only GOOD units (expected − writeoff) enter stock; input cost spreads over
    the good units (scrap raises good-unit cost). Broken units never enter stock.
    """
    from datetime import datetime as _dt
    conn = get_connection()
    formula = conn.execute("""
        SELECT cf.*, p.product_name AS output_product_name
          FROM conversion_formulas cf
          JOIN products p ON p.id = cf.output_product_id
         WHERE cf.id = ?
    """, (formula_id,)).fetchone()
    if not formula:
        conn.close()
        return False, 'ไม่พบสูตรการแปลง', {}

    # write-off (ของเสีย) — output units scrapped this run
    try:
        writeoff_qty = max(0, int(writeoff_qty or 0))
    except (ValueError, TypeError):
        writeoff_qty = 0
    expected_qty = formula['output_qty'] * multiplier
    if writeoff_qty > expected_qty:
        conn.close()
        return False, f'ตัดของเสียได้ไม่เกินจำนวนที่ผลิต ({expected_qty:,})', {}
    good_qty = expected_qty - writeoff_qty

    inputs = conn.execute("""
        SELECT cfi.*, p.product_name, p.unit_type,
               COALESCE(sl.quantity, 0) AS current_stock
          FROM conversion_formula_inputs cfi
          JOIN products p ON p.id = cfi.product_id
          LEFT JOIN stock_levels sl ON sl.product_id = cfi.product_id
         WHERE cfi.formula_id = ?
    """, (formula_id,)).fetchall()

    shortage = []
    for inp in inputs:
        needed = inp['quantity'] * multiplier
        if inp['current_stock'] < needed:
            shortage.append(
                f'{inp["product_name"]}: ต้องการ {needed:,} แต่มีแค่ {inp["current_stock"]:,} {inp["unit_type"]}'
            )
    if shortage:
        conn.close()
        return False, 'สต็อกไม่พอ: ' + ' | '.join(shortage), {}

    # ── WACC: คำนวณต้นทุน output จาก input WACCs ──────────────────────────
    total_input_cost = 0.0
    for inp in inputs:
        needed   = inp['quantity'] * multiplier
        inp_wacc = get_current_wacc(inp['product_id'], conn)
        total_input_cost += needed * inp_wacc

    # cost spreads over GOOD output only (scrap loss raises good-unit cost)
    output_unit_cost = total_input_cost / good_qty if good_qty > 0 else 0.0

    # ใช้ reference_no ที่ user ส่งมา หรือ generate ใหม่
    conv_ref = reference_no or f'CONV{formula_id}-{_dt.now().strftime("%Y%m%d%H%M%S")}'

    note_text = f'แปลง: {formula["name"]}'
    if extra_note:
        note_text += f' | {extra_note}'
    if writeoff_qty:
        note_text += f' | ตัดของเสีย {writeoff_qty:,}'

    for inp in inputs:
        needed = inp['quantity'] * multiplier
        conn.execute(
            "INSERT INTO transactions(product_id, txn_type, quantity_change, unit_mode, reference_no, note)"
            " VALUES (?,?,?,?,?,?)",
            (inp['product_id'], 'OUT', -needed, 'unit', conv_ref, note_text)
        )

    # only GOOD units enter stock; a total loss (good_qty=0) adds nothing
    if good_qty > 0:
        conn.execute(
            "INSERT INTO transactions(product_id, txn_type, quantity_change, unit_mode, reference_no, note)"
            " VALUES (?,?,?,?,?,?)",
            (formula['output_product_id'], 'IN', good_qty, 'unit', conv_ref, note_text)
        )

    # บันทึก conversion cost log (ใช้ตอน recalculate WACC output)
    conn.execute(
        "INSERT INTO conversion_cost_log"
        " (output_product_id, reference_no, event_date, output_qty, total_input_cost, unit_cost, writeoff_qty)"
        " VALUES (?,?,date('now'),?,?,?,?)",
        (formula['output_product_id'], conv_ref, good_qty, total_input_cost, output_unit_cost, writeoff_qty)
    )

    conn.commit()

    # Recalculate WACC for all involved products
    involved = [inp['product_id'] for inp in inputs] + [formula['output_product_id']]
    recalculate_waccs_for_products(involved)

    conn.close()
    msg = f'แปลงสำเร็จ: ได้ {good_qty:,} {formula["output_product_name"]}'
    if writeoff_qty:
        msg += f' (ตัดของเสีย {writeoff_qty:,})'
    return True, msg, {
        'output_qty': good_qty,
        'writeoff_qty': writeoff_qty,
        'output_name': formula['output_product_name'],
    }
