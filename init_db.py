
""" 
Database Initialization Script 
 
PURPOSE: 
- Creates all tables in PostgreSQL when first deploying 
- Migrates schema from SQLite to PostgreSQL 
- Run this ONCE when setting up Railway 
 
WHEN TO RUN: 
- First Railway deployment (PostgreSQL is empty) 
- After adding new tables 
- To reset database (careful - deletes all data!) 
 
HOW TO RUN: using railway posgresql database
- Railway dashboard → Settings → Start Command → python init_db.py 
- Or locally: python init_db.py 


HOW TO RUN using subapase postgresql database
- Make sure you have python-dotenv installed: pip install python-dotenv
- Run locally: python init_db.py

""" 

##################new try except block for new load env variable#####################
## this is for SUPABASE POSTGRESQL DATABASE CONNECTION ## (new main database)

import os

try:
    from dotenv import load_dotenv
    load_dotenv()  # ← load .env FIRST
    print("🔍 Loaded environment variables from .env file")
except ImportError:
    print("🔍 python-dotenv not installed, skipping .env loading")
    pass

DATABASE_URL = os.getenv('DATABASE_URL')  # ← THEN read it



from database import get_connection




def init_database(): 
    """
    Creates all database tables.

    IMPORTANT DIFFERENCE FROM SQLite:
    - SQLite uses: INTEGER PRIMARY KEY AUTOINCREMENT
    - PostgreSQL uses: SERIAL PRIMARY KEY
    - SERIAL = auto-incrementing integer in PostgreSQL
    """
    

    conn = get_connection()
    cursor = conn.cursor()


    if DATABASE_URL:
        print("🔧 Using PostgreSQL database")

    else:
        print("🔧 Using SQLite database")

    print("📋 Creating tables...")

    # ========================================
    # TABLE 1: raw_materials
    # ========================================
    if DATABASE_URL:

   
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS raw_materials(
            material_id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            category TEXT,
            unit TEXT,
            reorder_level DOUBLE PRECISION,
            is_housemade BOOLEAN DEFAULT FALSE,
            -- ADDED: organic feature — is_edible flags whether a material counts toward a
            --        batch's organic determination; is_organic flags the material itself.
            is_edible BOOLEAN DEFAULT TRUE,
            is_organic BOOLEAN DEFAULT FALSE,
            -- ADDED: units-conversion-layer feature — 'mass' or 'count'. The existing
            --        `unit` column is the display unit (e.g. 'lb'); stored quantities are
            --        in the canonical base unit (grams for mass).
            dimension TEXT
        )
        """)

    else:

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS raw_materials(
            material_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            category TEXT,
            unit TEXT,
            reorder_level REAL,
            is_housemade BOOLEAN DEFAULT FALSE,
            -- ADDED: organic feature — is_edible flags whether a material counts toward a
            --        batch's organic determination; is_organic flags the material itself.
            is_edible BOOLEAN DEFAULT TRUE,
            is_organic BOOLEAN DEFAULT FALSE,
            -- ADDED: units-conversion-layer feature — see Postgres branch above.
            dimension TEXT
        )
        """)
    
   # What changed from SQLite? 
   # OLD: material_id INTEGER PRIMARY KEY AUTOINCREMENT 
   # NEW: material_id SERIAL PRIMARY KEY 
   # SERIAL = PostgreSQL's auto-increment 
   # Same functionality, different syntax 


   #=========================
   #Lot number table for raw materials.
   #=========================
    if DATABASE_URL:

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS raw_material_lots(
                lot_id SERIAL PRIMARY KEY,
                material_id INTEGER,
                lot_number TEXT,
                -- ADDED: units-conversion-layer feature — NUMERIC (not DOUBLE PRECISION) so
                --        gram quantities and SQL SUM/comparisons stay exact, killing the
                --        float-precision noise this feature exists to fix.
                quantity NUMERIC,
                received_date TEXT,
                expiration_date TEXT,
                status TEXT DEFAULT 'active',
                supplier TEXT,
                location TEXT,
                cost_per_unit DOUBLE PRECISION,
                FOREIGN KEY (material_id) REFERENCES raw_materials(material_id)
                        )
                        """)
    else:
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
    print("  ✅ raw_materials/lots table created")
    
   # ======================================== 
   # TABLE 2: recipes 
   # ======================================== 
   
    if DATABASE_URL:

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS recipes(
            recipe_id SERIAL PRIMARY KEY,
            product_name TEXT NOT NULL,
            notes TEXT,
            product_unit TEXT,
            
            product_dimension TEXT,
           
            batch_type TEXT DEFAULT 'finished'
        )
        """)
    else:
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS recipes(
            recipe_id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT NOT NULL,
            notes TEXT,
            product_unit TEXT,
            
            product_dimension TEXT,
           
            batch_type TEXT DEFAULT 'finished'
        )
        """)
    print("  ✅ recipes table created")
    
   # ======================================== 
   # TABLE 3: recipe_materials 
   # ======================================== 
   
    if DATABASE_URL:
    
        cursor.execute(""" 
        CREATE TABLE IF NOT EXISTS recipe_materials(
            recipe_material_id SERIAL PRIMARY KEY,
            recipe_id INTEGER,
            material_id INTEGER,
            material_name TEXT,
            -- ADDED: units-conversion-layer feature — NUMERIC for exact gram amounts;
            --        `unit` records the entry unit (quantity_needed itself is in grams).
            quantity_needed NUMERIC,
            unit TEXT,
            FOREIGN KEY (material_id) REFERENCES raw_materials(material_id),
            FOREIGN KEY (recipe_id) REFERENCES recipes(recipe_id)
        )
        """)
    else:
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS recipe_materials(
            recipe_material_id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER,
            material_id INTEGER,
            material_name TEXT,
            quantity_needed REAL,
            -- ADDED: units-conversion-layer feature — see Postgres branch above.
            unit TEXT,
            FOREIGN KEY (material_id) REFERENCES raw_materials(material_id),
            FOREIGN KEY (recipe_id) REFERENCES recipes(recipe_id)
        )
        """)
    
    print("  ✅ recipe_materials table created")
    
   # ========================================
   # TABLE 4: shipments
   # ========================================
   # NOTE: must be created BEFORE batches — batches has a FOREIGN KEY referencing
   #       shipments(shipment_id), and PostgreSQL requires the referenced table to
   #       already exist at CREATE time (a fresh init fails otherwise).

    if DATABASE_URL:
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS shipments(
            shipment_id     SERIAL PRIMARY KEY,
            shipment_number TEXT NOT NULL UNIQUE,
            date_shipped    TEXT NOT NULL,
            destination     TEXT,
            notes           TEXT,
            category        TEXT
        )
        """)
    else:
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

    print("  ✅ shipments table created")

   # ========================================
   # TABLE 5: batches
   # ========================================
    if DATABASE_URL:


        cursor.execute("""
        CREATE TABLE IF NOT EXISTS batches(
            batch_id SERIAL PRIMARY KEY,
            product_name TEXT NOT NULL,
            -- CHANGED: fractional-quantity fix — was INTEGER, the only quantity column in the
            -- schema that wasn't NUMERIC/REAL. On Postgres that ROUNDED a fractional batch
            -- quantity on insert (18.75 -> 19) while _create_housemade_lot() converted the
            -- unrounded float for the output lot, so a mix batch and its lot diverged at
            -- creation with no edit involved (3 production rows: batches 83/84/85).
            quantity NUMERIC,
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
            -- ADDED: planned-deduction-mode feature. 'immediate' | 'deferred'. Only meaningful for
            -- Planned finished/mix batches. EXISTING Postgres deploys are migrated by hand in the
            -- Supabase SQL editor (ALTER TABLE + backfill of Planned finished/mix rows to
            -- 'deferred'), NOT by upgrade_schema() — this CREATE TABLE only fires on fresh DBs.
            deduction_mode TEXT DEFAULT 'immediate',
            mix_lot_id INTEGER DEFAULT NULL REFERENCES raw_material_lots(lot_id),
            shipment_id INTEGER DEFAULT NULL REFERENCES shipments(shipment_id)
        )
        """)
    else:
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS batches(
            batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT NOT NULL,
            -- CHANGED: fractional-quantity fix — see the Postgres branch above. SQLite doesn't
            -- enforce INTEGER affinity so it stored 18.75 fine, which is exactly why the test
            -- suite could never reproduce the Postgres rounding. Declared REAL for parity.
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
            deduction_mode TEXT DEFAULT 'immediate',  -- ADDED: planned-deduction-mode feature
            mix_lot_id INTEGER DEFAULT NULL REFERENCES raw_material_lots(lot_id),
            shipment_id INTEGER DEFAULT NULL REFERENCES shipments(shipment_id)
        )
        """)

    print("  ✅ batches table created")

   # ========================================
   # TABLE 6: batch_materials
   # ========================================

    if DATABASE_URL:

        cursor.execute(""" 
        CREATE TABLE IF NOT EXISTS batch_materials( 
            batch_material_id SERIAL PRIMARY KEY,
            batch_id INTEGER,
            material_id INTEGER,
            lot_id INTEGER,
            quantity_used DOUBLE PRECISION,
            cost_per_unit DOUBLE PRECISION,
            FOREIGN KEY (material_id) REFERENCES raw_materials(material_id), 
            FOREIGN KEY (batch_id) REFERENCES batches(batch_id),
            FOREIGN KEY(lot_id) REFERENCES raw_material_lots(lot_id)              
        ) 
        """) 
    else:
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
        ) 
        """) 
        

    
    print("  ✅ batch_materials table created")

   # ========================================
   # TABLE 6b: shipment_batches (partial-batch shipments)
   # ========================================
   # Junction carrying the quantity of a batch on a shipment. A batch can appear on several
   # shipments; remaining is derived (batches.quantity - SUM(shipment_batches.quantity)).
    if DATABASE_URL:
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS shipment_batches(
            shipment_batch_id SERIAL PRIMARY KEY,
            shipment_id INTEGER NOT NULL REFERENCES shipments(shipment_id),
            batch_id INTEGER NOT NULL REFERENCES batches(batch_id),
            quantity DOUBLE PRECISION NOT NULL
        )
        """)
    else:
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS shipment_batches(
            shipment_batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
            shipment_id INTEGER NOT NULL REFERENCES shipments(shipment_id),
            batch_id INTEGER NOT NULL REFERENCES batches(batch_id),
            quantity REAL NOT NULL
        )
        """)

    print("  ✅ shipment_batches table created")


   #=============================
   # TABLE 6: audit-logs
   #============================

    if DATABASE_URL:



        cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            log_id SERIAL PRIMARY KEY,
            action TEXT NOT NULL,
            details TEXT,
            timestamp TIMESTAMPTZ DEFAULT NOW()
        )
        """)
    else:
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            details TEXT,
            timestamp TEXT DEFAULT (datetime('now'))
        )
        """)
   
    print("  ✅ audit_log table created")
   # ========================================
   # Save all changes
   # ========================================
    conn.commit()
    conn.close()

    print("\n🎉 Database initialized successfully!")
    print("📊 All tables created and ready to use")
    upgrade_schema()


def upgrade_schema():
    """Adds indexes and missing columns to an existing database. Safe to run on a fresh DB."""
    conn = get_connection()
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

    # MIGRATION: PostgreSQL REAL is single-precision float4 (~6-7 significant digits), so it
    #            silently rounds decimal inputs. CREATE TABLE IF NOT EXISTS never alters columns
    #            on tables that already exist, so existing Supabase deploys keep the old REAL type
    #            until this runs. Widen every numeric column to DOUBLE PRECISION (float8, ~15 digits),
    #            matching SQLite's REAL. Note: values already stored as float4 stay truncated — only
    #            NEW writes gain the extra precision. PostgreSQL only; SQLite REAL is already double.
    if DATABASE_URL:
        _real_to_double = [
            ("raw_materials",     "reorder_level"),
            ("raw_material_lots", "quantity"),
            ("raw_material_lots", "cost_per_unit"),
            ("recipe_materials",  "quantity_needed"),
            ("batch_materials",   "quantity_used"),
            ("batch_materials",   "cost_per_unit"),
        ]
        for table, column in _real_to_double:
            try:
                cursor.execute(
                    f"ALTER TABLE {table} ALTER COLUMN {column} TYPE DOUBLE PRECISION"
                )
                conn.commit()
            except Exception:
                conn.rollback()  # table/column missing or already double precision
        print("  ✅ Numeric columns widened to DOUBLE PRECISION")

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_shipments_date        ON shipments(date_shipped)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_batches_shipment_id   ON batches(shipment_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sb_shipment_id        ON shipment_batches(shipment_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sb_batch_id           ON shipment_batches(batch_id)")
    conn.commit()
    conn.close()
    print("  ✅ Indexes applied")
 
 
# ============================================================ 
# RUN THIS SCRIPT 
# ============================================================ 
if __name__ == "__main__":
 
    init_database()
    