import argparse
import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, List, Tuple


POINTER_FILE = os.path.join(os.path.dirname(__file__), "state_pointer.json")


CHARSET = "abcdefghijklmnopqrstuvwxyz"
MAX_LENGTH = 6


@dataclass
class Pointer:
    version: int = 1
    charset: str = CHARSET
    max_length: int = MAX_LENGTH
    tld_index: int = 0
    length: int = 1
    index: int = 0  # position within the current length space
    batch_size: int = 25

    @property
    def tlds(self) -> List[str]:
        # TODO: make this configurable via a file or CLI; for now a small sample
        return [
            "com",
            "net",
            "org",
            "io",
            "co",
        ]

    def save(self) -> None:
        with open(POINTER_FILE, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls) -> "Pointer":
        if not os.path.exists(POINTER_FILE):
            return cls()
        with open(POINTER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(**data)


def index_to_label(idx: int, length: int, charset: str) -> str:
    base = len(charset)
    chars = []
    for _ in range(length):
        chars.append(charset[idx % base])
        idx //= base
    return "".join(reversed(chars))


def generate_batch(pointer: Pointer) -> List[Tuple[str, str]]:
    """Return a batch of (domain, tld) pairs and advance the pointer in-memory.

    Pointer is not saved here; caller should call pointer.save() after a successful batch.
    """
    batch: List[Tuple[str, str]] = []

    while len(batch) < pointer.batch_size and pointer.length <= pointer.max_length:
        tld = pointer.tlds[pointer.tld_index]

        # total combinations for this length
        total_for_length = len(pointer.charset) ** pointer.length
        if pointer.index >= total_for_length:
            # Move to next TLD for this length
            pointer.tld_index += 1
            pointer.index = 0
            if pointer.tld_index >= len(pointer.tlds):
                pointer.tld_index = 0
                pointer.length += 1
            continue

        label = index_to_label(pointer.index, pointer.length, pointer.charset)
        pointer.index += 1

        domain = f"{label}.{tld}"
        batch.append((domain, tld))

    return batch


# --- Domain checks: stubs for now ---


def check_domain_dns(domain: str) -> Dict:
    """Stub for DNS/registration check.

    Returns a dict with booleans/labels, to be implemented with real DNS + WHOIS later.
    """
    # TODO: implement DNS + optional WHOIS
    return {
        "registered": False,
        "has_dns": False,
    }


def check_domain_http(domain: str) -> Dict:
    """Stub for HTTP/product check.

    Only called for domains that appear registered.
    """
    # TODO: implement HTTP probing and classification
    return {
        "usage_state": "no_website",  # no_website | parked_or_placeholder | active_site
        "product_state": "unknown",   # active_product | unknown
    }


# --- Aggregation ---


def init_aggregates() -> Dict:
    return {
        "global": {
            "domains_tracked_lifetime": 0,
            "domains_tracked_24h": 0,
        },
        "length_stats": {},  # key: length -> counters
        "tld_stats": {},     # key: tld -> counters (for future use)
    }


def update_aggregates(aggr: Dict, domain: str, tld: str, length: int, dns_info: Dict, http_info: Dict) -> None:
    g = aggr["global"]
    g["domains_tracked_lifetime"] += 1
    g["domains_tracked_24h"] += 1

    ls = aggr["length_stats"].setdefault(length, {
        "length": length,
        "total_possible": len(CHARSET) ** length,
        "tracked_count": 0,
        "unregistered_found": 0,
        "unused_found": 0,
    })

    ls["tracked_count"] += 1

    if not dns_info.get("registered"):
        ls["unregistered_found"] += 1
    else:
        usage_state = http_info.get("usage_state")
        if usage_state in {"no_website", "parked_or_placeholder"}:
            ls["unused_found"] += 1

    ts = aggr["tld_stats"].setdefault(tld, {
        "tld": tld,
        "domains_checked_total": 0,
        "short_domains_checked_total": 0,
        "short_unregistered_count": 0,
        "short_no_website_count": 0,
        "short_active_site_count": 0,
    })

    ts["domains_checked_total"] += 1
    if length <= MAX_LENGTH:
        ts["short_domains_checked_total"] += 1
        if not dns_info.get("registered"):
            ts["short_unregistered_count"] += 1
        else:
            usage_state = http_info.get("usage_state")
            if usage_state in {"no_website", "parked_or_placeholder"}:
                ts["short_no_website_count"] += 1
            elif usage_state == "active_site":
                ts["short_active_site_count"] += 1


def build_payload(date_str: str, aggr: Dict) -> Dict:
    length_stats_list = sorted(aggr["length_stats"].values(), key=lambda x: x["length"])

    # tld_stats can be added later to the upload payload when the Worker supports it
    payload = {
        "date": date_str,
        "global": aggr["global"],
        "length_stats": length_stats_list,
    }

    return payload


# --- Upload stub ---


def upload_aggregate(api_base: str, api_key: str, payload: Dict) -> None:
    """Stub for calling the Worker admin endpoint.

    Implement this later with `requests` or `httpx`.
    """
    # Example target: f"{api_base.rstrip('/')}/api/admin/upload-aggregate"
    # Use header: {"x-admin-api-key": api_key}
    # TODO: implement
    print("[upload_aggregate] Would upload:")
    print(json.dumps(payload, indent=2))


def run(batch_size: int, api_base: str, api_key: str) -> None:
    pointer = Pointer.load()
    if batch_size:
        pointer.batch_size = batch_size

    batch = generate_batch(pointer)
    if not batch:
        print("No more domains to process within current configuration.")
        return

    aggr = init_aggregates()

    for domain, tld in batch:
        label = domain.split(".")[0]
        length = len(label)

        dns_info = check_domain_dns(domain)
        http_info = {"usage_state": "no_website", "product_state": "unknown"}
        if dns_info.get("registered"):
            http_info = check_domain_http(domain)

        update_aggregates(aggr, domain, tld, length, dns_info, http_info)

    # Build payload for "today"
    date_str = datetime.now(timezone.utc).date().isoformat()
    payload = build_payload(date_str, aggr)

    upload_aggregate(api_base, api_key, payload)

    # If upload succeeds (once implemented), save pointer
    pointer.save()


def main() -> None:
    parser = argparse.ArgumentParser(description="dom4in.net collector")
    parser.add_argument("--batch-size", type=int, default=25, help="Number of domains to process per run")
    parser.add_argument("--api-base", type=str, default="https://dom4in.net", help="Base URL for the backend API")
    parser.add_argument("--api-key", type=str, default="changeme-admin-key", help="Admin API key for upload")

    args = parser.parse_args()
    run(args.batch_size, args.api_base, args.api_key)


if __name__ == "__main__":
    main()
