"""Audit log: per-record history, before/after changes, atomic writes, recoverable failures."""
import json
import logging

import pytest

from database import get_db_connection
from services.audit import diff, log_action, view_logs


def _rows(db, action):
    cur = db.cursor()
    db.execute(cur, """SELECT entity_type, entity_id, changes, source FROM audit_log
                       WHERE action = %s ORDER BY log_id""", (action,))
    return cur.fetchall()


def test_diff_keeps_only_changed_fields():
    before = {"quantity": 100.0, "location": "Fern", "expiration_date": "2026-12-01"}
    after = {"quantity": 80.0, "location": "Fern", "expiration_date": "2026-12-01"}
    assert diff(before, after) == {"quantity": [100.0, 80.0]}


def test_diff_treats_db_types_equal_to_form_text():
    import datetime
    from decimal import Decimal
    # Postgres returns date/Decimal; the form sends strings/floats — not a change.
    assert diff({"d": datetime.date(2026, 11, 1), "q": Decimal("12.0")}, {"d": "2026-11-01", "q": 12.0}) == {}


def test_lot_edit_records_entity_changes_and_source(client, auth, make_material, make_lot, db):
    mid = make_material("Matcha", unit="kg")
    lid = make_lot(mid, 100000, lot_number="L1")
    form = {"lot_number": "L1", "quantity": "80", "original_quantity": "100", "expiration_date": "",
            "location": "Fern", "supplier": "", "cost_per_unit": "",
            "adjust_reason": "count_correction", "adjust_note": ""}
    assert client.post(f"/edit-lot/{lid}/update", data=form, headers=auth).status_code == 302

    ((entity_type, entity_id, changes, source),) = _rows(db, "lot_updated")
    assert (entity_type, entity_id) == ("lot", lid)
    changes = json.loads(changes)
    assert changes["quantity"] == [100000, 80000]
    assert changes["location"][1] == "Fern"
    assert "lot_number" not in changes  # unchanged fields are not recorded
    assert source == f"POST /edit-lot/{lid}/update"


def test_in_transaction_audit_row_rolls_back_with_the_change(make_material, db):
    mid = make_material("Matcha")
    tx = get_db_connection()
    cur = tx.cursor()
    tx.execute(cur, "UPDATE raw_materials SET name = 'Changed' WHERE material_id = %s", (mid,))
    log_action("material_updated", None, entity=("material", mid), db=tx, cursor=cur)
    tx.rollback()
    tx.close()
    assert _rows(db, "material_updated") == []


class _BrokenDB:
    def cursor(self): return None
    def execute(self, *a): raise RuntimeError("db down")
    def close(self): pass


def test_in_transaction_audit_failure_propagates():
    # Inside a transaction the error must reach the caller, so its change rolls back too.
    with pytest.raises(RuntimeError):
        log_action("x", db=_BrokenDB(), cursor=None)


def test_failed_standalone_write_logs_the_full_row(monkeypatch, caplog):
    import services.audit as audit
    monkeypatch.setattr(audit, "get_db_connection", lambda: _BrokenDB())
    with caplog.at_level(logging.ERROR):
        log_action("lot_deleted", "lot_number=L-12", entity=("lot", 7), changes={"quantity": [5, 0]})
    assert "lot_deleted" in caplog.text and "('lot', 7)" in caplog.text and "L-12" in caplog.text


def test_view_logs_filters_by_exact_record():
    log_action("lot_updated", entity=("lot", 4))
    log_action("lot_updated", entity=("lot", 42))   # 'lot_id=4' text search would also match this
    log_action("batch_updated", entity=("batch", 4))
    df, total = view_logs("lot", 4)
    assert total == 1 and list(df["entity_id"]) == [4] and list(df["entity_type"]) == ["lot"]
    _, everything = view_logs()
    assert everything == 3


def test_audit_page_shows_one_records_history(client, auth):
    log_action("lot_updated", entity=("lot", 4), changes={"location": ["A", "B"]})
    log_action("lot_updated", entity=("lot", 42))
    html = client.get("/audit-log?entity_type=lot&entity_id=4", headers=auth).get_data(as_text=True)
    assert "History of lot #4" in html
    assert "location: A" in html
    assert "lot #42" not in html
