"""
Product Requirements Analysis Agent.

4-phase workflow: Spec Review → Communicate with User → Spec Update → Spec Cleanup.

This agent ensures the product specification is complete, consistent, and ready
for the Product Planning Agent.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from llm_service import get_client, get_strands_model
from llm_service.strands_model import resolve_strands_model
from software_engineering_team.shared.json_utils import (
    default_decompose_by_sections,
)

# MAX_GAP_ROUNDS / MAX_SOP_ROUNDS are re-exported here for callers/tests that import
# them from this module.
from .context_discovery import (
    inject_context_answers_into_spec,
    run_context_constraints_discovery,
)
from .llm_io import call_llm_json, call_llm_text
from .models import (
    AnalysisPhase,
    AnalysisWorkflowResult,
    AnsweredQuestion,
    ArchitectureAnalysisResult,
    OpenQuestion,
    QuestionOption,
    SOPDecision,
    SOPSubPhase,
    SpecCleanupResult,
    SpecReviewResult,
)
from .qa_history import (
    extract_answer_from_qa_history,
    format_answered_questions_for_prompt,
    is_same_decision,
    parse_qa_history_blocks,
    read_qa_history,
    record_answers,
)
from .question_processing import (
    add_recommendations,
    cap_open_questions,
    consolidate_open_questions,
    dedupe_questions_by_answer_similarity,
    filter_duplicate_questions,
    filter_organizational_questions,
    parse_open_question,
    parse_question_option,
    parse_spec_review_response,
    review_question_answer_alignment,
)
from .sop_engine import (  # noqa: F401
    MAX_GAP_ROUNDS,
    MAX_SOP_ROUNDS,
    apply_architecture_approval,
    assess_sub_phase_gaps,
    build_architecture_approval_questions,
    build_question_options,
    evaluate_sop_conditionals,
    extract_sop_decisions_from_spec,
    format_architecture_document,
    generate_spec_aware_options,
    run_sop_phase1,
    run_sop_phase2_architecture,
)
from .spec_review import (
    format_context_for_review,
    run_spec_review,
)
from .spec_writing import (
    _merge_spec_cleanup_results,
    build_specialist_collaboration_plan,
    format_answered_questions,
    generate_prd_document,
    parse_spec_cleanup_response,
    run_spec_cleanup,
    update_spec,
    update_spec_for_consistency_and_clarity,
    update_spec_from_duplicates,
)
from .user_communication import (
    apply_all_defaults,
    apply_answers,
    communicate_with_user,
    convert_to_pending_questions,
    get_default_option,
    wait_for_answers,
)

logger = logging.getLogger(__name__)

OPEN_QUESTIONS_POLL_INTERVAL = 5.0
MAX_ITERATIONS = 100
MAX_DECOMPOSITION_DEPTH = 20
MAX_ISSUES = 10
MAX_GAPS = 10

# When deduplication reduces question count by this fraction or more, run consistency/clarity update and re-review.
DEDUP_REDUCTION_THRESHOLD = 0.5
MAX_CONSISTENCY_LOOPS = 3

# Subdirectory under repo where PRA writes all artifacts (validated_spec, PRD, updated_spec*, qa_history).
PRODUCT_ANALYSIS_SUBDIR = "plan/product_analysis"


def _section_title_from_chunk(chunk: str, max_len: int = 55) -> str:
    """Extract a short, meaningful title from a spec chunk (e.g. first markdown heading)."""
    if not chunk or not chunk.strip():
        return ""
    first_line = chunk.strip().split("\n")[0].strip()
    while first_line.startswith("#"):
        first_line = first_line.lstrip("#").strip()
    if not first_line:
        return ""
    return first_line[:max_len].strip()


class ProductRequirementsAnalysisAgent:
    """
    Product Requirements Analysis Agent with 4-phase workflow.

    Phases:
    1. Spec Review - Identify gaps and generate questions
    2. Communicate with User - Send questions, wait for answers
    3. Spec Update - Incorporate answers into spec
    4. Spec Cleanup - Validate and clean the spec

    The cycle (1-3) repeats until no open questions remain, then Spec Cleanup runs.
    """

    def __init__(self, llm_client=None) -> None:
        # A raw LLMClient (e.g. OllamaLLMClient) doesn't implement the Strands
        # Model interface (stream/update_config/get_config) directly, so it is
        # wrapped in an LLMClientModel rather than used as-is.
        self._model = resolve_strands_model(
            llm_client, agent_key="product_analysis", get_strands_model_fn=get_strands_model
        )
        # Keep LLMClient for context_sizing utilities
        self.llm = llm_client if llm_client is not None else get_client("product_analysis")

    def _has_existing_pra_artifacts(self, repo_path: Path) -> bool:
        """Return True if plan/product_analysis has prior PRA output we can resume from."""
        pa_dir = repo_path / "plan" / "product_analysis"
        if not pa_dir.is_dir():
            return False
        # qa_history.md: substantive only when length > 200 and contains iteration/answer markers
        qa_path = pa_dir / "qa_history.md"
        if qa_path.is_file():
            try:
                content = qa_path.read_text(encoding="utf-8")
                if len(content) > 200 and ("## Iteration" in content or "**Answer:**" in content):
                    return True
            except OSError:
                pass
        if (pa_dir / "validated_spec.md").is_file():
            return True
        # Any updated_spec_v*.md or updated_spec.md
        for p in pa_dir.iterdir():
            if p.is_file() and p.suffix == ".md":
                name = p.name
                if name == "updated_spec.md" or (
                    name.startswith("updated_spec_v") and name.endswith(".md")
                ):
                    return True
        return False

    def run_workflow(
        self,
        *,
        spec_content: str,
        repo_path: Path,
        job_id: Optional[str] = None,
        job_updater: Optional[Callable[..., None]] = None,
        max_iterations: int = MAX_ITERATIONS,
        context_files: Optional[Dict[str, str]] = None,
        initial_spec_path: Optional[Path] = None,
    ) -> AnalysisWorkflowResult:
        """
        Execute the full Product Requirements Analysis workflow.

        Args:
            spec_content: The initial specification content
            repo_path: Path to the repository for storing artifacts
            job_id: Job ID for question tracking (required for user communication)
            job_updater: Callback to update job status
            max_iterations: Maximum number of spec review cycles
            context_files: Optional dict of additional context files (path -> content)
            initial_spec_path: Path to the file the spec was loaded from (for rename when needing more detail)

        Returns:
            AnalysisWorkflowResult with validated spec and answered questions
        """
        start_time = time.monotonic()
        result = AnalysisWorkflowResult()
        current_spec = spec_content
        all_answered_questions: List[AnsweredQuestion] = []
        iteration = 0
        self._context_files = context_files or {}

        def _update_job(**kwargs: Any) -> None:
            if job_updater:
                try:
                    job_updater(**kwargs)
                except Exception:
                    pass

        logger.info("Product Requirements Analysis Agent: WORKFLOW START")

        from software_engineering_team.spec_parser import get_next_updated_spec_version

        base_version = get_next_updated_spec_version(repo_path)
        product_analysis_dir = repo_path / "plan" / "product_analysis"
        product_analysis_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Initialized %s for PRA artifacts", PRODUCT_ANALYSIS_SUBDIR)

        # If the product requirements document already exists, skip PRA and proceed to planning.
        prd_path = product_analysis_dir / "product_requirements_document.md"
        if prd_path.is_file():
            logger.info(
                "Product requirements document already present at %s; skipping PRA and proceeding to planning.",
                prd_path.name,
            )
            result.success = True
            result.summary = "PRD already present; skipping PRA and proceeding to planning."
            validated_spec_path = product_analysis_dir / "validated_spec.md"
            if validated_spec_path.is_file():
                result.validated_spec_path = str(validated_spec_path)
                try:
                    result.final_spec_content = validated_spec_path.read_text(encoding="utf-8")
                except OSError:
                    pass
            _update_job(
                current_phase=AnalysisPhase.SPEC_CLEANUP.value,
                progress=100,
                message=result.summary,
                status_text="PRD already present - skipping PRA, proceeding to planning",
            )
            return result

        # One-time context and constraints discovery (before first spec review) when job_id is set
        ok, current_spec = self._run_context_discovery(
            current_spec=current_spec,
            repo_path=repo_path,
            job_id=job_id,
            product_analysis_dir=product_analysis_dir,
            all_answered_questions=all_answered_questions,
            result=result,
            update_job=_update_job,
        )
        if not ok:
            return result

        while iteration < max_iterations:
            iteration += 1
            result.iterations = iteration

            # Phase 1: Spec Review
            result.current_phase = AnalysisPhase.SPEC_REVIEW
            _update_job(
                current_phase=AnalysisPhase.SPEC_REVIEW.value,
                progress=5 + (iteration - 1) * 15,
                message=f"Spec review iteration {iteration}",
                status_text=f"Analyzing specification for gaps and inconsistencies (iteration {iteration})",
            )

            def _on_spec_review_progress(_chunk_index: int, _total_chunks: int) -> None:
                _update_job(
                    status_text="Analyzing full specification for gaps and inconsistencies..."
                )

            def _phase_spec_review() -> Tuple["SpecReviewResult", str]:
                """Review the spec (re-running once if a clarification changed it);
                return the review result and the (possibly updated) spec."""
                _update_job(
                    status_text="Analyzing full specification for gaps and inconsistencies..."
                )
                spec_before_review = current_spec
                review, cur_spec = self._run_spec_review(
                    current_spec,
                    repo_path,
                    iteration=iteration,
                    spec_version=base_version + (iteration - 1),
                    answered_questions=all_answered_questions,
                    on_chunk_progress=_on_spec_review_progress,
                )
                if cur_spec != spec_before_review:
                    _update_job(
                        status_text="Re-analyzing full specification after clarification..."
                    )
                    review, cur_spec = self._run_spec_review(
                        cur_spec,
                        repo_path,
                        iteration=iteration,
                        spec_version=base_version + (iteration - 1),
                        answered_questions=all_answered_questions,
                        on_chunk_progress=_on_spec_review_progress,
                    )
                    logger.info("Re-ran spec review on clarified spec")
                result.spec_review_result = review
                if review.open_questions:
                    _update_job(
                        status_text=f"Found {len(review.issues)} issues, {len(review.gaps)} gaps, {len(review.open_questions)} questions"
                    )
                return review, cur_spec

            ok, ret = self._run_phase(result, "Spec review", _phase_spec_review)
            if not ok:
                return result
            spec_review_result, current_spec = ret

            # Consolidate duplicate/semantically-similar questions before sending to user
            original_count = len(spec_review_result.open_questions)
            _update_job(
                status_text="Consolidating open questions (merging duplicates)...",
            )
            consolidated_questions = self._consolidate_open_questions(
                spec_review_result.open_questions
            )
            if len(consolidated_questions) < original_count:
                _update_job(
                    status_text=f"Consolidated {original_count} questions into {len(consolidated_questions)} distinct questions",
                )
                logger.info(
                    "Consolidated open questions: %d -> %d",
                    original_count,
                    len(consolidated_questions),
                )
            spec_review_result = spec_review_result.model_copy(
                update={"open_questions": consolidated_questions}
            )
            open_count = len(spec_review_result.open_questions)
            count_before_dedup = open_count

            _update_job(
                status_text="Deduplicating questions whose answers we already have...",
            )
            deduped_questions = self._dedupe_questions_by_answer_similarity(
                spec_review_result.open_questions,
                all_answered_questions,
            )
            if len(deduped_questions) < open_count:
                _update_job(
                    status_text=f"Reduced to {len(deduped_questions)} questions (already have answers for the rest)",
                )
                logger.info(
                    "Deduped open questions by answer similarity: %d -> %d",
                    open_count,
                    len(deduped_questions),
                )
                spec_review_result = spec_review_result.model_copy(
                    update={"open_questions": deduped_questions}
                )
                open_count = len(spec_review_result.open_questions)

            # If deduplication reduced questions by 50%+, update spec for consistency/clarity and re-review
            reduction_ratio = (
                (count_before_dedup - len(deduped_questions)) / count_before_dedup
                if count_before_dedup > 0
                else 0.0
            )
            spec_review_result, current_spec, open_count = self._run_consistency_loops(
                result=result,
                current_spec=current_spec,
                spec_review_result=spec_review_result,
                open_count=open_count,
                reduction_ratio=reduction_ratio,
                count_before_dedup=count_before_dedup,
                deduped_questions=deduped_questions,
                repo_path=repo_path,
                iteration=iteration,
                base_version=base_version,
                all_answered_questions=all_answered_questions,
                update_job=_update_job,
                on_chunk_progress=_on_spec_review_progress,
            )

            # Cap only after consolidation + answer-similarity dedupe (and any
            # consistency re-runs) so near-duplicates do not crowd out distinct topics.
            capped_questions = cap_open_questions(spec_review_result.open_questions)
            if len(capped_questions) < len(spec_review_result.open_questions):
                _update_job(
                    status_text=(
                        f"Limited to {len(capped_questions)} open questions "
                        f"(from {len(spec_review_result.open_questions)})"
                    ),
                )
                spec_review_result = spec_review_result.model_copy(
                    update={"open_questions": capped_questions}
                )
                open_count = len(capped_questions)

            _update_job(
                status_text="Checking question and answer alignment...",
            )
            aligned_questions = self._review_question_answer_alignment(
                spec_review_result.open_questions
            )
            spec_review_result = spec_review_result.model_copy(
                update={"open_questions": aligned_questions}
            )

            _update_job(
                status_text="Adding recommendations to questions...",
            )
            questions_with_recommendations = self._add_recommendations(
                spec_review_result.open_questions, current_spec
            )
            spec_review_result = spec_review_result.model_copy(
                update={"open_questions": questions_with_recommendations}
            )

            logger.info(
                "Iteration %d: Found %d issues, %d gaps, %d open questions",
                iteration,
                len(spec_review_result.issues),
                len(spec_review_result.gaps),
                open_count,
            )

            if not spec_review_result.open_questions:
                logger.info("No open questions, proceeding to Spec Cleanup")
                break

            # If we need more detail and the input was validated_spec.md, rename it to
            # updated_spec_v{next} so we don't overwrite it; subsequent Q&A updates use v+1, v+2, ...
            validated_spec_path = product_analysis_dir / "validated_spec.md"
            if (
                iteration == 1
                and initial_spec_path is not None
                and initial_spec_path.resolve() == validated_spec_path.resolve()
                and validated_spec_path.exists()
            ):
                next_v = base_version
                target = product_analysis_dir / f"updated_spec_v{next_v}.md"
                validated_spec_path.rename(target)
                logger.info(
                    "Renamed validated_spec.md to %s (agent needs more detail); updates will use v%d+",
                    target.name,
                    next_v,
                )
                base_version = get_next_updated_spec_version(repo_path)

            # Phase 2: Communicate with User
            result.current_phase = AnalysisPhase.COMMUNICATE
            _update_job(
                current_phase=AnalysisPhase.COMMUNICATE.value,
                progress=10 + (iteration - 1) * 15,
                message=f"Waiting for answers to {len(spec_review_result.open_questions)} question(s)",
                status_text=f"Waiting for your input on {len(spec_review_result.open_questions)} question(s)",
            )

            ok, answered_questions = self._run_phase(
                result,
                "Communication",
                lambda: self._communicate_with_user(
                    job_id=job_id,
                    open_questions=spec_review_result.open_questions,
                    repo_path=repo_path,
                    iteration=iteration,
                ),
            )
            if not ok:
                return result

            if not answered_questions:
                raise RuntimeError(
                    "No answers received from user communication phase. "
                    "User input is required to proceed."
                )

            all_answered_questions.extend(answered_questions)
            result.answered_questions = all_answered_questions

            # Phase 3: Spec Update
            result.current_phase = AnalysisPhase.SPEC_UPDATE
            _update_job(
                current_phase=AnalysisPhase.SPEC_UPDATE.value,
                progress=15 + (iteration - 1) * 15,
                message=f"Updating spec with {len(answered_questions)} answers",
                status_text=f"Incorporating {len(answered_questions)} answer(s) into the specification",
            )

            def _phase_spec_update() -> str:
                """Incorporate the answered questions into the spec; return the updated spec."""
                _update_job(status_text="Generating updated specification based on your answers")
                cur_spec = self._update_spec(
                    current_spec=current_spec,
                    answered_questions=answered_questions,
                    repo_path=repo_path,
                    version=base_version + (iteration - 1),
                )
                _update_job(status_text="Incorporated answers into spec")
                _update_job(status_text="Specification updated successfully")
                return cur_spec

            ok, ret = self._run_phase(result, "Spec update", _phase_spec_update)
            if not ok:
                return result
            current_spec = ret

        # Phase 4: Spec Cleanup
        result.current_phase = AnalysisPhase.SPEC_CLEANUP
        _update_job(
            current_phase=AnalysisPhase.SPEC_CLEANUP.value,
            progress=90,
            message="Validating and cleaning specification",
            status_text="Validating specification completeness and consistency",
        )

        def _phase_cleanup() -> Tuple[SpecCleanupResult, str]:
            """Validate/clean the spec and generate the PRD; return the cleanup
            result and the generated PRD content."""
            _update_job(status_text="Running final validation and cleanup on specification")
            cleanup_chunks = default_decompose_by_sections(current_spec)
            cleanup_titles = [_section_title_from_chunk(c) for c in cleanup_chunks]

            def _on_spec_cleanup_chunk(chunk_index: int, total_chunks: int) -> None:
                if chunk_index < len(cleanup_titles) and cleanup_titles[chunk_index]:
                    status_text = f"Validating: {cleanup_titles[chunk_index]}..."
                else:
                    status_text = (
                        f"Validating specification (section {chunk_index + 1}/{total_chunks})..."
                    )
                _update_job(status_text=status_text)

            cleanup = self._run_spec_cleanup(
                current_spec,
                repo_path,
                on_chunk_progress=_on_spec_cleanup_chunk,
            )
            result.spec_cleanup_result = cleanup
            _update_job(status_text="Validation complete")
            # Generate a Product Requirements Document (PRD) from the cleaned spec
            prd = self._generate_prd_document(
                cleaned_spec=cleanup.cleaned_spec,
                answered_questions=all_answered_questions,
            )
            result.final_spec_content = cleanup.cleaned_spec
            return cleanup, prd

        ok, cleanup_ret = self._run_phase(result, "Spec cleanup", _phase_cleanup)
        if not ok:
            return result
        cleanup_result, prd_content = cleanup_ret

        # Save validated spec (cleaned spec) and PRD separately.
        product_analysis_dir = repo_path / "plan" / "product_analysis"
        product_analysis_dir.mkdir(parents=True, exist_ok=True)
        validated_spec_path = product_analysis_dir / "validated_spec.md"
        validated_spec_path.write_text(cleanup_result.cleaned_spec, encoding="utf-8")
        result.validated_spec_path = str(validated_spec_path)

        # Also write an explicit PRD file for clarity
        try:
            prd_path = product_analysis_dir / "product_requirements_document.md"
            prd_path.write_text(prd_content, encoding="utf-8")
            logger.info("Product Requirements Analysis: PRD saved to %s", prd_path.name)
        except Exception as exc:
            logger.warning("Product Requirements Analysis: Failed to write PRD alias file: %s", exc)

        result.success = True
        result.summary = (
            f"Analysis complete: {result.iterations} iteration(s), "
            f"{len(all_answered_questions)} questions answered. "
            f"Validated spec saved to validated_spec.md; PRD saved to product_requirements_document.md"
        )

        _update_job(
            current_phase=AnalysisPhase.SPEC_CLEANUP.value,
            progress=100,
            message=result.summary,
            status_text="Product analysis complete - validated spec and PRD generated",
        )

        elapsed = time.monotonic() - start_time
        logger.info("Product Requirements Analysis Agent: WORKFLOW COMPLETE in %.1fs", elapsed)

        return result

    def _run_context_discovery(
        self,
        *,
        current_spec: str,
        repo_path: Path,
        job_id: Optional[str],
        product_analysis_dir: Path,
        all_answered_questions: List[AnsweredQuestion],
        result: "AnalysisWorkflowResult",
        update_job: Callable[..., None],
    ) -> Tuple[bool, str]:
        """Run one-time SOP context/constraint + architecture discovery.

        When prior PRA artifacts exist, resumes by loading the latest spec and
        skipping discovery. Otherwise runs SOP Phase 1 (environment constraints)
        and the non-fatal SOP Phase 2 (architecture analysis). A no-op when
        ``job_id`` is ``None``.

        Preconditions: ``product_analysis_dir`` exists.
        Postconditions: returns ``(ok, current_spec)``. ``ok`` is ``False`` only
        when SOP Phase 1 fails (``result.failure_reason`` set); the caller must
        then ``return result``. Mutates ``result`` and ``all_answered_questions``.
        """
        if job_id is None:
            logger.info("job_id is None; skipping context discovery")
            return True, current_spec

        if self._has_existing_pra_artifacts(repo_path):
            logger.info(
                "Skipping context discovery; plan/product_analysis has prior PRA output, picking up from there."
            )
            result.current_phase = AnalysisPhase.SPEC_REVIEW
            update_job(
                current_phase=AnalysisPhase.SPEC_REVIEW.value,
                progress=5,
                message="Resuming from prior analysis; reviewing specification...",
                status_text="Resuming from prior analysis; reviewing specification...",
            )
            # Load current_spec from existing artifacts when resuming
            validated_spec_path = product_analysis_dir / "validated_spec.md"
            if validated_spec_path.is_file():
                current_spec = validated_spec_path.read_text(encoding="utf-8")
            else:
                # Latest updated_spec_v*.md or updated_spec.md by version or mtime
                candidates: List[Path] = []
                for p in product_analysis_dir.iterdir():
                    if not p.is_file() or p.suffix != ".md":
                        continue
                    name = p.name
                    if name == "updated_spec.md":
                        candidates.append(p)
                    elif name.startswith("updated_spec_v") and name.endswith(".md"):
                        candidates.append(p)
                if candidates:

                    def _spec_sort_key(path: Path) -> Tuple[int, float]:
                        # Prefer higher version number; then mtime
                        name = path.stem
                        if name.startswith("updated_spec_v"):
                            try:
                                ver = int(name.split("_v")[-1].split("_")[0])
                                return (ver, path.stat().st_mtime)
                            except (ValueError, IndexError):
                                pass
                        return (0, path.stat().st_mtime)

                    latest_spec_file = max(candidates, key=_spec_sort_key)
                    current_spec = latest_spec_file.read_text(encoding="utf-8")
            return True, current_spec

        # SOP Phase 1: Structured environment/constraint questions
        result.current_phase = AnalysisPhase.SOP_PHASE1
        update_job(
            current_phase=AnalysisPhase.SOP_PHASE1.value,
            progress=2,
            message="Gathering environment constraints (SOP Phase 1)...",
            status_text="Gathering environment constraints (SOP Phase 1)...",
        )

        def _phase_sop1() -> Tuple[List[SOPDecision], str]:
            decisions, cur_spec, sop_answered = self._run_sop_phase1(
                current_spec,
                repo_path,
                job_id,
                update_job,
            )
            all_answered_questions.extend(sop_answered)
            return decisions, cur_spec

        ok, ret = self._run_phase(result, "SOP Phase 1", _phase_sop1)
        if not ok:
            return False, current_spec
        sop_decisions, current_spec = ret

        # SOP Phase 2: Architecture analysis (autonomous + approval)
        result.current_phase = AnalysisPhase.SOP_PHASE2_ARCHITECTURE
        update_job(
            current_phase=AnalysisPhase.SOP_PHASE2_ARCHITECTURE.value,
            progress=8,
            message="Analyzing architecture (SOP Phase 2)...",
            status_text="Analyzing architecture (SOP Phase 2)...",
        )
        try:
            arch_result, current_spec = self._run_sop_phase2_architecture(
                current_spec,
                sop_decisions,
                repo_path,
                job_id,
                update_job,
            )
            result.architecture_analysis = arch_result
        except Exception as exc:
            logger.warning("SOP Phase 2 failed (non-fatal): %s", str(exc)[:200])
        return True, current_spec

    def _run_consistency_loops(
        self,
        *,
        result: "AnalysisWorkflowResult",
        current_spec: str,
        spec_review_result: "SpecReviewResult",
        open_count: int,
        reduction_ratio: float,
        count_before_dedup: int,
        deduped_questions: List[OpenQuestion],
        repo_path: Path,
        iteration: int,
        base_version: int,
        all_answered_questions: List[AnsweredQuestion],
        update_job: Callable[..., None],
        on_chunk_progress: Callable[[int, int], None],
    ) -> Tuple["SpecReviewResult", str, int]:
        """Clarify the spec and re-review while dedup keeps collapsing questions.

        Runs up to ``MAX_CONSISTENCY_LOOPS`` passes: each rewrites the spec for
        consistency/clarity from the Q&A history, re-reviews it, then
        re-consolidates and re-dedupes the resulting questions. Stops early once
        a pass drops below ``DEDUP_REDUCTION_THRESHOLD`` reduction or no open
        questions remain.

        Preconditions: ``spec_review_result``/``current_spec`` are the latest
        review output; ``reduction_ratio``/``count_before_dedup``/
        ``deduped_questions`` describe the dedup that just ran.
        Postconditions: returns the updated
        ``(spec_review_result, current_spec, open_count)``; sets
        ``result.spec_review_result`` on every pass.
        """
        consistency_loops = 0
        spec_version = base_version + (iteration - 1)
        while (
            reduction_ratio >= DEDUP_REDUCTION_THRESHOLD
            and consistency_loops < MAX_CONSISTENCY_LOOPS
            and len(spec_review_result.open_questions) > 0
        ):
            consistency_loops += 1
            update_job(
                status_text="Many duplicate questions found. Updating spec for clarity and to resolve conflicts using Q&A history...",
            )
            qa_history = self._read_qa_history(repo_path)
            update_job(
                status_text="Editing spec: clarifying answers and removing conflicting information...",
            )
            current_spec = self._update_spec_for_consistency_and_clarity(
                current_spec,
                repo_path,
                qa_history,
                all_answered_questions,
                spec_version,
                consistency_loops,
            )
            update_job(
                status_text="Spec updated. Re-analyzing full specification after consistency update...",
            )
            spec_review_result, current_spec = self._run_spec_review(
                current_spec,
                repo_path,
                iteration=iteration,
                spec_version=spec_version,
                answered_questions=all_answered_questions,
                on_chunk_progress=on_chunk_progress,
            )
            result.spec_review_result = spec_review_result
            # Re-consolidate and re-dedupe
            update_job(
                status_text="Re-consolidating and re-deduplicating questions after spec update...",
            )
            consolidated_questions = self._consolidate_open_questions(
                spec_review_result.open_questions
            )
            spec_review_result = spec_review_result.model_copy(
                update={"open_questions": consolidated_questions}
            )
            open_count = len(spec_review_result.open_questions)
            count_before_dedup = open_count
            deduped_questions = self._dedupe_questions_by_answer_similarity(
                spec_review_result.open_questions,
                all_answered_questions,
            )
            if len(deduped_questions) < open_count:
                logger.info(
                    "After consistency loop %d: deduped %d -> %d",
                    consistency_loops,
                    open_count,
                    len(deduped_questions),
                )
            spec_review_result = spec_review_result.model_copy(
                update={"open_questions": deduped_questions}
            )
            open_count = len(spec_review_result.open_questions)
            reduction_ratio = (
                (count_before_dedup - len(deduped_questions)) / count_before_dedup
                if count_before_dedup > 0
                else 0.0
            )
            if not spec_review_result.open_questions:
                logger.info("No open questions after consistency update, proceeding")
                break
        return spec_review_result, current_spec, open_count

    def _merge_spec_cleanup_results(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Delegates to :func:`spec_writing._merge_spec_cleanup_results`."""
        return _merge_spec_cleanup_results(results)

    def _format_context_for_review(self) -> str:
        """Delegates to :func:`spec_review.format_context_for_review`."""
        return format_context_for_review(self._context_files)

    def _run_spec_review(
        self,
        spec_content: str,
        repo_path: Path,
        iteration: int = 1,
        spec_version: Optional[int] = None,
        answered_questions: Optional[List[AnsweredQuestion]] = None,
        on_chunk_progress: Optional[Callable[[int, int], None]] = None,
    ) -> tuple[SpecReviewResult, str]:
        """Delegates to :func:`spec_review.run_spec_review` with this agent's state."""
        return run_spec_review(
            self._model,
            self.llm,
            self._context_files,
            spec_content,
            repo_path,
            iteration=iteration,
            spec_version=spec_version,
            answered_questions=answered_questions,
            on_chunk_progress=on_chunk_progress,
        )

    def _format_answered_questions_for_prompt(
        self, answered_questions: List[AnsweredQuestion]
    ) -> str:
        """Delegates to :func:`qa_history.format_answered_questions_for_prompt`."""
        return format_answered_questions_for_prompt(answered_questions)

    def _read_qa_history(self, repo_path: Path) -> str:
        """Delegates to :func:`qa_history.read_qa_history`."""
        return read_qa_history(repo_path)

    def _filter_duplicate_questions(
        self,
        new_questions: List[OpenQuestion],
        qa_history: str,
    ) -> tuple[List[OpenQuestion], List[OpenQuestion]]:
        """Delegates to :func:`question_processing.filter_duplicate_questions`."""
        return filter_duplicate_questions(new_questions, qa_history)

    def _filter_organizational_questions(self, questions: List[OpenQuestion]) -> List[OpenQuestion]:
        """Delegates to :func:`question_processing.filter_organizational_questions`."""
        return filter_organizational_questions(questions)

    def _extract_answer_from_qa_history(
        self,
        question: OpenQuestion,
        qa_history: str,
    ) -> Optional[AnsweredQuestion]:
        """Delegates to :func:`qa_history.extract_answer_from_qa_history`."""
        return extract_answer_from_qa_history(question, qa_history)

    def _parse_spec_review_response(self, raw: Any) -> SpecReviewResult:
        """Delegates to :func:`question_processing.parse_spec_review_response`."""
        return parse_spec_review_response(raw)

    def _parse_open_question(self, q_data: Any, index: int) -> OpenQuestion:
        """Delegates to :func:`question_processing.parse_open_question`."""
        return parse_open_question(q_data, index)

    # ------------------------------------------------------------------
    # SOP Phase 1 & 2 methods
    # ------------------------------------------------------------------

    def _call_llm_text(self, prompt: str) -> str:
        """Delegates to :func:`llm_io.call_llm_text` with this agent's model.

        Preconditions: ``prompt`` is a non-empty string.
        Postconditions: returns the model's stripped text; raises ``ValueError``
        on an invalid prompt.
        """
        return call_llm_text(self._model, prompt)

    def _call_llm_json(self, prompt: str) -> Optional[dict]:
        """Delegates to :func:`llm_io.call_llm_json` with this agent's model.

        Postconditions: returns the parsed ``dict`` or ``None`` (never raises on
        parse failure).
        """
        return call_llm_json(self._model, prompt)

    def _run_phase(
        self,
        result: "AnalysisWorkflowResult",
        name: str,
        fn: Callable[[], Any],
    ) -> Tuple[bool, Any]:
        """Run one fatal workflow phase, capturing failures uniformly.

        Collapses the ``try: <phase> except Exception as exc:
        result.failure_reason = f"... failed: {exc}"; logger.error(...); return``
        block that was copy-pasted across ``run_workflow``.

        The broad ``except Exception`` is intentional: it reproduces the five
        original copy-pasted phase guards verbatim, so failure handling is
        behaviour-identical. The full traceback is logged (``exc_info=True``) so
        the broad catch does not hide where an unexpected error originated.

        Preconditions: ``fn`` is a zero-argument callable implementing the phase;
        ``result`` is the in-progress :class:`AnalysisWorkflowResult`.
        Postconditions: returns ``(True, fn())`` when the phase succeeds. On any
        exception, sets ``result.failure_reason = f"{name} failed: {exc}"``, logs
        it, and returns ``(False, None)`` — the caller must then ``return result``.
        """
        try:
            return True, fn()
        except Exception as exc:
            result.failure_reason = f"{name} failed: {exc}"
            logger.error("Product Requirements Analysis: %s", result.failure_reason, exc_info=True)
            return False, None

    @staticmethod
    def _evaluate_sop_conditionals(
        question_def: Dict[str, Any],
        decisions_map: Dict[str, str],
    ) -> Optional[bool]:
        """Delegates to :func:`sop_engine.evaluate_sop_conditionals`."""
        return evaluate_sop_conditionals(question_def, decisions_map)

    def _extract_sop_decisions_from_spec(self, spec_content: str) -> List[SOPDecision]:
        """Delegates to :func:`sop_engine.extract_sop_decisions_from_spec`."""
        return extract_sop_decisions_from_spec(self._model, spec_content)

    def _generate_spec_aware_options(
        self,
        q_def: Dict[str, Any],
        spec_content: str,
        decisions_map: Dict[str, str],
    ) -> List[QuestionOption]:
        """Delegates to :func:`sop_engine.generate_spec_aware_options`."""
        return generate_spec_aware_options(self._model, q_def, spec_content, decisions_map)

    def _build_question_options(
        self,
        q_def: Dict[str, Any],
        spec_content: str,
        decisions_map: Dict[str, str],
    ) -> List[QuestionOption]:
        """Delegates to :func:`sop_engine.build_question_options`."""
        return build_question_options(self._model, q_def, spec_content, decisions_map)

    def _assess_sub_phase_gaps(
        self,
        sub_phase: SOPSubPhase,
        spec_content: str,
        all_decisions: List[SOPDecision],
        decisions_map: Dict[str, str],
    ) -> Tuple[bool, List[OpenQuestion]]:
        """Delegates to :func:`sop_engine.assess_sub_phase_gaps`."""
        return assess_sub_phase_gaps(
            self._model, sub_phase, spec_content, all_decisions, decisions_map
        )

    def _run_sop_phase1(
        self,
        spec_content: str,
        repo_path: Path,
        job_id: str,
        job_updater: Callable,
    ) -> Tuple[List[SOPDecision], str, List[AnsweredQuestion]]:
        """Delegates to :func:`sop_engine.run_sop_phase1`."""
        return run_sop_phase1(self._model, spec_content, repo_path, job_id, job_updater)

    def _run_sop_phase2_architecture(
        self,
        spec_content: str,
        sop_decisions: List[SOPDecision],
        repo_path: Path,
        job_id: str,
        job_updater: Callable,
    ) -> Tuple[ArchitectureAnalysisResult, str]:
        """Delegates to :func:`sop_engine.run_sop_phase2_architecture`."""
        return run_sop_phase2_architecture(
            self._model, spec_content, sop_decisions, repo_path, job_id, job_updater
        )

    def _build_architecture_approval_questions(
        self, arch_result: ArchitectureAnalysisResult
    ) -> List[OpenQuestion]:
        """Delegates to :func:`sop_engine.build_architecture_approval_questions`."""
        return build_architecture_approval_questions(arch_result)

    @staticmethod
    def _apply_architecture_approval(
        arch_result: ArchitectureAnalysisResult,
        answered: List[AnsweredQuestion],
    ) -> None:
        """Delegates to :func:`sop_engine.apply_architecture_approval`."""
        apply_architecture_approval(arch_result, answered)

    @staticmethod
    def _format_architecture_document(arch_result: ArchitectureAnalysisResult) -> str:
        """Delegates to :func:`sop_engine.format_architecture_document`."""
        return format_architecture_document(arch_result)

    def _run_context_constraints_discovery(self, spec_content: str) -> List[OpenQuestion]:
        """Delegates to :func:`context_discovery.run_context_constraints_discovery`."""
        return run_context_constraints_discovery(self._model, spec_content)

    def _inject_context_answers_into_spec(
        self,
        current_spec: str,
        answered_questions: List[AnsweredQuestion],
    ) -> str:
        """Delegates to :func:`context_discovery.inject_context_answers_into_spec`."""
        return inject_context_answers_into_spec(current_spec, answered_questions)

    def _parse_question_option(self, opt_data: Any, index: int) -> QuestionOption:
        """Delegates to :func:`question_processing.parse_question_option`."""
        return parse_question_option(opt_data, index)

    def _dedupe_questions_by_answer_similarity(
        self,
        open_questions: List[OpenQuestion],
        answered_questions: List[AnsweredQuestion],
    ) -> List[OpenQuestion]:
        """Delegates to :func:`question_processing.dedupe_questions_by_answer_similarity`."""
        return dedupe_questions_by_answer_similarity(open_questions, answered_questions)

    def _consolidate_open_questions(self, open_questions: List[OpenQuestion]) -> List[OpenQuestion]:
        """Delegates to :func:`question_processing.consolidate_open_questions`."""
        return consolidate_open_questions(self._model, open_questions)

    def _review_question_answer_alignment(
        self, open_questions: List[OpenQuestion]
    ) -> List[OpenQuestion]:
        """Delegates to :func:`question_processing.review_question_answer_alignment`."""
        return review_question_answer_alignment(self._model, open_questions)

    def _add_recommendations(
        self, open_questions: List[OpenQuestion], spec_content: str
    ) -> List[OpenQuestion]:
        """Delegates to :func:`question_processing.add_recommendations`."""
        return add_recommendations(self._model, open_questions, spec_content)

    def _communicate_with_user(
        self,
        job_id: Optional[str],
        open_questions: List[OpenQuestion],
        repo_path: Path,
        iteration: int,
    ) -> List[AnsweredQuestion]:
        """Delegates to :func:`user_communication.communicate_with_user`."""
        return communicate_with_user(job_id, open_questions, repo_path, iteration)

    def _wait_for_answers(self, job_id: str) -> bool:
        """Delegates to :func:`user_communication.wait_for_answers`."""
        return wait_for_answers(job_id)

    def _convert_to_pending_questions(
        self,
        open_questions: List[OpenQuestion],
    ) -> List[Dict[str, Any]]:
        """Delegates to :func:`user_communication.convert_to_pending_questions`."""
        return convert_to_pending_questions(open_questions)

    def _apply_all_defaults(
        self,
        open_questions: List[OpenQuestion],
    ) -> List[AnsweredQuestion]:
        """Delegates to :func:`user_communication.apply_all_defaults`."""
        return apply_all_defaults(open_questions)

    def _apply_answers(
        self,
        open_questions: List[OpenQuestion],
        submitted: List[Dict[str, Any]],
    ) -> List[AnsweredQuestion]:
        """Delegates to :func:`user_communication.apply_answers`."""
        return apply_answers(open_questions, submitted)

    def _get_default_option(self, q: OpenQuestion) -> Optional[QuestionOption]:
        """Delegates to :func:`user_communication.get_default_option`."""
        return get_default_option(q)

    def _update_spec(
        self,
        current_spec: str,
        answered_questions: List[AnsweredQuestion],
        repo_path: Path,
        version: int,
    ) -> str:
        """Delegates to :func:`spec_writing.update_spec`."""
        return update_spec(self._model, current_spec, answered_questions, repo_path, version)

    def _format_answered_questions(
        self,
        answered_questions: List[AnsweredQuestion],
    ) -> str:
        """Delegates to :func:`spec_writing.format_answered_questions`."""
        return format_answered_questions(answered_questions)

    def _build_specialist_collaboration_plan(
        self,
        cleaned_spec: str,
        answered_questions: List[AnsweredQuestion],
    ) -> str:
        """Delegates to :func:`spec_writing.build_specialist_collaboration_plan`."""
        return build_specialist_collaboration_plan(cleaned_spec, answered_questions)

    def _generate_prd_document(
        self,
        cleaned_spec: str,
        answered_questions: List[AnsweredQuestion],
    ) -> str:
        """Delegates to :func:`spec_writing.generate_prd_document`."""
        return generate_prd_document(self._model, self.llm, cleaned_spec, answered_questions)

    def _update_spec_from_duplicates(
        self,
        duplicate_questions: List[OpenQuestion],
        qa_history: str,
        current_spec: str,
        repo_path: Path,
        version: int,
    ) -> str:
        """Delegates to :func:`spec_writing.update_spec_from_duplicates`."""
        return update_spec_from_duplicates(
            self._model, duplicate_questions, qa_history, current_spec, repo_path, version
        )

    def _update_spec_for_consistency_and_clarity(
        self,
        current_spec: str,
        repo_path: Path,
        qa_history: str,
        all_answered_questions: List[AnsweredQuestion],
        version: int,
        consistency_loop: int,
    ) -> str:
        """Delegates to :func:`spec_writing.update_spec_for_consistency_and_clarity`."""
        return update_spec_for_consistency_and_clarity(
            self._model,
            current_spec,
            repo_path,
            qa_history,
            all_answered_questions,
            version,
            consistency_loop,
        )

    def _parse_qa_history_blocks(self, qa_history: str) -> List[Tuple[int, str, str, str]]:
        """Delegates to :func:`qa_history.parse_qa_history_blocks`."""
        return parse_qa_history_blocks(qa_history)

    def _is_same_decision(self, existing_question: str, new_question: str) -> bool:
        """Delegates to :func:`qa_history.is_same_decision`."""
        return is_same_decision(existing_question, new_question)

    def _record_answers(
        self,
        repo_path: Path,
        answered_questions: List[AnsweredQuestion],
        iteration: int,
    ) -> None:
        """Delegates to :func:`qa_history.record_answers`."""
        record_answers(repo_path, answered_questions, iteration)

    def _run_spec_cleanup(
        self,
        spec_content: str,
        repo_path: Path,
        on_chunk_progress: Optional[Callable[[int, int], None]] = None,
    ) -> SpecCleanupResult:
        """Delegates to :func:`spec_writing.run_spec_cleanup`."""
        return run_spec_cleanup(self.llm, spec_content, repo_path, on_chunk_progress)

    def _parse_spec_cleanup_response(
        self,
        raw: Any,
        fallback_spec: str,
    ) -> SpecCleanupResult:
        """Delegates to :func:`spec_writing.parse_spec_cleanup_response`."""
        return parse_spec_cleanup_response(raw, fallback_spec)
