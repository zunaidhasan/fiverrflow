# FiverrFlow

A Flask CRM for tracking Fiverr **presales** (leads) and **sold** orders, backed by Supabase Postgres.

Two linked pipelines: a lead is captured in `presales`, worked through the pipeline, and — once it closes — converted into a `sold` order that carries delivery dates, the assigned developer, and the order amount.

## Stack

| Piece | Choice |
|---|---|
| Web | Flask 3.0 + Jinja2 |
| Database | Supabase Postgres via raw `psycopg2` (no ORM) |
| Pooling | `psycopg2.pool.ThreadedConnectionPool` |
| Auth | Session cookies + Werkzeug `scrypt` hashes |
| Protection | Flask-WTF CSRF, Flask-Limiter |
| Frontend | Bootstrap 5 (CDN, SRI-pinned) + `static/css/app.css`, `static/js/app.js` |
| Serving | gunicorn (Linux) / waitress (Windows) |

## Features

- **Presales pipeline** — list and kanban views, drag-to-move stages, inline status/stage edits saved over `fetch` with revert-on-failure.
- **Sold orders** — delivery dates, overdue tracking, assigned leader/developer, order and bonus amounts, net-of-commission totals.
- **Convert lead → order** — `/leads/<id>/mark-sold` carries the lead across and links it via `sold.presale_id`.
- **Dashboard** — pipeline counts, quoted totals, revenue, overdue deliveries, recent activity.
- **Reports** — conversion rates, category breakdown, and monthly-lead performance.
- **CSV import/export** — for both presales and sold, with money parsing that tolerates `$6,000.00` and `6,000`.
- **Custom fields** — per-entity fields stored in a `JSONB` column, managed at `/settings/fields`.
- **Configurable stages** — add, reorder, and recolour pipeline stages at `/settings/stages`.
- **Team** — invite links, role changes (`admin` / `member`), removal.
- **Light/dark theme** — persisted to `localStorage`, pre-painted inline so there is no flash of the wrong theme.

### Pipeline stages

```
New Lead → Qualified → Proposal Sent → Negotiation → Closed Won / Closed Lost
```

Stages are rows in `pipeline_stages`, not constants — edit them in Settings.

## Setup

**1. Install dependencies**

```bash
pip install -r requirements.txt
```

**2. Configure the environment**

Copy `.env.example` to `.env` and fill it in. Generate `SECRET_KEY` with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

`DATABASE_URL` must be a Supabase Postgres URI. The app **refuses to start** when `APP_ENV=production` and `SECRET_KEY` is unset or still a placeholder — a forgeable session key is not something to discover in production.

**3. Apply migrations**

`migrations/000_baseline.sql` documents the existing schema and is **not** meant to be run against a live database. Apply only the numbered migrations after it:

```bash
psql "$DATABASE_URL" -f migrations/001_indexes.sql
```

Every statement is `IF NOT EXISTS`, so re-running is a no-op.

**4. Set a password**

Existing hashes are one-way, so passwords cannot be recovered — set a known one:

```bash
python scripts/set_password.py --email you@example.com --password 'your-password'
```

Add `--create --role admin` to make a new account. `--dry-run` shows what would change without writing.

**5. Run**

```bash
python app.py
```

To match production locally on Windows (gunicorn needs `fcntl` and will not run there):

```bash
python -m waitress --host=127.0.0.1 --port=8000 --threads=4 wsgi:application
```

With `APP_ENV=production` the session cookie is `Secure`, so it will not survive plain HTTP — test production config over HTTPS, or leave `APP_ENV=development` for local runs.

## Deployment

Targets a long-running host (Render, Railway, Fly), not serverless — the connection pool assumes a persistent process.

`render.yaml` and `Procfile` are both committed. On Render the blueprint is picked up automatically; set `DATABASE_URL` in the dashboard (it is marked `sync: false` so it is never committed) and let `SECRET_KEY` be generated.

```
gunicorn wsgi:application --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 60
```

Keep `DB_POOL_MAX` at or above the thread count per worker, and mind Supabase's own connection ceiling: `workers × threads` is the concurrency you are asking it to support.

