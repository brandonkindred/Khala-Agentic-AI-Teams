"""SOC2 Trust Service Criteria audit agents and report writer.

Provides both the legacy class-based agents (used internally by
``_run_tsc_agent``) and Strands Agent factory functions (``make_*``)
for use as Graph nodes.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from strands import Agent

from llm_service import compact_text
from shared_graph import build_agent

from .models import (
    FindingSeverity,
    NextStepsDocument,
    RepoContext,
    SOC2ComplianceReport,
    TSCAuditResult,
    TSCCategory,
    TSCFinding,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared prompt instructions for TSC agents
# ---------------------------------------------------------------------------

_TSC_OUTPUT_FORMAT = """
Respond with a single JSON object only. No markdown or explanation outside JSON.
- "summary": string (2–4 sentence summary of your audit for this criterion)
- "findings": array of objects, each with:
  - "severity": one of "critical", "high", "medium", "low", "informational"
  - "title": string (short title)
  - "description": string (what is wrong or missing)
  - "location": string (file path, module, or area; empty if general)
  - "recommendation": string (what to do to remediate)
  - "evidence_observed": string (what you saw in the repo that led to this finding)
- "compliant": boolean (true only if there are no critical or high severity findings)

Be specific and cite repo content where possible. If the repo has no relevant evidence (e.g. no auth code for Security), report that as a finding (e.g. "No authentication/authorization implementation found"). Do not invent file paths.
"""


def _parse_finding(d: Dict[str, Any], category: TSCCategory) -> TSCFinding:
    """Build TSCFinding from LLM response dict."""
    sev = (d.get("severity") or "medium").lower()
    try:
        severity = FindingSeverity(sev)
    except ValueError:
        severity = FindingSeverity.MEDIUM
    return TSCFinding(
        severity=severity,
        category=category,
        title=d.get("title") or "Untitled",
        description=d.get("description") or "",
        location=d.get("location") or "",
        recommendation=d.get("recommendation") or "",
        evidence_observed=d.get("evidence_observed") or "",
    )


def _run_tsc_agent(
    llm: Any,
    category: TSCCategory,
    criterion_name: str,
    focus_areas: str,
    context: RepoContext,
) -> TSCAuditResult:
    """Generic TSC audit: one criterion, one LLM call, return TSCAuditResult."""
    # Compute budgets from model context: reserve 8K tokens for prompt template + response
    ctx_tokens = llm.get_max_context_tokens() if hasattr(llm, "get_max_context_tokens") else 16384
    total_chars = int((ctx_tokens - 8000) * 3.5)
    readme_budget = min(total_chars // 4, 200_000)
    code_budget = total_chars - readme_budget
    prompt = f"""You are a SOC2 auditor specializing in the **{criterion_name}** Trust Service Criterion.
Your task is to review the following repository content and identify compliance gaps or risks.

**Criterion focus:** {focus_areas}

**Repository context:**
- Repo path: {context.repo_path}
- Tech stack (inferred): {context.tech_stack_hint}
- Files scanned: {context.file_list}

**README / docs (if any):**
```
{compact_text(context.readme_content, readme_budget, llm, "README content")}
```

**Code and configuration:**
```
{compact_text(context.code_summary, code_budget, llm, "code and configuration")}
```

