"""The one writer of `cashbook_transactions` (card F).

Every kind of cashbook row has its own operation here: manual, advance,
salary, commission, payout. A linked kind derives its immutable fields (amount,
date, category, description) from its source row, re-read under the caller's
lock, so a caller cannot hand in a mismatched copy.

Transaction contract. Every writer:
  1. raises NotInTransaction unless `conn.in_transaction`. This is a WEAK guard:
     it proves some transaction is open, not that it is IMMEDIATE nor that it
     began before the caller's reads. Callers open it with
     `database.immediate(conn)` BEFORE their first decision read.
  2. re-reads every source row it derives from, under that lock.
  3. never commits or rolls back: the caller's `database.immediate` does.
  4. writes exactly the explicit audit_log row its kind writes today, or none.

Validation reproduces TODAY's rules exactly (plan D-1 is parked): an amount
passes when `not float(x) <= 0` (inf passes; nan passes, binds NULL and the
NOT NULL constraint raises), dates are checked with `date.fromisoformat` only
where today's route checks them (manual, edit), and a `belongs_to_period` CHECK
failure raises sqlite3.IntegrityError as today.

PR-1 (card F): nothing calls this module yet. The routes move in PR-2, hr /
commission / the payout mirror in PR-3.

No Flask here, and never an import of hr, commission, a blueprint or the
mirror: `app.py` loads `blueprints.hr` before `blueprints.cashbook`, so any
ledger → hr edge is an import cycle.

Python 3.9 — Optional[...] not `X | None`.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from typing import Dict, Optional, Tuple

ADVANCE_CATEGORY = "เงินเดือน (เบิกล่วงหน้า)"
SALARY_CATEGORY = "เงินเดือน"
COMMISSION_CATEGORY = "จ่ายค่าคอมมิชชั่น"
PAYOUT_CATEGORY = "ยอดขายของ"
PAYOUT_CREATED_BY = "ระบบ"
MIN_DEPOSIT_DATE = "2026-01-01"
PLATFORM_ACCOUNT_CODE = {"lazada": "LEX", "shopee": "SPX"}
PLATFORM_LABEL_TH = {"lazada": "Lazada", "shopee": "Shopee"}
KINDS = ("manual", "salary", "advance", "commission", "payout")

# The shape of a stored `txn_date`. scripts/audit_cashbook_invariants.py keeps
# its own copy of this exact string (it is stdlib-only and cannot import this
# module); tests/test_audit_cashbook_invariants.py pins the two equal. Not
# enforced on any writer until D-1 (PR-4, parked).
ISO_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
ISO_DATE_RE = re.compile(ISO_DATE_PATTERN)


class CashbookError(ValueError):
    """Thai message; a route flashes str(e) as 'danger'."""


class LockedRow(CashbookError):
    """The row belongs to another module ('salary' | 'advance' | 'commission' |
    'payout') or is an advance already deducted ('advance_deducted')."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


class PolicyBlocked(CashbookError):
    """A category manual entry may not use: 'salary' | 'commission_in_engine' |
    'advance_on_edit' | 'advance_on_manual'. `rep` is the in-engine
    salesperson row for 'commission_in_engine' (the blueprint builds the link
    to their commission page from it), else None."""

    def __init__(self, kind, message, rep=None):
        super().__init__(message)
        self.kind = kind
        self.rep = rep


class CashbookPayoutMirrorError(Exception):
    """The platform has no active destination cashbook account (unknown
    platform, or the account is missing/inactive), or a payout key is not in
    marketplace_payouts. Raised instead of silently skipping — issue #533:
    "show a visible error and never skip silently"."""


class NotInTransaction(RuntimeError):
    """A ledger writer was called on a connection with no open transaction."""


def _require_txn(conn):
    if not conn.in_transaction:
        raise NotInTransaction(
            "cashbook_ledger เขียนได้เฉพาะภายใน transaction ที่ผู้เรียกเปิดไว้ "
            "(ใช้ database.immediate(conn) ก่อนอ่านข้อมูลที่ใช้ตัดสินใจ)"
        )


