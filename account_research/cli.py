"""CLI entrypoint with subcommands.

Usage:
    python -m account_research.cli run "BW Project Management"
    python -m account_research.cli run "BWPM" --candidate <uuid>
    python -m account_research.cli fact-check <entity_id>
    python -m account_research.cli estimate <entity_id>

For backward compatibility the `run` keyword is optional when the first
non-flag argument isn't a known subcommand:
    python -m account_research.cli "BW Project Management"   # same as `run`

Halt-on-ambiguous: when the Disambiguator returns multiple candidates the CLI
writes them to outputs/candidates/<slug>.json and exits with code 3. Resume
with `--candidate <uuid>`. See MEMORY.md → disambiguator-candidate-flag.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv

from account_research.agents.author import AuthorAgent, AuthorInput
from account_research.agents.base import PipelineContext
from account_research.agents.designer import DesignerAgent, DesignInput
from account_research.agents.disambiguator import DisambiguatorAgent
from account_research.agents.estimator import EstimatorAgent, EstimateInput
from account_research.agents.fact_checker import FactCheckerAgent, FactCheckInput
from account_research.agents.researcher import ResearcherAgent
from account_research.agents.reviewer import ReviewerAgent, ReviewInput
from account_research.orchestrator import run_with_revision
from account_research._progress import emit as progress
from account_research.db import REPO_ROOT, SessionLocal, init_db
from account_research.ledger import (
    PipelineRunRow,
    end_run,
    get_entity,
    get_evidence_for_entity,
    insert_entity,
    insert_estimate,
    insert_evidence_bulk,
    mark_verified,
    start_run,
)
from account_research.llm_client import LLMClient
from account_research.schemas.brief import BriefData
from account_research.schemas.entity import (
    CandidateOption,
    DisambigInput,
    Entity,
    EntityType,
)
from account_research.schemas.evidence import (
    EvidenceItem,
    VerifiedEvidenceItem,
)

SUBCOMMANDS = {"run", "fact-check", "estimate", "author", "design"}
BRIEFS_DIR = REPO_ROOT / "outputs" / "briefs"
PDFS_DIR = REPO_ROOT / "outputs" / "pdfs"

SUBCOMMANDS = {"run", "fact-check", "estimate", "author", "design", "review", "refine"}
CANDIDATES_DIR = REPO_ROOT / "outputs" / "candidates"


# ---------------------------------------------------------------------------
# Candidate file helpers (Disambiguator resume flow)
# ---------------------------------------------------------------------------


def _slug(query: str) -> str:
    h = hashlib.sha1(query.lower().strip().encode("utf-8")).hexdigest()[:12]
    safe = "".join(c if c.isalnum() else "_" for c in query.lower())[:40].strip("_")
    return f"{safe}_{h}"


def _candidates_path(query: str) -> Path:
    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    return CANDIDATES_DIR / f"{_slug(query)}.json"


def _save_candidates(query: str, candidates: list[CandidateOption]) -> Path:
    path = _candidates_path(query)
    path.write_text(
        json.dumps([c.model_dump(mode="json") for c in candidates], indent=2),
        encoding="utf-8",
    )
    return path


def _load_candidate(query: str, candidate_id: UUID) -> CandidateOption | None:
    path = _candidates_path(query)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    for raw in data:
        cand = CandidateOption.model_validate(raw)
        if cand.id == candidate_id:
            return cand
    return None


def _entity_from_candidate(cand: CandidateOption) -> Entity:
    return Entity(id=cand.id, name=cand.name, type=cand.type, primary_url=cand.primary_url)


# ---------------------------------------------------------------------------
# Brief + PDF persistence helpers
# ---------------------------------------------------------------------------


def _save_brief(entity_id: UUID, brief: BriefData) -> Path:
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    path = BRIEFS_DIR / f"{entity_id}.json"
    path.write_text(brief.model_dump_json(indent=2), encoding="utf-8")
    return path


def _load_brief(entity_id: UUID) -> BriefData | None:
    path = BRIEFS_DIR / f"{entity_id}.json"
    if not path.exists():
        return None
    return BriefData.model_validate_json(path.read_text(encoding="utf-8"))


def _pdf_slug(entity: Entity) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in entity.name)[:60].strip("_")
    return f"{safe}_{entity.id}"


# ---------------------------------------------------------------------------
# Common bootstrap
# ---------------------------------------------------------------------------


def _bootstrap(verbose: int) -> tuple[logging.Logger, LLMClient | None]:
    load_dotenv(REPO_ROOT / ".env")
    logging.basicConfig(
        level=logging.WARNING - 10 * min(verbose, 2),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("cli")
    init_db()
    llm = LLMClient() if os.environ.get("ANTHROPIC_API_KEY") else None
    return log, llm


def _require_api_key() -> int | None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "error: ANTHROPIC_API_KEY not set. Copy .env.example to .env and fill it in,\n"
            "or export ANTHROPIC_API_KEY in your shell before running.",
            file=sys.stderr,
        )
        return 1
    return None


# ---------------------------------------------------------------------------
# Subcommand: run
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    log, llm = _bootstrap(args.verbose)
    rc = _require_api_key()
    if rc is not None:
        return rc
    assert llm is not None

    with SessionLocal() as session:
        run_id = start_run(session, query=args.query)
        ctx = PipelineContext(run_id=run_id, session=session, llm_client=llm, logger=log)

        # ------------------------------------------------------------------
        # Phase 1 — Disambiguator (or skip via --candidate)
        # ------------------------------------------------------------------
        if args.candidate is not None:
            cand = _load_candidate(args.query, args.candidate)
            if cand is None:
                print(
                    f"error: --candidate {args.candidate} not found in cached "
                    f"candidates for query {args.query!r}. Run without --candidate first.",
                    file=sys.stderr,
                )
                end_run(session, run_id, status="error_candidate_not_found")
                session.commit()
                return 2
            entity = _entity_from_candidate(cand)
            log.info("Resumed with candidate %s (%s)", entity.name, entity.id)
            progress("disambiguator", "done", info="from_candidate")
        else:
            progress("disambiguator", "start")
            try:
                disambig = DisambiguatorAgent().run(
                    DisambigInput(
                        query=args.query,
                        entity_type_hint=args.entity_type,
                        geography_hint=args.geography_hint,
                    ),
                    ctx,
                )
            except Exception:
                progress("disambiguator", "failed")
                raise
            if disambig.status == "ambiguous":
                path = _save_candidates(args.query, disambig.candidates_top3)
                print("\nDisambiguator returned status=ambiguous. Top candidates:\n")
                for c in disambig.candidates_top3:
                    url = c.primary_url or "(no URL)"
                    print(f"  {c.id}  {c.name}  [{c.type.value}]  score={c.confidence_score:.2f}")
                    print(f"      {url}")
                    print(f"      {c.rationale}")
                print(f"\nCandidates saved to: {path}")
                print(f"Re-run with: python -m account_research.cli run {args.query!r} --candidate <uuid>")
                progress("disambiguator", "halted", info="ambiguous")
                end_run(session, run_id, status="halted_ambiguous")
                session.commit()
                return 3

            if disambig.status == "not_found":
                print(
                    f"Disambiguator returned status=not_found for {args.query!r}. "
                    f"Notes: {disambig.notes or '(none)'}",
                    file=sys.stderr,
                )
                progress("disambiguator", "halted", info="not_found")
                end_run(session, run_id, status="not_found")
                session.commit()
                return 4

            if disambig.entity is None:
                print("internal error: status=ok but no entity returned", file=sys.stderr)
                progress("disambiguator", "failed", info="no_entity")
                end_run(session, run_id, status="internal_error")
                session.commit()
                return 5

            entity = disambig.entity
            progress("disambiguator", "done")

        persisted_entity = insert_entity(session, entity)
        # Back-fill entity_id on the pipeline_run row now that we have it
        # (start_run() was called before disambiguation). Library uses this
        # to filter runs per entity.
        pr_row = session.get(PipelineRunRow, run_id)
        if pr_row is not None:
            pr_row.entity_id = persisted_entity.id
        session.commit()
        log.info("Resolved entity: %s (%s)", persisted_entity.name, persisted_entity.id)

        # ------------------------------------------------------------------
        # Outreach contacts for the page-1 panel (best-effort side-channel,
        # does NOT touch the evidence ledger).
        #
        # Two sources, tried in order:
        #   1. Apollo CRM via API key — gives name/title/email/phone when
        #      configured. Free tier returns name+title but emails come back
        #      as placeholders (filtered out in the connector).
        #   2. Web-search fallback — runs only when Apollo returns < 2 useful
        #      contacts. Costs ~$0.05-0.20 (1 Sonnet call + web tools).
        #      Returns name+title+linkedin_url (no email/phone — PII isn't
        #      verbatim on public pages).
        # ------------------------------------------------------------------
        from account_research.schemas.brief import ContactItem
        contacts: list[ContactItem] = []
        if persisted_entity.type == EntityType.COMPANY:
            try:
                from account_research.tools.connectors.apollo import ApolloConnector
                contacts = ApolloConnector(logger=log).search_contacts(
                    persisted_entity, top_n=3,
                )
                if contacts:
                    log.info(
                        "Apollo: %d contact(s) fetched for page-1 panel", len(contacts)
                    )
            except Exception as exc:  # noqa: BLE001 — non-fatal side-channel
                log.warning("Apollo contacts lookup failed: %s — continuing", exc)
                contacts = []

            # Web-search fallback when Apollo whiffed or returned too few.
            if len(contacts) < 2:
                try:
                    from account_research.tools.contact_finder import (
                        find_contacts_via_web, merge_contacts,
                    )
                    web_contacts = find_contacts_via_web(
                        persisted_entity, llm,
                        max_contacts=3, logger=log,
                    )
                    if web_contacts:
                        contacts = merge_contacts(
                            contacts, web_contacts, max_contacts=3,
                        )
                        log.info(
                            "contact_finder: total contacts after web fallback: %d",
                            len(contacts),
                        )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "contact_finder fallback failed: %s — continuing", exc,
                    )

        # ------------------------------------------------------------------
        # Phase 2 — Researcher
        # ------------------------------------------------------------------
        progress("researcher", "start")
        try:
            batch = ResearcherAgent().run(persisted_entity, ctx)
        except Exception:
            progress("researcher", "failed")
            raise
        inserted = insert_evidence_bulk(session, batch.items)
        session.commit()
        progress("researcher", "done")

        # ------------------------------------------------------------------
        # Phase 3 — Fact-Checker
        # ------------------------------------------------------------------
        corroboration_map: dict = {}
        if args.skip_fact_check:
            progress("fact_checker", "skipped")
            fc_report = None
            verified_items: list[VerifiedEvidenceItem] = []
        else:
            progress("fact_checker", "start")
            try:
                fc = FactCheckerAgent().run(FactCheckInput(items=list(batch.items)), ctx)
            except Exception:
                progress("fact_checker", "failed")
                raise
            for vi in fc.items:
                mark_verified(session, vi.id, vi.verification)
            session.commit()
            fc_report = fc.report
            verified_items = fc.items

            # Sprint 1.4 (revised) — never halt the pipeline for low
            # verification. Instead, when the Fact-Checker flags the batch
            # as below the 70% threshold, prune items that fail
            # is_acceptable_for_author() (i.e. neither verified NOR a
            # Tier-1/HIGH unverifiable fallback). The remaining set is the
            # one Author would have used anyway; we just surface the prune
            # in logs so the operator knows fewer claims will be available.
            if fc.flagged_low_verification:
                before = len(verified_items)
                verified_items = [
                    ev for ev in verified_items
                    if ev.is_acceptable_for_author()
                ]
                pruned = before - len(verified_items)
                log.warning(
                    "Fact-Checker flagged verification rate %.0f%% (%d/%d). "
                    "Pruned %d unacceptable item(s); proceeding with %d "
                    "Author-acceptable item(s).",
                    fc_report.verification_rate * 100,
                    fc_report.verified, fc_report.total,
                    pruned, len(verified_items),
                )

            # G1: cap aggregator items at ≤25% of the verified set.
            from account_research.agents.source_authority import (
                aggregator_share,
                cap_aggregators_if_exceeded,
                tier_breakdown,
            )
            agg_before = aggregator_share(verified_items)
            verified_items, dropped_agg = cap_aggregators_if_exceeded(verified_items)
            if dropped_agg:
                log.warning(
                    "Source-authority gate dropped %d aggregator item(s) "
                    "(aggregator share was %.0f%%; capped at 25%%)",
                    dropped_agg, agg_before * 100,
                )
            tiers = tier_breakdown(verified_items)
            log.info(
                "Tier breakdown after gating: T1=%d T2=%d T3=%d (total verified=%d)",
                tiers.get(1, 0), tiers.get(2, 0), tiers.get(3, 0),
                sum(tiers.values()),
            )

            # G2: multi-source corroboration. Items sharing claim-similarity
            # across DIFFERENT hosts (and same category) form groups. Recipes
            # weight corroborated signals 1.5× toward signals_present.
            from account_research.agents.corroboration import (
                compute_corroboration,
                corroboration_stats,
            )
            corroboration_map = compute_corroboration(verified_items)
            cstats = corroboration_stats(verified_items)
            log.info(
                "Corroboration: %d/%d items multi-source (share=%.0f%%, max_group=%d)",
                cstats.get("corroborated", 0),
                cstats.get("total", 0),
                float(cstats.get("corroborated_share", 0)) * 100,
                cstats.get("max_group_size", 1),
            )
            progress("fact_checker", "done")

        # ------------------------------------------------------------------
        # Phase 4 — Estimator
        # ------------------------------------------------------------------
        estimate_summary = []
        estimates_list = []
        if verified_items and not args.skip_estimator:
            progress("estimator", "start")
            try:
                est = EstimatorAgent().run(
                    EstimateInput(
                        entity=persisted_entity,
                        ledger=verified_items,
                        corroboration_map=corroboration_map,
                    ),
                    ctx,
                )
            except Exception:
                progress("estimator", "failed")
                raise
            for e in est.estimates:
                insert_estimate(session, e)
                estimate_summary.append((e.method_id, e.value_range, e.confidence.value))
            session.commit()
            estimates_list = list(est.estimates)
            insufficient_summary = [(i.method_id, i.signals_present, i.signals_required)
                                    for i in est.insufficient]
            if not est.estimates and est.insufficient:
                progress("estimator", "done", info="insufficient_signals")
            else:
                progress("estimator", "done")
        else:
            progress("estimator", "skipped")
            insufficient_summary = []

        # ------------------------------------------------------------------
        # Phase 5 — Author ↔ Designer ↔ Reviewer revision loop
        # ------------------------------------------------------------------
        brief_path = None
        pdf_path = None
        design_warnings: list[str] = []
        review_status = None
        iterations_used = 0
        author_failed = False
        if verified_items and not args.skip_author:
            target_pdf = PDFS_DIR / f"{_pdf_slug(persisted_entity)}.pdf"
            if args.skip_reviewer:
                # Single-shot Author + Designer, no revision loop.
                # Author can fail without halting the pipeline — falls back to
                # a skeleton BriefData ("Insufficient public data") so the
                # Designer still produces a PDF (memory: always-produce-pdf).
                progress("author", "start", iter=(1, 1))
                try:
                    brief = AuthorAgent().run(
                        AuthorInput(entity=persisted_entity,
                                    ledger=verified_items, estimates=estimates_list),
                        ctx,
                    )
                    progress("author", "done", iter=(1, 1))
                except Exception as exc:  # noqa: BLE001
                    progress("author", "failed", iter=(1, 1))
                    author_failed = True
                    log.warning(
                        "Author failed (%s) — falling back to skeleton brief", exc,
                    )
                    brief = BriefData.skeleton(
                        persisted_entity, verified_items, estimates_list,
                    )
                if contacts:
                    brief = brief.model_copy(update={"contacts": contacts})
                brief_path = _save_brief(persisted_entity.id, brief)
                if not args.skip_designer:
                    progress("designer", "start", iter=(1, 1))
                    try:
                        report = DesignerAgent().run(
                            DesignInput(brief=brief, out_path=str(target_pdf)), ctx
                        )
                    except Exception:
                        progress("designer", "failed", iter=(1, 1))
                        raise
                    progress("designer", "done", iter=(1, 1))
                    design_warnings = report.warnings
                    pdf_path = target_pdf
                else:
                    progress("designer", "skipped")
                progress("reviewer", "skipped")
                iterations_used = 1
            else:
                result = run_with_revision(
                    entity=persisted_entity,
                    ledger=verified_items,
                    estimates=estimates_list,
                    ctx=ctx,
                    pdf_path=target_pdf,
                    max_iterations=args.max_iterations,
                    use_vision=args.vision,
                    contacts=contacts,
                )
                brief_path = _save_brief(persisted_entity.id, result.brief)
                pdf_path = result.pdf_path
                review_status = result.review.status
                iterations_used = result.iterations_used
                if result.author_failed:
                    author_failed = True
        else:
            # Author phase skipped (no verified items, or --skip-author):
            # surface all three downstream agents as skipped so the UI is honest.
            progress("author", "skipped")
            progress("designer", "skipped")
            progress("reviewer", "skipped")

        final_status = "completed_author_fallback" if author_failed else "completed"
        end_run(
            session, run_id, status=final_status, iterations=iterations_used,
            final_pdf_path=str(pdf_path) if pdf_path else None,
        )
        session.commit()

        # ------------------------------------------------------------------
        # Print summary
        # ------------------------------------------------------------------
        print(f"\nEntity:    {persisted_entity.name} ({persisted_entity.type.value})")
        print(f"URL:       {persisted_entity.primary_url or '(none)'}")
        print(f"Run ID:    {run_id}")
        print(f"Evidence:  {inserted} items inserted into ledger")
        if batch.low_evidence_flag:
            print("WARNING:   low_evidence_flag is set (< 8 items)")
        if fc_report is not None:
            print(f"Verified:  {fc_report.verified}/{fc_report.total} "
                  f"({fc_report.verification_rate:.0%}), "
                  f"unverifiable={fc_report.unverifiable}, dead={fc_report.source_dead}")
        for mid, vr, conf in estimate_summary:
            print(f"Estimate:  {mid:<36s} {vr:<18s} confidence={conf}")
        for mid, present, required in insufficient_summary:
            print(f"Estimate:  {mid:<36s} insufficient signals ({present}/{required})")
        if brief_path:
            print(f"Brief:     {brief_path}")
        if pdf_path:
            print(f"PDF:       {pdf_path}")
        if review_status:
            print(f"Review:    {review_status} (iterations={iterations_used})")
        for w in design_warnings:
            print(f"WARNING:   {w}")

    return 0


# ---------------------------------------------------------------------------
# Subcommand: fact-check
# ---------------------------------------------------------------------------


def cmd_fact_check(args: argparse.Namespace) -> int:
    log, llm = _bootstrap(args.verbose)
    # Haiku fallback needs an API key but Fact-Check is otherwise LLM-free.
    # If the user passed --no-js-fallback or has no key, skip the rescue path.
    if args.no_js_fallback or not os.environ.get("ANTHROPIC_API_KEY"):
        llm = None

    with SessionLocal() as session:
        entity = get_entity(session, args.entity_id)
        if entity is None:
            print(f"error: entity {args.entity_id} not found in ledger.", file=sys.stderr)
            return 2

        items = get_evidence_for_entity(session, entity.id)
        plain_items: list[EvidenceItem] = [
            ev if not isinstance(ev, VerifiedEvidenceItem)
            else EvidenceItem(**ev.model_dump(exclude={"verification"}))
            for ev in items
        ]
        ctx = PipelineContext(session=session, llm_client=llm, logger=log)
        result = FactCheckerAgent().run(
            FactCheckInput(
                items=plain_items, use_cache=not args.no_cache,
                use_js_fallback=not args.no_js_fallback,
            ),
            ctx,
        )
        for vi in result.items:
            mark_verified(session, vi.id, vi.verification)
        session.commit()

        r = result.report
        print(f"\nEntity:        {entity.name} ({entity.id})")
        print(f"Total items:   {r.total}")
        print(f"Verified:      {r.verified} ({r.verification_rate:.0%})")
        print(f"Unverifiable:  {r.unverifiable}")
        print(f"Source dead:   {r.source_dead}")
        if result.js_fallback_recoveries:
            print(f"JS-rescued:    {result.js_fallback_recoveries} (via Haiku web_fetch)")
        if result.flagged_low_verification:
            print("WARNING:       verification rate below 70% threshold")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: estimate
# ---------------------------------------------------------------------------


def cmd_author(args: argparse.Namespace) -> int:
    log, llm = _bootstrap(args.verbose)
    rc = _require_api_key()
    if rc is not None:
        return rc
    assert llm is not None

    with SessionLocal() as session:
        entity = get_entity(session, args.entity_id)
        if entity is None:
            print(f"error: entity {args.entity_id} not found.", file=sys.stderr)
            return 2
        items = get_evidence_for_entity(session, entity.id)
        verified = [i for i in items if isinstance(i, VerifiedEvidenceItem)]
        if not verified:
            print(
                f"error: no verified evidence for {entity.name}. "
                f"Run `fact-check {entity.id}` first.",
                file=sys.stderr,
            )
            return 3
        from account_research.ledger import get_estimates_for_entity
        estimates = get_estimates_for_entity(session, entity.id)

        ctx = PipelineContext(session=session, llm_client=llm, logger=log)
        brief = AuthorAgent().run(
            AuthorInput(entity=entity, ledger=verified, estimates=estimates), ctx
        )
        path = _save_brief(entity.id, brief)
        print(f"\nEntity:    {entity.name}")
        print(f"Brief:     {path}")
        cited = brief.all_evidence_ids()
        print(f"Citations: {len(cited)} unique evidence_ids across the brief")
    return 0


def cmd_design(args: argparse.Namespace) -> int:
    log, _ = _bootstrap(args.verbose)
    with SessionLocal() as session:
        entity = get_entity(session, args.entity_id)
        if entity is None:
            print(f"error: entity {args.entity_id} not found.", file=sys.stderr)
            return 2
        brief = _load_brief(entity.id)
        if brief is None:
            print(
                f"error: no brief found for {entity.name} at "
                f"{BRIEFS_DIR / (str(entity.id) + '.json')}. Run `author "
                f"{entity.id}` first.",
                file=sys.stderr,
            )
            return 3
        pdf_path = PDFS_DIR / f"{_pdf_slug(entity)}.pdf"
        ctx = PipelineContext(session=session, logger=log)
        report = DesignerAgent().run(
            DesignInput(brief=brief, out_path=str(pdf_path)), ctx
        )
        print(f"\nEntity:   {entity.name}")
        print(f"PDF:      {report.pdf_path}")
        rendered = sum(1 for s in report.sections if s.rendered)
        print(f"Sections: {rendered}/{len(report.sections)} rendered")
        for w in report.warnings:
            print(f"WARNING:  {w}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    log, llm = _bootstrap(args.verbose)
    rc = _require_api_key()
    if rc is not None:
        return rc
    assert llm is not None

    with SessionLocal() as session:
        entity = get_entity(session, args.entity_id)
        if entity is None:
            print(f"error: entity {args.entity_id} not found.", file=sys.stderr)
            return 2
        brief = _load_brief(entity.id)
        if brief is None:
            print(
                f"error: no brief found for {entity.name}. Run `author "
                f"{entity.id}` first.",
                file=sys.stderr,
            )
            return 3

        items = get_evidence_for_entity(session, entity.id)
        verified = [i for i in items if isinstance(i, VerifiedEvidenceItem)]
        from account_research.ledger import get_estimates_for_entity
        estimates = get_estimates_for_entity(session, entity.id)

        pdf_path = PDFS_DIR / f"{_pdf_slug(entity)}.pdf"
        if not pdf_path.exists():
            print(
                f"error: PDF not found at {pdf_path}. Run `design "
                f"{entity.id}` first.",
                file=sys.stderr,
            )
            return 4

        ctx = PipelineContext(session=session, llm_client=llm, logger=log)
        report = ReviewerAgent().run(
            ReviewInput(
                brief=brief, ledger=verified, estimates=estimates,
                pdf_path=str(pdf_path), iteration=1, use_vision=args.vision,
            ),
            ctx,
        )
        print(f"\nEntity:    {entity.name}")
        print(f"PDF:       {pdf_path}")
        print(f"Status:    {report.status}")
        print(f"Issues:    {len(report.issues)}")
        for issue in report.issues:
            print(f"  [{issue.severity:<8s}] {issue.location} — {issue.issue}")
            if issue.suggested_fix:
                print(f"            fix: {issue.suggested_fix}")
    return 0


def cmd_estimate(args: argparse.Namespace) -> int:
    log, _ = _bootstrap(args.verbose)
    with SessionLocal() as session:
        entity = get_entity(session, args.entity_id)
        if entity is None:
            print(f"error: entity {args.entity_id} not found.", file=sys.stderr)
            return 2

        items = get_evidence_for_entity(session, entity.id)
        # Estimator requires VerifiedEvidenceItems; skip un-fact-checked items.
        verified = [i for i in items if isinstance(i, VerifiedEvidenceItem)]
        if not verified:
            print(
                f"error: no verified evidence for {entity.name}. "
                "Run `fact-check {entity_id}` first.",
                file=sys.stderr,
            )
            return 3

        ctx = PipelineContext(session=session, logger=log)
        result = EstimatorAgent().run(
            EstimateInput(entity=entity, ledger=verified), ctx
        )
        for e in result.estimates:
            insert_estimate(session, e)
        session.commit()

        print(f"\nEntity:  {entity.name} ({entity.id})")
        print(f"Verified items used: {len(verified)}")
        for e in result.estimates:
            print(f"  [OK] {e.method_id:<36s} {e.value_range:<18s} confidence={e.confidence.value}")
            print(f"       caveat: {e.caveat_text}")
        for i in result.insufficient:
            print(f"  [--] {i.method_id:<36s} {i.signals_present}/{i.signals_required} signals "
                  f"(missing: {', '.join(i.missing)})")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: refine
# ---------------------------------------------------------------------------


def cmd_refine(args: argparse.Namespace) -> int:
    """Incremental upgrade of an existing brief without re-running the full
    pipeline. Reuses entity + first-pass ledger; adds a supplemental research
    round focused on weaknesses, re-fact-checks, re-runs Author/Designer/Reviewer.
    """
    from account_research.refine import (
        analyze_weaknesses,
        build_focus_prompt,
    )
    from account_research.ledger import get_estimates_for_entity

    log, llm = _bootstrap(args.verbose)
    rc = _require_api_key()
    if rc is not None:
        return rc
    assert llm is not None

    with SessionLocal() as session:
        entity = get_entity(session, args.entity_id)
        if entity is None:
            print(f"error: entity {args.entity_id} not found.", file=sys.stderr)
            return 2

        run_id = start_run(session, query=f"refine:{entity.name}", entity_id=entity.id)
        session.commit()
        ctx = PipelineContext(run_id=run_id, session=session, llm_client=llm, logger=log)

        # Stepper: Disambiguator was done on the original run; reuse and signal it.
        progress("disambiguator", "done", info="from_db")

        all_items = get_evidence_for_entity(session, entity.id)
        verified_items_initial: list[VerifiedEvidenceItem] = [
            i for i in all_items if isinstance(i, VerifiedEvidenceItem)
        ]
        profile = analyze_weaknesses(verified_items_initial)
        log.info("Refine: weakness profile — %s", profile.summary_line())

        if profile.is_thin():
            print(
                f"error: ledger for {entity.name} has zero verified items. "
                f"Run a full pipeline pass instead.",
                file=sys.stderr,
            )
            end_run(session, run_id, status="refine_aborted_empty_ledger")
            session.commit()
            return 6

        # ---------------------------------------------------------------
        # Supplemental research (counts as the Researcher step in the stepper)
        # ---------------------------------------------------------------
        focus_cat = None
        if args.focus:
            from account_research.schemas.evidence import EvidenceCategory
            try:
                focus_cat = EvidenceCategory(args.focus)
            except ValueError:
                print(
                    f"warning: --focus {args.focus!r} is not a valid category; "
                    f"falling back to auto-detected weaknesses.",
                    file=sys.stderr,
                )

        focus_prompt = build_focus_prompt(
            entity, profile,
            existing_evidence_ids=[str(i.id) for i in verified_items_initial],
            focus_category=focus_cat,
        )

        progress("researcher", "start")
        try:
            new_items = ResearcherAgent().run_supplemental(
                entity, user_prompt=focus_prompt, ctx=ctx,
            )
        except Exception:
            progress("researcher", "failed")
            raise
        added = insert_evidence_bulk(session, new_items) if new_items else 0
        session.commit()
        log.info("Refine: supplemental added %d new evidence item(s)", added)
        progress("researcher", "done")

        # ---------------------------------------------------------------
        # Fact-check the new items (and optionally JS-rescue dead ones).
        # ---------------------------------------------------------------
        progress("fact_checker", "start")
        try:
            # Plain copies of just the NEW items for the Fact-Checker.
            new_plain = [
                EvidenceItem(**i.model_dump(exclude={"verification"}))
                if isinstance(i, VerifiedEvidenceItem) else i
                for i in new_items
            ]
            if new_plain:
                fc_new = FactCheckerAgent().run(
                    FactCheckInput(items=new_plain), ctx,
                )
                for vi in fc_new.items:
                    mark_verified(session, vi.id, vi.verification)
                session.commit()
                log.info(
                    "Refine: fact-checked %d new item(s) — verified=%d, "
                    "unverifiable=%d, dead=%d",
                    fc_new.report.total, fc_new.report.verified,
                    fc_new.report.unverifiable, fc_new.report.source_dead,
                )

            # JS-rescue previously unverifiable items — they may have been
            # JS-rendered pages we couldn't reach the first time.
            if not args.no_js_rescue:
                stale_unverif = [
                    EvidenceItem(**i.model_dump(exclude={"verification"}))
                    for i in verified_items_initial
                    if i.verification.status == "unverifiable"
                ][:20]  # cap so we don't burn Haiku budget
                if stale_unverif:
                    log.info(
                        "Refine: retrying %d previously-unverifiable item(s) "
                        "with JS fallback", len(stale_unverif),
                    )
                    fc_retry = FactCheckerAgent().run(
                        FactCheckInput(
                            items=stale_unverif,
                            use_js_fallback=True,
                        ),
                        ctx,
                    )
                    rescued = 0
                    for vi in fc_retry.items:
                        if vi.verification.status == "verified":
                            mark_verified(session, vi.id, vi.verification)
                            rescued += 1
                    session.commit()
                    log.info(
                        "Refine: JS-rescue verified %d previously-unverifiable item(s)",
                        rescued,
                    )
        except Exception:
            progress("fact_checker", "failed")
            raise
        progress("fact_checker", "done")

        # ---------------------------------------------------------------
        # Reload the FULL ledger (initial + new) post-verification.
        # ---------------------------------------------------------------
        all_items_after = get_evidence_for_entity(session, entity.id)
        verified_after = [
            i for i in all_items_after if isinstance(i, VerifiedEvidenceItem)
        ]
        usable_after = [i for i in verified_after if i.is_acceptable_for_author()]
        post_profile = analyze_weaknesses(verified_after)
        log.info("Refine: post-refine profile — %s", post_profile.summary_line())

        # ---------------------------------------------------------------
        # Apollo + web-search contacts (refresh — Apollo plan may have changed).
        # ---------------------------------------------------------------
        from account_research.schemas.brief import ContactItem
        contacts: list[ContactItem] = []
        if entity.type == EntityType.COMPANY:
            try:
                from account_research.tools.connectors.apollo import ApolloConnector
                contacts = ApolloConnector(logger=log).search_contacts(
                    entity, top_n=3,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Refine: Apollo contacts lookup failed: %s", exc)
                contacts = []
            if len(contacts) < 2:
                try:
                    from account_research.tools.contact_finder import (
                        find_contacts_via_web, merge_contacts,
                    )
                    web_contacts = find_contacts_via_web(
                        entity, llm, max_contacts=3, logger=log,
                    )
                    if web_contacts:
                        contacts = merge_contacts(
                            contacts, web_contacts, max_contacts=3,
                        )
                except Exception as exc:  # noqa: BLE001
                    log.warning("Refine: contact_finder failed: %s", exc)

        # ---------------------------------------------------------------
        # Re-run Estimator on the augmented ledger.
        # ---------------------------------------------------------------
        estimates_list = []
        if usable_after and not args.skip_estimator:
            progress("estimator", "start")
            try:
                from account_research.agents.corroboration import compute_corroboration
                corr_map = compute_corroboration(usable_after)
                est = EstimatorAgent().run(
                    EstimateInput(
                        entity=entity, ledger=usable_after,
                        corroboration_map=corr_map,
                    ),
                    ctx,
                )
            except Exception:
                progress("estimator", "failed")
                raise
            for e in est.estimates:
                insert_estimate(session, e)
            session.commit()
            estimates_list = list(est.estimates)
            if not est.estimates and est.insufficient:
                progress("estimator", "done", info="insufficient_signals")
            else:
                progress("estimator", "done")
        else:
            progress("estimator", "skipped")
            # Keep any prior estimates that were persisted earlier.
            estimates_list = list(get_estimates_for_entity(session, entity.id))

        # ---------------------------------------------------------------
        # Re-run Author → Designer → Reviewer.
        # ---------------------------------------------------------------
        target_pdf = PDFS_DIR / f"{_pdf_slug(entity)}.pdf"
        brief_path = None
        pdf_path = None
        iterations_used = 0
        review_status = None

        # Load the previous brief from disk — used as fallback if the new
        # Author crashes. Refine should NEVER make a brief worse than what
        # was already there.
        prior_brief = _load_brief(entity.id)

        if not usable_after:
            progress("author", "skipped")
            progress("designer", "skipped")
            progress("reviewer", "skipped")
        elif args.skip_reviewer:
            progress("author", "start", iter=(1, 1))
            author_failed = False
            try:
                brief = AuthorAgent().run(
                    AuthorInput(entity=entity, ledger=usable_after, estimates=estimates_list),
                    ctx,
                )
                progress("author", "done", iter=(1, 1))
            except Exception as exc:  # noqa: BLE001
                author_failed = True
                progress("author", "failed", iter=(1, 1))
                log.warning(
                    "Refine: Author failed (%s) — falling back to %s",
                    exc,
                    "previous brief" if prior_brief is not None else "skeleton",
                )
                brief = prior_brief if prior_brief is not None else BriefData.skeleton(
                    entity, usable_after, estimates_list,
                )
            if contacts:
                brief = brief.model_copy(update={"contacts": contacts})
            # Skip overwriting the brief JSON if Author failed AND we have a
            # prior — leaving the previous good brief intact on disk.
            if not (author_failed and prior_brief is not None):
                brief_path = _save_brief(entity.id, brief)
            else:
                brief_path = BRIEFS_DIR / f"{entity.id}.json"
            progress("designer", "start", iter=(1, 1))
            try:
                DesignerAgent().run(
                    DesignInput(brief=brief, out_path=str(target_pdf)), ctx,
                )
            except Exception:
                progress("designer", "failed", iter=(1, 1))
                raise
            progress("designer", "done", iter=(1, 1))
            pdf_path = target_pdf
            progress("reviewer", "skipped")
            iterations_used = 1
        else:
            result = run_with_revision(
                entity=entity, ledger=usable_after,
                estimates=estimates_list, ctx=ctx,
                pdf_path=target_pdf,
                max_iterations=args.max_iterations,
                use_vision=args.vision,
                contacts=contacts,
                prior_brief_fallback=prior_brief,
            )
            # If the Author fell back to the prior brief, don't overwrite
            # the persisted brief.json — leave the original intact on disk.
            author_failed = result.review.status == "human_review_needed" and not result.review.issues
            if not (author_failed and prior_brief is not None):
                brief_path = _save_brief(entity.id, result.brief)
            else:
                brief_path = BRIEFS_DIR / f"{entity.id}.json"
            pdf_path = result.pdf_path
            review_status = result.review.status
            iterations_used = result.iterations_used

        # Surface Author fallback into pipeline_run.status so the Library
        # UI can show "fell back to prior brief — Author crashed". Two
        # signals to combine: the single-shot local flag and the
        # OrchestratorResult.author_failed field.
        single_shot_failed = locals().get("author_failed", False)
        revision_failed = getattr(locals().get("result", None), "author_failed", False)
        final_status = (
            "completed_author_fallback"
            if (single_shot_failed or revision_failed)
            else "completed"
        )
        end_run(
            session, run_id, status=final_status, iterations=iterations_used,
            final_pdf_path=str(pdf_path) if pdf_path else None,
        )
        session.commit()

        # ---------------------------------------------------------------
        # Summary
        # ---------------------------------------------------------------
        print(f"\nEntity:           {entity.name} ({entity.type.value})")
        print(f"Run ID:           {run_id} (refine)")
        print(f"Items before:     verified={profile.verified_count}/"
              f"{profile.total_count} T1={profile.tier_counts.get(1, 0)}")
        print(f"Items after:      verified={post_profile.verified_count}/"
              f"{post_profile.total_count} T1={post_profile.tier_counts.get(1, 0)}")
        delta_verified = post_profile.verified_count - profile.verified_count
        delta_t1 = post_profile.tier_counts.get(1, 0) - profile.tier_counts.get(1, 0)
        print(f"Delta:            +{delta_verified} verified, +{delta_t1} Tier-1")
        if brief_path:
            print(f"Brief:            {brief_path}")
        if pdf_path:
            print(f"PDF:              {pdf_path}")
        if review_status:
            print(f"Review:           {review_status} (iterations={iterations_used})")

    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="account_research",
        description="Generate a citation-grounded research brief.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Run the pipeline end-to-end on a query.")
    p_run.add_argument("query")
    p_run.add_argument("--entity-type", choices=["person", "company", "unknown"], default="unknown")
    p_run.add_argument("--geo", dest="geography_hint", default=None)
    p_run.add_argument("--candidate", type=UUID, default=None)
    p_run.add_argument("--skip-fact-check", action="store_true")
    p_run.add_argument("--skip-estimator", action="store_true")
    p_run.add_argument("--skip-author", action="store_true")
    p_run.add_argument("--skip-designer", action="store_true")
    p_run.add_argument("--skip-reviewer", action="store_true",
                       help="Single-shot Author + Designer; no revision loop.")
    p_run.add_argument("--max-iterations", type=int, default=3,
                       help="Max revision iterations (Author ↔ Reviewer).")
    p_run.add_argument("--vision", action="store_true",
                       help="Reviewer also receives PDF page images (needs poppler).")
    p_run.add_argument("--verbose", "-v", action="count", default=0)
    p_run.set_defaults(func=cmd_run)

    p_fc = sub.add_parser("fact-check", help="Re-fetch and verify evidence for an existing entity.")
    p_fc.add_argument("entity_id", type=UUID)
    p_fc.add_argument("--no-cache", action="store_true", help="Bypass the on-disk fetch cache.")
    p_fc.add_argument("--no-js-fallback", action="store_true",
                      help="Skip the Haiku web_fetch retry on unverifiable items.")
    p_fc.add_argument("--verbose", "-v", action="count", default=0)
    p_fc.set_defaults(func=cmd_fact_check)

    p_est = sub.add_parser("estimate", help="Run the Estimator over an entity's verified ledger.")
    p_est.add_argument("entity_id", type=UUID)
    p_est.add_argument("--verbose", "-v", action="count", default=0)
    p_est.set_defaults(func=cmd_estimate)

    p_auth = sub.add_parser("author", help="Run the Author (Opus 4.7) to produce a BriefData JSON.")
    p_auth.add_argument("entity_id", type=UUID)
    p_auth.add_argument("--verbose", "-v", action="count", default=0)
    p_auth.set_defaults(func=cmd_author)

    p_des = sub.add_parser("design", help="Render the cached BriefData to a PDF.")
    p_des.add_argument("entity_id", type=UUID)
    p_des.add_argument("--verbose", "-v", action="count", default=0)
    p_des.set_defaults(func=cmd_design)

    p_rev = sub.add_parser("review", help="Run the Reviewer over a rendered PDF.")
    p_rev.add_argument("entity_id", type=UUID)
    p_rev.add_argument("--vision", action="store_true",
                       help="Send page images alongside text (needs poppler).")
    p_rev.add_argument("--verbose", "-v", action="count", default=0)
    p_rev.set_defaults(func=cmd_review)

    p_refine = sub.add_parser(
        "refine",
        help="Incremental upgrade: supplemental research + re-author on an "
             "existing entity, reusing the first-pass ledger.",
    )
    p_refine.add_argument("entity_id", type=UUID)
    p_refine.add_argument(
        "--focus", default=None,
        help="Optional category to prioritise (e.g. financial, leadership, "
             "geography). Defaults to auto-detected weaknesses.",
    )
    p_refine.add_argument("--skip-estimator", action="store_true")
    p_refine.add_argument(
        "--skip-reviewer", action="store_true",
        help="Single-shot Author+Designer; no revision loop.",
    )
    p_refine.add_argument(
        "--no-js-rescue", action="store_true",
        help="Skip retrying previously-unverifiable items with Haiku JS fetch.",
    )
    p_refine.add_argument("--max-iterations", type=int, default=3)
    p_refine.add_argument(
        "--vision", action="store_true",
        help="Reviewer also receives PDF page images (needs poppler).",
    )
    p_refine.add_argument("--verbose", "-v", action="count", default=0)
    p_refine.set_defaults(func=cmd_refine)

    return p


def main(argv: list[str] | None = None) -> int:
    argv_list = list(sys.argv[1:] if argv is None else argv)
    # Backward compat: if the first non-flag arg isn't a known subcommand,
    # treat it as `run <query>`.
    if argv_list and argv_list[0] not in SUBCOMMANDS and not argv_list[0].startswith("-"):
        argv_list = ["run", *argv_list]

    args = _build_parser().parse_args(argv_list)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
