"""sort/search feature — the shared order_by/search_where helpers, each list service's sort keys
and ?q= filter (rows AND the pagination total), and that every list page survives junk input."""

import re

import pytest

from services import order_by, search_where
from services.materials import get_all_materials
from services.lots import get_all_lots
from services.batches import get_batches, get_batches_planned, get_all_batches_with_id
from services.shipments import get_all_shipments
from services.recipes import get_all_recipes, get_all_recipes_with_id


# --- helpers -------------------------------------------------------------------------
def test_order_by_only_accepts_whitelisted_keys_and_directions():
    wl = {"name": "rm.name"}
    assert order_by(wl, "name", "desc", "x", "id DESC") == "ORDER BY rm.name DESC, id DESC"
    assert order_by(wl, "name", "'; DROP TABLE x", "x", "id DESC") == "ORDER BY rm.name ASC, id DESC"
    assert order_by(wl, "rm.name; DROP TABLE x", "asc", "x ASC", "id DESC") == "ORDER BY x ASC"
    assert order_by(wl, None, "asc", "x ASC", "id DESC") == "ORDER BY x ASC"


def test_search_where_is_parameterised_and_blank_is_a_noop():
    assert search_where(["a"], "  ") == ("", ())
    sql, params = search_where(["a", "b"], " Foo ")
    assert sql == "(LOWER(a) LIKE LOWER(%s) OR LOWER(b) LIKE LOWER(%s))"
    assert params == ("%Foo%", "%Foo%")


# --- seed helpers --------------------------------------------------------------------
def _batch(db, name, status="Ready", date_completed=None, planned=None, date_shipped=None, qty=10):
    cur = db.cursor()
    db.execute(cur, """INSERT INTO batches (product_name, quantity, status, batch_number, batch_type,
                                            date_completed, planned_completion_date, date_shipped)
                       VALUES (%s, %s, %s, %s, 'standard', %s, %s, %s)""",
               (name, qty, status, f"B-{name}", date_completed, planned, date_shipped))
    db.commit()


def _shipment(db, number, destination, date_shipped, n_batches=0):
    cur = db.cursor()
    db.execute(cur, "INSERT INTO shipments (shipment_number, date_shipped, destination) VALUES (%s, %s, %s)",
               (number, date_shipped, destination))
    sid = db.get_last_insert_id(cur)
    for i in range(n_batches):
        db.execute(cur, """INSERT INTO batches (product_name, quantity, status, batch_number, batch_type)
                           VALUES ('P', 1, 'Shipped', %s, 'standard')""", (f"{number}-{i}",))
        bid = db.get_last_insert_id(cur)
        db.execute(cur, "INSERT INTO shipment_batches (shipment_id, batch_id, quantity) VALUES (%s, %s, 1)", (sid, bid))
    db.commit()


# --- materials -----------------------------------------------------------------------
def test_materials_search_filters_rows_and_total(make_material):
    make_material("Matcha Powder", category="Tea")
    make_material("Oat Milk", category="Dairy-free")
    make_material("Ceremonial Matcha", category="Tea")
    df, total = get_all_materials(page=1, per_page=50, q="matcha")
    assert sorted(df["name"]) == ["Ceremonial Matcha", "Matcha Powder"]
    assert total == 2
    _, total_cat = get_all_materials(page=1, per_page=50, q="dairy")
    assert total_cat == 1


# --- lots ----------------------------------------------------------------------------
def test_lots_sort_and_search(make_material, make_lot):
    mid = make_material("Matcha")
    other = make_material("Sugar")
    make_lot(mid, 100, lot_number="L-OLD", received_date="2024-01-01", expiry_date="2025-06-01", supplier="Uji Co")
    make_lot(mid, 100, lot_number="L-NEW", received_date="2024-05-01", expiry_date=None, supplier="Uji Co")
    make_lot(other, 100, lot_number="S-1", received_date="2024-03-01", expiry_date="2025-01-01", supplier="Sweet Inc")

    df, _ = get_all_lots(page=1, per_page=50, sort_by="received", sort_dir="desc")
    assert list(df["lot_number"]) == ["L-NEW", "S-1", "L-OLD"]
    # soonest expiry first; the lot with no expiry goes last
    df, _ = get_all_lots(page=1, per_page=50, sort_by="expiry", sort_dir="asc")
    assert list(df["lot_number"]) == ["S-1", "L-OLD", "L-NEW"]

    df, total = get_all_lots(page=1, per_page=50, q="l-new")
    assert list(df["lot_number"]) == ["L-NEW"] and total == 1
    df, total = get_all_lots(page=1, per_page=50, q="sugar")      # by material name
    assert list(df["lot_number"]) == ["S-1"] and total == 1
    _, total = get_all_lots(page=1, per_page=50, q="uji")          # by supplier
    assert total == 2


