# ADDED: create_database() was in inventory_app.py but has nothing to do with
#        runtime inventory logic — it's a one-time SQLite schema initializer used
#        only in local dev. Moved here so the domain service files stay clean.


# NOTE: This function only for local SQLite - use init_db.py for PostgreSQL
def create_database():
    """Creates database with raw_materials, recipes, and ready_to_ship tables"""
    import sqlite3
    import os
    # SQLITE_PATH lets tests build the schema in an isolated throwaway DB; defaults to the
    # real local file (matches database.get_connection) so dev behaviour is unchanged.
    conn = sqlite3.connect(os.getenv('SQLITE_PATH', 'data/inventory.db'))
    cursor = conn.cursor()

    #Raw Materials

    #edits for lot number: deleted cost_per, quantity (will be gotten from SUM lot number)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS raw_materials(
                   material_id INTEGER PRIMARY KEY AUTOINCREMENT,
                   name TEXT NOT NULL UNIQUE,
                   category TEXT,
                   unit TEXT,
                   reorder_level REAL,
                   is_housemade BOOLEAN DEFAULT FALSE,
                   is_edible BOOLEAN DEFAULT TRUE,
                   is_organic BOOLEAN DEFAULT FALSE,
                   -- ADDED: units-conversion-layer feature — `dimension` is 'mass' or
                   -- 'count'. The existing `unit` column is the display unit shown to the
                   -- user (e.g. 'lb'); stored quantities are always in the canonical base
                   -- unit for the dimension (grams for mass, the material's own unit for counts).
                   dimension TEXT

                   )
                   """)

    # ADDED: organic feature — additive migration for pre-existing local SQLite DBs.
    #        The CREATE TABLE above only fires for brand-new DBs (IF NOT EXISTS), so existing
    #        installs need the new columns backfilled. SQLite has no ADD COLUMN IF NOT EXISTS,
    #        so we check PRAGMA table_info first and only ALTER when the column is missing.
    cursor.execute("PRAGMA table_info(raw_materials)")
    _rm_cols = {row[1] for row in cursor.fetchall()}
    if "is_edible" not in _rm_cols:
        cursor.execute("ALTER TABLE raw_materials ADD COLUMN is_edible BOOLEAN DEFAULT TRUE")
    if "is_organic" not in _rm_cols:
        cursor.execute("ALTER TABLE raw_materials ADD COLUMN is_organic BOOLEAN DEFAULT FALSE")

    # Shipments migration (add shipment_id FK to existing batches tables) lives further down,
    # after CREATE TABLE batches — see the guarded ALTER near the index-creation block. It must
    # not run here: on a fresh DB the batches table doesn't exist yet, so a copy here threw
    # "no such table: batches" before the table was created.

    #raw material lots.

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS raw_material_lots(
                   lot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                   material_id INTEGER,
                   lot_number TEXT,
                   quantity REAL,
                   received_date TEXT,
                   expiration_date TEXT,
                   status TEXT DEFAULT 'active',
                   supplier TEXT,
                   location TEXT,
                   cost_per_unit REAL,
                   FOREIGN KEY (material_id) REFERENCES raw_materials(material_id)
                   )
                   """)


    #Recipes
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS recipes(
                   recipe_id INTEGER PRIMARY KEY AUTOINCREMENT,
                   product_name TEXT NOT NULL,
                   notes TEXT,
                   product_unit TEXT,
    
                   product_dimension TEXT,

                   batch_type TEXT DEFAULT 'finished',
                   housemade_material_id INTEGER DEFAULT NULL


                   )
                   """)

    # ADDED: units-conversion-layer feature — additive migration for pre-existing SQLite DBs.
    cursor.execute("PRAGMA table_info(recipes)")
    _recipe_cols = {row[1] for row in cursor.fetchall()}
    if "product_dimension" not in _recipe_cols:
        cursor.execute("ALTER TABLE recipes ADD COLUMN product_dimension TEXT")
    # ADDED: batch-type-on-recipe feature — additive migration for pre-existing SQLite DBs.
    if "batch_type" not in _recipe_cols:
        cursor.execute("ALTER TABLE recipes ADD COLUMN batch_type TEXT DEFAULT 'finished'")
    # ADDED: edit-path safety fix — the house-made material a mix recipe produces into, so a recipe
    # rename can't split its component stock across two materials. Existing Postgres deploys:
    #     ALTER TABLE recipes ADD COLUMN IF NOT EXISTS housemade_material_id INTEGER DEFAULT NULL;
    if "housemade_material_id" not in _recipe_cols:
        cursor.execute("ALTER TABLE recipes ADD COLUMN housemade_material_id INTEGER DEFAULT NULL")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS recipe_materials(
                   recipe_material_id INTEGER PRIMARY KEY AUTOINCREMENT,
                   recipe_id INTEGER,
                   material_id INTEGER,
                   material_name TEXT,
                   quantity_needed REAL,
                   -- ADDED: units-conversion-layer feature — `unit` records the unit the
                   -- ingredient amount was entered in (for display/round-trip);
                   -- `quantity_needed` itself is stored in the material's base unit (grams
                   -- for mass) so batch deduction needs no conversion.
                   unit TEXT,
                   FOREIGN KEY (material_id) REFERENCES raw_materials(material_id),
                   FOREIGN KEY (recipe_id) REFERENCES recipes(recipe_id)

                   )
                   """)

    # Shipments (must come before batches FK reference)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS shipments(
                    shipment_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                    shipment_number TEXT NOT NULL UNIQUE,
                    date_shipped    TEXT NOT NULL,
                    destination     TEXT,
                    notes           TEXT,
                    category        TEXT
                   )
                   """)

    # Ready to ship
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS batches(
                    batch_id INTEGER PRIMARY KEY,
                    product_name TEXT NOT NULL,
                    -- CHANGED: fractional-quantity fix — was INTEGER, the only quantity column
                    -- in the schema that wasn't NUMERIC/REAL. Postgres ROUNDED a fractional
                    -- batch quantity on insert (18.75 -> 19) while the mix output lot kept the
                    -- unrounded value, desyncing the two at creation. SQLite never enforced the
                    -- affinity, which is why the test suite couldn't reproduce it.
                    quantity REAL,
                    date_completed TEXT,
                    status TEXT DEFAULT 'Ready',
                    notes TEXT,
                    batch_number TEXT,
                    date_shipped TEXT,
                    expiration_date TEXT,
                    planned_completion_date TEXT,
                    batch_type TEXT DEFAULT 'standard',
                    planned_lot_selections TEXT,
                    promotion_failure_reason TEXT,
                    deduction_mode TEXT DEFAULT 'immediate',
                    mix_lot_id INTEGER DEFAULT NULL REFERENCES raw_material_lots(lot_id),
                    shipment_id INTEGER DEFAULT NULL REFERENCES shipments(shipment_id)



                   )
                   """)

    # ADDED: planned-deduction-mode feature — additive migration for pre-existing SQLite DBs.
    # 'immediate' (deduct at creation) | 'deferred' (deduct at the planned completion date).
    # Only meaningful for Planned finished/mix batches; every other row stays 'immediate'.
    cursor.execute("PRAGMA table_info(batches)")
    _batch_cols = {row[1] for row in cursor.fetchall()}
    if "deduction_mode" not in _batch_cols:
        cursor.execute("ALTER TABLE batches ADD COLUMN deduction_mode TEXT DEFAULT 'immediate'")
        # Backfill is mandatory, not cosmetic: before this feature EVERY Planned finished/mix batch
        # deferred its deduction. Left at the 'immediate' default they would look already-deducted,
        # and promote_planned_batches would skip their deduction entirely — free stock, lost audit trail.
        cursor.execute("""
            UPDATE batches SET deduction_mode = 'deferred'
            WHERE status = 'Planned' AND batch_type IN ('finished', 'mix')
        """)

    # ADDED: fractional-quantity fix — batches.quantity changed INTEGER -> REAL/NUMERIC.
    # No SQLite migration is needed or possible here: SQLite doesn't enforce column affinity
    # (an INTEGER-declared column already stores 18.75 verbatim), and ALTER TABLE cannot change
    # a column's type anyway. Only PostgreSQL actually rounded on insert.
    # EXISTING Postgres deploys must be migrated by hand in the Supabase SQL editor, following
    # the same convention as deduction_mode above (see init_db.py) — this file only builds
    # fresh SQLite DBs:
    #     ALTER TABLE batches ALTER COLUMN quantity TYPE NUMERIC;
    # Then restore the three rows the INTEGER column had already rounded (the lot is the
    # authoritative side for these; the batch row is what got corrupted):
    #     UPDATE batches SET quantity = 37.5  WHERE batch_id = 83;   -- FTCC7513, was 38
    #     UPDATE batches SET quantity = 18.75 WHERE batch_id = 84;   -- FTCC7512, was 19
    #     UPDATE batches SET quantity = 18.75 WHERE batch_id = 85;   -- FTCC7510, was 19


    #Batch_materials
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS batch_materials(
                   batch_material_id INTEGER PRIMARY KEY AUTOINCREMENT,
                   batch_id INTEGER,
                   material_id INTEGER,
                   lot_id INTEGER,
                   quantity_used REAL,
                   cost_per_unit REAL, 
                   FOREIGN KEY (material_id) REFERENCES raw_materials(material_id),
                   FOREIGN KEY (batch_id) REFERENCES batches(batch_id),
                   FOREIGN KEY(lot_id) REFERENCES raw_material_lots(lot_id)


                   ) """)

    # Partial-batch shipments — junction table carrying the quantity of a batch on a shipment.
    # Replaces the one-batch-per-shipment model of batches.shipment_id: a batch can now appear on
    # several shipments, each row recording how much of it shipped. A batch's remaining quantity is
    # derived (batches.quantity - SUM(shipment_batches.quantity))
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS shipment_batches(
                   shipment_batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
                   shipment_id INTEGER NOT NULL,
                   batch_id INTEGER NOT NULL,
                   quantity REAL NOT NULL,
                   FOREIGN KEY (shipment_id) REFERENCES shipments(shipment_id),
                   FOREIGN KEY (batch_id) REFERENCES batches(batch_id)
                   )
                   """)

    #Audit Log
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS audit_log(
                   log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                   action TEXT NOT NULL,
                   details TEXT,
                   timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                   )""")




    # Indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_rml_material_id  ON raw_material_lots(material_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_rm_name_lower     ON raw_materials(LOWER(name))")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_recipe_mat_recipe ON recipe_materials(recipe_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bm_lot_id         ON batch_materials(lot_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bm_material_id    ON batch_materials(material_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sb_shipment_id    ON shipment_batches(shipment_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sb_batch_id       ON shipment_batches(batch_id)")

    # Save all table creations to the database
    conn.commit()
    conn.close()
    print(" Database created")
    upgrade_schema()


def upgrade_schema():
    """Adds indexes and missing columns to an existing SQLite database. Safe to run on a fresh DB."""
    import sqlite3
    import os
    conn = sqlite3.connect(os.getenv('SQLITE_PATH', 'data/inventory.db'))
    cursor = conn.cursor()
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_rml_material_id  ON raw_material_lots(material_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_rm_name_lower     ON raw_materials(LOWER(name))")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_recipe_mat_recipe ON recipe_materials(recipe_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bm_lot_id         ON batch_materials(lot_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bm_material_id    ON batch_materials(material_id)")
    try:
        cursor.execute("ALTER TABLE batches ADD COLUMN shipment_id INTEGER DEFAULT NULL REFERENCES shipments(shipment_id)")
        conn.commit()
    except Exception:
        conn.rollback()  # column already exists

    # ADDED: shipment-category feature — additive migration for pre-existing SQLite DBs.
    cursor.execute("PRAGMA table_info(shipments)")
    _shipment_cols = {row[1] for row in cursor.fetchall()}
    if "category" not in _shipment_cols:
        cursor.execute("ALTER TABLE shipments ADD COLUMN category TEXT")
        conn.commit()

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_shipments_date        ON shipments(date_shipped)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_batches_shipment_id   ON batches(shipment_id)")
    conn.commit()
    conn.close()
