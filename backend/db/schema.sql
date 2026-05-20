-- D1 schema for dom4in.net aggregated stats
--
-- Apply to the live DB (idempotent thanks to IF NOT EXISTS).
-- Run from the backend/ directory:
--   wrangler d1 execute DOM4IN_DB --remote --file=./db/schema.sql
-- For local dev DB, swap --remote for --local.

CREATE TABLE IF NOT EXISTS global_stats (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL, -- ISO date (e.g. 2025-11-20) or 'overall'
  domains_tracked_lifetime INTEGER NOT NULL DEFAULT 0,
  domains_tracked_24h INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_global_stats_date
  ON global_stats (date);

CREATE TABLE IF NOT EXISTS length_stats (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snap_date TEXT NOT NULL,
  tld TEXT NOT NULL, -- 'ALL' or a specific TLD like 'com'
  length INTEGER NOT NULL, -- 1-10
  total_possible INTEGER NOT NULL,
  tracked_count INTEGER NOT NULL DEFAULT 0,
  unregistered_found INTEGER NOT NULL DEFAULT 0,
  unused_found INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_length_stats_snap_tld_len
  ON length_stats (snap_date, tld, length);

CREATE UNIQUE INDEX IF NOT EXISTS idx_length_stats_unique
  ON length_stats (snap_date, tld, length);

CREATE TABLE IF NOT EXISTS tld_stats (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snap_date TEXT NOT NULL,
  tld TEXT NOT NULL,
  domains_checked_total INTEGER NOT NULL DEFAULT 0,
  short_domains_checked_total INTEGER NOT NULL DEFAULT 0,
  short_unregistered_count INTEGER NOT NULL DEFAULT 0,
  short_no_website_count INTEGER NOT NULL DEFAULT 0,
  short_active_site_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tld_stats_unique
  ON tld_stats (snap_date, tld);

-- Word-based stats by part-of-speech (POS) and length
CREATE TABLE IF NOT EXISTS word_pos_stats (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snap_date TEXT NOT NULL,
  tld TEXT NOT NULL,
  pos TEXT NOT NULL,
  length INTEGER NOT NULL,
  tracked_count INTEGER NOT NULL DEFAULT 0,
  unregistered_found INTEGER NOT NULL DEFAULT 0,
  unused_found INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_word_pos_stats_unique
  ON word_pos_stats (snap_date, tld, pos, length);

-- Opaque key/value store for collector state (e.g. scan pointers).
-- value is stored as a JSON-encoded string; the Worker does not interpret it.
CREATE TABLE IF NOT EXISTS state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Append-only run log. Collector POSTs a 'start' row with a fresh run_id,
-- then a 'finish' event updating status and counts. Lets the site show
-- "last updated N minutes ago" without inspecting stats tables.
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT,
  status TEXT NOT NULL DEFAULT 'running', -- 'running' | 'success' | 'partial' | 'failed'
  domains_processed INTEGER NOT NULL DEFAULT 0,
  errors_count INTEGER NOT NULL DEFAULT 0,
  source TEXT, -- e.g. 'local', 'github-actions'
  notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_started_at
  ON runs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status
  ON runs (status);

-- Idempotency guard for upload-aggregate. The collector sends a batch_id
-- per POST; if the same (run_id, batch_id) is replayed (e.g. GHA retry),
-- the Worker refuses to re-apply it.
CREATE TABLE IF NOT EXISTS upload_dedupe (
  run_id TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  uploaded_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (run_id, batch_id)
);

-- ---------------------------------------------------------------------------
-- v2: Ground-truth observatory expansion (CZDS + ICANN + RDAP + Brand watch)
-- All new tables. Existing tables above are untouched so old code keeps
-- working. Public read endpoints still expose aggregates only.
-- ---------------------------------------------------------------------------

-- Dimension table: every TLD we know about, with classification metadata.
-- Seeded from IANA root zone DB + CZDS coverage list. Lookup by `tld`.
CREATE TABLE IF NOT EXISTS tld_dim (
  tld TEXT PRIMARY KEY,
  type TEXT NOT NULL,                 -- 'gTLD' | 'ccTLD' | 'sTLD' | 'brand' | 'test'
  registry TEXT,                       -- e.g. 'Verisign', 'Identity Digital'
  jurisdiction TEXT,                   -- ISO-3166 alpha-2 country, or 'INT'
  in_czds INTEGER NOT NULL DEFAULT 0,  -- 1 if zone file available via CZDS
  launched_at TEXT,                    -- ISO date (delegation)
  ga_at TEXT,                          -- General Availability date (new gTLDs)
  status TEXT,                         -- 'active' | 'retired' | 'reserved'
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tld_dim_type ON tld_dim (type);
CREATE INDEX IF NOT EXISTS idx_tld_dim_czds ON tld_dim (in_czds);

-- Dimension table: ICANN-accredited registrars. Seeded from ICANN's
-- accredited-registrars list. RDAP responses give us a registrar IANA ID
-- per registered domain which joins here.
CREATE TABLE IF NOT EXISTS registrar_dim (
  iana_id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  display_name TEXT,
  jurisdiction TEXT,                   -- ISO-3166 country of accreditation
  accredited_at TEXT,                  -- accreditation date
  status TEXT,                         -- 'accredited' | 'terminated' | 'suspended'
  rdap_base_url TEXT,
  website TEXT,
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_registrar_dim_status ON registrar_dim (status);
CREATE INDEX IF NOT EXISTS idx_registrar_dim_jurisdiction ON registrar_dim (jurisdiction);

-- ICANN publishes monthly per-registrar transaction reports as CSVs. One
-- row per (month, registrar, tld). Net adds = the canonical signal of
-- registrar market share. domains_under_mgmt is a snapshot.
CREATE TABLE IF NOT EXISTS registrar_monthly_stats (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_month TEXT NOT NULL,          -- 'YYYY-MM'
  iana_id INTEGER NOT NULL,
  tld TEXT NOT NULL,
  net_adds INTEGER NOT NULL DEFAULT 0,
  renewals INTEGER NOT NULL DEFAULT 0,
  transfers INTEGER NOT NULL DEFAULT 0,
  deletes INTEGER NOT NULL DEFAULT 0,
  domains_under_mgmt INTEGER NOT NULL DEFAULT 0,
  source_url TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_registrar_monthly_unique
  ON registrar_monthly_stats (report_month, iana_id, tld);
CREATE INDEX IF NOT EXISTS idx_registrar_monthly_tld
  ON registrar_monthly_stats (tld, report_month);

-- Daily aggregate from CZDS zone-file diff. AGGREGATES ONLY — no per-domain
-- list is ever stored here. Per-domain detail lives in R2 zone snapshots,
-- which are private and only ever leave the system as paid exports.
CREATE TABLE IF NOT EXISTS zone_diff_daily (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snap_date TEXT NOT NULL,             -- 'YYYY-MM-DD' of the diff
  tld TEXT NOT NULL,
  registered_total INTEGER NOT NULL DEFAULT 0,
  new_today INTEGER NOT NULL DEFAULT 0,
  dropped_today INTEGER NOT NULL DEFAULT 0,
  zone_size_bytes INTEGER,             -- raw zone file size, sanity-check
  source TEXT NOT NULL DEFAULT 'czds', -- 'czds' | 'probe-estimate'
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_zone_diff_unique
  ON zone_diff_daily (snap_date, tld);
CREATE INDEX IF NOT EXISTS idx_zone_diff_date ON zone_diff_daily (snap_date);
CREATE INDEX IF NOT EXISTS idx_zone_diff_tld ON zone_diff_daily (tld);

-- Registrar × TLD coverage matrix. Each row: does this registrar carry this
-- TLD, and at what pricing. Populated by a monthly scrape of registrar
-- pricing pages. Stale rows are kept; last_verified_at tells the caller.
CREATE TABLE IF NOT EXISTS registrar_tld_coverage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  iana_id INTEGER NOT NULL,
  tld TEXT NOT NULL,
  carries INTEGER NOT NULL DEFAULT 0,  -- boolean
  first_year_cents INTEGER,            -- USD cents; NULL if unknown
  renewal_cents INTEGER,
  transfer_cents INTEGER,
  restrictions TEXT,                    -- free-text e.g. 'requires EU residency'
  last_verified_at TEXT,
  source_url TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_reg_tld_unique
  ON registrar_tld_coverage (iana_id, tld);
CREATE INDEX IF NOT EXISTS idx_reg_tld_tld ON registrar_tld_coverage (tld);

-- ---------------------------------------------------------------------------
-- v3: Company-tracker pivot (2026-05-19)
-- We dropped brand-watch entirely. dom4in.net is now a public data product
-- on DNS / infrastructure trends, with corpora drawn from:
--   - SEC EDGAR (all US public companies, ~6K)
--   - Wikipedia maintained indexes (S&P 500, Russell 1000 membership)
--   - Tranco (free academic top-1M website ranking)
-- Each domain gets one monthly_probe row per snap_month. provider_share_monthly
-- holds the pre-computed rollups the public site renders directly.
-- ---------------------------------------------------------------------------

-- Companies and websites we track. A row is either a public company (has
-- ticker / sec_cik) or a top-website-only entry (ticker NULL). canonical_domain
-- is the apex we probe; alternates (cocacola.com vs ko.com) go in `notes` until
-- we need an alternates table.
CREATE TABLE IF NOT EXISTS companies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  canonical_domain TEXT NOT NULL,      -- apex only, lowercase
  ticker TEXT,                          -- primary stock ticker, NULL for non-public
  exchange TEXT,                        -- 'NYSE' | 'NASDAQ' | 'LSE' | NULL
  sec_cik TEXT,                         -- SEC EDGAR CIK (US public companies)
  industry TEXT,                        -- SIC sector name, free-text otherwise
  -- Index/list membership flags. Cheap to query "all S&P 500" via flag.
  in_sp500 INTEGER NOT NULL DEFAULT 0,
  in_russell1000 INTEGER NOT NULL DEFAULT 0,
  in_russell3000 INTEGER NOT NULL DEFAULT 0,
  in_us_public INTEGER NOT NULL DEFAULT 0,   -- in SEC EDGAR public company list
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_domain ON companies (canonical_domain);
CREATE INDEX IF NOT EXISTS idx_companies_ticker ON companies (ticker);
CREATE INDEX IF NOT EXISTS idx_companies_cik ON companies (sec_cik);
CREATE INDEX IF NOT EXISTS idx_companies_sp500 ON companies (in_sp500);
CREATE INDEX IF NOT EXISTS idx_companies_r1000 ON companies (in_russell1000);

-- Tranco monthly snapshot — rank within the global top-sites list. We keep
-- ~one row per (domain, snap_month) to enable tier-based aggregations.
-- snap_month is 'YYYY-MM' (the Tranco list date we used). A domain that
-- falls out of the top 1M just won't have a row for that month.
CREATE TABLE IF NOT EXISTS tranco_ranks (
  domain TEXT NOT NULL,
  snap_month TEXT NOT NULL,
  rank INTEGER NOT NULL,
  PRIMARY KEY (domain, snap_month)
);

CREATE INDEX IF NOT EXISTS idx_tranco_month_rank ON tranco_ranks (snap_month, rank);

-- One probe row per (domain, month). All optional — null means "unknown / not
-- detected this month." Providers stored as short keys (e.g. 'cloudflare',
-- 'route53'); provider_dim maps key -> display + category.
CREATE TABLE IF NOT EXISTS monthly_probe (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snap_month TEXT NOT NULL,            -- 'YYYY-MM'
  domain TEXT NOT NULL,
  registrar_iana_id INTEGER,
  ns_provider TEXT,
  ns_records_count INTEGER,
  ns_country TEXT,                      -- modal ISO-3166 from NS IPs
  mx_provider TEXT,
  has_mx INTEGER,
  hosting_provider TEXT,                -- derived from A-record ASN
  a_asn INTEGER,
  has_aaaa INTEGER,
  dnssec INTEGER,
  caa_present INTEGER,
  cert_issuer TEXT,
  analytics_provider TEXT,              -- CSV: 'ga4,gtm,hubspot' etc
  tag_managers TEXT,
  marketing_stack TEXT,
  cdn_provider TEXT,
  http_server TEXT,
  probe_status TEXT,                    -- 'ok' | 'no-dns' | 'timeout' | 'http-error'
  notes TEXT,
  probed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_monthly_probe_unique
  ON monthly_probe (snap_month, domain);
CREATE INDEX IF NOT EXISTS idx_monthly_probe_domain ON monthly_probe (domain);

-- Provider dimension: short-key → display name + category. Drives the
-- chart legend and the detection rules referenced by the probe script.
CREATE TABLE IF NOT EXISTS provider_dim (
  provider_key TEXT PRIMARY KEY,        -- e.g. 'cloudflare'
  category TEXT NOT NULL,               -- 'ns' | 'mx' | 'cloud' | 'cdn' | 'analytics' | 'cert' | 'marketing'
  display_name TEXT NOT NULL,
  parent TEXT,                          -- e.g. 'aws' for sub-providers
  notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_provider_dim_cat ON provider_dim (category);

-- Pre-computed monthly rollups — the public site reads from here directly.
-- One row per (snap_month, tier, category, provider_key). tier values:
--   'sp500' | 'russell1000' | 'russell3000' | 'tranco100' | 'tranco1000'
--   | 'tranco10000' | 'all_us_public' | 'all'
CREATE TABLE IF NOT EXISTS provider_share_monthly (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snap_month TEXT NOT NULL,
  tier TEXT NOT NULL,
  category TEXT NOT NULL,
  provider_key TEXT NOT NULL,
  count INTEGER NOT NULL,
  total INTEGER NOT NULL,               -- denominator for share
  share REAL NOT NULL,                  -- count / total
  delta_count INTEGER,                  -- vs previous month, NULL if first
  delta_share REAL,
  computed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_share_unique
  ON provider_share_monthly (snap_month, tier, category, provider_key);
CREATE INDEX IF NOT EXISTS idx_provider_share_browse
  ON provider_share_monthly (tier, category, snap_month);
