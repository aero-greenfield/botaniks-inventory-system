# Botaniks Inventory System

The inventory and production system a 6-person matcha and tea wholesaler runs on every day.

I started as a warehouse employee at Botaniks Herbs & Tea, where stock was tracked in
spreadsheets and by memory. That meant regular material shortages, manual counts, and
batch details checked by hand. I built a demo, pitched it to the owner, and was moved
off warehouse work to build it during paid hours as the only engineer.

**What it changed:**

- Replaced manual tracking of 70+ raw materials and 45 products
- Saves the team 5 to 6 hours a week
- Zero stockout-driven batch cancellations since the June 2026 launch, down from one every 1 to 2 months
- Clients take 350kg+ pallet orders weekly, so a missed batch is a missed shipment

**Stack:** Python, Flask, Gunicorn · PostgreSQL (prod) / SQLite (dev, test) · pandas, openpyxl ·
Jinja2, HTML/CSS, JavaScript · pytest, Hypothesis · Docker, GitHub Actions, Render, Supabase, Cloudflare R2

![Python](https://img.shields.io/badge/Python-3-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-black?logo=flask&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1?logo=postgresql&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-07405E?logo=sqlite&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white)
![pytest](https://img.shields.io/badge/pytest-0A9EDC?logo=pytest&logoColor=white)
![Render](https://img.shields.io/badge/Render-46E3B7?logo=render&logoColor=white)

## Table of Contents

- [Demo](#demo)
  - [Batches](#batches)
  - [Recipes](#recipes)
  - [Materials](#materials)
  - [Shipments](#shipments)
- [Features](#features)
- [Architecture](#architecture)
  - [Schema](#schema)
  - [System](#system)
- [Engineering highlights](#engineering-highlights)
- [Testing & Reliability](#testing--reliability)
- [Known limitations](#known-limitations)
- [Docs](#docs)

## Demo

<!-- Screenshots use seeded demo data only (scripts/seed_demo_data.py), never real inventory, costs, or supplier data. -->

*Screenshots use seeded demo data.*

<p align="center">
  <img src="docs/screenshots/dashboard.png" alt="Dashboard with stat cards, a needs-reordering table with Low status pills, and recent activity" width="100%">
</p>
<p align="center"><sub><b>Dashboard</b>: low-stock alerts and recent activity at a glance</sub></p>

### Batches
Standard, mix, and planned finished batches, with status at a glance.

![Batches page listing production batches by type and status](docs/screenshots/batches.png)

### Recipes
Per-product material requirements, checked against live lot stock when a batch is created.

![Recipes page listing products and their materials](docs/screenshots/recipes.png)

![New recipe form with material rows and quantities](docs/screenshots/new-recipe.png)

### Materials
Stock is tracked per lot: expand a material to see each receipt's quantity, expiration, location, and supplier. House-made mixes are tracked alongside purchased materials.

![Raw materials list with an expanded lot detail row and stock status pills](docs/screenshots/materials.png)

Reorder levels and units are set per material.

![Add a raw material form](docs/screenshots/add-material.png)

### Shipments
History of what went out, and a form to ship finished batches.

![Shipments page listing shipped batches](docs/screenshots/shipments.png)

![Create shipment form selecting batches to ship](docs/screenshots/create-shipment.png)

## Features

- **Track raw materials by lot, not just a total number.** Every receipt gets its own lot with its own quantity, cost, and expiration date, so stock on hand is always traceable back to exactly what came in and when.
- **Build batches two ways: FIFO or pick-your-own.** Making a batch pulls from the oldest lot first by default, but staff can manually choose which lots to draw from when it matters.
- **Plan batches ahead of time.** Schedule a batch for a future completion date and the system holds off deducting materials until then, promoting it to "Ready" automatically once the date hits and stock allows.
- **Make house-made intermediate products.** "Mix" batches become their own trackable material, so a finished batch can consume something made in-house just like a purchased ingredient.
- **See low-stock problems before they're a problem.** The dashboard flags materials at or below their reorder level.
- **Log shipments and get an audit trail for free.** Every batch, lot, and shipment action is logged automatically, and anything can be exported to Excel.
- **Convert units at the edges, store one base unit.** Receipts can be entered in lb, oz, or kg; everything is converted to grams once on input and stored at full precision. See [Engineering highlights](#engineering-highlights).
- **Define recipes per product.** Each recipe lists the materials and quantities a batch needs, and batch creation checks coverage against live lot stock before deducting.
- **Edit or delete a batch without losing the materials it drew.** Deleting a batch can reallocate its consumed lots back to stock instead of just orphaning the deduction.

## Architecture

### Schema

<!-- ER diagram of the core schema: materials/lots, recipes, batches, and shipments -->

Column types below are the SQLite dev/test schema (`services/setup.py`). Production Postgres (`init_db.py`) differs: `raw_material_lots.quantity` and `recipe_materials.quantity_needed` are `NUMERIC`; `batch_materials.quantity_used` and `shipment_batches.quantity` are `DOUBLE PRECISION`. See [Engineering Decisions #2](docs/engineering-decisions.md#2-the-floatdecimal-bug-and-the-units-layer).

```mermaid
erDiagram
    %% planning: what materials exist, and what a recipe needs
    raw_materials ||--o{ raw_material_lots : "received as"
    raw_materials ||--o{ recipe_materials : "required by"
    recipes       ||--o{ recipe_materials : "lists"

    %% execution: a batch deducts lots, then ships
    raw_material_lots ||--o{ batch_materials   : "deducted into"
    batches            ||--o{ batch_materials   : "consumes"
    raw_material_lots  ||--o| batches           : "produced by (mix batches)"
    batches            ||--o{ shipment_batches  : "ships as"
    shipments          ||--o{ shipment_batches  : "contains"

    raw_materials {
        int material_id PK
        string name
        string dimension
        bool is_housemade
    }
    raw_material_lots {
        int lot_id PK
        int material_id FK
        real quantity
        string status
    }
    recipes {
        int recipe_id PK
        string product_name
        string batch_type
    }
    recipe_materials {
        int recipe_material_id PK
        int recipe_id FK
        int material_id FK
        real quantity_needed
    }
    batches {
        int batch_id PK
        string product_name
        string batch_type
        string status
        int mix_lot_id FK
    }
    batch_materials {
        int batch_material_id PK
        int batch_id FK
        int lot_id FK
        real quantity_used
    }
    shipments {
        int shipment_id PK
        string shipment_number
        string date_shipped
    }
    shipment_batches {
        int shipment_batch_id PK
        int shipment_id FK
        int batch_id FK
        real quantity
    }
```

### System

Two independent flows: the live app, and the nightly backup pipeline.

```mermaid
flowchart LR
    user[User] --> render[Render]
    render --> w1[Gunicorn worker 1]
    render --> w2[Gunicorn worker 2]
    w1 --> flask[Flask app]
    w2 --> flask
    flask --> pg[(Supabase Postgres)]

    cron[GitHub Actions nightly cron] --> dump[pg_dump]
    dump --> verify[verify_backup_integrity]
    verify --> supa[Supabase Storage]
    verify --> r2[Cloudflare R2]
    verify --> hc[healthchecks.io ping]
```

Two Gunicorn workers are why the promotion race in [Engineering Decisions #1](docs/engineering-decisions.md#1-the-double-deduction-race-and-the-conditional-update-fix) is possible at all: both can reach the same page-load trigger at once.

## Engineering highlights

- **A page-load promotion race that silently double-deducted stock, fixed with a conditional `UPDATE ... WHERE status='Planned'` claim instead of an engine-specific row lock.** [Read more →](docs/engineering-decisions.md#1-the-double-deduction-race-and-the-conditional-update-fix)
- **A float/Decimal bug where two equal quantities failed a stock-coverage check**, fixed with one base unit, `Decimal` conversion math, and a shared tolerance instead of per-callsite epsilons. [Read more →](docs/engineering-decisions.md#2-the-floatdecimal-bug-and-the-units-layer)
- **Stock is a derived `SUM` over lots, not a stored counter**, so FIFO, per-lot expiry, and auditable per-batch cost all fall out of one source of truth instead of a value that can drift. [Read more →](docs/engineering-decisions.md#3-lot-based-stock-as-a-derived-sum)
- **Backups are verified before they count as a backup.** `pg_dump` exiting 0 doesn't mean the data made it in, so every dump is checked for size, schema, and row content, mirrored to a second vendor, and watched by a dead-man's-switch ping. [Read more →](docs/engineering-decisions.md#4-verified-backups-with-an-off-site-copy)
- **Three N+1 query patterns collapsed into single batched queries**, cutting a 10-ingredient recipe check from 11 database connections to 1. [Read more →](docs/engineering-decisions.md#5-n1-query-elimination)

## Testing & Reliability

211 tests across 8 files in `test_scripts/` (pytest + Hypothesis), run against an isolated throwaway SQLite database that's rebuilt per session and wiped between tests. It never touches `data/inventory.db` or production. Hypothesis property tests guard the unit-conversion layer (round-tripping, unit aliases, the float-storage boundary) against classes of error rather than hand-picked values. The double-promotion race is reproduced deterministically by replaying a captured post-race snapshot rather than racing real threads; see [Engineering Decisions #1](docs/engineering-decisions.md#1-the-double-deduction-race-and-the-conditional-update-fix) for how.

The suite runs on SQLite; production runs Postgres. It verifies application logic, not Postgres-specific behavior (`Decimal` return types, `LASTVAL()`, the connection pool). There is no database-matched CI. The real gate is a local pre-push hook (`git config core.hooksPath hooks`) that blocks `git push` if pytest or an app-boot smoke test fails; a GitHub Actions workflow re-runs the suite on push/PR as an async backstop, since Render deploys on push with nothing else in between.

## Known limitations

- **Promotion runs on page load, not a schedule.** A Planned batch is only checked when someone loads a page that calls `promote_planned_batches()`; the double-deduction race is fixed, but a batch can still sit past its due date if nobody visits. A real repair pass (`scripts/repair_double_promotion.py`) fixed batches already corrupted before the fix shipped. **Next:** a scheduled trigger, e.g. a GitHub Actions cron hitting an internal endpoint.
- **Tests run on SQLite; production runs Postgres.** No database-matched CI, so a Postgres-specific bug could only surface in production. **Next:** a Postgres service container in CI.
- **One shared login, and an audit log that can't name names.** All six staff share one HTTP Basic Auth credential; `audit_log` records what happened, not who did it. **Next:** per-user accounts and a `user_id` column on every audit row.
- **Backup verification checks shape, not restorability.** Automated checks catch an empty, truncated, or schema-only dump; a full restore has only been tested by hand (most recently 2026-10-03). **Next:** a scheduled restore drill in CI.
- **No response security headers yet** (CSP, X-Frame-Options, HSTS). Every route is behind `@requires_auth`, but header hardening is still open.
- **The UI has design-system debt.** Formatting helpers (`fmt_num`, `dash`, `humanize`, `fmt_date`) exist but aren't wired into most templates, and some status colors are hardcoded instead of using the shared CSS tokens.


## Docs

- [Engineering Decisions](docs/engineering-decisions.md): the promotion race, the units layer, derived stock, verified backups, and N+1 elimination, each with the full reasoning behind it.
