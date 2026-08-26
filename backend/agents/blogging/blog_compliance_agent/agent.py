"""
Blog compliance agent: Brand and Style Enforcer with veto power.

Evaluates drafts against the brand spec prompt and produces compliance_report.json.
FAIL status blocks publication and triggers the rewrite loop.

Transient LLM errors propagate unwrapped for retry; non-transient errors fail closed
with a FAIL compliance report.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

from agents.blogging.shared.agent_base import _BlogAgentBase
from agents.blogging.shared.artifacts import read_artifact, read_latest_draft, write_artifact
from agents.blogging.shared.brand_spec import load_brand_spec_prompt
from agents.blogging.shared.json_retry import run_json_gate

from .models import ComplianceReport, Violation
from .prompts import COMPLIANCE_PROMPT

try:
    from agents.blogging.shared.errors import ComplianceError
except ImportError:  # pragma: no cover - defensive ImportError fallback for missing shared.errors; not exercised because conftest guarantees the import path resolves.

    class ComplianceError(Exception):
        pass


logger = logging.getLogger(__name__)

_JSON_RETRY_SUFFIX = (
    "\n\nRespond with a single JSON object only (no markdown, no code fences). "
    'Keys: "status", "violations", "required_fixes", "notes".'
)

_ALWAYS_ON_JSON_INSTRUCTION = "\n\nRespond with valid JSON only, no markdown fences."


def _fallback_compliance_report(exc: Exception) -> ComplianceReport:
    """When the compliance check cannot complete (JSON parse failure or unexpected error), fail closed with actionable guidance (no crash)."""
    return ComplianceReport(
        status="FAIL",
        violations=[],
        required_fixes=[
            "Automated brand compliance did not complete (LLM error). Re-run when the model is available, "
            "or review the draft against your brand spec manually."
        ],
        notes=(
            "Compliance check could not run to completion. This reflects a tooling/LLM failure, "
            f"not a verified brand finding. Error: {exc}"
        ),
    )


class BlogComplianceAgent(_BlogAgentBase):
    """
    Expert agent that checks a draft against the brand spec and produces a compliance report.

    FAIL status triggers the orchestrator to block publication and enter the rewrite loop.
    """

    def __init__(self, llm_client: Any) -> None:
        super().__init__(llm_client)

    def run(
        self,
        draft: str,
        brand_spec_prompt: str,
        validator_report: Optional[Dict[str, Any]] = None,
        *,
        work_dir: Optional[Union[str, Path]] = None,
        on_llm_request: Optional[Callable[[str], None]] = None,
    ) -> ComplianceReport:
        """
        Evaluate the draft against the brand spec and produce a compliance report.

        Args:
            draft: The draft text to evaluate.
            brand_spec_prompt: Full brand spec prompt text (e.g. from brand_spec_prompt.md).
            validator_report: Optional validator_report.json content.
            work_dir: If provided, write compliance_report.json here.
            on_llm_request: Optional callback invoked before the LLM call with a status message.

        Returns:
            ComplianceReport with status PASS or FAIL.

        Preconditions:
            - ``self._model`` is a usable LLM client (enforced in ``__init__``).
            - ``draft`` and ``brand_spec_prompt`` are strings (empty is tolerated but
              low-signal — an empty draft yields an uninformative report).
        Postconditions:
            - Always returns a ``ComplianceReport`` (never ``None``); ``status`` is
              normalized to ``"PASS"`` or ``"FAIL"``.
            - A transient LLM-transport error (``LLMRateLimitError`` / ``LLMTemporaryError``)
              propagates unwrapped so the caller (or Temporal) can retry; a non-transient
              LLM failure fails closed with a ``status="FAIL"`` fallback report rather than
              raising.
            - When ``work_dir`` is set and ``write_artifact`` is available, the report is
              persisted as ``compliance_report.json`` on both success and fail-closed
              fallback paths (exhausted JSON parse and unexpected non-transient errors).
        """
        brand_summary = (brand_spec_prompt or "").strip()

        # Pass only a concise summary of the validator report to avoid LLM echoing
        # long markdown content that breaks JSON parsing.
        if validator_report:
            checks = validator_report.get("checks", [])
            failed = [c.get("name", "unknown") for c in checks if c.get("status") == "FAIL"]
            validator_summary = (
                f"Overall: {validator_report.get('status', 'unknown')}. "
                f"Failed checks: {', '.join(failed) or 'none'}."
            )
        else:
            validator_summary = "No validator report available."

        prompt = COMPLIANCE_PROMPT.format(
            brand_spec_summary=brand_summary,
            validator_summary=validator_summary,
            draft=draft,
        )

        if on_llm_request:
            on_llm_request("Checking compliance with brand guidelines...")

        prompt_for_helper = prompt + _ALWAYS_ON_JSON_INSTRUCTION

        def _fallback_dict(exc: Exception) -> Dict[str, Any]:
            return _fallback_compliance_report(exc).to_dict()

        data = run_json_gate(
            self._model,
            "You are a brand compliance evaluator.",
            prompt_for_helper,
            strict_json_suffix=_JSON_RETRY_SUFFIX,
            fallback_builder=_fallback_dict,
            logger=logger,
        )

        status = (data.get("status") or "FAIL").upper()
        if status not in ("PASS", "FAIL"):
            status = "FAIL"

        raw_violations = data.get("violations") or []
        violations = []
        for v in raw_violations:
            if not isinstance(v, dict):
                continue
            violations.append(
                Violation(
                    rule_id=v.get("rule_id", "unknown"),
                    description=v.get("description", ""),
                    evidence_quotes=v.get("evidence_quotes") or [],
                    location_hint=v.get("location_hint"),
                )
            )

        required_fixes = data.get("required_fixes") or []
        if not isinstance(required_fixes, list):
            required_fixes = [str(required_fixes)] if required_fixes else []

        notes = data.get("notes")

        report = ComplianceReport(
            status=status,
            violations=violations,
            required_fixes=required_fixes,
            notes=notes,
        )

        if work_dir and write_artifact:
            write_artifact(work_dir, "compliance_report.json", report.to_dict())
            logger.info("Wrote compliance_report.json: status=%s", status)

        return report


def run_compliance_from_work_dir(
    work_dir: Union[str, Path],
    llm_client: Any,
    *,
    draft_artifact: str = "final.md",
    brand_spec_path: Optional[Union[str, Path]] = None,
) -> ComplianceReport:
    """
    Run the compliance agent using artifacts already present under ``work_dir``.

    Args:
        work_dir: Job workspace directory containing draft / validator / brand artifacts.
        llm_client: LLM client passed through to ``BlogComplianceAgent``.
        draft_artifact: Preferred draft filename (default ``final.md``); falls back to
            ``draft_v2.md`` then ``draft_v1.md`` when the preferred file is empty/missing.
        brand_spec_path: Optional explicit brand-spec path; otherwise uses
            ``work_dir/brand_spec_prompt.md``, then the team default under ``docs/``.

    Returns:
        ``ComplianceReport`` from ``BlogComplianceAgent.run``.
    """
    work_path = Path(work_dir).resolve()
    draft = read_latest_draft(work_dir, draft_artifact)

    validator_report = read_artifact(work_dir, "validator_report.json", default=None)

    brand_path = brand_spec_path or (work_path / "brand_spec_prompt.md")
    if not Path(brand_path).exists():
        _blogging_root = Path(__file__).resolve().parent.parent
        brand_path = _blogging_root / "docs" / "brand_spec_prompt.md"
    brand_spec_prompt = load_brand_spec_prompt(brand_path)

    agent = BlogComplianceAgent(llm_client=llm_client)
    return agent.run(draft, brand_spec_prompt, validator_report, work_dir=work_dir)
