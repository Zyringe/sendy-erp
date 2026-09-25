"""2026-09-25 — merge the duplicate `กร` conversion rows into `กุรุส` (issue #641 item 3).

WHY. Migration 193 (#610) translated every Express unit code stored in
`unit_conversions.bsn_unit` into its Sendy word, and deliberately skipped three —
`กร`, `ถง`, `บล` — because #603 was still deciding what `กร` means per product.
`ถง` and `บล` turned out to have no conversion rows at all, so `กร` is the whole
remainder and the last Express short code left in that column. Measured on prod
2026-09-25 (read-only): 12 rows, and every one of them sits beside a `กุรุส` row
on the same product at an IDENTICAL ratio — 144.0 for 1047, 1048, 1049, 1050,
1052, 1187, 1188, 1320 and 1.0 for 1186, 1321, 1338, 1801. No stored row anywhere
reads `กร`, `ถง` or `บล`, so this is a pure de-duplication: the twin already
carries the rule.

A plain translate is impossible: UNIQUE(product_id, bsn_unit) and the `กุรุส` row
is already there. So the duplicate is dropped and the twin carries on.

WHY IT IS UNBLOCKED. #603's hasp rebase ran on prod 2026-09-22 09:19Z (1187/1188
→ `ตัว` 1, `กุรุส` 144), after the sandpapers on 2026-09-20. Put ruled 1186 a
false positive and parked 1321, 1338, 1801 until a price or stock arrives. None of
that is disturbed: those keep their `กุรุส` row, ratio and all, so the evidence
#603 still needs survives.

WHY A SCRIPT AND NOT A MIGRATION (Put's call, 2026-09-25). This was written as
migration 195 first and measured: the runner applies it inside every
`database.init_db()`, so it reached the fixtures of the migration test suites that
come before it. Three of those files seed a `กร` conversion on purpose — 193's own
fixture carries the comment "# would abort as twin_ratio" — and 8 to 10 of their
tests went red depending on the design, every one of them caused by this change
and none of them by a real defect (baseline with the migration removed: 361
passed, 0 failed). Rewriting those assertions would have made them pass whether
or not 190 and 193 still did their job. A one-time cleanup of a measured set is
what `scripts/2026_09_19_split_belco_582.py` and `scripts/2026_09_20_rebase_gross_603.py`
already are here, and a script runs when a person runs it.

WHAT IT TOUCHES. `unit_conversions` only. No ratio, quantity, synced_to_stock,
ledger, cost or price row changes, asserted inside the transaction. It therefore
needs no `database.script_connection`: the #590 cost guard covers
`products.cost_price`/`opening_cost`, `product_cost_ledger` and
`conversion_cost_log`, none of which this opens.

SELECTION IS THE GUARD. It merges a row only when the twin exists at the same
ratio. A lone `กร` row, or a pair that disagrees, is a different operation (a
rename, or a ruling nobody has made) and is REPORTED and left alone.

PRECONDITIONS, checked before the first write; any one of them refuses the run:
  * a sales or purchase line still reads `กร` for a product being merged. That is
    the #609 shape: drop the conversion a stored line depends on and the next
    import finds the line unsyncable, rewrites it and moves stock. Scoped to the
    merged products, because a `กร` elsewhere is a spelling question (193 did
    those columns) and not a dependency.
  * `unit_map` has no BSN5657 `กร` → `กุรุส` row, so an import could write `กร`
    again onto a product whose conversion just went.
  * any product in the merge set has drifted since this was measured: its `กุรุส`
    ratio is no longer what its `กร` ratio says.

    python3 scripts/2026_09_25_merge_gross_twins_641.py --db PATH
    python3 scripts/2026_09_25_merge_gross_twins_641.py --db PATH --apply

Without --apply it rehearses: every write happens, every invariant is asserted,
then the transaction is rolled back. Re-running after a successful apply finds
nothing to merge and exits 0.
"""
import argparse
import json
import sqlite3
import sys

CODE = 'กร'
WORD = 'กุรุส'


class InvariantFailed(Exception):
    pass


def duplicates(conn):
    """The `กร` rows whose product already carries `กุรุส` at the same ratio.

    `IS` rather than `=` so a NULL ratio on both sides counts as equal instead of
    silently dropping the row out of the set.
    """
    return [dict(r) for r in conn.execute(f"""
        SELECT c.id, c.product_id, c.ratio, c.created_at, p.product_name, p.unit_type
          FROM unit_conversions c
          JOIN unit_conversions w ON w.product_id = c.product_id AND w.bsn_unit = ?
          JOIN products p ON p.id = c.product_id
         WHERE c.bsn_unit = ? AND w.ratio IS c.ratio
         ORDER BY c.product_id
    """, (WORD, CODE))]


def left_behind(conn):
    """Rows this script will NOT touch, with the reason, so "why is there still a
    กร row" has an answer without reading this file."""
    out = []
    for r in conn.execute(
            "SELECT c.id, c.product_id, c.ratio FROM unit_conversions c WHERE c.bsn_unit = ?",
            (CODE,)):
        twin = conn.execute(
            "SELECT ratio FROM unit_conversions WHERE product_id=? AND bsn_unit=?",
            (r[1], WORD)).fetchone()
        if twin is None:
            out.append({'id': r[0], 'product_id': r[1], 'ratio': r[2],
                        'reason': f'no {WORD} row on this product: a rename, not a duplicate'})
        elif twin[0] != r[2]:
            out.append({'id': r[0], 'product_id': r[1], 'ratio': r[2],
                        'reason': f'{CODE} {r[2]} vs {WORD} {twin[0]}: needs a ruling'})
    return out


