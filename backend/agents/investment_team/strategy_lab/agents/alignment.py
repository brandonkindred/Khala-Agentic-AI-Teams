"""Strands Agent that adjudicates entry-signal near-misses and proposes
Python code patches for misalignments surfaced by the deterministic
:class:`DeterministicAlignmentChecker`.

Used by :class:`StrategyLabOrchestrator` after the code-refinement loop
has produced a runnable backtest. The orchestrator drives a
problem-solving loop (up to ``MAX_ALIGNMENT_ROUNDS``) that:

  1. Runs the deterministic gate over the executed trade ledger.
  2. If aligned, exits the loop.
  3. If misaligned, asks this agent's :meth:`propose_code_fix` for a
     rewritten Python file grounded in the structured findings, then
     re-executes through the sandbox.

The deterministic gate may additionally consult
:meth:`adjudicate_near_miss` mid-check when an entry-signal predicate
misses within ``STRATEGY_LAB_ALIGNMENT_NEAR_MISS_PCT`` (default 1%
relative), so a one-tick / float-noise miss does not produce a
critical finding.

The previous prose-driven ``run(spec, code, trades, metrics)`` audit
entry-point has been removed; its job is now split across the gate
(``aligned`` verdict, per-rule findings) and ``propose_code_fix``
(code patch).
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field
from strands import Agent

from ..alignment_findings import AlignmentFinding, NearMissVerdict
from ..spec_dsl import format_rules_for_prompt, format_sizing_rule
from ._llm_envelope import invoke_agent
from ._response_schemas import ALIGNMENT_FIX_SCHEMA
from .model_factory import get_strands_model

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _alignment_max_attempts() -> int:
    """Resolve the envelope attempt count for the alignment fix-proposer.

    Driven by ``STRATEGY_LAB_ALIGNMENT_RETRIES`` (default ``2`` → 3 attempts
    total), preserving the historical alignment retry semantics now that the
    retry+backoff loop lives inside the LLM envelope rather than the
    orchestrator. Garbage / sub-zero values fall back to the default.

    Postconditions: returns an int ``>= 1``.
    """
    try:
        retries = max(int(os.environ.get("STRATEGY_LAB_ALIGNMENT_RETRIES", "2")), 0)
    except ValueError:
        retries = 2
    return retries + 1


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class AlignmentIssue(BaseModel):
    """One way the executed trades diverged from the spec.

    Mirrors a :class:`AlignmentFinding` row at the prompt level so the
    downstream analysis agents (``analysis.py``) keep a stable contract.
    ``rule_type`` is the high-level category the deterministic gate's
    ``check_name`` rolls up into:

      - ``"entry_rules"`` ← ``side`` / ``entry_signal``
      - ``"exit_rules"``  ← ``stop_loss`` / ``take_profit`` / ``signal_exit``
      - ``"sizing_rules"`` ← ``sizing``
      - ``"universe"``    ← ``universe``
      - ``"direction"``   ← (legacy alias for ``side``)
      - ``"risk_limits"`` ← (reserved for future gates)
    """

    rule_type: str = Field(
        description=(
            "Which part of the spec the trade ledger violated: "
            "'entry_rules' | 'exit_rules' | 'sizing_rules' | 'risk_limits' | "
            "'universe' | 'direction'."
        )
    )
    description: str
    severity: Literal["info", "warning", "critical"] = "warning"
    affected_trades: List[int] = Field(default_factory=list)


class TradeAlignmentReport(BaseModel):
    """Verdict carried between the alignment gate, the LLM patch
    proposer, and the orchestrator's loop body.

    The shape is unchanged from the prior LLM-only design so existing
    analysis prompts and the synthetic-veto path in
    ``orchestrator._resolve_alignment_report_for_analysis`` keep working.
    The new ``alignment_findings`` field carries the per-rule
    deterministic ledger so the persisted ``BacktestRecord`` and the
    Strategy Lab dashboard can surface fine-grained pass/fail rows.
    """

    aligned: bool
    rationale: str = ""
    issues: List[AlignmentIssue] = Field(default_factory=list)
    proposed_code: Optional[str] = None
    predicted_aligned_after_fix: bool = False
    changes_made: str = ""
    alignment_findings: List[AlignmentFinding] = Field(default_factory=list)


class AlignmentAuditError(Exception):
    """Raised by :class:`TradeAlignmentAgent` when an LLM call or its
    response parsing fails. The orchestrator catches this in its
    retry wrapper so the call can be retried, and falls closed
    once retries are exhausted (deterministic-gate verdict still
    drives ``aligned``)."""


# ---------------------------------------------------------------------------
# Mapping from deterministic check_name to AlignmentIssue.rule_type
# ---------------------------------------------------------------------------


_CHECK_TO_RULE_TYPE: Dict[str, str] = {
    "universe": "universe",
    "side": "direction",
    "sizing": "sizing_rules",
    "stop_loss": "exit_rules",
    "take_profit": "exit_rules",
    "signal_exit": "exit_rules",
    "entry_signal": "entry_rules",
}


def findings_to_issues(findings: List[AlignmentFinding]) -> List[AlignmentIssue]:
    """Aggregate a finding ledger into one :class:`AlignmentIssue` per
    spec category, preserving the affected trade indices.

    Only ``passed=False`` rows produce issues; ``info`` rows of
    passing checks are diagnostic and would only noise up the
    downstream analysis prompt.
    """
    by_rule_type: Dict[str, AlignmentIssue] = {}
    by_severity: Dict[str, str] = {}
    severity_rank = {"info": 0, "warning": 1, "critical": 2}
    for f in findings:
        if f.passed:
            continue
        rule_type = _CHECK_TO_RULE_TYPE.get(f.check_name, "entry_rules")
        issue = by_rule_type.get(rule_type)
        if issue is None:
            issue = AlignmentIssue(
                rule_type=rule_type,
                description=f.details,
                severity=f.severity,
                affected_trades=[f.trade_num],
            )
            by_rule_type[rule_type] = issue
            by_severity[rule_type] = f.severity
        else:
            # Take the worst severity and append the trade index;
            # description grows monotonically with one new line per
            # finding so the downstream prompt sees every cited
            # divergence.
            if severity_rank[f.severity] > severity_rank[by_severity[rule_type]]:
                issue.severity = f.severity
                by_severity[rule_type] = f.severity
            issue.description = f"{issue.description}\n{f.details}"
            if f.trade_num not in issue.affected_trades:
                issue.affected_trades.append(f.trade_num)
    return list(by_rule_type.values())


def synthesize_aligned_report(findings: List[AlignmentFinding]) -> TradeAlignmentReport:
    """Construct an ``aligned=True`` report from a passing deterministic ledger.

    Used by the orchestrator when the gate returns aligned: the report
    keeps the per-rule findings so the persisted record and the
    Strategy Lab dashboard can still render the green check rows.
    """
    return TradeAlignmentReport(
        aligned=True,
        rationale="Deterministic alignment gate passed all critical checks.",
        issues=[],
        proposed_code=None,
        predicted_aligned_after_fix=False,
        changes_made="",
        alignment_findings=findings,
    )


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------


_PROPOSE_FIX_USER_TEMPLATE = """\
Rewrite the strategy code so the next backtest run satisfies every
critical finding listed below. The deterministic alignment gate has
already enumerated the misalignment — your job is the code patch.

