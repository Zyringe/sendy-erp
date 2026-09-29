"""unit_conversion.py — every READ of `unit_conversions` that answers "how many
base units is one of this unit" (card A, ADR 0019).

A ratio is stored per product and per bill spelling: 1 `bsn_unit` =
`ratio` x the product's `unit_type` (its base unit). Writers stay where they
are (`models/bsn_sync.py`, the census in tests/test_unit_writer_census.py).

The readers do NOT share one rule, and this module does not make them. They
differ on how they compare a bill unit with the base unit, which spellings
they try, whether the base unit or a stored row wins, and what a miss means.
Each family has its own entry point and keeps its own rule; the caller keeps
its own miss action:

  family    entry point          base-unit compare   lookup                        miss
  word      word_ratio           unit WORD           spelling, then its word       None: price evidence skips the bill
  resolve   conversion_ratio     (caller, row wins)  spelling, then a word scan    price_lookup._resolve_unit decides
  bundle    conversion_ratio     unit WORD           the word, then a word scan    1.0 silent (promo gating, Q16)
  exact     exact_ratio          raw spelling        that spelling only            None: the quote CLI skips the bill
  exact     exact_ratios         (caller)            that spelling only            the call card keeps its price unconverted
  options   exact_ratios         (caller)            every stored row              n/a: the VAT-sub selector lists them all
  SQL       base_qty_sql & co.   raw, COALESCE ''    exact join                    1.0, counted by unratioed_line_sql

A blank bill unit is 1.0 in the word and SQL families and a miss in the
exact one. Nothing here rounds: the stock writer's 4-dp round belongs to the
stock writer. Where two families disagree today (a `กุรุส` line on a product
whose row is stored as `กร`: word 144, SQL 1.0 and counted), the difference
is pinned in tests/test_unit_conversion_families.py, not smoothed over here.

Stays outside this module on purpose: `bsn_sync._get_base_qty` and the other
stock-side exact readers (PR2), enumerators that list unit NAMES or admin
rows (`price_lookup._known_ratio_units`, `bsn_sync.get_all_unit_conversions`),
and the pending-list predicate. The reader census
(tests/test_unit_reader_census.py) names each with its reason.

Flask-free, stdlib + `bsn_units` only, so it runs on prod under
/opt/venv/bin/python. Python 3.9: no `X | None` syntax.
"""
from typing import Optional

import bsn_units


def _word(conn, spelling):
    if not spelling:
        return spelling
    return bsn_units.normalize_unit(spelling, conn=conn)


def conversion_ratio(conn, product_id, unit) -> Optional[float]:
    """This product's ratio for `unit`, or None. No base-unit short-circuit:
    the caller decides whether the base unit or a stored row wins.

    EXACT spelling first, the map only as a fallback. Wherever a row for the
    literal spelling exists, the answer is what it was before the map was
    consulted at all; the map only resolves asks that previously found
    nothing. A ledger row spelled `หล` keeps reading its OWN `หล` ratio
    rather than a `โหล` twin's for as long as both exist.

    `ORDER BY id` on the fallback so a product carrying two spellings of one
    หน่วย answers deterministically: the oldest row.
    """
    row = conn.execute(
        "SELECT ratio FROM unit_conversions WHERE product_id = ? AND bsn_unit = ?",
        (product_id, unit)
    ).fetchone()
    if row is not None:
        return float(row['ratio'])
    for r in conn.execute(
        "SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id = ? ORDER BY id",
        (product_id,)
    ).fetchall():
        if _word(conn, r['bsn_unit']) == unit:
            return float(r['ratio'])
    return None


def word_ratio(conn, product_id, unit_type, unit, cache) -> Optional[float]:
    """Ratio converting a sales line's own `unit` to the product's base unit,
    or None when neither its spelling nor its word has a row. The price
    resolver SKIPS such a bill and never assumes 1.

    A blank unit, or one whose word is the base unit's word, is 1.0 whatever
    a row keyed on the base unit says. `cache` is the caller's dict, keyed
    on (product_id, unit).
    """
    if not unit or _word(conn, unit) == _word(conn, unit_type):
        return 1.0
    key = (product_id, unit)
    if key in cache:
        return cache[key]
    val = conversion_ratio(conn, product_id, unit)
    if val is None:
        val = conversion_ratio(conn, product_id, _word(conn, unit))
    cache[key] = val
    return val


