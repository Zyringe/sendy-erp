# ADR 0014 — The internal P&L is accrual, is not a งบการเงิน, and prior-period costs get their own line

Status: Accepted · 2026-09-19

## Context

`/accounting` was a hybrid nobody had declared. **Revenue** was accrual (invoice date). **ต้นทุนขาย**
was accrual in timing but read against `products.cost_price` as it stands *now*. **ค่าใช้จ่าย** was
cash basis — a cashbook row's `txn_date`, i.e. the day money left. Three bases in one statement, and
no page or docstring said so.

The cost of not declaring it came due on 2026-09-17, when a management artifact and `/accounting`
disagreed on ส.ค. 2569 and the disagreement could not be settled: the argument was never about
arithmetic, it was about what the word "ผลจริง" meant. Two months of numbers were re-derived before
anyone noticed that "P&L" was naming two different figures in this app (this statement, and the
cashbook's **สุทธิเดือนนี้**) and that the statement's own vocabulary had no entry for **ต้นทุนขาย**
or **กำไรขั้นต้น** at all.

Put's ruling (2026-09-18) settled the audience first: **this number exists for his own monthly
decisions, and tax / the outside accountant are out of scope** — those run off the Express books.
He then asked for "a normal convention for other businesses such as us" rather than a house
invention. Researched against primary sources (TFRS for NPAEs ปรับปรุง 2565, พ.ร.บ.การบัญชี 2543,
ประมวลรัษฎากร), the convention is unambiguous on the points we needed, and one finding frames the
rest: **nothing in Thai law or standards regulates a monthly P&L at all** (NPAEs 4.3 requires
statements at least yearly, 4.4 makes interim reporting optional, พ.ร.บ.การบัญชี ม.10 closes every
12 months). So the convention for a month is to mirror the **annual** basis, because a month measured
differently cannot reconcile to the year it sits in.

- **Accrual is the only basis the standard admits.** NPAEs 3.2/3.3 name เกณฑ์คงค้าง and going concern
  as its only two assumptions; **เกณฑ์เงินสด appears zero times** in the standard. Cash-watching is
  common SME practice, but that is the *absence* of a convention, not one. The standard's answer to
  "I also need to see cash" is **two reports**, not one statement on a mixed basis.
- **Expenses belong to the month consumed** (NPAEs 3.20.2/3.18.2), not the month billed or paid.
- **Late bookkeeping is a completeness problem, not a basis problem** — which is why the incomplete-month
  reading is a separate mechanism and not a reason to abandon accrual.
- **An internal report is not a "งบการเงิน"** — that is a defined term (พ.ร.บ.การบัญชี ม.4).

The awkward case that forced the third decision: a ฿400,000 bonus labelled "โบนัสปี 68", paid
2026-03-09, plus ฿167,500 of staff bonuses paid 2026-01-31 — both confirmed by Put as compensation
for **FY2568** work. Under accrual neither belongs to a 2026 month. But Sendy holds no FY2568
statement, so moving them out of 2026 deletes ฿567,500 of real cost from every report the business
has.

## Decision

1. **The statement is accrual (เกณฑ์คงค้าง)**, and says so. Cash questions are answered by
   `/cashbook`, never by mixing a cash figure into this statement.
2. **It is labelled an internal report, never a งบการเงิน**, and never presented as a tax or
   statutory figure.
3. **A cost belonging to an earlier period gets its own line — ค่าใช้จ่ายของงวดก่อน — separate from
   ค่าใช้จ่ายดำเนินงาน.** The month's trading result is measured without it; the money stays visible.
4. **The statement is a งบกองรวม** (see ADR 0016) and carries that label.

Put chose (3) over both alternatives on 2026-09-18.

## Considered options

- **Cash basis throughout** — rejected: ต้นทุนขาย would become "cash paid for goods" (฿159,564 across
  nine months against ฿1.82m of COGS), destroying กำไรขั้นต้น, the one thing this statement measures
  that the cashbook cannot.
- **Full accrual including expenses, now** — the textbook answer, rejected for today: it needs a
  service period on every expense, which the cashbook does not capture, and adding that field means
  more work at entry time for a team already keying 41 days late (median, measured). Revisit if the
  entry lag ever closes.
- **Keep the undeclared hybrid** — rejected: it is what produced the 2026-09-17 argument.
- **Prior-period costs dropped from 2026 entirely** (the strictly correct accrual treatment) —
  rejected: with no FY2568 statement to receive them, ฿567,500 would disappear from every report.
  Correctness that creates a hole is worse than the disclosed approximation.
- **Prior-period costs left in the month paid** — rejected: it makes every March (bonus month) look
  like a disaster, permanently.

## Consequences

- **A month's ค่าใช้จ่ายดำเนินงาน is an approximation on the payment date**, deliberately, and must be
  disclosed as such. The categories where this actually bites are the lumpy ones (bonus, accounting
  fees, tax), and those are exactly what line (3) pulls out.
- **The prior-period line is not a dumping ground.** A cost goes there only when the period it belongs
  to is identified. "I am not sure which month" is not a prior-period cost.
- ⚠ **ม.65 ตรี(19) may read the ฿400,000 differently.** A bonus named for an accounting year and paid
  after that year closed is the fact pattern that provision targets, and if it were a distribution
  rather than compensation it would sit below the profit line entirely. **Put ruled it pre-agreed
  compensation for work (2026-09-19) and this statement follows that ruling.** The tax characterisation
  is a separate question owned by the outside accountant, not resolved here, and this ADR does not
  decide it.
- The convention research lives outside this repo (it is third-party knowledge, not ours):
  `~/FlawlessOS/wiki/finance/sme-management-pl-convention-th.md`.
