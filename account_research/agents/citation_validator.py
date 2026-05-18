"""Semantic citation validator (Phase 5 / D2, extended in Round 2 / G8).

The Author validates that every cited evidence_id exists in the ledger (UUID
check). That's necessary but not sufficient: the Author can cite UUID-X while
its prose paraphrases UUID-Y. This module compares the prose of each text-
bearing field against the joined `raw_quote` of its cited evidence and flags
weak overlaps so the Reviewer can arbitrate in the next iteration.

Two methods, gated by env vars:
  - Default: stop-word-stripped Jaccard token overlap + 4-gram shared-shingle
    check. Cheap, deterministic, but misses cross-language paraphrase.
  - When `USE_EMBEDDINGS_FOR_CITATIONS=1` and `VOYAGE_API_KEY` is set, uses
    Voyage AI cosine similarity. Catches ES↔EN paraphrase reliably. ~$0.001
    per brief in cost.

In both cases the validator only FLAGS — it never strips the citation. The
Reviewer is the authoritative arbiter.

Use:
    weak = compute_weak_citations(brief, ledger)
    # weak is a list of dicts with {location, prose, citations, jaccard|cosine}
"""
from __future__ import annotations

import os
from typing import Iterable
from uuid import UUID

from account_research.schemas.brief import BriefData
from account_research.schemas.evidence import VerifiedEvidenceItem
from account_research.utils.text_normalize import normalize_for_match

# G8: opt-in cross-language embeddings. Default OFF (Jaccard fallback).
_USE_EMBEDDINGS = (
    os.environ.get("USE_EMBEDDINGS_FOR_CITATIONS", "0").lower()
    in ("1", "true", "yes", "on")
)
_VOYAGE_MODEL = os.environ.get("VOYAGE_EMBEDDING_MODEL", "voyage-3")
_VOYAGE_COSINE_THRESHOLD = float(
    os.environ.get("VOYAGE_COSINE_THRESHOLD", "0.50")
)

# Stop words intentionally inline — keeping the module dependency-free.
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "of", "in", "on", "at", "to",
    "for", "with", "as", "is", "are", "was", "were", "be", "been", "being",
    "by", "from", "this", "that", "these", "those", "it", "its", "their",
    "his", "her", "our", "your", "we", "they", "you", "he", "she",
    "has", "have", "had", "do", "does", "did", "not", "no",
    # Spanish stopwords for LATAM briefs
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del",
    "y", "o", "pero", "en", "con", "por", "para", "como", "se", "su",
    "sus", "es", "son", "fue", "fueron", "ha", "han", "que", "este",
    "esta", "estos", "estas", "muy", "más", "menos",
})

_MIN_TOKEN_LEN = 3  # Drop trivial tokens like "5", "of"


def _tokens(text: str) -> set[str]:
    """Normalize, split on whitespace, drop stop-words + short tokens."""
    if not text:
        return set()
    normalized = normalize_for_match(text)
    # Keep digits + letters; treat anything else as boundary
    out: set[str] = set()
    buf = []
    for ch in normalized:
        if ch.isalnum():
            buf.append(ch)
        else:
            if buf:
                tok = "".join(buf)
                buf = []
                if len(tok) >= _MIN_TOKEN_LEN and tok not in _STOPWORDS:
                    out.add(tok)
    if buf:
        tok = "".join(buf)
        if len(tok) >= _MIN_TOKEN_LEN and tok not in _STOPWORDS:
            out.add(tok)
    return out


