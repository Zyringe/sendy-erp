-- ============================================================================
-- 153 — reconcile-scan: detect + confirm-apply for Sendy sales docs that
-- disappeared from Express entirely (deleted/cancelled at source).
--
-- See projects/express-integration/reconcile-scan-plan.md (rev 8, GO) §2 for
-- the full contract. Two tables:
--
--   express_reconcile_flags  — one open row per (doc_base) currently flagged.
--     `first_payload_json` is the forensic snapshot taken the FIRST time this
--     doc_base was flagged — immutable after insert (enforced by trigger
--     below), so a later re-detection/refresh can never quietly rewrite the
--     original evidence. `latest_payload_json` is refreshed every scan and is
--     the CAS reference apply() compares against just before mutating.
--     `suppression_active` is an explicit column (not inferred from state) so
--     a dismissed doc stays dismissed across repeated scans until either a
--     human reopens it or the doc reappears in Express (both clear the flag,
--     both are audited).
--
--   express_reconcile_events — append-only transition log (INSERT-only,
--     enforced below). One row per state/class transition, written in the
--     SAME transaction as the transition itself — a refused/failed operation
--     must leave neither a flag change nor an event.
--
-- NOT ตรวจบิล's tables (txn_review_docs/txn_review_flags): that scan
-- delete/recreates its rows wholesale every run and its UI is read-only by
-- design — coexisting there is unsafe for a lifecycle this feature needs
-- (open → applied/dismissed/reappeared, forensically immutable).
--
-- Drop-first shape (matches migration 151/152): every CREATE is preceded by
-- a DROP ... IF EXISTS so this file is re-runnable on a partially-recovered
-- DB. Rehearsed forward + rollback on a .backup copy before commit.
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS express_reconcile_flags_first_payload_immutable;
DROP TRIGGER IF EXISTS express_reconcile_events_no_update;
DROP TRIGGER IF EXISTS express_reconcile_events_no_delete;
DROP INDEX IF EXISTS idx_erf_open_doc_base;
DROP INDEX IF EXISTS idx_erf_doc_base;
DROP INDEX IF EXISTS idx_erf_state;
DROP INDEX IF EXISTS idx_erf_suppression_lookup;
DROP INDEX IF EXISTS idx_ere_flag_id;
DROP TABLE IF EXISTS express_reconcile_events;
DROP TABLE IF EXISTS express_reconcile_flags;

CREATE TABLE express_reconcile_flags (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_base             TEXT    NOT NULL,
    class                TEXT    NOT NULL
                          CHECK (class IN ('deleted', 'out_of_scope', 'parse_gap',
                                            'date_moved', 'data_gap')),
    -- Forensic snapshot from the FIRST time this doc_base was flagged.
    -- Immutable after insert (trigger below).
    first_payload_json   TEXT    NOT NULL,
    -- Refreshed on every re-detection scan; apply()'s CAS reference.
    latest_payload_json  TEXT    NOT NULL,
    first_seen_at        TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    last_seen_at         TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    state                TEXT    NOT NULL DEFAULT 'open'
                          CHECK (state IN ('open', 'applied', 'dismissed', 'reappeared')),
    -- Only a 'dismissed' row may carry suppression_active=1 — a later
    -- explicit reopen or a doc reappearing in Express clears it back to 0
    -- (state stays 'dismissed'; only the flag governing re-detection moves).
    suppression_active   INTEGER NOT NULL DEFAULT 0 CHECK (suppression_active IN (0, 1)),
    resolved_by          TEXT,
    resolved_at          TEXT,
    resolution_note      TEXT,
    CHECK (suppression_active = 0 OR state = 'dismissed'),
    CHECK (
        (state = 'open'
            AND resolved_by IS NULL AND resolved_at IS NULL AND resolution_note IS NULL)
        OR (state = 'applied'
            AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)
        OR (state = 'dismissed'
            AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL
            AND resolution_note IS NOT NULL AND resolution_note <> '')
        OR (state = 'reappeared'
            AND resolved_by = 'system' AND resolved_at IS NOT NULL)
    )
);

-- DB-enforced one-open-flag-per-doc: detection upserts transactionally with
-- this as the FINAL guard, not a pre-check (plan §2 storage section).
CREATE UNIQUE INDEX idx_erf_open_doc_base
    ON express_reconcile_flags(doc_base) WHERE state = 'open';
CREATE INDEX idx_erf_doc_base ON express_reconcile_flags(doc_base);
CREATE INDEX idx_erf_state ON express_reconcile_flags(state);
-- Dismissal-suppression lookup: "skip opening a new flag for (doc_base, class)
-- when a suppressed dismissal exists for that pair" (plan §2).
CREATE INDEX idx_erf_suppression_lookup
    ON express_reconcile_flags(doc_base, class) WHERE suppression_active = 1;

-- Forensics: the first-seen payload can never be rewritten by a later scan.
CREATE TRIGGER express_reconcile_flags_first_payload_immutable
BEFORE UPDATE ON express_reconcile_flags
WHEN NEW.first_payload_json IS NOT OLD.first_payload_json
BEGIN
    SELECT RAISE(ABORT,
        'first_payload_json คือหลักฐานต้นฉบับตอน flag ครั้งแรก ห้ามแก้ไข');
END;

CREATE TABLE express_reconcile_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    flag_id    INTEGER NOT NULL REFERENCES express_reconcile_flags(id),
    at         TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    from_state TEXT,
    to_state   TEXT    NOT NULL,
    from_class TEXT,
    to_class   TEXT,
    actor      TEXT,
    note       TEXT
);
CREATE INDEX idx_ere_flag_id ON express_reconcile_events(flag_id);

-- Append-only: no UPDATE, no DELETE, ever — proven by break-it-once tests.
CREATE TRIGGER express_reconcile_events_no_update
BEFORE UPDATE ON express_reconcile_events
BEGIN
    SELECT RAISE(ABORT, 'express_reconcile_events เป็น append-only log ห้ามแก้ไข');
END;

CREATE TRIGGER express_reconcile_events_no_delete
BEFORE DELETE ON express_reconcile_events
BEGIN
    SELECT RAISE(ABORT, 'express_reconcile_events เป็น append-only log ห้ามลบ');
END;

COMMIT;
