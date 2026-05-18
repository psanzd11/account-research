"""BaseAgent ABC + PipelineContext.

Every agent inherits from BaseAgent, declares its `name` and default `model`,
and implements `run(payload, ctx)`. Inputs and outputs are Pydantic models —
free-text intermediate outputs are forbidden by CLAUDE.md rule 6.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Generic, Type, TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy.orm import Session

from account_research.llm_client import LLMClient

InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)


class OrphanEvidenceError(RuntimeError):
    """Raised by the Designer when the BriefData cites evidence_ids that are
    not present in the supporting ledger.

    Author already filters orphans before returning (see
    ``_filter_unknown_citations`` in ``agents.author``), so a Designer-level
    orphan means Author's filter has a bug, or a hand-edited brief is being
    rendered directly via the ``design`` subcommand. Either way the
    orchestrator catches this and forces a revision iteration with the
    orphan set surfaced as a critical issue, rather than producing a PDF
    that violates CLAUDE.md rule 1 ("no claim without a citation").
    """

    def __init__(self, orphans: set[UUID]):
        self.orphans: set[UUID] = set(orphans)
        super().__init__(
            f"Brief cites {len(self.orphans)} evidence_id(s) not in the "
            f"ledger: {sorted(str(u) for u in self.orphans)[:5]}"
            + (" ..." if len(self.orphans) > 5 else "")
        )


@dataclass
class PipelineContext:
    """Per-run context handed to every agent in a pipeline run."""

    run_id: UUID = field(default_factory=uuid4)
    session: Session | None = None
    llm_client: LLMClient | None = None
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("pipeline"))
    iteration: int = 1

    def require_llm(self) -> LLMClient:
        if self.llm_client is None:
            raise RuntimeError("PipelineContext has no llm_client; configure one before running this agent")
        return self.llm_client

    def require_session(self) -> Session:
        if self.session is None:
            raise RuntimeError("PipelineContext has no session; configure one before running this agent")
        return self.session


class BaseAgent(ABC, Generic[InputT, OutputT]):
    """Abstract base. Concrete agents declare name + model and implement run()."""

    name: str = "unnamed"
    model: str = ""  # SONNET or OPUS — set on the subclass
    input_schema: Type[InputT]
    output_schema: Type[OutputT]

    @abstractmethod
    def run(self, payload: InputT, ctx: PipelineContext) -> OutputT:
        """Execute the agent. Must return an instance of output_schema."""
        ...
