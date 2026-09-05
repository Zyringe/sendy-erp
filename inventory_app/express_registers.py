"""Express reference-register vocabulary and guarded full replacements.

The daily DBF import isolates these registers from the money ledger: a failed
register leaves the previous export readable and is reported to the caller.
This module owns the facts every consumer must share and the replacement
transaction used by the three registers that are complete export-time mirrors.
"""

import sqlite3
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple


@dataclass(frozen=True)
class RegisterTable:
    name: str
    columns: Tuple[str, ...]


@dataclass(frozen=True)
class Replacement:
    tables: Tuple[RegisterTable, ...]
    delete_order: Tuple[str, ...]
    guard_group: int
    empty_noun: str


@dataclass(frozen=True)
class Register:
    key: str
    label: str
    stale_hint: str = ''
    snapshot_required: bool = False
    replacement: Optional[Replacement] = None


_BANK_CHEQUES = Replacement(
    # BKTRN has no unique natural key (CHQNUM repeats), so this is an export-time
    # mirror rather than an upserted document set.
    tables=(RegisterTable('express_bank_cheques', (
        'kind', 'type_code', 'cheque_no', 'trn_date_iso', 'cheque_date_iso',
        'received_date_iso', 'paid_in_date_iso', 'bank_code', 'branch',
        'bank_account', 'party_code', 'party_name', 'amount', 'charge',
        'vat_amount', 'net_amount', 'remaining_amount', 'status_code',
        'remark', 'ref_doc', 'ref_no', 'voucher',
    )),),
    delete_order=('express_bank_cheques',),
    guard_group=0,
    empty_noun='rows',
)

_SALES_ORDERS = Replacement(
    tables=(
        RegisterTable('express_sales_orders', (
            'so_no', 'so_date_iso', 'customer_code', 'customer_name',
            'salesperson_code', 'your_ref', 'pay_terms', 'delivery_date_iso',
            'completed_date_iso', 'total', 'discount_amount', 'vat_amount',
            'net_amount', 'status_code',
        )),
        RegisterTable('express_sales_order_lines', (
            'so_no', 'line_seq', 'product_code', 'product_name', 'ordered_qty',
            'cancelled_qty', 'remaining_qty', 'unit', 'unit_price', 'line_total',
        )),
    ),
    delete_order=('express_sales_order_lines', 'express_sales_orders'),
    guard_group=0,
    empty_noun='orders',
)

_GENERAL_LEDGER = Replacement(
    tables=(
        RegisterTable('express_gl_accounts', (
            'account_no', 'account_name', 'level', 'parent_no', 'account_type',
            'nature', 'status',
        )),
        RegisterTable('express_gl_vouchers', (
            'voucher', 'voucher_date_iso', 'journal_type', 'reference_no',
            'description', 'source_journal', 'status',
        )),
        RegisterTable('express_gl_lines', (
            'voucher', 'line_seq', 'voucher_date_iso', 'account_no',
            'description', 'entry_side', 'type_code', 'amount',
        )),
    ),
    delete_order=('express_gl_lines', 'express_gl_vouchers', 'express_gl_accounts'),
    # Accounts alone are not evidence that the journal parsed; vouchers are.
    guard_group=1,
    empty_noun='vouchers',
)


# This is vocabulary and storage shape, not caller policy. The daily upload
# warns and keeps yesterday's register after a failure; vat_book_builder must
# refuse to publish a from-scratch book without its two balance snapshots.
REGISTERS = (
    Register('ar_snapshot', 'ลูกหนี้คงค้าง', 'หน้าลูกหนี้ยังเป็นของรอบก่อน', True),
    Register('ap_snapshot', 'เจ้าหนี้คงค้าง', 'หน้าเจ้าหนี้ยังเป็นของรอบก่อน', True),
    Register('billing_notes', 'ใบวางบิล'),
    Register('bank_cheques', 'ทะเบียนเช็ค', replacement=_BANK_CHEQUES),
    Register('sales_orders', 'ใบสั่งขาย', replacement=_SALES_ORDERS),
    Register('general_ledger', 'บัญชีแยกประเภท', replacement=_GENERAL_LEDGER),
)

REGISTER_KEYS = frozenset(register.key for register in REGISTERS)
SNAPSHOT_KEYS = tuple(register.key for register in REGISTERS
                      if register.snapshot_required)
_BY_KEY = {register.key: register for register in REGISTERS}


def replace(key: str, record_groups: Sequence[Sequence[dict]], entity: str,
            db_path: str) -> Tuple[int, ...]:
    """Replace one complete Express register for ``entity`` atomically.

    An empty parsed guard group may be a broken export, so it cannot erase an
    already-stored register. Empty data is accepted only when there is nothing
    stored yet. Counts follow the descriptor's parent-first table order.
    """
    register = _BY_KEY[key]
    replacement = register.replacement
    if replacement is None:
        raise ValueError(f'{key} is not a replaceable register')
    if len(record_groups) != len(replacement.tables):
        raise ValueError(
            f'{key} expects {len(replacement.tables)} record groups, '
            f'got {len(record_groups)}')

    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute('PRAGMA busy_timeout=10000')
        guard_records = record_groups[replacement.guard_group]
        if not guard_records:
            guard_table = replacement.tables[replacement.guard_group].name
            existing = conn.execute(
                f'SELECT COUNT(*) FROM {guard_table} WHERE entity = ?',
                (entity,),
            ).fetchone()[0]
            if existing:
                raise ValueError(
                    f'{register.label}: parsed 0 {replacement.empty_noun} but '
                    f'{existing} already stored for {entity} — refusing to erase them')
            return tuple(0 for _group in record_groups)

        conn.execute('BEGIN IMMEDIATE')
        for table_name in replacement.delete_order:
            conn.execute(
                f'DELETE FROM {table_name} WHERE entity = ?', (entity,))
        for table, records in zip(replacement.tables, record_groups):
            columns = ('entity',) + table.columns
            placeholders = ', '.join('?' for _column in columns)
            conn.executemany(
                f"INSERT INTO {table.name} ({', '.join(columns)}) "
                f'VALUES ({placeholders})',
                [(entity,) + tuple(record[column] for column in table.columns)
                 for record in records],
            )
        conn.commit()
    finally:
        conn.close()
    return tuple(len(group) for group in record_groups)
