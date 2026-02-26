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
            )

            try:
                spec_review_result = self._run_spec_review(current_spec, repo_path)
                result.spec_review_result = spec_review_result
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
            )

            try:
                current_spec = self._update_spec(
                    current_spec=current_spec,
                    answered_questions=answered_questions,
                    repo_path=repo_path,
                    iteration=iteration,
                )
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
        )

        try:
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
        )

        elapsed = time.monotonic() - start_time
        logger.info(
            "Product Requirements Analysis Agent: WORKFLOW COMPLETE in %.1fs", elapsed
        )

        return result

    def _run_spec_review(
        self,
        spec_content: str,
        repo_path: Path,
    ) -> SpecReviewResult:
        """Run the Spec Review phase to identify gaps and questions."""
        prompt = SPEC_REVIEW_PROMPT.format(spec_content=spec_content[:12000])

        try:
            raw = self.llm.complete_json(prompt)
            return self._parse_spec_review_response(raw)
        except Exception as e:
            logger.warning("Spec review LLM call failed: %s", e)
            return SpecReviewResult(
                summary=f"Spec review failed: {e}",
            )

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

        try:
            raw = self.llm.complete_json(prompt)
            return self._parse_spec_cleanup_response(raw, spec_content)
        except Exception as e:
            logger.warning("Spec cleanup LLM call failed: %s", e)
            return SpecCleanupResult(
                is_valid=True,
                cleaned_spec=spec_content,
                summary=f"Spec cleanup skipped due to error: {e}",
            )

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
