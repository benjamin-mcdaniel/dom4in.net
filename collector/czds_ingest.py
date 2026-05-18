"""CZDS zone-file ingestion pipeline.

Runs nightly to:
  1. Authenticate with ICANN CZDS (username/password → JWT).
  2. List approved zone files (only TLDs we've been granted access to).
  3. Download each zone file (gzipped, plaintext one-domain-per-line format).
  4. Extract the set of registered second-level labels.
  5. Diff against yesterday's snapshot for the same TLD (stored in R2).
  6. Write today's snapshot to R2 (private, never public).
  7. POST today's aggregate counts (registered_total, new_today, dropped_today)
     to /api/admin/zone-diff. Per-domain detail never leaves this script.
  8. Scan today's new-registration set against active brand_watchlist
     patterns and POST any hits to /api/admin/brand-match. The matched
     domain DOES get persisted server-side because it IS the Brand
     Sentinel product — but it's never returned by public endpoints.

Designed for GitHub Actions nightly cron. Resumes naturally if interrupted:
each TLD is processed independently and the diff endpoint is idempotent on
(snap_date, tld).

Credentials are pulled from environment variables:
    CZDS_USERNAME, CZDS_PASSWORD      — ICANN CZDS portal account
    API_BASE, ADMIN_API_KEY            — dom4in backend
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID,
    R2_SECRET_ACCESS_KEY, R2_BUCKET    — Cloudflare R2 (for snapshots)

If R2 vars are missing, the script falls back to /tmp for "yesterday's"
snapshot (won't survive GHA reruns but lets you smoke-test locally).
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Set, Tuple

CZDS_AUTH_URL = "https://account-api.icann.org/api/authenticate"
CZDS_BASE_URL = "https://czds-api.icann.org"

# Conservative ceilings. CZDS hosts ~1500 TLDs but most are small. The .com
# zone is ~5GB compressed / ~25GB uncompressed and dominates everything.
# Skip .com unless explicitly opted in via CZDS_INCLUDE_COM=1 — most of
# the analytical value is in newer/smaller TLDs.
MAX_ZONE_BYTES = 500 * 1024 * 1024   # 500 MB compressed cap per TLD per run
HTTP_TIMEOUT = 600                    # 10 min, large zones are slow
BRAND_MATCH_BATCH = 200               # rows per POST to /api/admin/brand-match
ZONE_DIFF_BATCH = 200                 # rows per POST to /api/admin/zone-diff


# Domain pattern in CZDS zone files: typically `<label>.<tld>. NN IN NS ...`.
# We only need the second-level label (everything before the first dot).
ZONE_LINE_RE = re.compile(r"^([a-z0-9-]+)\.([a-z0-9-]+)\.\s+\d+\s+IN\s+NS\s+", re.IGNORECASE)


@dataclass
class CzdsConfig:
    username: str
    password: str
    include_com: bool
    api_base: str
    api_key: str
    r2_account_id: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_bucket: str

    @property
    def has_r2(self) -> bool:
        return bool(
            self.r2_account_id
            and self.r2_access_key_id
            and self.r2_secret_access_key
            and self.r2_bucket
        )


def load_config() -> CzdsConfig:
    username = os.environ.get("CZDS_USERNAME", "")
    password = os.environ.get("CZDS_PASSWORD", "")
    if not username or not password:
        raise SystemExit("CZDS_USERNAME and CZDS_PASSWORD must be set in env.")

    api_base = os.environ.get("API_BASE", "https://dom4in.net").rstrip("/")
    api_key = os.environ.get("ADMIN_API_KEY", "")
    if not api_key:
        raise SystemExit("ADMIN_API_KEY must be set in env.")

    return CzdsConfig(
        username=username,
        password=password,
        include_com=os.environ.get("CZDS_INCLUDE_COM", "0") == "1",
        api_base=api_base,
        api_key=api_key,
        r2_account_id=os.environ.get("R2_ACCOUNT_ID", ""),
        r2_access_key_id=os.environ.get("R2_ACCESS_KEY_ID", ""),
        r2_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY", ""),
        r2_bucket=os.environ.get("R2_BUCKET", ""),
    )


def authenticate(cfg: CzdsConfig) -> str:
    """Trade username/password for a CZDS JWT. Tokens last ~24 hours; we
    re-auth on every run rather than caching."""
    body = json.dumps({"username": cfg.username, "password": cfg.password}).encode("utf-8")
    req = urllib.request.Request(
        CZDS_AUTH_URL,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    token = payload.get("accessToken") or payload.get("access_token")
    if not token:
        raise SystemExit(f"CZDS auth returned no token: {payload}")
    return str(token)


def list_approved_zones(token: str) -> List[str]:
    """CZDS endpoint returns the list of zone-file URLs the account is
    approved to download. The TLD is the last path component, minus '.zone'."""
    req = urllib.request.Request(
        f"{CZDS_BASE_URL}/czds/downloads/links",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        links = json.loads(resp.read().decode("utf-8"))
    if not isinstance(links, list):
        raise SystemExit(f"Unexpected CZDS links response: {links!r}")
    return links


def tld_from_link(link: str) -> str:
    """https://.../czds/downloads/io.zone → 'io'"""
    name = link.rsplit("/", 1)[-1]
    return name.removesuffix(".zone").lower()


