# Product-label data is a standalone dataset, not tied to `products` (v1)

Own-brand product hang-tags (name, brand, EAN-13 barcode, วิธีใช้, ข้อแนะนำ, บรรจุ, size, a
fixed company block, and a fixed `ราคา : ตรวจสอบ ณ จุดขาย` line) live in an external 2017 Excel
master of ~1,104 rows. Put wants this data **in Sendy, editable**, so staff can print labels
self-service to a GoDEX thermal printer. **We import the Excel as-is into a standalone
`product_labels` table keyed by its own EAN-13 barcode, and edit/print from it directly — we do
NOT map rows onto `products` for v1.**

## Why

Mapping the label master onto the product catalogue is not viable and would block printing behind a
reconciliation project that would likely never finish cleanly:

- The label master is a **superset** of the live own-brand catalogue — **1,104 label rows vs 682
  own-brand products** — so hundreds of labels have no live product at all (discontinued / never
  imported / 2017-era).
- **No reliable key exists.** The Excel `No` / `ลำดับ` columns are a 2017 internal numbering: their
  values coincide with `products.id` **and** with the archived `legacy_product_sku_map.sku`, but the
  *names* disagree ~99% of the time (7–12 name agreements out of 1,104). Pure name-matching gives
  **12 exact of 1,104** (610 have no plausible match). Name-matching this catalogue is a known trap
  (see `verification-discipline` rule).
- A label is **self-contained** — it never shows live price or stock (price is always the fixed text
  "ตรวจสอบ ณ จุดขาย"), so it does not need the product record to print.

## Consequences

- The label's product **name lives separately** from Sendy's product master — renaming a product in
  Sendy will not propagate to its label; you edit the label. Acceptable for physical stickers that
  reprint rarely.
- **Future link to `products` (Put wants this later)** will be via a nullable `product_id` FK matched
  by **barcode** (populate product barcodes, then match), **not** via `No`/`ลำดับ`/legacy sku — those
  are confirmed dead ends.

## Considered options

- **(A) Tie each label to a Sendy product** — rejected: requires the mapping swamp above before a
  single label can print.
- **(B) Standalone `product_labels` table** — chosen: trivial import, ships fast, matches Put's ask.
