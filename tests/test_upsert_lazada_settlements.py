import models

def test_upsert_inserts_and_replaces_by_statement(tmp_db_conn):
    c = tmp_db_conn
    c.execute("DELETE FROM lazada_statement_settlement")
    n = models.upsert_lazada_settlements(c, [
        {'statement':'THJ-2026-0617','settled_at':'2026-06-18 02:25:02','amount':84.42},
        {'statement':'THJ-2026-0618','settled_at':'2026-06-19 02:26:12','amount':94.10},
    ])
    assert n == 2
    got = c.execute("SELECT settled_at, amount FROM lazada_statement_settlement WHERE statement='THJ-2026-0617'").fetchone()
    assert got['settled_at'] == '2026-06-18 02:25:02' and got['amount'] == 84.42
    # re-import same statement with a corrected time/amount = replace, not duplicate
    models.upsert_lazada_settlements(c, [
        {'statement':'THJ-2026-0617','settled_at':'2026-06-18 02:30:00','amount':85.00}])
    rows = c.execute("SELECT settled_at, amount FROM lazada_statement_settlement WHERE statement='THJ-2026-0617'").fetchall()
    assert len(rows) == 1 and rows[0]['settled_at'] == '2026-06-18 02:30:00' and rows[0]['amount'] == 85.00
