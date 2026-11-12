"""Tests for services/lots.py — receiving, FIFO/active stock reads, the housemade
quantity guard, cost cascade, the exhaust-threshold, and delete protection."""

import pytest

from services.lots import (
    receive_lot, get_lots_for_material, get_all_lots_for_material,
    get_material_stock_from_lots, get_active_lots_for_materials, update_lot,
    delete_lot, get_lot_by_id, get_batches_for_lot, exhaust_lot_if_depleted,
    LOT_EXHAUST_THRESHOLD, LotChangedError,
)
from database import get_db_connection


def test_receive_lot_rejects_non_positive(make_material):
    mid = make_material("Matcha")
    assert receive_lot(mid, "L1", 0, "2024-01-01") is None
    assert receive_lot(mid, "L2", -5, "2024-01-01") is None
    assert receive_lot(mid, "L3", None, "2024-01-01") is None


def test_stock_from_lots_sums_active_only(make_material, make_lot):
    mid = make_material("Matcha")
    make_lot(mid, 30)
    make_lot(mid, 20)
    assert get_material_stock_from_lots(mid) == 50


def test_stock_from_lots_excludes_expired(make_material, make_lot):
    mid = make_material("Matcha")
    make_lot(mid, 40)
    make_lot(mid, 100, expiry_date="2000-01-01")
    assert get_material_stock_from_lots(mid) == 40


def test_stock_from_lots_zero_when_none(make_material):
    mid = make_material("Empty")
    assert get_material_stock_from_lots(mid) == 0.0


# --- server-timezone-vs-business-timezone fix -----------------------------------------
def test_lot_expiring_pacific_tomorrow_still_active_during_utc_evening_rollover(
    freeze_business_time, make_material, make_lot
):
    # Render runs UTC; Botaniks runs Pacific. Frozen instant: 2026-01-02 06:00 UTC =
    # 2026-01-01 22:00 PST — Pacific's evening of Jan 1, but UTC's calendar date has already
    # rolled to Jan 2. A lot expiring on Pacific's actual tomorrow (Jan 2) must still count as
    # active: it was being read as already-expired a day early whenever "now" was computed from
    # the server's UTC clock instead of Botaniks' actual Pacific evening.
    from datetime import datetime
    from zoneinfo import ZoneInfo
    freeze_business_time(datetime(2026, 1, 2, 6, 0, 0, tzinfo=ZoneInfo("UTC")))

    mid = make_material("Matcha")
    make_lot(mid, 40, expiry_date="2026-01-02")
    assert get_material_stock_from_lots(mid) == 40


def test_active_lots_are_fifo_ordered(make_material, make_lot):
    mid = make_material("Matcha")
    make_lot(mid, 10, received_date="2024-03-01", lot_number="NEW")
    make_lot(mid, 10, received_date="2024-01-01", lot_number="OLD")
    df = get_lots_for_material(mid)
    assert list(df["lot_number"]) == ["OLD", "NEW"]


def test_active_lots_exclude_expired_and_zero(make_material, make_lot):
    mid = make_material("Matcha")
    make_lot(mid, 10, lot_number="GOOD")
    make_lot(mid, 10, lot_number="EXP", expiry_date="2000-01-01")
    df = get_lots_for_material(mid)
    assert list(df["lot_number"]) == ["GOOD"]


def test_get_all_lots_for_material_includes_expired(make_material, make_lot):
    mid = make_material("Matcha")
    make_lot(mid, 10, lot_number="GOOD")
    make_lot(mid, 10, lot_number="EXP", expiry_date="2000-01-01")
    df = get_all_lots_for_material(mid)
    assert set(df["lot_number"]) == {"GOOD", "EXP"}


def test_bulk_active_lots_grouping(make_material, make_lot):
    a = make_material("A")
    b = make_material("B")
    make_lot(a, 5)
    make_lot(b, 7)
    grouped = get_active_lots_for_materials([a, b])
    assert grouped[a][0]["quantity"] == 5
    assert grouped[b][0]["quantity"] == 7


def test_bulk_active_lots_empty_input():
    assert get_active_lots_for_materials([]) == {}


def test_exhaust_threshold_zeros_tiny_residual(make_material, make_lot, db):
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    cur = db.cursor()
    # leave a residual below the threshold, then exhaust
    db.execute(cur, "UPDATE raw_material_lots SET quantity = %s WHERE lot_id = %s",
               (LOT_EXHAUST_THRESHOLD / 2, lid))
    exhaust_lot_if_depleted(db, cur, lid)
    db.commit()
    db.execute(cur, "SELECT quantity, status FROM raw_material_lots WHERE lot_id = %s", (lid,))
    qty, status = cur.fetchone()
    assert qty == 0
    assert status == "inactive"


def test_exhaust_leaves_real_quantity_untouched(make_material, make_lot, db):
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    cur = db.cursor()
    exhaust_lot_if_depleted(db, cur, lid)
    db.commit()
    db.execute(cur, "SELECT quantity, status FROM raw_material_lots WHERE lot_id = %s", (lid,))
    qty, status = cur.fetchone()
    assert qty == 100
    assert status == "active"


