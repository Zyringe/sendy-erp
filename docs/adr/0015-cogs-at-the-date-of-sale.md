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
