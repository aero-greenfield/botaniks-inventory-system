# ADDED: these imports were all at the top of inventory_app.py — only the ones
#        actually used by the functions in this file are kept here.
from database import get_db_connection
import pandas as pd
import logging
from config import business_now  # lot-expiration check in check_negative_stock — see config.py

# ADDED: component-material-on-recipe feature — creating a Component (mix) recipe now
#        pre-creates its house-made raw_material (stock 0) so a finished recipe can reference
#        the component BEFORE any Component batch has been produced. We derive the material's
#        dimension from the recipe's declared product_dimension, falling back to inferring it
#        from the product_unit text (same fallback _create_housemade_lot in batches.py uses).
from services import units
from services import order_by, search_where

# sort/search feature — whitelist (URL key → SQL) for the recipe list pages.
_RECIPE_SORT_COLS = {
    "name": "r.product_name",
    "added": "r.recipe_id",
    "type": "r.batch_type",
}

# ADDED: get_raw_material lives in services.materials. recipes.py calls it inside
#        add_recipe, change_recipe, update_recipe, and check_negative_stock to
#        validate that a material exists before inserting into recipe_materials.
# REMOVED: get_raw_material import — add_recipe/change_recipe/update_recipe now batch-fetch
#          all material names in one query instead of calling this per ingredient
# REMOVED: get_material_stock_from_lots import — check_negative_stock now uses an inline
#          batch GROUP BY query instead of calling this per ingredient


# ========================
# RECIPE FUNCTIONS
# ========================


def get_recipe_names(q=""):
    # ADDED: lightweight name-search for the /api/recipes autocomplete endpoint.
    #        Returns up to 50 recipes matching the search string as {name, batch_type} dicts,
    #        so the autocomplete dropdown can show a type badge next to each product name.
    #        Filtering happens in SQL, not by fetching the full recipes table into Python.
    db = get_db_connection()
    cursor = db.cursor()
    try:
        db.execute(cursor,
            "SELECT product_name, batch_type FROM recipes WHERE LOWER(product_name) LIKE LOWER(%s) ORDER BY product_name LIMIT 50",
            (f"%{q}%",))
        return [{'name': row[0], 'batch_type': row[1] or 'finished'} for row in cursor.fetchall()]
    except Exception as e:
        logging.error(f"get_recipe_names: {e}")
        return []
    finally:
        db.close()


def get_recipe_unit(name):
    # ADDED: lightweight lookup of a recipe's product unit-of-measurement by product name.
    #        Used by the /api/recipe-unit endpoint to show the product's unit next to the
    #        quantity field on the create/edit batch pages. Single indexed SELECT, no JOIN,
    #        mirroring get_material_by_name in services/materials.py.
    db = get_db_connection()
    cursor = db.cursor()
    try:
        db.execute(cursor,
            "SELECT product_unit FROM recipes WHERE LOWER(product_name) = LOWER(%s) LIMIT 1",
            (name,))
        row = cursor.fetchone()
        return row[0] if row else None
    except Exception as e:
        logging.error(f"get_recipe_unit: {e}")
        return None
    finally:
        db.close()


def get_recipe_batch_type(name):
    # ADDED: batch-type-on-recipe feature — look up the batch type a recipe produces
    #        ('finished' or 'mix') by product name. The create-batch route uses this so the
    #        operator no longer picks the type per batch — it's resolved from the recipe.
    #        Single indexed SELECT, mirroring get_recipe_unit. Returns None if no recipe.
    db = get_db_connection()
    cursor = db.cursor()
    try:
        db.execute(cursor,
            "SELECT batch_type FROM recipes WHERE LOWER(product_name) = LOWER(%s) LIMIT 1",
            (name,))
        row = cursor.fetchone()
        return row[0] if row else None
    except Exception as e:
        logging.error(f"get_recipe_batch_type: {e}")
        return None
    finally:
        db.close()