def exact_ratio(conn, product_id, unit_type, unit) -> Optional[float]:
    """Ratio for `unit` spelled exactly as stored, or None. 1.0 when `unit`
    equals `unit_type` character for character (no strip, no map)."""
    if unit == unit_type:
        return 1.0
    row = conn.execute(
        "SELECT ratio FROM unit_conversions WHERE product_id = ? AND bsn_unit = ?",
        (product_id, unit)
    ).fetchone()
    return float(row['ratio']) if row is not None else None


def exact_ratios(conn, product_ids) -> dict:
    """{(product_id, bsn_unit): ratio} for every stored row of `product_ids`,
    in one query, in the table's own order (bsn_unit within a product). No
    base-unit entry is added and nothing is filtered: the call card drops a
    ratio of 0, the VAT-sub unit selector lists it."""
    ph = ",".join("?" * len(product_ids))
    rows = conn.execute(
        f"SELECT product_id, bsn_unit, ratio FROM unit_conversions WHERE product_id IN ({ph})",
        list(product_ids),
    ).fetchall()
    return {(r['product_id'], r['bsn_unit']): float(r['ratio']) for r in rows}


# ── SQL: a sales line's qty in the product's base unit ───────────────────────
# `sales_transactions.qty` is denominated in the unit written on the BILL
# (โหล, กล่อง, ซอง); `products.cost_price` is per `products.unit_type`. Every
# COGS over sales lines must convert first, the way the stock ledger already
# does: prod IV6901440-1 sold 16 โหล of pid 134 and `transactions` id 358869
# moved -192 ตัว, while /accounting costed that line at 16 x ฿12. Multiplying
# raw qty by cost_price understated prod COGS by ฿189,535 over Jan-Aug 2026.
# `sales_filters` re-exports these three for the dated rebase scripts.


def unit_conversion_join(st='st', uc='uc'):
    """LEFT JOIN pairing each sales line with the ratio for its OWN bill unit."""
    return ("LEFT JOIN unit_conversions {uc} ON {uc}.product_id = {st}.product_id "
            "AND {uc}.bsn_unit = {st}.unit".format(st=st, uc=uc))


def base_qty_sql(st='st', p='p', uc='uc'):
    """SQL expression: one line's qty expressed in the product's BASE unit.

    A blank bill unit, or one spelled exactly like the base unit, is ratio 1
    whatever a row keyed on that unit says. Otherwise the row joined on the
    bill's exact spelling, and 1 when there is none: COGS costs an unratioed
    line at ratio 1 rather than dropping it, because dropping it would
    understate COGS further. Pair this with unratioed_line_sql() so the page
    can disclose those lines.

    This is the exact-spelling family, not the word family: a line spelled
    `กุรุส` on a product whose row is stored as `กร` reads 1 here and 144 in
    word_ratio (see the module docstring).
    """
    return ("({st}.qty * CASE WHEN COALESCE({st}.unit, '') = '' "
            "OR COALESCE({st}.unit, '') = COALESCE({p}.unit_type, '') "
            "THEN 1.0 ELSE COALESCE({uc}.ratio, 1.0) END)".format(st=st, p=p, uc=uc))


def unratioed_line_sql(st='st', p='p', uc='uc'):
    """SQL expression: 1 when a mapped line's bill unit has no ratio, so
    base_qty_sql() fell back to 1 and that line's quantity is understated."""
    return ("CASE WHEN {p}.id IS NOT NULL AND {uc}.ratio IS NULL "
            "AND COALESCE({st}.unit, '') <> '' "
            "AND COALESCE({st}.unit, '') <> COALESCE({p}.unit_type, '') "
            "THEN 1 ELSE 0 END".format(st=st, p=p, uc=uc))
