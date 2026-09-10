"""Canonical answer to "what did the customer actually pay for this line?".

`sales_transactions.net` is ALWAYS ex-VAT. Turning it into cash depends on
`vat_type`:

    0  ยกเว้น VAT                        → customer paid `net`
    1  ไม่บวก VAT ตอนเก็บเงิน (หน้าร้าน)   → customer paid `net`
    2  แยก VAT (ต้องเพิ่ม VAT 7%)          → customer paid `net × 1.07`

sendy_erp/CLAUDE.md already calls this "Idiom เดียวทั้ง codebase". It was not:
until 2026-09-09 it was typed out by hand at 23 call sites in four spellings
(`vat_type = 2` / `vat_type=2`, aliased and not, one ending `AS spend`), and
`tests/test_vat_math.py` imported nothing from the app — it re-typed the SQL
and checked its own typing, so deleting `* 1.07` from a production query left
the suite green.

Getting this rule wrong is not cosmetic. Before 2026-05-19 the documentation
had it INVERTED (`1 → ×1.07, 2 → ÷1.07`) and payments_alloc/cashflow summed a
bare `net`; every fully-paid `แยก VAT` bill then read as "จ่ายเกิน 7%",
producing roughly ฿446k of customer credit that did not exist.

⚠ This module owns both directions of the CONVERSION and nothing else.

`net_from_cash()` is the carve-OUT: a VAT-inclusive price the customer pays,
back to the ex-VAT figure. (Not "net → ex-VAT" — `net` is already ex-VAT; what
gets divided is a customer-facing price.) Raw quotient, deliberately unrounded:
its user, models/vat_sub.py::compute_badge, divides again by a unit ratio and
compares with `>`. It prints no document, so it must not round like one.

  * **Document rounding is NOT here.** A quotation line reproduces how a human
    keys Express: **ปัดขึ้น** 2 decimals on the unit ex-VAT price, half-up on the
    line amount, one VAT line off the invoice total — see
    .claude/rules/quoting-and-pricing.md. That lives with whatever renders the
    quotation, and folding it in here would round every comparison too.
  * `net × 0.07`, the VAT amount shown on a document (templates/sales_doc.html),
    is a third thing again — the tax itself, not a conversion.

⚠ compute_badge has **no production caller** — it is exported through
models/__init__.py and exercised only by tests. The badge a user actually sees
is computed in JavaScript, by a line-for-line twin of that function in
templates/vat_sub/product_view.html (`const exVat = pricePerUnit / 1.07`), which
recomputes as the user types a price. So the constant still exists twice, in two
languages, and a `.py`-only sweep cannot see the live one. Collapsing that pair
is its own piece of work, not a constant to move.

⚠ No rounding here. Callers round where they always did — some per row, some
after SUM() — and moving that inside would silently shift every one of them.

Python 3.9 — no `X | None` syntax.
"""

# The two constants the whole rule is made of. Both spellings below are built
# from these, so the SQL and the Python cannot drift apart.
VAT_MULTIPLIER = 1.07
VAT_TYPE_ADDS_VAT = 2


def cash_sql(alias=''):
    """SQL expression converting `net` to what the customer paid.

    `alias` is the table alias used in the caller's FROM clause ('' when the
    table is unaliased), e.g. cash_sql('st') -> "CASE WHEN st.vat_type = 2 THEN
    st.net * 1.07 ELSE st.net END". Wrap it in SUM()/ROUND() yourself; this
    returns the per-row expression only.
    """
    p = '{}.'.format(alias) if alias else ''
    return ("CASE WHEN {p}vat_type = {t} THEN {p}net * {m} ELSE {p}net END"
            .format(p=p, t=VAT_TYPE_ADDS_VAT, m=VAT_MULTIPLIER))


def cash_from_net(net, vat_type):
    """Python twin of cash_sql(), for the row-at-a-time callers.

    Returns None for a NULL net, exactly as the SQL yields NULL — callers
    distinguish "no data" from "฿0.00". Any vat_type that is not 2 falls
    through unchanged, matching the CASE's ELSE branch.
    """
    if net is None:
        return None
    if vat_type == VAT_TYPE_ADDS_VAT:
        return net * VAT_MULTIPLIER
    return net


def net_from_cash(cash):
    """The inverse: a VAT-inclusive price back to its ex-VAT figure.

    Raw quotient, no rounding — see the module docstring. Returns None for a
    NULL input, the same contract as cash_from_net().
    """
    if cash is None:
        return None
    return cash / VAT_MULTIPLIER
