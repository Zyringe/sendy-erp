"""BSN import line identity and change detection — ONE definition.

`preview_import` and `import_weekly` must answer the same two questions about
every bill line: *is this the same line I already have*, and *has it changed*.
Before this module they each answered with their own copy of the same five
predicates, kept in step by hand.

That pairing is load-bearing. The operator ticks `apply_removals` because of a
count the PREVIEW produced, and the deletion is carried out by the COMMIT. A
drift between the two copies is a drift between what the operator agreed to and
what actually happened to the ledger.

Everything here is pure — no database, no IO, and no mutation of its arguments
— so the rules can be tested directly instead of through a whole import run.

Vocabulary: a *line key* identifies a line; a *field diff* says what changed
about it. Sales lines additionally have a *stock-event* question — "would
replacing this row change what the ledger posted?" — which is NOT the negation
of the field diff: it ignores price and net, and it looks at the party, which
the field diff never does.
"""

import bsn_units

# The tolerance both copies used. qty/price/net are REAL columns and a re-parse
# of the same file can differ in the last bit.
EPSILON = 1e-9

# The field-diff fields, in the order the confirm page renders them.
DIFF_FIELDS = ('qty', 'unit', 'unit_price', 'net', 'product_id')


def party_column(file_type):
    """The column holding the counterparty for this file type."""
    return 'customer' if file_type == 'sales' else 'supplier'


def entry_key(entry, file_type):
    """Line identity as it comes out of a parser.

    `doc_no` already carries the printed "-N" line suffix on the sales side, so
    it disambiguates on its own. Purchase documents have no suffix, so the key
    adds `line_seq` (from the parser, formalised by mig 091) to separate
    multiple lines of one product on one document.
    """
    if file_type == 'purchase':
        return (entry['doc_no'], entry['product_code_raw'],
                entry.get('line_seq', 1))
    return (entry['doc_no'], entry['product_code_raw'])


def row_key(row, file_type):
    """Line identity as it comes out of the database.

    The same concept as `entry_key` under different field names: a parsed entry
    calls the code `product_code_raw`, a stored row calls it `bsn_code`. That
    rename is the reason the two used to be written out separately.
    """
    if file_type == 'purchase':
        return (row['doc_no'], row['bsn_code'], row['line_seq'])
    return (row['doc_no'], row['bsn_code'])


def doc_base(doc_no):
    """The document number without its printed "-N" line suffix."""
    return doc_no.rsplit('-', 1)[0] if '-' in doc_no else doc_no


def _num_same(a, b):
    return abs((a or 0) - (b or 0)) < EPSILON


def _unit_same(stored_unit, new_unit):
    """Compare units with the STORED side normalised.

    Legacy and rebuild rows were saved with raw acronym units (หล/ตว/กก…) while
    the incoming entry's unit is normalised on the way in. Without normalising
    the stored side, re-importing the identical file flags ~95% of purchase
    rows as changed — cosmetic only (same base_qty), but it churns the ledger
    for nothing.
    """
    return bsn_units.normalize_unit(stored_unit or '') == (new_unit or '')


def field_diff(row, entry, product_id, new_unit):
    """What changed between the stored row and this entry.

    Returns ``[(field, old_value, new_value), …]``, empty when the line is a
    true re-upload — so ``not field_diff(...)`` is the `unchanged` verdict.

    `new_unit` is the ALREADY-NORMALISED incoming unit. The returned tuple
    reports the stored unit RAW, because the raw value is what the operator
    needs to see on the confirm page.

    `entry` is read with bare subscripts on purpose: its shape is a contract
    with the two parsers, and a missing key must raise here rather than compare
    quietly against None.
    """
    diffs = []
    if not _num_same(row['qty'], entry['qty']):
        diffs.append(('qty', row['qty'], entry['qty']))
    if not _unit_same(row['unit'], new_unit):
        diffs.append(('unit', row['unit'], new_unit))
    if not _num_same(row['unit_price'], entry['unit_price']):
        diffs.append(('unit_price', row['unit_price'], entry['unit_price']))
    if not _num_same(row['net'], entry['net']):
        diffs.append(('net', row['net'], entry['net']))
    if (row['product_id'] or 0) != (product_id or 0):
        diffs.append(('product_id', row['product_id'], product_id))
    return diffs


def stock_event_changed(row, entry, product_id, new_unit, file_type):
    """Would replacing this row change what the stock ledger posted?

    Deliberately NOT the negation of `field_diff`:

    * a price/net-only correction (the overwhelmingly common one) leaves the
      stock event untouched, so the replacement can carry the old row's
      platform record over instead of reversing and re-applying it — churn
      that is LOSSY, because the undo clamps at zero;
    * a change of party CAN change the stock event (it decides the platform),
      and `field_diff` never looks at the party.

    A 5 → 7 correction landed at 88 instead of 93 before mig 172 by getting
    this wrong in the other direction.
    """
    party_col = party_column(file_type)
    return not (
        _num_same(row['qty'], entry['qty'])
        and _unit_same(row['unit'], new_unit)
        and (row['product_id'] or 0) == (product_id or 0)
        and (row[party_col] or '').strip() == (entry['party'] or '').strip()
    )
