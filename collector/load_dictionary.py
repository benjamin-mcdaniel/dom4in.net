import os
import urllib.request

BASE_DIR = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(BASE_DIR, os.pardir))
WORDS_DIR = os.path.join(REPO_ROOT, "wordlists")
WORDS_ALL_PATH = os.path.join(WORDS_DIR, "words_10_all.txt")

# Single stable one-word-per-line English word list
# Source: https://github.com/dwyl/english-words/blob/master/words_alpha.txt
WORDS_URL = "https://raw.githubusercontent.com/dwyl/english-words/master/words_alpha.txt"

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


def main() -> None:
    print("Downloading English word list…")
    text = _download(WORDS_URL)

    words = list(_clean_words(text))
    words.sort()

    os.makedirs(WORDS_DIR, exist_ok=True)
    with open(WORDS_ALL_PATH, "w", encoding="utf-8") as f:
        for w in words:
            f.write(w + "\n")

    print(f"Wrote {len(words)} words (≤{MAX_LENGTH} chars) to {WORDS_ALL_PATH}")


if __name__ == "__main__":
    main()
