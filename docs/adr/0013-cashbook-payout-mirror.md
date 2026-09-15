# ADR 0013 — Marketplace payouts source the cashbook by mirror, keyed by value

Status: Accepted · 2026-09-15 · extends ADR 0008 to income

## Context

`LEX` and `SPX` are the KBank accounts that receive Lazada and Shopee payouts. Put never
withdraws from them. Their cashbook rows stopped in March 2026 (last hand-keyed 03-10/03-11),
but the payouts kept arriving — 37 Lazada (฿60,278.16) and 36 Shopee (฿161,645.00) since
2026-01-01 as of the 2026-09-15 prod snapshot. Sendy already records every payout per bank
deposit (`marketplace_reconcile.reconcile_payouts` writes one `marketplace_payouts` row per
cycle). ADR 0008 made salary/advance/commission each have one source of truth in the
cashbook; this extends the same idea to marketplace income.

## Decision

- **Platform → account is fixed and hard-coded**: lazada → `LEX`, shopee → `SPX`. A missing
  or inactive destination account raises, never skips silently.
- **Mirror, not copy-once.** After every `reconcile_payouts` call commits, the destination
  account's payout-sourced rows are made to equal `marketplace_payouts` for that platform,
  `deposit_date >= '2026-01-01'`, exactly: insert what's missing, delete what's gone. Running
  it twice changes nothing (`cashbook_payout_mirror.mirror_platform`).
- **Identified by VALUE, not by row id.** `marketplace_payouts` is deleted and rebuilt on
  every reconcile (its `id` is not stable), so a cashbook row cannot FK to a payout row.
  Instead the natural key — platform + deposit_date + amount, plus an occurrence number when
  two payouts share all three (real: `108_marketplace_payouts_drop_unique.sql`) — is stored
  directly on the row (`payout_platform` / `payout_deposit_date` / `payout_amount` /
  `payout_occurrence`, migration 182). A row is payout-sourced iff `payout_platform IS NOT
  NULL`. A partial UNIQUE index over the four columns is the DB-level backstop against
  double-mirroring; ordinary manual rows (all four NULL) are untouched by it.
- **Never a "linked & locked" FK like salary/advance/commission** (ADR 0006/0008's
  `payroll_item_id` / `salary_advance_id` / `commission_payout_id`) — those link to a table
  whose rows are permanent. This is the same LOCK behavior (`_reject_if_payout_row`, mirrors
  `_reject_if_salary_row`) built on a value key instead, because the thing being pointed at
  does not have a permanent identity to point at.
- **Manual rows are never touched by the mirror, permanently** — not inserted into, not
  deleted, not adopted, even when one coincidentally shares a payout's amount and date. The
  ONE exception is a one-time, explicitly-run conversion
  (`scripts/convert_legacy_cashbook_payout_rows.py`) that turned the 12 of 15 pre-existing
  hand-keyed LEX/SPX rows that already represented a real payout (matched by amount + within
  a 2-day date window) into payout-sourced rows, so the mirror wouldn't double them. The 3
  that matched nothing (฿550/฿145/฿30 on 2026-03-04) stay manual. That script is never
  re-invoked automatically; mirror_platform's own logic contains no adoption path.
- **Transaction boundary**: the mirror runs after the reconcile commits, in its own
  try/except at the call site (`blueprints/marketplace.py`). A mirror failure surfaces as a
  visible warning; the import itself still succeeds. Because the mirror is idempotent, the
  next import repairs it.
- **Row shape**: income, category `ยอดขายของ`, dated the payout's `deposit_date`, described
  `"<Platform> โอนเงิน (N ออเดอร์)"`, `created_by = 'ระบบ'`.

## Considered options

- **FK to `marketplace_payouts.id`** — rejected: the table is deleted and rebuilt on every
  reconcile, so the id is not a stable reference (this is documented on the table itself, mig
  108).
- **Copy-once on first sight, never re-check** — rejected: a payout whose amount changes on a
  later reconcile (rare, but the table IS a rebuild) would leave a stale cashbook row with no
  mechanism to correct it. Steady-state mirroring self-heals instead.
- **Auto-adopt any manual row matching a payout's amount+date, every run** — rejected: a
  manual row Put keys later that happens to coincide with a payout's amount and date would be
  silently absorbed forever, which is a different (and worse) failure mode than a visible
  duplicate Put can clean up. The one-time conversion is a deliberate, reviewed, single
  action — not a standing behavior.

## Consequences

- LEX/SPX stop needing hand-keying for marketplace income; a missed month self-heals on the
  next marketplace import rather than needing a backfill.
- `2026-01-01` is a hard floor — payouts before it (Put confirmed pre-2026 LEX/SPX deposits
  exist, e.g. ฿301,973.21 Lazada + ฿541,320.00 Shopee) are deliberately out of the mirror's
  scope; the cashbook's LEX/SPX balance before that date depends on the two opening-balance
  rows Put keys by hand from the 2025-12-31 KBank statement (out of this ticket's scope).
- TikTok has no receiving cashbook account yet — `PLATFORM_ACCOUNT_CODE` only maps
  lazada/shopee; adding a third platform is a one-line map entry plus its own reconcile
  wiring, not a schema change.
- Schema: `cashbook_transactions` += `payout_platform` (TEXT), `payout_deposit_date` (TEXT),
  `payout_amount` (REAL), `payout_occurrence` (INTEGER), all nullable; partial UNIQUE index
  on the four `WHERE payout_platform IS NOT NULL` (migration 182).
