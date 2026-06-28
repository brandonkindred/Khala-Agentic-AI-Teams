"""Unified Security Review service for the Software Engineering team.

This module is the single source of truth for the security-review logic that
used to be duplicated across four agents at different altitudes:

* ``security_agent.CybersecurityExpertAgent`` — full-codebase OWASP pass,
* ``backend_code_v2_team`` / ``frontend_code_v2_team`` ``SecurityToolAgent`` —
  per-task review-and-fix tool agents, and
* ``devops_team.DevSecOpsReviewAgent`` — IAM/secrets/network/artifact review.

Those agents keep their public class names, ``run`` signatures, and output
models; they delegate the *shared* concerns to this service via a ``profile``
parameter:

* the one canonical severity ordering and blocking rule
  (:func:`is_blocking`, :func:`any_blocking`, :func:`derive_approved`),
* profile-parameterized review-prompt assembly (:func:`build_review_prompt`):
  the ``code`` profile takes a backend/frontend *focus* list, the ``infra``
  profile reviews DevOps artifacts, and
* the optional policy-as-code (checkov) hook used by the ``infra`` gate
  (:func:`run_policy_scan`, :func:`infra_gate_passed`).

The full-codebase OWASP *system* prompt deliberately stays in
``security_agent/prompts.py`` because the ``DummyLLMClient`` test stub
pattern-matches on its anchor words; only the shared *logic* is centralized
here, not that one large prompt body.

Invariants:
    * ``SEVERITY_ORDER`` lists severities most-to-least severe; severities not
      listed (e.g. ``"minor"``/``"nit"``) rank below every listed one.
    * A finding blocks approval iff its severity is in
      :data:`BLOCKING_SEVERITIES` or it is explicitly flagged ``blocking``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Iterable, Optional

# --- Canonical severity model (single source of truth) -------------------
# Ordered most-severe first. The two structured agents emit only these five;
# devops ``ReviewFinding`` additionally allows "minor"/"nit", which rank below
# "low" and never block.
SEVERITY_ORDER: tuple[str, ...] = ("critical", "high", "medium", "low", "info")
BLOCKING_SEVERITIES: frozenset[str] = frozenset({"critical", "high"})

FALSE_POSITIVE_GUIDANCE: str = (
    "Be thorough but avoid false positives. Only report issues you can justify "
    "with a concrete attack vector and impact. Each recommendation must be actionable."
)

# --- Focus lists (lifted verbatim from the legacy prompts) ---------------
# The ``code`` profile differs between backend and frontend only by this list;
# the surrounding template is shared.
CODE_BACKEND_FOCUS: str = (
    "1. Injection — SQL, command, or template injection; unsanitized user input.\n"
    "2. Authentication/authorisation — bypass risks, weak or missing checks, privilege escalation.\n"
    "3. Secrets — hardcoded credentials, API keys, or tokens in code or config.\n"
    "4. Insecure defaults — weak crypto, missing HTTPS, or permissive CORS.\n"
    "5. Input validation and output encoding — missing or insufficient sanitisation."
)
CODE_FRONTEND_FOCUS: str = (
    "1. XSS — unescaped user input in DOM, innerHTML, or template interpolation.\n"
    "2. Sensitive data — tokens, keys, or PII in client code, localStorage, or URLs.\n"
    "3. Insecure forms — missing CSRF protection, weak validation, or credentials over HTTP.\n"
    "4. Dependency risks — known vulnerable packages or unsafe eval/Function usage.\n"
    "5. Content Security Policy (CSP) or secure headers not applied where needed."
)
INFRA_FOCUS: str = (
    "- IAM least privilege and trust policy safety\n"
    "- CI token/job privilege boundaries\n"
    "- secret handling and credential exposure\n"
    "- network exposure and insecure defaults\n"
    "- artifact integrity controls (scan/SBOM/signing references)"
)


class SecurityProfile(str, Enum):
    """Context profile selecting which surface a security review targets.

    Invariants: the two members exhaust the supported contexts; ``CODE`` is an
    application-diff (SAST-style) review parameterized by a focus list, while
    ``INFRA`` is a DevOps-artifact (IAM/secrets/network) review.
    """

    CODE = "code"
    INFRA = "infra"


# --- Review-prompt templates (one copy each) -----------------------------
# Keeps the literal ``{task_description}``/``{code}`` slots so the tool-agent
# lifecycle's ``review_prompt.format(task_description=..., code=...)`` still
# works after substitution; only ``{focus}`` is substituted here (via
# ``str.replace`` so the other braces are left untouched).
_CODE_REVIEW_TEMPLATE: str = """You are an expert Security specialist. Review the code from a security perspective only.

