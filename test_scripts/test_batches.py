"""Tests for services/batches.py — the most complex logic in the app.

Covers: standard immediate deduction, lot coverage/over-allocation validation, expired/
insufficient lot rejection, multi-lot deduction + exhaustion, allow_negative override,
mix (housemade) output-lot creation, planned-batch promotion (stored-lot and FIFO paths),
promotion failure recording, delete-with-reallocation, and adjust/check batch materials.

lot_selections shape (per add_to_batches docstring): {material_id: [{'lot_id', 'qty'}, ...]}.
"""

import pytest

from services import batches as batches_mod  # for monkeypatching _fetch_overdue in the race tests
from services.batches import (
    add_to_batches, promote_planned_batches, promote_batch_now, delete_batch, get_batch_materials,
    get_batch_materials_for_reallocation, adjust_batch_material, check_batch_materials_stock,
    get_batch_by_id, get_batches, get_batches_planned, get_all_batches_with_id,
    get_planned_lot_selections, update_planned_lot_selections, clear_planned_lot_selections,
)
from services.lots import get_material_stock_from_lots
from services.materials import get_raw_material


PAST = "2020-01-01"  # always overdue, so promote_planned_batches picks it up


def _lot_qty(db, lot_id):
    cur = db.cursor()
    db.execute(cur, "SELECT quantity, status FROM raw_material_lots WHERE lot_id = %s", (lot_id,))
    return cur.fetchone()


def _batch_status(db, batch_id):
    cur = db.cursor()
    db.execute(cur, "SELECT status, promotion_failure_reason FROM batches WHERE batch_id = %s", (batch_id,))
    return cur.fetchone()


# --- standard immediate deduction ----------------------------------------------------
# --- server-timezone-vs-business-timezone fix -----------------------------------------
def test_immediate_deduction_accepts_lot_expiring_pacific_tomorrow_during_utc_evening(
    freeze_business_time, make_material, make_lot, make_recipe, db
):
    # Render runs UTC; Botaniks runs Pacific. Frozen instant: 2026-01-02 06:00 UTC =
    # 2026-01-01 22:00 PST — Pacific's evening of Jan 1, but UTC's calendar date has already
    # rolled to Jan 2. add_to_batches' per-lot expiry check must still accept a lot expiring on
    # Pacific's actual tomorrow (Jan 2): it was being rejected as already-expired a day early
    # whenever "now" came from the server's UTC clock instead of Botaniks' actual Pacific evening.
    from datetime import datetime
    from zoneinfo import ZoneInfo
    freeze_business_time(datetime(2026, 1, 2, 6, 0, 0, tzinfo=ZoneInfo("UTC")))

    mid = make_material("Matcha")
    lid = make_lot(mid, 100, expiry_date="2026-01-02")
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    # Before the fix this raised ValueError("Lot ID ... not found or is not active/expired.") —
    # the lot looked expired a day early because the check used the server's UTC "now".
    bid = add_to_batches("Latte", 1, batch_number="TZLOT1",
                         lot_selections={mid: [{"lot_id": lid, "qty": 10}]})
    assert isinstance(bid, int)
    assert _lot_qty(db, lid)[0] == 90


def test_standard_batch_deducts_from_lot(make_material, make_lot, make_recipe, db):
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])

    bid = add_to_batches("Latte", 2, batch_number="B1",
                         lot_selections={mid: [{"lot_id": lid, "qty": 20}]})
    assert isinstance(bid, int)
    assert _lot_qty(db, lid)[0] == 80
    mats = get_batch_materials(bid)
    assert len(mats) == 1 and mats[0][1] == 20  # quantity_used


def test_over_allocation_sum_mismatch_raises(make_material, make_lot, make_recipe):
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    # required is 20 but only 5 selected -> coverage check fails before any write
    with pytest.raises(ValueError):
        add_to_batches("Latte", 2, batch_number="B2",
                       lot_selections={mid: [{"lot_id": lid, "qty": 5}]})


def test_insufficient_lot_quantity_raises(make_material, make_lot, make_recipe, db):
    mid = make_material("Matcha")
    lid = make_lot(mid, 5)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 20}])
    # sum (20) matches required (20), but the lot only holds 5 -> per-lot check fails
    with pytest.raises(ValueError):
        add_to_batches("Latte", 1, batch_number="B3",
                       lot_selections={mid: [{"lot_id": lid, "qty": 20}]})
    # rolled back: lot untouched
    assert _lot_qty(db, lid)[0] == 5


def test_missing_lot_selection_for_material_raises(make_material, make_lot, make_recipe):
    mid = make_material("Matcha")
    make_lot(mid, 100)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    with pytest.raises(ValueError):
        add_to_batches("Latte", 1, batch_number="B4", lot_selections={})


def test_none_lot_selections_raises(make_material, make_lot, make_recipe):
    mid = make_material("Matcha")
    make_lot(mid, 100)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    with pytest.raises(ValueError):
        add_to_batches("Latte", 1, batch_number="B5", lot_selections=None)


def test_recipe_with_no_materials_raises(make_material):
    # no recipe rows at all -> get_recipe returns empty
    with pytest.raises(ValueError):
        add_to_batches("ghost-product", 1, batch_number="B6",
                       lot_selections={1: [{"lot_id": 1, "qty": 1}]})


def test_deduction_spans_two_lots_and_exhausts(make_material, make_lot, make_recipe, db):
    mid = make_material("Matcha")
    old = make_lot(mid, 20, received_date="2024-01-01", lot_number="OLD")
    new = make_lot(mid, 20, received_date="2024-03-01", lot_number="NEW")
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 30}])
    # take all of OLD (20) and 10 of NEW
    bid = add_to_batches("Latte", 1, batch_number="B7",
                         lot_selections={mid: [{"lot_id": old, "qty": 20}, {"lot_id": new, "qty": 10}]})
    assert isinstance(bid, int)
    old_qty, old_status = _lot_qty(db, old)
    assert old_qty == 0 and old_status == "inactive"  # exhausted
    assert _lot_qty(db, new)[0] == 10


def test_expired_lot_is_rejected(make_material, make_lot, make_recipe):
    mid = make_material("Matcha")
    lid = make_lot(mid, 100, expiry_date="2000-01-01")
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    with pytest.raises(ValueError):
        add_to_batches("Latte", 1, batch_number="B8",
                       lot_selections={mid: [{"lot_id": lid, "qty": 10}]})