def stream_zone_labels(token: str, link: str) -> Tuple[Set[str], int]:
    """Stream a zone file and return (set-of-labels, bytes-downloaded).

    Zone files are served gzipped; we decompress on the fly so we never hold
    the full ~25GB .com uncompressed in memory. Each `<label>.<tld>. ... IN NS`
    occurrence yields one label; duplicates within the file (a domain has
    multiple NS records) are deduped by the set."""
    req = urllib.request.Request(
        link,
        headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"},
    )
    labels: Set[str] = set()
    bytes_downloaded = 0
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        # The server already gzips; urllib doesn't auto-decompress when we
        # explicitly asked for gzip, so wrap in a GzipFile.
        gz = gzip.GzipFile(fileobj=resp)
        # Iterate line-by-line; decode bytes to str leniently.
        reader = io.TextIOWrapper(gz, encoding="utf-8", errors="replace")
        for line in reader:
            bytes_downloaded += len(line)
            if bytes_downloaded > MAX_ZONE_BYTES:
                break
            m = ZONE_LINE_RE.match(line)
            if m:
                labels.add(m.group(1).lower())
    return labels, bytes_downloaded


# --------------------------------------------------------------------------
# R2 snapshot storage (for yesterday-vs-today diff).
# We use the S3-compatible API. Each TLD gets a single "latest.txt.gz" object;
# we read it, then overwrite it with today's set. Historical retention is the
# job of R2 versioning (enable on the bucket once).
# --------------------------------------------------------------------------

def _r2_endpoint(cfg: CzdsConfig) -> str:
    return f"https://{cfg.r2_account_id}.r2.cloudflarestorage.com"


