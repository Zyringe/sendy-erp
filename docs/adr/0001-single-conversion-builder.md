# ADR 0001 — One conversion builder (pack↔loose pairing), remove the advanced builder

Status: Accepted · 2026-06-24

## Context

The `/conversions` (แปลงสินค้า) feature had **two** ways to create a conversion formula:

1. **Pack↔loose pairing** (`/conversions/pair`, `upsert_pack_unpack_pair`) — pick a pack
   product + a loose product + a ratio; auto-creates the reciprocal pair (แพ็ค + แกะ).
2. **Advanced builder** (`/conversions/new`, `conversions/form.html`) — a general
   N-inputs → 1-output formula builder, also used as the per-formula **edit** screen
   (`/conversions/<id>/edit` rendered the same template).

Two "create formula" entry points sat side by side on the list page and overlapped
conceptually, which confused the owner. The data showed the advanced builder was dead:
**116 formulas, 100% pack↔loose pairs, 0 multi-input, 0 made by the advanced builder.**

## Decision

Remove the advanced builder entirely (button, `conversion_new`, `conversion_edit`,
`form.html`). The pack↔loose pairing tool becomes the **sole** create *and* edit path:

- It is idempotent (dedup key = output product + input set), so re-saving the same pair
  with a new ratio updates both formulas — that *is* editing.
- The per-row pencil "แก้ไข" reopens the pairing screen **prefilled** with that pair's
  pack, loose, and ratio (derived server-side from the formula).

## Consequences

- One mental model: every formula is a pack↔loose pair, created and edited on one screen.
- **Lost capability:** multi-input / non-pair "manufacturing" formulas can no longer be
  authored in the UI. This was unused (0 rows). If genuinely needed later, restore the
  builder from git rather than re-deriving it.
- A formula that is *not* pair-shaped (single input, pack/loose structure) cannot be
  edited via the pairing screen. None exist today; if one ever does (e.g. hand-inserted),
  the edit entry must fail loud rather than mis-prefill.
- No schema/migration change; `conversion_formulas` and `upsert_pack_unpack_pair` are
  unchanged. Reversible via git.
