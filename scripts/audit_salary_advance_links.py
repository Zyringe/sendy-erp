#!/usr/bin/env python
"""ตรวจความสอดคล้องระหว่าง cashbook_transactions กับ salary_advances (read-only).

เงินเบิกล่วงหน้า 1 ก้อน ต้องมี 2 แถวผูกกัน: แถวเงินสดใน `cashbook_transactions`
(หมวด "เงินเดือน (เบิกล่วงหน้า)") กับแถวสิทธิ์ใน `salary_advances` โดย cashbook
ถือ FK `salary_advance_id`. mig 128 (prod 2026-07-04) เพิ่งทำให้ /cashbook/new
เขียนคู่นี้ให้อัตโนมัติ — ทุกแถวก่อนหน้านั้นคีย์แยกกันสองที่ ไม่มีลิงก์

สคริปต์นี้หาแถวที่ยังไม่เข้าคู่ แล้ว *เสนอ* คู่ที่น่าจะใช่ ไม่แก้อะไรทั้งสิ้น

    python scripts/audit_salary_advance_links.py [--db PATH] [--json]

stdlib ล้วน (prod ไม่มี sqlite3 CLI และ pandas พังบน railway ssh) รันบน prod ได้:
    base64 < scripts/audit_salary_advance_links.py > /tmp/a.b64
    railway ssh "..." # decode → /opt/venv/bin/python /tmp/a.py --db /data/inventory.db
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict

ADVANCE_CATEGORY = "เงินเดือน (เบิกล่วงหน้า)"

# ⚠ กับดัก substring ภาษาไทย: "เงินเดือนX 4/26 (หักเบิกล่วงหน้า)" คือแถว *จ่าย
# เงินเดือนสุทธิ* ที่หักยอดเบิกออกแล้ว ไม่ใช่การเบิก — แต่มันมีคำว่า "เบิกล่วงหน้า"
# อยู่ข้างใน การเช็ค `"เบิกล่วงหน้า" in desc` เฉยๆ จึงกวาดมันติดมาด้วย (ของจริงบน
# prod: ct 323 / 324) ต้องตัด "หักเบิกล่วงหน้า" ออกก่อนเสมอ
ADVANCE_MARK = "เบิกล่วงหน้า"
DEDUCTION_MARK = "หักเบิกล่วงหน้า"


def looks_like_advance(description):
    d = description or ""
    return ADVANCE_MARK in d.replace(DEDUCTION_MARK, "")


def emp_label(emp):
    return "%s (%s/emp %d)" % (emp["nickname"] or emp["full_name"],
                               emp["emp_code"], emp["id"])


def matches_employee(tag, emp):
    """แท็ก ผู้ใช้ ในสมุดเงินสด ↔ พนักงาน

    แถวที่ระบบเขียนเองใช้ `nickname or full_name` (cashbook.py::_resolve_advance_rows)
    แต่แถวเก่าที่คนคีย์มือใช้ชื่อจริงคำแรก ('วิภา', 'วุฒิพงษ์') จึงรับทั้งสามแบบ
    """
    t = (tag or "").strip()
    if not t:
        return False
    full = emp["full_name"] or ""
    return t in {emp["nickname"] or "", full, full.split(" ")[0]}


def money(x):
    return round(float(x or 0), 2)


def fetch(conn):
    emps = {e["id"]: e for e in conn.execute(
        "SELECT id, emp_code, full_name, nickname, is_active FROM employees")}

    linked = defaultdict(list)  # advance_id -> [cashbook row]
    cash_unlinked = []
    for r in conn.execute(
            """SELECT id, txn_date, account_id, category, user_category, amount,
                      description, note, salary_advance_id
                 FROM cashbook_transactions
                ORDER BY txn_date, id"""):
        if r["salary_advance_id"] is not None:
            linked[r["salary_advance_id"]].append(r)
        elif r["category"] == ADVANCE_CATEGORY or looks_like_advance(r["description"]):
            cash_unlinked.append(r)

    advances = list(conn.execute(
        """SELECT id, employee_id, advance_date, amount, note, deducted_in_run_id,
                  from_account_id
             FROM salary_advances
            ORDER BY advance_date, id"""))
    adv_unlinked = [a for a in advances if a["id"] not in linked]
    return emps, advances, adv_unlinked, cash_unlinked, linked


def propose(emps, cash_unlinked, adv_unlinked):
    """จับคู่แถวเงินสดที่ยังไม่ลิงก์ เข้ากับ advance ที่ยังไม่ลิงก์

    รับเฉพาะคู่ที่ *ไม่กำกวมทั้งสองทาง* — แถวเงินสดเห็น advance ตัวเดียว และ
    advance ตัวนั้นก็เห็นแถวเงินสดแถวเดียวเช่นกัน ถ้าฝั่งใดฝั่งหนึ่งมีหลายตัว
    จะตกไปเป็น AMBIGUOUS ให้คนตัดสิน (บอลเบิก ฿1,300 สามครั้งใน 6 วัน)

    ชั้น 1 = คน+ยอด+วันตรงเป๊ะ · ชั้น 2 = คน+ยอด ตรง อยู่เดือนเดียวกัน
    (คนคีย์ cashbook วันที่เงินออก แต่คีย์ advance วันสิ้นเดือน)
    """
    def candidates(c, tier):
        out = []
        for a in adv_unlinked:
            emp = emps.get(a["employee_id"])
            if not emp or not matches_employee(c["user_category"], emp):
                continue
            if money(a["amount"]) != money(c["amount"]):
                continue
            if tier == 1 and a["advance_date"] != c["txn_date"]:
                continue
            if tier == 2 and a["advance_date"][:7] != c["txn_date"][:7]:
                continue
            out.append(a)
        return out

    pairs, ambiguous, unpaired = [], [], []
    taken = set()
    for tier in (1, 2):
        # ชั้นที่แล้วกินไปบ้าง คิดใหม่ทุกชั้นจากของที่ยังเหลือ
        pool = [c for c in cash_unlinked
                if c["id"] not in {p["cash"]["id"] for p in pairs}]
        cand = {c["id"]: [a for a in candidates(c, tier) if a["id"] not in taken]
                for c in pool}
        # ความไม่กำกวมย้อนกลับ: advance ตัวนี้ถูกกี่แถวเงินสดจีบ (ในชั้นนี้)
        suitors = defaultdict(list)
        for cid, lst in cand.items():
            for a in lst:
                suitors[a["id"]].append(cid)
        for c in pool:
            lst = cand[c["id"]]
            if len(lst) == 1 and len(suitors[lst[0]["id"]]) == 1:
                # ⚠ ชั้น 1 ตัดสินแทนคนได้เงียบๆ: บอลเบิก ฿1,300 สามครั้งใน 6 วัน
                # มองทั้งเดือนแล้วกำกวม แต่ "วันตรงกัน" ทำให้เหลือตัวเดียว
                # ทันที ติดธงไว้ให้คนเห็นว่าคู่นี้มาจากกติกา ไม่ใช่ความชัดในตัว
                rule_decided = tier == 1 and len(candidates(c, 2)) > 1
                pairs.append({"cash": c, "adv": lst[0], "tier": tier,
                              "rule_decided": rule_decided})
                taken.add(lst[0]["id"])

    paired_cash = {p["cash"]["id"] for p in pairs}
    for c in cash_unlinked:
        if c["id"] in paired_cash:
            continue
        lst = [a for a in candidates(c, 2) if a["id"] not in taken]
        (ambiguous if lst else unpaired).append({"cash": c, "options": lst})
    return pairs, ambiguous, unpaired


def loose_candidates(conn, emps, adv, exclude_ids):
    """advance ที่ยังหาคู่ไม่ได้ — กวาดสมุดเงินสด *ทั้งเล่ม* หายอดเท่ากันในเดือนเดียวกัน

    ไม่สนหมวดหรือ description เลย เพราะเคสที่หลุดคือแถวที่คนคีย์โดยไม่เขียนคำว่า
    "เบิกล่วงหน้า" ไว้ (adv 13 ของริน ↔ ct 319 'เงินเดือนวันแรก 1 วันแล้วออก')
    ผลลัพธ์เป็นเบาะแสให้คนตรวจ ไม่ใช่ข้อเสนอให้ apply
    """
    emp = emps.get(adv["employee_id"])
    rows = conn.execute(
        """SELECT id, txn_date, category, user_category, amount, description
             FROM cashbook_transactions
            WHERE salary_advance_id IS NULL
              AND ROUND(amount, 2) = ?
              AND strftime('%Y-%m', txn_date) = ?
            ORDER BY txn_date, id""",
        (money(adv["amount"]), adv["advance_date"][:7])).fetchall()
    return [r for r in rows
            if r["id"] not in exclude_ids
            and (emp is None or matches_employee(r["user_category"], emp))]


def linked_pair_problems(emps, linked, advances):
    """คู่ที่ลิงก์แล้วแต่ข้อมูลสองฝั่งไม่ตรงกัน — ใช้ตรวจซ้ำหลัง apply รอบ 2

    (Put ไม่ยอม bulk-apply เพราะกลัวลิงก์ผิดคู่แล้วไม่มีใครรู้ — นี่คือตัวที่รู้)
    """
    by_id = {a["id"]: a for a in advances}
    out = []
    for adv_id, rows in sorted(linked.items()):
        a = by_id.get(adv_id)
        if a is None:
            out.append("cashbook %s ชี้ไป advance %s ที่ไม่มีอยู่จริง"
                       % ([r["id"] for r in rows], adv_id))
            continue
        if len(rows) > 1:
            out.append("advance %d ถูกลิงก์จากแถวเงินสด %s (ต้องมีแถวเดียว)"
                       % (adv_id, [r["id"] for r in rows]))
        for r in rows:
            emp = emps.get(a["employee_id"])
            if money(r["amount"]) != money(a["amount"]):
                out.append("ct %d ↔ adv %d ยอดไม่ตรง (%.2f vs %.2f)"
                           % (r["id"], adv_id, money(r["amount"]), money(a["amount"])))
            if r["category"] != ADVANCE_CATEGORY:
                out.append("ct %d ↔ adv %d แต่หมวดเป็น '%s'"
                           % (r["id"], adv_id, r["category"]))
            if emp and r["user_category"] and not matches_employee(r["user_category"], emp):
                out.append("ct %d ↔ adv %d คนละคน (แท็ก '%s' vs %s)"
                           % (r["id"], adv_id, r["user_category"], emp_label(emp)))
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=os.environ.get(
        "DATABASE_PATH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "..", "inventory_app", "instance", "inventory.db")))
    p.add_argument("--json", action="store_true", help="พิมพ์ JSON แทนรายงาน")
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    emps, advances, adv_unlinked, cash_unlinked, linked = fetch(conn)
    pairs, ambiguous, unpaired = propose(emps, cash_unlinked, adv_unlinked)

    matched_adv = {p["adv"]["id"] for p in pairs}
    considered_cash = {c["id"] for c in cash_unlinked}
    leftovers = []
    for a in adv_unlinked:
        if a["id"] in matched_adv:
            continue
        leftovers.append((a, loose_candidates(conn, emps, a, considered_cash)))
    problems = linked_pair_problems(emps, linked, advances)

    def cash_str(c):
        return "ct %-4d %s %-10s %10.2f  [%s] %s" % (
            c["id"], c["txn_date"], c["user_category"] or "-", money(c["amount"]),
            c["category"], (c["description"] or "")[:44])

    def adv_str(a):
        emp = emps.get(a["employee_id"])
        return "adv %-3d %s %-10s %10.2f  หักใน run %s %s" % (
            a["id"], a["advance_date"],
            (emp["nickname"] or emp["full_name"]) if emp else "?",
            money(a["amount"]), a["deducted_in_run_id"] or "-",
            ("· " + a["note"]) if a["note"] else "")

    if args.json:
        json.dump({
            "db": args.db,
            "pairs": [{"cashbook_id": x["cash"]["id"], "advance_id": x["adv"]["id"],
                       "tier": x["tier"], "rule_decided": x["rule_decided"],
                       "txn_date": x["cash"]["txn_date"],
                       "advance_date": x["adv"]["advance_date"],
                       "amount": money(x["cash"]["amount"]),
                       "user_category": x["cash"]["user_category"],
                       "employee_id": x["adv"]["employee_id"],
                       "category_now": x["cash"]["category"]} for x in pairs],
            "ambiguous": [{"cashbook_id": x["cash"]["id"],
                           "options": [a["id"] for a in x["options"]]}
                          for x in ambiguous],
            "cashbook_no_match": [x["cash"]["id"] for x in unpaired],
            "advances_no_cashbook": [
                {"advance_id": a["id"], "loose_candidates": [r["id"] for r in c]}
                for a, c in leftovers],
            "linked_pair_problems": problems,
        }, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0

    print("ตรวจการผูกคู่เงินเบิกล่วงหน้า — %s" % args.db)
    print("=" * 78)
    print("แถวเงินสดที่ยังไม่ลิงก์ %d · advance ที่ยังไม่ลิงก์ %d · คู่ที่ลิงก์แล้ว %d"
          % (len(cash_unlinked), len(adv_unlinked), len(linked)))

    print("\n[1] เสนอจับคู่ — ไม่กำกวมทั้งสองทาง (%d)" % len(pairs))
    n_ruled = sum(1 for x in pairs if x["rule_decided"])
    if n_ruled:
        print("    ⚠ %d คู่ชี้ขาดด้วยกติกา 'วันตรงกัน' เท่านั้น — มองทั้งเดือนแล้วกำกวม"
              % n_ruled)
    for x in sorted(pairs, key=lambda x: x["cash"]["txn_date"]):
        print("  %s ชั้น %d  %s" % ("⚠" if x["rule_decided"] else " ",
                                     x["tier"], cash_str(x["cash"])))
        print("     └→  %s" % adv_str(x["adv"]))

    print("\n[2] กำกวม — ต้องให้คนชี้ (%d)" % len(ambiguous))
    for x in ambiguous:
        print("  %s" % cash_str(x["cash"]))
        for a in x["options"]:
            print("     ?   %s" % adv_str(a))

    print("\n[3] แถวเงินสดที่หา advance คู่ไม่เจอเลย (%d)" % len(unpaired))
    for x in unpaired:
        print("  %s" % cash_str(x["cash"]))

    print("\n[4] advance ที่ยังไม่มีแถวเงินสดคู่ (%d)" % len(leftovers))
    for a, cands in leftovers:
        print("  %s" % adv_str(a))
        for r in cands:
            print("     เบาะแส  ct %-4d %s %-10s %10.2f  [%s] %s" % (
                r["id"], r["txn_date"], r["user_category"] or "-",
                money(r["amount"]), r["category"], (r["description"] or "")[:40]))

    print("\n[5] คู่ที่ลิงก์แล้วแต่ข้อมูลขัดกัน (%d)" % len(problems))
    for m in problems:
        print("  ⚠ %s" % m)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