Identify any gaps, missing controls, or risks relative to this criterion. If the codebase does not address this criterion (e.g. no backup/monitoring for Availability), report that as a finding.
{_TSC_OUTPUT_FORMAT}"""

    data = llm.complete_json(prompt, temperature=0.1, think=True)
    summary = data.get("summary") or ""
    findings_raw = data.get("findings") or []
    findings = []
    for f in findings_raw:
        if isinstance(f, dict) and (f.get("title") or f.get("description")):
            findings.append(_parse_finding(f, category))
    compliant = data.get(
        "compliant",
        len([f for f in findings if f.severity in (FindingSeverity.CRITICAL, FindingSeverity.HIGH)])
        == 0,
    )
    return TSCAuditResult(
        category=category,
        summary=summary,
        findings=findings,
        compliant=compliant,
    )


# ---------------------------------------------------------------------------
# Per-TSC agents (thin wrappers with criterion-specific focus)
# ---------------------------------------------------------------------------


class SecurityTSCAgent:
    """Audits the repository against SOC2 Security (Common Criteria CC1–CC9)."""

    def run(self, llm: Any, context: RepoContext) -> TSCAuditResult:
        focus = (
            "Logical and physical access controls; authentication and authorization; "
            "encryption of data at rest and in transit; change management; risk assessment; "
            "monitoring and incident response; secure disposal of data."
        )
        return _run_tsc_agent(
            llm, TSCCategory.SECURITY, "Security (Common Criteria)", focus, context
        )


class AvailabilityTSCAgent:
    """Audits against SOC2 Availability criterion."""

    def run(self, llm: Any, context: RepoContext) -> TSCAuditResult:
        focus = (
            "System availability; capacity and performance management; "
            "backup and recovery; monitoring and incident management; environmental controls."
        )
        return _run_tsc_agent(llm, TSCCategory.AVAILABILITY, "Availability", focus, context)


class ProcessingIntegrityTSCAgent:
    """Audits against SOC2 Processing Integrity criterion."""

    def run(self, llm: Any, context: RepoContext) -> TSCAuditResult:
        focus = (
            "Processing completeness, validity, accuracy, timeliness, and authorization; "
            "data validation; error handling; reconciliation and quality assurance of processing."
        )
        return _run_tsc_agent(
            llm, TSCCategory.PROCESSING_INTEGRITY, "Processing Integrity", focus, context
        )


class ConfidentialityTSCAgent:
    """Audits against SOC2 Confidentiality criterion."""

    def run(self, llm: Any, context: RepoContext) -> TSCAuditResult:
        focus = (
            "Identification and classification of confidential information; "
            "disclosure only as agreed; secure handling and disposal of confidential data."
        )
        return _run_tsc_agent(llm, TSCCategory.CONFIDENTIALITY, "Confidentiality", focus, context)


class PrivacyTSCAgent:
    """Audits against SOC2 Privacy criterion."""

    def run(self, llm: Any, context: RepoContext) -> TSCAuditResult:
        focus = (
            "Collection, use, retention, disclosure, and disposal of personal information; "
            "consent; data subject rights; privacy notice and policies; PII handling in code/config."
        )
        return _run_tsc_agent(llm, TSCCategory.PRIVACY, "Privacy", focus, context)


# ---------------------------------------------------------------------------
# Report writer agent
# ---------------------------------------------------------------------------


class ReportWriterAgent:
    """
    Consumes all TSC audit results and produces either:
    - A SOC2 compliance report (when there are findings), or
    - A next-steps-for-certification document (when there are no material findings).
    """

    def run(
        self,
        llm: Any,
        repo_path: str,
        tsc_results: List[TSCAuditResult],
    ) -> tuple[SOC2ComplianceReport | None, NextStepsDocument | None]:
        """
        Returns (compliance_report, next_steps_document).
        Exactly one will be non-None: compliance_report if has_findings else next_steps_document.
        """
        has_findings = any(
            not r.compliant
            or any(
                f.severity in (FindingSeverity.CRITICAL, FindingSeverity.HIGH) for f in r.findings
            )
            for r in tsc_results
        )

        findings_by_tsc: Dict[str, List[Dict[str, Any]]] = {}
        for r in tsc_results:
            findings_by_tsc[r.category.value] = [f.model_dump() for f in r.findings]

        if has_findings:
            report = self._produce_compliance_report(llm, repo_path, tsc_results, findings_by_tsc)
            return (report, None)
        return (None, self._produce_next_steps(llm, repo_path, tsc_results))

    def _produce_compliance_report(
        self,
        llm: Any,
        repo_path: str,
        tsc_results: List[TSCAuditResult],
        findings_by_tsc: Dict[str, List[Dict[str, Any]]],
    ) -> SOC2ComplianceReport:
        """Generate full SOC2 compliance audit report with executive summary and recommendations."""
        summaries = "\n".join(f"- **{r.category.value}**: {r.summary}" for r in tsc_results)
        prompt = f"""You are a SOC2 lead auditor. Produce a **SOC2 Compliance Audit Report** for the following audit results.

**Repository:** {repo_path}

**Per-criterion summaries:**
{summaries}

**Findings by category (JSON):**
{findings_by_tsc}

Write a single JSON object with:
- "executive_summary": string (2–5 paragraphs: scope, overall posture, key risks, and high-level recommendation)
- "scope": string (one paragraph: what was in scope)
- "recommendations_summary": array of strings (prioritized remediation steps, ordered by impact)
- "raw_markdown": string (full report in markdown: title, executive summary, scope, findings by TSC with severity and recommendation, then recommendations summary)

Respond with valid JSON only. No text outside JSON."""

        data = llm.complete_json(prompt, temperature=0.2, think=True)
        findings_typed: Dict[str, List[TSCFinding]] = {}
        for cat, list_dicts in findings_by_tsc.items():
            try:
                findings_typed[cat] = [TSCFinding(**d) for d in list_dicts]
            except Exception:
                findings_typed[cat] = []
        return SOC2ComplianceReport(
            executive_summary=data.get("executive_summary") or "",
            scope=data.get("scope") or f"Repository: {repo_path}",
            findings_by_tsc=findings_typed,
            recommendations_summary=data.get("recommendations_summary") or [],
            raw_markdown=data.get("raw_markdown") or "",
        )

    def _produce_next_steps(
        self,
        llm: Any,
        repo_path: str,
        tsc_results: List[TSCAuditResult],
    ) -> NextStepsDocument:
        """Generate next steps for SOC2 certification when no material issues were found."""
        summaries = "\n".join(f"- **{r.category.value}**: {r.summary}" for r in tsc_results)
        prompt = f"""You are a SOC2 advisor. The following code repository was audited and **no material SOC2 compliance issues** were found. Produce a short document: "Next Steps for SOC2 Certification".

**Repository:** {repo_path}

**Audit summaries per criterion:**
{summaries}

