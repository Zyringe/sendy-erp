# `import_catalog_pricing.py` — Operator runbook

IDEMPOTENT CSV → Sendy importer for catalog pricing data (phase-2-finish-plan.md,
PR B / 2c). Reads the output of `normalize_base_price.py` and reconciles
`products.base_sell_price`, `product_price_tiers`, and `promotions`. A CSV row
is the desired state **only for the slots and tier labels it names** —
anything it doesn't mention is left alone. Re-running the identical file (at
any `--batch-date`) reports all-zero counters and changes nothing.

## TL;DR

```bash
cd ~/Sendai-Boonsawat/sendy_erp

# 1. Dry-run first — confirms counts, no writes persisted (the planner still
#    runs for real against a transaction; it just rolls back at the end)
~/.virtualenvs/erp/bin/python scripts/import_catalog_pricing.py \
  --csv "/path/to/normalized.csv" \
  --batch-date 2026-MM-DD

# 2. If counts look right → commit (auto-backs-up DB first)
~/.virtualenvs/erp/bin/python scripts/import_catalog_pricing.py \
  --csv "/path/to/normalized.csv" \
  --batch-date 2026-MM-DD \
  --commit
```

`--batch-date` is **required** — it's the date this batch is actually being
applied (normally today), and is written ONLY to `promotions.date_start` /
`date_end` for any promo row this run changes. It is never inferred or
defaulted. Base prices and tiers carry no date.

## What the script does

For each CSV row (skipping rows with non-integer `product_id`):

| CSV field | Sendy write |
|---|---|
| `base_sell_price` (numeric) | `UPDATE products SET base_sell_price = ?` — skipped if value already matches |
| tier1 / tier2 / `extra_tiers_json` | INSERT a new `(product_id, qty_label)`, UPDATE an existing one in place if price/note differ, leave alone if identical or if a DB tier isn't named by the CSV at all |
| `special_price` (numeric > 0) | one promo intent, `promo_type='fixed'` |
| `promo_type` + `promo_value` + bundle/gift fields | one promo intent, any type |

A promo intent is reconciled against whatever promo(s) currently occupy the
**same slot** (price and/or qty — see `models/promotions.py::promo_slot_sql`)
on that product:
- identical offer (same type/value/bundle/gift fields, `promo_name` ignored)
  → **preserved untouched**, regardless of `--batch-date`.
- changed or new → the old occupant is **date-closed** (`date_end = batch_date
  − 1`, `is_active` untouched — never flips early) and the new one is
  inserted at `date_start = batch_date`, `source='catalog-import'`.

**One failure rule.** Every validation failure — two rows for one product,
duplicate tier label in one row, a CSV row producing two price-slot promos in
one row, a row that would only partly replace a `mixed` promo occupying both
slots, or a `--batch-date` that would close a promo before its own start —
**aborts the WHOLE run before any write**, naming the offending
product/row/promo IDs, and exits non-zero. Never skip-a-row-and-continue.

All reads and writes happen inside one `BEGIN IMMEDIATE` transaction. Any
failure rolls back everything; dry-run always rolls back.

## Promo-name labels

INSERTed promo rows carry a `promo_name` derived from `--batch-date` (cosmetic
only — reconciliation never compares on it):

- `catalog <batch-date> (special_price)` — from the `ราคาพิเศษ` column
- `catalog <batch-date> (promo)` — from the `โปรโมชั่น` column

## CLI flags

| Flag | Default | Purpose |
|---|---|---|
| `--csv PATH` | (required) | Input CSV path |
| `--db PATH` | `inventory_app/instance/inventory.db` | Sendy DB path |
| `--batch-date YYYY-MM-DD` | (required) | ISO date this batch is applied — see above |
| `--commit` | off (dry-run) | Actually persist the writes |
| `--limit N` | none | Process only first N rows (useful with dry-run for sampling) |
| `--sample N` | 10 | Number of rows to show in the diff preview |
| `--no-backup` | off | Skip the auto-backup before `--commit` (NOT recommended) |

## Idempotency

Re-running the **same file** at the same or a newer `--batch-date` reports:

```
products.base_sell_price:  0 updated
product_price_tiers:       0 inserted / 0 updated
promotions:                0 closed / 0 inserted
```

and changes nothing (no `date_start` moves, no duplicate rows). If you need
to change something, just fix the CSV and re-run — there is no manual
cleanup step.

## Recovery / Rollback

The script makes a backup before `--commit`:
`inventory.db.backup-pre-catalog-import-YYYYMMDD-HHMMSS`.

To revert to pre-import state:

```bash
# 1. Stop Sendy
sendy-down

# 2. Restore backup
cd ~/Sendai-Boonsawat/sendy_erp/inventory_app/instance
mv inventory.db inventory.db.broken-from-import-attempt
mv inventory.db.backup-pre-catalog-import-YYYYMMDD-HHMMSS inventory.db

# 3. Restart Sendy
sendy-up
```

## What the dry-run output looks like

```
========================================================================
=== DRY RUN — no writes persisted ===
========================================================================

Rows processed:          1962
Rows flagged for review: 225

products.base_sell_price:  68 updated
product_price_tiers:       0 inserted / 0 updated
promotions:                1 closed / 5 inserted

⚠ Flagged rows (225) — auto-imported but review the notes:
  ... per-row list ...

DRY RUN complete (rolled back). To commit, re-run with: --commit
```

If `--batch-date` differs from today AND this run changes a tier's price, a
warning prints naming both dates — tier price changes are epoch-dated at
import time (wall clock), not `--batch-date`; promo changes carry
`--batch-date`. See phase-2-finish-plan.md B0.

## Tests

`tests/test_import_catalog_pricing.py` — run:
`cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_import_catalog_pricing.py -v`

Covers per-row planning, the idempotency headline (re-run at the same and a
newer batch date → all zeros), the tier reconciliation matrix, promo
preserve-vs-close+insert, the `mixed`-occupant partial-replacement refusal,
the two-price-slot-in-one-row abort, file-shape validation (duplicate
product_id / tier label), a concurrent-writer exclusion check, dry-run vs
commit producing the identical plan, and the pre-existing audit-trigger /
CHECK-constraint / atomic-rollback behavior.

## Known limitations

- **No customer-specific overrides** — all promo rows are baseline (no
  `customer_id`). Per-customer support is a future schema extension.
- **`gift` promo_type rarely produced** — most "free gift" rows in the source
  data classify as `mixed` (they also carry a percent or bundle component).
- **A `mixed` occupant split across two intents in the same row** (e.g. a CSV
  row supplying both a price-slot and a qty-slot intent that together replace
  one existing `mixed` row) is reconciled as "always changed" — there is no
  attempt to detect that two new rows are semantically replacing one old
  `mixed` row 1:1; it just closes the old one and inserts both new ones. Only
  matters if a future CSV shape produces this combination.
