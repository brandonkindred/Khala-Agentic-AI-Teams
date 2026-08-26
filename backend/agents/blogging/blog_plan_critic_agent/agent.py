"""Independent LLM pass that evaluates a ContentPlan against the author's brand spec + rubric.

The critic's verdict is authoritative: the planning loop terminates only when the
critic approves, and refine feedback is built from the critic's structured
violations instead of a generic string.

This agent intentionally runs as its own strands Agent (own session, own system
prompt) so the model critiques without being primed as the author's voice. It
uses the same LLM client as the planner per the project's tenet that per-role
model diversification is a future concern; only the role (prompt + session) is
separate today.

Transient LLM errors (``LLMRateLimitError``, ``LLMTemporaryError``) — including
when strands wraps them in ``EventLoopException`` — propagate unwrapped so the
job runner or Temporal activity owns retry/backoff. Non-transient failures and
JSON parse exhaustion fall back to a FAIL report.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional, Union

from agents.blogging.shared.agent_base import _BlogAgentBase
from agents.blogging.shared.artifacts import write_artifact
from agents.blogging.shared.content_plan import ContentPlan
from agents.blogging.shared.json_retry import run_json_gate

from .models import PlanCriticReport, PlanViolation
from .prompts import PLAN_CRITIC_SYSTEM, PLAN_CRITIC_USER_TEMPLATE

logger = logging.getLogger(__name__)


def _fallback_report(reason: str) -> PlanCriticReport:
    """When the critic LLM cannot be parsed, fail closed with an actionable note."""
    return PlanCriticReport(
        status="FAIL",
        approved=False,
        violations=[],
        notes=(
            "Plan critic did not produce parseable JSON after retries; treating as FAIL "
            "so the refine loop continues. Reason: " + reason
        ),
    )


class BlogPlanCriticAgent(_BlogAgentBase):
    """Evaluates a ContentPlan against the brand spec + writing guidelines + rubric.

    The agent is constructed once and reused across refine iterations. ``run`` is
    stateless: each call opens a fresh strands ``Agent`` with the critic system
    prompt so no context leaks between plans.
    """

    def __init__(self, llm_client: Any) -> None:
        super().__init__(llm_client)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        plan: ContentPlan,
        brand_spec_prompt: str,
        writing_guidelines: str,
        research_digest: str = "",
        on_llm_request: Optional[Callable[[str], None]] = None,
        work_dir: Optional[Union[str, Path]] = None,
        artifact_name: str = "plan_critic_report.json",
    ) -> PlanCriticReport:
        """Evaluate ``plan`` and return a ``PlanCriticReport``.

        Parameters:
            plan: the ContentPlan to evaluate.
            brand_spec_prompt: rendered brand spec text (author-owned source of truth).
            writing_guidelines: rendered writing guidelines text (author-owned).
            research_digest: optional research digest used by the planner; may be empty.
            on_llm_request: optional progress callback.
            work_dir: when provided, persists the report as JSON for inspection.
            artifact_name: override for the persisted filename (useful per iteration).

        Preconditions:
            - ``self._model`` is a usable LLM client (enforced in ``__init__``).
            - ``plan`` is a ``ContentPlan``; ``brand_spec_prompt`` and
              ``writing_guidelines`` are strings (empty is tolerated but low-signal).
        Postconditions:
            - Always returns a ``PlanCriticReport`` (never ``None``); ``status`` is
              normalized to ``"PASS"`` or ``"FAIL"`` and ``approved`` is reconciled
              with ``status`` and ``must_fix`` violations.
            - A transient LLM-transport error (``LLMRateLimitError`` / ``LLMTemporaryError``),
              including when strands wraps it in ``EventLoopException``, propagates
              unwrapped so the caller (or Temporal) can retry; JSON parse exhaustion
              and other unexpected non-transient errors fail closed via ``on_exhausted`` /
              ``on_unexpected_error`` hooks with a ``status="FAIL"`` fallback report
              rather than raising.
            - When ``work_dir`` is set and ``write_artifact`` is available, the report is
              persisted as ``artifact_name`` (default ``plan_critic_report.json``).
        """
        user_prompt = PLAN_CRITIC_USER_TEMPLATE.format(
            brand_spec_prompt=(brand_spec_prompt or "").strip(),
            writing_guidelines=(writing_guidelines or "").strip(),
            research_digest=(research_digest or "").strip() or "(no research digest supplied)",
            plan_json=json.dumps(plan.model_dump(mode="json"), indent=2),
        )

        if on_llm_request:
            on_llm_request("Plan critic: evaluating plan against brand spec + rubric...")

        soft_json_instruction = "\n\nRespond with valid JSON only, no markdown fences."
        strict_json_suffix = (
            "\n\nRespond with a single JSON object only (no markdown, no code fences). "
            'Keys: "status", "approved", "violations", "notes", "rubric_version".'
        )

        def _fallback_dict(exc: Exception) -> dict[str, Any]:
            return _fallback_report(str(exc)).model_dump(mode="json")

        data = run_json_gate(
            self._model,
            PLAN_CRITIC_SYSTEM,
            user_prompt + soft_json_instruction,
            max_attempts=2,
            strict_json_suffix=strict_json_suffix,
            fresh_agent_per_attempt=True,
            fallback_builder=_fallback_dict,
            logger=logger,
        )
        report = self._coerce_report(data)

        # Enforce the invariant: approved iff status == PASS with no must_fix items
        approved = report.status == "PASS" and report.must_fix_count() == 0
        if approved != report.approved:
            report = report.model_copy(update={"approved": approved})

        if work_dir and write_artifact is not None:
            try:
                write_artifact(work_dir, artifact_name, report.to_dict())
                logger.info(
                    "Wrote %s: status=%s, violations=%d",
                    artifact_name,
                    report.status,
                    len(report.violations),
                )
            except Exception as e:  # pragma: no cover - artifact writing is best-effort
                logger.warning("Failed to persist %s: %s", artifact_name, e)

        return report

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_report(data: dict[str, Any]) -> PlanCriticReport:
        """Best-effort coercion of raw LLM JSON into a PlanCriticReport.

        When the LLM returns partial, slightly-malformed, or wrongly-typed
        fields, coerce to a conservative FAIL rather than crashing.
        """
        try:
            status_raw = (
                str(data.get("status") or "FAIL").upper() if isinstance(data, dict) else "FAIL"
            )
            status = "PASS" if status_raw == "PASS" else "FAIL"

            raw_violations = data.get("violations") or []
            if not isinstance(raw_violations, list):
                raw_violations = []

            violations: list[PlanViolation] = []
            for v in raw_violations:
                if not isinstance(v, dict):
                    continue
                rule_id = str(v.get("rule_id") or "unknown").strip() or "unknown"
                severity_raw = str(v.get("severity") or "must_fix").lower()
                if severity_raw not in ("must_fix", "should_fix", "consider"):
                    severity_raw = "must_fix"
                description = str(v.get("description") or "").strip() or "(no description provided)"
                suggested_fix = (
                    str(v.get("suggested_fix") or "").strip() or "(no suggested fix provided)"
                )
                evidence_quote = v.get("evidence_quote")
                if isinstance(evidence_quote, str):
                    evidence_quote = evidence_quote.strip() or None
                else:
                    evidence_quote = None
                section = v.get("section")
                if isinstance(section, str):
                    section = section.strip() or None
                else:
                    section = None
                violations.append(
                    PlanViolation(
                        rule_id=rule_id,
                        severity=severity_raw,  # type: ignore[arg-type]
                        section=section,
                        evidence_quote=evidence_quote,
                        description=description,
                        suggested_fix=suggested_fix,
                    )
                )

            approved_raw = data.get("approved")
            approved = bool(approved_raw) if isinstance(approved_raw, bool) else (status == "PASS")

            notes = data.get("notes")
            if not isinstance(notes, str):
                notes = None

            rubric_version = data.get("rubric_version")
            if not isinstance(rubric_version, str) or not rubric_version.strip():
                rubric_version = "v1"

            return PlanCriticReport(
                status=status,
                approved=approved,
                violations=violations,
                notes=notes,
                rubric_version=rubric_version,
            )
        except Exception as e:
            logger.warning("Failed to coerce critic report: %s", e)
            return _fallback_report(str(e))


def build_refine_feedback_from_critic(report: PlanCriticReport) -> str:
    """Format the critic's violations into refine-loop feedback the planner can act on.

    Sorted by severity (must_fix first) so the refiner can't miss the blockers.
    """
    if not report.violations:
        if report.approved:
            return "Plan critic approved the plan; no refinement needed."
        return (
            "Plan critic rejected the plan but did not list violations; "
            "revisit the 13 rubric rules and tighten vague sections."
        )

    severity_order = {"must_fix": 0, "should_fix": 1, "consider": 2}
    ordered = sorted(
        report.violations,
        key=lambda v: (severity_order.get(v.severity, 3), v.rule_id),
    )

    lines: list[str] = [
        "An independent plan critic reviewed the previous plan and rejected it.",
        "Address every must_fix violation and resolve should_fix items where possible.",
        "",
    ]
    for v in ordered:
        where = f"[{v.section}] " if v.section else ""
        evidence = f'\n   evidence: "{v.evidence_quote}"' if v.evidence_quote else ""
        lines.append(
            f"- {v.severity.upper()} {where}{v.rule_id}: {v.description}{evidence}\n"
            f"   fix: {v.suggested_fix}"
        )
    if report.notes:
        lines.append("")
        lines.append(f"Critic notes: {report.notes}")
    return "\n".join(lines)
