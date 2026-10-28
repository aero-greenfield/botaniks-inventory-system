"""HTTP integration tests through the Flask test client (app.py + routes/api.py).

Exercises the form-parse -> validate -> service -> redirect/flash path end to end, plus
auth enforcement and the JSON API. CSRF and rate limiting are disabled by the `client`
fixture so requests are deterministic. Representative coverage, not every route.
"""

import json
import re
from datetime import datetime, timedelta

import pytest

from services.materials import get_material_id, get_raw_material
from services.lots import get_material_stock_from_lots
from services.batches import (
    add_to_batches, get_batches, get_batches_planned, get_planned_lot_selections,
    promote_planned_batches, get_batch_by_id,
)
from zoneinfo import ZoneInfo


def _mix_output_lot(db, material_name):
    cur = db.cursor()
    db.execute(cur, """SELECT l.lot_id FROM raw_material_lots l
                       JOIN raw_materials m ON l.material_id = m.material_id
                       WHERE m.name = %s""", (material_name,))
    return cur.fetchone()[0]


# --- auth ----------------------------------------------------------------------------
def test_protected_route_requires_auth(client):
    assert client.get("/inventory").status_code == 401
    assert client.get("/api/materials").status_code == 401


def test_health_is_public_and_reports_db(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "healthy"
    assert body["db"] == "connected"


# --- GET pages render with auth ------------------------------------------------------
GET_PAGES = [
    "/", "/inventory", "/recipes", "/batches", "/low-stock", "/audit-log",
    "/manage-materials", "/manage-lots", "/manage-recipes", "/manage-batches",
    "/manage-shipments", "/shipments", "/shipments/new",
    "/add-material", "/receive-lot", "/create-batch", "/add-recipe",
]


@pytest.mark.parametrize("path", GET_PAGES)
def test_get_pages_render(client, auth, path):
    resp = client.get(path, headers=auth)
    assert resp.status_code == 200, f"{path} returned {resp.status_code}"


# --- sort feature: date-header links render and accept asc/desc without error ---------
# The sortable header only renders when the table has rows, so seed one first.
def _seed_ready(db):
    cur = db.cursor()
    db.execute(cur, """INSERT INTO batches (product_name, quantity, status, batch_number, batch_type, date_completed)
                       VALUES ('R', 10, 'Ready', 'R', 'standard', '2024-01-01')""")
    db.commit()


def _seed_shipped(db):
    cur = db.cursor()
    db.execute(cur, """INSERT INTO batches (product_name, quantity, status, batch_number, batch_type)
                       VALUES ('S', 10, 'Shipped', 'S', 'standard')""")
    bid = db.get_last_insert_id(cur)
    db.execute(cur, "INSERT INTO shipments (shipment_number, date_shipped, destination) VALUES ('SH1', '2024-01-01', 'D')")
    sid = db.get_last_insert_id(cur)
    db.execute(cur, "INSERT INTO shipment_batches (shipment_id, batch_id, quantity) VALUES (%s, %s, 10)", (sid, bid))
    db.commit()


@pytest.mark.parametrize("url", ["/batches", "/batches?ready_sort=asc&planned_sort=desc"])
def test_batches_sort_header_renders(client, auth, db, url):
    _seed_ready(db)
    html = client.get(url, headers=auth).get_data(as_text=True)
    assert "sort-link" in html


@pytest.mark.parametrize("url", ["/shipped-batches", "/shipped-batches?sort=asc"])
def test_shipped_sort_header_renders(client, auth, db, url):
    _seed_shipped(db)
    html = client.get(url, headers=auth).get_data(as_text=True)
    assert "sort-link" in html


# --- JSON API ------------------------------------------------------------------------
def test_api_materials_autocomplete(client, auth, make_material):
    make_material("Matcha Ceremonial")
    make_material("Sugar")
    rows = client.get("/api/materials?q=matcha", headers=auth).get_json()
    assert rows == [{"name": "Matcha Ceremonial", "is_housemade": False}]


def test_api_materials_flags_housemade(client, auth, make_material):
    # component-badge feature: the autocomplete payload must carry is_housemade so house-made
    # (Component) materials get a badge in the recipe-material search dropdown.
    make_material("Raw Matcha")
    make_material("House Blend", is_housemade=True)
    rows = client.get("/api/materials", headers=auth).get_json()
    flags = {r["name"]: r["is_housemade"] for r in rows}
    assert flags["Raw Matcha"] is False
    assert flags["House Blend"] is True


def test_api_material_unit_lookup(client, auth, make_material):
    make_material("Matcha", unit="g", dimension="mass")
    body = client.get("/api/material-unit?name=Matcha", headers=auth).get_json()
    assert body == {"exists": True, "unit": "g", "dimension": "mass"}


def test_api_material_unit_missing(client, auth):
    body = client.get("/api/material-unit?name=ghost", headers=auth).get_json()
    assert body == {"exists": False}


def test_api_available_lots(client, auth, make_material, make_lot):
    mid = make_material("Matcha")
    make_lot(mid, 25)
    rows = client.get(f"/api/available-lots/{mid}", headers=auth).get_json()
    assert len(rows) == 1
    assert rows[0]["quantity"] == 25


def test_api_available_lots_invalid_id(client, auth):
    resp = client.get("/api/available-lots/abc", headers=auth)
    assert resp.status_code == 400


def test_api_recipe_materials_reports_entry_unit(client, auth, make_material, make_recipe):
    # lot-selection-unit fix: a recipe line entered in lb must tell the create-batch UI to
    # DISPLAY in lb (display_unit) and how to convert stored grams <-> lb (base_per_display),
    # not hardcode grams. quantity_needed stays in grams (the base unit).
    from services import units
    make_material("JasmineMix", is_housemade=True)
    make_recipe(
        "FinishedTea",
        [{"material_name": "JasmineMix", "quantity_needed": float(units.to_base(16, "lb")), "unit": "lb"}],
    )
    rows = client.get("/api/recipe-materials/FinishedTea", headers=auth).get_json()
    assert len(rows) == 1
    row = rows[0]
    assert row["display_unit"] == "lb"
    assert row["base_per_display"] == pytest.approx(453.59237)
    assert row["quantity_needed"] == pytest.approx(float(units.to_base(16, "lb")))


def test_api_recipe_materials_count_uses_material_unit(client, auth, make_material, make_recipe):
    # count materials never convert: display in the material's own unit, factor 1.
    make_material("Tins", unit="tin", dimension="count")
    make_recipe(
        "GiftSet",
        [{"material_name": "Tins", "quantity_needed": 3, "unit": "tin"}],
        product_unit="set", product_dimension="count",
    )
    rows = client.get("/api/recipe-materials/GiftSet", headers=auth).get_json()
    assert rows[0]["display_unit"] == "tin"
    assert rows[0]["base_per_display"] == 1.0


# --- POST /add-material --------------------------------------------------------------
def _material_form(**over):
    form = {
        "name": "Matcha",
        "category": "Tea",
        "unit": "g",
        "material_type": "mass",
        "reorder_level": "5",
        "is_edible": "1",
    }
    form.update(over)
    return form


def test_add_material_success_redirects_and_persists(client, auth):
    resp = client.post("/add-material", data=_material_form(), headers=auth)
    assert resp.status_code == 302
    assert get_material_id("Matcha") is not None


def test_add_material_duplicate_shows_error(client, auth, make_material):
    make_material("Matcha")
    resp = client.post("/add-material", data=_material_form(name="Matcha"), headers=auth)
    assert resp.status_code == 200
    assert b"already exists" in resp.data


def test_add_material_blank_name_400(client, auth):
    resp = client.post("/add-material", data=_material_form(name=""), headers=auth)
    assert resp.status_code == 400


def test_add_material_missing_dimension_400(client, auth):
    resp = client.post("/add-material", data=_material_form(material_type=""), headers=auth)
    assert resp.status_code == 400


def test_add_material_mass_unit_mismatch_400(client, auth):
    # 'tin' is a count label, not a recognized mass unit -> rejected for a Weight material
    resp = client.post("/add-material", data=_material_form(unit="tin", material_type="mass"), headers=auth)
    assert resp.status_code == 400


# --- POST /receive-lot ---------------------------------------------------------------
def _lot_form(**over):
    form = {
        "material_name": "Matcha",
        "lot_number": "L-100",
        "quantity": "50",
        "received_date": "2024-01-01",
        "cost_per_unit": "2",
    }
    form.update(over)
    return form


def test_receive_lot_success_redirects_to_inventory(client, auth, make_material):
    make_material("Matcha", unit="g", dimension="mass")
    resp = client.post("/receive-lot", data=_lot_form(), headers=auth)
    assert resp.status_code == 302
    assert "/inventory" in resp.headers["Location"]
    mid = get_material_id("Matcha")
    assert get_material_stock_from_lots(mid) == 50


def test_receive_lot_unknown_material_redirects_to_add_material(client, auth):
    resp = client.post("/receive-lot", data=_lot_form(material_name="Ghost"), headers=auth)
    assert resp.status_code == 302
    assert "/add-material" in resp.headers["Location"]


def test_receive_lot_non_positive_quantity_400(client, auth, make_material):
    make_material("Matcha")
    resp = client.post("/receive-lot", data=_lot_form(quantity="0"), headers=auth)
    assert resp.status_code == 400


def test_receive_lot_blank_lot_number_400(client, auth, make_material):
    make_material("Matcha")
    resp = client.post("/receive-lot", data=_lot_form(lot_number=""), headers=auth)
    assert resp.status_code == 400


# --- inventory page rendering --------------------------------------------------------
def test_inventory_per_group_numbering_continues_across_pages(client, auth, make_material):
    # row-numbering fix: the page splits into Raw / Housemade tables AFTER pagination. With 60 raw
    # (names sort first) + 5 housemade, page 1 is 50 raws and page 2 holds the last 10 raws (#51-60)
    # plus all 5 housemade. The housemade group must restart at 1 (no housemade on prior pages), NOT
    # jump to the global page offset of 51 (the bug). Raw must continue at 51.
    for i in range(60):
        make_material(f"R{i:03d}")
    for i in range(5):
        make_material(f"Z{i:03d}", is_housemade=True)
    html = client.get("/inventory?page=2", headers=auth).get_data(as_text=True)
    raw_section = html.split('id="raw-materials-section"')[1].split('id="housemade-materials-section"')[0]
    hm_section = html.split('id="housemade-materials-section"')[1]
    # the # column is `<td class="num">N</td>`; the Stock column has a <span> inside so it won't match.
    raw_nums = [int(n) for n in re.findall(r'<td class="num">(\d+)</td>', raw_section)]
    hm_nums = [int(n) for n in re.findall(r'<td class="num">(\d+)</td>', hm_section)]
    assert raw_nums == list(range(51, 61))
    assert hm_nums == [1, 2, 3, 4, 5]


def test_inventory_zero_stock_shows_out_not_low(client, auth, make_material, make_lot):
    # Bug 6: a material whose stock is a sub-epsilon residual (displays as 0) must read "Out of
    # Stock", not "Low Stock". reorder_level 100 makes the old `== 0` failure fall into "Low".
    mid = make_material("Trace", reorder_level=100)
    make_lot(mid, 0.0001)  # float-deduction residual
    html = client.get("/inventory", headers=auth).get_data(as_text=True)
    # match the badge markup specifically — "Low Stock" also appears as a sidebar nav link.
    assert '<span class="badge badge-out">Out of Stock</span>' in html
    assert '<span class="badge badge-low">Low Stock</span>' not in html


def test_dashboard_zero_stock_shows_out_not_low(client, auth, make_material, make_lot):
    # Same residual-vs-zero bug as the inventory page, but on the DASHBOARD ('/'), which
    # previously used a raw `stock_level <= 0` check instead of the epsilon-aware
    # material_status filter — so a 0.00004 g residual rendered as "Low" there alone.
    mid = make_material("Trace", reorder_level=100)
    make_lot(mid, 0.00004)  # float-deduction residual, displays as 0
    html = client.get("/", headers=auth).get_data(as_text=True)
    assert '<span class="badge badge-out">' in html
    assert '<span class="badge badge-low">' not in html


def test_create_same_day_planned_batch_short_stock_alerts(client, auth, make_material, make_lot, make_recipe):
    # A planned batch dated TODAY can't promote without stock, so it must hit the same
    # "Not Enough Stock" alert as an immediate batch rather than silently becoming Planned.
    mid = make_material("Matcha")
    make_lot(mid, 5)  # recipe needs 10
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}], batch_type="finished")
    today = datetime.now().strftime("%Y-%m-%d")
    resp = client.post("/create-batch", headers=auth, data={
        "product_name": "Latte", "batch_number": "SD1", "quantity": "1",
        "planned_completion_date": today,
    })
    assert resp.status_code == 400
    assert b"Not Enough Stock" in resp.data
    # and no batch was created at all (neither promoted nor planned)
    assert get_batches(page=1, per_page=50)[1] == 0
    assert get_batches_planned(page=1, per_page=50)[1] == 0