def _aws_sigv4_headers(
    method: str,
    url: str,
    payload: bytes,
    access_key: str,
    secret_key: str,
    region: str = "auto",
    service: str = "s3",
    extra_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Minimal AWS Signature V4 signer. R2 accepts SigV4 with region='auto'.
    We don't pull boto3 to keep the collector dependency-light."""
    import hmac
    from urllib.parse import urlparse, quote

    parsed = urlparse(url)
    host = parsed.netloc
    path = parsed.path or "/"
    query = parsed.query or ""

    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    payload_hash = hashlib.sha256(payload).hexdigest()

    headers = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if extra_headers:
        for k, v in extra_headers.items():
            headers[k.lower()] = v

    signed_header_names = sorted(headers.keys())
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in signed_header_names)
    signed_headers = ";".join(signed_header_names)

    # Canonical query string: keys sorted, values URL-encoded
    if query:
        pairs = []
        for kv in query.split("&"):
            if "=" in kv:
                k, v = kv.split("=", 1)
            else:
                k, v = kv, ""
            pairs.append((quote(k, safe="-_.~"), quote(v, safe="-_.~")))
        pairs.sort()
        canonical_query = "&".join(f"{k}={v}" for k, v in pairs)
    else:
        canonical_query = ""

    canonical_uri = quote(path, safe="/-_.~")

    canonical_request = "\n".join([
        method.upper(),
        canonical_uri,
        canonical_query,
        canonical_headers,
        signed_headers,
        payload_hash,
    ])

    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    def _sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = _sign(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    k_signing = _sign(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    auth_header = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    final = dict(headers)
    final["Authorization"] = auth_header
    # urllib expects header keys with normal casing; values are fine.
    return {k.title() if k != "Authorization" else k: v for k, v in final.items()}


def r2_get_labels(cfg: CzdsConfig, tld: str) -> Set[str]:
    """Read the previous snapshot for a TLD from R2. Missing object → empty set."""
    if not cfg.has_r2:
        # Local fallback: /tmp/dom4in_snapshots/<tld>.txt.gz
        path = os.path.join("/tmp/dom4in_snapshots", f"{tld}.txt.gz")
        if not os.path.exists(path):
            return set()
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            return {line.strip() for line in f if line.strip()}

    url = f"{_r2_endpoint(cfg)}/{cfg.r2_bucket}/zones/{tld}/latest.txt.gz"
    headers = _aws_sigv4_headers(
        "GET", url, b"", cfg.r2_access_key_id, cfg.r2_secret_access_key
    )
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return set()
        raise

    with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
        text = gz.read().decode("utf-8", errors="replace")
    return {line.strip() for line in text.splitlines() if line.strip()}


def r2_put_labels(cfg: CzdsConfig, tld: str, labels: Set[str]) -> None:
    """Write today's snapshot to R2 (or /tmp fallback)."""
    body = ("\n".join(sorted(labels)) + "\n").encode("utf-8")
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(body)
    payload = buf.getvalue()

    if not cfg.has_r2:
        os.makedirs("/tmp/dom4in_snapshots", exist_ok=True)
        path = os.path.join("/tmp/dom4in_snapshots", f"{tld}.txt.gz")
        with open(path, "wb") as f:
            f.write(payload)
        return

    url = f"{_r2_endpoint(cfg)}/{cfg.r2_bucket}/zones/{tld}/latest.txt.gz"
    extra = {"content-type": "application/gzip"}
    headers = _aws_sigv4_headers(
        "PUT", url, payload, cfg.r2_access_key_id, cfg.r2_secret_access_key,
        extra_headers=extra,
    )
    req = urllib.request.Request(url, data=payload, headers=headers, method="PUT")
    with urllib.request.urlopen(req, timeout=300) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"R2 PUT failed [{resp.status}] for {tld}")


# --------------------------------------------------------------------------
# Brand watchlist scanning. We pull active patterns from /api/admin/state
# (one row, key='active_watchlist_patterns', JSON-encoded). Keeping this in
# state instead of querying a dedicated endpoint avoids one more route on
# the Worker; the Worker is the source of truth either way.
# --------------------------------------------------------------------------

def fetch_active_patterns(cfg: CzdsConfig) -> List[Dict]:
    """Returns a list of {id, pattern, match_type, tld_filter (list|None)}.

    If the state key is absent or unreadable, returns []. That just means
    no brand matching this run — the rest of the pipeline still runs."""
    url = f"{cfg.api_base}/api/admin/state?key=active_watchlist_patterns"
    req = urllib.request.Request(url, headers={"x-admin-api-key": cfg.api_key})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    val = data.get("value")
    if not isinstance(val, list):
        return []
    out = []
    for r in val:
        if not isinstance(r, dict):
            continue
        pat = r.get("pattern")
        if not isinstance(pat, str) or not pat:
            continue
        out.append({
            "id": r.get("id"),
            "pattern": pat.lower(),
            "match_type": r.get("match_type", "exact"),
            "tld_filter": r.get("tld_filter"),
        })
    return out


def scan_for_brand_matches(
    tld: str,
    new_labels: Iterable[str],
    patterns: List[Dict],
) -> List[Dict]:
    """Yield {watchlist_id, pattern, matched_domain, tld, source} for each
    new-domain hit. Exact match is the cheap common path; 'contains' is a
    substring search; 'fuzzy' is left as a TODO (Levenshtein/edit distance)."""
    if not patterns:
        return []
    results: List[Dict] = []
    new_set = set(new_labels)
    for p in patterns:
        tld_filter = p.get("tld_filter")
        if isinstance(tld_filter, list) and tld_filter and tld not in tld_filter:
            continue
        pattern = p["pattern"]
        mt = p["match_type"]
        if mt == "exact":
            if pattern in new_set:
                results.append({
                    "watchlist_id": p.get("id"),
                    "pattern": pattern,
                    "matched_domain": f"{pattern}.{tld}",
                    "tld": tld,
                    "source": "czds-diff",
                })
        elif mt == "contains":
            for label in new_set:
                if pattern in label:
                    results.append({
                        "watchlist_id": p.get("id"),
                        "pattern": pattern,
                        "matched_domain": f"{label}.{tld}",
                        "tld": tld,
                        "source": "czds-diff",
                    })
        # 'fuzzy' deferred — needs a Levenshtein bound and would be slow
        # against millions of labels without an index.
    return results


# --------------------------------------------------------------------------
# Upload helpers (shared shape: POST /api/admin/... with {rows: [...]}).
# --------------------------------------------------------------------------

def post_admin(cfg: CzdsConfig, path: str, rows: List[Dict]) -> None:
    if not rows:
        return
    url = f"{cfg.api_base}{path}"
    payload = json.dumps({"rows": rows}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-admin-api-key": cfg.api_key,
            "User-Agent": "dom4in-czds/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"POST {path} failed [{resp.status}]: {resp.read()}")


def chunked(rows: List[Dict], n: int) -> Iterable[List[Dict]]:
    for i in range(0, len(rows), n):
        yield rows[i:i + n]


# --------------------------------------------------------------------------
# Main pipeline.
# --------------------------------------------------------------------------

def process_tld(cfg: CzdsConfig, token: str, link: str, patterns: List[Dict]) -> Optional[Dict]:
    """Returns the zone_diff_daily row for this TLD, or None on skip/failure."""
    tld = tld_from_link(link)

    if tld == "com" and not cfg.include_com:
        # .com is ~25GB uncompressed; skip unless explicitly opted in.
        print(f"  [skip] {tld}: .com excluded by default (set CZDS_INCLUDE_COM=1 to override)")
        return None

    snap_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"  [start] {tld}")
    t0 = time.time()
    try:
        today_labels, bytes_dl = stream_zone_labels(token, link)
    except Exception as err:
        print(f"  [error] {tld}: zone stream failed: {err}")
        return None

    yesterday_labels = r2_get_labels(cfg, tld)
    new_labels = today_labels - yesterday_labels
    dropped_labels = yesterday_labels - today_labels

    # Save today's snapshot for tomorrow's diff. Done before brand match
    # POSTs so even if the brand match step fails the snapshot is in place.
    try:
        r2_put_labels(cfg, tld, today_labels)
    except Exception as err:
        print(f"  [warn] {tld}: snapshot write failed: {err}")

    # Brand match scan only against today's new registrations.
    matches = scan_for_brand_matches(tld, new_labels, patterns)
    if matches:
        for batch in chunked(matches, BRAND_MATCH_BATCH):
            try:
                post_admin(cfg, "/api/admin/brand-match", batch)
            except Exception as err:
                print(f"  [warn] {tld}: brand-match POST failed: {err}")

    elapsed = int(time.time() - t0)
    print(
        f"  [done ] {tld}: total={len(today_labels):,} new={len(new_labels):,} "
        f"dropped={len(dropped_labels):,} matches={len(matches)} {elapsed}s"
    )

    return {
        "snap_date": snap_date,
        "tld": tld,
        "registered_total": len(today_labels),
        "new_today": len(new_labels),
        "dropped_today": len(dropped_labels),
        "zone_size_bytes": bytes_dl,
        "source": "czds",
    }


def main() -> int:
    cfg = load_config()

    print("Authenticating with CZDS ...")
    token = authenticate(cfg)

    print("Listing approved zone-file links ...")
    links = list_approved_zones(token)
    print(f"  {len(links)} TLDs approved")

    print("Fetching active brand-watchlist patterns ...")
    patterns = fetch_active_patterns(cfg)
    print(f"  {len(patterns)} active patterns")

    diff_rows: List[Dict] = []
    for link in links:
        row = process_tld(cfg, token, link, patterns)
        if row:
            diff_rows.append(row)
        # Flush diff aggregates every 20 TLDs so partial runs still post data.
        if len(diff_rows) >= ZONE_DIFF_BATCH:
            try:
                post_admin(cfg, "/api/admin/zone-diff", diff_rows)
            except Exception as err:
                print(f"  [warn] zone-diff partial flush failed: {err}")
            diff_rows = []

    # Final flush.
    for batch in chunked(diff_rows, ZONE_DIFF_BATCH):
        post_admin(cfg, "/api/admin/zone-diff", batch)

    print("CZDS ingest complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