def get_recipe_unit_and_dimension(name):
    # ADDED: units-conversion-layer feature — fetch a recipe's product_unit AND its declared
    #        product_dimension ('mass'/'count'). Used by batches.py when a mix/component batch
    #        turns the recipe into a housemade material, so the material's dimension is taken
    #        from what the user declared rather than guessed from the product_unit text.
    #        Returns (product_unit, product_dimension); either may be None.
    db = get_db_connection()
    cursor = db.cursor()
    try:
        db.execute(cursor,
            "SELECT product_unit, product_dimension FROM recipes WHERE LOWER(product_name) = LOWER(%s) LIMIT 1",
            (name,))
        row = cursor.fetchone()
        return (row[0], row[1]) if row else (None, None)
    except Exception as e:
        logging.error(f"get_recipe_unit_and_dimension: {e}")
        return (None, None)
    finally:
        db.close()


def get_recipe(product_name):
    """
    Gets recipe from recipes, which refrences recipe materials

    """

    db = get_db_connection()
    cursor = db.cursor()

    try:

        db.execute(cursor,"""
        SELECT recipe_id
        FROM recipes
        WHERE LOWER(product_name) = LOWER(%s)
                       """,(product_name,))
        row = cursor.fetchone() # get recipe_id from product name

        if not row:
            print(f"{product_name} not found in recipes")
            return None
        recipe_id = row[0]

        # CHANGED: units-conversion-layer feature — also return the material's dimension so the
        #          create-batch lot UI can label gram-denominated amounts with the correct base
        #          unit ('g' for mass) instead of the material's display unit.
        query = ("""
        SELECT r.product_name,
        r.notes,
        rm.material_id,
        raw.name AS material_name,
        rm.quantity_needed,
        raw.unit AS unit,
        rm.unit AS line_unit,
        raw.dimension AS dimension
        FROM recipes r
        JOIN recipe_materials rm ON r.recipe_id = rm.recipe_id
        JOIN raw_materials raw ON rm.material_id = raw.material_id
        WHERE r.recipe_id = %s
        ORDER BY rm.material_name ASC
        """)

        db.execute(cursor, query, (recipe_id,))
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        df = pd.DataFrame(rows, columns=columns)

        return df




    except Exception as e:
        logging.error(f"Error: {e} \ngetting recipe:{product_name}.")
        return None

    finally:
        db.close()


def check_negative_stock(product_name, quantity):
    """
    Returns a list of dicts for materials that would go negative if this batch was created.
    Each dict has: material_name, current_stock, required_amount, resulting_stock.
    Returns [] if all materials are sufficient, recipe is missing, or any lookup fails.
    """
    recipe_df = get_recipe(product_name)
    if recipe_df is None or recipe_df.empty:
        return []

    # ISSUE: the original loop called get_material_stock_from_lots(material_id) once per
    #        ingredient row. Each call opened its own DB connection and ran SELECT SUM(...).
    #        A 10-ingredient recipe = 11 total connections and 11 round-trips.
    # FIX: collect all material_ids, fetch their stock totals in one GROUP BY query,
    #      build a dict, then loop the DataFrame with no DB calls inside.

    # ADDED: gather all material ids so we can batch-fetch stock in one query
    material_ids = recipe_df['material_id'].tolist()
    placeholders = ', '.join(['%s'] * len(material_ids))  # one %s per id; DatabaseConnection.execute converts to ? for SQLite

    db = get_db_connection()
    cursor = db.cursor()
    try:
        date_now = business_now().strftime('%Y-%m-%d %H:%M:%S')  # CHANGED: server-timezone-vs-business-timezone fix
        # ADDED: single GROUP BY query replacing N per-material SUM queries
        db.execute(cursor, f"""
            SELECT material_id, COALESCE(SUM(quantity), 0)
            FROM raw_material_lots
            WHERE material_id IN ({placeholders})
              AND status = 'active'
              AND (expiration_date IS NULL OR expiration_date > %s)
            GROUP BY material_id
        """, (*material_ids, date_now))
        # float() coerces Decimal (PostgreSQL numeric) → float so it mixes with the float `quantity`
        stock_map = {row[0]: float(row[1]) for row in cursor.fetchall()}  # ADDED: {material_id: stock}
    except Exception as e:
        import logging as _log
        _log.error(f"check_negative_stock: batch stock query failed: {e}")
        return []
    finally:
        db.close()

    negative = []
    for _, row in recipe_df.iterrows():
        material_name = row['material_name']
        material_id = row['material_id']
        # float() guards against Decimal (PostgreSQL numeric) * float (quantity) TypeError
        required_amount = float(row['quantity_needed']) * quantity

        # REMOVED: get_material_stock_from_lots(material_id) — opened a new connection each iteration
        # current_stock = get_material_stock_from_lots(material_id)

        # ADDED: dict lookup — no DB call
        current_stock = stock_map.get(material_id, 0.0)
        if current_stock < required_amount:
            negative.append({
                'material_name': material_name,
                'current_stock': current_stock,
                'required_amount': required_amount,
                'resulting_stock': current_stock - required_amount,
            })
    return negative


