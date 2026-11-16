"""Tests for services/contacts.py — saved shipping contacts, and the buyer fields they fill
on shipments (copied, not linked)."""

import pytest

from services.contacts import get_contacts, get_contact, create_contact, update_contact, delete_contact
from services.shipments import create_shipment, get_shipment_by_id
from test_scripts.test_shipments import make_ready_batch  # noqa: F401 (fixture)


def test_contact_crud():
    cid = create_contact("Acme Tea", "acme@example.com", "12 Main St")
    assert get_contact(cid) == {'contact_id': cid, 'name': "Acme Tea",
                                'contact_info': "acme@example.com", 'destination': "12 Main St"}

    update_contact(cid, "Acme Tea Co", None, "34 Side St")
    assert get_contact(cid)['name'] == "Acme Tea Co"
    assert get_contact(cid)['contact_info'] is None

    delete_contact(cid)
    assert get_contact(cid) is None
    assert get_contacts() == []


def test_contacts_sorted_by_name():
    create_contact("zeta")
    create_contact("Alpha")
    assert [c['name'] for c in get_contacts()] == ["Alpha", "zeta"]


def test_duplicate_or_blank_name_rejected():
    cid = create_contact("Acme")
    with pytest.raises(ValueError):
        create_contact("acme")          # case-insensitive
    with pytest.raises(ValueError):
        create_contact("")
    other = create_contact("Beta")
    with pytest.raises(ValueError):
        update_contact(other, "ACME")
    update_contact(cid, "Acme", "new info")  # renaming to its own name is fine


def test_missing_contact_raises():
    with pytest.raises(ValueError):
        update_contact(9999, "X")
    with pytest.raises(ValueError):
        delete_contact(9999)


def test_deleting_contact_keeps_shipment_copy(make_ready_batch):
    cid = create_contact("Acme", "555-0100", "Dock 4")
    s = create_shipment({make_ready_batch("A", 10): 5}, destination="Dock 4",
                        buyer_name="Acme", buyer_contact="555-0100")
    delete_contact(cid)
    ship = get_shipment_by_id(s)['shipment']
    assert (ship['buyer_name'], ship['buyer_contact'], ship['destination']) == ("Acme", "555-0100", "Dock 4")
