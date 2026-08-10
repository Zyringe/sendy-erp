UPDATE product_code_mapping
   SET is_ignored = 1
 WHERE bsn_code IN ('ZZZ', '888ค8888');

INSERT OR IGNORE INTO unit_conversions (product_id, bsn_unit, ratio) VALUES
  (1211, 'กล่อง', 1.0), (1211, 'คค', 1.0), (1211, 'ใบ', 1.0),
  (1623, 'คค', 1.0), (1623, 'ครั้ง', 1.0);
