from database import get_db_connection
from services.audit import log_action, diff
import logging

# Saved shipping contacts. Shipments COPY these values when a contact is picked (no FK),
# so editing or deleting a contact never changes a past shipment's record.

_COLS = ['contact_id', 'name', 'contact_info', 'destination']


def _check_name(cursor, db, name, exclude_id=None):
    """Raises ValueError if name is blank or (case-insensitively) already used by another contact."""
    if not name:
        raise ValueError("Contact name cannot be blank.")
    db.execute(cursor, "SELECT contact_id FROM shipping_contacts WHERE LOWER(name) = LOWER(%s) AND contact_id != %s",
               (name, exclude_id or 0))
    if cursor.fetchone():
        raise ValueError(f"A contact named '{name}' already exists.")


def get_contacts():
    """All contacts as a list of dicts, ordered by name."""
    db = get_db_connection()
    cursor = db.cursor()
    try:
        db.execute(cursor, f"SELECT {', '.join(_COLS)} FROM shipping_contacts ORDER BY LOWER(name)")
        return [dict(zip(_COLS, r)) for r in cursor.fetchall()]
    except Exception as e:
        logging.error(f"Error getting shipping contacts: {e}")
        return []
    finally:
        db.close()


def get_contact(contact_id):
    """One contact as a dict, or None if not found."""
    db = get_db_connection()
    cursor = db.cursor()
    try:
        db.execute(cursor, f"SELECT {', '.join(_COLS)} FROM shipping_contacts WHERE contact_id = %s", (contact_id,))
        row = cursor.fetchone()
        return dict(zip(_COLS, row)) if row else None
    finally:
        db.close()


def create_contact(name, contact_info=None, destination=None):
    """Returns the new contact_id. Raises ValueError on blank/duplicate name."""
    db = get_db_connection()
    cursor = db.cursor()
    try:
        _check_name(cursor, db, name)
        db.execute(cursor, "INSERT INTO shipping_contacts (name, contact_info, destination) VALUES (%s, %s, %s)",
                   (name, contact_info, destination))
        contact_id = db.get_last_insert_id(cursor)
        log_action('contact_created', f"name={name}", entity=('contact', contact_id), db=db, cursor=cursor)
        db.commit()
        return contact_id
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def update_contact(contact_id, name, contact_info=None, destination=None):
    """Raises ValueError if not found or on blank/duplicate name."""
    db = get_db_connection()
    cursor = db.cursor()
    try:
        db.execute(cursor, "SELECT name, contact_info, destination FROM shipping_contacts WHERE contact_id = %s",
                   (contact_id,))
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"Contact {contact_id} not found.")
        _check_name(cursor, db, name, exclude_id=contact_id)
        db.execute(cursor, "UPDATE shipping_contacts SET name = %s, contact_info = %s, destination = %s WHERE contact_id = %s",
                   (name, contact_info, destination, contact_id))
        before = dict(zip(['name', 'contact_info', 'destination'], row))
        after = {'name': name, 'contact_info': contact_info, 'destination': destination}
        log_action('contact_updated', None, entity=('contact', contact_id),
                   changes=diff(before, after), db=db, cursor=cursor)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def delete_contact(contact_id):
    """Raises ValueError if not found. Shipments keep their copied buyer details."""
    db = get_db_connection()
    cursor = db.cursor()
    try:
        db.execute(cursor, "SELECT name FROM shipping_contacts WHERE contact_id = %s", (contact_id,))
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"Contact {contact_id} not found.")
        db.execute(cursor, "DELETE FROM shipping_contacts WHERE contact_id = %s", (contact_id,))
        log_action('contact_deleted', f"name={row[0]}", entity=('contact', contact_id), db=db, cursor=cursor)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
