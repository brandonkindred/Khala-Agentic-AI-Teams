"""
Brand-aligned blog writing pipeline with artifact persistence and gates.

Runs research -> review -> draft -> copy-editor loop. When work_dir is provided,
persists artifacts and runs validators, fact-check, and compliance. On FAIL,
enters closed-loop rewrite until PASS or max_rewrite_iterations.

Supports job_updater callback for UI phase tracking.
"""

from . import _path_setup  # noqa: F401

import logging
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Tuple, Union

from blog_compliance_agent import BlogComplianceAgent
from blog_copy_editor_agent import BlogCopyEditorAgent, CopyEditorInput
from blog_copy_editor_agent.models import FeedbackItem
from blog_draft_agent import BlogDraftAgent, DraftInput, ReviseDraftInput
from blog_fact_check_agent import BlogFactCheckAgent
from blog_research_agent.agent import ResearchAgent
from blog_research_agent.agent_cache import AgentCache
from blog_research_agent.allowed_claims import extract_allowed_claims
from llm_service import get_client, OllamaLLMClient
from blog_research_agent.models import ResearchBriefInput
from blog_publication_agent.models import PublishingPack
from blog_review_agent import BlogReviewAgent, BlogReviewInput
from shared.artifacts import read_artifact, write_artifact
from shared.brand_spec import load_brand_spec
from shared.errors import (
    BloggingError,
    ComplianceError,
    DraftError,
    FactCheckError,
    ResearchError,
    ReviewError,
)
from shared.models import BlogPhase, get_phase_progress
from validators.runner import run_validators_from_work_dir

logger = logging.getLogger(__name__)

STYLE_GUIDE_PATH = Path(__file__).resolve().parent.parent / "docs" / "brandon_kindred_brand_and_writing_style_guide.md"
DRAFT_EDITOR_ITERATIONS = 100
MAX_REWRITE_ITERATIONS = 100

# Default model - use environment variable or this default
DEFAULT_MODEL = "qwen3.5:397b-cloud"

PipelineStatus = Literal["PASS", "FAIL", "NEEDS_HUMAN_REVIEW"]

# Type alias for job updater callback
JobUpdater = Callable[..., None]


