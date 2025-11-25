import argparse
import json
import os
import random
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, List, Tuple


BASE_DIR = os.path.dirname(__file__)
POINTER_FILE = os.path.join(BASE_DIR, "state_pointer.json")
WORDS_POINTER_FILE = os.path.join(BASE_DIR, "state_words_pointer.json")
REPO_ROOT = os.path.abspath(os.path.join(BASE_DIR, os.pardir))
WORDS_FILE = os.path.join(REPO_ROOT, "wordlists", "words_10_all.txt")
CONFIG_FILE = os.path.join(BASE_DIR, "config.local.json")


CHARSET = "abcdefghijklmnopqrstuvwxyz"
MAX_LENGTH = 6

# Default DNS-over-HTTPS resolvers (you can override these in config.local.json)
DEFAULT_DNS_RESOLVERS = [
    {
        "name": "cloudflare",
        "url": "https://cloudflare-dns.com/dns-query",
    },
    {
        "name": "google",
        "url": "https://dns.google/resolve",
    },
    {
        "name": "quad9",
        "url": "https://dns.quad9.net/dns-query",
    },
    {
        "name": "opendns",
        "url": "https://doh.opendns.com/dns-query",
    },
]


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


@dataclass
class WordPointer:
    version: int = 1
    index: int = 0  # index into the word list

    def save(self) -> None:
        with open(WORDS_POINTER_FILE, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls) -> "WordPointer":
        if not os.path.exists(WORDS_POINTER_FILE):
            return cls()
        with open(WORDS_POINTER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(**data)


def load_words() -> List[str]:
    if not os.path.exists(WORDS_FILE):
        raise RuntimeError(
            f"Word file {WORDS_FILE} not found. Run `python load_dictionary.py` in the collector folder first."
        )
    with open(WORDS_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def generate_word_batch(word_pointer: WordPointer, tlds: List[str], count: int) -> List[Tuple[str, str]]:
    """Generate up to `count` (domain, tld) pairs from the word list and advance the word pointer."""
    words = load_words()
    if not words:
        return []

    batch: List[Tuple[str, str]] = []
    idx = word_pointer.index
    total_words = len(words)
    tld_index = 0

    while len(batch) < count and total_words > 0:
        if idx >= total_words:
            # Restart from beginning for now when we reach the end
            idx = 0

        word = words[idx]
        idx += 1

        # cycle through TLDs for this word until we hit count
        for _ in range(len(tlds)):
            if len(batch) >= count:
                break
            tld = tlds[tld_index]
            tld_index = (tld_index + 1) % len(tlds)
            domain = f"{word}.{tld}"
            batch.append((domain, tld))

    word_pointer.index = idx
    return batch


def build_doh_url(resolver_url: str, domain: str) -> str:
    """Build a DNS-over-HTTPS URL for an A record lookup.

    Both Cloudflare and Google support name/type query parameters returning DNS JSON.
    """
    parsed = urllib.parse.urlparse(resolver_url)
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path or ''}"
    query = urllib.parse.urlencode({"name": domain, "type": "A"})
    return f"{base}?{query}"


def check_domain_dns(domain: str, resolver: Dict) -> Dict:
    """DNS/registration check using DNS-over-HTTPS.

    resolver is a dict with at least a 'url' key.
    """
    url = build_doh_url(resolver["url"], domain)
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/dns-json",
            "User-Agent": "dom4in-collector/1.0",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status != 200:
                return {"resolver_error": True}
            body = resp.read()
    except (urllib.error.URLError, socket.timeout, ssl.SSLError):
        return {"resolver_error": True}

    try:
        data = json.loads(body.decode("utf-8", errors="ignore"))
    except Exception:
        return {"resolver_error": True}

    # Basic health: if the resolver returns malformed JSON or no Status, mark as error.
    status_code = data.get("Status")
    if status_code is None:
        return {"resolver_error": True}

    # Status 0 with at least one Answer means we treat as registered/has_dns.
    if status_code == 0 and data.get("Answer"):
        return {
            "registered": True,
            "has_dns": True,
            "resolver_error": False,
        }

    # Non-zero status or no answers: treat as no DNS/likely unregistered, but resolver is healthy.
    return {
        "registered": False,
        "has_dns": False,
        "resolver_error": False,
    }


def check_domain_http(domain: str) -> Dict:
    """Simple HTTP/product check using urllib.

    Only called for domains that appear registered.
    """
    url = f"https://{domain}"
    req = urllib.request.Request(url, headers={"User-Agent": "dom4in-collector/1.0"})

    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
            status = resp.getcode() or 0
            content_type = resp.headers.get("Content-Type", "")
            body = resp.read(4096)  # read up to 4KB
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, Exception):
        # Any network/HTTP error, including RemoteDisconnected, is treated as no website.
        return {
            "usage_state": "no_website",
            "product_state": "unknown",
        }

    text_snippet = ""
    if isinstance(body, bytes):
        try:
            text_snippet = body.decode("utf-8", errors="ignore")
        except Exception:
            text_snippet = ""

    text_lower = text_snippet.lower()

    # Basic usage classification
    if status >= 500:
        usage_state = "no_website"
    elif any(keyword in text_lower for keyword in ["domain parking", "parked domain", "this domain is for sale"]):
        usage_state = "parked_or_placeholder"
    elif status in (301, 302, 303, 307, 308):
        usage_state = "parked_or_placeholder"
    elif "text/html" in content_type and len(text_snippet.strip()) > 0:
        usage_state = "active_site"
    else:
        usage_state = "no_website"

    # Very rough product detection
    product_keywords = ["pricing", "plans", "subscribe", "sign up", "buy now", "api docs", "api documentation"]
    product_state = "active_product" if any(k in text_lower for k in product_keywords) else "unknown"

    return {
        "usage_state": usage_state,
        "product_state": product_state,
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


def upload_aggregate(api_base: str, api_key: str, payload: Dict, dry_run: bool = False) -> Tuple[int, str]:
    """Call the Worker admin endpoint (or just print in dry-run mode).

    Returns a tuple of (status_code, body_text) for logging.
    """
    url = f"{api_base.rstrip('/')}/api/admin/upload-aggregate"

    if dry_run:
        print(f"[dry-run] Would POST to {url} with payload:")
        print(json.dumps(payload, indent=2))
        return 0, "dry-run"

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-admin-api-key": api_key,
            "User-Agent": "dom4in-collector/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
            text = body.decode("utf-8", errors="ignore")
            print(f"[upload_aggregate] Status {resp.status}: {text}")
            return resp.status, text
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="ignore")
        print(f"[upload_aggregate] HTTP error {e.code}: {text}")
        return e.code, text
    except urllib.error.URLError as e:
        print(f"[upload_aggregate] URL error: {e}")
        return 0, str(e)


