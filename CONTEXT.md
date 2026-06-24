# Sendy — Domain Glossary

> Ubiquitous language for the Sendy ERP. Glossary only — no implementation details.
> Add a term the moment a conversation reveals it was ambiguous.

## Conversions (the `/conversions` feature — "แปลงสินค้า")

- **สูตรแปลงสินค้า (conversion formula)** — the umbrella concept: a rule that consumes
  one or more input products from stock and produces an output product into stock.
  Stored in `conversion_formulas` (+ `conversion_formula_inputs`). Running one is a
  stock movement, not a sale. The `/conversions` page lists these formulas.

- **แปลง (run a conversion)** — *executing* an existing formula once (× a multiplier):
  decrement the inputs, increment the output. Distinct from *creating* a formula.

- **สร้างสูตรแปลง แพ็ค-ตัวหลวม (pack↔loose pairing)** — the **only** way to create or
  edit a conversion formula. Picks one pack product (e.g. unit แผง) + one loose product
  (e.g. unit ตัว) + a ratio, and creates a **reciprocal pair** of formulas in one step
  (แกะ: แผง→ตัว, and แพ็ค: ตัว→แผง). Backed by `upsert_pack_unpack_pair`; idempotent, so
  re-saving the same pack+loose with a new ratio **edits** the existing pair (no
  duplicate). Was previously labelled "จับคู่แพ็ค-ตัวหลวม". Editing a formula reopens
  this same screen, prefilled.

- **สูตรขั้นสูง (advanced formula) — REMOVED.** Formerly a general N-inputs → 1-output
  builder for manufacturing/re-packing. Removed because it overlapped with the pairing
  tool and was never used (0 of the formulas in use). See `docs/adr/0001`.

- **ทิศเดียว (one-way / partnerless)** — a pack/unpack formula whose reciprocal half is
  missing. Flagged for review because a pair is normally created together.

- **pack vs loose** — "pack" = a product sold as a multi-unit bundle (units like
  แผง/แพ็ค/ชุด/กล่อง/โหล); "loose" = the single-piece form (ตัว/อัน/ชิ้น/ใบ/ดอก).
  The boundary is **not** derivable from `unit_type` alone — the unit vocabulary is
  inconsistent, so the two are related explicitly via a pack↔loose pairing, never
  inferred by filtering on unit.