def run_pipeline(
    brief: ResearchBriefInput,
    *,
    work_dir: Optional[Union[str, Path]] = None,
    llm_client: Optional[OllamaLLMClient] = None,
    draft_editor_iterations: int = DRAFT_EDITOR_ITERATIONS,
    max_rewrite_iterations: int = MAX_REWRITE_ITERATIONS,
    run_gates: bool = True,
    job_updater: Optional[JobUpdater] = None,
):
    """
    Run the full blog writing pipeline: research -> review -> draft -> copy-editor loop.

    When work_dir is provided, persists artifacts. When run_gates is True (default when
    work_dir is set), runs validators, fact-check, and compliance. On FAIL, enters
    closed-loop rewrite until PASS or max_rewrite_iterations.

    Args:
        brief: The research brief input describing the blog topic.
        work_dir: Optional directory for artifact persistence.
        llm_client: Optional LLM client (defaults to qwen3.5:397b-cloud).
        draft_editor_iterations: Number of draft/copy-edit iterations.
        max_rewrite_iterations: Max compliance rewrite attempts.
        run_gates: Whether to run validators/compliance gates.
        job_updater: Optional callback for UI phase tracking updates.
            Called with (phase, progress, status_text, **kwargs).

    Returns:
        Tuple of (research_result, review_result, draft_result, status).
        status is PASS, FAIL, or NEEDS_HUMAN_REVIEW.
        
    Raises:
        ResearchError: If research phase fails.
        ReviewError: If title/outline generation fails.
        DraftError: If draft generation fails.
        ComplianceError: If compliance check fails unrecoverably.
        FactCheckError: If fact check fails unrecoverably.
    """
    
    def _update(
        phase: BlogPhase,
        sub_progress: float = 0.0,
        status_text: str = "",
        **kwargs: Any,
    ) -> None:
        """Update job status if job_updater is provided."""
        if job_updater:
            try:
                progress = get_phase_progress(phase, sub_progress)
                job_updater(
                    phase=phase.value,
                    progress=progress,
                    status_text=status_text,
                    **kwargs,
                )
            except Exception as e:
                logger.warning("Failed to update job status: %s", e)
    
    if llm_client is None:
        llm_client = get_client("blog")

    if work_dir is not None:
        work_path = Path(work_dir).resolve()
        work_path.mkdir(parents=True, exist_ok=True)
        logger.info("Artifact work_dir: %s", work_path)

    # 1. Research
    _update(
        BlogPhase.RESEARCH,
        sub_progress=0.0,
        status_text="Starting web and arXiv research...",
    )
    
    try:
        cache = AgentCache(cache_dir=".agent_cache")
        research_agent = ResearchAgent(llm_client=llm_client, cache=cache)
        research_result = research_agent.run(brief)
    except BloggingError:
        raise
    except Exception as e:
        raise ResearchError(f"Research failed: {e}", cause=e) from e
    
    logger.info("Research complete: %s references", len(research_result.references))
    _update(
        BlogPhase.RESEARCH,
        sub_progress=1.0,
        status_text=f"Research complete: {len(research_result.references)} sources found",
        research_sources_count=len(research_result.references),
    )

    research_document = research_result.compiled_document or ""
    if not research_document and research_result.references:
        parts = ["## Sources\n"]
        for ref in research_result.references:
            parts.append(f"- **{ref.title}** ({ref.url}): {ref.summary}")
            if ref.key_points:
                parts.append("  Key points: " + "; ".join(ref.key_points[:3]))
        research_document = "\n".join(parts)

    if work_dir is not None:
        write_artifact(work_dir, "research_packet.md", research_document)
        logger.info("Persisted research_packet.md")
        # Extract and persist allowed claims for claims policy
        try:
            allowed = extract_allowed_claims(
                llm_client,
                research_document,
                research_result.references,
                topic=brief.brief[:100],
            )
            write_artifact(work_dir, "allowed_claims.json", allowed.to_dict())
            logger.info("Persisted allowed_claims.json (%s claims)", len(allowed.claims))
        except Exception as e:
            logger.warning("Could not extract allowed claims: %s", e)

    # 2. Review
    _update(
        BlogPhase.REVIEW,
        sub_progress=0.0,
        status_text="Generating title choices and outline...",
    )
    
    try:
        review_agent = BlogReviewAgent(llm_client=llm_client)
        review_input = BlogReviewInput(
            brief=brief.brief,
            audience=brief.audience,
            tone_or_purpose=brief.tone_or_purpose,
            references=research_result.references,
        )
        review_result = review_agent.run(review_input)
    except BloggingError:
        raise
    except Exception as e:
        raise ReviewError(f"Review failed: {e}", cause=e) from e
    
    logger.info("Review complete: %s title choices", len(review_result.title_choices))
    _update(
        BlogPhase.REVIEW,
        sub_progress=1.0,
        status_text=f"Review complete: {len(review_result.title_choices)} title choices generated",
    )

    if work_dir is not None:
        write_artifact(work_dir, "outline.md", review_result.outline)
        # Optionally persist content_brief with title choices
        content_brief = "# Content Brief\n\n## Title Choices\n"
        for i, tc in enumerate(review_result.title_choices, 1):
            content_brief += f"{i}. {tc.title} [{tc.probability_of_success:.0%}]\n"
        content_brief += "\n## Outline\n\n" + review_result.outline
        write_artifact(work_dir, "content_brief.md", content_brief)
        logger.info("Persisted outline.md and content_brief.md")

    # 3. Draft + Copy Editor loop
    style_guide_text = STYLE_GUIDE_PATH.read_text().strip() if STYLE_GUIDE_PATH.exists() else None
    brand_spec_path = Path(__file__).resolve().parent.parent / "docs" / "brand_spec.yaml"
    allowed_claims_data = (
        read_artifact(work_dir, "allowed_claims.json") if work_dir is not None else None
    )
    draft_agent = BlogDraftAgent(
        llm_client=llm_client,
        default_style_guide_path=STYLE_GUIDE_PATH,
        brand_spec_path=brand_spec_path if brand_spec_path.exists() else None,
    )
    copy_editor_agent = BlogCopyEditorAgent(
        llm_client=llm_client,
        default_style_guide_path=STYLE_GUIDE_PATH,
        brand_spec_path=brand_spec_path if brand_spec_path.exists() else None,
    )

    draft_result = None
    for iteration in range(1, draft_editor_iterations + 1):
        if iteration == 1:
            # Initial draft
            _update(
                BlogPhase.DRAFT_INITIAL,
                sub_progress=0.0,
                status_text="Generating initial draft...",
                draft_iterations=iteration,
            )
            
            try:
                draft_input = DraftInput(
                    research_document=research_document,
                    outline=review_result.outline,
                    audience=brief.audience,
                    tone_or_purpose=brief.tone_or_purpose,
                    style_guide=style_guide_text,
                    brand_spec_path=str(brand_spec_path) if brand_spec_path.exists() else None,
                    allowed_claims=allowed_claims_data if isinstance(allowed_claims_data, dict) else None,
                )
                draft_result = draft_agent.run(draft_input)
            except BloggingError:
                raise
            except Exception as e:
                raise DraftError(f"Initial draft generation failed: {e}", iteration=iteration, cause=e) from e
            
            logger.info("Draft iteration %s: initial draft, length=%s", iteration, len(draft_result.draft))
            _update(
                BlogPhase.DRAFT_INITIAL,
                sub_progress=1.0,
                status_text=f"Initial draft complete ({len(draft_result.draft)} chars)",
                draft_iterations=iteration,
            )
            if work_dir is not None:
                write_artifact(work_dir, "draft_v1.md", draft_result.draft)
        else:
            # Copy edit loop
            sub_progress = (iteration - 1) / draft_editor_iterations
            _update(
                BlogPhase.COPY_EDIT_LOOP,
                sub_progress=sub_progress,
                status_text=f"Copy edit iteration {iteration - 1}/{draft_editor_iterations - 1}...",
                draft_iterations=iteration,
            )
            
            try:
                copy_editor_input = CopyEditorInput(
                    draft=draft_result.draft,
                    audience=brief.audience,
                    tone_or_purpose=brief.tone_or_purpose,
                    style_guide=style_guide_text,
                )
                copy_editor_result = copy_editor_agent.run(copy_editor_input)
                logger.info(
                    "Copy editor iteration %s: %s feedback items",
                    iteration,
                    len(copy_editor_result.feedback_items),
                )
                revise_input = ReviseDraftInput(
                    draft=draft_result.draft,
                    feedback_items=copy_editor_result.feedback_items,
                    feedback_summary=copy_editor_result.summary,
                    research_document=research_document,
                    outline=review_result.outline,
                    audience=brief.audience,
                    tone_or_purpose=brief.tone_or_purpose,
                    style_guide=style_guide_text,
                    brand_spec_path=str(brand_spec_path) if brand_spec_path.exists() else None,
                    allowed_claims=allowed_claims_data if isinstance(allowed_claims_data, dict) else None,
                )
                draft_result = draft_agent.revise(revise_input)
            except BloggingError:
                raise
            except Exception as e:
                raise DraftError(f"Draft revision failed: {e}", iteration=iteration, cause=e) from e
            
            logger.info("Draft iteration %s: revised, length=%s", iteration, len(draft_result.draft))
            if work_dir is not None:
                write_artifact(work_dir, "draft_v2.md", draft_result.draft)
    
    _update(
        BlogPhase.COPY_EDIT_LOOP,
        sub_progress=1.0,
        status_text="Draft editing complete",
        draft_iterations=draft_editor_iterations,
    )

    status: PipelineStatus = "PASS"
    if work_dir is not None:
        write_artifact(work_dir, "final.md", draft_result.draft)
        logger.info("Persisted final.md")
        
    if work_dir is not None and run_gates:
        brand_spec = load_brand_spec(brand_spec_path)
        compliance_agent = BlogComplianceAgent(llm_client=llm_client)
        fact_check_agent = BlogFactCheckAgent(llm_client=llm_client)

        for rewrite_iter in range(max_rewrite_iterations):
            # Fact check phase
            _update(
                BlogPhase.FACT_CHECK,
                sub_progress=rewrite_iter / max_rewrite_iterations,
                status_text=f"Running fact check (iteration {rewrite_iter + 1})...",
                rewrite_iterations=rewrite_iter,
            )
            
            try:
                validator_report = run_validators_from_work_dir(work_dir)
                fact_report = fact_check_agent.run(
                    draft_result.draft,
                    allowed_claims=allowed_claims_data if isinstance(allowed_claims_data, dict) else None,
                    require_disclaimer_for=brand_spec.content_rules.safety.require_disclaimer_for,
                    work_dir=work_dir,
                )
            except BloggingError:
                raise
            except Exception as e:
                raise FactCheckError(f"Fact check failed: {e}", cause=e) from e
            
            # Compliance phase
            _update(
                BlogPhase.COMPLIANCE,
                sub_progress=rewrite_iter / max_rewrite_iterations,
                status_text=f"Running compliance check (iteration {rewrite_iter + 1})...",
                rewrite_iterations=rewrite_iter,
            )
            
            try:
                compliance_report = compliance_agent.run(
                    draft_result.draft,
                    brand_spec=brand_spec,
                    validator_report=validator_report.model_dump() if hasattr(validator_report, "model_dump") else None,
                    work_dir=work_dir,
                )
            except BloggingError:
                raise
            except Exception as e:
                raise ComplianceError(f"Compliance check failed: {e}", cause=e) from e

            all_pass = (
                validator_report.status == "PASS"
                and fact_report.claims_status == "PASS"
                and fact_report.risk_status == "PASS"
                and compliance_report.status == "PASS"
            )
            if all_pass:
                status = "PASS"
                logger.info("All gates PASS on rewrite iteration %s", rewrite_iter + 1)
                
                _update(
                    BlogPhase.FINALIZE,
                    sub_progress=0.5,
                    status_text="All checks passed, finalizing...",
                )
                
                pack = PublishingPack(
                    title_options=[tc.title for tc in review_result.title_choices[:5]],
                    meta_description=draft_result.draft[:155].strip() or None,
                    tags=[],
                )
                write_artifact(work_dir, "publishing_pack.json", pack.model_dump())
                logger.info("Wrote publishing_pack.json")
                
                _update(
                    BlogPhase.FINALIZE,
                    sub_progress=1.0,
                    status_text="Pipeline complete - all checks passed",
                )
                break

            if rewrite_iter >= max_rewrite_iterations - 1:
                status = "NEEDS_HUMAN_REVIEW"
                logger.warning(
                    "Max rewrite iterations (%s) reached; status=NEEDS_HUMAN_REVIEW",
                    max_rewrite_iterations,
                )
                _update(
                    BlogPhase.FINALIZE,
                    sub_progress=1.0,
                    status_text=f"Needs human review after {max_rewrite_iterations} rewrite attempts",
                )
                break

            # Rewrite loop
            _update(
                BlogPhase.REWRITE_LOOP,
                sub_progress=(rewrite_iter + 1) / max_rewrite_iterations,
                status_text=f"Rewriting to address issues (iteration {rewrite_iter + 1}/{max_rewrite_iterations})...",
                rewrite_iterations=rewrite_iter + 1,
            )
            
            feedback_items = [
                FeedbackItem(
                    category="compliance",
                    severity="must_fix",
                    location=None,
                    issue=fix,
                    suggestion=fix,
                )
                for fix in compliance_report.required_fixes
            ]
            if not feedback_items:
                feedback_items = [
                    FeedbackItem(
                        category="compliance",
                        severity="must_fix",
                        location=None,
                        issue="Validator or compliance check failed; see validator_report.json and compliance_report.json",
                        suggestion="Address all violations and re-run.",
                    )
                ]
            
            try:
                revise_input = ReviseDraftInput(
                    draft=draft_result.draft,
                    feedback_items=feedback_items,
                    feedback_summary=f"Compliance FAIL: {len(compliance_report.violations)} violations. Apply required_fixes.",
                    research_document=research_document,
                    outline=review_result.outline,
                    audience=brief.audience,
                    tone_or_purpose=brief.tone_or_purpose,
                    style_guide=style_guide_text,
                    brand_spec_path=str(brand_spec_path) if brand_spec_path.exists() else None,
                    allowed_claims=allowed_claims_data if isinstance(allowed_claims_data, dict) else None,
                )
                draft_result = draft_agent.revise(revise_input)
            except BloggingError:
                raise
            except Exception as e:
                raise DraftError(f"Rewrite revision failed: {e}", iteration=rewrite_iter + 1, cause=e) from e
            
            write_artifact(work_dir, "final.md", draft_result.draft)
            logger.info("Rewrite iteration %s: applied fixes, re-running gates", rewrite_iter + 1)
    else:
        # No gates - mark as finalized
        _update(
            BlogPhase.FINALIZE,
            sub_progress=1.0,
            status_text="Pipeline complete (gates skipped)",
        )

    return research_result, review_result, draft_result, status


def main() -> None:
    """CLI entrypoint: run pipeline with optional work_dir."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    brief = ResearchBriefInput(
        brief="LLM observability best practices for large enterprises",
        audience="CTOs and platform teams",
        tone_or_purpose="technical deep-dive",
        max_results=20,
    )

    work_dir = Path(__file__).resolve().parent / "run_dir"
    research_result, review_result, draft_result, status = run_pipeline(brief, work_dir=work_dir)

    print("\n--- Title choices ---")
    for i, tc in enumerate(review_result.title_choices, 1):
        print(f"{i}. {tc.title}  [{tc.probability_of_success:.0%}]")
    print("\n--- Outline ---\n")
    print(review_result.outline)
    print("\n--- Draft ---\n")
    print(draft_result.draft[:2000] + ("..." if len(draft_result.draft) > 2000 else ""))
    print(f"\nStatus: {status}")
    print(f"Artifacts written to {work_dir}")


if __name__ == "__main__":
    main()
