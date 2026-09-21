# ADR 0015 — ต้นทุนขาย is read at the date of sale, so a closed month stops moving

Status: Accepted · 2026-09-19 · builds on ADR 0014

## Context

`/accounting` computed ต้นทุนขาย as `qty in base units × products.cost_price`, where `cost_price` is
the product's **current** weighted average. That makes the figure a live re-derivation rather than a
period measurement: it changes whenever the average changes, for months that closed long ago, with no
sale and no purchase involved.

This is not theoretical. On **2026-09-18 at 08:52:55–57** something rebuilt every row of
`product_cost_ledger` (4,709 rows / 1,754 products) and updated `cost_price` on 147 products. The
Jan–Sep 2026 ต้นทุนขาย moved from ฿1,819,673.96 to ฿1,819,963.42 — **฿289.46, between two reads on one
morning**. Replaying the 147 old→new costs over the window's base quantities ties to that ฿289.46 to
the satang.

Two further facts about that event matter for this decision:

- **No route or scheduler in the app can do it.** Every recalc entry point is per-product or
  file-scoped. A whole-population rebuild is only produced by a human-run script under `scripts/`.
- **The data names no actor.** `audit_log` holds the 147 `cost_price` updates with `user` NULL — but a
  known-human UI cost edit the previous day is also NULL (only 2,862 of 227,906 audit rows name a user
  at all), so NULL distinguishes nothing. **Who ran it is unknown and could not be determined.** That
  is a gap in the audit trail, not evidence of anything.

Whatever ran it, the lesson is the same: as long as a closed period's cost is read from a mutable
current-cost column, the period figure is only ever "true as of this page load".

The convention is unambiguous (researched against primary sources; see ADR 0014). NPAEs 8.15: cost of
sales is the **carrying amount at the date of sale**, expensed in the period of the related revenue,
and 18.11 names the discipline — **การจับคู่รายได้และค่าใช้จ่าย**. A closed month may move only for an
**error** (5.18, restated and disclosed) or a **policy change** (5.12), both with mandatory
disclosure; พ.ร.บ.การบัญชี ม.39 penalises a silent rewrite of the books. Permitted cost formulas are
ราคาเจาะจง / FIFO / ถัวเฉลี่ยแต่ละงวด only — LIFO is absent from the standard entirely.

Feasibility, measured on prod: `product_cost_ledger` carries one row per cost-changing **IN** event
(`PURCHASE` 3,788 · `INITIAL` 884 · `CONVERSION_IN` 37 — no sale row ever enters it) with a running
`wacc_after` per base unit, so "the cost as it stood on date D" is the last row at or before D.
Coverage of the 2,878 Jan–Sep 2026 cost-bearing sales lines: **2,440 (84.8%)**, and **≥98.3% from
2026-03-03 onward** — but only **446 of 850** Jan–Feb lines, because the `INITIAL` rows reset every
product's cost to its opening cost on 2026-03-03 and the app deliberately discards the basis before
that.

**The stake is small and that is the point.** Over 2026-03-03 → 09-30: historical ฿1,259,563.34 vs
current-WACC ฿1,257,687.32 = **+฿1,876.02 (+0.15%)**, differing on 165 of 1,994 matched lines. We are
not buying accuracy. We are buying a number that stops changing.

## Decision

1. **From 2026-03-03, ต้นทุนขาย is read at the date of sale** from `product_cost_ledger`'s running
   `wacc_after`, and a closed month's figure is therefore fixed.
2. **2026-01 and 2026-02 stay on ทุนเฉลี่ยวันนี้ and the page says so.** The ledger cannot reach
   before the opening-cost reset, and pretending those two months are on the same basis would be the
   silent rewrite the standard warns about.
3. A closed month's ต้นทุนขาย changes only as a **disclosed correction**, never as a side effect of a
   cost recalculation.

## Considered options

- **Leave it on current WACC** — rejected: it makes the defect permanent, and it is the mechanism
  behind the 2026-09-17 disagreement that started this work.
