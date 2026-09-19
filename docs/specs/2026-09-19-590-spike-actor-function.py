"""Spike D: an app-defined SQL function carries the actor into PERSISTENT triggers.
Temp DB only. Answers, each printed as a line:
 1. CREATE TRIGGER referencing an unregistered function: allowed on a raw conn?
 2. registered conn: guard + audit row carry the actor
 3. registered conn, actor None: guard refuses (fail closed, readable message)
 4. RAW conn (no function), UPDATE of a NON-cost column: must still work (column-list trigger)
 5. RAW conn, UPDATE of cost_price: fails closed ('no such function')
 6. RAW conn, INSERT when an INSERT trigger references the function (even with a false WHEN)
 7. trusted_schema=OFF: does the function still run from a trigger?
 8. two connections, interleaved: actor never crosses connections
 9. executemany bulk (1000 rows) under one txn: every audit row signed
"""
import os, sqlite3, tempfile
db = 'file:spike590?mode=memory&cache=shared'
raw = sqlite3.connect(db, uri=True)
raw.executescript("""
CREATE TABLE products(id INTEGER PRIMARY KEY, product_name TEXT, cost_price REAL DEFAULT 0, opening_cost REAL DEFAULT 0);
CREATE TABLE audit_log(id INTEGER PRIMARY KEY, table_name TEXT, row_id INT, action TEXT, changed_fields TEXT, user TEXT);
""")
try:
    raw.executescript("""
    CREATE TRIGGER cost_guard BEFORE UPDATE OF cost_price, opening_cost ON products
    WHEN (OLD.cost_price IS NOT NEW.cost_price OR OLD.opening_cost IS NOT NEW.opening_cost)
     AND sendy_actor() IS NULL
    BEGIN SELECT RAISE(ABORT, 'cost change needs an actor'); END;
    CREATE TRIGGER cost_audit AFTER UPDATE OF cost_price, opening_cost ON products
    WHEN (OLD.cost_price IS NOT NEW.cost_price OR OLD.opening_cost IS NOT NEW.opening_cost)
    BEGIN INSERT INTO audit_log(table_name,row_id,action,changed_fields,user)
          VALUES('products', NEW.id, 'UPDATE', json_object('cost_price', json_array(OLD.cost_price, NEW.cost_price)), sendy_actor()); END;
    """)
    print('1 CREATE TRIGGER with unregistered fn on raw conn: ALLOWED')
except Exception as e:
    print('1 CREATE TRIGGER with unregistered fn: REFUSED', e)
raw.execute("INSERT INTO products(id, product_name, cost_price) VALUES (1,'a',10),(2,'b',20)"); raw.commit()

def conn_as(actor_box):
    c = sqlite3.connect(db, uri=True, timeout=5)
    c.create_function('sendy_actor', 0, lambda: actor_box[0])
    return c

box = ['ui:admin']
c = conn_as(box)
c.execute("UPDATE products SET cost_price=11 WHERE id=1"); c.commit()
print('2 signed update ->', c.execute("select user from audit_log order by id desc limit 1").fetchone())
box[0] = None
try:
    c.execute("UPDATE products SET cost_price=12 WHERE id=1"); print('3 unsigned update: ALLOWED (bad)')
except sqlite3.DatabaseError as e:
    print('3 unsigned update refused:', e); c.rollback()
raw2 = sqlite3.connect(db, uri=True)
try:
    raw2.execute("UPDATE products SET product_name='x' WHERE id=1"); raw2.commit(); print('4 raw non-cost update: OK')
except Exception as e:
    print('4 raw non-cost update FAILED:', e)
try:
    raw2.execute("UPDATE products SET cost_price=13 WHERE id=1"); print('5 raw cost update: ALLOWED (bad)')
except Exception as e:
    print('5 raw cost update refused:', type(e).__name__, e); raw2.rollback()
try:
    raw2.execute("UPDATE products SET product_name='y', cost_price=cost_price WHERE id=1"); print('5b raw no-op cost in SET: ALLOWED')
except Exception as e:
    print('5b raw no-op cost in SET refused:', e); raw2.rollback()
raw2.executescript("""CREATE TRIGGER ins_audit AFTER INSERT ON products WHEN NEW.cost_price <> 0 AND 0
BEGIN INSERT INTO audit_log(table_name,row_id,action,user) VALUES('products',NEW.id,'INSERT',sendy_actor()); END;""")
try:
    raw2.execute("INSERT INTO products(product_name) VALUES ('zero-cost')"); raw2.commit(); print('6 raw INSERT with fn-referencing INSERT trigger (WHEN false): OK')
except Exception as e:
    print('6 raw INSERT with fn-referencing INSERT trigger (WHEN false) FAILED:', e); raw2.rollback()
raw2.execute("DROP TRIGGER ins_audit"); raw2.commit()
box[0] = 'ui:admin'
c.execute("PRAGMA trusted_schema=OFF")
try:
    c.execute("UPDATE products SET cost_price=14 WHERE id=1"); c.commit(); print('7 trusted_schema=OFF: fn still runs from trigger')
except Exception as e:
    print('7 trusted_schema=OFF:', e); c.rollback()
c.execute("PRAGMA trusted_schema=ON")
# 8 interleave: A declares, B writes unsigned, A writes
boxA, boxB = ['script:A'], [None]
A, B = conn_as(boxA), conn_as(boxB)
A.execute("UPDATE products SET cost_price=15 WHERE id=1"); A.commit()
try:
    B.execute("UPDATE products SET cost_price=25 WHERE id=2"); print('8 B unsigned: ALLOWED (crossed!)')
except sqlite3.DatabaseError as e:
    print('8 B unsigned refused while A is signed:', e); B.rollback()
# 9 bulk
A.executemany("UPDATE products SET cost_price=? WHERE id=?", [(i, 1 + (i % 2)) for i in range(1, 1001)]); A.commit()
n, signed = A.execute("select count(*), sum(user='script:A') from audit_log where id > 2").fetchone()
print('9 bulk audit rows', n, 'signed', signed)
print('sqlite', sqlite3.sqlite_version)