# --- batches -------------------------------------------------------------------------
def test_ready_batches_sort_by_product_and_search(db):
    _batch(db, "Zesty", date_completed="2024-01-01")
    _batch(db, "Apple", date_completed="2024-02-01")
    _batch(db, "Mango", date_completed="2024-03-01")
    df, _ = get_batches(page=1, per_page=50, sort="asc", sort_by="product")
    assert list(df["product_name"]) == ["Apple", "Mango", "Zesty"]
    df, total = get_batches(page=1, per_page=50, q="b-mango")      # by batch #
    assert list(df["product_name"]) == ["Mango"] and total == 1
    # unknown column falls back to the date default, not an error
    df, _ = get_batches(page=1, per_page=50, sort_by="nope")
    assert list(df["product_name"]) == ["Mango", "Apple", "Zesty"]


def test_planned_batches_search(db):
    _batch(db, "Apple", status="Planned", planned="2099-01-01")
    _batch(db, "Mango", status="Planned", planned="2099-02-01")
    df, total = get_batches_planned(page=1, per_page=50, q="app")
    assert list(df["product_name"]) == ["Apple"] and total == 1


def test_manage_batches_date_sort_uses_the_date_shown_per_status(db):
    _batch(db, "Old", date_completed="2024-01-01")
    _batch(db, "Plan", status="Planned", planned="2099-01-01")
    _batch(db, "Ship", status="Shipped", date_completed="2023-01-01", date_shipped="2024-06-01")
    df, _ = get_all_batches_with_id(page=1, per_page=50, sort_by="date", sort_dir="desc")
    assert list(df["product_name"]) == ["Plan", "Ship", "Old"]
    df, total = get_all_batches_with_id(page=1, per_page=50, q="planned")   # by status
    assert list(df["product_name"]) == ["Plan"] and total == 1


# --- shipments -----------------------------------------------------------------------
def test_shipments_sort_and_search(db):
    _shipment(db, "SH-1", "Tokyo", "2024-01-01", n_batches=1)
    _shipment(db, "SH-2", "Berlin", "2024-03-01", n_batches=3)
    _shipment(db, "SH-3", "Toronto", "2024-02-01", n_batches=0)
    df, _ = get_all_shipments(page=1, per_page=50, sort_by="batches", sort_dir="desc")
    assert list(df["shipment_number"]) == ["SH-2", "SH-1", "SH-3"]
    df, _ = get_all_shipments(page=None, sort_by="date", sort_dir="asc")
    assert list(df["shipment_number"]) == ["SH-1", "SH-3", "SH-2"]
    df, total = get_all_shipments(page=1, per_page=50, q="to")
    assert sorted(df["shipment_number"]) == ["SH-1", "SH-3"] and total == 2


# --- recipes -------------------------------------------------------------------------
def test_recipes_sort_keeps_ingredients_grouped_and_search(make_material, make_recipe):
    make_material("Matcha")
    make_material("Water")
    lines = [{"material_name": "Matcha", "quantity_needed": 5}, {"material_name": "Water", "quantity_needed": 50}]
    make_recipe("Alpha Latte", lines)
    make_recipe("Zen Shot", lines)

    df, total = get_all_recipes(page=1, per_page=50, sort_by="name", sort_dir="desc")
    assert list(df["recipe_product_name"]) == ["Zen Shot", "Zen Shot", "Alpha Latte", "Alpha Latte"]
    assert total == 2
    df, total = get_all_recipes(page=1, per_page=50, q="zen")
    assert set(df["recipe_product_name"]) == {"Zen Shot"} and total == 1

    df, _ = get_all_recipes_with_id(page=1, per_page=50, sort_by="added", sort_dir="desc")
    assert list(df["product_name"]) == ["Zen Shot", "Alpha Latte"]


# --- routes: every list page accepts sort/search input, including hostile input -------
LIST_PAGES = ["/inventory", "/manage-materials", "/manage-lots", "/batches", "/manage-batches",
              "/shipments", "/manage-shipments", "/recipes", "/manage-recipes"]