def test_create_future_planned_batch_short_stock_allowed(client, auth, make_material, make_lot, make_recipe):
    # A FUTURE-dated DEFERRED plan reserves production against stock that hasn't arrived yet, so a
    # current shortfall must NOT block it — it's created and sits Planned (no alert).
    # CHANGED: planned-deduction-mode feature — deferral must now be asked for; see the
    # deduction_mode='immediate' counterpart below, which IS blocked by the same shortfall.
    mid = make_material("Matcha")
    make_lot(mid, 5)  # short for a need of 10
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}], batch_type="finished")
    future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    resp = client.post("/create-batch", headers=auth, data={
        "product_name": "Latte", "batch_number": "FUT1", "quantity": "1",
        "planned_completion_date": future, "deduction_mode": "deferred",
    }, follow_redirects=False)
    assert resp.status_code == 302  # redirect to batches, created
    # future plan isn't overdue, so it sits in the Planned list (not promoted/deducted)
    assert get_batches_planned(page=1, per_page=50)[1] == 1


def test_create_future_planned_batch_immediate_blocked_on_short_stock(client, auth, make_material, make_lot, make_recipe):
    # planned-deduction-mode feature — an IMMEDIATE planned batch takes stock today, so a shortfall
    # blocks it exactly like a Ready batch, even though its completion date is far in the future.
    mid = make_material("Matcha")
    make_lot(mid, 5)  # short for a need of 10
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}], batch_type="finished")
    future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    resp = client.post("/create-batch", headers=auth, data={
        "product_name": "Latte", "batch_number": "FUT2", "quantity": "1",
        "planned_completion_date": future, "deduction_mode": "immediate",
    }, follow_redirects=False)
    assert resp.status_code == 400
    assert b"Not Enough Stock" in resp.data
    assert get_batches_planned(page=1, per_page=50)[1] == 0


