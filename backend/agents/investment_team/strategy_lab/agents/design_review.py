"""Strands Agent that reviews a proposed ``StrategySpec`` and emits a critique.

The reviewer sees only the spec and the deterministic readiness findings
already produced by :class:`SpecReadinessGate`; it does not see code,
and its output is a critique — never a revised spec, never code. The
orchestrator runs this agent in a bounded design-review loop, asking the
designer to ``revise`` until the reviewer's ``ready`` flag is true or
the round budget is exhausted.

Models (``CritiqueIssue`` / ``SpecCritique``) live alongside the agent
so consumers import a single module (mirrors ``alignment.py``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field
from strands import Agent

from ...models import StrategySpec
from ..quality_gates.models import QualityGateResult
from ..spec_dsl import format_rules_for_prompt, format_sizing_rule
from ._parse_helpers import extract_json_object
from .model_factory import get_strands_model

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


_CRITIQUE_FIELDS: tuple[str, ...] = (
    "entry_rules",
    "exit_rules",
    "sizing",
    "target_symbols",
    "risk_limits",
    "timeframe",
    "hypothesis",
    "signal_definition",
)


class CritiqueIssue(BaseModel):
    """A single point on which the reviewer thinks the spec is not ready.

    ``field`` names the spec field the issue applies to (or the closest
    proxy when the issue is cross-cutting). ``severity`` follows the same
    ladder as :class:`QualityGateResult` so downstream consumers can mix
    deterministic findings and reviewer critiques into one timeline.
    """

    field: str = Field(
        description=(
            "Spec field the issue applies to: one of "
            "'entry_rules' | 'exit_rules' | 'sizing' | 'target_symbols' | "
            "'risk_limits' | 'timeframe' | 'hypothesis' | 'signal_definition'."
        )
    )
    severity: Literal["info", "warning", "critical"] = "warning"
    description: str
    suggested_fix: str = ""


class SpecCritique(BaseModel):
    """Verdict from one design-review round.

    ``ready`` is the only field the orchestrator branches on. When
    ``ready=True`` the loop exits and the spec advances to code synthesis;
    when ``ready=False`` the designer's ``revise`` method must address
    every issue here before the next review round.
    """

    ready: bool
    rationale: str = ""
    issues: List[CritiqueIssue] = Field(default_factory=list)
    readiness_findings: List[str] = Field(
        default_factory=list,
        description=(
            "Snapshot of deterministic SpecReadinessGate findings the "
            "reviewer was shown, persisted so the audit trail shows the "
            "full set of inputs to the verdict."
        ),
    )
    round: int = 0


class DesignReviewError(Exception):
    """Raised when the LLM call or response parsing fails inside
    :class:`DesignReviewAgent`. The orchestrator falls closed on this
    (``ready=False`` with a synthetic critical issue) so a reviewer
    transport hiccup cannot silently advance a half-validated spec."""


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------


_REVIEW_USER_TEMPLATE = """\
Review the strategy specification below and decide whether it is implementable.

## Candidate Strategy Specification
Asset class: {asset_class}
Hypothesis: {hypothesis}
Signal definition: {signal_definition}
Timeframe: {timeframe}
Entry rules:
{entry_rules}
Exit rules:
{exit_rules}
Sizing: {sizing_rules}
Target symbols: {target_symbols}
Risk limits: {risk_limits}
Speculative: {speculative}

## Deterministic Readiness Findings ({n_readiness} run)
{readiness_block}

## Prior Critiques on this lineage ({n_prior_critiques})
{prior_critiques_block}

## Instructions

You are the only LLM in this loop. The deterministic gate already
catches mechanical errors — do not duplicate them. Focus on substantive
defects: thesis coherence, signal alignment, risk-control completeness,
universe ↔ thesis fit, sizing realism, and hand-wavy or measurable-edge-
absent specifications.

Return ONLY a JSON object — no markdown — with this shape:

{{
  "ready": false,
  "rationale": "1-3 sentences",
  "issues": [
    {{
      "field": "entry_rules | exit_rules | sizing | target_symbols | risk_limits | timeframe | hypothesis | signal_definition",
      "severity": "info | warning | critical",
      "description": "what's wrong",
      "suggested_fix": "concrete revision the designer should apply"
    }}
  ]
}}

