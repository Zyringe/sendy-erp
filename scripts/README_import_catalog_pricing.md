# `import_catalog_pricing.py` — Operator runbook

One-shot CSV → Sendy importer for catalog pricing data. Reads the output of
`normalize_base_price.py` and populates `products.base_sell_price`,
`product_price_tiers`, and `promotions` in a single atomic transaction.

## TL;DR

```bash
cd ~/Sendai-Boonsawat/sendy_erp

# 1. Dry-run first — confirms counts, no writes
~/.virtualenvs/erp/bin/python scripts/import_catalog_pricing.py \
  --csv "/path/to/normalized.csv"

# 2. If counts look right → commit (auto-backs-up DB first)
~/.virtualenvs/erp/bin/python scripts/import_catalog_pricing.py \
  --csv "/path/to/normalized.csv" \
  --commit
```

## What the script does

For each CSV row (skipping rows with non-integer `product_id`):

| CSV field | Sendy write |
|---|---|
| `base_sell_price` (numeric) | `UPDATE products SET base_sell_price = ?` — skipped if value already matches |
| `tier1_qty_label` + `tier1_price` + `tier1_note` | `INSERT INTO product_price_tiers` |
| `tier2_qty_label` + `tier2_price` + `tier2_note` | `INSERT INTO product_price_tiers` |
| `extra_tiers_json` (array) | One `INSERT INTO product_price_tiers` per entry |
| `special_price` (numeric > 0) | `INSERT INTO promotions` with `promo_type='fixed'` |
| `promo_type` + `promo_value` + bundle/gift fields | `INSERT INTO promotions` carrying all relevant cols |

All writes happen inside a single `BEGIN/COMMIT`. Any failure rolls back ALL rows.

## Promo-name labels

INSERTed promo rows carry `promo_name` labels so you can filter / clean up later:

- `catalog 2026-05-25 (special_price)` — from the `ราคาพิเศษ` column
- `catalog 2026-05-25 (promo)` — from the `โปรโมชั่น` column

To find all imported promos: `SELECT * FROM promotions WHERE promo_name LIKE 'catalog 2026-05-25%';`

## CLI flags

| Flag | Default | Purpose |
|---|---|---|
| `--csv PATH` | (required) | Input CSV path |
| `--db PATH` | `inventory_app/instance/inventory.db` | Sendy DB path |
| `--commit` | off (dry-run) | Actually write to DB |
| `--limit N` | none | Process only first N rows (useful with `--dry-run` for sampling) |
| `--sample N` | 10 | Number of rows to show in the diff preview |
| `--no-backup` | off | Skip the auto-backup before `--commit` (NOT recommended) |

## Idempotency

**This is a one-shot importer.** Re-running on an already-imported CSV will:
- Fail loudly on `product_price_tiers` via `UNIQUE(product_id, qty_label)` constraint
- Create DUPLICATE `promotions` rows (no UNIQUE on that table)
- `base_sell_price` UPDATEs are idempotent (no-op when value already matches)

If you need to re-import, manually clear the previous import first:

```sql
-- WARNING: review COUNT(*) before each DELETE
BEGIN;

DELETE FROM promotions
 WHERE promo_name LIKE 'catalog 2026-05-25%';

-- Tier deletion is harder — there's no batch-label column. Either delete
-- by product_id (if you know which products were imported), or just rerun
-- after dropping ALL tiers (only do this if no other source has populated them):
-- DELETE FROM product_price_tiers WHERE created_at > '2026-05-25';

-- base_sell_price has no "revert" — manually UPDATE to 0 if you want a fresh start:
-- UPDATE products SET base_sell_price = 0 WHERE id IN (...);

COMMIT;
```

## Recovery / Rollback

The script makes a backup before `--commit`: `inventory.db.backup-pre-catalog-import-YYYYMMDD-HHMMSS`.

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
=== DRY RUN — no writes ===
========================================================================

Rows processed:        1963
Rows flagged for review: 12

products.base_sell_price UPDATEs:  699
  from 0.0 → real value:           699
  from existing value → diff:      0

product_price_tiers INSERTs:       246
  tier1:                           236
  tier2:                           9
  extra_tiers_json (3+ tiers):     1

promotions INSERTs:                485
  percent                          356
  fixed                            66
  bundle                           30
  mixed                            33
  gift                             0
  (from special_price column):     66
  (from โปรโมชั่น column):           419

⚠ Flagged rows (12) — auto-imported but review the notes:
  ... per-row list ...

📄 Sample of first 10 planned writes:
  pid=1 BLT-MYM-SD-#230-4in-AC-UN-1
      UPDATE base_sell_price=30.0 (was 0.0)
      + promo fixed val=20.0
  ...
```

## Tests

`tests/test_import_catalog_pricing.py` — 19 cases:
- Per-row write planning unit tests (10 cases covering each input pattern)
- E2E on `tmp_db` fixture (9 cases including dry-run-vs-commit, audit-trigger fire, empty-row skip, non-integer-pid skip, UNIQUE collision on re-run, CHECK constraint rejection, atomic rollback on partial failure)

Run: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_import_catalog_pricing.py -v`

## Known limitations

- **No diff mode** — re-import requires manual cleanup. If you need to update existing imported rows, the script doesn't help; do it directly in SQL.
- **No per-row dry-run output of every write** — `--sample` shows the first N only. For full per-row inspection use `--limit N --dry-run`.
- **No customer-specific overrides** — all promo rows are baseline (no `customer_id`). Per-customer support is a future schema extension (1-line ALTER per mig 086 forward-compat note).
- **`gift` promo_type rarely produced** — most "free gift" rows in the source data classified as `mixed` (because they also had a percent or bundle component). Pure `gift` rows would only appear if a future CSV has "แถม X 20 ดอก" with no other promo content.
