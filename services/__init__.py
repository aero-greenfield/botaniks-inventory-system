# ADDED: empty __init__.py makes 'services/' a Python package so other files can do
#        'from services.materials import ...' etc.


# sort/search feature — shared by every list page. `sort_by` and `q` arrive from the URL, so:
#   * sort_by is only ever used as a key into a hard-coded whitelist (never interpolated), and
#   * q only ever travels as a bound %s parameter.
def order_by(whitelist, sort_by, sort_dir, default, tiebreak):
    """ORDER BY clause for a whitelisted sort key, else `default`. Direction is ASC/DESC only.
    A whitelist value may be 'col IS NULL, col' so blanks sort last on SQLite and PostgreSQL alike."""
    if sort_by not in whitelist:
        return f"ORDER BY {default}"
    direction = 'DESC' if str(sort_dir).lower() == 'desc' else 'ASC'
    return f"ORDER BY {whitelist[sort_by]} {direction}, {tiebreak}"


def search_where(columns, q):
    """('(LOWER(a) LIKE LOWER(%s) OR ...)', params) for a partial, case-insensitive match on any
    of `columns`; ('', ()) when q is blank."""
    q = (q or '').strip()
    if not q:
        return '', ()
    # shortcut: % and _ typed by the user act as LIKE wildcards (still a bound param, so harmless); escape if anyone complains.
    sql = '(' + ' OR '.join(f"LOWER({c}) LIKE LOWER(%s)" for c in columns) + ')'
    return sql, (f"%{q}%",) * len(columns)
