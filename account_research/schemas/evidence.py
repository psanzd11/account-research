from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class ConfidenceLevel(str, Enum):
    VERIFIED = "verified"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class EvidenceCategory(str, Enum):
    COMPANY_FACTS = "company_facts"
    FINANCIAL = "financial"
    LEADERSHIP = "leadership"
    PRODUCTS = "products"
    CLIENTS = "clients"
    GEOGRAPHY = "geography"
    RECOGNITION = "recognition"
    TEAM = "team"
    OTHER = "other"


class SourceType(str, Enum):
    OFFICIAL_SITE = "official_site"
    SEC_FILING = "sec_filing"
    GOV_REGISTRY = "gov_registry"
    PRESS = "press"
    LINKEDIN = "linkedin"
    NEWS = "news"
    SOCIAL = "social"
    AGGREGATOR = "aggregator"
    OTHER = "other"


class Verification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # A5 (2026-05-18): "rejected" status added for items that fail BOTH the
    # anchored fuzzy match AND a full-page LCS sanity check. Rejected items
    # never enter the Author-acceptable ledger — they are pruned before
    # is_acceptable_for_author() even considers Tier-1 fallback.
    status: Literal["verified", "unverifiable", "contradicted", "source_dead", "rejected"]
    method: Literal["exact_match", "semantic_match", "url_404", "content_changed", "fuzzy_match"]
    checked_at: datetime
    similarity: float | None = Field(default=None, ge=0.0, le=1.0)
    # A5: LCS-style ratio computed against the full normalized page text.
    # Separate field from ``similarity`` (which is the anchored window ratio
    # used by the fuzzy_match path). Populated by the Fact-Checker when
    # available; older rows leave this None.
    claim_similarity_score: float | None = Field(default=None, ge=0.0, le=1.0)


class EvidenceItem(BaseModel):
    """Single fact in the ledger. raw_quote is mandatory (CLAUDE.md rule 5)."""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    entity_id: UUID
    claim: str = Field(min_length=3)
    category: EvidenceCategory
    source_url: HttpUrl
    source_type: SourceType
    raw_quote: str = Field(min_length=3)
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    confidence: ConfidenceLevel
    notes: str | None = None

    @field_validator("raw_quote")
    @classmethod
    def _quote_not_placeholder(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("raw_quote must not be empty")
        if stripped.lower() in {"n/a", "na", "tbd", "unknown", "..."}:
            raise ValueError(f"raw_quote may not be a placeholder ({stripped!r})")
        return v


class VerifiedEvidenceItem(EvidenceItem):
    verification: Verification

    def is_acceptable_for_author(self) -> bool:
        """Whether Author may cite this item.

        Accepts:
        - any item Fact-Checker verified, OR
        - Tier-1 sources (official site / SEC / gov registry) carrying
          confidence=HIGH whose re-fetch came back `unverifiable`
          (e.g. JS-rendered pages, soft 403s, content_changed). These keep
          their original raw_quote which the Researcher captured at fetch
          time and which the source's nature makes load-bearing on its own.

        Rejects `source_dead` and `contradicted` regardless of source/confidence.
        """
        if self.verification.status == "verified":
            return True
        # A5: rejected items never reach the Author — fail-loud at the
        # Author boundary so the brief reflects honest evidence only.
        if self.verification.status != "unverifiable":
            return False
        tier1 = {
            SourceType.OFFICIAL_SITE,
            SourceType.SEC_FILING,
            SourceType.GOV_REGISTRY,
        }
        return (
            self.source_type in tier1
            and self.confidence == ConfidenceLevel.HIGH
        )


class LedgerReport(BaseModel):
    """Summary statistics produced by the Fact-Checker."""

    model_config = ConfigDict(extra="forbid")

    total: int = Field(ge=0)
    verified: int = Field(ge=0)
    unverifiable: int = Field(ge=0)
    source_dead: int = Field(ge=0)
    estimated: int = Field(ge=0, default=0)

    @property
    def verification_rate(self) -> float:
        return self.verified / self.total if self.total else 0.0


class EvidenceBatch(BaseModel):
    """Researcher's output: a list of evidence items for one entity.

    Wrapped because `LLMClient.complete_with_json` emits exactly one record per
    tool call; the Researcher needs to return many items in a single turn.
    """

    model_config = ConfigDict(extra="forbid")

    entity_id: UUID
    items: list[EvidenceItem] = Field(default_factory=list)
    notes: str | None = None
    low_evidence_flag: bool = False  # set when items count < threshold (SPEC §4.2)