def get_all_recipes(page=None, per_page=50, sort_by=None, sort_dir='asc', q=None):
    """
    Returns all recipes and their materials as a DataFrame.

    Columns returned:
    - recipe_product_name: Name of the recipe/product
    - notes: Optional notes about the recipe
    - material_name: Name of the material/ingredient
    - quantity: Amount of material needed per batch
    - unit: Unit of measurement (from raw_materials table)

    # ADDED: page/per_page pagination parameters. Pagination is at the RECIPE level, not the
    #        ingredient-row level, so a recipe is never split across page boundaries.
    # page=None → full table, no LIMIT (used by export routes); returns (df, None)
    # page=int  → recipes N through N+per_page and all their ingredients; returns (df, total_recipe_count)
    #
    # CHANGED: was pd.read_sql_query(query, db.conn) — converted to cursor approach so the
    #          %s→? wrapper handles both SQLite and PostgreSQL consistently.
    """
    db = get_db_connection()
    cursor = db.cursor()
    columns = ['recipe_product_name', 'notes', 'product_unit', 'batch_type', 'material_name', 'quantity', 'unit']

    try:
        # CHANGED: INNER JOIN -> LEFT JOIN so a recipe still appears even when it has no
        #          materials, or its materials have a NULL/orphaned material_id. Previously
        #          such recipes were counted by "COUNT(*) FROM recipes" (the header total)
        #          but silently dropped from the table — the count and the list disagreed.
        full_query = """
        SELECT r.product_name AS recipe_product_name, r.notes, r.product_unit, r.batch_type,
               raw.name AS material_name, rm.quantity_needed AS quantity,
               -- CHANGED: units-conversion-layer feature — show the unit the line was entered
               -- in (recipe_materials.unit), falling back to the material's unit for old rows.
               -- quantity_needed is stored in grams; the route converts it back to this unit.
               COALESCE(rm.unit, raw.unit) AS unit
        FROM recipes r
        LEFT JOIN recipe_materials rm ON r.recipe_id = rm.recipe_id
        LEFT JOIN raw_materials raw ON rm.material_id = raw.material_id
        ORDER BY r.product_name ASC, raw.name ASC
        """
        # Note: unit comes from raw_materials (raw.unit), NOT recipe_materials

        if page is None:
            # ADDED: page=None → full table without LIMIT (for exports)
            db.execute(cursor, full_query)
            result = cursor.fetchall()
            return (pd.DataFrame(result, columns=columns), None)

        # ADDED: total distinct recipe count for pagination metadata
        # sort/search feature — same WHERE on the count so page numbers match the filtered list.
        where, where_params = search_where(["r.product_name"], q)
        where_sql = f"WHERE {where}" if where else ""
        order_clause = order_by(_RECIPE_SORT_COLS, sort_by, sort_dir, "r.product_name ASC", "r.recipe_id DESC")
        db.execute(cursor, f"SELECT COUNT(*) FROM recipes r {where_sql}", where_params or None)
        total = cursor.fetchone()[0]

        # ADDED: paginate at the recipe level using a subquery so no recipe is split across pages.
        #        Inner SELECT gets the recipe_ids for this page; outer JOIN fetches all their ingredients.
        # Inner and outer ORDER BY must match, or the page's recipes come back reshuffled.
        paginated_query = f"""
        SELECT r.product_name AS recipe_product_name, r.notes, r.product_unit, r.batch_type,
               raw.name AS material_name, rm.quantity_needed AS quantity,
               -- CHANGED: units-conversion-layer feature — show the unit the line was entered
               -- in (recipe_materials.unit), falling back to the material's unit for old rows.
               -- quantity_needed is stored in grams; the route converts it back to this unit.
               COALESCE(rm.unit, raw.unit) AS unit
        FROM recipes r
        LEFT JOIN recipe_materials rm ON r.recipe_id = rm.recipe_id
        LEFT JOIN raw_materials raw ON rm.material_id = raw.material_id
        WHERE r.recipe_id IN (
            SELECT r.recipe_id FROM recipes r {where_sql} {order_clause} LIMIT %s OFFSET %s
        )
        {order_clause}, raw.name ASC
        """
        db.execute(cursor, paginated_query, where_params + (per_page, (page - 1) * per_page))
        result = cursor.fetchall()
        return (pd.DataFrame(result, columns=columns), total)

    except Exception as e:
        logging.error(f"Error getting all recipes: {e}")
        return (pd.DataFrame(), 0)

    finally:
        db.close()


