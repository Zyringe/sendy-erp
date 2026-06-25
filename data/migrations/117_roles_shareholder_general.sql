-- data/migrations/117_roles_shareholder_general.sql
-- Extend users.role CHECK to add 'shareholder' + 'general'. SQLite can't ALTER a
-- CHECK in place → table rebuild. employees.user_id FK is preserved (foreign_keys
-- toggled off around the swap, per mig 069 convention). NOTE: no self-insert into
-- applied_migrations.
PRAGMA foreign_keys = OFF;
BEGIN;
CREATE TABLE users_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    UNIQUE NOT NULL,
    password_hash TEXT    NOT NULL,
    display_name  TEXT,
    role          TEXT    NOT NULL DEFAULT 'staff'
                          CHECK(role IN ('admin','manager','staff','shareholder','general')),
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
INSERT INTO users_new SELECT id,username,password_hash,display_name,role,is_active,created_at FROM users;
DROP TABLE users;
ALTER TABLE users_new RENAME TO users;
COMMIT;
PRAGMA foreign_keys = ON;
