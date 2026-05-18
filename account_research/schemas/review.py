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
    # B7 (Plan B Phase 3): True iff this Reviewer iteration successfully
    # attached PDF page images to the LLM call. False when the rasterizer
    # bailed (typically: poppler is not on PATH on this machine, OR the
    # REVIEWER_VISION_ON_REVISION=0 kill switch is set). Surfaced in
    # pages/2_Run.py so the operator knows when the small-font caveat catch
    # was unavailable for the run.
    vision_used: bool = False

    @property
    def has_critical(self) -> bool:
        return any(i.severity == "critical" for i in self.issues)
