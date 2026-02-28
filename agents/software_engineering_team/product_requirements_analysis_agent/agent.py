"""
Product Requirements Analysis Agent.

4-phase workflow: Spec Review → Communicate with User → Spec Update → Spec Cleanup.

This agent ensures the product specification is complete, consistent, and ready
for the Product Planning Agent.
"""

from __future__ import annotations

import json
import logging
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from .models import (
    AnalysisPhase,
    AnalysisWorkflowResult,
    AnsweredQuestion,
    OpenQuestion,
    QuestionOption,
    SpecCleanupResult,
    SpecReviewResult,
)
from .prompts import (
    SPEC_CLEANUP_CHUNK_PROMPT,
    SPEC_CLEANUP_PROMPT,
    SPEC_REVIEW_CHUNK_PROMPT,
    SPEC_REVIEW_PROMPT,
    SPEC_UPDATE_PROMPT,
)

if TYPE_CHECKING:
    from shared.llm import LLMClient

logger = logging.getLogger(__name__)

OPEN_QUESTIONS_POLL_INTERVAL = 5.0
MAX_ITERATIONS = 5
MAX_DECOMPOSITION_DEPTH = 20
MAX_ISSUES = 10
MAX_GAPS = 10


def _dedupe_items(items: List[str], similarity_threshold: float = 0.85) -> List[str]:
    """Remove near-duplicate items from a list based on string similarity.
    
    Uses SequenceMatcher to detect items that are variations of the same concern.
    Keeps the first occurrence (typically more complete) and discards similar ones.
    
    The threshold of 0.85 catches obvious duplicates (same sentence with minor word changes)
    while preserving items that follow similar patterns but address different topics.
    
    Args:
        items: List of string items to deduplicate.
        similarity_threshold: Items with similarity >= this value are considered duplicates (0.0-1.0).
    
    Returns:
        Deduplicated list preserving order.
    """
    if not items:
        return items
    
    unique: List[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        is_duplicate = False
        item_lower = item.lower()
        for existing in unique:
            ratio = SequenceMatcher(None, item_lower, existing.lower()).ratio()
            if ratio >= similarity_threshold:
                is_duplicate = True
                break
        if not is_duplicate:
            unique.append(item)
    return unique


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

    def __init__(self, llm_client: "LLMClient") -> None:
        if llm_client is None:
            raise ValueError("llm_client is required")
        self.llm = llm_client

    def run_workflow(
        self,
        *,
        spec_content: str,
        repo_path: Path,
        job_id: Optional[str] = None,
        job_updater: Optional[Callable[..., None]] = None,
        max_iterations: int = MAX_ITERATIONS,
        context_files: Optional[Dict[str, str]] = None,
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

            try:
                _update_job(status_text="Performing gap analysis on the specification")
                spec_review_result, current_spec = self._run_spec_review(
                    current_spec, repo_path, iteration
                )
                result.spec_review_result = spec_review_result
                if spec_review_result.open_questions:
                    _update_job(
                        status_text=f"Found {len(spec_review_result.issues)} issues, {len(spec_review_result.gaps)} gaps, {len(spec_review_result.open_questions)} questions"
                    )
            except Exception as exc:
                result.failure_reason = f"Spec review failed: {exc}"
                logger.error("Product Requirements Analysis: %s", result.failure_reason)
                return result

            logger.info(
                "Iteration %d: Found %d issues, %d gaps, %d open questions",
                iteration,
                len(spec_review_result.issues),
                len(spec_review_result.gaps),
                len(spec_review_result.open_questions),
            )

            if not spec_review_result.open_questions:
                logger.info("No open questions, proceeding to Spec Cleanup")
                break

            # Phase 2: Communicate with User
            result.current_phase = AnalysisPhase.COMMUNICATE
            _update_job(
                current_phase=AnalysisPhase.COMMUNICATE.value,
                progress=10 + (iteration - 1) * 15,
                message=f"Waiting for answers to {len(spec_review_result.open_questions)} question(s)",
                status_text=f"Waiting for your input on {len(spec_review_result.open_questions)} question(s)",
            )

            try:
                answered_questions = self._communicate_with_user(
                    job_id=job_id,
                    open_questions=spec_review_result.open_questions,
                    repo_path=repo_path,
                    iteration=iteration,
                )
            except Exception as exc:
                result.failure_reason = f"Communication failed: {exc}"
                logger.error("Product Requirements Analysis: %s", result.failure_reason)
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

            try:
                _update_job(status_text="Generating updated specification based on your answers")
                current_spec = self._update_spec(
                    current_spec=current_spec,
                    answered_questions=answered_questions,
                    repo_path=repo_path,
                    iteration=iteration,
                )
                _update_job(status_text="Specification updated successfully")
            except Exception as exc:
                result.failure_reason = f"Spec update failed: {exc}"
                logger.error("Product Requirements Analysis: %s", result.failure_reason)
                return result

        # Phase 4: Spec Cleanup
        result.current_phase = AnalysisPhase.SPEC_CLEANUP
        _update_job(
            current_phase=AnalysisPhase.SPEC_CLEANUP.value,
            progress=90,
            message="Validating and cleaning specification",
            status_text="Validating specification completeness and consistency",
        )

        try:
            _update_job(status_text="Running final validation and cleanup on specification")
            cleanup_result = self._run_spec_cleanup(current_spec, repo_path)
            result.spec_cleanup_result = cleanup_result
            result.final_spec_content = cleanup_result.cleaned_spec
        except Exception as exc:
            result.failure_reason = f"Spec cleanup failed: {exc}"
            logger.error("Product Requirements Analysis: %s", result.failure_reason)
            return result

        # Save validated spec
        validated_spec_path = repo_path / "plan" / "validated_spec.md"
        validated_spec_path.parent.mkdir(parents=True, exist_ok=True)
        validated_spec_path.write_text(cleanup_result.cleaned_spec, encoding="utf-8")
        result.validated_spec_path = str(validated_spec_path)

        result.success = True
        result.summary = (
            f"Analysis complete: {result.iterations} iteration(s), "
            f"{len(all_answered_questions)} questions answered. "
            f"Validated spec saved to {validated_spec_path.name}"
        )

        _update_job(
            current_phase=AnalysisPhase.SPEC_CLEANUP.value,
            progress=100,
            message=result.summary,
            status_text="Product analysis complete - specification validated",
        )

        elapsed = time.monotonic() - start_time
        logger.info(
            "Product Requirements Analysis Agent: WORKFLOW COMPLETE in %.1fs", elapsed
        )

        return result

    def _parse_json_with_recovery(
        self,
        prompt: str,
        phase_name: str = "LLM",
        decompose_fn: Optional[Callable[[str], List[str]]] = None,
        merge_fn: Optional[Callable[[List[Dict[str, Any]]], Dict[str, Any]]] = None,
        original_content: Optional[str] = None,
        chunk_prompt_template: Optional[str] = None,
        _depth: int = 0,
        _continuation_attempted: bool = False,
        _partial_responses: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Parse LLM JSON response with 3-step recovery flow.

        Recovery flow:
        1. On truncation: Attempt continuation via multi-turn conversation (5 cycles max)
        2. If continuation fails: Decompose task into smaller chunks (20 levels max)
        3. If decomposition fails: Write post-mortem and raise error

        Args:
            prompt: The prompt to send to the LLM
            phase_name: Name for logging purposes
            decompose_fn: Optional function to split content into chunks
            merge_fn: Optional function to merge results from chunks
            original_content: The original content being processed (for decomposition)
            chunk_prompt_template: Template for chunk prompts (must have {chunk_content})
            _depth: Internal recursion depth tracker
            _continuation_attempted: Whether continuation was already tried (internal)
            _partial_responses: Accumulated partial responses for post-mortem (internal)

        Returns:
            Parsed JSON dict.

        Raises:
            RuntimeError: If all recovery strategies fail.
        """
        from shared.llm import LLMTruncatedError, LLMJsonParseError
        from shared.continuation import ResponseContinuator, MAX_CONTINUATION_CYCLES
        from shared.post_mortem import write_post_mortem

        if _partial_responses is None:
            _partial_responses = []

        try:
            return self.llm.complete_json(prompt)
        except LLMTruncatedError as e:
            error_type = "LLMTruncatedError"
            _partial_responses.append(e.partial_content)

            if not _continuation_attempted:
                logger.info(
                    "PRA %s: Response truncated (%d chars). Step 1 -> Attempting continuation",
                    phase_name,
                    len(e.partial_content),
                )

                continued_content = self._attempt_continuation_for_json(
                    prompt=prompt,
                    partial_content=e.partial_content,
                    phase_name=phase_name,
                )

                if continued_content:
                    _partial_responses.append(continued_content)
                    try:
                        return self.llm._extract_json(continued_content)
                    except LLMJsonParseError:
                        logger.warning(
                            "PRA %s: Continuation produced content but JSON parse failed. "
                            "Step 2 -> Decomposing task",
                            phase_name,
                        )

                logger.warning(
                    "PRA %s: Continuation exhausted. Step 2 -> Decomposing task (depth %d/%d)",
                    phase_name,
                    _depth + 1,
                    MAX_DECOMPOSITION_DEPTH,
                )
            else:
                logger.warning(
                    "PRA %s: %s (%d chars). Decomposing task (depth %d/%d)",
                    phase_name,
                    error_type,
                    len(e.partial_content),
                    _depth + 1,
                    MAX_DECOMPOSITION_DEPTH,
                )

            result = self._decompose_and_process(
                prompt=prompt,
                phase_name=phase_name,
                decompose_fn=decompose_fn,
                merge_fn=merge_fn,
                original_content=original_content,
                chunk_prompt_template=chunk_prompt_template,
                _depth=_depth,
                _continuation_attempted=True,
                _partial_responses=_partial_responses,
            )
            if result:
                return result

            write_post_mortem(
                agent_name=f"PRA_{phase_name}",
                task_description=f"Product Requirements Analysis - {phase_name}",
                original_prompt=prompt,
                partial_responses=_partial_responses,
                continuation_attempts=MAX_CONTINUATION_CYCLES if not _continuation_attempted else 0,
                decomposition_depth=_depth,
                error=e,
            )

            raise RuntimeError(
                f"PRA {phase_name}: All recovery strategies exhausted. "
                f"Continuation cycles: {MAX_CONTINUATION_CYCLES}, Decomposition depth: {_depth}/{MAX_DECOMPOSITION_DEPTH}. "
                f"See post_mortems/POST_MORTEMS.md for details."
            ) from e

        except LLMJsonParseError as e:
            error_type = "LLMJsonParseError"
            _partial_responses.append(getattr(e, "response_preview", ""))
            logger.warning(
                "PRA %s: %s detected. Step 2 -> Decomposing task (depth %d/%d)",
                phase_name,
                error_type,
                _depth + 1,
                MAX_DECOMPOSITION_DEPTH,
            )

            result = self._decompose_and_process(
                prompt=prompt,
                phase_name=phase_name,
                decompose_fn=decompose_fn,
                merge_fn=merge_fn,
                original_content=original_content,
                chunk_prompt_template=chunk_prompt_template,
                _depth=_depth,
                _continuation_attempted=True,
                _partial_responses=_partial_responses,
            )
            if result:
                return result

            write_post_mortem(
                agent_name=f"PRA_{phase_name}",
                task_description=f"Product Requirements Analysis - {phase_name}",
                original_prompt=prompt,
                partial_responses=_partial_responses,
                continuation_attempts=0,
                decomposition_depth=_depth,
                error=e,
            )

            raise RuntimeError(
                f"PRA {phase_name}: Decomposition exhausted at depth {_depth}/{MAX_DECOMPOSITION_DEPTH}. "
                f"See post_mortems/POST_MORTEMS.md for details."
            ) from e

    def _attempt_continuation_for_json(
        self,
        prompt: str,
        partial_content: str,
        phase_name: str,
        max_cycles: int = 5,
    ) -> Optional[str]:
        """Attempt to continue a truncated JSON response.

        Args:
            prompt: The original prompt.
            partial_content: The truncated response content.
            phase_name: Phase name for logging and log file naming.
            max_cycles: Maximum continuation cycles.

        Returns:
            Complete content if successful, None if continuation fails.
        """
        from shared.continuation import ResponseContinuator, ContinuationResult

        system_message = (
            "You are a strict JSON generator. Respond with a single valid JSON object only, "
            "no explanatory text, no Markdown, no code fences."
        )

        try:
            continuator = ResponseContinuator(
                base_url=self.llm.base_url,
                model=self.llm.model,
                timeout=self.llm.timeout,
                max_cycles=max_cycles,
            )

            result: ContinuationResult = continuator.attempt_continuation(
                original_prompt=prompt,
                partial_content=partial_content,
                system_prompt=system_message,
                json_mode=True,
                task_id=f"PRA_{phase_name}",
            )

            if result.success:
                logger.info(
                    "PRA %s: Continuation succeeded after %d cycles (%d chars total)",
                    phase_name,
                    result.cycles_used,
                    len(result.content),
                )
                return result.content

            logger.warning(
                "PRA %s: Continuation exhausted after %d cycles (%d chars accumulated)",
                phase_name,
                result.cycles_used,
                len(result.content),
            )
            return None

        except Exception as e:
            logger.warning(
                "PRA %s: Continuation failed with error: %s",
                phase_name,
                str(e)[:100],
            )
            return None

    def _decompose_and_process(
        self,
        prompt: str,
        phase_name: str,
        decompose_fn: Optional[Callable[[str], List[str]]],
        merge_fn: Optional[Callable[[List[Dict[str, Any]]], Dict[str, Any]]],
        original_content: Optional[str],
        chunk_prompt_template: Optional[str],
        _depth: int,
        _continuation_attempted: bool = False,
        _partial_responses: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Decompose content into chunks and process each recursively.

        Raises:
            RuntimeError: If max decomposition depth is reached.
        """
        if decompose_fn is None or merge_fn is None:
            logger.warning(
                "PRA %s: Decomposition not available (no decompose/merge functions)",
                phase_name,
            )
            return {}

        if not original_content:
            logger.warning(
                "PRA %s: Decomposition not possible (no content to decompose)",
                phase_name,
            )
            return {}

        if _depth >= MAX_DECOMPOSITION_DEPTH:
            raise RuntimeError(
                f"PRA {phase_name}: Maximum decomposition depth ({MAX_DECOMPOSITION_DEPTH}) "
                "reached without successful response"
            )

        chunks = decompose_fn(original_content)
        if len(chunks) <= 1:
            logger.warning(
                "PRA %s: Cannot decompose further (only %d chunk)",
                phase_name,
                len(chunks),
            )
            return {}

        logger.info(
            "PRA %s: Decomposing into %d chunks (depth %d/%d)",
            phase_name,
            len(chunks),
            _depth + 1,
            MAX_DECOMPOSITION_DEPTH,
        )

        results: List[Dict[str, Any]] = []
        for i, chunk in enumerate(chunks):
            logger.debug(
                "PRA %s: Processing chunk %d/%d (%d chars)",
                phase_name,
                i + 1,
                len(chunks),
                len(chunk),
            )

            if chunk_prompt_template:
                chunk_prompt = chunk_prompt_template.format(chunk_content=chunk)
            else:
                chunk_prompt = self._create_generic_chunk_prompt(prompt, chunk)

            try:
                chunk_result = self._parse_json_with_recovery(
                    prompt=chunk_prompt,
                    phase_name=f"{phase_name}_chunk{i + 1}",
                    decompose_fn=decompose_fn,
                    merge_fn=merge_fn,
                    original_content=chunk,
                    chunk_prompt_template=chunk_prompt_template,
                    _depth=_depth + 1,
                    _continuation_attempted=_continuation_attempted,
                    _partial_responses=_partial_responses,
                )
                if chunk_result:
                    results.append(chunk_result)
            except RuntimeError as e:
                logger.warning(
                    "PRA %s: Chunk %d/%d failed: %s",
                    phase_name,
                    i + 1,
                    len(chunks),
                    str(e)[:100],
                )

        if results:
            merged = merge_fn(results)
            logger.info(
                "PRA %s: Merged %d chunk results successfully",
                phase_name,
                len(results),
            )
            return merged

        logger.warning("PRA %s: Chunk processing produced no valid results", phase_name)
        return {}

    def _create_generic_chunk_prompt(self, original_prompt: str, chunk: str) -> str:
        """Create a generic chunk prompt based on the original prompt."""
        # Extract the JSON schema hint from the original prompt if present
        schema_match = re.search(r"(\{[^}]*\"[^\"]+\"[^}]*\})", original_prompt)
        schema_hint = schema_match.group(1) if schema_match else ""

        return f"""Process this portion of the content and return JSON with the same structure.

CONTENT CHUNK:
---
{chunk}
---

{f"Expected JSON structure: {schema_hint}" if schema_hint else "Return JSON with the relevant fields found in this chunk."}

Keep your response concise. Only include findings from THIS chunk.
"""

    def _decompose_spec_for_review(
        self, spec_content: str, chunk_size: int = 4000
    ) -> List[str]:
        """Split spec into reviewable sections by markdown headers or fixed chunks.

        Args:
            spec_content: The full specification content
            chunk_size: Maximum size for fixed chunks (fallback)

        Returns:
            List of spec sections/chunks
        """
        # Try splitting by ## headers first
        sections = re.split(r"\n(?=## )", spec_content)
        if len(sections) > 1:
            return [s.strip() for s in sections if s.strip()]

        # Try splitting by # headers
        sections = re.split(r"\n(?=# )", spec_content)
        if len(sections) > 1:
            return [s.strip() for s in sections if s.strip()]

        # Fall back to fixed-size chunks
        chunks = []
        for i in range(0, len(spec_content), chunk_size):
            chunk = spec_content[i : i + chunk_size]
            if chunk.strip():
                chunks.append(chunk)
        return chunks if chunks else [spec_content]

    def _merge_spec_review_results(
        self, results: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Combine issues, gaps, and questions from multiple chunk reviews.

        Args:
            results: List of parsed JSON dicts from chunk reviews

        Returns:
            Merged dict with concatenated lists
        """
        merged: Dict[str, Any] = {
            "issues": [],
            "gaps": [],
            "open_questions": [],
            "summary": "",
        }

        summaries = []
        for r in results:
            if isinstance(r.get("issues"), list):
                merged["issues"].extend(r["issues"])
            if isinstance(r.get("gaps"), list):
                merged["gaps"].extend(r["gaps"])
            if isinstance(r.get("open_questions"), list):
                merged["open_questions"].extend(r["open_questions"])
            if r.get("summary"):
                summaries.append(str(r["summary"]))

        merged["summary"] = (
            f"Reviewed {len(results)} sections. " + " ".join(summaries[:3])
        )
        return merged

    def _merge_spec_cleanup_results(
        self, results: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Combine cleanup results from multiple chunks.

        Args:
            results: List of parsed JSON dicts from chunk cleanup

        Returns:
            Merged dict with combined validation issues and cleaned spec
        """
        merged: Dict[str, Any] = {
            "is_valid": True,
            "validation_issues": [],
            "cleaned_spec": "",
            "summary": "",
        }

        cleaned_parts = []
        for r in results:
            if r.get("is_valid") is False:
                merged["is_valid"] = False
            if isinstance(r.get("validation_issues"), list):
                merged["validation_issues"].extend(r["validation_issues"])
            if r.get("cleaned_spec"):
                cleaned_parts.append(str(r["cleaned_spec"]))

        merged["cleaned_spec"] = "\n\n".join(cleaned_parts)
        merged["summary"] = f"Cleanup completed for {len(results)} sections"
        return merged

    def _format_context_for_review(self) -> str:
        """Format context files for inclusion in the spec review prompt."""
        if not self._context_files:
            return ""
        
        from spec_parser import format_context_for_prompt
        formatted = format_context_for_prompt(self._context_files)
        
        if not formatted:
            return ""
        
        return f"""

## Additional Context Files

The following additional files were provided in the project folder. Review these alongside the main specification to understand the full context:

{formatted}

---
"""

    def _run_spec_review(
        self,
        spec_content: str,
        repo_path: Path,
        iteration: int = 1,
    ) -> tuple[SpecReviewResult, str]:
        """Run the Spec Review phase to identify gaps and questions.
        
        Args:
            spec_content: Current specification content.
            repo_path: Path to the repository.
            iteration: Current iteration number for versioning.
            
        Returns:
            Tuple of (SpecReviewResult, updated_spec_content). The spec may be
            updated if duplicate questions were found and clarified.
        """
        # Read previously answered questions to avoid asking duplicates
        qa_history = self._read_qa_history(repo_path)

        # Build the full content including context files
        context_section = self._format_context_for_review()
        full_spec_content = spec_content
        if context_section:
            full_spec_content = spec_content + context_section
            logger.info(
                "Spec review: Including %d context files in review",
                len(self._context_files),
            )

        if qa_history:
            prompt = SPEC_REVIEW_PROMPT.format(spec_content=full_spec_content[:20000])
            prompt += f"""

IMPORTANT: The following questions have ALREADY been answered. Do NOT ask these questions again or any variations of them. Only ask NEW questions about topics NOT covered below:

Previously Answered Questions:
---
{qa_history}
---
"""
        else:
            prompt = SPEC_REVIEW_PROMPT.format(spec_content=full_spec_content[:20000])

        raw = self._parse_json_with_recovery(
            prompt=prompt,
            phase_name="spec_review",
            decompose_fn=self._decompose_spec_for_review,
            merge_fn=self._merge_spec_review_results,
            original_content=spec_content,
            chunk_prompt_template=SPEC_REVIEW_CHUNK_PROMPT,
        )

        if not raw:
            # All recovery failed - return a result indicating retry is needed
            logger.warning(
                "PRA spec_review: No JSON recovered, will retry in next iteration"
            )
            return (
                SpecReviewResult(
                    summary="Spec review JSON parsing failed - will retry",
                    issues=["JSON parsing failed - response may have been truncated"],
                    gaps=[],
                    open_questions=[],
                ),
                spec_content,
            )

        result = self._parse_spec_review_response(raw)
        updated_spec = spec_content

        # Filter out any questions that are duplicates of previously answered ones
        if qa_history and result.open_questions:
            filtered, duplicates = self._filter_duplicate_questions(
                result.open_questions, qa_history
            )
            result.open_questions = filtered
            
            # If duplicates found, update spec with their existing answers
            # This fills gaps that caused questions to be re-asked
            if duplicates:
                logger.info(
                    "Found %d duplicate questions - clarifying spec with existing answers",
                    len(duplicates),
                )
                updated_spec = self._update_spec_from_duplicates(
                    duplicates, qa_history, spec_content, repo_path, iteration
                )

        return result, updated_spec

    def _read_qa_history(self, repo_path: Path) -> str:
        """Read the QA history file if it exists."""
        qa_file = repo_path / "plan" / "qa_history.md"
        if qa_file.exists():
            try:
                return qa_file.read_text(encoding="utf-8")
            except Exception as e:
                logger.warning("Failed to read qa_history.md: %s", e)
        return ""

    def _filter_duplicate_questions(
        self,
        new_questions: List[OpenQuestion],
        qa_history: str,
    ) -> tuple[List[OpenQuestion], List[OpenQuestion]]:
        """Filter out questions that appear to be duplicates of answered ones.
        
        Returns:
            Tuple of (filtered_questions, duplicate_questions).
            - filtered_questions: Questions that are NOT duplicates (should be asked)
            - duplicate_questions: Questions that ARE duplicates (already answered)
        """
        qa_history_lower = qa_history.lower()
        filtered = []
        duplicates = []
        
        for q in new_questions:
            q_text_lower = q.question_text.lower()
            
            # Check for exact or near-exact matches in qa_history
            # Extract key phrases from the question (simplified heuristic)
            key_words = [w for w in q_text_lower.split() if len(w) > 4]
            
            # If most key words appear in qa_history, likely a duplicate
            if key_words:
                matches = sum(1 for w in key_words if w in qa_history_lower)
                match_ratio = matches / len(key_words)
                
                if match_ratio > 0.6:
                    logger.debug(
                        "Filtering duplicate question (%.0f%% match): %s",
                        match_ratio * 100,
                        q.question_text[:60],
                    )
                    duplicates.append(q)
                    continue
            
            filtered.append(q)
        
        if duplicates:
            logger.info(
                "Filtered %d duplicate questions based on qa_history",
                len(duplicates),
            )
        
        return filtered, duplicates

    def _extract_answer_from_qa_history(
        self,
        question: OpenQuestion,
        qa_history: str,
    ) -> Optional[AnsweredQuestion]:
        """Extract a previously recorded answer from qa_history.md for a duplicate question.
        
        Parses the qa_history.md markdown format to find the best matching Q&A pair.
        
        Args:
            question: The duplicate question to find an answer for.
            qa_history: Raw content of qa_history.md file.
            
        Returns:
            AnsweredQuestion if a matching answer was found, None otherwise.
        """
        import re
        
        if not qa_history:
            return None
        
        q_text_lower = question.question_text.lower()
        key_words = [w for w in q_text_lower.split() if len(w) > 4]
        
        if not key_words:
            return None
        
        # Parse qa_history.md sections - format is:
        # ### Question text
        # **Answer:** Answer text
        # **Rationale:** Optional rationale
        # *(Auto-answered with X% confidence)* or *(Default applied)*
        
        # Split into Q&A blocks by "### " headers
        blocks = re.split(r'\n###\s+', qa_history)
        
        best_match: Optional[tuple[float, str, str, str]] = None  # (score, question, answer, rationale)
        
        for block in blocks[1:]:  # Skip first block (header)
            lines = block.strip().split('\n')
            if not lines:
                continue
            
            recorded_question = lines[0].strip()
            recorded_question_lower = recorded_question.lower()
            
            # Calculate match score
            matches = sum(1 for w in key_words if w in recorded_question_lower)
            match_ratio = matches / len(key_words) if key_words else 0
            
            if match_ratio > 0.5:  # Good enough match
                # Extract answer from block
                answer = ""
                rationale = ""
                
                for line in lines[1:]:
                    if line.startswith("**Answer:**"):
                        answer = line.replace("**Answer:**", "").strip()
                    elif line.startswith("**Rationale:**"):
                        rationale = line.replace("**Rationale:**", "").strip()
                
                if answer and (best_match is None or match_ratio > best_match[0]):
                    best_match = (match_ratio, recorded_question, answer, rationale)
        
        if best_match:
            _, matched_q, answer, rationale = best_match
            logger.debug(
                "Extracted answer for duplicate question: '%s' -> '%s'",
                question.question_text[:40],
                answer[:40],
            )
            return AnsweredQuestion(
                question_id=question.id,
                question_text=question.question_text,
                selected_option_id="from_history",
                selected_answer=answer,
                was_auto_answered=False,
                was_default=False,
                rationale=rationale or f"Previously answered (matched: {matched_q[:50]})",
                confidence=0.9,  # High confidence since it was user-answered before
            )
        
        return None

    def _parse_spec_review_response(self, raw: Any) -> SpecReviewResult:
        """Parse LLM response into SpecReviewResult.
        
        Applies deduplication and enforces max limits on issues/gaps to prevent
        runaway repetitive output from the LLM.
        """
        if not isinstance(raw, dict):
            return SpecReviewResult(summary="Spec review completed (no structured output)")

        raw_issues = raw.get("issues", [])
        raw_gaps = raw.get("gaps", [])
        raw_questions = raw.get("open_questions", [])

        # Deduplicate and limit issues/gaps to prevent repetitive LLM output
        issues = list(raw_issues) if isinstance(raw_issues, list) else []
        gaps = list(raw_gaps) if isinstance(raw_gaps, list) else []
        
        original_issue_count = len(issues)
        original_gap_count = len(gaps)
        
        issues = _dedupe_items(issues)[:MAX_ISSUES]
        gaps = _dedupe_items(gaps)[:MAX_GAPS]
        
        if len(issues) < original_issue_count or len(gaps) < original_gap_count:
            logger.info(
                "Deduplicated spec review results: issues %d->%d, gaps %d->%d",
                original_issue_count, len(issues),
                original_gap_count, len(gaps),
            )

        open_questions = []
        if isinstance(raw_questions, list):
            for i, q in enumerate(raw_questions):
                open_questions.append(self._parse_open_question(q, i))

        return SpecReviewResult(
            issues=issues,
            gaps=gaps,
            open_questions=open_questions,
            summary=str(raw.get("summary", "") or "Spec review complete"),
        )

    def _parse_open_question(self, q_data: Any, index: int) -> OpenQuestion:
        """Parse a single open question from LLM output."""
        if isinstance(q_data, dict):
            raw_options = q_data.get("options", [])
            options = []
            for i, opt in enumerate(raw_options):
                options.append(self._parse_question_option(opt, i))

            if options and not any(opt.is_default for opt in options):
                sorted_opts = sorted(options, key=lambda o: o.confidence, reverse=True)
                sorted_opts[0] = QuestionOption(
                    id=sorted_opts[0].id,
                    label=sorted_opts[0].label,
                    is_default=True,
                    rationale=sorted_opts[0].rationale,
                    confidence=sorted_opts[0].confidence,
                )
                options = sorted_opts

            return OpenQuestion(
                id=str(q_data.get("id", f"q{index}")),
                question_text=str(q_data.get("question_text", "")),
                context=str(q_data.get("context", "")),
                options=options,
                allow_multiple=bool(q_data.get("allow_multiple", False)),
                source="spec_review",
                category=str(q_data.get("category", "general")),
                priority=str(q_data.get("priority", "medium")),
            )

        return OpenQuestion(
            id=f"q{index}",
            question_text=str(q_data),
            context="This question was identified during spec review.",
            options=[
                QuestionOption(
                    id="opt1", label="Yes", is_default=True, rationale="", confidence=0.5
                ),
                QuestionOption(
                    id="opt2", label="No", is_default=False, rationale="", confidence=0.5
                ),
            ],
            allow_multiple=False,
            source="spec_review",
        )

    def _parse_question_option(self, opt_data: Any, index: int) -> QuestionOption:
        """Parse a single question option from LLM output."""
        if isinstance(opt_data, dict):
            return QuestionOption(
                id=str(opt_data.get("id", f"opt{index}")),
                label=str(opt_data.get("label", "")),
                is_default=bool(opt_data.get("is_default", False)),
                rationale=str(opt_data.get("rationale", "")),
                confidence=float(opt_data.get("confidence", 0.5)),
            )
        return QuestionOption(
            id=f"opt{index}",
            label=str(opt_data),
            is_default=index == 0,
            rationale="",
            confidence=0.5,
        )

    def _communicate_with_user(
        self,
        job_id: Optional[str],
        open_questions: List[OpenQuestion],
        repo_path: Path,
        iteration: int,
    ) -> List[AnsweredQuestion]:
        """Send questions to user and wait for response."""
        if not job_id:
            raise RuntimeError(
                "No job_id provided - cannot communicate with user for answers. "
                "A job_id is required to collect user input."
            )

        from shared.job_store import (
            add_pending_questions,
            get_submitted_answers,
            is_waiting_for_answers,
            update_job,
        )

        pending = self._convert_to_pending_questions(open_questions)
        add_pending_questions(job_id, pending)

        update_job(
            job_id,
            waiting_for_answers=True,
            message=f"Waiting for answers to {len(open_questions)} question(s)",
        )

        logger.info(
            "Communicate with user: Sent %d questions, waiting for response",
            len(open_questions),
        )

        if not self._wait_for_answers(job_id):
            raise RuntimeError("Job was cancelled or failed while waiting for user answers")

        submitted = get_submitted_answers(job_id)
        answered = self._apply_answers(open_questions, submitted)

        update_job(job_id, waiting_for_answers=False)
        self._record_answers(repo_path, answered, iteration)

        return answered

    def _wait_for_answers(self, job_id: str) -> bool:
        """Wait indefinitely for user to submit answers."""
        from shared.job_store import get_job, is_waiting_for_answers

        while True:
            if not is_waiting_for_answers(job_id):
                return True

            job_data = get_job(job_id)
            if job_data and job_data.get("status") in ("failed", "completed", "cancelled"):
                return False

            time.sleep(OPEN_QUESTIONS_POLL_INTERVAL)

    def _convert_to_pending_questions(
        self,
        open_questions: List[OpenQuestion],
    ) -> List[Dict[str, Any]]:
        """Convert OpenQuestion models to pending question dicts for job store."""
        pending = []
        for q in open_questions:
            options = [
                {
                    "id": opt.id,
                    "label": opt.label,
                    "is_default": opt.is_default,
                    "rationale": opt.rationale,
                    "confidence": opt.confidence,
                }
                for opt in q.options
            ]
            if not options:
                options = [{"id": "other", "label": "Provide answer in text field"}]

            pending.append(
                {
                    "id": q.id,
                    "question_text": q.question_text,
                    "context": q.context,
                    "options": options,
                    "allow_multiple": q.allow_multiple,
                    "required": True,
                    "source": q.source,
                    "category": q.category,
                    "priority": q.priority,
                }
            )
        return pending

    def _apply_all_defaults(
        self,
        open_questions: List[OpenQuestion],
    ) -> List[AnsweredQuestion]:
        """Apply default answers to all questions."""
        answered = []
        for q in open_questions:
            default_opt = self._get_default_option(q)
            answered.append(
                AnsweredQuestion(
                    question_id=q.id,
                    question_text=q.question_text,
                    selected_option_id=default_opt.id if default_opt else "unknown",
                    selected_answer=default_opt.label
                    if default_opt
                    else "No default available",
                    was_default=True,
                    rationale=default_opt.rationale if default_opt else "",
                    confidence=default_opt.confidence if default_opt else 0.0,
                )
            )
        return answered

    def _apply_answers(
        self,
        open_questions: List[OpenQuestion],
        submitted: List[Dict[str, Any]],
    ) -> List[AnsweredQuestion]:
        """Merge submitted answers with defaults for unanswered questions."""
        submitted_by_id = {s.get("question_id"): s for s in submitted}
        answered = []

        for q in open_questions:
            sub = submitted_by_id.get(q.id)
            if sub:
                other_text = sub.get("other_text") or ""
                was_auto = sub.get("was_auto_answered", False)
                
                # Handle multi-select questions
                selected_ids = sub.get("selected_option_ids", [])
                selected_id = sub.get("selected_option_id", "")
                
                if selected_ids:
                    # Multi-select: build combined answer from all selected options
                    selected_labels = []
                    for opt_id in selected_ids:
                        if opt_id == "other" and other_text:
                            selected_labels.append(other_text)
                        else:
                            opt = next((o for o in q.options if o.id == opt_id), None)
                            if opt:
                                selected_labels.append(opt.label)
                    selected_answer = "; ".join(selected_labels) if selected_labels else "Unknown"
                    # Use first selected ID for backward compatibility
                    primary_selected_id = selected_ids[0] if selected_ids else ""
                else:
                    # Single-select: use the single selected option
                    selected_ids = [selected_id] if selected_id else []
                    primary_selected_id = selected_id
                    if selected_id == "other" and other_text:
                        selected_answer = other_text
                    else:
                        opt = next((o for o in q.options if o.id == selected_id), None)
                        selected_answer = opt.label if opt else other_text or "Unknown"

                answered.append(
                    AnsweredQuestion(
                        question_id=q.id,
                        question_text=q.question_text,
                        selected_option_id=primary_selected_id,
                        selected_option_ids=selected_ids,
                        selected_answer=selected_answer,
                        was_auto_answered=was_auto,
                        was_default=False,
                        rationale=sub.get("rationale") or "",
                        confidence=float(sub.get("confidence") or 0.0),
                        other_text=other_text,
                    )
                )
            else:
                default_opt = self._get_default_option(q)
                answered.append(
                    AnsweredQuestion(
                        question_id=q.id,
                        question_text=q.question_text,
                        selected_option_id=default_opt.id if default_opt else "unknown",
                        selected_option_ids=[default_opt.id] if default_opt else [],
                        selected_answer=default_opt.label
                        if default_opt
                        else "No default available",
                        was_default=True,
                        rationale=default_opt.rationale if default_opt else "",
                        confidence=default_opt.confidence if default_opt else 0.0,
                    )
                )

        return answered

    def _get_default_option(self, q: OpenQuestion) -> Optional[QuestionOption]:
        """Get the default option for a question."""
        default = next((opt for opt in q.options if opt.is_default), None)
        if default:
            return default

        if q.options:
            sorted_by_confidence = sorted(
                q.options, key=lambda o: o.confidence, reverse=True
            )
            return sorted_by_confidence[0]

        return None

    def _update_spec(
        self,
        current_spec: str,
        answered_questions: List[AnsweredQuestion],
        repo_path: Path,
        iteration: int,
    ) -> str:
        """Update the spec with answered questions."""
        answered_text = self._format_answered_questions(answered_questions)

        prompt = SPEC_UPDATE_PROMPT.format(
            spec_content=current_spec,
            answered_questions=answered_text,
        )

        try:
            updated_spec = self.llm.complete_text(prompt)
        except Exception as e:
            logger.error("Failed to update spec with LLM: %s", e)
            return current_spec

        plan_dir = repo_path / "plan"
        plan_dir.mkdir(parents=True, exist_ok=True)

        spec_file = plan_dir / f"updated_spec_v{iteration}.md"
        spec_file.write_text(updated_spec, encoding="utf-8")
        logger.info("Saved updated spec to %s", spec_file)

        latest_file = plan_dir / "updated_spec.md"
        latest_file.write_text(updated_spec, encoding="utf-8")

        return updated_spec

    def _format_answered_questions(
        self,
        answered_questions: List[AnsweredQuestion],
    ) -> str:
        """Format answered questions for the LLM prompt."""
        lines = []
        for aq in answered_questions:
            lines.append(f"Q: {aq.question_text}")
            lines.append(f"A: {aq.selected_answer}")
            if aq.rationale:
                lines.append(f"Rationale: {aq.rationale}")
            if aq.was_auto_answered:
                lines.append(f"(Auto-answered with {aq.confidence:.0%} confidence)")
            elif aq.was_default:
                lines.append("(Default applied)")
            lines.append("")
        return "\n".join(lines)

    def _update_spec_from_duplicates(
        self,
        duplicate_questions: List[OpenQuestion],
        qa_history: str,
        current_spec: str,
        repo_path: Path,
        iteration: int,
    ) -> str:
        """Update spec using answers from qa_history for duplicate questions.
        
        When a question is re-asked but was previously answered, this indicates
        the spec wasn't updated clearly enough. This method extracts the existing
        answers and re-applies them with emphasis on clarity.
        
        Args:
            duplicate_questions: Questions that were filtered as duplicates.
            qa_history: Raw content of qa_history.md file.
            current_spec: Current specification content.
            repo_path: Path to the repository.
            iteration: Current iteration number.
            
        Returns:
            Updated specification content.
        """
        from .prompts import SPEC_CLARIFICATION_PROMPT
        
        # Extract answers from qa_history for each duplicate
        extracted_answers: List[AnsweredQuestion] = []
        for q in duplicate_questions:
            answer = self._extract_answer_from_qa_history(q, qa_history)
            if answer:
                extracted_answers.append(answer)
        
        if not extracted_answers:
            logger.debug("No answers extracted from qa_history for duplicates")
            return current_spec
        
        logger.info(
            "Clarifying spec with %d previously answered questions that were re-asked",
            len(extracted_answers),
        )
        
        # Format the Q&A pairs for the clarification prompt
        qa_pairs = self._format_answered_questions(extracted_answers)
        
        prompt = SPEC_CLARIFICATION_PROMPT.format(
            spec_content=current_spec,
            duplicate_qa_pairs=qa_pairs,
        )
        
        try:
            clarified_spec = self.llm.complete_text(prompt)
        except Exception as e:
            logger.error("Failed to clarify spec with LLM: %s", e)
            return current_spec
        
        # Save the clarified spec
        plan_dir = repo_path / "plan"
        plan_dir.mkdir(parents=True, exist_ok=True)
        
        clarification_file = plan_dir / f"spec_clarification_v{iteration}.md"
        clarification_file.write_text(clarified_spec, encoding="utf-8")
        logger.info("Saved clarified spec to %s", clarification_file)
        
        # Also update the latest spec
        latest_file = plan_dir / "updated_spec.md"
        latest_file.write_text(clarified_spec, encoding="utf-8")
        
        return clarified_spec

    def _record_answers(
        self,
        repo_path: Path,
        answered_questions: List[AnsweredQuestion],
        iteration: int,
    ) -> None:
        """Save answered questions to /plan/qa_history.md."""
        plan_dir = repo_path / "plan"
        plan_dir.mkdir(parents=True, exist_ok=True)

        qa_file = plan_dir / "qa_history.md"

        content = f"\n## Iteration {iteration}\n\n"
        for aq in answered_questions:
            content += f"### {aq.question_text}\n"
            content += f"**Answer:** {aq.selected_answer}\n"
            if aq.rationale:
                content += f"**Rationale:** {aq.rationale}\n"
            if aq.was_auto_answered:
                content += f"*Auto-answered with {aq.confidence:.0%} confidence*\n"
            elif aq.was_default:
                content += "*(Default applied)*\n"
            if aq.other_text:
                content += f"*Custom text:* {aq.other_text}\n"
            content += "\n"

        mode = "a" if qa_file.exists() else "w"
        if mode == "w":
            content = (
                "# Q&A History\n\n"
                "This file records all questions and answers from Product Requirements Analysis.\n"
                + content
            )

        with open(qa_file, mode, encoding="utf-8") as f:
            f.write(content)

        logger.info("Recorded %d answers to %s", len(answered_questions), qa_file)

    def _run_spec_cleanup(
        self,
        spec_content: str,
        repo_path: Path,
    ) -> SpecCleanupResult:
        """Run the Spec Cleanup phase to validate and clean the spec."""
        prompt = SPEC_CLEANUP_PROMPT.format(spec_content=spec_content)

        raw = self._parse_json_with_recovery(
            prompt=prompt,
            phase_name="spec_cleanup",
            decompose_fn=self._decompose_spec_for_review,
            merge_fn=self._merge_spec_cleanup_results,
            original_content=spec_content,
            chunk_prompt_template=SPEC_CLEANUP_CHUNK_PROMPT,
        )

        if not raw:
            # All recovery failed - return the original spec as valid
            logger.warning(
                "PRA spec_cleanup: No JSON recovered, returning original spec"
            )
            return SpecCleanupResult(
                is_valid=True,
                cleaned_spec=spec_content,
                summary="Spec cleanup skipped - JSON parsing failed",
            )

        return self._parse_spec_cleanup_response(raw, spec_content)

    def _parse_spec_cleanup_response(
        self,
        raw: Any,
        fallback_spec: str,
    ) -> SpecCleanupResult:
        """Parse LLM response into SpecCleanupResult."""
        if not isinstance(raw, dict):
            return SpecCleanupResult(
                is_valid=True,
                cleaned_spec=fallback_spec,
                summary="Spec cleanup completed (no structured output)",
            )

        return SpecCleanupResult(
            is_valid=bool(raw.get("is_valid", True)),
            validation_issues=list(raw.get("validation_issues", []))
            if isinstance(raw.get("validation_issues"), list)
            else [],
            cleaned_spec=str(raw.get("cleaned_spec", fallback_spec)),
            summary=str(raw.get("summary", "Spec cleanup complete")),
        )