def add_recipe(product_name, materials, notes=None, product_unit=None, product_dimension=None, batch_type='finished'):

    db = get_db_connection()
    cursor = db.cursor()


    """
    Adds a new recipe to the database.

    Parameters:
        product_name (str): Name of the product.
        materials (list of dict): Each dict contains 'material_name' and 'quantity_needed'.
        notes (str, optional): Additional notes for the recipe.

    Example:
        materials = [
            {'material_name': 'Matcha Powder', 'quantity_needed': 10},
            {'material_name': 'Milk', 'quantity_needed': 200}
        ]
        add_recipe('Matcha Latte', materials, notes='Sweetened')
    """

    try:

        db.execute(cursor,"""
        INSERT INTO recipes (product_name, notes, product_unit, product_dimension, batch_type)
        VALUES (%s, %s, %s, %s, %s)
         """,(product_name, notes, product_unit, product_dimension, batch_type))
        recipe_id = db.get_last_insert_id(cursor) # get recipe_id, able to add to recipe_materials

        # ISSUE: the loop below called get_raw_material(name) once per material — each call
        #        opened its own DB connection (SELECT on raw_materials) and closed it.
        #        10 ingredients = 10 extra connections inside a function that already has one open.
        # FIX: batch-fetch all material names in one query on the already-open connection,
        #      build a name→id dict, then resolve ids from the dict inside the loop.

        # ADDED: collect all names so we can look them all up in one query
        names = [m["material_name"] for m in materials]
        placeholders = ', '.join(['%s'] * len(names))  # one %s per name; wrapper converts to ? for SQLite
        db.execute(cursor, f"""
            SELECT material_id, name
            FROM raw_materials
            WHERE LOWER(name) IN ({placeholders})
        """, [n.lower() for n in names])
        # ADDED: {lowercase_name: material_id} lookup dict — replaces per-name get_raw_material calls
        material_lookup = {row[1].lower(): row[0] for row in cursor.fetchall()}

        for material in materials:
            material_name = material["material_name"]
            quantity_needed = material['quantity_needed']
            # ADDED: units-conversion-layer feature — `unit` is the entry unit the amount was
            #        typed in; `quantity_needed` is already stored in the material's base unit
            #        (grams for mass) by the route, so batch deduction needs no conversion.
            unit = material.get('unit')

            # REMOVED: get_raw_material(material_name) — opened a new connection per iteration
            # material_info = get_raw_material(material_name)

            # ADDED: dict lookup — no extra connection or query
            material_id = material_lookup.get(material_name.lower())
            if material_id is None:
                logging.error(f"Material '{material_name}' not found in raw_materials while adding recipe '{product_name}'.")
                raise ValueError(f"Material '{material_name}' not found in raw_materials. Please add it to inventory before creating the recipe.")

            db.execute(cursor,"""
            INSERT INTO recipe_materials (
                   recipe_id,
                   material_name,
                   material_id,
                   quantity_needed,
                   unit)
            VALUES (%s,%s,%s,%s,%s)
              """,(recipe_id, material_name, material_id, quantity_needed, unit))


        db.commit()
        print(f"Recipe '{product_name}' added successfully with {len(materials)} materials.")
        return recipe_id



    except Exception as e:
        logging.error(f"Error: {e}")
        db.rollback()
        return None

    finally:
        db.close()





