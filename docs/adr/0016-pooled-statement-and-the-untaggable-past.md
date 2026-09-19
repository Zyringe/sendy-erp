# ADR 0016 — The internal P&L is a pooled statement, and the un-tagged past can never be split

Status: Accepted · 2026-09-19 · builds on ADR 0014

## Context

Two juristic persons share this workspace — **BSN** (บุญสวัสดิ์ นำชัย) and **SD** (เซ็นไดเทรดดิ้ง) —
under one director, and the workspace's own standing rule is that P&L is kept **separate per company**.
`/accounting` does not do that, and cannot today:

- **Revenue is BSN only.** The ERP holds BSN sales; SD does not yet invoice on its own, so even the
  B2C marketplace sales are booked under BSN.
- **Expenses are whatever flowed through the shared accounts.** `cashbook_transactions` has no company
  column. The operating cash runs through `ชฎามาศ`, `392`, `กิติยา`, `Put-Cash` — bank and personal
  accounts that are not scoped to an entity.

So the statement subtracts a two-company expense pool from one company's revenue. That understates
profit by construction, and nothing on the page said so.

Asked which it was, Put's answer (2026-09-18) was that the accounts genuinely mix and the mix cannot be
recovered from what is stored. The convention agrees this is a real situation with a defined honest
answer: sibling companies under common control get **two separate statements**, never a merged one
("consolidation" is the wrong concept for siblings and NPAEs 4.23 does not require it anyway), and the
mechanism is **รายการระหว่างกัน** through ลูกหนี้/เจ้าหนี้กิจการที่เกี่ยวข้องกัน plus เงินทดรองจ่าย
(a documented reimbursement is not revenue — ข้อหารือ 0706/9958). Until that exists, what can honestly
be reported is **one pooled statement, labelled pooled**.

Put then declined to start tagging company at entry time (2026-09-19), having been shown the cost:
**a bank movement carries no company intent, so a month that passes un-tagged can never be split apart
afterwards.** This is not a reversible deferral. Every month of delay is a month permanently pooled.
He accepted that in exchange for not adding a decision to every row a team already keys 41 days late
(median, measured on prod).

## Decision

1. **`/accounting` is a งบกองรวม and is labelled as one** — BSN revenue against a shared expense pool.
2. **No company tagging at entry time**, for now. Not "not yet, then backfill" — there is no backfill.
3. **Per-company statements are out of scope** until รายการระหว่างกัน exists, which is a separate piece
   of work with its own trigger (SD beginning to invoice on its own is the natural one).
4. The label is not optional decoration. An unlabelled version of this statement is a wrong number
   presented as a right one.

## Considered options

- **Tag every row with a company from next month** — rejected by Put: it puts a judgement call on every
  line at entry time, against the known constraint that only one person handles digital work and the
  keying is already months behind.
- **Tag only the rows that are obviously SD** (marketplace shipping, platform fees) and treat the rest
  as BSN — offered and rejected. It would have recovered the separable part at almost no entry cost, but
  Put's call is that SD is not invoicing on its own yet, so the split has nothing to serve.
- **Allocate shared overhead by a written key** (the convention's mechanism for genuinely shared cost) —
  premature: an allocation key on top of an un-split cash pool would be a second estimate stacked on a
  first.

## Consequences

- **The pooled figure understates profit, structurally and permanently, for every month up to whenever
  tagging starts.** Say it on the page; do not "improve" the number by guessing a split.
- **The past is closed.** Any future request for per-company history for 2026 has one honest answer: it
  cannot be produced from this data. Record that here so nobody re-opens it as a data-cleaning task.
- ⚠ **Related-party exposure exists regardless of what this statement reports.** Two commonly-owned
  companies moving cash between them without documentation is reachable by ประมวลรัษฎากร ม.65 ตรี(13)
  (whose expense is it), ม.65 ทวิ(4) (was it priced), and ม.71 ทวิ (do the terms shift profit — its
  *adjustment* power carries no revenue threshold, unlike its reporting form at ฿200m). On these facts
  all three land on the same undocumented movements, and one fix closes all three: record every crossing
  through ลูกหนี้/เจ้าหนี้กิจการที่เกี่ยวข้องกัน. **The trigger to watch is not size but a CIT rate
  divergence** between the two companies (SME bands vs flat 20%) or a carried-forward loss.
  This is a legal/tax matter owned outside this repo — background in
  `~/FlawlessOS/wiki/finance/sme-management-pl-convention-th.md` §6. This ADR records it so the
  reporting decision is not mistaken for a ruling that there is no exposure.