# ── kinds and locks ──────────────────────────────────────────────────────────

def row_kind(row) -> str:
    """Which module owns a cashbook row, by its link columns. Every DB the
    ledger sees is migrated, so a missing column is an error, not 'manual'."""
    if row["payroll_item_id"] is not None:
        return "salary"
    if row["salary_advance_id"] is not None:
        return "advance"
    if row["commission_payout_id"] is not None:
        return "commission"
    if row["payout_platform"] is not None:
        return "payout"
    return "manual"


_SALARY_LOCKED = "รายการนี้เป็นรายการเงินเดือนที่ผูกกับ Payroll — แก้ไข/ลบที่นี่ไม่ได้"
_ADVANCE_EDIT_LOCKED = ("รายการเบิกล่วงหน้าแก้ไขที่นี่ไม่ได้ — ให้ลบแล้วเพิ่มใหม่ "
                        "(ทำได้ก่อนถูกหักในรอบเงินเดือน)")
_PAYOUT_LOCKED = ("รายการนี้เป็นยอดโอนจากมาร์เก็ตเพลสที่ระบบลงให้อัตโนมัติ — "
                  "แก้ไข/ลบที่นี่ไม่ได้ (แก้ที่หน้ามาร์เก็ตเพลสแทน)")
_ADVANCE_DEDUCTED = "รายการเบิกล่วงหน้านี้ถูกหักในรอบเงินเดือนแล้ว — ลบไม่ได้"


def _commission_locked(can_cancel_commission):
    cancel_clause = ('ยกเลิกได้ที่หน้าคอมมิชชั่นเท่านั้น' if can_cancel_commission
                     else 'ให้แอดมินยกเลิกที่หน้าคอมมิชชั่น')
    return ("รายการนี้เป็นรายการคอมมิชชั่นที่ผูกกับหน้าคอมมิชชั่น — "
            f"แก้ไข/ลบที่นี่ไม่ได้ ({cancel_clause})")


def _lock(row, action, can_cancel_commission):
    """(kind, text) of the first lock `row` hits for `action`, in the
    blueprint's order (edit: salary, advance, commission, payout; delete:
    salary, commission, payout), else None. An advance row on delete is not
    locked here: whether it was deducted is `cancel_manual`'s check."""
    if action not in ("edit", "delete"):
        raise ValueError(f"unknown action {action!r}")
    if row["payroll_item_id"] is not None:
        return "salary", _SALARY_LOCKED
    if action == "edit" and row["salary_advance_id"] is not None:
        return "advance", _ADVANCE_EDIT_LOCKED
    if row["commission_payout_id"] is not None:
        return "commission", _commission_locked(can_cancel_commission)
    if row["payout_platform"] is not None:
        return "payout", _PAYOUT_LOCKED
    return None


def lock_reason(row, action, *, can_cancel_commission=True) -> Optional[str]:
    """The text today's `_reject_if_*` flashes for `row` on `action`
    ('edit' | 'delete'), or None. `can_cancel_commission` is whether the
    current role may cancel at /commission (it picks the commission wording)."""
    hit = _lock(row, action, can_cancel_commission)
    return hit[1] if hit else None


# ── policy blocks (manual entry) ─────────────────────────────────────────────

def _salespersons_with_real_name(conn, where_sql):
    """SELECT code, name, real_name FROM salespersons {where_sql}, tolerating
    a DB that predates mig 129 (no real_name column yet): absent column ==
    every real_name is NULL, the correct "no aliases known yet" default."""
    try:
        return conn.execute(
            f"SELECT code, name, real_name FROM salespersons {where_sql}"
        ).fetchall()
    except sqlite3.OperationalError:
        return conn.execute(
            f"SELECT code, name, NULL AS real_name FROM salespersons {where_sql}"
        ).fetchall()