def get_all_recipes_with_id(page=None, per_page=50, sort_by=None, sort_dir='asc', q=None):
    """
    returns all recipes with their recipe_id,
    used for manage page when editing recipe,
    to get recipe id from product name.

    # ADDED: page/per_page pagination parameters. One row per recipe (DISTINCT), so
    #        simple LIMIT/OFFSET is safe here — no risk of splitting a recipe across pages.
    # page=None → full table; returns (df, None)
    # page=int  → paginated slice; returns (df, total_recipe_count)
    #
    # CHANGED: was pd.read_sql_query(query, db.conn) — converted to cursor approach so
    #          LIMIT/OFFSET params go through the %s→? wrapper consistently.
    """

    db = get_db_connection()
    cursor = db.cursor()
    columns = ['recipe_id', 'product_name', 'notes', 'batch_type']

    try:
        # CHANGED: dropped the INNER JOINs to recipe_materials/raw_materials. They excluded
        #          recipes with no valid materials, which meant a broken recipe couldn't be
        #          listed here to be edited or deleted. The manage page needs EVERY recipe.
        where, where_params = search_where(["r.product_name"], q)
        where_sql = f"WHERE {where}" if where else ""
        order_clause = order_by(_RECIPE_SORT_COLS, sort_by, sort_dir, "r.product_name ASC", "r.recipe_id DESC")
        base_query = f"""
        SELECT r.recipe_id, r.product_name, r.notes, r.batch_type
        FROM recipes r
        {where_sql}
        {order_clause}
        """

        if page is None:
            # ADDED: full table path — no LIMIT/OFFSET
            db.execute(cursor, base_query, where_params or None)
            result = cursor.fetchall()
            return (pd.DataFrame(result, columns=columns), None)

        # ADDED: total distinct recipe count for pagination metadata
        db.execute(cursor, f"SELECT COUNT(*) FROM recipes r {where_sql}", where_params or None)
        total = cursor.fetchone()[0]

        # ADDED: LIMIT/OFFSET for the requested page
        db.execute(cursor, base_query + " LIMIT %s OFFSET %s", where_params + (per_page, (page - 1) * per_page))
        result = cursor.fetchall()
        return (pd.DataFrame(result, columns=columns), total)

    except Exception as e:
        logging.error(f"Error getting all recipes with id: {e}")
        return (pd.DataFrame(), 0)

    finally:
        db.close()


def get_recipe_by_id(recipe_id):
    """
    gets recipe by id, used for managment page,

    returns full recipe details, including materials and quantities,
    given recipe_id, used for manage page when editing recipe,
    to get recipe details from recipe id.

    """
    db = get_db_connection()
    cursor = db.cursor()

    try:
        # CHANGED: INNER JOIN -> LEFT JOIN. With INNER JOINs a recipe whose materials are
        #          orphaned (material_id NULL or not in raw_materials) returned zero rows,
        #          so the edit page 404'd ("Recipe Not Found") and the recipe could not be
        #          fixed. LEFT JOIN keeps the recipe row; COALESCE falls back to the name
        #          stored on recipe_materials so the orphaned material is shown and editable.
        query = ("""
       SELECT r.recipe_id,
       r.product_name,
       r.notes,
       r.product_unit,
       r.product_dimension,
       r.batch_type,
       rm.material_id,
       COALESCE(raw.name, rm.material_name) AS material_name,
       rm.quantity_needed,
       rm.unit,
       raw.unit AS material_display_unit
       FROM recipes r
       LEFT JOIN recipe_materials rm ON rm.recipe_id = r.recipe_id
       LEFT JOIN raw_materials raw ON rm.material_id = raw.material_id
       WHERE r.recipe_id = %s
        """)

        db.execute(cursor, query, (recipe_id,))
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        df = pd.DataFrame(rows, columns=columns)

        return df

    except Exception as e:
        logging.error(f"Error: {e} \ngetting recipe by id:{recipe_id}.")
        return None

    finally:
        db.close()


