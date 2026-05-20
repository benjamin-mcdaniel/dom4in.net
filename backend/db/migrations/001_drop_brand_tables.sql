-- One-shot cleanup: drop the deprecated brand-watch tables.
-- Run once via the cleanup-v3.yml workflow (or manually:
--   wrangler d1 execute DOM4IN_DB --remote --file=./db/migrations/001_drop_brand_tables.sql
-- ). Safe to re-run thanks to IF EXISTS.

DROP TABLE IF EXISTS brand_match_event;
DROP TABLE IF EXISTS brand_watchlist;
