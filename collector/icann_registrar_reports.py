"""ICANN registrar monthly transaction report ingestion.

ICANN publishes per-registrar transaction reports each month at
    https://www.icann.org/resources/pages/registrar-reports-2017-08-09-en
with a CSV for each (year, month). Each CSV row is one
(registrar, registry/TLD) pair with columns for net-adds, renewals,
transfers, deletes, and end-of-month domains-under-management.

This is the canonical source of registrar market-share data and there is no
faster-cadence equivalent — registrars submit reports two months after the
fact, so the "May 2026" report appears around July 2026. We poll monthly
and idempotently upsert into registrar_monthly_stats.

The report URL pattern has historically been:
    https://www.icann.org/sites/default/files/mrr/<YYYY>/mrr-<YYYY>-<MM>-en.csv
ICANN occasionally renames or relocates these files. The script tries the
canonical URL first and falls back to scraping the index page for any link
ending in `-<YYYY>-<MM>-en.csv`. If both fail for a given month we skip it
and emit a warning — next run picks it up automatically.

Run with no arguments to ingest the latest 3 months (idempotent), or pass
`--month YYYY-MM` to backfill a specific month.

Environment:
    API_BASE, ADMIN_API_KEY  — dom4in backend
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.local.json")

INDEX_URL = "https://www.icann.org/resources/pages/registrar-reports-2017-08-09-en"
CANONICAL_URL = "https://www.icann.org/sites/default/files/mrr/{year}/mrr-{year}-{month:02d}-en.csv"

# Column-name candidates. ICANN's CSV header drifts subtly between months
# (case, whitespace, "Net Adds" vs "net-adds" vs "Net Adds 1-Year"). We map
# header-name → canonical field by checking lowercase substring.
COLUMN_HINTS = {
    "iana_id": ["iana-id", "iana id", "registrar-id"],
    "registrar_name": ["registrar name", "registrar-name", "registrar"],
    "tld": ["tld", "gtld"],
    "net_adds": ["net adds", "net-adds", "net_add"],
    "renewals": ["renew"],
    "transfers": ["transfer"],
    "deletes": ["delete"],
    "domains_under_mgmt": ["domains-in-host", "dum", "domains under", "end-of-month-domains"],
}


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


def fetch_bytes(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "dom4in-icann/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def find_csv_url_for_month(year: int, month: int) -> Optional[str]:
    """Try canonical URL first; on 404 scrape the index page for a matching link."""
    candidate = CANONICAL_URL.format(year=year, month=month)
    try:
        with urllib.request.urlopen(urllib.request.Request(candidate, method="HEAD"), timeout=15) as resp:
            if resp.status == 200:
                return candidate
    except urllib.error.HTTPError:
        pass
    except Exception:
        pass

    # Fall back: scrape the index page.
    try:
        html = fetch_bytes(INDEX_URL).decode("utf-8", errors="replace")
    except Exception:
        return None

    pattern = re.compile(
        rf'href="([^"]*mrr[^"]*{year}-{month:02d}[^"]*\.csv)"',
        re.IGNORECASE,
    )
    m = pattern.search(html)
    if not m:
        return None
    href = m.group(1)
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return "https://www.icann.org" + href
    return href


def normalize_header(headers: List[str]) -> Dict[str, int]:
    """Return {canonical_field: column_index} based on COLUMN_HINTS."""
    out: Dict[str, int] = {}
    lower = [h.strip().lower() for h in headers]
    for field, hints in COLUMN_HINTS.items():
        for i, h in enumerate(lower):
            if any(hint in h for hint in hints):
                out[field] = i
                break
    return out


def parse_csv(content: bytes, source_url: str, report_month: str) -> List[Dict]:
    text = content.decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return []
    headers = rows[0]
    col = normalize_header(headers)
    required = {"iana_id", "tld"}
    if not required.issubset(col.keys()):
        # Some early ICANN reports have a totally different shape; skip them.
        return []

    out: List[Dict] = []
    for r in rows[1:]:
        if len(r) < len(headers):
            continue

        def cell(field: str) -> str:
            idx = col.get(field)
            if idx is None or idx >= len(r):
                return ""
            return r[idx].strip()

        try:
            iana_id = int(cell("iana_id"))
        except ValueError:
            continue
        tld = cell("tld").lower().lstrip(".")
        if not tld:
            continue

        def num(field: str) -> int:
            v = cell(field)
            if not v:
                return 0
            try:
                return int(v.replace(",", ""))
            except ValueError:
                return 0

        out.append({
            "report_month": report_month,
            "iana_id": iana_id,
            "tld": tld,
            "net_adds": num("net_adds"),
            "renewals": num("renewals"),
            "transfers": num("transfers"),
            "deletes": num("deletes"),
            "domains_under_mgmt": num("domains_under_mgmt"),
            "source_url": source_url,
        })
    return out


def post_admin(cfg: Dict[str, str], path: str, rows: List[Dict]) -> None:
    if not rows:
        return
    url = f"{cfg['api_base']}{path}"
    payload = json.dumps({"rows": rows}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={
            "Content-Type": "application/json",
            "x-admin-api-key": cfg["api_key"],
            "User-Agent": "dom4in-icann/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"POST {path} [{resp.status}]: {resp.read()}")


def chunked(rows: List[Dict], n: int = 200) -> Iterable[List[Dict]]:
    for i in range(0, len(rows), n):
        yield rows[i:i + n]


def candidate_months(months_back: int) -> List[Tuple[int, int]]:
    """Return (year, month) for the last N months including current month-2.
    ICANN publishes ~2 months in arrears, so always start from today-60d."""
    today = datetime.now(timezone.utc).date()
    seed = today.replace(day=1) - timedelta(days=2)  # last day of prev month
    seed = seed.replace(day=1) - timedelta(days=1)   # last day of month before that
    out: List[Tuple[int, int]] = []
    cursor = seed
    for _ in range(months_back):
        out.append((cursor.year, cursor.month))
        # Step back one month
        cursor = (cursor.replace(day=1) - timedelta(days=1)).replace(day=1)
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--month", help="YYYY-MM (single-month backfill)", default=None)
    p.add_argument("--months-back", type=int, default=3, help="How many months to attempt (default 3)")
    args = p.parse_args()

    cfg = load_config()

    if args.month:
        try:
            y, m = args.month.split("-")
            targets = [(int(y), int(m))]
        except Exception:
            raise SystemExit(f"--month must be YYYY-MM, got {args.month!r}")
    else:
        targets = candidate_months(args.months_back)

    total_rows = 0
    for year, month in targets:
        report_month = f"{year:04d}-{month:02d}"
        print(f"Looking for {report_month} ...")
        url = find_csv_url_for_month(year, month)
        if not url:
            print(f"  not yet published — skip")
            continue
        try:
            content = fetch_bytes(url, timeout=120)
        except Exception as err:
            print(f"  fetch failed: {err}")
            continue
        rows = parse_csv(content, url, report_month)
        if not rows:
            print(f"  parsed 0 rows (unknown CSV shape?)")
            continue
        print(f"  parsed {len(rows):,} rows; uploading ...")
        for batch in chunked(rows):
            post_admin(cfg, "/api/admin/registrar-monthly", batch)
        total_rows += len(rows)

    print(f"Done. Total rows uploaded: {total_rows:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
