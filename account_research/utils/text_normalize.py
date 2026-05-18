"""Unicode-aware text normalization shared by Fact-Checker and Author semantic
validator.

Why this exists: substring matching against page HTML fails for legitimate
quotes when Unicode forms differ (NFC vs NFD diacritics, smart quotes, em/en
dashes, ellipsis character vs three dots). Plain `.lower()` plus whitespace
collapse — what the v1 normalizer did — left ~88% of evidence unverifiable.
"""
from __future__ import annotations

import html
import re
import unicodedata

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Smart punctuation → ASCII. Build once at import.
_PUNCT_MAP = {
    # Quotes
    "‘": "'",  # left single
    "’": "'",  # right single
    "‚": "'",  # single low-9
    "‛": "'",  # single high-reversed-9
    "“": '"',  # left double
    "”": '"',  # right double
    "„": '"',  # double low-9
    "‟": '"',  # double high-reversed-9
    "«": '"',  # «
    "»": '"',  # »
    # Dashes
    "‐": "-",  # hyphen
    "‑": "-",  # non-breaking hyphen
    "‒": "-",  # figure dash
    "–": "-",  # en dash
    "—": "-",  # em dash
    "―": "-",  # horizontal bar
    "−": "-",  # minus
    # Ellipsis
    "…": "...",
    # Whitespace variants that NFKD doesn't fold
    " ": " ",  # non-breaking space
    " ": " ",  # figure space
    " ": " ",  # narrow no-break space
}

_PUNCT_TRANS = str.maketrans(_PUNCT_MAP)


def strip_html(raw: str) -> str:
    """Convert HTML to a flat text blob suitable for substring search."""
    no_tags = _TAG_RE.sub(" ", raw)
    return html.unescape(no_tags)


def normalize_for_match(text: str) -> str:
    """Aggressive normalization for substring/fuzzy comparison.

    Pipeline:
      1. NFKD decompose
      2. drop combining marks (ó → o, é → e)
      3. map smart punctuation to ASCII
      4. collapse all whitespace runs to single space
      5. lowercase
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    mapped = stripped.translate(_PUNCT_TRANS)
    collapsed = _WS_RE.sub(" ", mapped)
    return collapsed.strip().lower()
