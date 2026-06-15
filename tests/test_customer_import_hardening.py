"""TDD tests for the hardened import_customers_from_bsn function.

Tests are written FIRST and must fail against the current implementation.
Run: cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_customer_import_hardening.py -q

Contract:
  - Protected rows (contact_normalized_at IS NOT NULL): only operational fields updated
    (salesperson, zone, customer_type, credit_days, tax_id, imported_at).
    Contact fields (name, phone, fax, contact, address, nickname, contact_orig_json,
    contact_normalized_at/by) are untouched.
  - Un-normalized rows / new rows: run through normalize_customer.
    If confidence=='auto' AND a field actually changes -> apply fax split + mark normalized.
    If confidence=='review' OR no actual change -> keep raw, leave contact_normalized_at NULL.
  - Return value is (inserted, updated, protected) — caller receives named tuple or 3-int
    tuple; route updated to unpack correctly.
  - Lossless: every >=7-digit number in the imported phone survives in the resulting row.
"""
import json
import os
import re
import sqlite3

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_customer(code, **kwargs):
    """Return a minimal customer import dict (all BSN export keys present)."""
    return {
        'code': code,
        'name': kwargs.get('name', f'ร้านทดสอบ {code}'),
        'salesperson': kwargs.get('salesperson', 'SP01'),
        'zone': kwargs.get('zone', 'ZONE1'),
        'customer_type': kwargs.get('customer_type', 'R'),
        'address': kwargs.get('address', ''),
        'phone': kwargs.get('phone', ''),
        'tax_id': kwargs.get('tax_id', ''),
        'credit_days': kwargs.get('credit_days', 0),
        'contact': kwargs.get('contact', ''),
    }


def _fetch_customer(conn, code):
    row = conn.execute(
        "SELECT * FROM customers WHERE code=?", (code,)
    ).fetchone()
    return dict(row) if row else None


def _insert_protected_row(conn, code, phone, fax, credit_days=0, zone='ZONE_OLD'):
    """Insert a customers row that is already normalized (contact_normalized_at IS NOT NULL)."""
    conn.execute("""
        INSERT INTO customers
            (code, name, salesperson, zone, customer_type, phone, fax,
             credit_days, contact_normalized_at, contact_normalized_by,
             imported_at)
        VALUES (?,?,?,?,?,?,?,?,datetime('now','localtime'),'manual_clean',datetime('now','localtime'))
    """, (code, f'ร้าน {code}', 'SP_OLD', zone, 'R', phone, fax, credit_days))
    conn.commit()


def _digit_runs_7plus(text):
    """All 7+ digit runs in text (for lossless check)."""
    if not text:
        return set()
    runs = set()
    for m in re.finditer(r"\d[\d().\-/]*\d", text or ""):
        digs = re.sub(r"\D", "", m.group(0))
        if len(digs) >= 7:
            runs.add(digs)
    return runs


def _all_digits_in_row(row):
    """Collect all 7+ digit runs from phone + fax + contact columns of a DB row."""
    combined = " ".join([
        row.get('phone') or '',
        row.get('fax') or '',
        row.get('contact') or '',
    ])
    return re.sub(r"\D", "", combined)  # return concat for substring search


# ---------------------------------------------------------------------------
# Fixture: import_customers_from_bsn via models
# ---------------------------------------------------------------------------
@pytest.fixture
def run_import(tmp_db):
    """Return a callable that invokes models.import_customers_from_bsn with a
    list of customer dicts, and returns the result + a fresh connection for
    assertions."""
    import models
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    def _run(customers):
        return models.import_customers_from_bsn(customers)

    yield _run, conn
    conn.close()


