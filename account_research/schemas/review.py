from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ReviewIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Literal["critical", "warning", "info"]
    location: str  # e.g. "page 1, hero badge"
    claim: str
    issue: str
    suggested_fix: str | None = None
    evidence_id: UUID | None = None


class ReviewerReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    status: Literal["approved", "revision_required", "human_review_needed"]
    iteration: int = Field(ge=1, le=4)
    issues: list[ReviewIssue] = Field(default_factory=list)
    pdf_path: str
    reviewed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def has_critical(self) -> bool:
        return any(i.severity == "critical" for i in self.issues)
