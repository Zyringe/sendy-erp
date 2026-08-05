-- ============================================================================
-- 154 — vat-substitute: human-curated substitutability groups.
--
-- See projects/vat-substitute/plan.md (rev 12, Codex GO) §5 for the full
-- contract. Three tables:
--
--   vat_sub_groups         — one row per curated group (label only; the
--     label is set once at creation and never touched by the sheet-apply
--     re-run per §4.7's "rename-stable" property; manual rename via the
--     dedicated route is fine).
--   vat_sub_members        — xp5 codes that belong to a group. `xp5_code` is
--     NOT a foreign key: the VAT book (vat_book.db) is a SEPARATE SQLite
--     file rebuilt from Express, so there is no cross-database FK to declare
--     — membership is validated by the app at write time against the
--     CURRENTLY published vat_book (plan §4.5), not by a DB constraint.
--   vat_sub_product_links  — main-DB products linked into a group (many-to-
--     many per decision 11: a product may sit in several groups).
--
-- UNIQUE(group_id, xp5_code) / UNIQUE(group_id, product_id) are the ON
-- CONFLICT DO NOTHING targets every curation write relies on for idempotent
-- promote/add-member/link-product (plan §4.6).
--
-- All three PKs are `id INTEGER PRIMARY KEY` (rowid aliases) — none of the
-- mig 150/151 row_key concern applies (that was for TEXT-PK tables only).
-- Audit triggers follow the plain INSERT/UPDATE/DELETE style of migration
-- 070 (row_id = NEW/OLD.id).
--
-- Drop-first shape (matches migrations 151-153): every CREATE is preceded by
-- a DROP ... IF EXISTS so this file is re-runnable on a partially-recovered
-- DB. Rehearsed forward + rollback on a .backup copy before commit.
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS audit_vat_sub_groups_insert;
DROP TRIGGER IF EXISTS audit_vat_sub_groups_update;
DROP TRIGGER IF EXISTS audit_vat_sub_groups_delete;
DROP TRIGGER IF EXISTS audit_vat_sub_members_insert;
DROP TRIGGER IF EXISTS audit_vat_sub_members_delete;
DROP TRIGGER IF EXISTS audit_vat_sub_product_links_insert;
DROP TRIGGER IF EXISTS audit_vat_sub_product_links_delete;
DROP INDEX IF EXISTS idx_vat_sub_members_xp5_code;
DROP INDEX IF EXISTS idx_vat_sub_members_group;
DROP INDEX IF EXISTS idx_vat_sub_product_links_product;
DROP INDEX IF EXISTS idx_vat_sub_product_links_group;
DROP TABLE IF EXISTS vat_sub_product_links;
DROP TABLE IF EXISTS vat_sub_members;
DROP TABLE IF EXISTS vat_sub_groups;

CREATE TABLE vat_sub_groups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    label      TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE vat_sub_members (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id   INTEGER NOT NULL REFERENCES vat_sub_groups(id) ON DELETE CASCADE,
    xp5_code   TEXT    NOT NULL,
    added_from TEXT    NOT NULL CHECK (added_from IN ('sheet', 'promote', 'manual')),
    created_at TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (group_id, xp5_code)
);

CREATE TABLE vat_sub_product_links (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id   INTEGER NOT NULL REFERENCES vat_sub_groups(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    created_at TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (group_id, product_id)
);

CREATE INDEX idx_vat_sub_members_xp5_code ON vat_sub_members(xp5_code);
CREATE INDEX idx_vat_sub_members_group ON vat_sub_members(group_id);
CREATE INDEX idx_vat_sub_product_links_product ON vat_sub_product_links(product_id);
CREATE INDEX idx_vat_sub_product_links_group ON vat_sub_product_links(group_id);

-- ── audit: vat_sub_groups ────────────────────────────────────────────────
CREATE TRIGGER audit_vat_sub_groups_insert
AFTER INSERT ON vat_sub_groups
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('vat_sub_groups', NEW.id, 'INSERT',
        json_object('label', NEW.label));
END;

CREATE TRIGGER audit_vat_sub_groups_update
AFTER UPDATE ON vat_sub_groups
WHEN OLD.label IS NOT NEW.label
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('vat_sub_groups', NEW.id, 'UPDATE',
        json_object('label', json_array(OLD.label, NEW.label)));
END;

CREATE TRIGGER audit_vat_sub_groups_delete
BEFORE DELETE ON vat_sub_groups
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('vat_sub_groups', OLD.id, 'DELETE',
        json_object('label', OLD.label));
END;

-- ── audit: vat_sub_members (no UPDATE — a member row is add/remove only) ──
CREATE TRIGGER audit_vat_sub_members_insert
AFTER INSERT ON vat_sub_members
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('vat_sub_members', NEW.id, 'INSERT',
        json_object('group_id', NEW.group_id, 'xp5_code', NEW.xp5_code,
                    'added_from', NEW.added_from));
END;

CREATE TRIGGER audit_vat_sub_members_delete
BEFORE DELETE ON vat_sub_members
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('vat_sub_members', OLD.id, 'DELETE',
        json_object('group_id', OLD.group_id, 'xp5_code', OLD.xp5_code,
                    'added_from', OLD.added_from));
END;

-- ── audit: vat_sub_product_links (no UPDATE — link/unlink only) ──────────
CREATE TRIGGER audit_vat_sub_product_links_insert
AFTER INSERT ON vat_sub_product_links
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('vat_sub_product_links', NEW.id, 'INSERT',
        json_object('group_id', NEW.group_id, 'product_id', NEW.product_id));
END;

CREATE TRIGGER audit_vat_sub_product_links_delete
BEFORE DELETE ON vat_sub_product_links
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('vat_sub_product_links', OLD.id, 'DELETE',
        json_object('group_id', OLD.group_id, 'product_id', OLD.product_id));
END;

COMMIT;