# ---------------------------------------------------------------------------
# Test 1: Protected row keeps cleaned contact, updates operational fields
# ---------------------------------------------------------------------------
def test_protected_row_keeps_contact_updates_operational(tmp_db, run_import):
    """A row with contact_normalized_at IS NOT NULL must not have its contact
    fields overwritten. Operational fields (credit_days, zone) must update."""
    run, conn = run_import

    CODE = 'TEST_PROTECTED_001'
    CLEAN_PHONE = '099-CLEAN'
    CLEAN_FAX = '02-FAXCLEAN'

    _insert_protected_row(conn, CODE, phone=CLEAN_PHONE, fax=CLEAN_FAX,
                          credit_days=0, zone='ZONE_OLD')

    # Re-import with messy phone + new operational data
    customers = [_make_customer(
        CODE,
        phone='เฮีย081-0433196 mess',
        credit_days=30,
        zone='NEWZONE',
    )]

    result = run(customers)

    # Return must carry a 'protected' count
    assert len(result) == 3, (
        f"Expected (inserted, updated, protected) 3-tuple, got {result!r}"
    )
    inserted, updated, protected = result
    assert protected == 1, f"Expected 1 protected, got {protected}"
    assert updated == 0, f"Expected 0 updated (row was protected, not a normal update)"

    row = _fetch_customer(conn, CODE)
    assert row is not None

    # Contact fields UNTOUCHED
    assert row['phone'] == CLEAN_PHONE, (
        f"phone was clobbered: expected {CLEAN_PHONE!r}, got {row['phone']!r}"
    )
    assert row['fax'] == CLEAN_FAX, (
        f"fax was clobbered: expected {CLEAN_FAX!r}, got {row['fax']!r}"
    )
    assert row['contact_normalized_at'] is not None, "contact_normalized_at was cleared"

    # Operational fields UPDATED
    assert row['credit_days'] == 30, (
        f"credit_days not updated: got {row['credit_days']}"
    )
    assert row['zone'] == 'NEWZONE', (
        f"zone not updated: got {row['zone']!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: Fresh clean-except-fax import gets fax split + marked normalized
# ---------------------------------------------------------------------------
def test_fresh_fax_split_auto_marked(tmp_db, run_import):
    """A new customer row whose phone carries 'F:' is auto-split and marked
    contact_normalized_at='bsn_import'."""
    run, conn = run_import

    CODE = 'TEST_AUTO_FAX_002'
    RAW_PHONE = '02-1234567,F:02-7654321'

    customers = [_make_customer(CODE, phone=RAW_PHONE, contact='')]
    inserted, updated, protected = run(customers)
    assert inserted == 1, f"Expected 1 inserted, got {inserted}"

    row = _fetch_customer(conn, CODE)
    assert row is not None

    # Fax split
    assert row['phone'] == '02-1234567', (
        f"phone after fax split: {row['phone']!r}"
    )
    assert row['fax'] == '02-7654321', (
        f"fax after fax split: {row['fax']!r}"
    )

    # Marked as auto-normalized
    assert row['contact_normalized_at'] is not None, (
        "contact_normalized_at should be set for auto-applied rows"
    )
    assert row['contact_normalized_by'] == 'bsn_import', (
        f"contact_normalized_by: {row['contact_normalized_by']!r}"
    )

    # contact_orig_json must carry the original phone string
    assert row['contact_orig_json'] is not None, "contact_orig_json must be set"
    orig = json.loads(row['contact_orig_json'])
    assert orig['phone'] == RAW_PHONE, (
        f"contact_orig_json.phone: {orig.get('phone')!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: Fresh messy import stays raw, not marked
# ---------------------------------------------------------------------------
def test_fresh_messy_import_stays_raw_not_marked(tmp_db, run_import):
    """A new customer whose phone has a person-name prefix (review confidence)
    must be stored raw, with fax=NULL and contact_normalized_at=NULL."""
    run, conn = run_import

    CODE = 'TEST_MESSY_003'
    RAW_PHONE = 'เฮีย081-0433196,056-352038'

    customers = [_make_customer(CODE, phone=RAW_PHONE, contact='')]
    inserted, updated, protected = run(customers)
    assert inserted == 1

    row = _fetch_customer(conn, CODE)
    assert row is not None

    # Raw phone stored unchanged
    assert row['phone'] == RAW_PHONE, (
        f"Messy phone should be stored raw, got: {row['phone']!r}"
    )
    # fax stays NULL
    assert row['fax'] is None, f"fax should be NULL for review rows, got: {row['fax']!r}"
    # NOT marked normalized
    assert row['contact_normalized_at'] is None, (
        "contact_normalized_at must stay NULL for review-confidence rows"
    )


# ---------------------------------------------------------------------------
# Test 4: Lossless — every >=7-digit number in imported phone survives in row
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code,raw_phone,contact", [
    ('TEST_LOSSLESS_AUTO_004A', '02-1234567,F:02-7654321', ''),
    ('TEST_LOSSLESS_MESSY_004B', 'เฮีย081-0433196,056-352038', ''),
])
def test_lossless_digits_survive(tmp_db, run_import, code, raw_phone, contact):
    """Every 7+ digit run in the imported phone must appear somewhere in the
    resulting row (phone + fax + contact combined)."""
    run, conn = run_import

    customers = [_make_customer(code, phone=raw_phone, contact=contact)]
    run(customers)

    row = _fetch_customer(conn, code)
    assert row is not None

    orig_runs = _digit_runs_7plus(raw_phone)
    row_digits = _all_digits_in_row(row)

    for run_digits in orig_runs:
        assert run_digits in row_digits, (
            f"Lost digit run {run_digits!r} from phone {raw_phone!r}. "
            f"Row phone={row.get('phone')!r} fax={row.get('fax')!r} "
            f"contact={row.get('contact')!r}"
        )


# ---------------------------------------------------------------------------
# Test 5: Existing un-normalized row re-imports and updates normally
# ---------------------------------------------------------------------------
def test_existing_unnormalized_row_updates_normally(tmp_db, run_import):
    """An existing row with contact_normalized_at IS NULL is NOT in the
    protected branch — name/phone/zone refresh normally."""
    run, conn = run_import

    CODE = 'TEST_UNNORM_005'

    # Insert a row WITHOUT contact_normalized_at (un-normalized)
    conn.execute("""
        INSERT INTO customers
            (code, name, salesperson, zone, customer_type, phone,
             credit_days, imported_at)
        VALUES (?,?,?,?,?,?,?,datetime('now','localtime'))
    """, (CODE, 'ชื่อเก่า', 'SP_OLD', 'OLD_ZONE', 'R', '02-0000000', 0))
    conn.commit()

    # Re-import with fresh data (clean phone so auto path applies or raw stored)
    NEW_PHONE = '02-9999999'
    customers = [_make_customer(
        CODE,
        name='ชื่อใหม่',
        phone=NEW_PHONE,
        zone='NEW_ZONE',
        credit_days=15,
    )]
    inserted, updated, protected = run(customers)

    # Must NOT count as protected
    assert protected == 0, f"Un-normalized row should not be protected, got protected={protected}"
    assert updated == 1, f"Expected 1 updated, got {updated}"

    row = _fetch_customer(conn, CODE)
    # Name refreshed from import (import is source of truth for name when un-normalized)
    assert row['name'] == 'ชื่อใหม่', f"name should update for un-normalized row: {row['name']!r}"
    # Zone and credit_days updated
    assert row['zone'] == 'NEW_ZONE'
    assert row['credit_days'] == 15
