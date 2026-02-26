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
    SPEC_CLEANUP_PROMPT,
    SPEC_REVIEW_PROMPT,
    SPEC_UPDATE_PROMPT,
)

if TYPE_CHECKING:
    from shared.llm import LLMClient

logger = logging.getLogger(__name__)

OPEN_QUESTIONS_POLL_INTERVAL = 5.0
MAX_ITERATIONS = 5
JSON_RECOVERY_MAX_RETRIES = 2


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
    ) -> AnalysisWorkflowResult:
        """
        Execute the full Product Requirements Analysis workflow.

        Args:
            spec_content: The initial specification content
            repo_path: Path to the repository for storing artifacts
            job_id: Job ID for question tracking (required for user communication)
            job_updater: Callback to update job status
            max_iterations: Maximum number of spec review cycles

        Returns:
            AnalysisWorkflowResult with validated spec and answered questions
        """
        start_time = time.monotonic()
        result = AnalysisWorkflowResult()
        current_spec = spec_content
        all_answered_questions: List[AnsweredQuestion] = []
        iteration = 0

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
                spec_review_result = self._run_spec_review(current_spec, repo_path)
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
        max_retries: int = JSON_RECOVERY_MAX_RETRIES,
    ) -> Dict[str, Any]:
        """Parse LLM JSON response with multi-stage recovery.

        Recovery stages:
        1. llm.complete_json() - uses LLMClient's built-in continuation retry
        2. Regex extraction - find {...} in raw response (handles truncation)
        3. Retry with simplified prompt (if max_retries > 0)

        Returns empty dict only if all recovery attempts fail.
        """
        last_error: Optional[Exception] = None

        for attempt in range(max_retries + 1):
            try:
                return self.llm.complete_json(prompt)
            except Exception as e:
                last_error = e
                logger.warning(
                    "PRA %s: JSON parse failed (attempt %d/%d): %s",
                    phase_name,
                    attempt + 1,
                    max_retries + 1,
                    str(e)[:200],
                )

                # Try regex extraction from the error message or raw response
                raw_preview = getattr(e, "response_preview", "") or str(e)
                extracted = self._extract_json_fallback(raw_preview)
                if extracted:
                    logger.info(
                        "PRA %s: Recovered JSON via regex extraction", phase_name
                    )
                    return extracted

        logger.error(
            "PRA %s: All JSON recovery attempts failed: %s", phase_name, last_error
        )
        return {}

    def _extract_json_fallback(self, raw: str) -> Dict[str, Any]:
        """Extract JSON object from raw text using regex."""
        # Find the outermost JSON object
        match = re.search(r"\{[\s\S]*\}", raw)
        if match:
            json_str = match.group()
            try:
                return json.loads(json_str)
            except json.JSONDecodeError:
                # Try cleaning trailing commas
                cleaned = re.sub(r",\s*([}\]])", r"\1", json_str)
                try:
                    return json.loads(cleaned)
                except json.JSONDecodeError:
                    # Try truncating to last complete field
                    truncated = self._truncate_to_valid_json(cleaned)
                    if truncated:
                        try:
                            return json.loads(truncated)
                        except json.JSONDecodeError:
                            pass
        return {}

    def _truncate_to_valid_json(self, json_str: str) -> str:
        """Try to truncate a malformed JSON string to make it valid.

        Useful for responses that were cut off mid-stream.
        """
        # Find where arrays/objects might have been truncated
        # Try progressively removing content from the end
        for end_char in ["}]}", "]}", "}", "]"]:
            idx = json_str.rfind(end_char[0])
            if idx > 0:
                candidate = json_str[: idx + 1]
                # Count braces/brackets to see if we can close them
                open_braces = candidate.count("{") - candidate.count("}")
                open_brackets = candidate.count("[") - candidate.count("]")
                if open_braces >= 0 and open_brackets >= 0:
                    # Add missing closing characters
                    candidate += "]" * open_brackets + "}" * open_braces
                    return candidate
        return ""

    def _run_spec_review(
        self,
        spec_content: str,
        repo_path: Path,
    ) -> SpecReviewResult:
        """Run the Spec Review phase to identify gaps and questions."""
        # Read previously answered questions to avoid asking duplicates
        qa_history = self._read_qa_history(repo_path)

        if qa_history:
            prompt = SPEC_REVIEW_PROMPT.format(spec_content=spec_content[:12000])
            prompt += f"""

IMPORTANT: The following questions have ALREADY been answered. Do NOT ask these questions again or any variations of them. Only ask NEW questions about topics NOT covered below:

Previously Answered Questions:
---
{qa_history}
---
"""
        else:
            prompt = SPEC_REVIEW_PROMPT.format(spec_content=spec_content[:12000])

        raw = self._parse_json_with_recovery(prompt, "spec_review")

        if not raw:
            # All recovery failed - return a result indicating retry is needed
            logger.warning(
                "PRA spec_review: No JSON recovered, will retry in next iteration"
            )
            return SpecReviewResult(
                summary="Spec review JSON parsing failed - will retry",
                issues=["JSON parsing failed - response may have been truncated"],
                gaps=[],
                open_questions=[],
            )

        result = self._parse_spec_review_response(raw)

        # Filter out any questions that are duplicates of previously answered ones
        if qa_history and result.open_questions:
            result.open_questions = self._filter_duplicate_questions(
                result.open_questions, qa_history
            )

        return result

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
    ) -> List[OpenQuestion]:
        """Filter out questions that appear to be duplicates of answered ones."""
        qa_history_lower = qa_history.lower()
        filtered = []
        
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
                    continue
            
            filtered.append(q)
        
        if len(filtered) < len(new_questions):
            logger.info(
                "Filtered %d duplicate questions based on qa_history",
                len(new_questions) - len(filtered),
            )
        
        return filtered

    def _parse_spec_review_response(self, raw: Any) -> SpecReviewResult:
        """Parse LLM response into SpecReviewResult."""
        if not isinstance(raw, dict):
            return SpecReviewResult(summary="Spec review completed (no structured output)")

        issues = raw.get("issues", [])
        gaps = raw.get("gaps", [])
        raw_questions = raw.get("open_questions", [])

        open_questions = []
        if isinstance(raw_questions, list):
            for i, q in enumerate(raw_questions):
                open_questions.append(self._parse_open_question(q, i))

        return SpecReviewResult(
            issues=list(issues) if isinstance(issues, list) else [],
            gaps=list(gaps) if isinstance(gaps, list) else [],
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
                selected_id = sub.get("selected_option_id", "")
                other_text = sub.get("other_text") or ""
                was_auto = sub.get("was_auto_answered", False)

                if selected_id == "other" and other_text:
                    selected_answer = other_text
                else:
                    opt = next((o for o in q.options if o.id == selected_id), None)
                    selected_answer = opt.label if opt else other_text or "Unknown"

                answered.append(
                    AnsweredQuestion(
                        question_id=q.id,
                        question_text=q.question_text,
                        selected_option_id=selected_id,
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

        raw = self._parse_json_with_recovery(prompt, "spec_cleanup")

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