# --- batches page: component (mix) display ------------------------------------------
def test_batches_page_shows_mix_prefix_and_original_total(client, auth, db,
                                                          make_material, make_lot, make_recipe):
    # Bug 5: a Component batch's batch# shows the MIX-BATCH- prefix (display-only).
    # Bug 4: after a finished batch consumes part of the Component, the Component row shows
    #        "remaining / original-produced" like a partially-shipped finished batch.
    mid = make_material("Matcha")
    lid = make_lot(mid, 1000, cost_per_unit=3)
    make_recipe("Mix", [{"material_name": "Matcha", "quantity_needed": 1}],
                batch_type="mix", product_unit="g", product_dimension="mass")
    add_to_batches("Mix", 100, batch_number="C1", batch_type="mix",
                   lot_selections={mid: [{"lot_id": lid, "qty": 100}]})
    mix_lot = _mix_output_lot(db, "Mix")
    mix_mid = get_raw_material("Mix")[0]
    make_recipe("FinishedTea", [{"material_name": "Mix", "quantity_needed": 10, "unit": "g"}])
    add_to_batches("FinishedTea", 1, batch_number="F1",
                   lot_selections={mix_mid: [{"lot_id": mix_lot, "qty": 10}]})

    html = client.get("/batches", headers=auth).get_data(as_text=True)
    assert "MIX-BATCH-C1" in html          # Bug 5: prefixed component batch#
    assert "/ 100" in html                  # Bug 4: 90 remaining / 100 originally produced


