"""
Tool: audit.generate_case_study

Generate a case study document from audit findings and client context
using the structured case study templates asset.
"""

from pathlib import Path
from typing import Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, Field

from ...models import Finding, Severity

_ASSETS_DIR = Path(__file__).resolve().parent.parent.parent / "a11y_agency_strands" / "assets"
_TEMPLATES_CACHE: Optional[dict] = None


def _load_templates() -> dict:
    """Load and cache case study templates from the YAML asset."""
    global _TEMPLATES_CACHE
    if _TEMPLATES_CACHE is None:
        templates_path = _ASSETS_DIR / "case_study_templates.yaml"
        with open(templates_path) as fh:
            _TEMPLATES_CACHE = yaml.safe_load(fh)
    return _TEMPLATES_CACHE


class GenerateCaseStudyInput(BaseModel):
    """Input for generating a case study from audit results."""

    audit_id: str = Field(..., description="Audit identifier")
    findings: List[Finding] = Field(default_factory=list, description="Audit findings")
    client_context: Dict = Field(
        default_factory=dict,
        description="Client-provided data for template placeholders (company name, metrics, etc.)",
    )
    template_key: Literal[
        "comprehensive",
        "basic_audit",
        "premium_assessment",
        "enterprise_analysis",
        "executive_summary",
        "video_script",
    ] = Field(default="comprehensive", description="Case study template variant")
    industry: Optional[Literal["ecommerce", "saas", "healthcare"]] = Field(
        default=None, description="Optional industry-specific template override"
    )


class GenerateCaseStudyOutput(BaseModel):
    """Output from generating a case study."""

    artifact_ref: str = Field(..., description="Reference path to the generated case study artifact")
    template_used: str = Field(default="", description="Name of the template applied")
    template_key: str = Field(default="", description="Key of the template applied")
    industry: Optional[str] = Field(default=None, description="Industry template used, if any")
    sections: List[Dict] = Field(default_factory=list, description="Populated case study sections")
    metrics: Dict = Field(default_factory=dict, description="Summary metrics derived from findings")


async def generate_case_study(input_data: GenerateCaseStudyInput) -> GenerateCaseStudyOutput:
    """
    Generate a case study document from audit findings and client context.

    Selects the appropriate template from the case study templates asset,
    populates it with finding data and client-provided context, and returns
    a structured case study ready for rendering into final deliverable formats.
    """
    data = _load_templates()

    # Select template
    if input_data.industry and input_data.industry in data.get("industry_templates", {}):
        template = data["industry_templates"][input_data.industry]
    else:
        template = data["templates"].get(
            input_data.template_key,
            data["templates"]["comprehensive"],
        )

    template_name = template.get("name", input_data.template_key)

    # Derive metrics from findings
    severity_counts = {
        "total": len(input_data.findings),
        "critical": sum(1 for f in input_data.findings if f.severity == Severity.CRITICAL),
        "high": sum(1 for f in input_data.findings if f.severity == Severity.HIGH),
        "medium": sum(1 for f in input_data.findings if f.severity == Severity.MEDIUM),
        "low": sum(1 for f in input_data.findings if f.severity == Severity.LOW),
    }

    metrics = {"severity_breakdown": severity_counts}
    # Merge relevant client-context metrics
    for key in (
        "wcag_compliance_before_pct",
        "wcag_compliance_after_pct",
        "conversion_rate_improvement_pct",
        "user_satisfaction_before",
        "user_satisfaction_after",
        "revenue_impact_amount",
        "roi_multiplier",
    ):
        if key in input_data.client_context:
            metrics[key] = input_data.client_context[key]

    # Populate sections
    sections = _populate_sections(template, input_data.client_context, input_data.findings)

    artifact_ref = f"case_study_{input_data.audit_id}_{input_data.template_key}.json"

    return GenerateCaseStudyOutput(
        artifact_ref=artifact_ref,
        template_used=template_name,
        template_key=input_data.template_key,
        industry=input_data.industry,
        sections=sections,
        metrics=metrics,
    )


def _populate_sections(
    template: dict,
    client_context: dict,
    findings: List[Finding],
) -> List[Dict]:
    """Walk template sections and merge client_context values.

    Handles three template structures:
    - Standard templates: ``sections`` list of ``{name: {fields, metrics, items}}``
    - Industry templates: ``solution_focus``, ``challenge_areas``, ``result_metrics``
    - Video script templates: ``segments`` list + ``key_soundbites``
    """
    populated: List[Dict] = []

    # --- Standard templates (sections key) ---
    raw_sections = template.get("sections", [])
    for section in raw_sections:
        if isinstance(section, dict):
            for section_name, section_def in section.items():
                filled: Dict = {"section": section_name}
                if isinstance(section_def, dict):
                    for key in section_def.get("fields", []):
                        filled[key] = client_context.get(key, f"[{key}]")
                    if "metrics" in section_def:
                        filled["metrics"] = {
                            m: client_context.get(m) for m in section_def["metrics"]
                        }
                    if "items" in section_def:
                        filled["items"] = section_def["items"]
                filled["finding_count"] = len(findings)
                populated.append(filled)

    # --- Video script templates (segments + key_soundbites) ---
    if "segments" in template:
        for segment in template["segments"]:
            if isinstance(segment, dict):
                for segment_name, segment_def in segment.items():
                    filled = {
                        "section": segment_name,
                        "type": "video_segment",
                        "duration_minutes": segment_def.get("duration_minutes", 0),
                        "questions": segment_def.get("questions", []),
                    }
                    populated.append(filled)
    if "key_soundbites" in template:
        populated.append({
            "section": "key_soundbites",
            "type": "video_soundbites",
            "items": template["key_soundbites"],
        })

    # --- Industry template special keys ---
    if "solution_focus" in template:
        populated.append({"section": "solution_focus", "items": template["solution_focus"]})
    if "challenge_areas" in template:
        populated.append({"section": "challenge_areas", "items": template["challenge_areas"]})
    if "result_metrics" in template:
        populated.append({
            "section": "result_metrics",
            "metrics": {m: client_context.get(m) for m in template["result_metrics"]},
        })

    return populated


async def list_available_templates() -> Dict:
    """Return a summary of all available case study templates."""
    data = _load_templates()
    summary: Dict = {"templates": {}, "industry_templates": {}}
    for key, tpl in data.get("templates", {}).items():
        summary["templates"][key] = {
            "name": tpl.get("name", key),
            "description": tpl.get("description", ""),
        }
    for key, tpl in data.get("industry_templates", {}).items():
        summary["industry_templates"][key] = {
            "name": tpl.get("name", key),
            "description": tpl.get("description", ""),
        }
    return summary
