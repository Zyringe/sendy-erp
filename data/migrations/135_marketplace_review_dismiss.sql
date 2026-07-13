-- 135_marketplace_review_dismiss.sql
-- /marketplace/review: acknowledge a bucket-D order that has NO Express IV
-- (sale was never keyed — verified against the whole IV universe, see
-- Operations/05_analysis-reports/data-quality/bucket_d_iv_candidates_2026-07-13.md).
-- A dismissed order is excluded from the worklist buckets but listed in its own
-- "รับทราบแล้ว" section with an undo, so the acknowledgement is auditable and
-- reversible. Mirrors marketplace_amount_review (mig 102), keyed the same way.
CREATE TABLE IF NOT EXISTS marketplace_review_dismissals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    platform     TEXT NOT NULL,
    order_sn     TEXT NOT NULL,
    reason       TEXT,
    dismissed_by TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (platform, order_sn)
);
