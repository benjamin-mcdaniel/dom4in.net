"""Seed the `companies` and `tranco_ranks` tables.

Pulls four free sources, dedupes by canonical domain, and posts to:
    POST /api/admin/companies      (corpus rows)
    POST /api/admin/tranco-ranks   (top-sites ranks)

Sources (all public, no auth):
  1. SEC EDGAR company tickers JSON
         https://www.sec.gov/files/company_tickers.json
     Authoritative list of US public companies with CIK + ticker. No website
     field, so we derive canonical domain from the ticker via a small lookup
     in seed/manual_domain_overrides.json + heuristic.
  2. S&P 500 constituents
         https://en.wikipedia.org/wiki/List_of_S%26P_500_companies
     Wikipedia table, scraped server-side. Membership flag only.
  3. Russell 1000 constituents
         https://en.wikipedia.org/wiki/Russell_1000_Index
     Same pattern as S&P 500.
  4. Tranco Top 1M
         https://tranco-list.eu/top-1m.csv.zip
     Standard academic top-sites list. We import the top 10,000 by default.

Run:
    python collector/seed/company_corpus.py
    # env:
    #   API_BASE=https://dom4in.net
    #   ADMIN_API_KEY=...
    #   TRANCO_LIMIT=10000          (override how many Tranco entries to import)
    #   TRANCO_LIST_ID=             (specific list ID; default = latest)
    #   SKIP_TRANCO=1               (skip the Tranco fetch for fast iteration)
    #   SKIP_EDGAR=1                (skip EDGAR — useful while iterating)

Domain derivation strategy for EDGAR:
  - First check seed/manual_domain_overrides.json for a hard-coded mapping
    (CIK -> domain). This is where you fix the inevitable wrong guesses.
  - If no override, build a candidate like `{ticker}.com` and accept it. We
    do NOT verify the domain resolves here — that's the probe step's job.
    Wrong guesses surface as `probe_status='no-dns'` next month and we
    correct them in the overrides file.

Idempotent: the worker endpoints all upsert on canonical_domain or
(domain, snap_month).
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(BASE_DIR, os.pardir, os.pardir))
CONFIG_FILE = os.path.join(REPO_ROOT, "collector", "config.local.json")
OVERRIDES_FILE = os.path.join(BASE_DIR, "manual_domain_overrides.json")

EDGAR_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
RUSSELL1000_WIKI_URL = "https://en.wikipedia.org/wiki/Russell_1000_Index"
TRANCO_LATEST_URL = "https://tranco-list.eu/top-1m.csv.zip"

DEFAULT_TRANCO_LIMIT = 10_000

UA = "dom4in-corpus-seed/1.0 (contact: benjamin.f.mcdaniel@gmail.com)"
# SEC requires a specific UA header with contact info. Wikipedia also wants
# a real UA. Both are public free APIs but rate-limit anonymous traffic.


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def load_config() -> Dict[str, str]:
    api_base = os.environ.get("API_BASE", "").rstrip("/")
    api_key = os.environ.get("ADMIN_API_KEY", "")
    if (not api_base or not api_key) and os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        api_base = api_base or str(cfg.get("api_base", "")).rstrip("/")
        api_key = api_key or str(cfg.get("admin_api_key", ""))
    if not api_base or not api_key:
        raise SystemExit("Missing API_BASE / ADMIN_API_KEY.")
    return {"api_base": api_base, "api_key": api_key}


def fetch(url: str, timeout: int = 60, extra_headers: Optional[Dict] = None) -> bytes:
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def post_admin(cfg: Dict[str, str], path: str, rows: List[Dict]) -> None:
    if not rows:
        return
    url = f"{cfg['api_base']}{path}"
    body = json.dumps({"rows": rows}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "x-admin-api-key": cfg["api_key"],
            "User-Agent": UA,
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"POST {path} [{resp.status}]: {resp.read()}")


def chunked(rows: List[Dict], n: int = 200):
    for i in range(0, len(rows), n):
        yield rows[i:i + n]


def load_overrides() -> Dict[str, str]:
    """CIK or ticker -> canonical_domain. Either key works."""
    if not os.path.exists(OVERRIDES_FILE):
        return {}
    with open(OVERRIDES_FILE, "r", encoding="utf-8") as f:
        return {str(k).lower(): str(v).lower() for k, v in json.load(f).items()}


def normalize_domain(d: str) -> str:
    """Strip protocol, www, trailing slash, lowercase."""
    s = d.strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^www\.", "", s)
    s = s.split("/")[0]
    s = s.split("?")[0]
    return s


def domain_from_ticker(ticker: str) -> str:
    """Heuristic only — `{ticker}.com`. We do not verify resolution here;
    wrong guesses surface in the probe step and get added to the overrides
    file. This keeps the seeder fast and offline-friendly."""
    t = re.sub(r"[^a-z0-9]", "", ticker.lower())
    return f"{t}.com"


# ---------------------------------------------------------------------------
# SEC EDGAR — all US public companies with a ticker
# ---------------------------------------------------------------------------

def fetch_edgar(overrides: Dict[str, str]) -> List[Dict]:
    """Returns a list of {name, ticker, sec_cik, canonical_domain, in_us_public=1}."""
    raw = fetch(EDGAR_TICKERS_URL, timeout=60)
    data = json.loads(raw.decode("utf-8"))
    out: List[Dict] = []
    seen_domains: Set[str] = set()
    # EDGAR JSON is a dict with numeric string keys -> {cik_str, ticker, title}
    for _, entry in data.items():
        if not isinstance(entry, dict):
            continue
        ticker = str(entry.get("ticker", "")).strip()
        name = str(entry.get("title", "")).strip()
        cik = str(entry.get("cik_str", "")).strip()
        if not ticker or not name or not cik:
            continue
        # Apply overrides keyed on CIK or ticker.
        override = overrides.get(cik) or overrides.get(ticker.lower())
        domain = normalize_domain(override) if override else domain_from_ticker(ticker)
        if domain in seen_domains:
            # Two tickers heuristically map to the same domain (e.g. share
            # classes). Skip duplicates — first one wins.
            continue
        seen_domains.add(domain)
        out.append({
            "name": name,
            "canonical_domain": domain,
            "ticker": ticker,
            "exchange": None,
            "sec_cik": cik.zfill(10),
            "industry": None,
            "in_us_public": 1,
            "in_sp500": 0,
            "in_russell1000": 0,
            "in_russell3000": 0,
        })
    return out


# ---------------------------------------------------------------------------
# S&P 500 + Russell 1000 — Wikipedia scrapes (membership flags only)
# ---------------------------------------------------------------------------

# Liberal regex: ticker cell looks like
#   <td><a ...>AAPL</a></td> ... or <td>AAPL</td>
# We extract uppercase 1-5 char alpha or BRK.B-style.
TICKER_TD_RE = re.compile(r">([A-Z][A-Z0-9.\-]{0,5})</a>", re.MULTILINE)
WIKI_TABLE_RE = re.compile(r"<table[^>]*wikitable[^>]*>(.+?)</table>", re.DOTALL)


def extract_tickers_from_wiki(url: str) -> Set[str]:
    """Quick-and-dirty: pull any 1-5 letter uppercase ticker from any
    wikitable on the page. False positives are filtered later by intersecting
    with EDGAR's known-ticker set."""
    try:
        html = fetch(url, extra_headers={"Accept": "text/html"}).decode("utf-8", errors="replace")
    except Exception as err:
        print(f"  warning: fetch failed for {url}: {err}")
        return set()
    tickers: Set[str] = set()
    for m in WIKI_TABLE_RE.finditer(html):
        body = m.group(1)
        for hit in TICKER_TD_RE.finditer(body):
            tickers.add(hit.group(1))
    return tickers


