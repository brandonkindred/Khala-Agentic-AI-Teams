"""SOC2 Trust Service Criteria audit agents and report writer.

The class-based agents (``SecurityTSCAgent`` … ``PrivacyTSCAgent`` and
``ReportWriterAgent``) each perform one reasoning pass followed by one JSON
formatting pass via :func:`complete_json_via_reasoning` and return a typed
result. They are the audit pipeline's units of work: the thread-mode
orchestrator and the Temporal activities both drive them via
:mod:`soc2_compliance_team.pipeline`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from llm_service import compact_text, complete_json_via_reasoning

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

# Context-window budget for the TSC reasoning prompt: reserve tokens for the
# prompt template + response, convert the remainder to chars, then split
# between README and code with a README hard cap.
_RESPONSE_RESERVE_TOKENS = 8000
_CHARS_PER_TOKEN_ESTIMATE = 3.5
_README_BUDGET_FRACTION = 4
_README_MAX_CHARS = 200_000
_DEFAULT_CONTEXT_TOKENS = 16384

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

Transcribe the analysis below faithfully — do not add or invent information
beyond it. In particular: do not introduce findings it does not state, do not
fill in file paths it does not name (leave "location" empty instead), and do
not change its compliance verdict.
"""
# Formatting-pass (think=False) instructions: the JSON shape plus a
# transcribe-only guard.
#
# This pass receives ONLY the reasoning pass's prose — never the repository
# content — so it cannot audit anything. The investigative directives ("cite
# repo content", "report that as a finding", "do not invent file paths") live
# in _TSC_REASONING_INSTRUCTION, which reaches the pass that actually sees
# the repo. Leaving them here invited a transcription-only call to invent a
# location or a spurious critical/high finding, which would flip
# ``compliant``.

_TSC_REASONING_INSTRUCTION = """
Think through this carefully, then write your audit as structured prose (not JSON): a short
summary of your audit for this criterion, followed by one entry per finding covering severity
(critical/high/medium/low/informational), title, description, location (file path, module, or
area — empty if general), recommendation, and the evidence you observed. Finally, state whether
the repo is compliant for this criterion (compliant only if there are no critical or high
severity findings).

Be specific and cite repo content where possible. If the repo has no relevant evidence (e.g. no
auth code for Security), report that as a finding (e.g. "No authentication/authorization
implementation found"). Do not invent file paths.
"""


