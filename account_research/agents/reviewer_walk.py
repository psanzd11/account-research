"""Sprint 3.1 — programmatic walk JSON↔PDF.

For each leaf value in `BriefData` that the Designer renders, assert the
value appears in the extracted PDF text. Missing values become CRITICAL
`designer_dropped_field` issues — surfaced to the Reviewer agent before its
LLM pass so the LLM doesn't need to scan for them.

Why this exists: the LLM Reviewer is asked to verify *every visible claim*,
but in practice it confirmed approved status while the Designer had silently
dropped a stat or chip during rendering (e.g. layout overflow truncated the
last industry chip). Walking field-by-field deterministically catches this
class of regression that the LLM tends to overlook.

The "Insufficient public data" sentinel is treated as an intentional empty
and never raises a dropped-field issue.
"""
from __future__ import annotations

from account_research.schemas.brief import BriefData
from account_research.schemas.review import ReviewIssue
from account_research.utils.text_normalize import normalize_for_match


_INSUFFICIENT_SENTINEL = "insufficient public data"


def _is_sentinel(text: str | None) -> bool:
    if not text:
        return False
    return _INSUFFICIENT_SENTINEL in normalize_for_match(text)


def _value_in(needle: str, haystack: str) -> bool:
    """Substring check after aggressive normalization on both sides."""
    n = normalize_for_match(needle)
    if not n:
        return True  # nothing to check
    return n in haystack


def walk_brief_against_pdf(
    brief: BriefData, pdf_text: str
) -> list[ReviewIssue]:
    """Return one CRITICAL ReviewIssue per leaf field whose rendered value
    is missing from the PDF text. Empty list when every value is present.

    Fields checked:
      - hero.name
      - hero.badge.value + hero.badge.caveat (when badge has a method_id)
      - stats[i].value
      - industries[i].name
      - geographic_footprint[i].location
      - sources[i].title

    Fields whose value is the "Insufficient public data" sentinel are
    treated as honest empties and skipped.
    """
    haystack = normalize_for_match(pdf_text)
    issues: list[ReviewIssue] = []

    def report(location: str, value: str, kind: str) -> None:
        issues.append(ReviewIssue(
            severity="critical",
            location=location,
            claim=value[:200],
            issue=(
                f"designer_dropped_field: the Author emitted this {kind} value "
                f"but it is not present in the rendered PDF text. The Designer "
                f"likely lost it during layout."
            ),
            suggested_fix=(
                "Inspect the Designer output for this field; if the layout "
                "truncated it, tighten upstream caps or drop the field."
            ),
        ))

    # Hero name (the brief's headline)
    if not _value_in(brief.hero.name, haystack):
        report("hero.name", brief.hero.name, "hero name")

    badge = brief.hero.badge
    if badge is not None and badge.value is not None and not _is_sentinel(badge.value):
        if not _value_in(badge.value, haystack):
            report("hero.badge.value", badge.value, "hero badge value")
        # A method_id without a visible caveat is the v0 failure mode this
        # whole project exists to prevent.
        if badge.method_id and not _is_sentinel(badge.caveat):
            if not badge.caveat or not _value_in(badge.caveat, haystack):
                report(
                    "hero.badge.caveat",
                    badge.caveat or "(empty caveat)",
                    "badge caveat",
                )

    for i, stat in enumerate(brief.stats):
        if _is_sentinel(stat.value):
            continue
        if not _value_in(stat.value, haystack):
            report(f"stats[{i}].value", stat.value, "stat")

    for i, chip in enumerate(brief.industries):
        if not _value_in(chip.name, haystack):
            report(f"industries[{i}].name", chip.name, "industry chip")

    for i, geo in enumerate(brief.geographic_footprint):
        if not _value_in(geo.location, haystack):
            report(
                f"geographic_footprint[{i}].location", geo.location, "footprint location"
            )

    for i, src in enumerate(brief.sources):
        # Sources are rendered in a grid; either the title or the url should
        # be visible. Accept either.
        title_ok = _value_in(src.title, haystack)
        url_ok = _value_in(str(src.url), haystack)
        if not (title_ok or url_ok):
            report(
                f"sources[{i}].title",
                f"{src.title} ({src.url})",
                "source reference",
            )

    return issues
