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

-- Brand watch — patterns we're looking for in newly-registered domains.
-- A watchlist entry is owned either by a paying customer (owner_email set)
-- or is 'public' (owner_email NULL) for the catalog of brand reports we
-- sell on the marketplace. notify=1 means we email the owner on match.
CREATE TABLE IF NOT EXISTS brand_watchlist (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_email TEXT,                    -- NULL = public/catalog pattern
  pattern TEXT NOT NULL,               -- the brand/keyword (lower-case, no TLD)
  match_type TEXT NOT NULL DEFAULT 'exact', -- 'exact' | 'contains' | 'fuzzy'
  tld_filter TEXT,                     -- CSV of TLDs to scope to; NULL = all
  notify INTEGER NOT NULL DEFAULT 0,
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  expires_at TEXT,                     -- e.g. 24 months from purchase
  active INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_watchlist_pattern ON brand_watchlist (pattern);
CREATE INDEX IF NOT EXISTS idx_watchlist_owner ON brand_watchlist (owner_email);
CREATE INDEX IF NOT EXISTS idx_watchlist_active ON brand_watchlist (active);

-- Match events. The CZDS/RDAP pipelines emit a row here whenever a newly
-- registered domain hits a watchlist pattern. matched_domain is stored in
-- full because the value of the product IS the specific domain; it is
-- never returned by public endpoints.
CREATE TABLE IF NOT EXISTS brand_match_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  detected_at TEXT NOT NULL DEFAULT (datetime('now')),
  watchlist_id INTEGER,                -- FK to brand_watchlist.id (nullable for ad-hoc scans)
  pattern TEXT NOT NULL,
  matched_domain TEXT NOT NULL,
  tld TEXT NOT NULL,
  registrar_iana_id INTEGER,
  registered_at TEXT,                  -- when the domain appeared in the zone
  source TEXT NOT NULL,                -- 'czds-diff' | 'rdap-sample' | 'probe'
  notified INTEGER NOT NULL DEFAULT 0,
  notified_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_match_event_pattern ON brand_match_event (pattern, detected_at);
CREATE INDEX IF NOT EXISTS idx_match_event_detected ON brand_match_event (detected_at);
CREATE INDEX IF NOT EXISTS idx_match_event_watchlist ON brand_match_event (watchlist_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_match_event_dedupe
  ON brand_match_event (watchlist_id, matched_domain, source);
