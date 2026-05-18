from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class EntityType(str, Enum):
    PERSON = "person"
    COMPANY = "company"


class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1)
    type: EntityType
    primary_url: HttpUrl | None = None
    aliases: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DisambigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    entity_type_hint: Literal["person", "company", "unknown"] = "unknown"
    geography_hint: str | None = None


class RejectedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    url: HttpUrl | None = None
    reason: str


class CandidateOption(BaseModel):
    """A top candidate when Disambiguator returns ambiguous."""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    name: str
    type: EntityType
    primary_url: HttpUrl | None = None
    confidence_score: float = Field(ge=0.0, le=1.0)
    rationale: str


class DisambigResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "ambiguous", "not_found"]
    entity: Entity | None = None
    candidates_top3: list[CandidateOption] = Field(default_factory=list)
    candidates_rejected: list[RejectedCandidate] = Field(default_factory=list)
    notes: str | None = None
