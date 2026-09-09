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

⚠ This module owns ONE direction: net → cash. It deliberately does NOT own:

  * `net → ex-VAT` (dividing by 1.07). That is the carve-OUT used for quotes
    and the VAT sub-book, and it rounds differently — **ปัดขึ้น** 2 decimals to
    match how Express stores a line, see .claude/rules/quoting-and-pricing.md.
    Lives in models/vat_sub.py and its templates. Same constant, different rule;
    folding them together would apply the wrong rounding to one of them.
  * `net × 0.07`, the VAT amount shown on a document (templates/sales_doc.html).

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
