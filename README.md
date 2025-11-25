# dom4in.net

dom4in.net is a domain market stats dashboard. It samples short domains (currently 1–10 character labels) across several major TLDs and shows **aggregated** data only (no per-domain lists), similar to a stock market overview.

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
  - `letter_length_counts` for 1–10 character labels.
- Renders:
  - KPI cards (lifetime and 24h counts)
  - A table and per-length visualization of 1–10 character label stats (aggregated across TLDs)
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
  - **Aggregation semantics:**
    - `global_stats.domains_tracked_lifetime` – cumulative sum of all `domains_tracked_lifetime` values sent for each date. This is effectively total domain searches lifetime since the last DB reset.
    - `global_stats.domains_tracked_24h` – cumulative sum per date (per day). Each run on a given date adds to that day's total.
    - `length_stats` – cumulative lifetime aggregates under `snap_date = 'overall'` and `tld = 'ALL'`. New uploads add their counts to existing rows per `(snap_date, tld, length)` rather than overwriting them.

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
  - `length` (INTEGER, 1–10)
  - `total_possible` (INTEGER, e.g. `26^length` for labels a–z)
  - `tracked_count` – number of domain searches (label×TLD checks) in this length bucket
  - `unregistered_found` – currently surfaced in the UI as **"Parking Detected"**
  - `unused_found` – surfaced as **"Unused / No Website"**
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

- Generating candidate labels and combining them with a list of TLDs:
  - **Short mode**: synthetic labels using `CHARSET` (currently 1–10 characters a–z).
  - **Word mode**: real words (≤10 characters) from an external dictionary.
- Checking:
  - DNS via DNS-over-HTTPS (Cloudflare/Google/etc by default).
  - HTTP (simple check of `https://<domain>`).
- Classifying each domain as:
  - `registered` / `unregistered`
  - `usage_state`: `no_website` | `parked_or_placeholder` | `active_site`
  - `product_state`: `active_product` | `unknown`
- Aggregating counts over each **block** of domains.
- Uploading a JSON aggregate for each block to `POST /api/admin/upload-aggregate`.
- Maintaining **restart-safe pointers** so it resumes where it left off for both short and word modes.

Main file: `collector/collector.py`

Dictionary loader (one-time per environment): `collector/load_dictionary.py`.

Config (gitignored): `collector/config.local.json`:

```json
{
  "api_base": "https://dom4in.net",
  "admin_api_key": "YOUR_ADMIN_API_KEY_HERE",
  "dns_resolvers": [
    { "name": "cloudflare", "url": "https://cloudflare-dns.com/dns-query" },
    { "name": "google",     "url": "https://dns.google/resolve" }
  ],
  "per_request_delay_ms": 0,
  "block_pause_seconds": 0
}
```

Pointer/state files (gitignored):

- `collector/state_pointer.json` – remembers where the collector last stopped in the short label/TLD space.
- `collector/state_words_pointer.json` – remembers progress through the word list when word mode is enabled.
Dictionary file (gitignored):

- `wordlists/words_10_all.txt` – generated by `collector/load_dictionary.py` from a public English word list.

---

## 3. Configuration & settings

### 3.1 Collector config (local-only)

`collector/config.local.json` (gitignored) controls how the collector talks to the backend and DNS:

- `api_base` – Base URL for the backend API, e.g. `https://dom4in.net`.
- `admin_api_key` – Shared secret used as the `x-admin-api-key` header. Must match `ADMIN_API_KEY` configured for the Worker.
- `dns_resolvers` – Optional list of DNS-over-HTTPS resolvers. Each entry:

  ```json
  { "name": "cloudflare", "url": "https://cloudflare-dns.com/dns-query" }
  ```

  If omitted, defaults to Cloudflare and Google DoH endpoints. The collector rotates resolvers every 25 queries and falls back on error.

- `per_request_delay_ms` – Optional integer (milliseconds). If >0, the collector sleeps this long after each domain to avoid hammering remote endpoints.

### 3.2 Backend local config

`backend/.dev.vars` (gitignored) is used only for local `wrangler dev`:

```bash
ADMIN_API_KEY=your-admin-key-here
```

In production, the same `ADMIN_API_KEY` value must be set as an environment variable/secret on the `dom4in-backend` Worker in the Cloudflare dashboard. The Worker never reads secrets from `wrangler.toml` in production; only from its environment.

### Collector CLI

Run from repo root:

```bash
python collector/collector.py [options]
```

Options:

- `--api-base` – override API base URL (default from config file; typically `https://dom4in.net`).
- `--api-key` – override admin API key (default from config file).
- `--dry-run` – do not POST to backend, just print the payload.
- `--print-each` – print each domain and its classification as it is processed.
- `--reset-pointer` – delete the short-mode pointer file and exit.
- `--reset-db` – call `/api/admin/reset-stats` (requires valid admin key) and exit; can be combined with `--reset-pointer`.
- `--short` – enable short label mode (1–6 characters from `CHARSET`).
- `--word` – enable word-based mode using `wordlists/words_10_all.txt`.
- `--pause N` – optional pause (in seconds) between randomly sized blocks.

If neither `--short` nor `--word` is passed, the collector defaults to short mode. If both are passed, it alternates blocks (short → word → short → …).

### Typical usage

#### One-time setup

1. Create `collector/config.local.json` with `api_base` and `admin_api_key` matching the Worker configuration.
2. Ensure `ADMIN_API_KEY` is set for the Worker:
   - Locally: put it in `backend/.dev.vars` for `wrangler dev`.
   - Production: set `ADMIN_API_KEY` as a variable/secret in the Cloudflare Worker dashboard.
3. Generate the word dictionary (if you plan to use `--word`):

   ```bash
   cd collector
   python load_dictionary.py
   cd ..
   ```

#### Continuous mixed run (short + words)

```bash
python collector/collector.py --short --word --pause 60
```

- Picks a random block size between 25 and 80 domains.
- For each block:
  - Generates domains (short or word mode, alternating when both are enabled).
  - Runs DNS+HTTP checks and aggregates stats.
  - Uploads a single aggregate payload for that block.
  - Prints a brief summary to the console.
  - Saves pointers so it can be safely restarted.
- Sleeps `--pause` seconds between blocks.

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