## Strategy Specification (source of truth, immutable)
Asset class: {asset_class}
Hypothesis: {hypothesis}
Signal definition: {signal_definition}
Target symbols: {target_symbols}
Entry rules: {entry_rules}
Exit rules: {exit_rules}
Sizing rules: {sizing_rules}
Risk limits: {risk_limits}

## Critical Findings ({n_critical} critical, {n_info_warning} info/warning)
{findings_section}

## Current Strategy Code
```python
{strategy_code}
```

## Prior Alignment-Fix Attempts ({n_prior_attempts} so far)
{prior_attempts_text}

Return ONLY a JSON object with no markdown.
"""


_NEAR_MISS_USER_TEMPLATE = """\
Adjudicate this single entry-signal near-miss.

rule_id:         {rule_id}
predicate:       {predicate_repr}
computed_value:  {computed_value:.10g}
threshold:       {threshold:.10g}
symbol:          {symbol}
entry_date:      {entry_date}

Return ONLY a JSON object like:
{{"legitimate": <bool>, "rationale": "<one sentence>"}}
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class TradeAlignmentAgent:
    """Narrow LLM partner for the deterministic alignment gate.

    Two entry points:

    - :meth:`adjudicate_near_miss` — single yes/no decision on whether a
      predicate that missed within tolerance should still count as
      satisfied. No code generation. Called from the gate when a
      check #7 evaluation is a tight numerical near-miss.
    - :meth:`propose_code_fix` — produces a rewritten Python file given
      a structured findings ledger and the current strategy code.
      Called from the orchestrator's alignment-loop body when the
      gate's verdict is misaligned.

    The agent never reads the trade ledger directly anymore — every
    determination is grounded in either a single predicate evaluation
    (near-miss) or the per-rule deterministic findings (fix proposal).
    """

    def adjudicate_near_miss(
        self,
        *,
        rule_id: str,
        predicate_repr: str,
        computed_value: float,
        threshold: float,
        symbol: str,
        entry_date: str,
    ) -> NearMissVerdict:
        """Return a yes/no verdict on whether the near-miss is legitimate.

        Pre: ``computed_value`` and ``threshold`` are finite floats; the
        deterministic gate has already confirmed the relative miss is
        within ``STRATEGY_LAB_ALIGNMENT_NEAR_MISS_PCT``.
        Post: returns a :class:`NearMissVerdict`. On parse failure or
        transport error, raises :class:`AlignmentAuditError` (the
        gate wraps this with a fail-closed default).
        """
        system_prompt = (_PROMPT_DIR / "alignment_near_miss.md").read_text(encoding="utf-8")
        user_prompt = _NEAR_MISS_USER_TEMPLATE.format(
            rule_id=rule_id,
            predicate_repr=predicate_repr,
            computed_value=computed_value,
            threshold=threshold,
            symbol=symbol,
            entry_date=entry_date,
        )
        agent = Agent(
            model=get_strands_model("strategy_ideation"),
            system_prompt=system_prompt,
            tools=[],
        )
        try:
            raw = invoke_agent(
                agent,
                user_prompt,
                agent_key="strategy_ideation",
                phase="alignment_near_miss",
                logger=logger,
            )
            parsed = _extract_json(raw)
        except Exception as exc:
            logger.debug(
                "Near-miss adjudicator failed to produce parseable JSON: %s",
                exc,
                exc_info=True,
            )
            raise AlignmentAuditError(f"{type(exc).__name__}: {exc}") from exc

        legitimate = _parse_legitimate(parsed.get("legitimate", False))
        rationale = str(parsed.get("rationale", "")).strip()
        return NearMissVerdict(legitimate=legitimate, rationale=rationale)

    def propose_code_fix(
        self,
        *,
        spec: Any,
        code: str,
        findings: List[AlignmentFinding],
        prior_attempts: Optional[List[str]] = None,
    ) -> TradeAlignmentReport:
        """Produce a rewritten strategy file grounded in deterministic findings.

        Pre: ``findings`` contains at least one ``severity="critical",
        passed=False`` row; ``code`` is the most-recently-executed
        strategy source (parses and runs cleanly).
        Post: returns a :class:`TradeAlignmentReport` with
        ``aligned=False`` and ``proposed_code`` set to the rewritten
        Python file. ``alignment_findings`` is preserved verbatim so
        downstream consumers see the same ledger the gate emitted.
        On LLM transport / parse failure, raises
        :class:`AlignmentAuditError`; the orchestrator's retry wrapper
        catches and falls closed.
        """
        # Loaded raw on purpose — the propose-fix prompt contains
        # literal ``{...}`` patterns in code examples, so it must not
        # pass through ``str.format``.
        system_prompt = (_PROMPT_DIR / "alignment_propose_fix.md").read_text(encoding="utf-8")

        critical = [f for f in findings if f.severity == "critical" and not f.passed]
        info_warning = [f for f in findings if f not in critical]
        prior_text = (
            "None yet."
            if not prior_attempts
            else "\n".join(f"  Round {i + 1}: {a}" for i, a in enumerate(prior_attempts))
        )

        user_prompt = _PROPOSE_FIX_USER_TEMPLATE.format(
            asset_class=getattr(spec, "asset_class", "?"),
            hypothesis=getattr(spec, "hypothesis", "?"),
            signal_definition=getattr(spec, "signal_definition", "?"),
            target_symbols=list(getattr(spec, "target_symbols", []) or []),
            entry_rules=format_rules_for_prompt(getattr(spec, "entry_rules", []) or []),
            exit_rules=format_rules_for_prompt(getattr(spec, "exit_rules", []) or []),
            sizing_rules=format_sizing_rule(spec.sizing)
            if getattr(spec, "sizing", None) is not None
            else "(none)",
            risk_limits=spec.risk_limits.model_dump_json()
            if hasattr(getattr(spec, "risk_limits", None), "model_dump_json")
            else str(getattr(spec, "risk_limits", "")),
            n_critical=len(critical),
            n_info_warning=len(info_warning),
            findings_section=_format_findings_section(findings),
            strategy_code=code,
            n_prior_attempts=len(prior_attempts) if prior_attempts else 0,
            prior_attempts_text=prior_text,
        )

        agent = Agent(
            model=get_strands_model("strategy_ideation", response_schema=ALIGNMENT_FIX_SCHEMA),
            system_prompt=system_prompt,
            tools=[],
        )
        try:
            raw = invoke_agent(
                agent,
                user_prompt,
                agent_key="strategy_ideation",
                phase="alignment_propose_fix",
                max_attempts=_alignment_max_attempts(),
                logger=logger,
            )
            parsed = _extract_json(raw)
        except Exception as exc:
            logger.debug(
                "Alignment fix proposer failed to produce parseable JSON: %s",
                exc,
                exc_info=True,
            )
            raise AlignmentAuditError(f"{type(exc).__name__}: {exc}") from exc

        # ``preserve_proposed_code=True``: the deterministic gate has
        # already decided misaligned, so an LLM that over-claims
        # ``aligned=True`` while supplying a patch should not silently
        # drop the patch. The orchestrator clamps ``aligned`` to
        # ``False`` itself and uses the patch on the next iteration —
        # without this, the loop would dead-end at ``no_proposed_fix``
        # on the very first LLM over-claim and leave the deterministic
        # critical findings unrepaired.
        report = _coerce_report(parsed, fallback_code=code, preserve_proposed_code=True)
        # Preserve the deterministic findings on the returned report so
        # the orchestrator's persistence path sees them regardless of
        # what the LLM echoed back.
        report.alignment_findings = list(findings)
        return report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_findings_section(findings: List[AlignmentFinding], max_rows: int = 60) -> str:
    """Render a compact, decision-relevant view of the findings ledger.

    Critical findings come first (those the patch must address), then
    a tail of info / warning rows for context. The cap prevents the
    prompt growing unbounded on pathologically misaligned ledgers.
    """
    if not findings:
        return "No findings produced."

    critical = [f for f in findings if f.severity == "critical" and not f.passed]
    info_warning = [f for f in findings if f not in critical]
    ordered = critical + info_warning
    truncated = len(ordered) > max_rows
    visible = ordered[:max_rows]
    lines = []
    for f in visible:
        head = f"[{f.severity.upper()}] {f.check_name}"
        if f.rule_id:
            head += f" ({f.rule_id})"
        head += f" trade #{f.trade_num}"
        if f.computed_value is not None and f.expected_value is not None:
            head += f" — computed={f.computed_value:.6g}, expected={f.expected_value:.6g}"
        lines.append(head)
        lines.append(f"    {f.details}")
    if truncated:
        lines.append(f"  ... ({len(ordered) - max_rows} additional findings not shown) ...")
    return "\n".join(lines)


