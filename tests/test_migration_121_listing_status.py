"""Migration 121: marketplace_listing_status.

Grain = (platform, product_id_str) = one marketplace listing. Verifies:
- Lazada status aggregates per listing: delisted IFF every variation is inactive.
- Shopee status = delisted for the embedded "not shown" ids, live otherwise.
- The status CHECK constraint rejects anything but live/delisted.
- (platform, product_id_str) is unique (no dup listings from multi-variation rows).
"""
import os
import re
import sqlite3

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_MIG = os.path.join(_REPO, "data", "migrations", "121_marketplace_listing_status.sql")


def _first_shopee_notshown_id():
    sql = open(_MIG, encoding="utf-8").read()
    m = re.search(r"product_id_str IN \(([^)]+)\)", sql)
    assert m, "migration 121 should embed the shopee not-shown id list"
    return m.group(1).split(",")[0].strip().strip("'")


def _seed_and_migrate(conn):
    conn.executescript(
        "CREATE TABLE platform_skus (id INTEGER PRIMARY KEY, platform TEXT, "
        "product_id_str TEXT, raw_json TEXT, internal_product_id INTEGER);"
    )
    del_id = _first_shopee_notshown_id()
    rows = [
        # lazada listing with EVERY variation inactive -> delisted
        ("lazada", "LZ_ALL_INACTIVE", '{"status":"inactive"}'),
        ("lazada", "LZ_ALL_INACTIVE", '{"status":"inactive"}'),
        # lazada listing with a mix -> live (still buyable)
        ("lazada", "LZ_MIXED", '{"status":"inactive"}'),
        ("lazada", "LZ_MIXED", '{"status":"active"}'),
        # lazada with no status key -> defaults active -> live
        ("lazada", "LZ_NOSTATUS", "{}"),
        # shopee: an embedded not-shown id (two variations) -> delisted
        ("shopee", del_id, None),
        ("shopee", del_id, None),
        # shopee not in the list -> live
        ("shopee", "SHOPEE_LIVE_X", None),
    ]
    conn.executemany(
        "INSERT INTO platform_skus(platform, product_id_str, raw_json) VALUES (?,?,?)", rows
    )
    conn.commit()
    conn.executescript(open(_MIG, encoding="utf-8").read())
    return del_id


def _status(conn, platform, pidstr):
    r = conn.execute(
        "SELECT status FROM marketplace_listing_status WHERE platform=? AND product_id_str=?",
        (platform, pidstr),
    ).fetchone()
    return r[0] if r else None


def test_backfill_status_per_listing():
    conn = sqlite3.connect(":memory:")
    del_id = _seed_and_migrate(conn)
    assert _status(conn, "lazada", "LZ_ALL_INACTIVE") == "delisted"
    assert _status(conn, "lazada", "LZ_MIXED") == "live"
    assert _status(conn, "lazada", "LZ_NOSTATUS") == "live"
    assert _status(conn, "shopee", del_id) == "delisted"
    assert _status(conn, "shopee", "SHOPEE_LIVE_X") == "live"


def test_one_row_per_listing_no_dups():
    conn = sqlite3.connect(":memory:")
    _seed_and_migrate(conn)
    dups = conn.execute(
        "SELECT COUNT(*) FROM (SELECT platform, product_id_str FROM marketplace_listing_status "
        "GROUP BY platform, product_id_str HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    assert dups == 0


def test_status_check_constraint():
    conn = sqlite3.connect(":memory:")
    _seed_and_migrate(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO marketplace_listing_status(platform, product_id_str, status) "
            "VALUES ('shopee', 'BAD', 'banned')"
        )