- ``ready=true`` ONLY when no deterministic finding is critical AND you cannot identify a substantive defect.
- ``ready=false`` requires at least one entry in ``issues``.
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class DesignReviewAgent:
    """Review a proposed ``StrategySpec`` and return a :class:`SpecCritique`.

    Contract:
      Pre — ``spec`` is a constructed ``StrategySpec``;
            ``readiness_results`` is the deterministic gate output the
            orchestrator already ran on the same spec.
      Post — returns a :class:`SpecCritique`. The agent never returns
             code or a revised spec.
      Invariant — on LLM transport failure or unparseable JSON, the
             agent falls closed with ``ready=False`` and a single
             critical ``review_parse_error`` issue. This matches the
             fail-closed treatment in :class:`TradeAlignmentAgent`.
    """

    def run(
        self,
        spec: StrategySpec,
        readiness_results: Optional[List[QualityGateResult]] = None,
        prior_critiques: Optional[List[SpecCritique]] = None,
    ) -> SpecCritique:
        """Run one design-review round.

        Returns a :class:`SpecCritique`. Never raises — transport failures
        produce a fail-closed critique instead so the orchestrator's loop
        never stalls on a reviewer hiccup.
        """
        system_prompt = (_PROMPT_DIR / "design_review_system.md").read_text(encoding="utf-8")

        readiness_block, readiness_findings = _format_readiness(readiness_results or [])
        prior_block = _format_prior_critiques(prior_critiques or [])

        user_prompt = _REVIEW_USER_TEMPLATE.format(
            asset_class=spec.asset_class,
            hypothesis=spec.hypothesis,
            signal_definition=spec.signal_definition,
            timeframe=spec.timeframe,
            entry_rules=format_rules_for_prompt(spec.entry_rules),
            exit_rules=format_rules_for_prompt(spec.exit_rules),
            sizing_rules=format_sizing_rule(spec.sizing),
            target_symbols=list(spec.target_symbols),
            risk_limits=spec.risk_limits.model_dump_json(),
            speculative=spec.speculative,
            n_readiness=len(readiness_results or []),
            readiness_block=readiness_block,
            n_prior_critiques=len(prior_critiques or []),
            prior_critiques_block=prior_block,
        )

        agent = Agent(
            model=get_strands_model("strategy_design_review"),
            system_prompt=system_prompt,
            tools=[],
        )

        try:
            result = agent(user_prompt)
            parsed = extract_json_object(str(result))
        except Exception as exc:  # noqa: BLE001 — fail-closed on any LLM/parse fault
            logger.warning("DesignReviewAgent failed to produce parseable JSON: %s", exc)
            return _fail_closed_critique(exc, readiness_findings)

        critique = _coerce_critique(parsed, readiness_findings)
        return critique


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_readiness(results: List[QualityGateResult]) -> tuple[str, List[str]]:
    """Render readiness findings as a deterministic block, return findings list.

    Pre: ``results`` is a list of ``QualityGateResult``.
    Post: returns ``(block_text, findings_list)`` where ``findings_list``
    is the same shape persisted on the resulting :class:`SpecCritique`'s
    ``readiness_findings`` field.
    """
    if not results:
        return "(no findings)", []
    lines: List[str] = []
    findings: List[str] = []
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        line = f"  [{status}|{r.severity}] {r.details}"
        lines.append(line)
        findings.append(f"{r.severity}: {r.details}")
    return "\n".join(lines), findings


def _format_prior_critiques(prior: List[SpecCritique]) -> str:
    """Render past critiques so the reviewer does not re-raise resolved issues."""
    if not prior:
        return "None yet."
    lines: List[str] = []
    for c in prior:
        lines.append(
            f"  Round {c.round}: ready={c.ready} ({len(c.issues)} issues) — {c.rationale[:160]}"
        )
    return "\n".join(lines)


def _coerce_critique(parsed: Dict[str, Any], readiness_findings: List[str]) -> SpecCritique:
    """Convert a parsed LLM JSON dict into a :class:`SpecCritique`.

    Tolerant of mild schema drift so a single off-spec issue doesn't
    discard the rest of the review. Issues with unknown ``field`` values
    are remapped onto a permissive default (`hypothesis`) so the
    designer still sees the critique text.
    """
    ready = bool(parsed.get("ready", False))
    rationale = str(parsed.get("rationale", "")).strip()
    raw_issues = parsed.get("issues") or []

    issues: List[CritiqueIssue] = []
    for raw in raw_issues:
        if not isinstance(raw, dict):
            continue
        field = str(raw.get("field", "hypothesis"))
        if field not in _CRITIQUE_FIELDS:
            field = "hypothesis"
        severity_raw = str(raw.get("severity", "warning"))
        severity: Literal["info", "warning", "critical"] = (
            severity_raw if severity_raw in ("info", "warning", "critical") else "warning"
        )
        try:
            issues.append(
                CritiqueIssue(
                    field=field,
                    severity=severity,
                    description=str(raw.get("description", "")),
                    suggested_fix=str(raw.get("suggested_fix", "")),
                )
            )
        except Exception:
            # Best-effort: one bad item shouldn't bin the rest.
            continue

    # Contract: ready=False but no issues is incoherent. Synthesise a
    # placeholder so the designer's `revise()` call has something to
    # act on, rather than looping with an empty critique.
    if not ready and not issues:
        issues.append(
            CritiqueIssue(
                field="hypothesis",
                severity="warning",
                description=(rationale or "Reviewer reported not-ready without naming an issue."),
                suggested_fix="Tighten the hypothesis or rule definitions.",
            )
        )

    return SpecCritique(
        ready=ready,
        rationale=rationale,
        issues=issues,
        readiness_findings=list(readiness_findings),
    )


def _fail_closed_critique(exc: Exception, readiness_findings: List[str]) -> SpecCritique:
    """Build the fail-closed critique used when the reviewer LLM fails."""
    return SpecCritique(
        ready=False,
        rationale=(
            f"DesignReviewAgent fell closed: {type(exc).__name__}: {exc}. "
            "Treat as not-ready until the next round produces a parseable verdict."
        ),
        issues=[
            CritiqueIssue(
                field="hypothesis",
                severity="critical",
                description=(f"review_parse_error: {type(exc).__name__}: {exc}"),
                suggested_fix=(
                    "Re-emit the spec; if the failure recurs, the design loop will abort the cycle."
                ),
            )
        ],
        readiness_findings=list(readiness_findings),
    )


__all__ = [
    "CritiqueIssue",
    "DesignReviewAgent",
    "DesignReviewError",
    "SpecCritique",
]