def test_allow_negative_override_creates_batch(make_material, make_lot, make_recipe, db):
    mid = make_material("Matcha")
    lid = make_lot(mid, 5)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 20}])
    bid = add_to_batches("Latte", 1, batch_number="B9", allow_negative=True,
                         lot_selections={mid: [{"lot_id": lid, "qty": 20}]})
    # batch is created despite the lot not covering the recipe (coverage/per-lot checks bypassed)
    assert isinstance(bid, int)
    assert get_batch_materials(bid)[0][1] == 20  # full requested amount recorded as used
    # the deduction drives the lot to -15, which is below the exhaust threshold, so it is
    # zeroed and deactivated rather than left showing a negative quantity.
    qty, status = _lot_qty(db, lid)
    assert qty == 0 and status == "inactive"


# --- mix / housemade -----------------------------------------------------------------
def test_mix_batch_creates_housemade_material_and_lot(make_material, make_lot, make_recipe, db):
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    make_recipe("Component", [{"material_name": "Matcha", "quantity_needed": 10}],
                batch_type="mix", product_unit="g", product_dimension="mass")
    bid = add_to_batches("Component", 2, batch_number="C1", batch_type="mix",
                         lot_selections={mid: [{"lot_id": lid, "qty": 20}]})
    assert isinstance(bid, int)
    # input deducted
    assert _lot_qty(db, lid)[0] == 80
    # housemade material now exists and has a MIX-BATCH lot of 2 g (to_base(2,'g'))
    hm = get_raw_material("Component")
    assert hm is not None
    cur = db.cursor()
    db.execute(cur, """SELECT quantity FROM raw_material_lots
                       WHERE lot_number = %s""", ("MIX-BATCH-C1",))
    assert cur.fetchone()[0] == 2


def test_mix_lot_stored_full_precision(make_material, make_lot, make_recipe, db):
    # lot-selection-unit fix: the produced mix lot must be stored at FULL precision grams, not
    # round()ed to 4 dp. Rounding 1 lb (453.59237 g) to 4 dp gave 453.5924 g, which then read as
    # "insufficient" for a recipe needing the exact produced amount.
    from services import units
    mid = make_material("Matcha")
    lid = make_lot(mid, 1000)
    make_recipe("Component", [{"material_name": "Matcha", "quantity_needed": 10}],
                batch_type="mix", product_unit="lb", product_dimension="mass")
    add_to_batches("Component", 1, batch_number="MX1", batch_type="mix",
                   lot_selections={mid: [{"lot_id": lid, "qty": 10}]})
    cur = db.cursor()
    db.execute(cur, "SELECT quantity FROM raw_material_lots WHERE lot_number = %s", ("MIX-BATCH-MX1",))
    stored = cur.fetchone()[0]
    assert abs(float(stored) - float(units.to_base(1, "lb"))) < 1e-9  # 453.59237, not 453.5924


def test_exactly_enough_mix_covers_requirement(make_material, make_lot, make_recipe, db):
    # Regression for the reported bug: 176 lb of housemade mix on hand, a finished product needing
    # 16 lb x 11 batches = 176 lb. The stored grams (rounded to 4 dp, as the old mix path wrote
    # them) and the required grams differ by ~0.00002 g — below the 0.1 mg storage resolution but
    # ABOVE the old 1e-6 comparison tolerance, so the batch was wrongly refused. With the aligned
    # tolerance it now builds.
    from services import units
    def lb(x):
        return float(units.to_base(x, "lb"))
    mix = make_material("JasmineMix", is_housemade=True)
    # the mix lot as the OLD code stored it: full grams rounded to 4 dp
    lot = make_lot(mix, round(lb(176), 4))
    make_recipe("FinishedTea",
                [{"material_name": "JasmineMix", "quantity_needed": lb(16), "unit": "lb"}])
    bid = add_to_batches("FinishedTea", 11, batch_number="F1",
                         lot_selections={mix: [{"lot_id": lot, "qty": lb(16) * 11}]})
    assert isinstance(bid, int)
    # lot is driven to ~0 and exhausted (the tiny residual is below the exhaust threshold)
    qty, status = _lot_qty(db, lot)
    assert qty == 0 and status == "inactive"


# --- organic propagation through a mix -----------------------------------------------
def _material_flags(db, name):
    cur = db.cursor()
    db.execute(cur, "SELECT is_edible, is_organic FROM raw_materials WHERE name = %s", (name,))
    row = cur.fetchone()
    return (bool(row[0]), bool(row[1]))


def _mix_lot_for(db, material_name):
    cur = db.cursor()
    db.execute(cur, """SELECT l.lot_id FROM raw_material_lots l
                       JOIN raw_materials m ON l.material_id = m.material_id
                       WHERE m.name = %s""", (material_name,))
    return cur.fetchone()[0]


def test_organic_mix_material_is_edible_and_organic(make_material, make_lot, make_recipe, db):
    # (a) A Component made from an organic, edible input must yield a house-made material that is
    #     BOTH edible and organic, so it shows the Organic tag on the inventory page.
    mid = make_material("Matcha", is_organic=True, is_edible=True)
    lid = make_lot(mid, 100)
    make_recipe("OrgMix", [{"material_name": "Matcha", "quantity_needed": 10}],
                batch_type="mix", product_unit="g", product_dimension="mass")
    add_to_batches("OrgMix", 2, batch_number="OM1", batch_type="mix",
                   lot_selections={mid: [{"lot_id": lid, "qty": 20}]})
    is_edible, is_organic = _material_flags(db, "OrgMix")
    assert is_organic is True
    assert is_edible is True


def test_finished_batch_organic_when_only_input_is_organic_mix(make_material, make_lot, make_recipe, db):
    # (b) A finished batch whose only edible input is an organic mix must itself read as organic.
    mid = make_material("Matcha", is_organic=True, is_edible=True)
    lid = make_lot(mid, 1000)
    make_recipe("OrgMix", [{"material_name": "Matcha", "quantity_needed": 1}],
                batch_type="mix", product_unit="g", product_dimension="mass")
    add_to_batches("OrgMix", 100, batch_number="OM2", batch_type="mix",
                   lot_selections={mid: [{"lot_id": lid, "qty": 100}]})
    mix_lot = _mix_lot_for(db, "OrgMix")
    mix_mid = get_raw_material("OrgMix")[0]
    make_recipe("FinishedTea", [{"material_name": "OrgMix", "quantity_needed": 10, "unit": "g"}])
    fid = add_to_batches("FinishedTea", 1, batch_number="FT1",
                         lot_selections={mix_mid: [{"lot_id": mix_lot, "qty": 10}]})
    df, _ = get_batches(page=1, per_page=50)
    row = df[df["batch_id"] == fid].iloc[0]
    assert int(row["is_organic"]) == 1


