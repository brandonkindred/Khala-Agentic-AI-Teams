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
import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from ..alignment_findings import AlignmentFinding, NearMissVerdict
from ..budget_config import StrategyLabBudgetConfig
from ._agent_runner import run_single_shot_agent
from ._parse_helpers import coerce_strict_bool
from ._prompt_context import render_prior_attempts, spec_prompt_fields
from ._response_schemas import ALIGNMENT_FIX_SCHEMA

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Loaded once at import — these system prompts are static, so re-reading them
# from disk on every near-miss adjudication / propose-fix call is wasted I/O.
# (The propose-fix prompt is held raw on purpose: it contains literal ``{...}``
# code examples and must never pass through ``str.format``.)
_NEAR_MISS_SYSTEM_PROMPT = (_PROMPT_DIR / "alignment_near_miss.md").read_text(encoding="utf-8")
_PROPOSE_FIX_SYSTEM_PROMPT = (_PROMPT_DIR / "alignment_propose_fix.md").read_text(encoding="utf-8")

# The JSON Schema the LLM response must conform to, rendered once for
# injection into the *user* prompt only (mirrors
# ``refinement._REFINEMENT_SCHEMA_JSON``). Never splice this into
# ``_PROPOSE_FIX_SYSTEM_PROMPT`` — that prompt is held raw on purpose (see
# above) and is never passed through ``str.format``.
_ALIGNMENT_FIX_SCHEMA_JSON = json.dumps(ALIGNMENT_FIX_SCHEMA, indent=2)


def _alignment_max_attempts() -> int:
    """Resolve the envelope attempt count for the alignment fix-proposer.

    Driven by ``STRATEGY_LAB_ALIGNMENT_RETRIES`` (default ``2`` → 3 attempts
    total), preserving the historical alignment retry semantics now that the
    retry+backoff loop lives inside the LLM envelope rather than the
    orchestrator. Garbage / sub-zero values fall back to the default.

    Postconditions: returns an int ``>= 1``.
    """
    retries = StrategyLabBudgetConfig.from_env().alignment_retries
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

Your response MUST conform to this JSON Schema:

```json
{response_schema_json}
```
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
        system_prompt = _NEAR_MISS_SYSTEM_PROMPT
        user_prompt = _NEAR_MISS_USER_TEMPLATE.format(
            rule_id=rule_id,
            predicate_repr=predicate_repr,
            computed_value=computed_value,
            threshold=threshold,
            symbol=symbol,
            entry_date=entry_date,
        )

        def _on_failure(exc: Exception) -> Any:
            logger.debug(
                "Near-miss adjudicator failed to produce parseable JSON: %s",
                exc,
                exc_info=True,
            )
            raise AlignmentAuditError(f"{type(exc).__name__}: {exc}") from exc

        _, parsed = run_single_shot_agent(
            agent_key="strategy_alignment",
            phase="alignment_near_miss",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            charge=True,
            logger=logger,
            on_failure=_on_failure,
        )

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
        system_prompt = _PROPOSE_FIX_SYSTEM_PROMPT

        critical = [f for f in findings if f.severity == "critical" and not f.passed]
        info_warning = [f for f in findings if f not in critical]
        prior_text = render_prior_attempts(prior_attempts)

        user_prompt = _PROPOSE_FIX_USER_TEMPLATE.format(
            **spec_prompt_fields(spec, defensive=True),
            target_symbols=list(getattr(spec, "target_symbols", []) or []),
            n_critical=len(critical),
            n_info_warning=len(info_warning),
            findings_section=_format_findings_section(findings),
            strategy_code=code,
            n_prior_attempts=len(prior_attempts) if prior_attempts else 0,
            prior_attempts_text=prior_text,
            response_schema_json=_ALIGNMENT_FIX_SCHEMA_JSON,
        )

        def _on_failure(exc: Exception) -> Any:
            logger.debug(
                "Alignment fix proposer failed to produce parseable JSON: %s",
                exc,
                exc_info=True,
            )
            raise AlignmentAuditError(f"{type(exc).__name__}: {exc}") from exc

        _, parsed = run_single_shot_agent(
            agent_key="strategy_alignment",
            phase="alignment_propose_fix",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            charge=True,
            max_attempts=_alignment_max_attempts(),
            logger=logger,
            on_failure=_on_failure,
        )

        # ``preserve_proposed_code=True``: the deterministic gate has
        # already decided misaligned, so an LLM that over-claims
        # ``aligned=True`` while supplying a patch should not silently
        # drop the patch. The orchestrator clamps ``aligned`` to
        # ``False`` itself and uses the patch on the next iteration —
        # without this, the loop would dead-end at ``no_proposed_fix``
        # on the very first LLM over-claim and leave the deterministic
        # critical findings unrepaired.
        try:
            report = _coerce_report(parsed, fallback_code=code, preserve_proposed_code=True)
        except Exception as exc:
            # Fail OPEN, not closed: a residual coercion error must never discard a
            # usable patch the LLM produced. Preserve ``proposed_code`` directly and
            # let the orchestrator re-execute it (``aligned`` stays False).
            logger.debug(
                "Alignment fix report coercion degraded; preserving raw patch: %s",
                exc,
                exc_info=True,
            )
            proposed_raw = parsed.get("proposed_code")
            report = TradeAlignmentReport(
                aligned=False,
                rationale=str(parsed.get("rationale", "")).strip(),
                issues=[],
                proposed_code=(
                    str(proposed_raw).strip()
                    if isinstance(proposed_raw, str) and proposed_raw.strip()
                    else None
                ),
                predicted_aligned_after_fix=False,
                changes_made=str(parsed.get("changes_made", "")).strip(),
            )
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
        # Render the trade as an explicit integer field, NOT a copyable
        # ``trade #N`` token. The fix-proposer prompt asks the LLM to echo each
        # issue's ``affected_trades`` (a List[int]); a ``trade #N`` string was
        # being copied back verbatim, producing ``affected_trades=['trade #1']``
        # and a ValidationError that failed the whole loop closed.
        head += f" [trade_num={f.trade_num}]"
        if f.computed_value is not None and f.expected_value is not None:
            head += f" — computed={f.computed_value:.6g}, expected={f.expected_value:.6g}"
        lines.append(head)
        lines.append(f"    {f.details}")
    if truncated:
        lines.append(f"  ... ({len(ordered) - max_rows} additional findings not shown) ...")
    return "\n".join(lines)