def update_recipe(recipe_id, product_name=None, materials=None, notes=None, product_unit=None, product_dimension=None, batch_type=None):
    """
    changes a pre exisitng recipe

    Parameters:
        recipe_id (int): The ID of the recipe to change.
        materials (list of dict): Each dict contains 'material_name' and 'quantity_needed'.
        notes (str, optional): Additional notes for the recipe.

    Example:
        materials = [
            {'material_name': 'Matcha Powder', 'quantity_needed': 10},
            {'material_name': 'Milk', 'quantity_needed': 200}
        ]
        change_recipe('Matcha Latte', materials, notes='Sweetened')



        """

    db = get_db_connection()
    cursor = db.cursor()

    try:


        # for changing notes and product unit of measurement
        db.execute(cursor,"""
        UPDATE recipes
        SET notes = %s,
            product_unit = %s,
            product_dimension = %s,
            batch_type = %s
        WHERE recipe_id = %s

                """,(notes, product_unit, product_dimension, batch_type, recipe_id,))

        if product_name is not None:
            db.execute(cursor, """
            UPDATE recipes
            SET product_name = %s
            WHERE recipe_id = %s
            """, (product_name, recipe_id))


        db.execute(cursor,"""
        DELETE FROM recipe_materials
        WHERE recipe_id = %s
                       """,(recipe_id,)) # delete old materials

        # ISSUE: loop called get_raw_material(name) once per material — new connection each time.
        # FIX: batch-fetch all names on the already-open connection, resolve from dict in the loop.

        # ADDED: one batch SELECT replacing N per-name get_raw_material calls
        names = [m["material_name"] for m in materials]
        placeholders = ', '.join(['%s'] * len(names))
        db.execute(cursor, f"""
            SELECT material_id, name
            FROM raw_materials
            WHERE LOWER(name) IN ({placeholders})
        """, [n.lower() for n in names])
        material_lookup = {row[1].lower(): row[0] for row in cursor.fetchall()}

        for material in materials:
            material_name = material["material_name"]
            quantity_needed = material['quantity_needed']
            # ADDED: units-conversion-layer feature — entry unit; quantity_needed is in base units.
            unit = material.get('unit')

            # REMOVED: get_raw_material(material_name) — opened a new connection per iteration
            # material_info = get_raw_material(material_name)

            # ADDED: dict lookup — no extra connection or query
            material_id = material_lookup.get(material_name.lower())
            # CHANGED: was a print()-and-continue, which inserted a recipe_materials row with
            #          material_id = NULL. That row is then dropped by the raw_materials JOIN on
            #          the recipes page, hiding the whole recipe. Raise instead (matches add_recipe)
            #          so the bad edit is rejected and rolled back rather than silently corrupting.
            if material_id is None:
                raise ValueError(f"Material '{material_name}' not found in raw_materials. Please add it to inventory before updating the recipe.")

            db.execute(cursor,"""
            INSERT INTO recipe_materials (

            recipe_id,
            material_name,
            material_id,
            quantity_needed,
            unit)
            VALUES (%s,%s,%s,%s,%s)
                """, (recipe_id, material_name, material_id, quantity_needed, unit))


            if cursor.rowcount == 0:
                    raise ValueError(f"recipe ID {recipe_id} not found — nothing inserted")
        db.commit()
        logging.info(f"Recipe with recipe_id:{recipe_id} changed successfully with {len(materials)} materials.")
        return recipe_id


    except Exception as e:
        logging.error(f"Error: {e}")
        db.rollback()
        return None

    finally:
        db.close()


def delete_recipe_by_id(recipe_id):
    "deletes recipe by id"

    db = get_db_connection()
    cursor = db.cursor()

    try:
        db.execute(cursor, """
        DELETE FROM recipe_materials
        WHERE recipe_id = %s
        """, (recipe_id,))

        db.execute(cursor, """
        DELETE FROM recipes
        WHERE recipe_id = %s
        """, (recipe_id,))

        if cursor.rowcount == 0:
            raise ValueError(f"Recipe with id {recipe_id} not found.")

        db.commit()
        logging.info(f"Deleted recipe with id {recipe_id}")
        return True

    except Exception as e:
        logging.error(f"Error: {e}")
        db.rollback()
        return None

    finally:
        db.close()
