-- ============================================================================
-- Migration 149 — durable operational alerts (system_alerts) + /alerts surface
--
-- Why
--   PR2 makes a WACC cost-identity failure raise instead of silently writing a
--   wrong cost basis. The caller surfaces that as a flash message. But Put is
--   the only person in the business who acts on digital problems, and the flash
--   reaches whoever ran the import — who will not reliably relay it. A
--   flash-only failure is therefore effectively SILENT to the one person who
--   can fix it.
--
--   This gives the failure somewhere durable to live, surfaced on the existing
--   /alerts page (which today shows only live-computed negative stock and
--   persists nothing).
--
-- Design notes
--   dedupe_key is INCIDENT IDENTITY (kind + product + reference + source line +
--   operation), NOT the whole context. If it were derived from the full
--   context, a retry in a new batch would change the batch id and raise a
--   SECOND unresolved alert for the same underlying breakage. Diagnostics
--   (filename, batch id, timestamps) live in context_json and are deliberately
--   excluded from the key.
--
--   The partial UNIQUE index enforces "at most one UNRESOLVED alert per
--   incident" while still allowing the same incident to alert again AFTER it
--   has been acknowledged — which is what you want: a recurrence is news.
--
--   resolved_by is TEXT holding the username, matching the house convention
--   (audit-style `created_by TEXT -- username (NULL ถ้า system)`).
--
--   Only admin/manager may RESOLVE (enforced in the route, not here): the same
--   team members who fail to relay the message must not be able to clear the
--   alert before Put ever sees it. That would defeat the entire point.
--
--   Deliberately NOT built: delivery channels, assignment, escalation, or any
--   generic workflow engine. Create / list / count / resolve, nothing more.
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS system_alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    severity     TEXT NOT NULL DEFAULT 'error'
                 CHECK (severity IN ('error', 'warning')),
    message      TEXT NOT NULL,
    dedupe_key   TEXT NOT NULL,
    context_json TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    resolved_at  TEXT,
    resolved_by  TEXT
);

-- At most one UNRESOLVED alert per incident identity.
CREATE UNIQUE INDEX IF NOT EXISTS idx_system_alerts_open_dedupe
    ON system_alerts(kind, dedupe_key)
 WHERE resolved_at IS NULL;

-- The nav badge counts unresolved alerts on EVERY rendered request
-- (access_control injects alert_count globally), so keep that lookup cheap.
CREATE INDEX IF NOT EXISTS idx_system_alerts_open
    ON system_alerts(resolved_at)
 WHERE resolved_at IS NULL;

COMMIT;