def in_engine_commission_rep(conn, recipient):
    """The in-engine salesperson row (code, name, real_name) that `recipient`
    (a ผู้ใช้ tag, trimmed) matches by code, name, or real_name alias, else
    None (the D3 gate: เจียรนัย=ต๋อ/06(-L), ทวีเกียรติ=ท/03). Active
    salespersons are the in-engine set; off-system reps match nothing and stay
    manual (hybrid). An alias shared by two codes resolves to the lower code."""
    recipient = (recipient or "").strip()
    if not recipient:
        return None
    rows = _salespersons_with_real_name(conn, "WHERE is_active = 1 ORDER BY code")
    for r in rows:
        idents = {r["code"], r["name"]}
        if r["real_name"]:
            idents.add(r["real_name"])
        if recipient in idents:
            return r
    return None


def policy_block(conn, category, raw_user_category) -> Optional[PolicyBlocked]:
    """Why manual entry may not use `category`, or None. Salary is always
    blocked; commission only for an in-engine recipient, matched on the RAW
    typed tag (before any ผู้ใช้ tag resolution). Plain text: the blueprint
    appends the link to the rep's commission page."""
    if category == SALARY_CATEGORY:
        return PolicyBlocked("salary", "เงินเดือนบันทึกที่หน้าเงินเดือน (HR) เท่านั้น")
    if category == COMMISSION_CATEGORY:
        rep = in_engine_commission_rep(conn, raw_user_category or "")
        if rep is not None:
            return PolicyBlocked("commission_in_engine",
                                 "คอมมิชชั่นของเซลส์ในระบบบันทึกที่หน้าคอมมิชชั่นเท่านั้น", rep)
    return None


def edit_policy_block(conn, category, raw_user_category) -> Optional[PolicyBlocked]:
    """Why an edit may not set `category`, or None. Editing an unlinked row
    INTO the advance category would leave it invisible to payroll (how a
    ฿1,000 advance escaped run 6), so it is refused; correction is delete +
    re-add. Salary / commission defer to the create path's rule."""
    if category == ADVANCE_CATEGORY:
        return PolicyBlocked(
            "advance_on_edit",
            "เปลี่ยนเป็นหมวดเบิกล่วงหน้าที่นี่ไม่ได้ — ให้ลบรายการนี้แล้ว"
            "เพิ่มใหม่ที่หน้าบันทึกรายการ เพื่อให้ระบบผูกกับรายการเบิกของพนักงานให้")
    return policy_block(conn, category, raw_user_category)


# ── today's field rules ──────────────────────────────────────────────────────

