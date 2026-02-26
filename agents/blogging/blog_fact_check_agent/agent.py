"""
Fact-Checker and Risk Officer agent.

Verifies claims are supported, flags hazards, and identifies required disclaimers.

All errors are raised explicitly - no silent failures.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from blog_research_agent.llm import LLMClient

from .models import FactCheckReport
from .prompts import FACT_CHECK_PROMPT

try:
    from shared.artifacts import write_artifact
    from shared.brand_spec import BrandSpec, load_brand_spec
except ImportError:
    write_artifact = None
    load_brand_spec = None
    BrandSpec = None

try:
    from shared.errors import FactCheckError, LLMError
except ImportError:
    class FactCheckError(Exception):
        pass
    class LLMError(Exception):
        pass

logger = logging.getLogger(__name__)


class BlogFactCheckAgent:
    """
    Agent that verifies claims and flags risk. Gates on claims_status and risk_status.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(
        self,
        draft: str,
        allowed_claims: Optional[Dict[str, Any]] = None,
        require_disclaimer_for: Optional[List[str]] = None,
        *,
        work_dir: Optional[Union[str, Path]] = None,
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
            [{"id": c.get("id"), "text": c.get("text"), "citations": c.get("citations", [])} for c in claims_list],
            indent=2,
        )

        prompt = FACT_CHECK_PROMPT.format(
            draft=draft[:12000],
            allowed_claims_text=allowed_text[:4000],
            require_disclaimer_for=", ".join(require_disclaimer_for),
        )

        try:
            data = self.llm.complete_json(prompt, temperature=0.1)
        except LLMError:
            raise
        except Exception as e:
            logger.error("Fact-check failed: %s", e)
            raise FactCheckError(
                f"Fact-check failed: {e}",
                cause=e,
            ) from e

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
            data = report.dict() if hasattr(report, "dict") else report.model_dump()
            write_artifact(work_dir, "fact_check_report.json", data)
            logger.info("Wrote fact_check_report.json: claims=%s risk=%s", claims_status, risk_status)

        return report


def run_fact_check_from_work_dir(
    work_dir: Union[str, Path],
    llm_client: LLMClient,
    *,
    draft_artifact: str = "final.md",
) -> FactCheckReport:
    """Run fact-check using artifacts from work_dir."""
    try:
        from shared.artifacts import read_artifact
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
    work_path = Path(work_dir).resolve()
    brand_path = work_path / "brand_spec.yaml"
    if not brand_path.exists():
        _blogging_root = Path(__file__).resolve().parent.parent
        brand_path = _blogging_root / "docs" / "brand_spec.yaml"
    if brand_path.exists() and load_brand_spec:
        try:
            spec = load_brand_spec(brand_path)
            require_disclaimer = spec.content_rules.safety.require_disclaimer_for or require_disclaimer
        except Exception:
            pass

    agent = BlogFactCheckAgent(llm_client=llm_client)
    return agent.run(
        draft,
        allowed_claims=allowed_claims,
        require_disclaimer_for=require_disclaimer,
        work_dir=work_dir,
    )