def preconditions(conn, dups):
    """Problems that must all be empty before anything is written."""
    bad = []
    pids = [d['product_id'] for d in dups]
    if pids:
        ph = ','.join('?' * len(pids))
        for table in ('sales_transactions', 'purchase_transactions'):
            for r in conn.execute(
                    f"SELECT id, doc_no, product_id FROM {table} "
                    f"WHERE unit = ? AND product_id IN ({ph})", [CODE] + pids):
                bad.append(f'{table} id {r[0]} ({r[1]}) still reads {CODE} on product '
                           f'{r[2]}, which is in the merge set (#609)')
        row = conn.execute(
            "SELECT 1 FROM unit_map WHERE book='BSN5657' AND spelling=? AND word=?",
            (CODE, WORD)).fetchone()
        if row is None:
            bad.append(f'unit_map has no BSN5657 {CODE} -> {WORD} row; an import could '
                       f'write {CODE} again')
    # Drift: the twin must still agree. `duplicates()` already requires it, so a
    # mismatch here means the row moved between the two reads.
    for d in dups:
        twin = conn.execute(
            "SELECT ratio FROM unit_conversions WHERE product_id=? AND bsn_unit=?",
            (d['product_id'], WORD)).fetchone()
        if twin is None or twin[0] != d['ratio']:
            bad.append(f'product {d["product_id"]}: {WORD} ratio moved to '
                       f'{twin[0] if twin else None} while {CODE} says {d["ratio"]}')
    return bad


def snapshot(conn, pids):
    """Everything that must NOT move, per product."""
    if not pids:
        return {}
    ph = ','.join('?' * len(pids))
    units = {}
    for r in conn.execute(
            f"SELECT product_id, bsn_unit, ratio FROM unit_conversions "
            f"WHERE product_id IN ({ph})", pids):
        units.setdefault(r[0], {})[r[1]] = r[2]
    return {
        'units': units,
        'products': {r[0]: (r[1], r[2], r[3], r[4]) for r in conn.execute(
            f"SELECT id, cost_price, opening_cost, base_sell_price, unit_type FROM products "
            f"WHERE id IN ({ph})", pids)},
        'stock': {r[0]: r[1] for r in conn.execute(
            f"SELECT product_id, quantity FROM stock_levels WHERE product_id IN ({ph})", pids)},
        'ledger': {r[0]: (r[1], r[2]) for r in conn.execute(
            f"SELECT product_id, COUNT(*), COALESCE(SUM(quantity_change),0) FROM transactions "
            f"WHERE product_id IN ({ph}) GROUP BY product_id", pids)},
        'totals': tuple(conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(quantity_change),0) FROM transactions").fetchone()),
        'uc_count': conn.execute("SELECT COUNT(*) FROM unit_conversions").fetchone()[0],
    }


def merge(conn, dups):
    conn.executemany("DELETE FROM unit_conversions WHERE id = ?",
                     [(d['id'],) for d in dups])


def assert_invariants(conn, before, dups):
    after = snapshot(conn, [d['product_id'] for d in dups])
    if not dups:
        return
    if after['uc_count'] != before['uc_count'] - len(dups):
        raise InvariantFailed(
            f'unit_conversions went {before["uc_count"]} -> {after["uc_count"]}, '
            f'expected -{len(dups)}')
    for d in dups:
        pid = d['product_id']
        want = {u: r for u, r in before['units'][pid].items() if u != CODE}
        if after['units'].get(pid) != want:
            raise InvariantFailed(
                f'product {pid} conversions are {after["units"].get(pid)}, expected {want}')
        if after['units'][pid].get(WORD) != d['ratio']:
            raise InvariantFailed(f'product {pid} lost its {WORD} ratio {d["ratio"]}')
    for key in ('products', 'stock', 'ledger', 'totals'):
        if before[key] != after[key]:
            raise InvariantFailed(f'{key} moved: {before[key]} -> {after[key]}')


def run(conn, apply):
    dups = duplicates(conn)
    skipped = left_behind(conn)
    report = {'merge': dups, 'skipped': skipped, 'applied': False}
    if not dups:
        report['note'] = 'nothing to merge'
        return 0, report

    conn.execute("BEGIN IMMEDIATE")
    try:
        bad = preconditions(conn, dups)
        if bad:
            conn.rollback()
            report['problems'] = bad
            return 2, report
        before = snapshot(conn, [d['product_id'] for d in dups])
        merge(conn, dups)
        assert_invariants(conn, before, dups)
    except Exception as exc:
        conn.rollback()
        report['problems'] = [f'{type(exc).__name__}: {exc}']
        return 2, report

    if apply:
        conn.commit()
        report['applied'] = True
    else:
        conn.rollback()
    return 0, report


def main():
    ap = argparse.ArgumentParser(description='Merge duplicate กร conversions (#641 item 3)')
    ap.add_argument('--db', required=True)
    ap.add_argument('--apply', action='store_true',
                    help='commit; without it every write is rolled back after the checks')
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        code, report = run(conn, args.apply)
    finally:
        conn.close()

    for d in report['merge']:
        print(f"  merge  pid {d['product_id']:>5}  {CODE} {d['ratio']}  "
              f"unit_type={d['unit_type']}  {d['product_name'][:46]}")
    for s in report['skipped']:
        print(f"  keep   pid {s['product_id']:>5}  {s['reason']}")
    print(json.dumps({k: v for k, v in report.items() if k not in ('merge', 'skipped')},
                     ensure_ascii=False))
    if code == 0 and not report['applied'] and report['merge']:
        print('REHEARSAL ONLY — rerun with --apply to commit')
    sys.exit(code)


if __name__ == '__main__':
    main()