def _amount(value):
    """Today's rule: refused when unparseable or `<= 0`. nan passes (as today)."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        amount = None
    if amount is None or amount <= 0:
        raise CashbookError("จำนวนเงินต้องมากกว่า 0")
    return amount


def _txn_date(value):
    if not value:
        raise CashbookError("กรุณาระบุวันที่")
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        raise CashbookError("รูปแบบวันที่ไม่ถูกต้อง")
    return value


def _active_account(conn, account_id):
    row = conn.execute(
        "SELECT * FROM cashbook_accounts WHERE id = ? AND is_active = 1", (account_id,)
    ).fetchone()
    if row is None:
        raise CashbookError("กรุณาเลือกบัญชีที่ถูกต้องและยังใช้งานอยู่")
    return row


def _manual_fields(conn, account_id, txn_date, direction, category, amount):
    """The route's per-row rules (cashbook.py `_validate_batch` / txn_edit), one
    refusal at a time. Returns the parsed amount."""
    _active_account(conn, account_id)
    _txn_date(txn_date)
    amount = _amount(amount)
    if direction not in ("income", "expense"):
        raise CashbookError("ประเภทไม่ถูกต้อง")
    if not category:
        raise CashbookError("กรุณาระบุหมวดหมู่")
    return amount


def _require_active_category(conn, category, direction):
    row = conn.execute(
        "SELECT 1 FROM cashbook_categories WHERE name=? AND direction=? AND is_active=1",
        (category, direction),
    ).fetchone()
    if row is None:
        raise CashbookError("หมวดหมู่นี้ไม่มีอยู่หรือถูกปิดใช้งานแล้ว กรุณาเลือกหมวดหมู่ที่ใช้งานอยู่")


def _insert_mapping_unique(sql, params, conn, link_column, message):
    """INSERT; only 'UNIQUE constraint failed: cashbook_transactions.<link>'
    becomes a CashbookError. Anything else (a CHECK, a NOT NULL) re-raises."""
    try:
        return conn.execute(sql, params).lastrowid
    except sqlite3.IntegrityError as e:
        if f"UNIQUE constraint failed: cashbook_transactions.{link_column}" in str(e):
            raise CashbookError(message)
        raise


def _audit(conn, txn_id, action, fields, user):
    conn.execute(
        """INSERT INTO audit_log (table_name, row_id, action, changed_fields, user)
             VALUES ('cashbook_transactions', ?, ?, ?, ?)""",
        (txn_id, action, json.dumps(fields, ensure_ascii=False), user),
    )


# ── manual ───────────────────────────────────────────────────────────────────

def post_manual(conn, *, account_id, txn_date, direction, category, user_category,
                raw_user_category, amount, description, note, actor) -> int:
    """One manual row (cashbook.py new_transaction, non-advance branch).
    `user_category` is the tag after ผู้ใช้ resolution; `raw_user_category` the
    typed one the commission backstop reads. The (category, direction) row must
    exist and be active: the route confirm-creates it first. No explicit audit
    row (today)."""
    _require_txn(conn)
    amount = _manual_fields(conn, account_id, txn_date, direction, category, amount)
    if category == ADVANCE_CATEGORY:
        raise PolicyBlocked("advance_on_manual",
                            "รายการเบิกล่วงหน้าต้องบันทึกพร้อมพนักงาน (ผ่าน post_advance) เท่านั้น")
    blocked = policy_block(conn, category, raw_user_category)
    if blocked is not None:
        raise blocked
    _require_active_category(conn, category, direction)
    return conn.execute(
        """INSERT INTO cashbook_transactions
           (account_id, txn_date, direction, category, user_category,
            amount, description, note, created_by)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (account_id, txn_date, direction, category, user_category or None, amount,
         description or None, note or None, actor),
    ).lastrowid


def post_advance(conn, *, account_id, txn_date, employee_id, amount, description, note,
                 actor) -> Tuple[int, int]:
    """A cashbook-sourced salary advance: the HR `salary_advances` row, then the
    linked expense row, from ONE date and ONE amount. The tag is the employee's
    `COALESCE(nickname, full_name)` (so nickname '' stays '' and is stored NULL,
    as today). Returns (salary_advance_id, cashbook id)."""
    _require_txn(conn)
    amount = _manual_fields(conn, account_id, txn_date, "expense", ADVANCE_CATEGORY, amount)
    emp = conn.execute(
        "SELECT id, COALESCE(nickname, full_name) AS display"
        "  FROM employees WHERE id=? AND is_active=1",
        (employee_id,),
    ).fetchone()
    if emp is None:
        raise CashbookError("กรุณาเลือกพนักงานสำหรับรายการเบิกล่วงหน้า")
    _require_active_category(conn, ADVANCE_CATEGORY, "expense")
    adv_id = conn.execute(
        """INSERT INTO salary_advances
           (employee_id, advance_date, amount, from_account_id, note)
           VALUES (?,?,?,?,?)""",
        (emp["id"], txn_date, amount, account_id, note or None),
    ).lastrowid
    txn_id = conn.execute(
        """INSERT INTO cashbook_transactions
           (account_id, txn_date, direction, category, user_category,
            amount, description, note, created_by, salary_advance_id)
           VALUES (?,?,'expense',?,?,?,?,?,?,?)""",
        (account_id, txn_date, ADVANCE_CATEGORY, emp["display"] or None, amount,
         description or None, note or None, actor, adv_id),
    ).lastrowid
    return adv_id, txn_id


