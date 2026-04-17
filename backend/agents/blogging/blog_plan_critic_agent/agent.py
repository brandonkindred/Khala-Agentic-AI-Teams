"""Independent LLM pass that evaluates a ContentPlan against the author's brand spec + rubric.

The critic's verdict is authoritative: the planning loop terminates only when the
critic approves, and refine feedback is built from the critic's structured
violations instead of a generic string.

This agent intentionally runs as its own strands Agent (own session, own system
prompt) so the model critiques without being primed as the author's voice. It
uses the same LLM client as the planner per the project's tenet that per-role
model diversification is a future concern; only the role (prompt + session) is
separate today.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional, Union

from blog_planning_agent.json_utils import parse_json_object
from shared.content_plan import ContentPlan
from strands import Agent

from .models import PlanCriticReport, PlanViolation
from .prompts import PLAN_CRITIC_SYSTEM, PLAN_CRITIC_USER_TEMPLATE

try:
    from shared.artifacts import write_artifact
except ImportError:  # pragma: no cover - defensive; artifacts may be unavailable in tests
    write_artifact = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_JSON_RETRY_SUFFIX = (
    "\n\nRespond with a single JSON object only (no markdown, no code fences). "
    'Keys: "status", "approved", "violations", "notes", "rubric_version".'
)

_MAX_CRITIC_LLM_ATTEMPTS = 2

_RESEARCH_DIGEST_CHAR_CAP = 8000
_BRAND_SPEC_CHAR_CAP = 16000
_WRITING_GUIDELINES_CHAR_CAP = 16000


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


class BlogPlanCriticAgent:
    """Evaluates a ContentPlan against the brand spec + writing guidelines + rubric.

    The agent is constructed once and reused across refine iterations. ``run`` is
    stateless: each call opens a fresh strands ``Agent`` with the critic system
    prompt so no context leaks between plans.
    """

    def __init__(self, llm_client: Any) -> None:
        assert llm_client is not None, "llm_client is required"
        self._model = llm_client

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
        """
        user_prompt = PLAN_CRITIC_USER_TEMPLATE.format(
            brand_spec_prompt=(brand_spec_prompt or "").strip()[:_BRAND_SPEC_CHAR_CAP],
            writing_guidelines=(writing_guidelines or "").strip()[:_WRITING_GUIDELINES_CHAR_CAP],
            research_digest=(research_digest or "").strip()[:_RESEARCH_DIGEST_CHAR_CAP]
            or "(no research digest supplied)",
            plan_json=json.dumps(plan.model_dump(mode="json"), indent=2),
        )

        if on_llm_request:
            on_llm_request("Plan critic: evaluating plan against brand spec + rubric...")

        data: Optional[dict[str, Any]] = None
        last_err: Optional[Exception] = None
        for attempt in range(_MAX_CRITIC_LLM_ATTEMPTS):
            suffix = (
                "\n\nRespond with valid JSON only, no markdown fences."
                if attempt == 0
                else _JSON_RETRY_SUFFIX
            )
            try:
                agent = Agent(model=self._model, system_prompt=PLAN_CRITIC_SYSTEM)
                raw = str(agent(user_prompt + suffix)).strip()
                data = parse_json_object(raw)
                break
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                last_err = e
                logger.warning(
                    "Plan critic JSON parse failed on attempt %s/%s: %s",
                    attempt + 1,
                    _MAX_CRITIC_LLM_ATTEMPTS,
                    e,
                )
            except Exception as e:  # pragma: no cover - network / infra errors
                last_err = e
                logger.warning(
                    "Plan critic LLM call failed on attempt %s/%s: %s",
                    attempt + 1,
                    _MAX_CRITIC_LLM_ATTEMPTS,
                    e,
                )

        if data is None:
            report = _fallback_report(str(last_err) if last_err else "unknown")
        else:
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

        When the LLM returns partial or slightly-malformed fields, coerce to a
        conservative FAIL rather than crashing.
        """
        status_raw = (data.get("status") or "FAIL").upper() if isinstance(data, dict) else "FAIL"
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
            description = (v.get("description") or "").strip() or "(no description provided)"
            suggested_fix = (v.get("suggested_fix") or "").strip() or "(no suggested fix provided)"
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
