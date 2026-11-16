"""Shared pytest fixtures for the matcha-inventory test suite.

Isolation model
---------------
Production runs PostgreSQL; this suite runs SQLite through the same `database.py`
abstraction. Every test runs against a *throwaway* SQLite file created in a temp dir
(SQLITE_PATH), never the real `data/inventory.db`. The schema is built once per session;
every table is wiped before and after each test so tests don't see each other's rows.

Environment is set HERE, at conftest import time — before `app`, `auth`, `database`, or any
`services.*` module is imported — because several of those read env vars at import:
  * app.py            -> requires SECRET_KEY (raises without it)
  * auth.py           -> asserts AUTH_USERNAME / AUTH_PASSWORD
  * database.py       -> reads DATABASE_URL once into a module global (must be unset -> SQLite)
  * database/setup.py -> read SQLITE_PATH lazily at connect time (so the temp path is honoured)
"""

import os
import sys
import base64
import tempfile
from datetime import datetime

# --- env must be set before importing the app/services (see module docstring) ---------
_TEST_USER = "testuser"
_TEST_PASS = "testpass"
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ["AUTH_USERNAME"] = _TEST_USER
os.environ["AUTH_PASSWORD"] = _TEST_PASS
os.environ.pop("DATABASE_URL", None)  # force SQLite mode

_TMP_DIR = tempfile.mkdtemp(prefix="matcha-test-")
os.environ["SQLITE_PATH"] = os.path.join(_TMP_DIR, "test_inventory.db")

# Make the project root importable regardless of where pytest is invoked from.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import pytest  # noqa: E402

from services.setup import create_database  # noqa: E402
from database import get_db_connection  # noqa: E402
from services.materials import add_raw_material  # noqa: E402
from services.lots import receive_lot  # noqa: E402
from services.recipes import add_recipe  # noqa: E402

# Child tables first so DELETEs don't trip foreign keys.
_TABLES = [
    "batch_materials",
    "shipment_batches",
    "batches",
    "shipments",
    "shipping_contacts",
    "recipe_materials",
    "recipes",
    "raw_material_lots",
    "raw_materials",
    "audit_log",
]


def _wipe():
    db = get_db_connection()
    cur = db.cursor()
    for table in _TABLES:
        db.execute(cur, f"DELETE FROM {table}")
    db.commit()
    db.close()


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """Build the schema once in the throwaway SQLite DB for the whole session."""
    create_database()
    yield


@pytest.fixture(autouse=True)
def _clean_db(_schema):
    """Empty every table before and after each test for full isolation."""
    _wipe()
    yield
    _wipe()


# --- low-level DB handle -------------------------------------------------------------
@pytest.fixture
def db():
    """A DatabaseConnection wrapper; closed automatically after the test."""
    conn = get_db_connection()
    yield conn
    try:
        conn.close()
    except Exception:
        pass


# --- factory fixtures ----------------------------------------------------------------
@pytest.fixture
def make_material():
    """Insert a raw material; returns its material_id.

    Defaults to a mass material measured in grams (the canonical base unit), so quantities
    passed to make_lot/recipes are already in base units and need no conversion.
    """
    def _make(name, *, category="Tea", unit="g", reorder_level=0,
              dimension="mass", is_edible=True, is_organic=False, is_housemade=False):
        mid = add_raw_material(
            name=name, category=category, unit=unit, reorder_level=reorder_level,
            is_housemade=is_housemade, is_edible=is_edible, is_organic=is_organic,
            dimension=dimension,
        )
        assert isinstance(mid, int), f"add_raw_material returned {mid!r} for {name!r}"
        return mid
    return _make


@pytest.fixture
def make_lot():
    """Insert a lot for a material; returns its lot_id. Quantities are in the material's base unit."""
    _counter = {"n": 0}

    def _make(material_id, quantity, *, lot_number=None, received_date="2024-01-01",
              expiry_date=None, location=None, supplier=None, cost_per_unit=None):
        _counter["n"] += 1
        if lot_number is None:
            lot_number = f"LOT-{material_id}-{_counter['n']}"
        lid = receive_lot(
            material_id, lot_number, quantity, received_date,
            expiry_date=expiry_date, location=location, supplier=supplier,
            cost_per_unit=cost_per_unit,
        )
        assert isinstance(lid, int), f"receive_lot returned {lid!r}"
        return lid
    return _make


@pytest.fixture
def make_recipe():
    """Insert a recipe; returns its recipe_id.

    `materials` is a list of {'material_name', 'quantity_needed'[, 'unit']} dicts.
    quantity_needed is in the material's base unit (grams for mass), as the route would store it.
    """
    def _make(product_name, materials, *, notes=None, product_unit="g",
              product_dimension="mass", batch_type="finished"):
        rid = add_recipe(
            product_name, materials, notes=notes, product_unit=product_unit,
            product_dimension=product_dimension, batch_type=batch_type,
        )
        assert isinstance(rid, int), f"add_recipe returned {rid!r}"
        return rid
    return _make


# --- HTTP client ---------------------------------------------------------------------
def _auth_header(username=_TEST_USER, password=_TEST_PASS):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def auth():
    """HTTP Basic auth header dict for the test credentials."""
    return _auth_header()


@pytest.fixture
def client():
    """Flask test client with CSRF and rate limiting disabled for deterministic tests."""
    from app import app, limiter
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    limiter.enabled = False
    with app.test_client() as c:
        yield c


# --- server-timezone-vs-business-timezone fix ------------------------------------------
# config.business_today()/business_now() are what every planned-batch and lot-expiration date
# check now goes through instead of bare datetime.now(). Both call `datetime.now(tz)` using the
# `datetime` name bound in config.py's own module globals — since `from config import
# business_today` only imports the function object (not a copy), patching config.datetime here
# freezes the clock for every caller across the app (app.py, services/*), regardless of which
# module imported the function.
@pytest.fixture
def freeze_business_time(monkeypatch):
    """Freeze the business-timezone clock to a specific UTC instant.

    Usage: freeze_business_time(datetime(2026, 1, 2, 6, 0, 0, tzinfo=ZoneInfo("UTC")))
    Pick a UTC instant deliberately near the Pacific day boundary to exercise the bug this
    fixture exists to catch: business_today() must return Pacific's day, not UTC's.
    """
    import config

    def _freeze(utc_instant):
        class _Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return utc_instant.astimezone(tz) if tz else utc_instant
        monkeypatch.setattr(config, "datetime", _Frozen)

    return _freeze
