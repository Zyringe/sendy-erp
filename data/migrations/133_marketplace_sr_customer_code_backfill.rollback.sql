-- No-op rollback (same pattern as mig 131). The forward migration relabeled
-- blank marketplace SR customer_code values to their real platform code. Blanking
-- them again would re-introduce the /customers duplicate bug it fixed and has no
-- semantic value, so there is deliberately nothing to roll back. It would also be
-- unsafe: a targeted "set back to blank" could catch a legitimately-coded SR row
-- imported after this migration.
SELECT 1;