Focus on:
{focus}

**Task context:**
{task_description}

**Code to review:**
{code}

**Output format (template – use exactly these section headers):**

## PASSED ##
true
## END PASSED ##
## ISSUES ##
---
source: security
severity: critical|high|medium|low|info
description: what is wrong from a security perspective
file_path: which file
recommendation: how to fix it
---
## END ISSUES ##
## SUMMARY ##
brief security assessment
## END SUMMARY ##

- Use "---" to separate each issue block. Use source: security for every issue. Omit ## ISSUES ## / ## END ISSUES ## if there are no issues.
- Do not use JSON. Use only the template above. No explanatory text before or after.
"""

_INFRA_REVIEW_TEMPLATE: str = """You are DevSecOpsReviewAgent.

Review DevOps artifacts for:
{focus}

Output JSON:
- approved: boolean (false if any blocking finding exists)
- findings: list of ReviewFinding fields:
  finding_id, severity, area, file_ref, issue, rationale, recommended_fix, blocking, exploitability
- summary: string

Set blocking=true for high-risk exploitable defaults.
Return JSON only.
"""


def severity_rank(severity: str) -> int:
    """Return a stable sort index for ``severity`` (lower == more severe).

    Preconditions:
        ``severity`` is a string (case-insensitive); empty/unknown values are
        accepted and ranked last.
    Postconditions:
        Returns the index of ``severity`` in :data:`SEVERITY_ORDER`, or
        ``len(SEVERITY_ORDER)`` for any value not listed there (so unknown
        severities sort after every known one). Pure; no side effects.
    """
    try:
        return SEVERITY_ORDER.index((severity or "").strip().lower())
    except ValueError:
        return len(SEVERITY_ORDER)


def is_blocking(severity: str, *, explicit_blocking: bool = False) -> bool:
    """Return whether a single finding blocks approval.

    Preconditions:
        ``severity`` is a string (case-insensitive); ``explicit_blocking`` is a
        bool reflecting a finding's own hard-blocker flag.
    Postconditions:
        Returns ``True`` iff ``explicit_blocking`` is true or ``severity`` (case
        folded) is in :data:`BLOCKING_SEVERITIES`. Pure; no side effects.
    """
    if explicit_blocking:
        return True
    return (severity or "").strip().lower() in BLOCKING_SEVERITIES


def any_blocking(
    findings: Iterable[Any],
    *,
    severity_attr: str = "severity",
    blocking_attr: str = "blocking",
) -> bool:
    """Return whether *any* finding in ``findings`` blocks approval.

    Reads each finding's severity via ``getattr(f, severity_attr, "")`` and its
    explicit hard-blocker flag via ``getattr(f, blocking_attr, False)`` — so it
    works for both ``SecurityVulnerability`` (no ``blocking`` attribute) and
    ``ReviewFinding`` (has one).

    Preconditions:
        ``findings`` is iterable; each item exposes ``severity_attr`` (and
        optionally ``blocking_attr``) as attributes.
    Postconditions:
        Returns ``True`` iff at least one finding satisfies :func:`is_blocking`.
        An empty iterable yields ``False``. Pure; no side effects.
    """
    return any(
        is_blocking(
            getattr(f, severity_attr, ""),
            explicit_blocking=bool(getattr(f, blocking_attr, False)),
        )
        for f in findings
    )


def derive_approved(
    findings: Iterable[Any],
    *,
    llm_approved: Optional[bool] = None,
    severity_attr: str = "severity",
    blocking_attr: str = "blocking",
) -> bool:
    """Compute the canonical ``approved`` flag from a finding set.

    This reconciles the two legacy variants into one rule:
        * if any finding blocks (:func:`any_blocking`), approval is ``False``
          regardless of what the LLM claimed;
        * otherwise, honor ``llm_approved`` when the model supplied it;
        * otherwise default to approved.

    Preconditions:
        ``findings`` is iterable of items exposing ``severity_attr`` (and
        optionally ``blocking_attr``); ``llm_approved`` is ``True``/``False`` to
        defer to the model, or ``None`` when the model gave no explicit flag.
    Postconditions:
        Returns ``False`` when :func:`any_blocking` is true; else
        ``bool(llm_approved)`` when ``llm_approved is not None``; else ``True``.
        Pure; no side effects.
    """
    if any_blocking(findings, severity_attr=severity_attr, blocking_attr=blocking_attr):
        return False
    if llm_approved is not None:
        return bool(llm_approved)
    return True


def build_review_prompt(
    profile: SecurityProfile | str,
    *,
    focus: Optional[str] = None,
) -> str:
    """Assemble the review prompt for a security ``profile``.

    Preconditions:
        * ``profile`` is a :class:`SecurityProfile` or its string value
          (``"code"``/``"infra"``).
        * For ``CODE``, ``focus`` is a non-empty focus list (use
          :data:`CODE_BACKEND_FOCUS` / :data:`CODE_FRONTEND_FOCUS`).
        * For ``INFRA``, ``focus`` must be ``None`` (the profile uses the fixed
          :data:`INFRA_FOCUS`).
    Postconditions:
        Returns a non-empty prompt string. The ``CODE`` prompt preserves the
        literal ``{task_description}`` and ``{code}`` slots so the tool-agent
        lifecycle can ``.format(...)`` them later; the ``INFRA`` prompt requests
        JSON-only output. Pure; no side effects.
    """
    profile = SecurityProfile(profile)
    if profile is SecurityProfile.CODE:
        if not (focus and focus.strip()):
            raise ValueError("code profile requires a non-empty 'focus' list")
        return _CODE_REVIEW_TEMPLATE.replace("{focus}", focus)
    # INFRA
    if focus is not None:
        raise ValueError("infra profile does not accept a 'focus' argument")
    return _INFRA_REVIEW_TEMPLATE.replace("{focus}", INFRA_FOCUS)


def infra_gate_passed(devsec_approved: bool, policy_success: bool) -> bool:
    """Combine the infra security-review and policy-as-code results.

    Centralizes the infra ``security_review`` gate decision so the DevOps
    orchestrator routes both the LLM review and the checkov scan through one
    rule.

    Preconditions:
        ``devsec_approved`` and ``policy_success`` are bools (the former from a
        ``DevSecOpsReviewOutput.approved``, the latter from a
        ``PolicyAsCodeOutput.success``).
    Postconditions:
        Returns ``True`` iff both inputs are truthy. Pure; no side effects.
    """
    return bool(devsec_approved) and bool(policy_success)


def run_policy_scan(repo_path: str, *, runner: Any = None) -> Any:
    """Run the policy-as-code (checkov) scan over a repository.

    Lazily constructs the default :class:`PolicyAsCodeToolAgent` so importing
    this module never pulls in the checkov tooling unless a scan is requested;
    callers/tests may inject ``runner`` to substitute a stub.

    Preconditions:
        ``repo_path`` is a non-empty path string; ``runner`` (when provided)
        exposes ``run(PolicyAsCodeInput) -> PolicyAsCodeOutput``.
    Postconditions:
        Returns the ``PolicyAsCodeOutput`` produced by the runner (carrying
        ``success``/``checks``/``findings``). No other side effects beyond the
        scan the runner performs.
    """
    assert repo_path and repo_path.strip(), "repo_path is required"
    from devops_team.tool_agents.policy_as_code import (
        PolicyAsCodeInput,
        PolicyAsCodeToolAgent,
    )

    runner = runner if runner is not None else PolicyAsCodeToolAgent()
    return runner.run(PolicyAsCodeInput(repo_path=repo_path))