def apply_index_membership(edgar_rows: List[Dict], sp500: Set[str], r1000: Set[str]) -> None:
    """Mutates rows in place to set in_sp500 / in_russell1000 flags."""
    for r in edgar_rows:
        t = r["ticker"]
        if t in sp500:
            r["in_sp500"] = 1
        if t in r1000:
            r["in_russell1000"] = 1
            # If in Russell 1000, also in Russell 3000 by definition.
            r["in_russell3000"] = 1


# ---------------------------------------------------------------------------
# Tranco — top-sites ranks
# ---------------------------------------------------------------------------

def fetch_tranco(limit: int) -> List[Dict]:
    """Returns {domain, snap_month, rank} for the top `limit` entries."""
    print(f"  downloading Tranco top-1M ({TRANCO_LATEST_URL}) ...")
    raw = fetch(TRANCO_LATEST_URL, timeout=180)
    snap_month = datetime.now(timezone.utc).strftime("%Y-%m")
    out: List[Dict] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        name = next((n for n in zf.namelist() if n.endswith(".csv")), None)
        if not name:
            raise RuntimeError("Tranco zip has no CSV inside")
        with zf.open(name) as f:
            reader = csv.reader(io.TextIOWrapper(f, encoding="utf-8"))
            for row in reader:
                if len(row) < 2:
                    continue
                try:
                    rank = int(row[0])
                except ValueError:
                    continue
                if rank > limit:
                    break
                domain = normalize_domain(row[1])
                if domain:
                    out.append({"domain": domain, "snap_month": snap_month, "rank": rank})
    return out


