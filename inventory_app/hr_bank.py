"""Bank name normalization — maps legacy free-text values to canonical Thai names.

Kept in sync with data/migrations/115_normalize_bank_names.sql CASE expressions.
"""

_LEGACY_BANK_MAP = {
    "กสิกร":     "ธนาคารกสิกรไทย",
    "กสิกรไทย":  "ธนาคารกสิกรไทย",
    "กรุงไทย":   "ธนาคารกรุงไทย",
    "ไทยพาณิชย์": "ธนาคารไทยพาณิชย์",
}


def normalize_bank(value):
    """Return canonical bank name, or empty string if value is blank."""
    v = (value or "").strip()
    return _LEGACY_BANK_MAP.get(v, v)