Write a single JSON object with:
- "title": string (e.g. "Next Steps for SOC2 Certification")
- "introduction": string (2–4 sentences: codebase audit result and what this document covers)
- "steps": array of objects, each with "title" and "description" (and optionally "resources"), e.g. engage CPA firm, scope examination, document controls, collect evidence, Type I then Type II
- "recommended_timeline": string (high-level timeline, e.g. "3–6 months readiness, then 2–4 months for Type I/II examination")
- "raw_markdown": string (full document in markdown for display/saving)

Respond with valid JSON only. No text outside JSON."""

        data = llm.complete_json(prompt, temperature=0.2, think=True)
        steps = data.get("steps") or []
        if not isinstance(steps, list):
            steps = []
        return NextStepsDocument(
            title=data.get("title") or "Next Steps for SOC2 Certification",
            introduction=data.get("introduction") or "",
            steps=[
                s if isinstance(s, dict) else {"title": str(s), "description": ""} for s in steps
            ],
            recommended_timeline=data.get("recommended_timeline") or "",
            raw_markdown=data.get("raw_markdown") or "",
        )


# ---------------------------------------------------------------------------
# Strands Agent factories for Graph nodes
# ---------------------------------------------------------------------------

_TSC_SYSTEM_PROMPT_TEMPLATE = """You are a SOC2 auditor specializing in the **{criterion}** Trust Service Criterion.
Your task is to review repository content and identify compliance gaps or risks.

**Criterion focus:** {focus}

Analyze the repository context provided in the user message and produce your audit.

{output_format}"""


def _make_tsc_agent(criterion: str, focus: str) -> Agent:
    """Create a Strands Agent for a specific TSC criterion."""
    return build_agent(
        name=f"{criterion.lower().replace(' ', '_')}_tsc_agent",
        system_prompt=_TSC_SYSTEM_PROMPT_TEMPLATE.format(
            criterion=criterion,
            focus=focus,
            output_format=_TSC_OUTPUT_FORMAT,
        ),
        agent_key="soc2",
        description=f"SOC2 {criterion} auditor",
    )


def make_security_tsc_agent() -> Agent:
    """Create a Strands Agent for Security TSC auditing."""
    return _make_tsc_agent(
        "Security (Common Criteria)",
        "Logical and physical access controls; authentication and authorization; "
        "encryption of data at rest and in transit; change management; risk assessment; "
        "monitoring and incident response; secure disposal of data.",
    )


def make_availability_tsc_agent() -> Agent:
    """Create a Strands Agent for Availability TSC auditing."""
    return _make_tsc_agent(
        "Availability",
        "System availability; capacity and performance management; "
        "backup and recovery; monitoring and incident management; environmental controls.",
    )


def make_processing_integrity_tsc_agent() -> Agent:
    """Create a Strands Agent for Processing Integrity TSC auditing."""
    return _make_tsc_agent(
        "Processing Integrity",
        "Processing completeness, validity, accuracy, timeliness, and authorization; "
        "data validation; error handling; reconciliation and quality assurance of processing.",
    )


def make_confidentiality_tsc_agent() -> Agent:
    """Create a Strands Agent for Confidentiality TSC auditing."""
    return _make_tsc_agent(
        "Confidentiality",
        "Identification and classification of confidential information; "
        "disclosure only as agreed; secure handling and disposal of confidential data.",
    )


def make_privacy_tsc_agent() -> Agent:
    """Create a Strands Agent for Privacy TSC auditing."""
    return _make_tsc_agent(
        "Privacy",
        "Collection, use, retention, disclosure, and disposal of personal information; "
        "consent; data subject rights; privacy notice and policies; PII handling in code/config.",
    )


_REPORT_WRITER_SYSTEM_PROMPT = """You are a SOC2 lead auditor. You receive audit findings from five TSC specialist \
agents (Security, Availability, Processing Integrity, Confidentiality, Privacy).

Analyze all the findings from the upstream auditors and produce one of two outputs:

**If there are critical or high severity findings:**
Produce a SOC2 Compliance Audit Report as a single JSON object with:
- "report_type": "compliance_audit"
- "executive_summary": string (2-5 paragraphs: scope, overall posture, key risks, high-level recommendation)
- "scope": string (one paragraph: what was in scope)
- "findings_by_tsc": object mapping TSC category to array of finding objects
- "recommendations_summary": array of strings (prioritized remediation steps)
- "raw_markdown": string (full report in markdown)

**If there are NO critical or high severity findings:**
Produce a Next Steps for SOC2 Certification document as a single JSON object with:
- "report_type": "next_steps"
- "title": string
- "introduction": string (2-4 sentences)
- "steps": array of objects with "title" and "description"
- "recommended_timeline": string
- "raw_markdown": string (full document in markdown)

Respond with valid JSON only. No text outside JSON."""


def make_report_writer_agent() -> Agent:
    """Create a Strands Agent for SOC2 report writing (fan-in compositor)."""
    return build_agent(
        name="soc2_report_writer",
        system_prompt=_REPORT_WRITER_SYSTEM_PROMPT,
        agent_key="soc2",
        description="SOC2 report writer that synthesizes all TSC audit findings",
    )