Query helpers retry once after an `OperationalError` by rebuilding the pool — free-tier instances that sleep and wake with a stale pool recover on the next request instead of 500ing.

`/health` runs `SELECT 1` and returns `{"status":"ok","database":"up"}`, or 503 when the database is unreachable. It is wired to `healthCheckPath`.

## Routes

| Route | Purpose |
|---|---|
| `/` `/login` `/register` `/logout` | Auth. Login is rate-limited to 10/min, register to 5/hour. |
| `/dashboard` | Aggregates and recent activity |
| `/reports` | Conversion, category, and monthly performance |
| `/leads` | Presales list; `?view=kanban` for the board |
| `/leads/new` `/leads/<id>/edit` `/leads/<id>/delete` | Lead CRUD |
| `/leads/<id>/status` `/leads/<id>/stage` | JSON endpoints; require `X-CSRFToken` |
| `/leads/<id>/mark-sold` | Convert a lead into an order |
| `/clients` `/clients/new` `/clients/<id>/edit` `/clients/<id>/delete` | Sold orders |
| `/import` `/export/leads` `/export/clients` | CSV in and out |
| `/settings` `/settings/stages` `/settings/fields` | Pipeline and custom fields |
| `/team` `/team/invite` `/team/<uid>/role` `/team/<uid>/delete` | Members (admin only) |
| `/health` | Liveness probe |

## Schema

Seven tables: `users`, `presales`, `sold`, `activities`, `pipeline_stages`, `custom_fields`, `invitations`. Both `presales` and `sold` carry a `custom_data JSONB` column driven by `custom_fields`.

Full definitions are in `migrations/000_baseline.sql`, reverse-engineered from the live database.

Migrations are **forward-only and additive** — `ADD COLUMN`, `CREATE INDEX`, `ADD CONSTRAINT`. Nothing is dropped or recreated, because Supabase holds the only copy of the data.

## Security

- CSRF on every POST, including the JSON endpoints via `X-CSRFToken`.
- `HttpOnly` + `SameSite=Lax` session cookies; `Secure` in production.
- `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` on every response; HSTS in production.
- Rate limiting on login and register.
- Tracebacks are logged, never rendered — the browser gets a plain 500 page.
- Uploads capped at 5 MB.
- Both password schemes verify; legacy `salt:sha256` hashes are transparently upgraded to `scrypt` on next login.

`.env` is gitignored. If a database password is ever exposed, rotate it in Supabase under **Settings → Database → Reset password** and update `.env`.

## Scripts

| Script | Purpose |
|---|---|
| `scripts/set_password.py` | Set or create a user's password |
| `scripts/backfill_dates.py` | Recover `date` values from the legacy SQLite file by username |
| `scripts/dedupe_sold.py` | Find and remove duplicate `sold` rows, keeping the earliest |

Both data scripts **dry-run by default** and require `--apply` to write. `dedupe_sold.py` writes a CSV backup to `backups/` before deleting anything.

## Tests

Pure helpers (money/date parsing, URL normalization, password-verify, redirect
guards) are unit-tested and need no database:

```bash
pip install -r requirements-dev.txt
pytest -q
```

## Project layout

```
app.py              all routes, queries, and helpers
wsgi.py             gunicorn/waitress entry point
migrations/         ordered .sql, forward-only
scripts/            operational scripts, dry-run by default
static/css|js/      extracted assets, cache-busted by mtime
templates/          Jinja2, all extending base.html
tests/              pytest unit tests for pure helpers
instance/           legacy SQLite (gitignored, reference only)
.env.example        documented env vars — copy to .env and fill in
```

## Environment variables

| Variable | Purpose | Required |
|---|---|---|
| `DATABASE_URL` | Supabase pooler URI (sslmode forced to `require`, mangled query strings repaired) | Yes |
| `SECRET_KEY` | Session signing; app refuses to start in prod without it | Yes (prod) |
| `APP_ENV` | `production` forces Secure cookies + HSTS | No (default `development`) |
| `DB_POOL_MAX` | Pooled connections per worker (keep ≥ threads) | No (default `8`) |
| `COMMISSION_RATE` | Net revenue share, e.g. `0.8` for 80% | No (default `0.8`) |