def test_component_row_shows_kg_not_raw_grams_after_recipe_rename(
    client, auth, db, make_material, make_lot, make_recipe
):
    # renamed-recipe fix, end to end. Reproduces the production incident: a 4 kg Component
    # (4000 g output lot) whose recipe was later renamed printed a bare "4000" — the unit lookup
    # went NULL, so the route skipped the gram->kg conversion AND the template dropped the unit
    # suffix. Note "4000 < 236" was also false, which is why even the "/ total" suffix vanished.
    mid = make_material("Matcha")
    lid = make_lot(mid, 10000)
    make_recipe("KgMix", [{"material_name": "Matcha", "quantity_needed": 1000}],
                batch_type="mix", product_unit="kg", product_dimension="mass")
    add_to_batches("KgMix", 4, batch_number="KG9", batch_type="mix",
                   lot_selections={mid: [{"lot_id": lid, "qty": 4000}]})

    cur = db.cursor()
    db.execute(cur, "UPDATE recipes SET product_name = %s WHERE product_name = %s",
               ("KgMix V1", "KgMix"))
    db.commit()

    html = client.get("/batches", headers=auth).get_data(as_text=True)
    assert "MIX-BATCH-KG9" in html
    assert "4000" not in html, "Component row must not print the output lot in raw grams"
    assert "kg" in html, "the unit suffix must render"


