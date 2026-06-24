-- Lazada per-statement (รอบบิล) settlement timestamps + amounts, from the wallet
-- "Deposit/Settlement" rows. These carry the precise (~02:2x) time each statement
-- settled into the Lazada balance. reconcile_payouts re-anchors Lazada income to
-- this time (vs the statement file's date-only release date, which is occasionally
-- off-by-one near the ~3am settlement and mis-groups income across deposit cycles).
PRAGMA busy_timeout=10000;

CREATE TABLE IF NOT EXISTS lazada_statement_settlement (
    statement   TEXT PRIMARY KEY,   -- รอบบิล, e.g. THJ2K7MP-2026-0531
    settled_at  TEXT NOT NULL,      -- 'YYYY-MM-DD HH:MM:SS' (precise, from wallet)
    amount      REAL NOT NULL       -- settlement amount for the รอบบิล (cross-check)
);