def reset_db(api_base: str, api_key: str) -> None:
    """Call the admin reset endpoint to clear aggregated stats in D1."""
    url = f"{api_base.rstrip('/')}/api/admin/reset-stats"
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "x-admin-api-key": api_key,
            "User-Agent": "dom4in-collector/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
            print(f"[reset_db] Status {resp.status}: {body.decode('utf-8', errors='ignore')}")
    except urllib.error.HTTPError as e:
        print(f"[reset_db] HTTP error {e.code}: {e.read().decode('utf-8', errors='ignore')}")
    except urllib.error.URLError as e:
        print(f"[reset_db] URL error: {e}")


def reset_pointer() -> None:
    if os.path.exists(POINTER_FILE):
        os.remove(POINTER_FILE)
        print(f"Pointer file removed: {POINTER_FILE}")
    else:
        print("Pointer file does not exist; nothing to reset.")


def run(
    api_base: str,
    api_key: str,
    dry_run: bool = False,
    print_each: bool = False,
    use_short: bool = True,
    use_words: bool = False,
    block_pause_seconds: int = 0,
) -> None:
    pointer = Pointer.load()
    word_pointer = WordPointer.load()

    # Load DNS resolvers (from config if available, else defaults)
    config_resolvers = []
    per_request_delay_ms = 0
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            config_resolvers = cfg.get("dns_resolvers", [])
            per_request_delay_ms = int(cfg.get("per_request_delay_ms", 0))
        except Exception:
            config_resolvers = []
            per_request_delay_ms = 0

    resolvers = config_resolvers or DEFAULT_DNS_RESOLVERS
    if not resolvers:
        print("Error: no DNS resolvers configured.")
        return

    resolver_index = 0
    queries_with_current = 0

    # Default mode: if neither flag is set, behave as "short" mode
    if not use_short and not use_words:
        use_short = True

    # Alternate between short and words when both are enabled
    next_mode = "short" if use_short else "words"

    while True:
        # Start fresh aggregates for this block
        aggr = init_aggregates()

        # Pick a random block size between 25 and 80
        block_count = random.randint(25, 80)

        # Decide which generator to use for this block
        mode_for_block = next_mode
        if mode_for_block == "short" and not use_short and use_words:
            mode_for_block = "words"
        elif mode_for_block == "words" and not use_words and use_short:
            mode_for_block = "short"

        if mode_for_block == "short":
            pointer.batch_size = block_count
            batch = generate_batch(pointer)
            # set up tlds for short mode
            tlds_for_block = pointer.tlds
        else:
            tlds_for_block = pointer.tlds  # reuse the same TLD set
            batch = generate_word_batch(word_pointer, tlds_for_block, block_count)

        if not batch:
            print("No more domains to process within current configuration.")
            break

        print(f"Starting block: {mode_for_block} - {len(batch)} domains")

        for domain, tld in batch:
            # Rotate resolver every 25 queries
            if queries_with_current >= 25:
                resolver_index = (resolver_index + 1) % len(resolvers)
                queries_with_current = 0

            resolver = resolvers[resolver_index]

            label = domain.split(".")[0]
            length = len(label)

            dns_info = check_domain_dns(domain, resolver)
            if dns_info.get("resolver_error"):
                # Mark this resolver as unhealthy for this run and move on to the next
                print(f"Resolver {resolver.get('name', resolver_index)} had an error, rotating.")
                resolver_index = (resolver_index + 1) % len(resolvers)
                queries_with_current = 0
                # Try once more with the next resolver
                dns_info = check_domain_dns(domain, resolvers[resolver_index])

            queries_with_current += 1

            http_info = {"usage_state": "no_website", "product_state": "unknown"}
            if dns_info.get("registered"):
                http_info = check_domain_http(domain)

            if print_each:
                print(
                    f"{domain} -> registered={dns_info.get('registered')}, "
                    f"has_dns={dns_info.get('has_dns')}, "
                    f"usage={http_info.get('usage_state')}, "
                    f"product={http_info.get('product_state')}"
                )

            update_aggregates(aggr, domain, tld, length, dns_info, http_info)

            # Optional small delay to avoid hammering endpoints too hard
            if per_request_delay_ms > 0:
                time.sleep(per_request_delay_ms / 1000.0)

        # Build and upload payload for this block
        date_str = datetime.now(timezone.utc).date().isoformat()
        payload = build_payload(date_str, aggr)

        length_stats_list = payload.get("length_stats", [])
        total_tracked_block = sum(ls.get("tracked_count", 0) for ls in length_stats_list)
        status_code, body_text = upload_aggregate(api_base, api_key, payload, dry_run=dry_run)

        # Simple console summary for the block
        print(f"{mode_for_block} - {total_tracked_block} domains updated - {status_code} {body_text}")

        # Save pointers so progress is kept
        pointer.save()
        word_pointer.save()

        # Flip mode when both are enabled
        if use_short and use_words:
            next_mode = "words" if mode_for_block == "short" else "short"

        # Optional pause between blocks to avoid constant hammering
        if block_pause_seconds > 0:
            print(f"Pausing for {block_pause_seconds} seconds before next block…")
            time.sleep(block_pause_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="dom4in.net collector")
    parser.add_argument("--api-base", type=str, default=None, help="Base URL for the backend API (overrides config file)")
    parser.add_argument("--api-key", type=str, default=None, help="Admin API key for upload (overrides config file)")
    parser.add_argument("--dry-run", action="store_true", help="Do not POST to backend, just print payload")
    parser.add_argument("--print-each", action="store_true", help="Print each domain and its classification as it is processed")
    parser.add_argument("--reset-pointer", action="store_true", help="Reset pointer to the beginning and exit")
    parser.add_argument("--reset-db", action="store_true", help="Reset aggregated stats in the backend database and exit")
    parser.add_argument("--short", action="store_true", help="Enable short label mode (1-6 characters from charset)")
    parser.add_argument("--word", action="store_true", help="Enable word-based mode using words_10_all.txt")
    parser.add_argument("--pause", type=int, default=None, help="Optional pause in seconds between blocks when running continuously")

    args = parser.parse_args()

    # Load defaults from local config file if present
    config = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception as e:
            print(f"Warning: failed to read config from {CONFIG_FILE}: {e}")

    api_base = args.api_base or config.get("api_base", "https://dom4in.net")
    api_key = args.api_key or config.get("admin_api_key", "")
    cfg_block_pause = int(config.get("block_pause_seconds", 0)) if config else 0
    if args.pause is not None:
        block_pause_seconds = max(0, args.pause)
    else:
        block_pause_seconds = max(0, cfg_block_pause)

    if args.reset_db:
        if not api_key:
            print("Error: admin API key is required to reset DB.")
            return
        reset_db(api_base, api_key)
        # Optionally also reset pointer in the same 1-liner
        if args.reset_pointer:
            reset_pointer()
        return

    if args.reset_pointer:
        reset_pointer()
        return

    if not api_key:
        print("Error: admin API key is required. Set it in collector/config.local.json or pass --api-key.")
        return

    run(
        api_base,
        api_key,
        dry_run=args.dry_run,
        print_each=args.print_each,
        use_short=args.short,
        use_words=args.word,
        block_pause_seconds=block_pause_seconds,
    )


if __name__ == "__main__":
    main()