# --- planned-deduction-mode feature: planned batches are visible and editable ---------
def _planned_deferred_batch(make_material, make_lot, make_recipe):
    """A deferred Planned finished batch with one reserved lot. Returns (batch_id, material_id, lot_id)."""
    mid = make_material("Matcha")
    lid = make_lot(mid, 100, lot_number="RES9")
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}], batch_type="finished")
    bid = add_to_batches("Latte", 1, batch_number="PD1", planned_completion_date="2999-01-01",
                         batch_type="finished", deduction_mode="deferred",
                         lot_selections={mid: [{"lot_id": lid, "qty": 10}]})
    return bid, mid, lid


def test_materials_endpoint_shows_reserved_lots_of_deferred_batch(client, auth, make_material, make_lot, make_recipe):
    # A deferred batch has no batch_materials rows; the drawer must still show its chosen lots,
    # flagged as reservations rather than deductions.
    bid, _mid, _lid = _planned_deferred_batch(make_material, make_lot, make_recipe)
    payload = client.get(f"/batches/{bid}/materials", headers=auth).get_json()
    assert payload["planned"] is True
    assert payload["fifo"] is False
    assert [m["lot_number"] for m in payload["materials"]] == ["RES9"]


def test_materials_endpoint_reports_fifo_when_no_lots_reserved(client, auth, make_material, make_lot, make_recipe):
    make_material("Matcha")
    mid = get_material_id("Matcha")
    make_lot(mid, 100)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}], batch_type="finished")
    bid = add_to_batches("Latte", 1, batch_number="PD2", planned_completion_date="2999-01-01",
                         batch_type="finished", deduction_mode="deferred", lot_selections=None)
    payload = client.get(f"/batches/{bid}/materials", headers=auth).get_json()
    assert payload["materials"] == []
    assert payload["fifo"] is True


def test_mark_ready_on_deferred_batch_deducts_stock_and_shows_materials(client, auth, make_material, make_lot, make_recipe):
    # Regression: "Mark Ready" on a deferred Planned batch only flipped the status, so nothing was
    # deducted and the drawer said "No materials recorded".
    make_material("Matcha")
    mid = get_material_id("Matcha")
    lid = make_lot(mid, 100)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}], batch_type="finished")
    bid = add_to_batches("Latte", 1, batch_number="MR1", planned_completion_date="2999-01-01",
                         batch_type="finished", deduction_mode="deferred", lot_selections=None)

    resp = client.post(f"/edit-batch/{bid}/change-status", headers=auth,
                       data={"status": "Ready", "next": "batches"})

    assert resp.status_code == 302
    assert get_batch_by_id(bid)[4] == "Ready"
    assert get_material_stock_from_lots(mid) == 90
    payload = client.get(f"/batches/{bid}/materials", headers=auth).get_json()
    assert payload["planned"] is False
    assert len(payload["materials"]) == 1


def test_mark_ready_with_short_stock_keeps_batch_planned_and_flashes(client, auth, make_material, make_lot, make_recipe):
    make_material("Matcha")
    mid = get_material_id("Matcha")
    make_lot(mid, 5)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}], batch_type="finished")
    bid = add_to_batches("Latte", 1, batch_number="MR2", planned_completion_date="2999-01-01",
                         batch_type="finished", deduction_mode="deferred", lot_selections=None)

    resp = client.post(f"/edit-batch/{bid}/change-status", headers=auth, data={"status": "Ready"})

    assert resp.status_code == 302
    assert get_batch_by_id(bid)[4] == "Planned"
    assert get_material_stock_from_lots(mid) == 5
    with client.session_transaction() as sess:
        flashes = sess.get("_flashes", [])
    assert any(cat == "error" and "stays Planned" in msg for cat, msg in flashes)


def test_edit_page_opens_for_deferred_planned_batch(client, auth, make_material, make_lot, make_recipe):
    # Regression: this used to 404 ("Batch Materials Not Found") because a deferred batch has no
    # batch_materials rows, making every planned batch un-editable.
    bid, _mid, _lid = _planned_deferred_batch(make_material, make_lot, make_recipe)
    resp = client.get(f"/edit-batch/{bid}", headers=auth)
    assert resp.status_code == 200
    assert b"Planned Lot Selection" in resp.data