def test_update_lot_changes_fields(make_material, make_lot):
    mid = make_material("Matcha")
    lid = make_lot(mid, 10)
    assert update_lot(lid, location="Shelf B", supplier="Acme") is True
    df = get_lot_by_id(lid)
    assert df.iloc[0]["location"] == "Shelf B"
    assert df.iloc[0]["supplier"] == "Acme"


def test_update_lot_blocks_quantity_on_housemade(make_material, make_lot):
    mid = make_material("Mix Product", is_housemade=True, category="Mix")
    lid = make_lot(mid, 10, lot_number="MIX-BATCH-001")
    # quantity edit must be silently dropped for a housemade lot; other fields still apply
    assert update_lot(lid, quantity=999, location="X", is_housemade=True) is True
    df = get_lot_by_id(lid)
    assert df.iloc[0]["quantity"] == 10
    assert df.iloc[0]["location"] == "X"


def test_update_lot_cost_cascades_to_batch_materials(make_material, make_lot, db):
    mid = make_material("Matcha")
    lid = make_lot(mid, 10, cost_per_unit=2)
    cur = db.cursor()
    db.execute(cur, """INSERT INTO batches (product_name, quantity, status) VALUES (%s, %s, 'Ready')""",
               ("B", 1))
    bid = db.get_last_insert_id(cur)
    db.execute(cur, """INSERT INTO batch_materials (batch_id, material_id, lot_id, quantity_used, cost_per_unit)
                       VALUES (%s, %s, %s, %s, %s)""", (bid, mid, lid, 5, 2))
    db.commit()
    update_lot(lid, cost_per_unit=9)
    db.execute(cur, "SELECT cost_per_unit FROM batch_materials WHERE lot_id = %s", (lid,))
    assert cur.fetchone()[0] == 9


def test_delete_lot_blocked_when_referenced_by_batch(make_material, make_lot, db):
    mid = make_material("Matcha")
    lid = make_lot(mid, 10)
    cur = db.cursor()
    db.execute(cur, """INSERT INTO batches (product_name, quantity, status) VALUES (%s, %s, 'Ready')""",
               ("B", 1))
    bid = db.get_last_insert_id(cur)
    db.execute(cur, """INSERT INTO batch_materials (batch_id, material_id, lot_id, quantity_used, cost_per_unit)
                       VALUES (%s, %s, %s, %s, %s)""", (bid, mid, lid, 5, 1))
    db.commit()
    assert delete_lot(lid) is False
    assert get_lot_by_id(lid) is not None  # still there
    assert get_batches_for_lot(lid)[0]["batch_id"] == bid


def test_delete_lot_succeeds_when_unreferenced(make_material, make_lot):
    mid = make_material("Matcha")
    lid = make_lot(mid, 10)
    assert delete_lot(lid) is True
    assert get_lot_by_id(lid) is None


# lot-edit-safety: status follows a hand-edited quantity, and a stale edit can't overwrite stock.

def test_update_lot_zero_marks_inactive(make_material, make_lot):
    mid = make_material("Matcha")
    lid = make_lot(mid, 10)
    assert update_lot(lid, quantity=0) is True
    df = get_lot_by_id(lid)
    assert df.iloc[0]["quantity"] == 0
    assert df.iloc[0]["status"] == "inactive"
    assert get_material_stock_from_lots(mid) == 0


def test_update_lot_refill_reactivates_exhausted_lot(make_material, make_lot, db):
    mid = make_material("Matcha")
    lid = make_lot(mid, 10)
    # a batch used it all: the deduction path leaves it at 0 and 'inactive'
    cur = db.cursor()
    db.execute(cur, "UPDATE raw_material_lots SET quantity = 0 WHERE lot_id = %s", (lid,))
    exhaust_lot_if_depleted(db, cur, lid)
    db.commit()
    assert update_lot(lid, quantity=25) is True
    df = get_lot_by_id(lid)
    assert df.iloc[0]["status"] == "active"
    assert lid in [int(x) for x in get_lots_for_material(mid)["lot_id"]]


def test_update_lot_without_quantity_leaves_status_alone(make_material, make_lot):
    mid = make_material("Matcha")
    lid = make_lot(mid, 10)
    update_lot(lid, quantity=0)
    update_lot(lid, location="Fern")
    assert get_lot_by_id(lid).iloc[0]["status"] == "inactive"


def test_update_lot_refuses_stale_quantity(make_material, make_lot, db):
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    # a batch draws 20 after the user opened the edit page showing 100
    cur = db.cursor()
    db.execute(cur, "UPDATE raw_material_lots SET quantity = 80 WHERE lot_id = %s", (lid,))
    db.commit()
    with pytest.raises(LotChangedError):
        update_lot(lid, quantity=90, location="Fern", expected_quantity=100)
    df = get_lot_by_id(lid)
    assert df.iloc[0]["quantity"] == 80          # deduction kept
    assert df.iloc[0]["location"] is None        # nothing else saved either


def test_update_lot_applies_when_quantity_matches_expected(make_material, make_lot):
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    assert update_lot(lid, quantity=90, expected_quantity=100) is True
    assert get_lot_by_id(lid).iloc[0]["quantity"] == 90