def test_get_batches_reports_mix_remaining_and_consumed_count(make_material, make_lot, make_recipe, db):
    # mix-usage feature: after a finished batch draws from a Component's output lot, get_batches
    # must report the lot's remaining quantity (base units) and how many batches consumed it.
    mid = make_material("Matcha", is_organic=True, is_edible=True)
    lid = make_lot(mid, 1000)
    make_recipe("OrgMix", [{"material_name": "Matcha", "quantity_needed": 1}],
                batch_type="mix", product_unit="g", product_dimension="mass")
    mix_bid = add_to_batches("OrgMix", 100, batch_number="OM3", batch_type="mix",
                             lot_selections={mid: [{"lot_id": lid, "qty": 100}]})
    mix_lot = _mix_lot_for(db, "OrgMix")
    mix_mid = get_raw_material("OrgMix")[0]
    make_recipe("FinishedTea", [{"material_name": "OrgMix", "quantity_needed": 10, "unit": "g"}])
    add_to_batches("FinishedTea", 1, batch_number="FT2",
                   lot_selections={mix_mid: [{"lot_id": mix_lot, "qty": 10}]})
    df, _ = get_batches(page=1, per_page=50)
    mix_row = df[df["batch_id"] == mix_bid].iloc[0]
    assert mix_row["mix_remaining"] == 90        # 100 g produced - 10 g consumed
    assert mix_row["mix_consumed_count"] == 1


# --- renamed-recipe fix: product_unit must survive a recipe rename -------------------
# get_batches recovers a batch's display unit by text-matching batches.product_name against
# recipes.product_name. Renaming a recipe silently orphaned 43 production mix batches, so that
# lookup returned NULL -> app.py skipped its base->display conversion -> a Component row printed
# its output lot in raw grams ("4000") instead of "4 kg". The fallback resolves the unit through
# foreign keys (mix_lot_id -> lot.material_id -> raw_materials.unit) instead of a name.
def _rename_recipe(db, old_name, new_name):
    cur = db.cursor()
    db.execute(cur, "UPDATE recipes SET product_name = %s WHERE product_name = %s",
               (new_name, old_name))
    db.commit()


def _ready_row(batch_id):
    df, _ = get_batches(page=1, per_page=50)
    return df[df["batch_id"] == batch_id].iloc[0]


def test_mix_product_unit_survives_recipe_rename(make_material, make_lot, make_recipe, db):
    # Mirrors production exactly: a kg-denominated Component of 4 kg stores a 4000 g output lot.
    mid = make_material("Matcha")
    lid = make_lot(mid, 10000)
    make_recipe("KgMix", [{"material_name": "Matcha", "quantity_needed": 1000}],
                batch_type="mix", product_unit="kg", product_dimension="mass")
    bid = add_to_batches("KgMix", 4, batch_number="KG1", batch_type="mix",
                         lot_selections={mid: [{"lot_id": lid, "qty": 4000}]})

    # before the rename the recipe lookup supplies the unit
    row = _ready_row(bid)
    assert row["product_unit"] == "kg"
    assert row["mix_remaining"] == 4000      # base units (grams)

    # the production incident: recipe renamed, batch.product_name left pointing at the old name
    _rename_recipe(db, "KgMix", "KgMix V1")

    # regression: NULL here is what printed "4000" instead of "4 kg"
    row = _ready_row(bid)
    assert row["product_unit"] == "kg", "unit must fall back to the house-made material"
    assert row["mix_remaining"] == 4000

    # and the conversion the route performs (app.py ~1271) now yields the right display value
    from services import units
    assert float(units.from_base(row["mix_remaining"], row["product_unit"])) == 4.0


def test_renamed_recipe_on_non_mix_batch_yields_null_unit_without_error(
    make_material, make_lot, make_recipe, db
):
    # A standard/finished batch has mix_lot_id = NULL, so the fallback subquery returns NULL.
    # That must degrade quietly to None, not raise — the route already guards on a falsy unit.
    mid = make_material("Matcha")
    lid = make_lot(mid, 1000)
    make_recipe("PlainTea", [{"material_name": "Matcha", "quantity_needed": 10}])
    bid = add_to_batches("PlainTea", 1, batch_number="PT1",
                         lot_selections={mid: [{"lot_id": lid, "qty": 10}]})
    assert _ready_row(bid)["product_unit"] == "g"

    _rename_recipe(db, "PlainTea", "PlainTea V1")
    assert _ready_row(bid)["product_unit"] is None


def test_recipe_unit_keeps_precedence_over_housemade_fallback(make_material, make_lot, make_recipe, db):
    # The fallback must not change behaviour for batches whose recipe still matches: COALESCE
    # keeps the recipe's product_unit first even though a house-made unit is also reachable.
    mid = make_material("Matcha")
    lid = make_lot(mid, 10000)
    make_recipe("LbMix", [{"material_name": "Matcha", "quantity_needed": 100}],
                batch_type="mix", product_unit="lb", product_dimension="mass")
    bid = add_to_batches("LbMix", 2, batch_number="LB1", batch_type="mix",
                         lot_selections={mid: [{"lot_id": lid, "qty": 200}]})
    assert _ready_row(bid)["product_unit"] == "lb"


def test_finished_batch_cost_includes_component_cost(make_material, make_lot, make_recipe, db):
    # cost-of-component: a finished batch consuming a Component (mix) must include the mix's own
    # production cost. The mix output lot is given cost_per_unit = (input cost) / produced qty, which
    # the finished batch records into batch_materials and rolls into batch_cost. Previously the mix
    # lot had a NULL cost, so the component contributed $0.
    mid = make_material("Matcha")
    lid = make_lot(mid, 1000, cost_per_unit=3)
    make_recipe("Mix", [{"material_name": "Matcha", "quantity_needed": 1}],
                batch_type="mix", product_unit="g", product_dimension="mass")
    # 100 units consumes 100 g @ $3 = $300 to produce 100 g of mix -> $3/g on the output lot
    add_to_batches("Mix", 100, batch_number="MC1", batch_type="mix",
                   lot_selections={mid: [{"lot_id": lid, "qty": 100}]})
    mix_lot = _mix_lot_for(db, "Mix")
    mix_mid = get_raw_material("Mix")[0]
    make_recipe("FinishedTea", [{"material_name": "Mix", "quantity_needed": 10, "unit": "g"}])
    fid = add_to_batches("FinishedTea", 1, batch_number="FC1",
                         lot_selections={mix_mid: [{"lot_id": mix_lot, "qty": 10}]})
    df, _ = get_batches(page=1, per_page=50)
    row = df[df["batch_id"] == fid].iloc[0]
    assert row["batch_cost"] == pytest.approx(10 * 3)  # 10 g of mix @ $3/g = $30


