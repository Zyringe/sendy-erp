# Shopee Marketplace Data Hub — Design Spec

- **Date:** 2026-06-16
- **Status:** Approved design → ready for implementation plan
- **Repo:** `sendy_erp` (Sendy ERP, Flask + SQLite)
- **Owner:** Put · spec by Claudy (main thread) · marketplace memory owner = Chuya

## Problem

Today Sendy imports two of Shopee's three report files, and under-reads the one it does
import:

- **Order** file → imported into `marketplace_orders` + `marketplace_order_items`.
- **การเงิน / Income** file (`Income.โอนเงินสำเร็จ.*.xlsx`) → `parse_income_transfer.py`
  keeps only **3 of ~45 columns** (`order_sn`, `settled_at`, net payout). Every per-order
  fee line is discarded.
- **Balance** file (`my_balance_transaction_report.*.xlsx`) → **not imported at all.**

Two consequences:

1. **No per-order fee visibility.** Put can't see why a ฿55 order nets ฿35 — the
   commission / service / transaction / platform / ads-from-escrow / tax breakdown exists
   in the การเงิน file but is thrown away.
2. **Bank deposits can't be reconciled.** The settlement page's "ก้อนเงินเข้าบัญชี"
   matcher (PR #147) tries to match a typed bank amount against `settled_at`-ordered
   `actual_payout` prefix sums. But the real bank deposit grouping is the **weekly
   auto-withdrawal cycle**, keyed by wallet-credit *timestamp*, which lives **only** in the
   Balance file. So real deposits (e.g. ฿7,689 on 2026-06-16) fail to match.

### Verified ground truth (2026-06-16, prod snapshot mig 106)

- Shopee runs a weekly auto-withdrawal (`การถอนเงินอัตโนมัติ`, type `การถอนเงิน`) every
  ~Tuesday ~01:1x that zeroes the wallet to ฿0. **Each bank deposit = sum of all per-order
  income (`รายรับจากคำสั่งซื้อ`) credited between two consecutive withdrawals**, plus any
  `รายการปรับปรุง` (adjustments) in that window.
- Withdrawal 2026-06-16 01:17 = **฿7,689 = exactly 39 orders** credited 06-09 01:19 →
  06-16 01:17, **zero fees/adjustments** in that cycle. Prior withdrawal 06-09 = **฿5,890 =
  32 orders**. Both reconcile to the satang against the ERP.
- The "฿8,368" Put saw from Shopee was a **calendar-week** income view (orders dated
  06-08..06-14), *not* a transfer. The ฿679 gap vs ฿7,689 is a grouping-boundary artifact
  (drops 06-08 orders already paid in the prior cycle, adds 06-15 orders), **not a fee.**
  Lesson: never attribute a deposit-vs-week gap to "fees" without the balance ledger —
  fees/adjustments appear as explicit `รายการปรับปรุง` rows.

## Goal

A single place in Sendy to upload all Shopee marketplace files and get:

1. **Per-order fee breakdown + net payout** (the การเงิน "Income" sheet, fully captured).
2. **Exact bank-deposit reconciliation** — which orders are in each Shopee bank
   withdrawal, driven by the Balance file (not manual ticking).

### Non-goals (this build)

- Lazada / TikTok parsers (interface is pluggable; actual parsers later).
- Finance/cashbook posting of fees (ads-from-escrow, commission as expenses) → routed to
  Nami in a later phase.
- A full fee-analytics dashboard (later phase; basic order-level display is in scope).

## Scope decisions (confirmed by Put, 2026-06-16)

| Decision | Choice |
|---|---|
| Platforms now | **Shopee only**, pluggable parser interface for Lazada/TikTok later |
| Fee detail stored | **Key named buckets + `fee_raw_json`** (not all 45 cols, not just net) |
| Deposit reconciliation | **Balance-driven auto-reconcile**, supersedes manual tick (PR #147) |
| Upload UX | **One auto-detecting box** (like `/import-data` ขาย/ซื้อ/AR/AP) |
| Data model shape | **Two new tables** (`marketplace_order_fees`, `marketplace_wallet_txns`) + `marketplace_payouts`; keep `marketplace_orders` clean |

## The three files (what each provides)

| File | Sheet(s) | Key contents | Role |
|---|---|---|---|
| **Order** `Order.all.*.xlsx` | 1 | buyer, products/SKU, qty, address, status, order date | "what sold" — already imported |
| **การเงิน** `Income.โอนเงินสำเร็จ.*.xlsx` | `Income`, `Service Fee Details`, `Adjustment`, `Summary` | per-order: ราคาปกติ, ส่วนลด, ค่าจัดส่ง, ค่าคอมมิชชั่น, ค่าบริการ, ค่าธุรกรรมการชำระเงิน, ค่าธรรมเนียมโครงสร้างพื้นฐาน, ค่าธรรมเนียมเติมเงินโฆษณาจาก Escrow, ภาษี → **จำนวนเงินที่โอนแล้ว (net)**; fee % per order | **fees → net** (currently 3 cols only) |
| **Balance** `my_balance_transaction_report.*.xlsx` | `Transaction Report` | rows of `รายรับจากคำสั่งซื้อ` / `การถอนเงิน` / `รายการปรับปรุง` with timestamp, order_sn, signed amount, **running balance** | **net → bank deposit** (not imported) |

Reconciliation identity (per order, verified): `item_value − Σ(all fee lines) = net_payout`.
Reconciliation identity (per deposit): `Σ(order net in cycle) + Σ(adjustments in cycle) = withdrawal amount`.

## Architecture

Data flow: **Order (what) → การเงิน (fees → net) → Balance (net → bank deposit).**

### 1. Auto-detecting upload box
Extend `/marketplace/import` into one drag-drop box. A **file detector** sniffs each upload
(by sheet names + header signature, not just filename) and routes:
- Order file → existing order import (unchanged).
- การเงิน/Income → new rich Income parser.
- Balance → new wallet-ledger parser.
- Platform = Shopee for now (detector returns platform; interface ready for Lazada/TikTok).
- Diff-based, **idempotent**, preview → confirm (mirror `/import-data`). Unknown file → clear
  Thai error, no partial write.

### 2. Parsers (pluggable, TDD)
Define a `MarketplaceParser` protocol (`detect(file) -> bool`, `parse(file) -> rows`) so new
platforms slot in without touching the box.
- `parse_income.py` — extend today's `parse_income_transfer.py`: read the `Income` sheet into
  named fee buckets + `fee_raw_json` (full row), read `Adjustment` and `Service Fee Details`
  sheets. Header auto-detect already exists (`find_income_header_row`).
- `parse_balance.py` — read `Transaction Report` (banner above header ~row 17): emit wallet
  rows (timestamp, type, order_sn, signed amount, running_balance, description).

Parsers are the **risky** units → TDD first (per `erp-engineering-discipline`: parsers/regex/
schema get a failing test before implementation), with fixtures from the real files.

### 3. Data model (one migration, next NNN)
- **`marketplace_order_fees`** (1:1 per settled order):
  `id, platform, order_sn (UNIQUE per platform), item_value, fee_commission, fee_service,
   fee_transaction, fee_platform, fee_ads_escrow, fee_tax, shipping_net, fee_total,
   net_payout, fee_pct, fee_raw_json, source_file, created_at`.
- **`marketplace_wallet_txns`** (every Balance-file row):
  `id, platform, txn_time, txn_type (income|withdrawal|adjustment), order_sn (nullable),
   amount (signed), running_balance, description, source_file, created_at`.
  Idempotency key: `(platform, txn_time, txn_type, order_sn, amount)` or a row hash.
- **`marketplace_payouts`** (one row per `การถอนเงิน` = one bank deposit):
  `id, platform, deposit_date, amount, status, source_file, created_at`.
- **`marketplace_orders.payout_id`** → FK to `marketplace_payouts` (order → its bank deposit).
  (Reuse/retire the existing `payout_batch_id` + `payout_batches` from PR #147.)

### 4. Reconciliation engine (`marketplace_reconcile.py`)
From `marketplace_wallet_txns` ordered by `txn_time`: segment income rows into withdrawal
cycles (each `withdrawal` row closes a cycle). For each cycle write a `marketplace_payouts`
row and set `payout_id` on the orders in it. **Invariant asserted** (independent of construction):
`Σ(cycle order amounts) + Σ(cycle adjustments) == withdrawal.amount` within ฿0.01 — fail loud
on mismatch (catches an incomplete Balance file). Adjustments are recorded and attributed to
the cycle.

### 5. UI
- **Order detail** (existing marketplace order modal): add a "ค่าธรรมเนียม & เงินโอน" section —
  item value, each fee line, net, fee %, and "อยู่ในยอดโอนเข้าธนาคาร #X (date · ฿)".
- **Settlement page** (`/marketplace/settlement`): replace the manual enter-amount + tick flow
  with the **real deposit list** from `marketplace_payouts` (date · ฿ · #orders · click →
  order list with per-order fees). The old manual matcher (PR #147) is retired or left
  read-only.
- Restart Sendy after adding any route (werkzeug URL-map rule); curl each new/changed route
  for non-500; exercise real POST for the upload box.

## Phases (→ implementation plan)

| Phase | Scope | Verify |
|---|---|---|
| **P0** | Parsers (`parse_income`, `parse_balance`) + file detector, TDD with real fixtures | pytest green; parse sample files to expected rows |
| **P1** | Migration: 3 new tables + `marketplace_orders.payout_id` | migration applies on fresh + real DB; schema asserts |
| **P2** | Rich Income import wired + backfill historical การเงิน files | per-order `item−fees==net` reconciles; counts match file |
| **P3** | Balance import + reconciliation engine + backfill | each `marketplace_payouts` sum == withdrawal; ฿7,689→39 orders & ฿5,890→32 orders reproduce |
| **P4** | Unified auto-detecting upload box (preview/confirm, idempotent) | upload each of the 3 files via real POST; re-upload = no dups |
| **P5** | UI: order-detail fee section + settlement deposit list; retire manual tool | routes 200 after restart; Put confirms authed render |
| **P6** *(later)* | Fee dashboard; Nami/cashbook hook for ads-from-escrow + commission expense; Lazada parser | out of scope for first ship |

## Risks / notes

- การเงิน file only covers **settled** orders → unsettled orders have no fee row yet (expected;
  `marketplace_order_fees` is sparse until settlement).
- `settled_at` is date-only and ≠ wallet-credit time at cycle boundaries → **use the Balance
  ledger for deposit grouping, การเงิน for fees.** Never mix them for deposit math.
- Idempotency everywhere (diff-based) — re-importing the same/overlapping date-range file must
  not double-count. `created_at` is a business date, not insert time → diff by content key.
- `sendy_erp` is a **separate git repo** with PR + fetch-before-shared-branches discipline →
  ask Put before any commit/PR; `main` auto-deploys to Railway (merge = deploy). Pre-merge
  gate: boot the real app, curl routes, exercise the real upload POST.
- Two prod workers (`gunicorn -w 2`) → no per-worker in-memory state for import intent; use
  session or DB.
- The ads-from-escrow fee (`ค่าธรรมเนียมเติมเงินโฆษณาจาก Escrow`) and commission are real
  marketing/expense lines → finance interest (Nami) in P6, not silently dropped.

## Related prior work
- PR #96 marketplace order import · PR #135 settlement (Income 3-col) import · PR #142 order↔IV
  matching · PR #147 manual deposit-batch (superseded here). Marketplace memory: Chuya
  `marketplace_state.md` + open item `0z` (this build is the planned fix).