def _get_row(conn, txn_id):
    row = conn.execute(
        "SELECT * FROM cashbook_transactions WHERE id=?", (txn_id,)
    ).fetchone()
    if row is None:
        raise CashbookError(f"ไม่พบรายการ cashbook id {txn_id}")
    return row


def amend_manual(conn, txn_id, *, account_id, txn_date, direction, category, user_category,
                 raw_user_category, amount, description, note, actor,
                 can_cancel_commission=True) -> dict:
    """Edit a manual row's 8 user fields (cashbook.py txn_edit). Locked kinds
    are refused; the category rule applies only when (category, direction)
    changed, so rows in retired categories stay editable. Writes the explicit
    field-diff audit row only when something changed. Returns that diff."""
    _require_txn(conn)
    row = _get_row(conn, txn_id)
    hit = _lock(row, "edit", can_cancel_commission)
    if hit:
        raise LockedRow(*hit)
    amount = _manual_fields(conn, account_id, txn_date, direction, category, amount)
    blocked = edit_policy_block(conn, category, raw_user_category)
    if blocked is not None:
        raise blocked
    if category != row["category"] or direction != row["direction"]:
        _require_active_category(conn, category, direction)
    new_vals = {
        "account_id": int(account_id), "txn_date": txn_date, "direction": direction,
        "category": category, "user_category": user_category or None,
        "amount": amount, "description": description or None, "note": note or None,
    }
    changed = {
        field: [row[field], new_v]
        for field, new_v in new_vals.items() if row[field] != new_v
    }
    conn.execute(
        """UPDATE cashbook_transactions
           SET account_id=?, txn_date=?, direction=?, category=?, user_category=?,
               amount=?, description=?, note=?
           WHERE id=?""",
        (*new_vals.values(), txn_id),
    )
    if changed:
        _audit(conn, txn_id, "UPDATE", changed, actor)
    return changed


def cancel_manual(conn, txn_id, *, actor, can_cancel_commission=True) -> sqlite3.Row:
    """Delete a manual row, or an advance row together with its un-deducted
    `salary_advances` row (cashbook.py txn_delete). A deducted advance →
    LockedRow('advance_deducted'), checked before the delete and again
    atomically on the cascade. Returns the deleted row."""
    _require_txn(conn)
    row = _get_row(conn, txn_id)
    hit = _lock(row, "delete", can_cancel_commission)
    if hit:
        raise LockedRow(*hit)
    adv_id = row["salary_advance_id"]
    if adv_id is not None:
        adv = conn.execute(
            "SELECT deducted_in_run_id FROM salary_advances WHERE id=?", (adv_id,)
        ).fetchone()
        if adv is not None and adv["deducted_in_run_id"] is not None:
            raise LockedRow("advance_deducted", _ADVANCE_DEDUCTED)
    # The child first: the salary_advances FK forbids dropping the parent while
    # this row still references it.
    conn.execute("DELETE FROM cashbook_transactions WHERE id=?", (txn_id,))
    if adv_id is not None:
        cur = conn.execute(
            "DELETE FROM salary_advances WHERE id=? AND deducted_in_run_id IS NULL",
            (adv_id,),
        )
        if cur.rowcount == 0:
            # the caller's database.immediate rolls the row delete back
            raise LockedRow("advance_deducted", _ADVANCE_DEDUCTED)
    _audit(conn, txn_id, "DELETE", {
        "account_id": row["account_id"], "txn_date": row["txn_date"],
        "direction": row["direction"], "category": row["category"],
        "amount": row["amount"],
    }, actor)
    return row