# --- planned-deduction-mode feature: immediate vs deferred ---------------------------
# A Planned batch now chooses WHEN its materials leave stock. 'immediate' (the default) deducts at
# creation and writes batch_materials rows straight away; 'deferred' reserves lots in the
# planned_lot_selections JSON and deducts them at promotion.

def _deduction_mode(db, batch_id):
    cur = db.cursor()
    db.execute(cur, "SELECT deduction_mode FROM batches WHERE batch_id = %s", (batch_id,))
    return cur.fetchone()[0]


def test_planned_immediate_deducts_at_creation(make_material, make_lot, make_recipe, db):
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    bid = add_to_batches("Latte", 2, batch_number="PI1", planned_completion_date=PAST,
                         batch_type="finished", deduction_mode="immediate",
                         lot_selections={mid: [{"lot_id": lid, "qty": 20}]})
    # stock moved now, and the consumption is recorded — even though the batch is still Planned
    assert _lot_qty(db, lid)[0] == 80
    assert _batch_status(db, bid)[0] == "Planned"
    assert _deduction_mode(db, bid) == "immediate"
    assert len(get_batch_materials(bid)) == 1

    # promotion has nothing left to deduct and cannot fail
    promote_planned_batches()
    assert _lot_qty(db, lid)[0] == 80
    status, reason = _batch_status(db, bid)
    assert status == "Ready"
    assert reason is None


def test_planned_immediate_is_the_default(make_material, make_lot, make_recipe, db):
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    bid = add_to_batches("Latte", 1, batch_number="PI2", planned_completion_date="2999-01-01",
                         batch_type="finished",
                         lot_selections={mid: [{"lot_id": lid, "qty": 10}]})
    assert _deduction_mode(db, bid) == "immediate"
    assert _lot_qty(db, lid)[0] == 90


def test_planned_immediate_mix_defers_only_its_output_lot(make_material, make_lot, make_recipe, db):
    # A Planned Component consumes its inputs today but does not EXIST as stock until its
    # completion date — the house-made output lot is born at promotion in either mode.
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    make_recipe("Component", [{"material_name": "Matcha", "quantity_needed": 10}],
                batch_type="mix", product_unit="g", product_dimension="mass")
    bid = add_to_batches("Component", 2, batch_number="PM1", planned_completion_date=PAST,
                         batch_type="mix", deduction_mode="immediate",
                         lot_selections={mid: [{"lot_id": lid, "qty": 20}]})
    assert _lot_qty(db, lid)[0] == 80  # inputs consumed now
    cur = db.cursor()
    db.execute(cur, "SELECT quantity FROM raw_material_lots WHERE lot_number = %s", ("MIX-BATCH-PM1",))
    assert cur.fetchone() is None  # ...but no output lot yet

    promote_planned_batches()
    assert _batch_status(db, bid)[0] == "Ready"
    assert _lot_qty(db, lid)[0] == 80  # not double-deducted
    db.execute(cur, "SELECT quantity FROM raw_material_lots WHERE lot_number = %s", ("MIX-BATCH-PM1",))
    assert cur.fetchone()[0] == 2


# --- planned-deduction-mode feature: reading + editing the reserved lots --------------
def test_get_planned_lot_selections_returns_reserved_picks(make_material, make_lot, make_recipe):
    mid = make_material("Matcha")
    lid = make_lot(mid, 100, lot_number="RES1")
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    bid = add_to_batches("Latte", 2, batch_number="PS1", planned_completion_date="2999-01-01",
                         batch_type="finished", deduction_mode="deferred",
                         lot_selections={mid: [{"lot_id": lid, "qty": 20}]})
    # nothing was deducted, so the drawer's normal source is empty...
    assert get_batch_materials(bid) == []
    # ...and the reserved picks come from the batch itself, in get_batch_materials' shape
    rows = get_planned_lot_selections(bid)
    assert len(rows) == 1
    name, qty, unit, lot_id, lot_number, material_id, _cost, lot_status = rows[0]
    assert (name, qty, lot_id, lot_number, material_id) == ("Matcha", 20, lid, "RES1", mid)
    assert lot_status == "active"


def test_get_planned_lot_selections_flags_unavailable_lot(make_material, make_lot, make_recipe, db):
    # A reserved lot can be used up before the planned date. Surface it rather than dropping the row.
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    bid = add_to_batches("Latte", 1, batch_number="PS2", planned_completion_date="2999-01-01",
                         batch_type="finished", deduction_mode="deferred",
                         lot_selections={mid: [{"lot_id": lid, "qty": 10}]})
    cur = db.cursor()
    db.execute(cur, "UPDATE raw_material_lots SET status = 'exhausted' WHERE lot_id = %s", (lid,))
    db.commit()
    assert get_planned_lot_selections(bid)[0][7] == "unavailable"


def test_update_planned_lot_selections_changes_what_promotion_deducts(make_material, make_lot, make_recipe, db):
    mid = make_material("Matcha")
    first = make_lot(mid, 100, received_date="2024-01-01")
    second = make_lot(mid, 100, received_date="2024-06-01")
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    bid = add_to_batches("Latte", 1, batch_number="PE1", planned_completion_date=PAST,
                         batch_type="finished", deduction_mode="deferred",
                         lot_selections={mid: [{"lot_id": first, "qty": 10}]})

    assert update_planned_lot_selections(bid, {mid: [{"lot_id": second, "qty": 10}]}) is True
    promote_planned_batches()
    assert _lot_qty(db, first)[0] == 100   # original pick untouched
    assert _lot_qty(db, second)[0] == 90   # the re-picked lot is what got deducted


def test_update_planned_lot_selections_rejects_unavailable_lot(make_material, make_lot, make_recipe, db):
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    dead = make_lot(mid, 100)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    bid = add_to_batches("Latte", 1, batch_number="PE2", planned_completion_date="2999-01-01",
                         batch_type="finished", deduction_mode="deferred",
                         lot_selections={mid: [{"lot_id": lid, "qty": 10}]})
    cur = db.cursor()
    db.execute(cur, "UPDATE raw_material_lots SET status = 'exhausted' WHERE lot_id = %s", (dead,))
    db.commit()
    with pytest.raises(ValueError):
        update_planned_lot_selections(bid, {mid: [{"lot_id": dead, "qty": 10}]})
    # the rejected write left the original picks intact
    assert get_planned_lot_selections(bid)[0][3] == lid


