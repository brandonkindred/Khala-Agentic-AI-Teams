"""
Fact-Checker and Risk Officer agent.

Verifies claims are supported, flags hazards, and identifies required disclaimers.

Transient LLM errors propagate unwrapped for retry; exhausted JSON parse fails closed
with a FAIL report; other unexpected errors raise FactCheckError.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from agents.blogging.shared.agent_base import _BlogAgentBase
from agents.blogging.shared.artifacts import read_artifact, read_latest_draft, write_artifact
from agents.blogging.shared.errors import FactCheckError
from agents.blogging.shared.json_retry import run_json_gate

from llm_service import LLMRateLimitError, LLMTemporaryError

from .models import FactCheckReport
from .prompts import FACT_CHECK_PROMPT

logger = logging.getLogger(__name__)

_ALWAYS_ON_JSON_INSTRUCTION = "\n\nRespond with valid JSON only, no markdown fences."

_JSON_RETRY_SUFFIX = (
    "\n\nCRITICAL: Your previous response contained invalid JSON. "
    "Output ONLY a single valid JSON object. No code blocks or markdown in values."
)


class BlogFactCheckAgent(_BlogAgentBase):
    """
    Expert agent that verifies claims and flags risk. Gates on claims_status and risk_status.
    """

    def __init__(self, llm_client: Any) -> None:
        super().__init__(llm_client)

    def run(
        self,
        draft: str,
        allowed_claims: Optional[Dict[str, Any]] = None,
        require_disclaimer_for: Optional[List[str]] = None,
        *,
        work_dir: Optional[Union[str, Path]] = None,
        on_llm_request: Optional[Callable[[str], None]] = None,
    ) -> FactCheckReport:
        """
        Run fact-check and risk assessment.

        Args:
            draft: The draft text.
            allowed_claims: allowed_claims.json content.
            require_disclaimer_for: Categories requiring disclaimers (from brand_spec).
            work_dir: If provided, write fact_check_report.json.
            on_llm_request: Optional callback invoked before the LLM call with a status message.

        Returns:
            FactCheckReport with claims_status and risk_status.

        Preconditions:
            - ``self._model`` is a usable LLM client (enforced in ``__init__``).
            - ``draft`` is a string (empty is tolerated but low-signal — an empty draft
              yields an uninformative report).
        Postconditions:
            - Always returns a ``FactCheckReport`` (never ``None``) on success and
              exhausted-JSON fallback paths; ``claims_status`` and ``risk_status`` are
              normalized to ``"PASS"`` or ``"FAIL"`` (unknown / missing values fail closed
              as ``"FAIL"``).
            - A transient LLM-transport error (``LLMRateLimitError`` / ``LLMTemporaryError``)
              propagates unwrapped so the caller (or Temporal) can retry.
            - Any other unexpected error is logged at exception level and raised as
              ``FactCheckError``.
            - When JSON parsing is exhausted after retries, returns a fallback report with
              ``claims_status="FAIL"`` and ``risk_status="FAIL"`` rather than raising.
            - When ``work_dir`` is set and ``write_artifact`` is available, the report is
              persisted as ``fact_check_report.json`` on both success and exhausted-JSON
              fallback paths.
        """
        require_disclaimer_for = require_disclaimer_for or ["medical", "legal", "financial"]
        claims_list = (allowed_claims or {}).get("claims") or []
        allowed_text = json.dumps(
            [
                {"id": c.get("id"), "text": c.get("text"), "citations": c.get("citations", [])}
                for c in claims_list
            ],
            indent=2,
        )

        prompt = FACT_CHECK_PROMPT.format(
            draft=draft,
            allowed_claims_text=allowed_text,
            require_disclaimer_for=", ".join(require_disclaimer_for),
        )

        if on_llm_request:
            on_llm_request("Checking facts and claims...")

        prompt_for_helper = prompt + _ALWAYS_ON_JSON_INSTRUCTION

        def _on_exhausted(_exc: Exception) -> Dict[str, Any]:
            return {
                "claims_status": "FAIL",
                "risk_status": "FAIL",
                "risk_flags": ["Could not parse fact-check result; re-run fact check."],
                "required_disclaimers": [],
                "notes": "Fallback report: JSON parse failed after 2 attempts.",
            }

        try:
            data = run_json_gate(
                self._model,
                FACT_CHECK_PROMPT.split("{draft}")[0].strip(),
                prompt_for_helper,
                strict_json_suffix=_JSON_RETRY_SUFFIX,
                on_exhausted=_on_exhausted,
                logger=logger,
            )
        except (LLMRateLimitError, LLMTemporaryError):
            raise
        except Exception as e:
            logger.exception("Fact-check failed: %s", e)
            raise FactCheckError(f"Fact-check failed: {e}", cause=e) from e

        claims_status = (data.get("claims_status") or "FAIL").upper()
        if claims_status not in ("PASS", "FAIL"):
            claims_status = "FAIL"
        risk_status = (data.get("risk_status") or "FAIL").upper()
        if risk_status not in ("PASS", "FAIL"):
            risk_status = "FAIL"

        report = FactCheckReport(
            claims_status=claims_status,
            risk_status=risk_status,
            claims_verified=data.get("claims_verified") or [],
            risk_flags=data.get("risk_flags") or [],
            required_disclaimers=data.get("required_disclaimers") or [],
            notes=data.get("notes"),
        )

        if work_dir and write_artifact:
            data = report.to_dict()
            write_artifact(work_dir, "fact_check_report.json", data)
            logger.info(
                "Wrote fact_check_report.json: claims=%s risk=%s", claims_status, risk_status
            )

        return report


def run_fact_check_from_work_dir(
    work_dir: Union[str, Path],
    llm_client: Any,
    *,
    draft_artifact: str = "final.md",
) -> FactCheckReport:
    """
    Run the fact-check agent using artifacts already present under ``work_dir``.

    Args:
        work_dir: Job workspace directory containing draft / allowed-claims artifacts.
        llm_client: LLM client passed through to ``BlogFactCheckAgent``.
        draft_artifact: Preferred draft filename (default ``final.md``); falls back to
            ``draft_v2.md`` then ``draft_v1.md`` when the preferred file is empty/missing.

    Returns:
        ``FactCheckReport`` from ``BlogFactCheckAgent.run``. Disclaimer categories are
        currently hardcoded to medical / legal / financial.
    """
    draft = read_latest_draft(work_dir, draft_artifact)

    allowed_claims = read_artifact(work_dir, "allowed_claims.json", default=None)
    if not isinstance(allowed_claims, dict):
        allowed_claims = None

    require_disclaimer = ["medical", "legal", "financial"]

    agent = BlogFactCheckAgent(llm_client=llm_client)
    return agent.run(
        draft,
        allowed_claims=allowed_claims,
        require_disclaimer_for=require_disclaimer,
        work_dir=work_dir,
    )
