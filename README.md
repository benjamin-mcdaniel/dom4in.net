# dom4in.net

dom4in.net is a domain market stats dashboard. It samples short domains (1–6 character labels) across several major TLDs and shows **aggregated** data only (no per-domain lists), similar to a stock market overview.

This repo has three main parts:

- `frontend/` – Cloudflare Pages static site (HTML/CSS/JS)
- `backend/` – Cloudflare Worker (via Wrangler) + D1 database schema
- `collector/` – Local Python collector that probes domains and uploads aggregates

---

## 1. Architecture overview

### Frontend (Cloudflare Pages)

- Static HTML/JS in `frontend/index.html`.
- Calls `GET /api/stats/overview` to fetch:
  - `domains_tracked_lifetime`
  - `domains_tracked_24h`
  - `last_updated_at`
  - `letter_length_counts` for 1–6 character labels.
- Renders:
  - KPI cards (lifetime and 24h counts)
  - A table of 1–6 character label stats (aggregated across TLDs)
  - A small nav (`Overview`, `Short domains`, `About`).

### Backend (Cloudflare Worker)

- Code: `backend/src/index.js`
- Config: `backend/wrangler.toml`
- D1 schema: `backend/db/schema.sql`

Endpoints:

- `GET /api/health`
  - Simple JSON `{ "status": "ok" }`.

- `GET /api/stats/overview`
  - Reads from D1 (`env.DB`):
    - `global_stats` (latest row by `date`)
    - `length_stats` for `tld = 'ALL'` and that `date`.
  - Returns JSON like:

    ```json
    {
      "domains_tracked_lifetime": 123456,
      "domains_tracked_24h": 7890,
      "last_updated_at": "2025-11-20T18:30:00Z",
      "letter_length_counts": [
        {
          "length": 1,
          "total_possible": 26,
          "tracked": 26,
          "unregistered_found": 5,
          "unused_found": 2
        }
      ]
    }
    ```

- `POST /api/admin/upload-aggregate`
  - **Internal** admin endpoint for the collector.
  - Auth: `x-admin-api-key` header must equal `env.ADMIN_API_KEY`.
  - Body format:

    ```json
    {
      "date": "2025-11-20",
      "global": {
        "domains_tracked_lifetime": 123456,
        "domains_tracked_24h": 7890
      },
      "length_stats": [
        {
          "length": 1,
          "total_possible": 26,
          "tracked_count": 26,
          "unregistered_found": 5,
          "unused_found": 2
        }
      ]
    }
    ```

  - Upserts into `global_stats` and replaces `length_stats` rows for that date + `tld = 'ALL'`.

- `POST /api/admin/reset-stats`
  - **Internal** admin endpoint to clear aggregates.
  - Auth: same `x-admin-api-key` check.
  - Deletes all rows from `global_stats`, `length_stats`, and `tld_stats`.

### Database (Cloudflare D1)

Defined in `backend/db/schema.sql`:

- `global_stats`
  - `date` (TEXT, ISO date or label like `overall`)
  - `domains_tracked_lifetime`
  - `domains_tracked_24h`
  - `created_at`, `updated_at`
  - Unique index on `date`

- `length_stats`
  - `snap_date` (TEXT)
  - `tld` (TEXT, `'ALL'` for aggregates across TLDs)
  - `length` (INTEGER, 1–6)
  - `total_possible` (INTEGER, e.g. `26^length` for labels a–z)
  - `tracked_count`, `unregistered_found`, `unused_found`
  - Unique index on `(snap_date, tld, length)`

- `tld_stats`
  - For future per-TLD summaries (currently not surfaced in the API/FE).

Apply schema to remote D1:

```bash
cd backend
wrangler d1 execute DOM4IN_DB --remote --file=db/schema.sql
```

---

## 2. Collector (local Python)

The collector runs **locally** and is responsible for:

- Generating candidate labels (currently 1–6 letters a–z) and combining them with a list of TLDs.
- Checking:
  - DNS via DNS-over-HTTPS (Cloudflare + Google by default).
  - HTTP (simple check of `https://<domain>`).
- Classifying each domain as:
  - `registered` / `unregistered`
  - `usage_state`: `no_website` | `parked_or_placeholder` | `active_site`
  - `product_state`: `active_product` | `unknown`
- Aggregating counts over the run.
- Uploading a single JSON aggregate to `POST /api/admin/upload-aggregate`.
- Maintaining a **restart-safe pointer** so it resumes where it left off.