def test_update_planned_lot_selections_rejects_already_deducted_batch(make_material, make_lot, make_recipe):
    # An immediate batch's consumption lives in batch_materials; rewriting the JSON would be a lie.
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    bid = add_to_batches("Latte", 1, batch_number="PE3", planned_completion_date="2999-01-01",
                         batch_type="finished", deduction_mode="immediate",
                         lot_selections={mid: [{"lot_id": lid, "qty": 10}]})
    with pytest.raises(ValueError):
        update_planned_lot_selections(bid, {mid: [{"lot_id": lid, "qty": 10}]})


def test_clear_planned_lot_selections_falls_back_to_fifo(make_material, make_lot, make_recipe, db):
    mid = make_material("Matcha")
    old = make_lot(mid, 100, received_date="2024-01-01")
    new = make_lot(mid, 100, received_date="2024-06-01")
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    bid = add_to_batches("Latte", 1, batch_number="PC1", planned_completion_date=PAST,
                         batch_type="finished", deduction_mode="deferred",
                         lot_selections={mid: [{"lot_id": new, "qty": 10}]})
    clear_planned_lot_selections(bid)
    assert get_planned_lot_selections(bid) == []

    promote_planned_batches()
    assert _lot_qty(db, old)[0] == 90   # FIFO took the oldest lot, not the cleared pick
    assert _lot_qty(db, new)[0] == 100


# --- promotion race: a batch may only ever be promoted once --------------------------
# Bug: promote_planned_batches SELECTed the overdue batches and then deducted, with an
# unconditional status flip. sqlite3 opens a transaction on the first write, never on a SELECT, so
# two concurrent promotes (from /batches and /manage-batches) both read the batch as Planned and
# each deducted the full amount — doubled stock deduction, duplicate batch_materials rows, and two
# house-made lots for a mix. _claim_batch's `AND status = 'Planned'` now decides a single winner.
#
# The race is reproduced deterministically, without threads, by replaying a STALE overdue snapshot:
# exactly what the losing racer holds in memory after the winner has committed.

def _capture_overdue():
    """The rows promote_planned_batches would act on right now (the racer's snapshot)."""
    from database import get_db_connection
    db = get_db_connection()
    cur = db.cursor()
    try:
        return batches_mod._fetch_overdue(db, cur, "2999-12-31")
    finally:
        db.close()


def _replay_stale(monkeypatch, stale):
    """Make the next promote act on `stale` instead of re-reading the DB."""
    monkeypatch.setattr(batches_mod, "_fetch_overdue", lambda db, cur, now: stale)


def _bm_rows(db, batch_id):
    cur = db.cursor()
    db.execute(cur, "SELECT material_id, lot_id, quantity_used FROM batch_materials WHERE batch_id = %s", (batch_id,))
    return cur.fetchall()


def test_stale_snapshot_cannot_promote_a_batch_twice(monkeypatch, make_material, make_lot, make_recipe, db):
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    bid = add_to_batches("Latte", 2, batch_number="RACE1", planned_completion_date=PAST,
                         batch_type="finished", deduction_mode="deferred",
                         lot_selections={mid: [{"lot_id": lid, "qty": 20}]})

    stale = _capture_overdue()          # what the loser read before the winner committed
    promote_planned_batches()           # the winner
    assert _lot_qty(db, lid)[0] == 80
    assert len(_bm_rows(db, bid)) == 1

    _replay_stale(monkeypatch, stale)   # the loser acts on its stale snapshot
    promote_planned_batches()

    assert _lot_qty(db, lid)[0] == 80             # NOT 60 — no second deduction
    assert len(_bm_rows(db, bid)) == 1            # NOT 2 — no duplicate material row
    assert _batch_status(db, bid)[0] == "Ready"


def test_stale_snapshot_cannot_duplicate_a_mix_output_lot(monkeypatch, make_material, make_lot, make_recipe, db):
    # The reported symptom: two MIX-BATCH-<batch_number> lots, i.e. phantom stock.
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    make_recipe("Component", [{"material_name": "Matcha", "quantity_needed": 10}],
                batch_type="mix", product_unit="g", product_dimension="mass")
    add_to_batches("Component", 2, batch_number="RACE2", planned_completion_date=PAST,
                   batch_type="mix", deduction_mode="deferred",
                   lot_selections={mid: [{"lot_id": lid, "qty": 20}]})

    stale = _capture_overdue()
    promote_planned_batches()
    _replay_stale(monkeypatch, stale)
    promote_planned_batches()

    cur = db.cursor()
    db.execute(cur, "SELECT COUNT(*) FROM raw_material_lots WHERE lot_number = %s", ("MIX-BATCH-RACE2",))
    assert cur.fetchone()[0] == 1
    assert _lot_qty(db, lid)[0] == 80  # inputs deducted once


def test_stale_snapshot_cannot_duplicate_an_immediate_mix_output_lot(monkeypatch, make_material, make_lot, make_recipe, db):
    # The immediate branch creates the house-made lot at promotion too, so it raced identically.
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    make_recipe("Component", [{"material_name": "Matcha", "quantity_needed": 10}],
                batch_type="mix", product_unit="g", product_dimension="mass")
    add_to_batches("Component", 2, batch_number="RACE3", planned_completion_date=PAST,
                   batch_type="mix", deduction_mode="immediate",
                   lot_selections={mid: [{"lot_id": lid, "qty": 20}]})

    stale = _capture_overdue()
    promote_planned_batches()
    _replay_stale(monkeypatch, stale)
    promote_planned_batches()

    cur = db.cursor()
    db.execute(cur, "SELECT COUNT(*) FROM raw_material_lots WHERE lot_number = %s", ("MIX-BATCH-RACE3",))
    assert cur.fetchone()[0] == 1
    assert _lot_qty(db, lid)[0] == 80  # deducted once, at creation


def test_promotion_is_idempotent_when_called_twice(make_material, make_lot, make_recipe, db):
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    bid = add_to_batches("Latte", 1, batch_number="IDEM", planned_completion_date=PAST,
                         batch_type="finished", deduction_mode="deferred",
                         lot_selections={mid: [{"lot_id": lid, "qty": 10}]})
    promote_planned_batches()
    promote_planned_batches()
    assert _lot_qty(db, lid)[0] == 90
    assert len(_bm_rows(db, bid)) == 1


def test_failed_promotion_rolls_back_its_claim(make_material, make_lot, make_recipe, db):
    # The claim flips status to Ready *before* the stock check. If coverage fails, that claim must be
    # rolled back or the batch would sit Ready having deducted nothing.
    mid = make_material("Matcha")
    lid = make_lot(mid, 5)  # short for a need of 10
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    bid = add_to_batches("Latte", 1, batch_number="FAIL1", planned_completion_date=PAST,
                         batch_type="finished", deduction_mode="deferred", lot_selections=None)
    promote_planned_batches()

    status, reason = _batch_status(db, bid)
    assert status == "Planned"
    assert reason
    assert _lot_qty(db, lid)[0] == 5      # untouched
    assert _bm_rows(db, bid) == []


