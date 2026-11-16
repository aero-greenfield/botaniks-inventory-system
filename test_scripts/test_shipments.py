"""Tests for services/shipments.py — partial-batch allocation via the shipment_batches
junction, derived batch status (Ready -> Partially Shipped -> Shipped), edit re-validation,
and delete restoring remaining quantity. Ported from the old test_partial_shipments.py
standalone script onto pytest fixtures.
"""

import pytest

from services.shipments import (
    create_shipment, get_shipment_by_id, get_all_shipments,
    update_shipment, delete_shipment, _batch_remaining,
)


@pytest.fixture
def make_ready_batch(db):
    """Insert a produced (Ready) standard batch directly; returns batch_id."""
    def _make(name, qty):
        cur = db.cursor()
        db.execute(cur, """
            INSERT INTO batches (product_name, quantity, status, batch_number, batch_type)
            VALUES (%s, %s, 'Ready', %s, 'standard')
        """, (name, qty, name))
        bid = db.get_last_insert_id(cur)
        db.commit()
        return bid
    return _make


def _status(db, batch_id):
    cur = db.cursor()
    db.execute(cur, "SELECT status FROM batches WHERE batch_id = %s", (batch_id,))
    row = cur.fetchone()
    return row[0] if row else None


def _remaining(db, batch_id):
    cur = db.cursor()
    return _batch_remaining(cur, db, batch_id)


def test_partial_allocation_sets_partially_shipped(make_ready_batch, db):
    a = make_ready_batch("A", 100)
    create_shipment({a: 30}, destination="Dest1")
    assert _remaining(db, a) == 70
    assert _status(db, a) == "Partially Shipped"


def test_full_allocation_across_two_shipments_marks_shipped(make_ready_batch, db):
    a = make_ready_batch("A", 100)
    b = make_ready_batch("B", 50)
    create_shipment({a: 30})
    create_shipment({a: 70, b: 20})
    assert _status(db, a) == "Shipped"
    assert abs(_remaining(db, a)) < 1e-9
    assert _status(db, b) == "Partially Shipped"
    assert _remaining(db, b) == 30


def test_over_allocation_raises(make_ready_batch):
    b = make_ready_batch("B", 50)
    with pytest.raises(ValueError):
        create_shipment({b: 9999})


def test_cannot_ship_planned_batch(db):
    cur = db.cursor()
    db.execute(cur, """INSERT INTO batches (product_name, quantity, status, batch_type)
                       VALUES ('P', 10, 'Planned', 'finished')""")
    pid = db.get_last_insert_id(cur)
    db.commit()
    with pytest.raises(ValueError):
        create_shipment({pid: 5})


def test_empty_shipment_raises():
    with pytest.raises(ValueError):
        create_shipment({})


def test_shipment_detail_carries_line_and_batch_totals(make_ready_batch):
    a = make_ready_batch("A", 100)
    s = create_shipment({a: 70}, destination="Dest")
    detail = get_shipment_by_id(s)
    line = next(x for x in detail["batches"] if x["batch_id"] == a)
    assert line["quantity"] == 70          # shipped on this shipment
    assert line["batch_quantity"] == 100   # produced total


def test_edit_removes_and_adjusts_lines(make_ready_batch, db):
    a = make_ready_batch("A", 100)
    b = make_ready_batch("B", 50)
    create_shipment({a: 30})           # a partially shipped on s1
    s2 = create_shipment({a: 70, b: 20})
    # drop A from s2, raise B to 40 (B remaining excl. s2 is 30, +current 20 => cap 50)
    update_shipment(s2, lines={b: 40})
    d2 = get_shipment_by_id(s2)
    assert all(x["batch_id"] != a for x in d2["batches"])
    line_b = next(x for x in d2["batches"] if x["batch_id"] == b)
    assert line_b["quantity"] == 40
    assert _status(db, a) == "Partially Shipped"  # still on s1
    assert _remaining(db, a) == 70
    assert _remaining(db, b) == 10


def test_edit_over_allocation_raises(make_ready_batch):
    b = make_ready_batch("B", 50)
    s = create_shipment({b: 20})
    with pytest.raises(ValueError):
        update_shipment(s, lines={b: 60})  # cap is 50


def test_delete_restores_remaining_and_status(make_ready_batch, db):
    b = make_ready_batch("B", 50)
    s = create_shipment({b: 20})
    assert _status(db, b) == "Partially Shipped"
    assert delete_shipment(s) is True
    assert _status(db, b) == "Ready"
    assert _remaining(db, b) == 50


def test_delete_missing_shipment_raises():
    with pytest.raises(ValueError):
        delete_shipment(999999)


def test_get_all_shipments_counts_lines(make_ready_batch):
    a = make_ready_batch("A", 100)
    create_shipment({a: 10})
    df, _ = get_all_shipments(page=None)
    assert not df.empty
    assert int(df.iloc[0]["batch_count"]) == 1


def test_shipment_number_built_from_given_date(make_ready_batch):
    a = make_ready_batch("A", 100)
    s = create_shipment({a: 10}, date_shipped="2026-07-15")
    detail = get_shipment_by_id(s)
    assert detail["shipment"]["shipment_number"] == "2607151"
    assert detail["shipment"]["date_shipped"] == "2026-07-15"


def test_shipment_number_counter_increments_per_date(make_ready_batch):
    a = make_ready_batch("A", 100)
    b = make_ready_batch("B", 100)
    c = make_ready_batch("C", 100)
    s1 = create_shipment({a: 10}, date_shipped="2026-07-15")
    s2 = create_shipment({b: 10}, date_shipped="2026-07-15")
    s3 = create_shipment({c: 10}, date_shipped="2026-07-16")  # different date resets the counter
    assert get_shipment_by_id(s1)["shipment"]["shipment_number"] == "2607151"
    assert get_shipment_by_id(s2)["shipment"]["shipment_number"] == "2607152"
    assert get_shipment_by_id(s3)["shipment"]["shipment_number"] == "2607161"


def test_shipment_category_round_trips(make_ready_batch):
    a = make_ready_batch("A", 100)
    s = create_shipment({a: 10}, category="Wholesale")
    assert get_shipment_by_id(s)["shipment"]["category"] == "Wholesale"

    update_shipment(s, category="Retail")
    assert get_shipment_by_id(s)["shipment"]["category"] == "Retail"


def test_update_shipment_number_collision_raises(make_ready_batch):
    a = make_ready_batch("A", 100)
    b = make_ready_batch("B", 100)
    create_shipment({a: 10}, date_shipped="2026-07-15")           # -> 2607151
    s2 = create_shipment({b: 10}, date_shipped="2026-07-16")      # -> 2607161
    with pytest.raises(ValueError):
        update_shipment(s2, shipment_number="2607151")


def test_buyer_fields_round_trip(make_ready_batch):
    a = make_ready_batch("A", 10)
    s = create_shipment({a: 5}, buyer_name="Acme", carrier="UPS",
                        tracking_number="1Z999", buyer_contact="acme@example.com")
    ship = get_shipment_by_id(s)['shipment']
    assert (ship['buyer_name'], ship['carrier'], ship['tracking_number'], ship['buyer_contact']) == \
           ("Acme", "UPS", "1Z999", "acme@example.com")

    update_shipment(s, carrier="FedEx", tracking_number=None)
    ship = get_shipment_by_id(s)['shipment']
    assert (ship['buyer_name'], ship['carrier'], ship['tracking_number']) == ("Acme", "FedEx", None)
