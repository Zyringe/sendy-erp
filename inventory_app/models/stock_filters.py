"""Canonical answer to "does this BSN line move stock?".

WHY A CONSTANT AND NOT A COLUMN
    A DB column has to be set at every mapping-creation path, and there are
    four (`mapping.upsert_mapping`, `imports.py`'s new-code registration,
    `suggestions.py`, `vat_book_builder.seed_products_from_stmas`). The
    VAT-book build inserts its mappings AFTER migrations run, so a
    migration-set column is silently 0 on every fresh build — a fresh build
    would then import these codes as ordinary stock, over the full history.
    A constant cannot be forgotten by a creation path that does not exist yet.

WHAT THESE CODES ARE
    Express pseudo-codes that carry money but not goods:
      ZZZ       ส่วนลดพิเศษ — a customer discount. Express allows ONE discount
                per line, so a second (baht-per-unit) discount is keyed as its
                own ZZZ line. Dropping it OVERSTATES revenue.
      888ค8888  ค่าขนส่ง — billable delivery income. Dropping it UNDERSTATES
                revenue. It previously synced to stock and left pid 1211
                holding phantom inventory.

    888ค8887 (ค่าVAT) is deliberately absent: VAT is tax, not revenue, and
    `vat_type` already computes it. It stays `is_ignored`.

ADDING A CODE
    Requires a deploy, on purpose. Also re-read design.md §3.4 first — every
    reader of `synced_to_stock` has to agree, and one of them DELETES rows.
"""

NON_STOCK_BSN_CODES = frozenset({'ZZZ', '888ค8888'})


class NonStockCodeError(ValueError):
    """A write tried to put a non-stock billable code into a state that would
    drop its revenue (today: is_ignored=1). Refused rather than silently
    overridden — a DB that says one thing while the importer does another is
    its own bug."""


def is_non_stock_code(bsn_code):
    return bool(bsn_code) and bsn_code in NON_STOCK_BSN_CODES


def _sql_literals():
    return ", ".join(
        "'" + c.replace("'", "''") + "'" for c in sorted(NON_STOCK_BSN_CODES))


def non_stock_clause(alias=''):
    """SQL predicate keeping only rows that DO move stock.

    `alias` is the table alias in the caller's FROM ('' when unaliased):
    non_stock_clause('st') -> "st.bsn_code NOT IN ('888ค8888', 'ZZZ')".

    NULL-safe: `bsn_code IS NULL` is an unmapped legacy row, which is not a
    non-stock code, so it must be KEPT. `NOT IN` returns NULL (falsy) for a
    NULL left side, which would silently drop those rows — hence the
    explicit IS NULL leg.
    """
    p = '{}.'.format(alias) if alias else ''
    return "({p}bsn_code IS NULL OR {p}bsn_code NOT IN ({lits}))".format(
        p=p, lits=_sql_literals())