Main file: `collector/collector.py`

Config (gitignored): `collector/config.local.json`:

```json
{
  "api_base": "https://dom4in.net",
  "admin_api_key": "YOUR_ADMIN_API_KEY_HERE",
  "dns_resolvers": [
    { "name": "cloudflare", "url": "https://cloudflare-dns.com/dns-query" },
    { "name": "google",     "url": "https://dns.google/resolve" }
  ],
  "per_request_delay_ms": 0
}
```

Pointer file (gitignored):

- `collector/state_pointer.json` – remembers where the collector last stopped in the label/TLD space.

### Collector CLI

Run from repo root:

```bash
python collector/collector.py [options]
```

Options:

- `--count N` – number of domains to process in this run (default 25).
- `--api-base` – override API base URL (default from config file; typically `https://dom4in.net`).
- `--api-key` – override admin API key (default from config file).
- `--dry-run` – do not POST to backend, just print the payload.
- `--print-each` – print each domain and its classification as it is processed.
- `--reset-pointer` – delete the pointer file and exit.
- `--reset-db` – call `/api/admin/reset-stats` (requires valid admin key) and exit; can be combined with `--reset-pointer`.

### Typical usage

#### One-time setup

1. Create `collector/config.local.json` with `api_base` and `admin_api_key` matching the Worker configuration.
2. Ensure `ADMIN_API_KEY` is set for the Worker:
   - Locally: put it in `backend/.dev.vars` for `wrangler dev`.
   - Production: set `ADMIN_API_KEY` as a variable/secret in the Cloudflare Worker dashboard.

#### Dry-run test

```bash
python collector/collector.py --count 10 --print-each --dry-run
```

- Prints domains and their classification.
- Prints the JSON aggregate it would send.
- Does **not** change the DB.

#### Real run

```bash
python collector/collector.py --count 100 --print-each
```

- Processes 100 domains.
- Uploads aggregate stats to `/api/admin/upload-aggregate`.
- Updates D1 and therefore the dashboard.

#### Full reset (DB + pointer) in one line

```bash
python collector/collector.py --reset-db --reset-pointer
```

- Calls `/api/admin/reset-stats` to clear D1 aggregates.
- Deletes `collector/state_pointer.json`.
- Next run starts from the first label/TLD again.

---

## 3. Running locally

### Backend (Worker) dev

From `backend/`:

```bash
wrangler dev
```

Wrangler will:

- Use `wrangler.toml` for config.
- If present, load environment variables from `.dev.vars` (e.g. `ADMIN_API_KEY`).

Local endpoints (default):

- `http://127.0.0.1:8787/api/health`
- `http://127.0.0.1:8787/api/stats/overview`

### Frontend dev

Simplest: open `frontend/index.html` directly in your browser for static layout. For local API tests against `wrangler dev`, you can temporarily point the fetch URL at the dev Worker URL instead of `/api/stats/overview`.

---

## 4. Deployment

### Frontend (Cloudflare Pages)

- Pages project connected to this repo.
- Root: repo root.
- Build command: none (static).
- Output directory: `frontend`.
- Auto-deploys on push to `main`.

### Backend (Worker) via GitHub Actions

Workflow: `.github/workflows/deploy-worker.yml`.

- Triggers on push to `main` when `backend/**` changes.
- Steps:
  - Checkout repo
  - Install Node
  - Install Wrangler
  - `wrangler deploy` from `backend/`

Requires GitHub Secrets:

- `CLOUDFLARE_API_TOKEN`
- `CLOUDFLARE_ACCOUNT_ID`

### Routes

In Cloudflare dashboard, configure a route:

- Pattern: `dom4in.net/api/*`
- Worker: `dom4in-backend`

Then frontend calls to `/api/...` from `dom4in.net` will be routed to the Worker.

---

## 5. Design constraints & notes

- Frontend is static (no server-side JS) and runs on Cloudflare Pages.
- Backend is a Cloudflare Worker using D1 for structured data.
- The collector is designed to be:
  - Local-only (runs on your machine).
  - Restart-safe via a pointer file.
  - Configurable via `config.local.json` (API base, admin key, DNS resolvers, delay).
- Only aggregated stats are stored in the cloud DB; per-domain raw data stays local.
- `CHARSET` currently includes only `a–z`. Extending to digits or other characters will increase the search space and should be a deliberate change in `collector/collector.py`.
