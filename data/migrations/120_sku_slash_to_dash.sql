BEGIN;
-- '/' in sku_code is legit fraction sizing (1/2", 5/16", 3/8", box 10.7/8x...)
-- but is a path separator, so it breaks any consumer using sku_code as a
-- folder/URL component (the photo tool's <category>/<sku>/raw/ layout). Map
-- '/'→'-' so sku_code is path-safe; matches the build_sku_code change in this
-- PR (test_sku_code_slash.py). 251 rows as of 2026-06-29; collision check
-- confirmed REPLACE('/','-') maps no sku onto another's. Pure data (no DDL) so
-- schema.sql is unchanged; rollback restores each row explicitly.
UPDATE products SET sku_code = REPLACE(sku_code, '/', '-')
    WHERE sku_code LIKE '%/%';
COMMIT;
