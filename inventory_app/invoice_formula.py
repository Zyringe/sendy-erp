"""Pure helper: derive the customer-facing 'list -discount = paid' formula
for one sales_transactions line, from that line's own numbers only (#527).

No DB access. This only decides WHETHER the line's own numbers explain its
`total` and `net` (the reconciliation guard below) and WHAT to show if they
do. The FINAL number in the rendered formula ("= 53.90") is the card's own
`price_per_unit` (already derived via vat_math.cash_from_net) — this helper
never recomputes it, so nothing here can drift from the badges that compare
against that same figure; the caller appends it.

Reconciliation guard, BOTH required, else -> None ("no formula", render the
price alone):
  1. unit_price*qty reduced by the KEYED discount, rounded to 2dp, is within
     +/-0.01 of `total` (the line's own pre-VAT, pre-bill-discount subtotal).
  2. `net` is not more than `total` (a negative bill discount, #525, must
     never render).

"Nothing to derive" -- no line discount, no bill discount, and not a แยก VAT
line -- also returns None: printing "55.00 = 55.00" would be noise, not a
derivation.

No discount string was found anywhere in the app to reuse (review R5
compares total/qty, not this) -- this is new code.
"""


def _parse_discount(discount):
    """-> (kind, label, multiplier, baht_amount).

    kind is None for blank/None input (multiplier 1.0, nothing to subtract).
    'percent' keeps the keyed text VERBATIM as label ("15+5%") and compounds
    each '+'-separated segment for the guard's own arithmetic -- never
    back-computed into one effective percentage (a real 15+5% deal must
    never read "-19.25%").
    'baht' parses a possibly comma-grouped amount taken off the WHOLE line
    (Express keys a baht discount per line, not per piece -- #525).
    """
    s = str(discount).strip() if discount is not None else ''
    if not s:
        return None, None, 1.0, None
    if s.endswith('%'):
        mult = 1.0
        for part in s[:-1].split('+'):
            mult *= (1 - float(part) / 100)
        return 'percent', s, mult, None
    amount = float(s.replace(',', ''))
    return 'baht', f'{amount:,g}', 1.0, amount


def invoice_line_formula(unit_price, qty, discount, total, net, vat_type):
    """Returns None, or a dict of the parts to render:
        {'unit_price': float,
         'discount_kind': 'percent' | 'baht' | None,
         'discount_label': str | None,   # as keyed (percent) or ':,g' (baht)
         'bill_discount_pct': float | None,
         'vat': bool}
    """
    try:
        kind, label, mult, baht_amount = _parse_discount(discount)
    except ValueError:
        # A keyed discount that isn't blank, percent-suffixed, or a clean
        # baht number (an Express typo, e.g. '15+%' or stray text) must
        # degrade to "no formula" — never 500 the whole customer page over
        # one bad row (code-review finding, #527).
        return None

    if kind == 'baht':
        recomputed = round(unit_price * qty - baht_amount, 2)
    else:
        recomputed = round(unit_price * qty * mult, 2)
    if total is None or abs(recomputed - total) > 0.01:
        return None
    if net is None or net > total + 0.005:
        return None

    bill_discount_pct = None
    if total and abs(total - net) > 0.005:
        bill_discount_pct = round((1 - net / total) * 100, 2)

    has_vat = (vat_type == 2)
    if kind is None and bill_discount_pct is None and not has_vat:
        return None  # nothing to derive

    return {
        'unit_price': unit_price,
        'discount_kind': kind,
        'discount_label': label,
        'bill_discount_pct': bill_discount_pct,
        'vat': has_vat,
    }