# ── salary (ADR 0006) ────────────────────────────────────────────────────────

def post_salary(conn, *, item_id, account_id, pay_date, actor) -> int:
    """The per-employee salary pay-event row for `payroll_items.id = item_id`
    (hr.post_salary_payment). Refuses in hr's order with hr's texts: item
    exists → net_pay > 0 → not already linked → account active → account
    non-transfer → run exists → run finalized. Amount = net_pay; no date check
    (as today)."""
    _require_txn(conn)
    item = conn.execute("SELECT * FROM payroll_items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        raise CashbookError(f"ไม่พบรายการ payroll_items id {item_id}")
    if item["net_pay"] <= 0:
        raise CashbookError("เงินสุทธิ <= 0 — ไม่มียอดโอน ไม่สามารถบันทึกการจ่ายได้")
    existing = conn.execute(
        "SELECT id FROM cashbook_transactions WHERE payroll_item_id = ?", (item_id,)
    ).fetchone()
    if existing is not None:
        raise CashbookError(
            f"รายการนี้ถูกบันทึกจ่ายไปแล้ว (cashbook id {existing['id']}) — "
            f"ต้องยกเลิกการจ่ายก่อนจึงจะบันทึกใหม่ได้"
        )
    account = conn.execute(
        "SELECT * FROM cashbook_accounts WHERE id = ?", (account_id,)
    ).fetchone()
    if account is None or account["is_active"] != 1:
        raise CashbookError("บัญชีที่เลือกไม่ถูกต้องหรือถูกปิดใช้งานแล้ว")
    if account["is_transfer"] == 1:
        raise CashbookError("ไม่สามารถจ่ายเงินเดือนเข้าบัญชีประเภทเงินโอนได้")
    run = conn.execute(
        "SELECT year_month, status FROM payroll_runs WHERE id = ?", (item["run_id"],)
    ).fetchone()
    if run is None:
        raise CashbookError("ไม่พบรอบเงินเดือนของรายการนี้")
    if run["status"] != "finalized":
        raise CashbookError("บันทึกการจ่ายได้เฉพาะรอบเงินเดือนที่ finalized แล้วเท่านั้น")
    emp = conn.execute(
        "SELECT nickname, full_name FROM employees WHERE id = ?", (item["employee_id"],)
    ).fetchone()
    display_name = (emp["nickname"] or emp["full_name"]) if emp else ""
    txn_date = pay_date or date.today().isoformat()
    description = f"เงินเดือน {run['year_month']} — {display_name}"
    txn_id = _insert_mapping_unique(
        """INSERT INTO cashbook_transactions
             (account_id, txn_date, direction, category, amount,
              user_category, description, created_by,
              payroll_run_id, payroll_item_id)
           VALUES (?, ?, 'expense', ?, ?, ?, ?, ?, ?, ?)""",
        (account_id, txn_date, SALARY_CATEGORY, item["net_pay"], display_name, description,
         actor, item["run_id"], item_id),
        conn, "payroll_item_id",
        "รายการนี้ถูกบันทึกจ่ายไปแล้ว — ต้องยกเลิกการจ่ายก่อนจึงจะบันทึกใหม่ได้",
    )
    _audit(conn, txn_id, "INSERT", {"payroll_item_id": item_id, "amount": item["net_pay"],
                                    "account_id": account_id}, actor)
    return txn_id


def cancel_salary(conn, *, item_id, actor) -> Optional[int]:
    """Void a salary pay-event (hr.void_salary_payment). No-op (None) when no
    row is linked. Returns the deleted cashbook id."""
    _require_txn(conn)
    row = conn.execute(
        "SELECT id FROM cashbook_transactions WHERE payroll_item_id = ?", (item_id,)
    ).fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM cashbook_transactions WHERE id = ?", (row["id"],))
    _audit(conn, row["id"], "DELETE", {"payroll_item_id": item_id}, actor)
    return row["id"]


# ── commission (ADR 0008) ────────────────────────────────────────────────────

def post_commission(conn, *, payout_id, account_id, actor) -> int:
    """The locked expense row for a `commission_payouts` row that the caller
    (commission.record_payout) inserted in this same transaction. Amount, date
    and year-month come from the payout row. The account check is a BACKSTOP:
    record_payout checks it before the payout INSERT. `actor` '' → created_by
    NULL and audit user '' (as today)."""
    _require_txn(conn)
    payout = conn.execute(
        "SELECT * FROM commission_payouts WHERE id = ?", (payout_id,)
    ).fetchone()
    if payout is None:
        raise CashbookError(f"ไม่พบรายการจ่ายค่าคอม id {payout_id}")
    account = conn.execute(
        "SELECT * FROM cashbook_accounts WHERE id = ?", (account_id,)
    ).fetchone()
    if account is None or account["is_active"] != 1:
        raise CashbookError("บัญชีที่เลือกไม่ถูกต้องหรือถูกปิดใช้งานแล้ว")
    if account["is_transfer"] == 1:
        raise CashbookError("ไม่สามารถจ่ายค่าคอมมิชชั่นเข้าบัญชีประเภทเงินโอนได้")
    code = payout["salesperson_code"]
    sp = conn.execute("SELECT name FROM salespersons WHERE code = ?", (code,)).fetchone()
    sp_name = sp["name"] if sp else code
    description = f"ค่าคอมมิชชั่น {payout['year_month']} — {sp_name}"
    txn_id = _insert_mapping_unique(
        """INSERT INTO cashbook_transactions
               (account_id, txn_date, direction, category, amount,
                user_category, description, created_by, commission_payout_id)
           VALUES (?, ?, 'expense', ?, ?, ?, ?, ?, ?)""",
        (account_id, payout["paid_date"], COMMISSION_CATEGORY, payout["amount_paid"], sp_name,
         description, actor or None, payout_id),
        conn, "commission_payout_id",
        "รายการจ่าย commission นี้ถูกบันทึกลงบัญชีรับ-จ่ายไปแล้ว",
    )
    _audit(conn, txn_id, "INSERT", {"commission_payout_id": payout_id,
                                    "amount": payout["amount_paid"],
                                    "account_id": account_id}, actor or '')
    return txn_id


def cancel_commission(conn, *, payout_id, actor) -> Optional[int]:
    """Delete the cashbook row linked to a commission payout (the cashbook half
    of commission.delete_payout; the payout row itself stays the caller's).
    None when nothing is linked."""
    _require_txn(conn)
    cb = conn.execute(
        "SELECT id FROM cashbook_transactions WHERE commission_payout_id = ?", (payout_id,)
    ).fetchone()
    if cb is None:
        return None
    conn.execute("DELETE FROM cashbook_transactions WHERE id = ?", (cb["id"],))
    _audit(conn, cb["id"], "DELETE", {"commission_payout_id": payout_id}, actor or '')
    return cb["id"]


# ── marketplace payouts (ADR 0013, issue #533) ───────────────────────────────

def payout_target(conn, platform) -> Dict[Tuple[str, float, int], int]:
    """{(deposit_date, amount, occurrence): n_orders} for every
    marketplace_payouts row of `platform` on/after MIN_DEPOSIT_DATE.

    occurrence numbers duplicates sharing (deposit_date, amount) in the
    payouts table's own row order (deposit_date, id ASC) — stable across a
    rebuild that reproduces the same rows in the same order.
    """
    seen = {}
    target = {}
    for r in conn.execute(
        """SELECT deposit_date, amount, n_orders FROM marketplace_payouts
            WHERE platform = ? AND deposit_date >= ?
            ORDER BY deposit_date, id""",
        (platform, MIN_DEPOSIT_DATE),
    ):
        amt = round(r['amount'], 2)
        key2 = (r['deposit_date'], amt)
        occurrence = seen.get(key2, 0) + 1
        seen[key2] = occurrence
        target[(r['deposit_date'], amt, occurrence)] = r['n_orders']
    return target


def _platform_account_id(conn, platform):
    account_code = PLATFORM_ACCOUNT_CODE.get(platform)
    if account_code is None:
        raise CashbookPayoutMirrorError(
            f"ไม่รู้จักแพลตฟอร์ม '{platform}' — ไม่มีบัญชีปลายทางที่กำหนดไว้สำหรับยอดโอน"
        )
    account = conn.execute(
        "SELECT id FROM cashbook_accounts WHERE code = ? AND is_active = 1", (account_code,),
    ).fetchone()
    if account is None:
        label = PLATFORM_LABEL_TH.get(platform, platform)
        raise CashbookPayoutMirrorError(
            f"ไม่พบบัญชี {account_code} (หรือถูกปิดใช้งาน) — ยอดโอนของ {label} "
            "ยังไม่ถูกบันทึกลงบัญชีรับ-จ่าย"
        )
    return account['id']


def _payout_description(platform, n_orders):
    return f"{PLATFORM_LABEL_TH.get(platform, platform)} โอนเงิน ({n_orders} ออเดอร์)"


def post_payout(conn, *, platform, deposit_date, amount, occurrence) -> int:
    """One locked income row for a payout key. The key must be in the
    platform's target set, recomputed here from marketplace_payouts; n_orders
    and the account come from there, never from the caller. No explicit audit
    row (today)."""
    _require_txn(conn)
    account_id = _platform_account_id(conn, platform)
    key = (deposit_date, round(amount, 2), occurrence)
    target = payout_target(conn, platform)
    if key not in target:
        raise CashbookPayoutMirrorError(
            f"ไม่พบยอดโอน {PLATFORM_LABEL_TH.get(platform, platform)} วันที่ {deposit_date} "
            f"ยอด {amount} (ลำดับ {occurrence}) ใน marketplace_payouts"
        )
    return _insert_mapping_unique(
        """INSERT INTO cashbook_transactions
             (account_id, txn_date, direction, category, amount,
              description, created_by,
              payout_platform, payout_deposit_date, payout_amount, payout_occurrence)
           VALUES (?, ?, 'income', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (account_id, key[0], PAYOUT_CATEGORY, key[1], _payout_description(platform, target[key]),
         PAYOUT_CREATED_BY, platform, key[0], key[1], occurrence),
        conn, "payout_platform", "ยอดโอนนี้ถูกบันทึกลงบัญชีรับ-จ่ายไปแล้ว",
    )


def _payout_row(conn, txn_id):
    row = _get_row(conn, txn_id)
    if row_kind(row) != "payout":
        raise CashbookError(f"รายการ cashbook id {txn_id} ไม่ใช่ยอดโอนจากมาร์เก็ตเพลส")
    return row


def cancel_payout(conn, *, txn_id) -> None:
    """Delete a payout-sourced row (its key left marketplace_payouts)."""
    _require_txn(conn)
    _payout_row(conn, txn_id)
    conn.execute("DELETE FROM cashbook_transactions WHERE id = ?", (txn_id,))


def set_payout_description(conn, *, txn_id) -> None:
    """Re-derive a payout row's description from the n_orders of its own key."""
    _require_txn(conn)
    row = _payout_row(conn, txn_id)
    platform = row["payout_platform"]
    key = (row["payout_deposit_date"], round(row["payout_amount"], 2), row["payout_occurrence"])
    target = payout_target(conn, platform)
    if key not in target:
        raise CashbookPayoutMirrorError(
            f"ยอดโอนของรายการ cashbook id {txn_id} ไม่อยู่ใน marketplace_payouts แล้ว"
        )
    conn.execute(
        "UPDATE cashbook_transactions SET description = ? WHERE id = ?",
        (_payout_description(platform, target[key]), txn_id),
    )