- **Restate Jan–Feb from the pre-cutover ledger** — rejected on measurement. The pre-2026-03-03
  `wacc_after` values are a basis the app discards, and they include `hist = 0.0000` rows (one line
  alone worth −฿30,000); the whole-window historical figure came out −3.08%, an artefact, not a truth.
- **Mark Jan–Feb unreadable** (reuse the incomplete-month mechanism) — rejected as heavier than the
  problem: those months are readable, just on a declared different basis.

## Correction, 2026-09-21 (implementation, #593 stories 1-7)

Two statements above are measurably wrong. This ADR's own decision 3 requires a closed figure to
change only as a **disclosed correction**, and the standard it cites requires an error to be
restated rather than quietly overwritten, so they are corrected here rather than edited away.

**1. "their cost is ฿0 regardless" is false.** The lines sitting on products with no ledger row
carry **฿3,080** of real cost over 2026-03-03 → 09-30 (measured on prod 2026-09-20). That
parenthetical is the sentence that made story 4 look like pure disclosure; following it would mean
costing real shipments at zero, which trades a ฿1,876 problem for a ฿3,080 one. Those lines keep
`products.cost_price` and are counted in the new `no_ledger_lines`.

**2. The +฿1,876.02 was measured with a lookup this ADR's own decision forbids.** "The cost as it
stood on date D" was taken as the last ledger row at or before D, with no floor. But `wacc.py`
writes an `INITIAL` row only when `opening_cost > 0`, so **871 products have none** and 51
post-cutover lines resolve straight back to a pre-2026-03-03 `PURCHASE` row — exactly the basis the
Considered-options section rejects as an artefact. Flooring the lookup at the cutover costs ฿0.67.

Separately, `wacc_after = 0` is a **sentinel, not a cost**: the walk freezes the running average at
0 when stock goes negative and the write guard deliberately leaves `cost_price` alone, so for that
class the ledger is the worse source (prod pid 714 carries ledger `0.0` against `cost_price` 7.0;
182 pre-cutover rows carry 0). Put ruled 2026-09-20 that the recorded cost wins, because goods that
left the warehouse had one. That costs ฿336.00.

**The implemented movement is therefore +฿2,212.69 over Jan–Sep 2026, not +฿1,876.02**, measured on
prod 2026-09-21 by running the shipped expression itself. The +0.15% framing is unchanged and so is
everything the Decision section says. What changed is that the number now matches the decision.

**What "a closed month is fixed" does and does not mean.** Decision 1 above says a closed month's
figure "is therefore fixed". Narrow that: it becomes immune to `cost_price` drift, which is what
actually moved it. `product_cost_ledger` is DELETEd and rebuilt per product by `wacc.py`, so a
change to historical `transactions` — a re-import, a unit rebase — or to the walk itself still moves
history, as decision 3's disclosed-correction case. Measured on a copy of prod 2026-09-20:
rebuilding all 1,755 products moved closed-month ต้นทุนขาย by **฿0.00**, with 1,748 products
reproducing exactly and 7 differing by at most ฿0.000021 of float re-accumulation noise. The page
states the narrower claim.

## Consequences

- Jan–Sep 2026 ต้นทุนขาย rises ~฿1,876 (+0.15%). Per-month movement is smaller still.
- **The statement gains a per-month basis label.** Two bases coexist in 2026 by design; an unlabelled
  chart spanning January to September would be comparing two things.
- 54 lines sit on products with no ledger row at all (their cost is ฿0 regardless) and 1 line is
  unmapped — these are disclosed as `no_cost` / `unknown_ratio` counts, not silently costed.
- ⚠ **The audit gap stays open until fixed separately.** Cost writes must record their actor, or the
  next 08:52-style event will be equally unexplainable. Tracked as its own work item; this ADR does
  not close it.
- This supersedes nothing, but it does retire the reasoning in `models/accounting.py`'s docstring that
  described current-WACC COGS as acceptable.