def _coerce_affected_trades(value: Any) -> List[int]:
    """Best-effort coercion of an ``affected_trades`` value into ``List[int]``.

    Pre: ``value`` is whatever the LLM echoed — a list of ints, numeric strings,
    or human tokens like ``"trade #1"``; a scalar; or ``None``.
    Post: returns the integers recoverable from it, in order, dropping any element
    no integer can be parsed from. Never raises. This is what keeps a metadata
    drift (the LLM copying a ``trade #N`` string into a ``List[int]`` field) from
    aborting the fix-proposer loop and discarding a usable ``proposed_code`` patch.
    """
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out: List[int] = []
    for item in items:
        if isinstance(item, bool):
            continue  # bool is an int subclass but never a trade number
        if isinstance(item, int):
            out.append(item)
        elif isinstance(item, float) and item.is_integer():
            out.append(int(item))
        elif isinstance(item, str):
            match = re.search(r"-?\d+", item)
            if match:
                out.append(int(match.group()))
    return out


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
        # Normalise the one field the LLM most often mis-types: ``affected_trades``
        # is a List[int], but the findings ledger references human trade numbers the
        # LLM tends to copy back as strings. Coerce BEFORE validation so the common
        # case parses on the primary path and never trips the fallback.
        raw = {**raw, "affected_trades": _coerce_affected_trades(raw.get("affected_trades"))}
        try:
            issues.append(AlignmentIssue.model_validate(raw))
        except Exception:
            # Best-effort coercion — keep going on a single bad issue. This branch
            # must NEVER raise: if it did, the ``proposed_code`` extraction below
            # would be skipped and a usable patch discarded. Drop an issue we still
            # cannot construct rather than fail the whole report.
            try:
                issues.append(
                    AlignmentIssue(
                        rule_type=str(raw.get("rule_type", "entry_rules")),
                        description=str(raw.get("description", "(unparseable issue)")),
                        severity=str(raw.get("severity", "warning"))
                        if str(raw.get("severity", "warning")) in ("info", "warning", "critical")
                        else "warning",  # type: ignore[arg-type]
                        affected_trades=_coerce_affected_trades(raw.get("affected_trades")),
                    )
                )
            except Exception:
                logger.debug("Dropping an unparseable alignment issue: %r", raw, exc_info=True)
                continue

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
    return coerce_strict_bool(raw)