@pytest.mark.parametrize("path", LIST_PAGES)
@pytest.mark.parametrize("qs", ["?sort=name&dir=desc", "?sort=';DROP TABLE batches;--&dir=x",
                                "?q=';DROP TABLE batches;--", "?q=%25&page=2",
                                "?ready_by=product&planned_by=bogus&q=x"])
def test_list_pages_accept_sort_and_search(client, auth, path, qs):
    resp = client.get(path + qs, headers=auth)
    assert resp.status_code == 200, f"{path}{qs} returned {resp.status_code}"


def test_search_shows_no_match_message_and_keeps_query_in_box(client, auth, db):
    _batch(db, "Apple", date_completed="2024-01-01")
    html = client.get("/manage-batches?q=zzz", headers=auth).get_data(as_text=True)
    assert "No batches match" in html
    assert 'value="zzz"' in html


# --- review fixes ----------------------------------------------------------------------
def test_available_sort_follows_the_component_leftover_shown(db, make_material, make_lot):
    # Component rows show what's left of their output lot, so "Available" must sort by that,
    # not by produced quantity. Big produced 100 but has 5 left; Small produced 10, all left.
    mid = make_material("House Mix", is_housemade=True)
    big_lot = make_lot(mid, 5)
    small_lot = make_lot(mid, 10)
    cur = db.cursor()
    for name, qty, lot in (("Big", 100, big_lot), ("Small", 10, small_lot)):
        db.execute(cur, """INSERT INTO batches (product_name, quantity, status, batch_number, batch_type,
                                                date_completed, mix_lot_id)
                           VALUES (%s, %s, 'Ready', %s, 'mix', '2024-01-01', %s)""", (name, qty, name, lot))
    db.commit()
    df, _ = get_batches(page=1, per_page=50, sort="asc", sort_by="available")
    assert list(df["product_name"]) == ["Big", "Small"]


def test_batches_search_form_carries_the_open_tab(client, auth):
    html = client.get("/batches?tab=planned", headers=auth).get_data(as_text=True)
    assert 'id="search-tab" value="planned"' in html


def test_shipments_page_zero_is_page_one(client, auth, db):
    _shipment(db, "SH-1", "Tokyo", "2024-01-01")
    html = client.get("/shipments?page=0", headers=auth).get_data(as_text=True)
    assert "SH-1" in html


# --- Manage Lots "Show" filter (active / inactive / all) --------------------------------
def _active_and_inactive_lot(db, make_material, make_lot):
    mid = make_material("Matcha")
    make_lot(mid, 100, lot_number="L-ACTIVE")
    gone = make_lot(mid, 100, lot_number="L-USED-UP")
    cur = db.cursor()
    db.execute(cur, "UPDATE raw_material_lots SET quantity = 0, status = 'inactive' WHERE lot_id = %s", (gone,))
    db.commit()


def test_lots_status_filter_rows_and_total(db, make_material, make_lot):
    _active_and_inactive_lot(db, make_material, make_lot)
    df, total = get_all_lots(page=1, per_page=50, status="active")
    assert list(df["lot_number"]) == ["L-ACTIVE"] and total == 1
    df, total = get_all_lots(page=1, per_page=50, status="inactive")
    assert list(df["lot_number"]) == ["L-USED-UP"] and total == 1
    _, total = get_all_lots(page=1, per_page=50, status=None)
    assert total == 2


@pytest.mark.parametrize("qs, shown, hidden, selected", [
    ("", ["L-ACTIVE"], ["L-USED-UP"], "Active only"),           # default: active only
    ("?show=all", ["L-ACTIVE", "L-USED-UP"], [], "All lots"),
    ("?show=inactive", ["L-USED-UP"], ["L-ACTIVE"], "Inactive only"),
    ("?show=bogus", ["L-ACTIVE"], ["L-USED-UP"], "Active only"),  # junk falls back to active
])
def test_manage_lots_show_filter(client, auth, db, make_material, make_lot, qs, shown, hidden, selected):
    _active_and_inactive_lot(db, make_material, make_lot)
    html = client.get("/manage-lots" + qs, headers=auth).get_data(as_text=True)
    for lot in shown:
        assert lot in html
    for lot in hidden:
        assert lot not in html
    assert re.search(r"<option[^>]*selected[^>]*>" + selected + "</option>", html)
