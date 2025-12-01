-- D1 schema for dom4in.net aggregated stats

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
