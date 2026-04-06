from .asset_registry import AssetRegistry


def write_docx_from_template(template: str, context: dict) -> str:
    return f"docx://{template}?keys={','.join(sorted(context.keys()))}"


def render_pdf(docx_path: str) -> str:
    return f"pdf://{docx_path}"


def export_backlog_csv(findings: list[dict]) -> str:
    return f"csv://backlog?rows={len(findings)}"


def create_jira_issues(findings: list[dict]) -> list[str]:
    return [f"A11Y-{idx + 1}" for idx, _ in enumerate(findings)]


# ---------------------------------------------------------------------------
# Case study template helpers
# ---------------------------------------------------------------------------

def _load_case_study_templates() -> dict:
    """Load and cache the case study templates YAML asset."""
    return AssetRegistry.load("case_study_templates.yaml")


def get_case_study_template(
    template_key: str,
    industry: str | None = None,
) -> dict:
    """Return a case study template definition by key.

    Args:
        template_key: One of ``comprehensive``, ``basic_audit``,
            ``premium_assessment``, ``enterprise_analysis``,
            ``executive_summary``, or ``video_script``.
        industry: Optional industry slug (``ecommerce``, ``saas``,
            ``healthcare``) to fetch an industry-specific template instead.

    Returns:
        The template definition dict from the YAML asset.
    """
    data = _load_case_study_templates()
    if industry and industry in data.get("industry_templates", {}):
        return data["industry_templates"][industry]
    return data["templates"].get(template_key, data["templates"]["comprehensive"])


def render_case_study(
    engagement_id: str,
    findings: list[dict],
    client_context: dict,
    template_key: str = "comprehensive",
    industry: str | None = None,
) -> dict:
    """Render a case study document from audit findings and client data.

    This tool selects the appropriate template based on ``template_key``
    and optional ``industry``, then merges the engagement findings and
    ``client_context`` values into the template structure.

    Args:
        engagement_id: The engagement/audit identifier.
        findings: List of finding dicts from the audit pipeline.
        client_context: Client-provided data to populate template
            placeholders (company name, metrics, testimonials, etc.).
        template_key: Template variant to use.
        industry: Optional industry slug for an industry-specific template.

    Returns:
        Dict with ``template_used``, ``artifact`` reference path,
        ``sections`` populated with merged data, and summary ``metrics``.
    """
    template = get_case_study_template(template_key, industry)
    template_name = template.get("name", template_key)

    # Derive summary metrics from findings
    severity_counts = {}
    for f in findings:
        sev = f.get("severity", "unknown")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1

    metrics = {
        "total_findings": len(findings),
        "severity_breakdown": severity_counts,
        "wcag_compliance_before_pct": client_context.get("wcag_compliance_before_pct"),
        "wcag_compliance_after_pct": client_context.get("wcag_compliance_after_pct"),
        "conversion_rate_improvement_pct": client_context.get("conversion_rate_improvement_pct"),
        "user_satisfaction_before": client_context.get("user_satisfaction_before"),
        "user_satisfaction_after": client_context.get("user_satisfaction_after"),
    }
    # Strip None values
    metrics = {k: v for k, v in metrics.items() if v is not None}

    sections = _populate_template_sections(template, client_context, findings)

    artifact_ref = f"case_study_{engagement_id}_{template_key}.json"

    return {
        "template_used": template_name,
        "template_key": template_key,
        "industry": industry,
        "engagement_id": engagement_id,
        "artifact": artifact_ref,
        "sections": sections,
        "metrics": metrics,
    }


def _populate_template_sections(
    template: dict,
    client_context: dict,
    findings: list[dict],
) -> list[dict]:
    """Walk template sections and fill placeholders from client_context.

    Handles three template structures:
    - Standard templates: ``sections`` list of ``{name: {fields, metrics, items}}``
    - Industry templates: ``solution_focus``, ``challenge_areas``, ``result_metrics``
    - Video script templates: ``segments`` list + ``key_soundbites``
    """
    populated: list[dict] = []

    # --- Standard templates (sections key) ---
    raw_sections = template.get("sections", [])
    for section in raw_sections:
        if isinstance(section, dict):
            for section_name, section_def in section.items():
                filled = {"section": section_name}
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

    # --- Industry templates ---
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


def list_case_study_templates() -> dict:
    """Return a summary of all available case study templates.

    Returns:
        Dict with ``templates`` and ``industry_templates`` keys, each
        mapping template keys to their name and description.
    """
    data = _load_case_study_templates()
    summary: dict = {"templates": {}, "industry_templates": {}}
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