def tranco_to_companies(tranco_rows: List[Dict], existing_domains: Set[str]) -> List[Dict]:
    """Tranco entries that aren't already a public-company row become
    standalone 'website' entries (ticker NULL, in_us_public=0). We attach
    the name = domain for now — fancy entity resolution can come later."""
    out: List[Dict] = []
    for r in tranco_rows:
        d = r["domain"]
        if d in existing_domains:
            continue
        out.append({
            "name": d,
            "canonical_domain": d,
            "ticker": None,
            "exchange": None,
            "sec_cik": None,
            "industry": None,
            "in_us_public": 0,
            "in_sp500": 0,
            "in_russell1000": 0,
            "in_russell3000": 0,
        })
        existing_domains.add(d)
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    cfg = load_config()
    overrides = load_overrides()
    print(f"Loaded {len(overrides)} manual domain overrides.")

    edgar_rows: List[Dict] = []
    if os.environ.get("SKIP_EDGAR") == "1":
        print("Skipping EDGAR per SKIP_EDGAR=1.")
    else:
        print(f"Fetching SEC EDGAR ({EDGAR_TICKERS_URL}) ...")
        edgar_rows = fetch_edgar(overrides)
        print(f"  parsed {len(edgar_rows)} public-company rows")

    sp500_tickers: Set[str] = set()
    r1000_tickers: Set[str] = set()
    if edgar_rows:
        print("Fetching S&P 500 constituents from Wikipedia ...")
        sp500_tickers = extract_tickers_from_wiki(SP500_WIKI_URL)
        print(f"  found {len(sp500_tickers)} candidate tickers (will intersect with EDGAR)")
        print("Fetching Russell 1000 constituents from Wikipedia ...")
        r1000_tickers = extract_tickers_from_wiki(RUSSELL1000_WIKI_URL)
        print(f"  found {len(r1000_tickers)} candidate tickers")
        apply_index_membership(edgar_rows, sp500_tickers, r1000_tickers)
        n_sp = sum(1 for r in edgar_rows if r["in_sp500"])
        n_r1 = sum(1 for r in edgar_rows if r["in_russell1000"])
        print(f"  intersected: {n_sp} S&P 500, {n_r1} Russell 1000")

    # Upload EDGAR rows first so domain dedupe works against the DB-known set.
    if edgar_rows:
        print(f"Uploading {len(edgar_rows)} EDGAR companies ...")
        for batch in chunked(edgar_rows):
            post_admin(cfg, "/api/admin/companies", batch)

    if os.environ.get("SKIP_TRANCO") == "1":
        print("Skipping Tranco per SKIP_TRANCO=1.")
    else:
        try:
            limit = int(os.environ.get("TRANCO_LIMIT", DEFAULT_TRANCO_LIMIT))
        except ValueError:
            limit = DEFAULT_TRANCO_LIMIT
        print(f"Fetching Tranco top {limit:,} ...")
        try:
            tranco_rows = fetch_tranco(limit)
        except Exception as err:
            print(f"  warning: Tranco fetch failed: {err}")
            tranco_rows = []

        if tranco_rows:
            print(f"  uploading {len(tranco_rows):,} rank rows ...")
            for batch in chunked(tranco_rows, 500):
                post_admin(cfg, "/api/admin/tranco-ranks", batch)

            existing = {r["canonical_domain"] for r in edgar_rows}
            new_companies = tranco_to_companies(tranco_rows, existing)
            if new_companies:
                print(f"  uploading {len(new_companies):,} non-public Tranco-only websites ...")
                for batch in chunked(new_companies):
                    post_admin(cfg, "/api/admin/companies", batch)

    print("Corpus seed complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
