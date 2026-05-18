"""One-shot audit: list every entity and report whether its confidence score
is currently computable (brief exists + validates) or not."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from account_research.db import SessionLocal
from account_research.ledger import (
    EntityRow,
    EvidenceRow,
    get_evidence_for_entity,
)
from account_research.quality import confidence_score, ledger_only_score
from account_research.schemas.brief import BriefData
from account_research.schemas.evidence import VerifiedEvidenceItem

BRIEFS = REPO / "outputs" / "briefs"


def main() -> int:
    with SessionLocal() as s:
        entities = (
            s.query(EntityRow).order_by(EntityRow.created_at.desc()).all()
        )
        for e in entities:
            brief_path = BRIEFS / f"{e.id}.json"
            ev_rows = (
                s.query(EvidenceRow)
                .filter(EvidenceRow.entity_id == e.id)
                .all()
            )
            items = len(ev_rows)
            score: float | None = None
            kind = "—"
            if brief_path.exists():
                try:
                    brief = BriefData.model_validate_json(
                        brief_path.read_text(encoding="utf-8")
                    )
                    ledger = [
                        ev for ev in get_evidence_for_entity(s, e.id)
                        if isinstance(ev, VerifiedEvidenceItem)
                    ]
                    score = confidence_score(brief, ledger)
                    kind = "FULL"
                except Exception:
                    pass
            if score is None:
                score = ledger_only_score(ev_rows)
                if score is not None:
                    kind = "FALLBACK*"
            score_str = f"{score:.0f}" if score is not None else "—"
            print(
                f"{str(e.id)[:8]}  items={items:3d}  "
                f"brief={'Y' if brief_path.exists() else 'N'}  "
                f"score={score_str:>4s} ({kind:9s})  name={e.name[:50]}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