def _parse_finding(d: Dict[str, Any], category: TSCCategory) -> TSCFinding:
    """Build TSCFinding from LLM response dict.

    Preconditions:
        * ``d`` is a mapping produced by parsing the LLM's JSON response for
          one finding (arbitrary/untrusted keys and value types — the LLM may
          omit fields, use unexpected casing, or send non-string values).
        * ``category`` is the :class:`TSCCategory` this finding was raised
          under (supplied by the caller, not read from ``d``).
    Postconditions:
        * Always returns a :class:`TSCFinding` — never raises. Missing or
          falsy string fields (``title``, ``description``, ``location``,
          ``recommendation``, ``evidence_observed``) default to ``"Untitled"``
          (title) or ``""`` (the rest). An unparseable/unrecognized
          ``severity`` (including non-string values) defaults to
          ``FindingSeverity.MEDIUM`` rather than raising.
    """
    # str() before .lower(): the value comes straight from an LLM response, so
    # a non-string (e.g. a bare int severity) must not crash the audit — it
    # falls through to the ValueError branch and defaults to MEDIUM.
    sev = str(d.get("severity") or "medium").lower()
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
    """Generic TSC audit: one criterion, two LLM calls (reasoning + formatting), return TSCAuditResult.

    Preconditions:
        * ``llm`` is an ``LLMClient``-compatible object usable by
          :func:`complete_json_via_reasoning` and :func:`compact_text`
          (``get_max_context_tokens`` is optional — falls back to
          ``_DEFAULT_CONTEXT_TOKENS`` when absent).
        * ``category`` is a valid :class:`TSCCategory` value;
          ``criterion_name`` and ``focus_areas`` are non-empty strings
          describing the TSC criterion being audited.
        * ``context`` is a fully-populated :class:`RepoContext` for the
          repository under audit.
    Postconditions:
        * Returns a :class:`TSCAuditResult` for ``category``. ``findings`` is
          built only from entries in the parsed response's ``findings`` list
          that are dicts with a non-empty ``title`` or ``description``
          (malformed/empty entries are silently dropped, never raised on).
        * ``compliant`` is always computed deterministically from
          ``findings``: ``True`` unless at least one parsed finding is
          CRITICAL or HIGH severity. The LLM's own ``compliant`` value is
          never used for the result — only compared against the computed
          value to log a warning on mismatch — so a null, non-boolean, or
          findings-contradicting LLM verdict can never affect or crash this
          function.
        * Propagates whatever :func:`complete_json_via_reasoning` raises
          (e.g. on LLM/transport failure) — this function does not catch or
          degrade those failures itself.
    """
    # Compute budgets from model context: reserve tokens for prompt template + response
    ctx_tokens = (
        llm.get_max_context_tokens()
        if hasattr(llm, "get_max_context_tokens")
        else _DEFAULT_CONTEXT_TOKENS
    )
    available_tokens = max(ctx_tokens - _RESPONSE_RESERVE_TOKENS, 0)
    total_chars = int(available_tokens * _CHARS_PER_TOKEN_ESTIMATE)
    readme_budget = min(total_chars // _README_BUDGET_FRACTION, _README_MAX_CHARS)
    code_budget = total_chars - readme_budget
    reasoning_prompt = f"""You are a SOC2 auditor specializing in the **{criterion_name}** Trust Service Criterion.
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
{_TSC_REASONING_INSTRUCTION}"""

    data = complete_json_via_reasoning(
        llm,
        reasoning_prompt=reasoning_prompt,
        reasoning_system_prompt=None,
        formatting_instructions=_TSC_OUTPUT_FORMAT,
        reasoning_temperature=0.1,
        objective="evaluate soc2 control",
    )
    summary = data.get("summary") or ""
    findings_raw = data.get("findings") or []
    findings = []
    for f in findings_raw:
        if isinstance(f, dict) and (f.get("title") or f.get("description")):
            findings.append(_parse_finding(f, category))
    has_critical_or_high = any(
        f.severity in (FindingSeverity.CRITICAL, FindingSeverity.HIGH) for f in findings
    )
    computed_compliant = not has_critical_or_high
    llm_compliant = data.get("compliant")
    if isinstance(llm_compliant, bool) and llm_compliant != computed_compliant:
        logger.warning(
            "LLM compliance verdict %s inconsistent with findings; using computed value %s",
            llm_compliant,
            computed_compliant,
        )
    compliant = computed_compliant
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
        """Synthesize the fan-in report from all TSC audit results.

        Preconditions:
            - ``tsc_results`` is the list of per-criterion audit results; it
              may be empty (e.g. all criteria failed and were isolated to
              fail-closed placeholders upstream, or a caller invokes this with
              nothing to report).
        Postconditions:
            - Returns ``(compliance_report, next_steps_document)`` with exactly
              one element non-None: the compliance report when any result is
              non-compliant or has a critical/high finding, otherwise the
              next-steps document. An empty ``tsc_results`` has no findings by
              definition, so it always takes the next-steps branch.
            - Delegates the LLM work and its call contract to
              ``_produce_compliance_report`` / ``_produce_next_steps`` (see
              their own docstrings); this method makes no LLM call itself.
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
            # ``mode="json"`` so enum fields serialize to their string values
            # (e.g. "high"), not enum reprs like ``<FindingSeverity.HIGH: 'high'>``,
            # which is what the report-writer prompt renders as "JSON".
            findings_by_tsc[r.category.value] = [f.model_dump(mode="json") for f in r.findings]

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
        """Run the reasoning + formatting passes and assemble the compliance report.

        Preconditions:
            * ``tsc_results`` is the list of per-criterion audit results that
              produced ``findings_by_tsc`` (already-serialized, one entry per
              criterion in ``tsc_results``, in the same category order).
            * ``findings_by_tsc`` is the JSON-serialized form of
              ``tsc_results``'s findings, embedded in the reasoning prompt so
              the model can cite them.
        Postconditions:
            * Performs exactly two sequential LLM calls via
              :func:`complete_json_via_reasoning`: a reasoning prose pass
              (``think=True``) followed by a JSON formatting pass
              (``think=False``) that supplies ``executive_summary``, ``scope``,
              ``recommendations_summary``, and ``raw_markdown``.
            * ``findings_by_tsc`` on the returned report is built directly from
              ``tsc_results`` — the already-typed ``TSCFinding`` objects the
              caller passed in — never from the LLM's formatting-pass output;
              the formatting pass is not asked to reproduce findings, so there
              is nothing to re-validate here.
        """
        summaries = "\n".join(f"- **{r.category.value}**: {r.summary}" for r in tsc_results)
        reasoning_prompt = f"""You are a SOC2 lead auditor. Produce a **SOC2 Compliance Audit Report** for the following audit results.

**Repository:** {repo_path}

**Per-criterion summaries:**
{summaries}

**Findings by category (JSON):**
{json.dumps(findings_by_tsc, indent=2)}

Think this through, then write the report as structured prose covering: an executive summary
(scope, overall posture, key risks, and high-level recommendation), the scope of what was
audited, prioritized remediation recommendations (ordered by impact), and the full report body
(title, executive summary, scope, findings by TSC with severity and recommendation, then the
recommendations)."""

        data = complete_json_via_reasoning(
            llm,
            reasoning_prompt=reasoning_prompt,
            reasoning_system_prompt=None,
            formatting_instructions=(
                "Write a single JSON object with:\n"
                '- "executive_summary": string (2–5 paragraphs: scope, overall posture, key risks, and high-level recommendation)\n'
                '- "scope": string (one paragraph: what was in scope)\n'
                '- "recommendations_summary": array of strings (prioritized remediation steps, ordered by impact)\n'
                '- "raw_markdown": string (full report in markdown: title, executive summary, scope, '
                "findings by TSC with severity and recommendation, then recommendations summary)\n\n"
                "Respond with valid JSON only. No text outside JSON. "
                "Transcribe the analysis below faithfully — do not add or invent "
                "information beyond it."
            ),
            reasoning_temperature=0.2,
            objective="generate soc2 report",
        )
        # Sourced from tsc_results (already-typed TSCFinding objects), not from
        # the LLM's formatting-pass output — the formatting pass only supplies
        # executive_summary/scope/recommendations_summary/raw_markdown, so
        # re-parsing findings_by_tsc's serialized dicts back into TSCFinding
        # here would be a redundant, purely-input round-trip.
        findings_typed: Dict[str, List[TSCFinding]] = {
            r.category.value: r.findings for r in tsc_results
        }
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
        """Run the reasoning + formatting passes and assemble the next-steps document.

        Preconditions:
            * ``tsc_results`` is the list of per-criterion audit results with
              no material findings (``run`` only calls this on that branch);
              it may be empty.
        Postconditions:
            * Performs exactly two sequential LLM calls via
              :func:`complete_json_via_reasoning`: a reasoning prose pass
              (``think=True``) followed by a JSON formatting pass
              (``think=False``) that supplies ``title``, ``introduction``,
              ``steps``, ``recommended_timeline``, and ``raw_markdown``.
        """
        summaries = "\n".join(f"- **{r.category.value}**: {r.summary}" for r in tsc_results)
        reasoning_prompt = f"""You are a SOC2 advisor. The following code repository was audited and **no material SOC2 compliance issues** were found. Produce a short document: "Next Steps for SOC2 Certification".

**Repository:** {repo_path}

**Audit summaries per criterion:**
{summaries}

Think this through, then write the document as structured prose: a title, a short introduction
(2-4 sentences on the audit result and what this document covers), the recommended steps (e.g.
engage CPA firm, scope examination, document controls, collect evidence, Type I then Type II —
each with a title and description, and optionally resources), and a high-level recommended
timeline."""

        data = complete_json_via_reasoning(
            llm,
            reasoning_prompt=reasoning_prompt,
            reasoning_system_prompt=None,
            formatting_instructions=(
                "Write a single JSON object with:\n"
                '- "title": string (e.g. "Next Steps for SOC2 Certification")\n'
                '- "introduction": string (2–4 sentences: codebase audit result and what this document covers)\n'
                '- "steps": array of objects, each with "title" and "description" (and optionally "resources"), '
                "e.g. engage CPA firm, scope examination, document controls, collect evidence, Type I then Type II\n"
                '- "recommended_timeline": string (high-level timeline, e.g. "3–6 months readiness, then 2–4 months '
                'for Type I/II examination")\n'
                '- "raw_markdown": string (full document in markdown for display/saving)\n\n'
                "Respond with valid JSON only. No text outside JSON. "
                "Transcribe the analysis below faithfully — do not add or invent "
                "information beyond it."
            ),
            reasoning_temperature=0.2,
            objective="produce soc2 next steps",
        )
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