def test_one_failing_batch_does_not_block_a_healthy_one(make_material, make_lot, make_recipe, db):
    # Each batch gets its own transaction, so a rollback for the short one must not discard the
    # deduction already committed for the good one.
    short = make_material("Short")
    make_lot(short, 5)
    make_recipe("ShortDrink", [{"material_name": "Short", "quantity_needed": 10}])
    bad = add_to_batches("ShortDrink", 1, batch_number="MIX_BAD", planned_completion_date=PAST,
                         batch_type="finished", deduction_mode="deferred", lot_selections=None)

    ok = make_material("Plenty")
    ok_lot = make_lot(ok, 100)
    make_recipe("GoodDrink", [{"material_name": "Plenty", "quantity_needed": 10}])
    good = add_to_batches("GoodDrink", 1, batch_number="MIX_GOOD", planned_completion_date=PAST,
                          batch_type="finished", deduction_mode="deferred",
                          lot_selections={ok: [{"lot_id": ok_lot, "qty": 10}]})

    promote_planned_batches()

    assert _batch_status(db, bad)[0] == "Planned"
    assert _batch_status(db, good)[0] == "Ready"
    assert _lot_qty(db, ok_lot)[0] == 90


def test_promotion_writes_an_audit_row(make_material, make_lot, make_recipe, db):
    # log_action opens a second connection; while promote held SQLite's write lock its INSERT failed
    # with "database is locked" and audit.py swallowed it, so promotions were never audited.
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    add_to_batches("Latte", 1, batch_number="AUD1", planned_completion_date=PAST,
                   batch_type="finished", deduction_mode="deferred",
                   lot_selections={mid: [{"lot_id": lid, "qty": 10}]})
    promote_planned_batches()

    cur = db.cursor()
    db.execute(cur, "SELECT COUNT(*) FROM audit_log WHERE action = %s", ("planned_batch_promoted",))
    assert cur.fetchone()[0] == 1


# --- planned promotion: stored lots --------------------------------------------------
def test_planned_finished_defers_then_promotes_stored_lots(make_material, make_lot, make_recipe, db):
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    bid = add_to_batches("Latte", 2, batch_number="P1", planned_completion_date=PAST,
                         batch_type="finished", deduction_mode="deferred",
                         lot_selections={mid: [{"lot_id": lid, "qty": 20}]})
    assert isinstance(bid, int)
    # deferred: nothing deducted yet, batch is Planned
    assert _lot_qty(db, lid)[0] == 100
    assert _batch_status(db, bid)[0] == "Planned"

    promote_planned_batches()
    assert _lot_qty(db, lid)[0] == 80
    assert _batch_status(db, bid)[0] == "Ready"


# --- planned promotion: FIFO fallback ------------------------------------------------
def test_planned_promotes_via_fifo_when_no_stored_lots(make_material, make_lot, make_recipe, db):
    mid = make_material("Matcha")
    old = make_lot(mid, 20, received_date="2024-01-01", lot_number="OLD")
    new = make_lot(mid, 20, received_date="2024-03-01", lot_number="NEW")
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 30}])
    bid = add_to_batches("Latte", 1, batch_number="P2", planned_completion_date=PAST,
                         batch_type="finished", deduction_mode="deferred", lot_selections=None)
    assert isinstance(bid, int)
    promote_planned_batches()
    # FIFO drained OLD first (oldest), then 10 of NEW
    assert _lot_qty(db, old)[0] == 0
    assert _lot_qty(db, new)[0] == 10
    assert _batch_status(db, bid)[0] == "Ready"


def test_planned_promotion_failure_keeps_planned_with_reason(make_material, make_lot, make_recipe, db):
    mid = make_material("Matcha")
    make_lot(mid, 5)  # not enough for need of 10
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    bid = add_to_batches("Latte", 1, batch_number="P3", planned_completion_date=PAST,
                         batch_type="finished", deduction_mode="deferred", lot_selections=None)
    promote_planned_batches()
    status, reason = _batch_status(db, bid)
    assert status == "Planned"
    assert reason and "Insufficient" in reason


def test_planned_promotion_failure_reports_insufficient_not_missing_selection(
    make_material, make_lot, make_recipe, db
):
    # Regression: a planned batch can store lot selections that OMIT a material the
    # create-batch UI couldn't auto-fill (no/insufficient active lots at creation). At
    # promotion that material has no stored entry, so it must fall back to FIFO and report
    # the REAL "Insufficient lot stock" reason — not the misleading "No stored lot selection".
    mid = make_material("Matcha")
    make_lot(mid, 5)  # only 5 g on hand, recipe needs 10
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    # non-None but empty: stored_lot_selections is a dict that doesn't contain `mid`
    bid = add_to_batches("Latte", 1, batch_number="P4", planned_completion_date=PAST,
                         batch_type="finished", deduction_mode="deferred", lot_selections={})
    promote_planned_batches()
    status, reason = _batch_status(db, bid)
    assert status == "Planned"
    assert reason
    assert "Insufficient" in reason
    assert "no stored lot selection" not in reason.lower()


# --- manual promotion ("Mark Ready" before the planned date) -------------------------
# Bug: the Mark Ready button only flipped status, so a deferred batch became Ready without ever
# deducting its lots or writing batch_materials (drawer: "No materials recorded").
FUTURE = "2999-01-01"  # not overdue, so only a manual promotion can move it


def _mix_lot_id(db, batch_id):
    cur = db.cursor()
    db.execute(cur, "SELECT mix_lot_id FROM batches WHERE batch_id = %s", (batch_id,))
    return cur.fetchone()[0]


def test_manual_promote_deferred_fifo_deducts_and_records(make_material, make_lot, make_recipe, db):
    mid = make_material("Matcha")
    old = make_lot(mid, 20, received_date="2024-01-01", lot_number="OLD")
    new = make_lot(mid, 20, received_date="2024-03-01", lot_number="NEW")
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 30}])
    bid = add_to_batches("Latte", 1, batch_number="MP1", planned_completion_date=FUTURE,
                         batch_type="finished", deduction_mode="deferred", lot_selections=None)

    ok, reason = promote_batch_now(bid)

    assert ok is True and reason is None
    assert _batch_status(db, bid)[0] == "Ready"
    assert _lot_qty(db, old)[0] == 0
    assert _lot_qty(db, new)[0] == 10
    assert len(_bm_rows(db, bid)) == 2
    assert get_batch_materials(bid)  # what the drawer reads


