from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from account_research.schemas.evidence import ConfidenceLevel

# Acceptable separators in a value_range: "-", "–", "to" (case-insensitive),
# possibly with whitespace. Catches things like "$1-3M", "$0.5–2M USD", "10 to 20".
_RANGE_RE = re.compile(r"(?:[-–]|\bto\b)", re.IGNORECASE)


class SignalUsed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: UUID
    signal: str
    value: float | int | bool | str | list[str]
    weight: float | None = Field(default=None, ge=0.0, le=1.0)


class Estimate(BaseModel):
    """An estimated metric. Always carries a range, method_id, and caveat (CLAUDE.md rule 3)."""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    entity_id: UUID
    metric: str = Field(min_length=1)
    applies_to: Literal["person", "company"]
    value_range: str = Field(min_length=1)
    unit: str | None = None
    confidence: ConfidenceLevel
    method_id: str = Field(min_length=1)
    signals_used: list[SignalUsed] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    caveat_text: str = Field(min_length=1)
    computed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("value_range")
    @classmethod
    def _must_be_range(cls, v: str) -> str:
        if not _RANGE_RE.search(v):
            raise ValueError(
                f"value_range must express a range (use '-', '–', or 'to'); got {v!r}. "
                "Estimates may never be point values (CLAUDE.md rule 3)."
            )
        return v

    @field_validator("confidence")
    @classmethod
    def _confidence_is_low_or_better(cls, v: ConfidenceLevel) -> ConfidenceLevel:
        if v == ConfidenceLevel.UNKNOWN:
            raise ValueError(
                "An Estimate with confidence=unknown should not be produced; "
                "Estimator must return null instead."
            )
        return v


class InsufficientSignals(BaseModel):
    """Returned in place of an Estimate when minimum_required_signals is unmet."""

    model_config = ConfigDict(extra="forbid")

    metric: str
    method_id: str
    signals_present: int
    signals_required: int
    missing: list[str]
    reason: str = "insufficient_signals"