def _shingles(text: str, n: int = 4) -> set[str]:
    """Character-level n-grams of the normalized text. Used as a fallback when
    Jaccard is low but the strings share a literal substring (e.g. a year)."""
    if not text:
        return set()
    norm = normalize_for_match(text).replace(" ", "")
    if len(norm) < n:
        return {norm} if norm else set()
    return {norm[i:i + n] for i in range(len(norm) - n + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def check_one(
    prose: str,
    evidence_ids: Iterable[UUID],
    quotes_by_id: dict[UUID, str],
    *,
    jaccard_threshold: float = 0.10,
) -> dict | None:
    """Return a weak-citation dict if the prose is poorly grounded in the
    cited quotes, else None.

    `prose` is the human-readable text in the BriefData field.
    `quotes_by_id` maps every evidence_id present in the input ledger to its
    raw_quote (full text, untruncated).
    """
    cited_quotes = " ".join(
        quotes_by_id[eid] for eid in evidence_ids if eid in quotes_by_id
    )
    if not cited_quotes:
        # All citations are unknown (already caught by UUID check); skip.
        return None

    p_tok = _tokens(prose)
    q_tok = _tokens(cited_quotes)
    j = jaccard(p_tok, q_tok)
    if j >= jaccard_threshold:
        return None

    # Fallback: 4-gram overlap (catches numbers, names that didn't survive
    # tokenization). If at least one shared shingle, accept.
    p_shg = _shingles(prose, 4)
    q_shg = _shingles(cited_quotes, 4)
    if p_shg & q_shg:
        return None

    return {
        "prose": prose[:300],
        "cited_evidence_ids": [str(e) for e in evidence_ids],
        "jaccard": round(j, 3),
        "missing_token_overlap": True,
    }


def _field_pairs(brief: BriefData) -> list[tuple[str, str, list[UUID]]]:
    """Yield (location_label, prose, evidence_ids) for every text-bearing
    field in BriefData that should be semantically supported by its citations."""
    out: list[tuple[str, str, list[UUID]]] = []

    # quick_take.body + best_angle share the same evidence_ids
    qt = brief.quick_take
    out.append(("quick_take.body", qt.body, list(qt.evidence_ids)))
    if qt.best_angle:
        out.append(("quick_take.best_angle", qt.best_angle, list(qt.evidence_ids)))

    for i, s in enumerate(brief.stats):
        if s.evidence_id is not None:
            out.append((
                f"stats[{i}]",
                f"{s.value} — {s.label}",
                [s.evidence_id],
            ))

    for i, t in enumerate(brief.timeline):
        prose = " ".join(filter(None, [t.label, t.description]))
        out.append((f"timeline[{i}]", prose, list(t.evidence_ids)))

    for i, c in enumerate(brief.who_they_are_cards):
        out.append((f"who_they_are[{i}]", c.body, list(c.evidence_ids)))

    for i, c in enumerate(brief.dna_cards):
        out.append((f"dna[{i}]", c.detail, list(c.evidence_ids)))

    for i, s in enumerate(brief.strategic_signals):
        out.append((f"strategic_signals[{i}]", s.body, list(s.evidence_ids)))

    for i, r in enumerate(brief.scorecard):
        out.append((f"scorecard[{i}]", r.rationale, list(r.evidence_ids)))

    for i, k in enumerate(brief.key_signals):
        out.append((f"key_signals[{i}]", k.body, list(k.evidence_ids)))

    for i, a in enumerate(brief.recommended_approach):
        out.append((f"recommended_approach[{i}]", a.body, list(a.evidence_ids)))

    for i, q in enumerate(brief.discovery_questions):
        out.append((f"discovery_questions[{i}]", q.question, list(q.evidence_ids)))

    for i, n in enumerate(brief.next_steps):
        out.append((f"next_steps[{i}]", n.body, list(n.evidence_ids)))

    # Skip industries (name-only), geographic (location-only), recap_stats
    # (already covered by stats), sources (URL-only), methodology (no citations).

    return out


def compute_weak_citations(
    brief: BriefData,
    ledger: list[VerifiedEvidenceItem],
    *,
    jaccard_threshold: float = 0.10,
) -> list[dict]:
    """Walk every text-bearing field in `brief` and return a list of weak-
    citation flags. Empty list means every cited evidence is semantically
    aligned with its prose.

    Uses Voyage embeddings cosine similarity when USE_EMBEDDINGS_FOR_CITATIONS=1
    AND VOYAGE_API_KEY is set; falls back to Jaccard + 4-gram otherwise.
    """
    quotes_by_id: dict[UUID, str] = {ev.id: ev.raw_quote for ev in ledger}

    if _USE_EMBEDDINGS and os.environ.get("VOYAGE_API_KEY", "").strip():
        try:
            return _compute_weak_citations_embeddings(
                brief, quotes_by_id,
                cosine_threshold=_VOYAGE_COSINE_THRESHOLD,
            )
        except Exception as exc:  # noqa: BLE001
            # Fail open to Jaccard
            import logging
            logging.getLogger("citation_validator").warning(
                "Voyage embeddings unavailable (%s); falling back to Jaccard",
                exc,
            )

    flags: list[dict] = []
    for location, prose, evidence_ids in _field_pairs(brief):
        if not evidence_ids:
            continue
        out = check_one(
            prose, evidence_ids, quotes_by_id,
            jaccard_threshold=jaccard_threshold,
        )
        if out is not None:
            out["location"] = location
            flags.append(out)
    return flags


def _compute_weak_citations_embeddings(
    brief: BriefData,
    quotes_by_id: dict[UUID, str],
    *,
    cosine_threshold: float,
) -> list[dict]:
    """Voyage AI embedding path. Imported lazily so the dep stays optional."""
    import voyageai  # type: ignore[import-not-found]
    client = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])

    # Collect all pairs that need scoring
    pairs = [
        (loc, prose, evidence_ids)
        for loc, prose, evidence_ids in _field_pairs(brief)
        if evidence_ids
    ]
    if not pairs:
        return []

    # Build the embedding inputs: prose + joined quotes per pair
    prose_inputs = [p[1] for p in pairs]
    quote_inputs = [
        " ".join(quotes_by_id[eid] for eid in p[2] if eid in quotes_by_id)
        for p in pairs
    ]
    all_inputs = prose_inputs + quote_inputs
    result = client.embed(all_inputs, model=_VOYAGE_MODEL, input_type="document")
    embeddings = result.embeddings
    n = len(pairs)
    prose_vecs = embeddings[:n]
    quote_vecs = embeddings[n:]

    flags: list[dict] = []
    for i, (loc, prose, evidence_ids) in enumerate(pairs):
        if not quote_inputs[i]:
            continue
        cos = _cosine(prose_vecs[i], quote_vecs[i])
        if cos < cosine_threshold:
            flags.append({
                "location": loc,
                "prose": prose[:300],
                "cited_evidence_ids": [str(e) for e in evidence_ids],
                "cosine": round(cos, 3),
                "missing_semantic_overlap": True,
            })
    return flags


def _cosine(a, b) -> float:
    """Cosine similarity between two iterables of floats."""
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