def test_manual_promote_deferred_uses_stored_lot_picks(make_material, make_lot, make_recipe, db):
    mid = make_material("Matcha")
    old = make_lot(mid, 50, received_date="2024-01-01")
    picked = make_lot(mid, 50, received_date="2024-03-01")
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    bid = add_to_batches("Latte", 1, batch_number="MP2", planned_completion_date=FUTURE,
                         batch_type="finished", deduction_mode="deferred",
                         lot_selections={mid: [{"lot_id": picked, "qty": 10}]})

    ok, _ = promote_batch_now(bid)

    assert ok is True
    assert _lot_qty(db, picked)[0] == 40
    assert _lot_qty(db, old)[0] == 50  # FIFO must not override the user's pick


def test_manual_promote_deferred_mix_creates_output_lot(make_material, make_lot, make_recipe, db):
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    make_recipe("Blend", [{"material_name": "Matcha", "quantity_needed": 10}],
                batch_type="mix", product_unit="g", product_dimension="mass")
    bid = add_to_batches("Blend", 2, batch_number="MP3", planned_completion_date=FUTURE,
                         batch_type="mix", deduction_mode="deferred", lot_selections=None)
    assert _mix_lot_id(db, bid) is None

    ok, _ = promote_batch_now(bid)

    assert ok is True
    assert _lot_qty(db, lid)[0] == 80
    assert _mix_lot_id(db, bid) is not None


def test_manual_promote_insufficient_stock_stays_planned(make_material, make_lot, make_recipe, db):
    mid = make_material("Matcha")
    lid = make_lot(mid, 5)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    bid = add_to_batches("Latte", 1, batch_number="MP4", planned_completion_date=FUTURE,
                         batch_type="finished", deduction_mode="deferred", lot_selections=None)

    ok, reason = promote_batch_now(bid)

    assert ok is False and reason
    status, stored_reason = _batch_status(db, bid)
    assert status == "Planned"
    assert stored_reason
    assert _lot_qty(db, lid)[0] == 5
    assert _bm_rows(db, bid) == []


def test_manual_promote_immediate_mode_does_not_deduct_twice(make_material, make_lot, make_recipe, db):
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    bid = add_to_batches("Latte", 1, batch_number="MP5", planned_completion_date=FUTURE,
                         batch_type="finished", deduction_mode="immediate",
                         lot_selections={mid: [{"lot_id": lid, "qty": 10}]})
    assert _lot_qty(db, lid)[0] == 90  # deducted at creation

    ok, _ = promote_batch_now(bid)

    assert ok is True
    assert _batch_status(db, bid)[0] == "Ready"
    assert _lot_qty(db, lid)[0] == 90
    assert len(_bm_rows(db, bid)) == 1


def test_manual_promote_twice_deducts_once(make_material, make_lot, make_recipe, db):
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    bid = add_to_batches("Latte", 1, batch_number="MP6", planned_completion_date=FUTURE,
                         batch_type="finished", deduction_mode="deferred", lot_selections=None)

    first, _ = promote_batch_now(bid)
    second, second_reason = promote_batch_now(bid)  # double-click: batch is no longer Planned

    assert first is True
    assert second is False and second_reason
    assert _lot_qty(db, lid)[0] == 90
    assert len(_bm_rows(db, bid)) == 1


def test_manual_promote_writes_an_audit_row(make_material, make_lot, make_recipe, db):
    mid = make_material("Matcha")
    make_lot(mid, 100)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    bid = add_to_batches("Latte", 1, batch_number="MP7", planned_completion_date=FUTURE,
                         batch_type="finished", deduction_mode="deferred", lot_selections=None)
    promote_batch_now(bid)

    cur = db.cursor()
    db.execute(cur, "SELECT COUNT(*) FROM audit_log WHERE action = %s", ("planned_batch_promoted",))
    assert cur.fetchone()[0] == 1


# --- delete with reallocation --------------------------------------------------------
def test_delete_batch_reallocates_stock(make_material, make_lot, make_recipe, db):
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    bid = add_to_batches("Latte", 2, batch_number="D1",
                         lot_selections={mid: [{"lot_id": lid, "qty": 20}]})
    assert _lot_qty(db, lid)[0] == 80

    realloc = get_batch_materials_for_reallocation(bid)
    assert delete_batch(bid, materials_to_reallocate=realloc, reallocate=True) is True
    # stock returned to the original lot
    assert _lot_qty(db, lid)[0] == 100
    assert get_batch_by_id(bid) is None


def test_delete_batch_reallocation_reactivates_exhausted_lot(make_material, make_lot, make_recipe, db):
    # A batch that fully depletes a lot leaves it quantity=0, status='inactive'.
    # Deleting with reallocation must restore the quantity AND reactivate the lot,
    # otherwise the stock counts but the lot is unusable in dropdowns/deduction.
    mid = make_material("Matcha")
    lid = make_lot(mid, 20)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 20}])
    bid = add_to_batches("Latte", 1, batch_number="D3",
                         lot_selections={mid: [{"lot_id": lid, "qty": 20}]})
    assert _lot_qty(db, lid) == (0, "inactive")  # exhausted

    realloc = get_batch_materials_for_reallocation(bid)
    assert delete_batch(bid, materials_to_reallocate=realloc, reallocate=True) is True
    assert _lot_qty(db, lid) == (20, "active")  # restored and reactivated


def test_delete_batch_without_reallocation_leaves_stock_deducted(make_material, make_lot, make_recipe, db):
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    bid = add_to_batches("Latte", 2, batch_number="D2",
                         lot_selections={mid: [{"lot_id": lid, "qty": 20}]})
    assert delete_batch(bid) is True
    assert _lot_qty(db, lid)[0] == 80  # NOT restored
    assert get_batch_by_id(bid) is None


# --- adjust / check batch materials --------------------------------------------------
def test_adjust_batch_material_increase_then_decrease(make_material, make_lot, make_recipe, db):
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    bid = add_to_batches("Latte", 2, batch_number="A1",
                         lot_selections={mid: [{"lot_id": lid, "qty": 20}]})
    assert _lot_qty(db, lid)[0] == 80

    # increase usage to 25 -> deduct 5 more
    assert adjust_batch_material(bid, {mid: 25}) is True
    assert _lot_qty(db, lid)[0] == 75

    # decrease usage to 10 -> return 15
    assert adjust_batch_material(bid, {mid: 10}) is True
    assert _lot_qty(db, lid)[0] == 90


