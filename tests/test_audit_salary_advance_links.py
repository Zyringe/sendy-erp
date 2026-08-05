"""Tests for scripts/audit_salary_advance_links.py (synthetic ids).

The detector is read-only, but its output drives a hand-applied cleanup of
money rows — so the parts that can silently say "nothing wrong" are pinned
here: the Thai substring filter, the both-ways uniqueness rule, the
exact-date tie-break flag, and the linked-pair consistency check (whose
clean run on prod prints "0" and would look identical if it were incapable
of firing).
"""
import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import audit_salary_advance_links as aud  # noqa: E402

ADV = aud.ADVANCE_CATEGORY
SALARY = "เงินเดือน"


def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE employees (id INTEGER PRIMARY KEY, emp_code TEXT,
            full_name TEXT, nickname TEXT, is_active INTEGER DEFAULT 1);
        CREATE TABLE salary_advances (id INTEGER PRIMARY KEY, employee_id INTEGER,
            advance_date TEXT, amount REAL, note TEXT, deducted_in_run_id INTEGER,
            from_account_id INTEGER);
        CREATE TABLE cashbook_transactions (id INTEGER PRIMARY KEY, account_id INTEGER,
            txn_date TEXT, direction TEXT, category TEXT, user_category TEXT,
            amount REAL, description TEXT, note TEXT, salary_advance_id INTEGER);
    """)
    c.execute("INSERT INTO employees VALUES (5,'EMP005','วุฒิพงษ์ แปงนุจา','บอล',1)")
    c.execute("INSERT INTO employees VALUES (4,'EMP004','วิภา ขมสันเทียะ','หลุย',1)")
    return c


def _adv(c, aid, emp, date, amt, run=None):
    c.execute("INSERT INTO salary_advances (id,employee_id,advance_date,amount,"
              "deducted_in_run_id) VALUES (?,?,?,?,?)", (aid, emp, date, amt, run))


def _cash(c, cid, date, tag, amt, desc, cat=SALARY, link=None):
    c.execute("INSERT INTO cashbook_transactions (id,account_id,txn_date,direction,"
              "category,user_category,amount,description,salary_advance_id) "
              "VALUES (?,1,?,'expense',?,?,?,?,?)", (cid, date, cat, tag, amt, desc, link))


def _run(c):
    emps, advances, adv_unlinked, cash_unlinked, linked = aud.fetch(c)
    pairs, ambiguous, unpaired = aud.propose(emps, cash_unlinked, adv_unlinked)
    return emps, advances, linked, cash_unlinked, pairs, ambiguous, unpaired


def test_exact_date_breaks_the_tie_and_the_pair_is_flagged_as_rule_decided():
    """บอล's three ฿1,300 advances in 6 days: ambiguous by month, unique by day.

    The tie-break is a RULE, not a fact in the data — every such pair must carry
    rule_decided so a reviewer sees which links the rule decided for them.
    """
    c = _db()
    for aid, d in ((12, "2026-04-20"), (14, "2026-04-23"), (15, "2026-04-25")):
        _adv(c, aid, 5, d, 1300, run=4)
    for cid, d in ((316, "2026-04-20"), (320, "2026-04-23"), (322, "2026-04-25")):
        _cash(c, cid, d, "วุฒิพงษ์", 1300, "เงินเดือนวุฒิพงษ์ 4/26 (เบิกล่วงหน้า)")

    _e, _a, _l, cash_unlinked, pairs, ambiguous, unpaired = _run(c)
    assert len(cash_unlinked) == 3
    assert len(pairs) == 3 and not ambiguous and not unpaired
    assert {(p["cash"]["id"], p["adv"]["id"]) for p in pairs} == {
        (316, 12), (320, 14), (322, 15)}
    assert all(p["tier"] == 1 for p in pairs)
    assert [p["rule_decided"] for p in pairs] == [True, True, True]


def test_a_unique_pair_is_not_flagged_rule_decided():
    """Control for the test above — the flag must distinguish, not always fire."""
    c = _db()
    _adv(c, 11, 4, "2026-04-20", 2000, run=4)
    _cash(c, 315, "2026-04-20", "วิภา", 2000, "เงินเดือนวิภา 4/26 (เบิกล่วงหน้า)")

    *_, pairs, ambiguous, unpaired = _run(c)
    assert len(pairs) == 1 and not ambiguous and not unpaired
    assert pairs[0]["rule_decided"] is False


def test_deduction_row_is_not_mistaken_for_an_advance():
    """'(หักเบิกล่วงหน้า)' = the NET SALARY payout, and it contains 'เบิกล่วงหน้า'.

    A bare substring test cannot tell the two apart; the paired assertion below
    is what makes this test capable of failing in both directions.
    """
    c = _db()
    _cash(c, 323, "2026-04-30", "วิภา", 9400, "เงินเดือนวิภา 4/26 (หักเบิกล่วงหน้า)")
    _cash(c, 315, "2026-04-20", "วิภา", 2000, "เงินเดือนวิภา 4/26 (เบิกล่วงหน้า)")

    *_, cash_unlinked, _p, _am, _u = _run(c)
    assert [r["id"] for r in cash_unlinked] == [315]
    assert aud.looks_like_advance("เงินเดือนวิภา 4/26 (หักเบิกล่วงหน้า)") is False
    assert aud.looks_like_advance("เงินเดือนวิภา 4/26 (เบิกล่วงหน้า)") is True


def test_same_month_different_day_pairs_at_tier_2():
    """Cash keyed on the day it left; the advance keyed at month-end (ct 642/adv 26)."""
    c = _db()
    _adv(c, 26, 4, "2026-06-30", 3000, run=6)
    _cash(c, 642, "2026-06-01", "หลุย", 3000, "เงินรายวันทั้งเดือน", cat=ADV)

    *_, pairs, ambiguous, unpaired = _run(c)
    assert len(pairs) == 1 and not ambiguous and not unpaired
    assert (pairs[0]["cash"]["id"], pairs[0]["adv"]["id"], pairs[0]["tier"]) == (642, 26, 2)


def test_two_advances_on_the_same_day_stay_ambiguous():
    """Uniqueness is required BOTH ways — one cash row, two identical advances."""
    c = _db()
    _adv(c, 12, 5, "2026-04-20", 1300, run=4)
    _adv(c, 13, 5, "2026-04-20", 1300, run=4)
    _cash(c, 316, "2026-04-20", "วุฒิพงษ์", 1300, "เงินเดือนวุฒิพงษ์ 4/26 (เบิกล่วงหน้า)")

    *_, pairs, ambiguous, unpaired = _run(c)
    assert not pairs and not unpaired
    assert len(ambiguous) == 1
    assert {a["id"] for a in ambiguous[0]["options"]} == {12, 13}


def test_a_wrong_amount_on_a_linked_pair_is_reported():
    """Check [5] prints 0 on prod — prove it can print something."""
    c = _db()
    _adv(c, 27, 4, "2026-07-01", 7000)
    _cash(c, 638, "2026-07-01", "หลุย", 7000, "เงินรายวัน", cat=ADV, link=27)
    emps, advances, linked, *_ = _run(c)
    assert len(linked) == 1
    assert aud.linked_pair_problems(emps, linked, advances) == []

    c.execute("UPDATE cashbook_transactions SET amount = 6000 WHERE id = 638")
    emps, advances, linked, *_ = _run(c)
    problems = aud.linked_pair_problems(emps, linked, advances)
    assert len(problems) == 1 and "ยอดไม่ตรง" in problems[0]


def test_a_linked_pair_pointing_at_the_wrong_person_is_reported():
    c = _db()
    _adv(c, 27, 4, "2026-07-01", 7000)          # หลุย
    _cash(c, 638, "2026-07-01", "วุฒิพงษ์", 7000, "เงินรายวัน", cat=ADV, link=27)
    emps, advances, linked, *_ = _run(c)
    problems = aud.linked_pair_problems(emps, linked, advances)
    assert len(problems) == 1 and "คนละคน" in problems[0]


def test_an_advance_with_no_cash_row_surfaces_its_loose_candidate():
    """ริน's ฿400: the cash row never said 'เบิกล่วงหน้า', so only the loose
    same-amount/same-month sweep can find it."""
    c = _db()
    c.execute("INSERT INTO employees VALUES (6,'EMP006','ธิติวุฒิ จันทพรม','ริน',0)")
    _adv(c, 13, 6, "2026-04-23", 400, run=4)
    _cash(c, 319, "2026-04-23", "ธิติวุฒิ", 400,
          "เงินเดือนธิติวุฒิ จันทพรม (1 วันแล้วออก)")

    emps, _a, _l, cash_unlinked, pairs, *_ = _run(c)
    assert not pairs and not cash_unlinked          # invisible to the normal pass
    adv = c.execute("SELECT * FROM salary_advances WHERE id = 13").fetchone()
    cands = aud.loose_candidates(c, emps, adv, exclude_ids=set())
    assert [r["id"] for r in cands] == [319]
