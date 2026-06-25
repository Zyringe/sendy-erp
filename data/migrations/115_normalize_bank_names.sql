-- One-time normalize of legacy free-text employees.bank_name to canonical
-- Thai names (matches inventory_app/hr_bank.py::_LEGACY_BANK_MAP).
BEGIN;
UPDATE employees SET bank_name = 'ธนาคารกสิกรไทย'   WHERE TRIM(bank_name) IN ('กสิกร','กสิกรไทย');
UPDATE employees SET bank_name = 'ธนาคารกรุงไทย'     WHERE TRIM(bank_name) = 'กรุงไทย';
UPDATE employees SET bank_name = 'ธนาคารไทยพาณิชย์'  WHERE TRIM(bank_name) = 'ไทยพาณิชย์';
COMMIT;