def test_update_planned_lots_route_rewrites_reservation(client, auth, db, make_material, make_lot, make_recipe):
    bid, mid, lid = _planned_deferred_batch(make_material, make_lot, make_recipe)
    other = make_lot(mid, 100, lot_number="RES10")
    resp = client.post(f"/edit-batch/{bid}/planned-lots", headers=auth, data={
        "lot_selection": json.dumps({str(mid): [{"lot_id": other, "qty": 10}]}),
    }, follow_redirects=False)
    assert resp.status_code == 302
    assert get_planned_lot_selections(bid)[0][3] == other
    # no stock moved — this is a reservation, not a deduction
    cur = db.cursor()
    db.execute(cur, "SELECT quantity FROM raw_material_lots WHERE lot_id = %s", (lid,))
    assert cur.fetchone()[0] == 100


def test_quantity_change_clears_reserved_lots(client, auth, make_material, make_lot, make_recipe):
    # Stored per-lot sums are validated against quantity_needed * quantity at promotion, so stale
    # picks would strand the batch as Planned. The route clears them and warns instead.
    bid, _mid, _lid = _planned_deferred_batch(make_material, make_lot, make_recipe)
    resp = client.post(f"/edit-batch/{bid}/update-details", headers=auth, data={
        "product_name": "Latte", "quantity": "3", "planned_completion_date": "2999-01-01",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert get_planned_lot_selections(bid) == []
    assert b"lot selections were cleared" in resp.data


# --- server-timezone-vs-business-timezone fix -----------------------------------------
# Reproduces the two exact production bugs reported: Render runs UTC, Botaniks runs Pacific.
# Frozen instant throughout: 2026-01-02 06:00 UTC = 2026-01-01 22:00 PST (Pacific is UTC-8 in
# January, no DST) — Pacific's evening of Jan 1, but UTC has already rolled over to Jan 2.
_FROZEN_UTC_EVENING = datetime(2026, 1, 2, 6, 0, 0, tzinfo=ZoneInfo("UTC"))
_PACIFIC_TODAY = "2026-01-01"
_PACIFIC_TOMORROW = "2026-01-02"


def test_planned_date_of_today_is_not_rejected_as_past(client, auth, freeze_business_time,
                                                        make_material, make_lot, make_recipe):
    # Bug 1: submitting Pacific's actual "today" was rejected with "cannot be in the past",
    # because it was compared against the server's UTC date, already one day ahead in the evening.
    freeze_business_time(_FROZEN_UTC_EVENING)
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}], batch_type="finished")
    resp = client.post("/create-batch", headers=auth, data={
        "product_name": "Latte", "batch_number": "TZ1", "quantity": "1",
        "planned_completion_date": _PACIFIC_TODAY, "deduction_mode": "deferred",
        "lot_selection": json.dumps({str(mid): [{"lot_id": lid, "qty": 10}]}),
    }, follow_redirects=False)
    assert resp.status_code == 302  # was 400 "cannot be in the past" before the fix
    assert get_batches_planned(page=1, per_page=50)[1] == 1


def test_planned_date_of_tomorrow_does_not_auto_promote(client, auth, freeze_business_time,
                                                         make_material, make_lot, make_recipe):
    # Bug 2 (the big one): submitting Pacific's actual "tomorrow" got promoted to Ready
    # immediately, because that date already equalled the server's UTC "today".
    freeze_business_time(_FROZEN_UTC_EVENING)
    mid = make_material("Matcha")
    lid = make_lot(mid, 100)
    make_recipe("Latte", [{"material_name": "Matcha", "quantity_needed": 10}], batch_type="finished")
    resp = client.post("/create-batch", headers=auth, data={
        "product_name": "Latte", "batch_number": "TZ2", "quantity": "1",
        "planned_completion_date": _PACIFIC_TOMORROW, "deduction_mode": "deferred",
        "lot_selection": json.dumps({str(mid): [{"lot_id": lid, "qty": 10}]}),
    }, follow_redirects=False)
    assert resp.status_code == 302

    batch_id = get_batches_planned(page=1, per_page=50)[0].iloc[0]["batch_id"]
    promote_planned_batches()  # must NOT promote — Pacific's "today" is still one day short
    batch = get_batch_by_id(int(batch_id))
    assert batch[4] == "Planned"  # was "Ready" before the fix
    # and the lot must be untouched — nothing should have been deducted
    cur_db = get_material_stock_from_lots(mid)
    assert cur_db == 100
