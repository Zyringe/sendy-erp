"""Owner of "is this a return document?" for the BSN trade books.

CONTEXT.md "Trade documents": a document's KIND is the prefix of its number.
  sales side     IV invoice, HS cash sale (ขายสด), SR credit note (RETURN)
  purchase side  HP, RR purchases, GR purchase return / credit note (RETURN)
So exactly one prefix per side is a return: SR on 'sales', GR on 'purchase'.
HS is deliberately NOT a return on either reading: it is a sale (#514) and
counts as revenue. AR/settlement code that excludes HS because a cash sale is
never a receivable asks a different question and still types 'HS%' itself.

doc_base vs doc_no: both carry the same prefix (doc_no is doc_base plus a
per-line '-N'), so the prefix test is column-agnostic. The COLUMN is a
parameter (`col`, default 'doc_base') because callers differ and NULL
semantics matter: `col NOT LIKE 'SR%'` is NULL, not TRUE, for a NULL column.
Each call site passes the column it always used; nothing here coalesces unless
`null_safe=True` is asked for.

`side` has no default on purpose: a purchase caller that forgot it would get
the SR test, which is always true on purchase_transactions, so GR returns would
silently count as purchases. `alias` is required too ('' for an unaliased table).
`is_return_sql` returns a predicate fragment; `is_return` is its Python twin
(case-insensitive like SQLite's LIKE, None -> False). tests/test_document_kind
runs both against each other. A new document prefix is one edit here.
"""

_RETURN_PREFIX = {'sales': 'SR', 'purchase': 'GR'}


def _prefix(side):
    try:
        return _RETURN_PREFIX[side]
    except KeyError:
        raise ValueError("side must be 'sales' or 'purchase', got %r" % (side,))


def _col(alias, col):
    return '{}.{}'.format(alias, col) if alias else col


def return_prefix(side):
    """The return prefix itself ('SR' / 'GR') for the one caller that needs it
    as text: a regex over a report line (parse_weekly._SR_DOC_LINE)."""
    return _prefix(side)


def is_return_sql(alias, side, col='doc_base'):
    """SQL predicate: the row's document is a return (SR on sales, GR on purchase)."""
    return "{} LIKE '{}%'".format(_col(alias, col), _prefix(side))


def not_return_sql(alias, side, col='doc_base', null_safe=False):
    """SQL predicate: the row's document is NOT a return. `null_safe` makes a
    NULL column count as not-a-return instead of dropping the row."""
    c = _col(alias, col)
    if null_safe:
        c = "COALESCE({}, '')".format(c)
    return "{} NOT LIKE '{}%'".format(c, _prefix(side))


def is_return(doc_no, side):
    """Python twin of is_return_sql for a doc_no or doc_base string."""
    prefix = _prefix(side)
    if not doc_no:
        return False
    head = doc_no[:len(prefix)]
    # ASCII only: str.upper() folds e.g. U+017F to 'S', SQLite LIKE does not.
    return head.isascii() and head.upper() == prefix
