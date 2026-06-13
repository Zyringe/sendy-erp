PRAGMA busy_timeout=10000;
BEGIN IMMEDIATE;

CREATE TABLE customer_call_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_code TEXT    NOT NULL,
    kind          TEXT    NOT NULL CHECK (kind IN ('note','call','data_flag')),
    body          TEXT,
    created_by    TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    deleted_at    TEXT,
    deleted_by    TEXT
);
CREATE INDEX idx_call_log_customer ON customer_call_log(customer_code, created_at);

CREATE TABLE customer_crm (
    customer_code    TEXT    PRIMARY KEY,
    tags             TEXT,
    next_call_date   TEXT,
    call_target_days INTEGER,
    updated_by       TEXT,
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

COMMIT;
