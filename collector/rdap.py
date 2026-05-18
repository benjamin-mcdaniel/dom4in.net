"""RDAP (Registration Data Access Protocol) lookup helper.

RDAP is the JSON successor to WHOIS. For our purposes the only field we
care about is the sponsoring registrar's IANA ID, which lives inside the
`entities` array of the response under role 'registrar'.

We rely on the IANA RDAP bootstrap service to map TLD → responsible RDAP
server. This avoids hardcoding ~1500 TLD-specific endpoints:

    https://data.iana.org/rdap/dns.json

That file is updated by IANA whenever a new TLD comes online, so the
bootstrap stays current with no code changes.

Usage:
    from rdap import RdapClient
    client = RdapClient()
    info = client.lookup("example.com")
    # info = {"registrar_iana_id": 292, "registrar_name": "MarkMonitor Inc."}

Designed to be opt-in (sampled) rather than called on every domain — RDAP
servers rate-limit aggressively and a 1M-domain crawl would burn through
quotas. The collector should call this on, say, 1% of newly-registered
domains to build a representative registrar-share signal.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

IANA_BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
DEFAULT_TIMEOUT = 8                # per-request timeout — RDAP can be slow
BOOTSTRAP_TTL_SECONDS = 24 * 3600  # refresh the bootstrap file once a day


class RdapClient:
    """Stateful client that caches the IANA bootstrap and the (optionally)
    last successful endpoint for each TLD. Not thread-safe — instantiate
    per worker if you parallelize."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout
        self._bootstrap: Optional[Dict[str, List[str]]] = None  # tld → [base_urls]
        self._bootstrap_loaded_at: float = 0.0

    # -- bootstrap ----------------------------------------------------------

    def _load_bootstrap(self) -> None:
        req = urllib.request.Request(
            IANA_BOOTSTRAP_URL,
            headers={"User-Agent": "dom4in-rdap/1.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        # Bootstrap shape: {"services": [[[tlds], [base_urls]], ...], ...}
        mapping: Dict[str, List[str]] = {}
        for entry in data.get("services", []):
            if not isinstance(entry, list) or len(entry) < 2:
                continue
            tlds, urls = entry[0], entry[1]
            if not isinstance(tlds, list) or not isinstance(urls, list):
                continue
            cleaned_urls = [u.rstrip("/") for u in urls if isinstance(u, str)]
            for tld in tlds:
                if isinstance(tld, str):
                    mapping[tld.lower()] = cleaned_urls
        self._bootstrap = mapping
        self._bootstrap_loaded_at = time.time()

    def _ensure_bootstrap(self) -> None:
        stale = (
            self._bootstrap is None
            or (time.time() - self._bootstrap_loaded_at) > BOOTSTRAP_TTL_SECONDS
        )
        if stale:
            self._load_bootstrap()

    def _endpoints_for(self, tld: str) -> List[str]:
        self._ensure_bootstrap()
        assert self._bootstrap is not None
        return self._bootstrap.get(tld.lower(), [])

    # -- lookup -------------------------------------------------------------

    def lookup(self, domain: str) -> Optional[Dict[str, object]]:
        """Return a dict with registrar info, or None on any failure.

        We're best-effort: a single failed RDAP call costs us one sample,
        not the whole pipeline. Callers should treat None as "no signal"
        and move on."""
        if "." not in domain:
            return None
        tld = domain.rsplit(".", 1)[-1].lower()
        endpoints = self._endpoints_for(tld)
        if not endpoints:
            return None

        for base in endpoints:
            url = f"{base}/domain/{domain}"
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "Accept": "application/rdap+json",
                        "User-Agent": "dom4in-rdap/1.0",
                    },
                )
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    if resp.status != 200:
                        continue
                    body = json.loads(resp.read().decode("utf-8"))
                    info = self._extract_registrar(body)
                    if info is not None:
                        return info
            except urllib.error.HTTPError as err:
                # 404 here means "not registered" — useful negative signal.
                if err.code == 404:
                    return {"registered": False}
                continue
            except Exception:
                continue
        return None

    # -- response parsing ---------------------------------------------------

    @staticmethod
    def _extract_registrar(body: Dict) -> Optional[Dict[str, object]]:
        """Pull registrar IANA ID + name from an RDAP response.

        Schema reminder: each entity in `entities[]` has a `roles` array.
        The one with role 'registrar' carries `publicIds` like:
            [{"type": "IANA Registrar ID", "identifier": "292"}]
        and a `vcardArray` with the name as the second-cell of the 'fn' line."""
        entities = body.get("entities") or []
        if not isinstance(entities, list):
            return {"registered": True}

        for ent in entities:
            if not isinstance(ent, dict):
                continue
            roles = ent.get("roles") or []
            if "registrar" not in [str(r).lower() for r in roles]:
                continue

            iana_id: Optional[int] = None
            for pid in ent.get("publicIds") or []:
                if not isinstance(pid, dict):
                    continue
                if "iana" in str(pid.get("type", "")).lower():
                    try:
                        iana_id = int(pid.get("identifier"))
                    except (TypeError, ValueError):
                        pass

            name: Optional[str] = None
            vcard = ent.get("vcardArray")
            if isinstance(vcard, list) and len(vcard) >= 2 and isinstance(vcard[1], list):
                for prop in vcard[1]:
                    if isinstance(prop, list) and len(prop) >= 4 and prop[0] == "fn":
                        name = str(prop[3])
                        break

            return {
                "registered": True,
                "registrar_iana_id": iana_id,
                "registrar_name": name,
            }

        # Domain exists in RDAP but no registrar entity (rare). Mark as
        # registered without attribution rather than None so callers can
        # distinguish "no RDAP signal at all" from "RDAP says yes but no
        # registrar field."
        return {"registered": True}
