-- No-op: the forward migration normalized ''→NULL on employees date columns.
-- Restoring empty strings has no semantic meaning and would re-break payroll
-- generation, so there is deliberately nothing to roll back.
SELECT 1;
