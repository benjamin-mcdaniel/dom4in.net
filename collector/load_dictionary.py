import os
import urllib.request

BASE_DIR = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(BASE_DIR, os.pardir))
WORDS_DIR = os.path.join(REPO_ROOT, "wordlists")
WORDS_ALL_PATH = os.path.join(WORDS_DIR, "words_10_all.txt")

# POS-specific English word lists (10k, lowercase) from:
# https://github.com/david47k/top-english-wordlists
NOUNS_URL = "https://raw.githubusercontent.com/david47k/top-english-wordlists/master/top_english_nouns_lower_10000.txt"
VERBS_URL = "https://raw.githubusercontent.com/david47k/top-english-wordlists/master/top_english_verbs_lower_10000.txt"
ADJECTIVES_URL = "https://raw.githubusercontent.com/david47k/top-english-wordlists/master/top_english_adjs_lower_10000.txt"
ADVERBS_URL = "https://raw.githubusercontent.com/david47k/top-english-wordlists/master/top_english_advs_lower_10000.txt"

WORDS_NOUNS_PATH = os.path.join(WORDS_DIR, "words_10_nouns.txt")
WORDS_VERBS_PATH = os.path.join(WORDS_DIR, "words_10_verbs.txt")
WORDS_ADJECTIVES_PATH = os.path.join(WORDS_DIR, "words_10_adjectives.txt")
WORDS_ADVERBS_PATH = os.path.join(WORDS_DIR, "words_10_adverbs.txt")

MAX_LENGTH = 10


def _download(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _clean_words(text: str):
    seen = set()
    for line in text.splitlines():
        w = line.strip().lower()
        if not w:
            continue
        if len(w) > MAX_LENGTH:
            continue
        if not w.isalpha():
            continue
        if w in seen:
            continue
        seen.add(w)
        yield w


def _write_words(path: str, words) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for w in sorted(words):
            f.write(w + "\n")


def main() -> None:
    print("Downloading POS-based English word lists (10k each, lowercase)…")

    os.makedirs(WORDS_DIR, exist_ok=True)

    # Download and clean each POS list separately
    nouns = set(_clean_words(_download(NOUNS_URL)))
    verbs = set(_clean_words(_download(VERBS_URL)))
    adjs = set(_clean_words(_download(ADJECTIVES_URL)))
    advs = set(_clean_words(_download(ADVERBS_URL)))

    # Write per-POS files
    _write_words(WORDS_NOUNS_PATH, nouns)
    _write_words(WORDS_VERBS_PATH, verbs)
    _write_words(WORDS_ADJECTIVES_PATH, adjs)
    _write_words(WORDS_ADVERBS_PATH, advs)

    # Build unified words_10_all.txt as union of all POS lists
    all_words = nouns | verbs | adjs | advs
    _write_words(WORDS_ALL_PATH, all_words)

    print(
        f"Wrote {len(nouns)} nouns, {len(verbs)} verbs, {len(adjs)} adjectives, {len(advs)} adverbs, "
        f"{len(all_words)} unique words (≤{MAX_LENGTH} chars) to wordlists/."
    )


if __name__ == "__main__":
    main()