def _coerce_report(
    parsed: Dict[str, Any],
    fallback_code: str,
    *,
    preserve_proposed_code: bool = False,
) -> TradeAlignmentReport:
    """Convert raw LLM JSON into a :class:`TradeAlignmentReport`.

    Tolerates loose schemas (missing fields, snake_case vs camelCase
    issues) so a small format drift in the LLM does not abort the
    alignment loop. The deterministic findings list is *not* read from
    the LLM — it is preserved by the caller (``propose_code_fix``).

    ``preserve_proposed_code=True`` skips the defensive
    "aligned=true ⇒ null proposed_code" coercion. The fix-proposer
    path sets this because the deterministic gate has already decided
    misaligned; an LLM that over-claims ``aligned=true`` while still
    supplying a patch should not silently drop the patch — the
    orchestrator clamps ``aligned`` to ``False`` afterwards and the
    patch is still worth a re-execution.
    """
    aligned = bool(parsed.get("aligned", False))
    rationale = str(parsed.get("rationale", "")).strip()
    raw_issues = parsed.get("issues") or []

    issues: List[AlignmentIssue] = []
    for raw in raw_issues:
        if not isinstance(raw, dict):
            continue
        try:
            issues.append(AlignmentIssue.model_validate(raw))
        except Exception:
            # Best-effort coercion — keep going on a single bad issue
            issues.append(
                AlignmentIssue(
                    rule_type=str(raw.get("rule_type", "entry_rules")),
                    description=str(raw.get("description", "(unparseable issue)")),
                    severity=str(raw.get("severity", "warning"))
                    if str(raw.get("severity", "warning")) in ("info", "warning", "critical")
                    else "warning",  # type: ignore[arg-type]
                    affected_trades=list(raw.get("affected_trades") or []),
                )
            )

    proposed_code_raw = parsed.get("proposed_code")
    proposed_code = (
        str(proposed_code_raw).strip()
        if isinstance(proposed_code_raw, str) and proposed_code_raw.strip()
        else None
    )

    predicted = bool(parsed.get("predicted_aligned_after_fix", False))
    changes = str(parsed.get("changes_made", "")).strip()

    if aligned and not preserve_proposed_code:
        # Defensive: ignore proposed_code / changes when the LLM says
        # aligned — used on the standalone-helper path where
        # ``aligned=True`` means "no fix needed". On the fix-proposer
        # path callers pass ``preserve_proposed_code=True`` so an
        # over-claimed ``aligned=True`` doesn't strip a usable patch
        # (the orchestrator clamps ``aligned`` to ``False`` itself).
        proposed_code = None
        predicted = False
        changes = ""

    # If misaligned but no code was proposed, the loop has nothing to
    # act on. Mark prediction false so the orchestrator exits cleanly.
    if not aligned and proposed_code is None:
        predicted = False

    return TradeAlignmentReport(
        aligned=aligned,
        rationale=rationale,
        issues=issues,
        proposed_code=proposed_code,
        predicted_aligned_after_fix=predicted,
        changes_made=changes,
    )


def _parse_legitimate(raw: Any) -> bool:
    """Strict parse of the LLM's ``legitimate`` field.

    Plain ``bool(raw)`` would treat ``"false"`` (the string) as truthy
    and wave through a misaligned trade. Accept only real ``bool``s and
    the case-insensitive string literals ``"true"`` / ``"false"``;
    anything else (including unexpected ints, None, malformed JSON
    types) falls closed to ``False``. The near-miss path is a
    correctness boundary — never legitimise without an unambiguous
    yes.
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        normalised = raw.strip().lower()
        if normalised == "true":
            return True
        if normalised == "false":
            return False
    return False


def _extract_json(text: str) -> Dict[str, Any]:
    """Extract a JSON object from LLM output, handling markdown fences."""
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)

    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in LLM response")

    depth = 0
    end = start
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    try:
        return json.loads(text[start:end])
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse JSON from LLM response: {e}") from e