def test_adjust_batch_material_insufficient_stock_aborts(make_material, make_lot, make_recipe, db):
    mid = make_material("Matcha")
    lid = make_lot(mid, 20)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    bid = add_to_batches("Latte", 1, batch_number="A2",
                         lot_selections={mid: [{"lot_id": lid, "qty": 10}]})
    # lot now has 10 left; asking to use 1000 would need 990 more -> abort, no change
    assert adjust_batch_material(bid, {mid: 1000}) is None
    assert _lot_qty(db, lid)[0] == 10


def test_check_batch_materials_stock_dry_run(make_material, make_lot, make_recipe):
    mid = make_material("Matcha")
    lid = make_lot(mid, 20)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    bid = add_to_batches("Latte", 1, batch_number="A3",
                         lot_selections={mid: [{"lot_id": lid, "qty": 10}]})
    # 10 left in lot: bumping usage to 15 (needs 5 more) is fine; to 1000 is not
    assert check_batch_materials_stock(bid, {mid: 15}) is True
    assert check_batch_materials_stock(bid, {mid: 1000}) is False


# --- list / read queries (the per-page rendering paths) ------------------------------
def test_get_batches_exposes_derived_columns(make_material, make_lot, make_recipe):
    mid = make_material("Matcha", is_organic=True, is_edible=True)
    lid = make_lot(mid, 100, cost_per_unit=3)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}], product_unit="g")
    bid = add_to_batches("Latte", 2, batch_number="L1",
                         lot_selections={mid: [{"lot_id": lid, "qty": 20}]})
    df, total = get_batches(page=1, per_page=50)
    row = df[df["batch_id"] == bid].iloc[0]
    assert total == 1
    assert row["batch_cost"] == pytest.approx(20 * 3)   # quantity_used * cost_per_unit
    assert row["remaining"] == 2                          # produced, nothing shipped yet
    assert row["product_unit"] == "g"
    assert int(row["is_organic"]) == 1                    # only organic edible material used


def test_get_batches_planned_includes_failure_reason(make_material, make_lot, make_recipe):
    mid = make_material("Matcha")
    make_lot(mid, 5)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    bid = add_to_batches("Latte", 1, batch_number="L2", planned_completion_date=PAST,
                         batch_type="finished", deduction_mode="deferred", lot_selections=None)
    promote_planned_batches()  # fails -> stays planned with a reason
    df, total = get_batches_planned(page=1, per_page=50)
    row = df[df["batch_id"] == bid].iloc[0]
    assert total == 1
    assert row["promotion_failure_reason"]


def test_get_all_batches_with_id_lists_every_status(make_material, make_lot, make_recipe):
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}])
    add_to_batches("Latte", 1, batch_number="R1",
                   lot_selections={mid: [{"lot_id": lid, "qty": 10}]})
    add_to_batches("Latte", 1, batch_number="PL1", planned_completion_date="2999-01-01",
                   batch_type="finished", deduction_mode="deferred", lot_selections=None)
    df, total = get_all_batches_with_id(page=1, per_page=50)
    assert total == 2
    assert set(df["batch_number"]) == {"R1", "PL1"}


# --- sort feature: Ready / Planned / Shipped order by their date column ---------------
# Dates are stored as YYYY-MM-DD TEXT, so string ordering is chronological. Rows are
# inserted directly here so we control the date columns without driving the full flow.
def _insert_ready(db, name, date_completed):
    cur = db.cursor()
    db.execute(cur, """INSERT INTO batches (product_name, quantity, status, batch_number, batch_type, date_completed)
                       VALUES (%s, 10, 'Ready', %s, 'standard', %s)""", (name, name, date_completed))
    db.commit()


def _insert_planned(db, name, planned_completion_date):
    cur = db.cursor()
    db.execute(cur, """INSERT INTO batches (product_name, quantity, status, batch_number, batch_type, planned_completion_date)
                       VALUES (%s, 10, 'Planned', %s, 'standard', %s)""", (name, name, planned_completion_date))
    db.commit()


def _insert_shipped(db, name, date_shipped):
    cur = db.cursor()
    db.execute(cur, """INSERT INTO batches (product_name, quantity, status, batch_number, batch_type)
                       VALUES (%s, 10, 'Shipped', %s, 'standard')""", (name, name))
    bid = db.get_last_insert_id(cur)
    db.execute(cur, """INSERT INTO shipments (shipment_number, date_shipped, destination)
                       VALUES (%s, %s, 'Dest')""", (f"S-{name}", date_shipped))
    sid = db.get_last_insert_id(cur)
    db.execute(cur, """INSERT INTO shipment_batches (shipment_id, batch_id, quantity)
                       VALUES (%s, %s, 10)""", (sid, bid))
    db.commit()


def test_get_batches_sorts_by_date_completed(db):
    _insert_ready(db, "A", "2024-01-01")
    _insert_ready(db, "B", "2024-03-01")
    _insert_ready(db, "C", "2024-02-01")
    # default is newest-first (desc)
    df_default, _ = get_batches(page=1, per_page=50)
    assert list(df_default["date_completed"]) == ["2024-03-01", "2024-02-01", "2024-01-01"]
    df_asc, _ = get_batches(page=1, per_page=50, sort="asc")
    assert list(df_asc["date_completed"]) == ["2024-01-01", "2024-02-01", "2024-03-01"]
    df_desc, _ = get_batches(page=1, per_page=50, sort="desc")
    assert list(df_desc["date_completed"]) == ["2024-03-01", "2024-02-01", "2024-01-01"]


def test_get_batches_planned_sorts_by_planned_completion_date(db):
    _insert_planned(db, "A", "2099-01-01")
    _insert_planned(db, "B", "2099-03-01")
    _insert_planned(db, "C", "2099-02-01")
    df_default, _ = get_batches_planned(page=1, per_page=50)
    assert list(df_default["planned_completion_date"]) == ["2099-03-01", "2099-02-01", "2099-01-01"]
    df_asc, _ = get_batches_planned(page=1, per_page=50, sort="asc")
    assert list(df_asc["planned_completion_date"]) == ["2099-01-01", "2099-02-01", "2099-03-01"]


def test_get_batches_shipped_sorts_by_date_shipped(db):
    from services.batches import get_batches_shipped
    _insert_shipped(db, "A", "2024-01-01")
    _insert_shipped(db, "B", "2024-03-01")
    _insert_shipped(db, "C", "2024-02-01")
    df_default, _ = get_batches_shipped(page=1, per_page=50)
    assert list(df_default["date_shipped"]) == ["2024-03-01", "2024-02-01", "2024-01-01"]
    df_asc, _ = get_batches_shipped(page=1, per_page=50, sort="asc")
    assert list(df_asc["date_shipped"]) == ["2024-01-01", "2024-02-01", "2024-03-01"]
