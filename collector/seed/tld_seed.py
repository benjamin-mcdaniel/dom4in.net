"""Seed the tld_dim table from IANA's authoritative TLD list.

Usage:
    python collector/seed/tld_seed.py            # uses config.local.json
    API_BASE=https://dom4in.net ADMIN_API_KEY=... python collector/seed/tld_seed.py

Sources (all public, no auth needed):
    - https://data.iana.org/TLD/tlds-alpha-by-domain.txt
        Canonical newline-delimited list of every TLD in the root zone.
    - https://czds.icann.org/sites/default/files/czds_active_tlds.txt
        Best-effort list of TLDs that publish zone files via CZDS. Used to
        mark which entries we can ingest authoritatively vs which we still
        have to probe.

Type classification is heuristic and good-enough for now:
    - Two-letter alpha → ccTLD
    - Three+ letter pure-alpha → gTLD (most accurate post-2012)
    - Starts with 'xn--' → IDN ccTLD/gTLD (left as gTLD; root DB has the truth)
    - Hand-coded list of sTLD/legacy (gov, edu, mil, int, arpa)

This script is idempotent — it upserts via the admin /api/admin/tld-dim
endpoint, so re-running it just refreshes metadata for known TLDs.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Dict, List, Set

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(BASE_DIR, os.pardir, os.pardir))
CONFIG_FILE = os.path.join(REPO_ROOT, "collector", "config.local.json")

IANA_TLD_LIST_URL = "https://data.iana.org/TLD/tlds-alpha-by-domain.txt"
CZDS_ACTIVE_TLDS_URL = "https://czds.icann.org/sites/default/files/czds_active_tlds.txt"

# Special / sponsored TLDs that don't fit the simple "2 letters = ccTLD"
# rule. Everything else is classified by the heuristics above.
STLD_LIKE = {
    "gov": ("sTLD", "US"),
    "edu": ("sTLD", "US"),
    "mil": ("sTLD", "US"),
    "int": ("sTLD", "INT"),
    "arpa": ("sTLD", "INT"),
    "aero": ("sTLD", "INT"),
    "asia": ("sTLD", "INT"),
    "cat": ("sTLD", "INT"),
    "coop": ("sTLD", "INT"),
    "jobs": ("sTLD", "INT"),
    "mobi": ("sTLD", "INT"),
    "museum": ("sTLD", "INT"),
    "post": ("sTLD", "INT"),
    "tel": ("sTLD", "INT"),
    "travel": ("sTLD", "INT"),
    "xxx": ("sTLD", "INT"),
}


def fetch_text(url: str, timeout: int = 30) -> str:
    """Fetch a URL, returning UTF-8 decoded text. Caller handles exceptions."""
    req = urllib.request.Request(url, headers={"User-Agent": "dom4in-tld-seed/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_iana_tlds(text: str) -> List[str]:
    """IANA's file is one TLD per line, upper-case, prefixed with a header
    comment. We lowercase, strip, and skip blank/comment lines."""
    out: List[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s.lower())
    return out


def parse_czds_tlds(text: str) -> Set[str]:
    """CZDS-active list is also newline-delimited TLDs. Case varies."""
    out: Set[str] = set()
    for line in text.splitlines():
        s = line.strip().lower()
        if not s or s.startswith("#"):
            continue
        # Some lines might be 'tld\tregistry' depending on format drift —
        # take the first whitespace-separated token.
        token = s.split()[0]
        if token:
            out.add(token)
    return out


def classify(tld: str) -> Dict[str, str]:
    """Return {type, jurisdiction} for a given TLD using the heuristics
    described in the module docstring. Brand TLDs (post-2012 gTLDs owned by
    a single company) are hard to distinguish from open gTLDs without the
    IANA root DB JSON; we leave them as 'gTLD' and let manual updates flag
    individual brand TLDs later if needed."""
    if tld in STLD_LIKE:
        kind, jurisdiction = STLD_LIKE[tld]
        return {"type": kind, "jurisdiction": jurisdiction}

    if tld.startswith("xn--"):
        # IDN. Could be either ccTLD or gTLD; default to gTLD as the safer
        # initial guess. Manual edits can correct.
        return {"type": "gTLD", "jurisdiction": "INT"}

    if len(tld) == 2 and tld.isalpha():
        # ASCII ccTLD. ISO-3166 alpha-2 code uppercased == jurisdiction.
        return {"type": "ccTLD", "jurisdiction": tld.upper()}

    return {"type": "gTLD", "jurisdiction": "INT"}


def build_rows(iana_tlds: List[str], czds_active: Set[str]) -> List[Dict]:
    rows: List[Dict] = []
    for tld in iana_tlds:
        cls = classify(tld)
        rows.append({
            "tld": tld,
            "type": cls["type"],
            "jurisdiction": cls["jurisdiction"],
            "in_czds": 1 if tld in czds_active else 0,
            "status": "active",
        })
    return rows


def load_config() -> Dict[str, str]:
    """Resolve API base and admin key from env first, then config.local.json."""
    api_base = os.environ.get("API_BASE", "").rstrip("/")
    api_key = os.environ.get("ADMIN_API_KEY", "")

    if (not api_base or not api_key) and os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        api_base = api_base or str(cfg.get("api_base", "")).rstrip("/")
        api_key = api_key or str(cfg.get("admin_api_key", ""))

    if not api_base or not api_key:
        raise SystemExit(
            "Missing API_BASE / ADMIN_API_KEY. Set as env vars or in collector/config.local.json."
        )
    return {"api_base": api_base, "api_key": api_key}


def upload_rows(api_base: str, api_key: str, rows: List[Dict]) -> None:
    """POST the seed rows to the Worker admin endpoint in chunks. The
    endpoint upserts (ON CONFLICT DO UPDATE) so re-runs are safe."""
    url = f"{api_base}/api/admin/tld-dim"
    chunk = 200
    total = len(rows)
    sent = 0
    for i in range(0, total, chunk):
        batch = rows[i:i + chunk]
        payload = json.dumps({"rows": batch}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-admin-api-key": api_key,
                "User-Agent": "dom4in-tld-seed/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                if resp.status >= 300:
                    raise SystemExit(f"Upload failed [{resp.status}]: {body}")
        except urllib.error.HTTPError as err:
            raise SystemExit(f"HTTP {err.code} from {url}: {err.read().decode('utf-8', 'replace')}") from err
        sent += len(batch)
        print(f"  uploaded {sent}/{total}")


def main() -> int:
    cfg = load_config()

    print(f"Fetching IANA TLD list from {IANA_TLD_LIST_URL} ...")
    iana_text = fetch_text(IANA_TLD_LIST_URL)
    iana_tlds = parse_iana_tlds(iana_text)
    print(f"  got {len(iana_tlds)} TLDs")

    print(f"Fetching CZDS active-TLD list from {CZDS_ACTIVE_TLDS_URL} ...")
    try:
        czds_text = fetch_text(CZDS_ACTIVE_TLDS_URL)
        czds_active = parse_czds_tlds(czds_text)
        print(f"  got {len(czds_active)} CZDS-active TLDs")
    except Exception as err:
        # CZDS list is helpful but not load-bearing — proceed without if it's down.
        print(f"  warning: CZDS list unreachable ({err}); marking 0 TLDs as CZDS-active")
        czds_active = set()

    rows = build_rows(iana_tlds, czds_active)
    print(f"Built {len(rows)} rows. Uploading to {cfg['api_base']}/api/admin/tld-dim ...")
    upload_rows(cfg["api_base"], cfg["api_key"], rows)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
