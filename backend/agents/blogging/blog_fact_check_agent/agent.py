"""
Fact-Checker and Risk Officer agent.

Verifies claims are supported, flags hazards, and identifies required disclaimers.

All errors are raised explicitly - no silent failures.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from agents.blogging.shared.json_retry import call_json_with_retry
from strands import Agent

from llm_service import LLMRateLimitError, LLMTemporaryError

from .models import FactCheckReport
from .prompts import FACT_CHECK_PROMPT

try:
    from agents.blogging.shared.artifacts import write_artifact
except ImportError:
    write_artifact = None

try:
    from agents.blogging.shared.errors import FactCheckError, LLMError
except ImportError:

    class FactCheckError(Exception):
        pass

    class LLMError(Exception):
        pass


logger = logging.getLogger(__name__)

_ALWAYS_ON_JSON_INSTRUCTION = "\n\nRespond with valid JSON only, no markdown fences."

_JSON_RETRY_SUFFIX = (
    "\n\nCRITICAL: Your previous response contained invalid JSON. "
    "Output ONLY a single valid JSON object. No code blocks or markdown in values."
)


class BlogFactCheckAgent:
    """
    Expert agent that verifies claims and flags risk. Gates on claims_status and risk_status.
    """

    def __init__(self, llm_client: Any) -> None:
        assert llm_client is not None, "llm_client is required"
        self._model = llm_client

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
            work_dir: If provided, write fact_check_report.json (or merge into compliance_report).

        Returns:
            FactCheckReport with claims_status and risk_status.
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

        def _agent_factory():
            return Agent(
                model=self._model,
                system_prompt=FACT_CHECK_PROMPT.split("{draft}")[0].strip(),
            )

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
            data = call_json_with_retry(
                _agent_factory,
                prompt_for_helper,
                max_attempts=2,
                strict_json_suffix=_JSON_RETRY_SUFFIX,
                on_exhausted=_on_exhausted,
                logger=logger,
            )
        except (LLMRateLimitError, LLMTemporaryError):
            raise
        except Exception as e:
            logger.exception("Fact-check failed: %s", e)
            raise FactCheckError(f"Fact-check failed: {e}", cause=e) from e

        claims_status = (data.get("claims_status") or "PASS").upper()
        if claims_status not in ("PASS", "FAIL"):
            claims_status = "PASS"
        risk_status = (data.get("risk_status") or "PASS").upper()
        if risk_status not in ("PASS", "FAIL"):
            risk_status = "PASS"

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
    """Run fact-check using artifacts from work_dir."""
    try:
        from agents.blogging.shared.artifacts import read_artifact
    except ImportError:
        raise ImportError("shared.artifacts required")

    draft = read_artifact(work_dir, draft_artifact, default="")
    if not draft:
        draft = read_artifact(work_dir, "draft_v2.md", default="") or read_artifact(
            work_dir, "draft_v1.md", default=""
        )

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
