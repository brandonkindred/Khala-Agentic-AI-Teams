"""Strands Agent that diagnoses zero-trade backtest failures and proposes
Python code fixes targeted at the deterministic `zero_trade_category`
classified by the trading service (see issue #404).

Used by :class:`StrategyLabOrchestrator` ahead of the generic
:class:`RefinementAgent` whenever a refinement-loop backtest produces a
critical zero-trade anomaly. The orchestrator drives a one-shot repair
attempt per refinement round: the proposed code is sent through code
safety + a fresh backtest + the anomaly gates before being committed
over the previous known-good state. Failed proposals fall through to
the generic refinement agent so existing behavior is preserved.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, get_args

from pydantic import BaseModel, Field

from ...models import BacktestExecutionDiagnostics, CoverageReport, StrategySpec, ZeroTradeCategory
from ..coverage_probe import format_coverage_report
from ._agent_runner import run_single_shot_agent
from ._parse_helpers import StrategySpecParseError, validate_structured_rules
from ._prompt_context import render_prior_attempts, spec_prompt_fields
from ._response_schemas import ZERO_TRADE_REPAIR_SCHEMA

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Loaded once at import — the system prompt is static, so re-reading it from disk
# on every zero-trade repair is wasted I/O. Appends the shared sizing/drawdown
# risk-framing reference (deployed size IS the per-trade loss cap; no
# max-drawdown constraint exists) so the canonical wording lives in one place
# instead of an independently-worded inline copy.
_SYSTEM_PROMPT = (
    (_PROMPT_DIR / "zero_trade_repair_system.md").read_text(encoding="utf-8")
    + "\n\n"
    + (_PROMPT_DIR / "_sizing_risk_framing.md").read_text(encoding="utf-8")
)

# The JSON Schema the LLM response must conform to, rendered once for
# injection into the prompt (mirrors ``refinement._REFINEMENT_SCHEMA_JSON``).
_ZERO_TRADE_REPAIR_SCHEMA_JSON = json.dumps(ZERO_TRADE_REPAIR_SCHEMA, indent=2)

# Spec keys the orchestrator will honour from a ZeroTradeRepairReport's
# ``proposed_spec_updates``. Anything else is silently dropped — the
# specialized repair agent must not invent fields.
#
# #530: narrowed to ``risk_limits`` only. The repair agent must fix the
# **code**, not weaken the **spec**. Rule-shaped keys (entry/exit/sizing),
# the hypothesis, and the signal_definition stay locked at this stage so
# a "make trades happen" LLM cannot quietly mutate the thesis. The
# orchestrator additionally logs and gates off-list proposals.
_ALLOWED_SPEC_UPDATE_KEYS = frozenset({"risk_limits"})

# Cap on `last_order_events` included in the repair prompt. The model
# already trims to 20; 10 is enough signal for the LLM to spot the
# failure pattern while keeping the JSON line under ~1 KB.
_DIAGNOSTICS_LAST_EVENTS_CAP = 10


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ZeroTradeRepairReport(BaseModel):
    """Verdict from one zero-trade repair attempt."""

    root_cause_category: ZeroTradeCategory
    evidence: str = ""
    code_issue: Optional[str] = None
    strategy_rule_issue: Optional[str] = None
    proposed_code: Optional[str] = None
    expected_order_count_change: int = 0
    expected_trade_count_change: int = 0
    changes_made: str = ""
    proposed_spec_updates: Optional[Dict[str, Any]] = Field(default=None)
    # #530: keys the agent filtered out of ``proposed_spec_updates`` before
    # returning. Populated by ``_coerce_report`` so the orchestrator can
    # surface a ``logger.warning`` + ``zero_trade_repair_dropped_spec_keys``
    # quality gate even when the agent strips off-list drift in production
    # (otherwise the visibility added in #530 would only fire when the agent
    # is stubbed in tests).
    dropped_spec_update_keys: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


_ZERO_TRADE_USER_TEMPLATE = """\
The most recent backtest produced zero trades. Diagnose the failure
using the deterministic execution diagnostics below and propose a
minimal Python code fix so the next run emits and closes trades that
remain consistent with the strategy specification.

## Strategy Specification (source of truth)
Asset class: {asset_class}
Hypothesis: {hypothesis}
Signal definition: {signal_definition}
Entry rules: {entry_rules}
Exit rules: {exit_rules}
Sizing rules: {sizing_rules}
Risk limits: {risk_limits}

## Current Strategy Code
```python
{strategy_code}
```

## Execution Diagnostics
Zero-trade category: {zero_trade_category}
Summary: {summary}
{diagnostics_block}{coverage_block}

## Prior Zero-Trade Repair Attempts ({n_prior_attempts} so far)
{prior_attempts_text}

## Instructions
1. Restate the `zero_trade_category` and quote the counters / rejection
   reasons / lifecycle events that prove the diagnosis.
2. Identify the specific code branch that produced the failure.
3. Rewrite the FULL Python module so the identified failure no longer
   occurs while preserving the spec's intent. Keep the
   `class _(Strategy)` + `on_bar(self, ctx, bar)` contract and use only
   allowed imports.
4. Predict the change in order and trade count your fix should produce.

Return ONLY a JSON object with no markdown.

Your response MUST conform to this JSON Schema:

```json
{response_schema_json}
```
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class ZeroTradeRepairAgent:
    """Diagnose a zero-trade backtest and propose a targeted code fix."""

    def run(
        self,
        spec: StrategySpec,
        code: str,
        diagnostics: BacktestExecutionDiagnostics,
        prior_attempts: Optional[List[str]] = None,
        *,
        coverage_report: Optional[CoverageReport] = None,
    ) -> ZeroTradeRepairReport:
        """Run one specialized zero-trade repair attempt.

        Returns a :class:`ZeroTradeRepairReport`. On parser failure the
        report falls back to ``proposed_code=None`` with the parse error
        in ``evidence`` so the orchestrator falls through to the generic
        refinement agent (matching the alignment agent's
        no-infinite-loop posture).

        ``coverage_report`` (issue #452) is the deterministic rule-coverage
        verdict produced by the orchestrator (issue #451) on the same
        zero/low-trade run. When provided, a compact JSON block is added
        to the prompt so the repair agent sees the static probe's view
        alongside the executor diagnostics. ``None`` is rendered as a
        blank section.
        """
        if diagnostics.zero_trade_category is None:
            # The orchestrator should not have routed a non-zero-trade
            # diagnostics envelope here. Be defensive — return a no-op
            # report so the caller falls through to generic refinement.
            return ZeroTradeRepairReport(
                root_cause_category="UNKNOWN_ZERO_TRADE_PATH",
                evidence=(
                    "Diagnostics envelope had no zero_trade_category; skipping specialized repair."
                ),
            )

        system_prompt = _SYSTEM_PROMPT

        prior_text = render_prior_attempts(prior_attempts)

        coverage_rendered = format_coverage_report(coverage_report)
        coverage_section = f"\n{coverage_rendered}" if coverage_rendered else ""

        user_prompt = _ZERO_TRADE_USER_TEMPLATE.format(
            **spec_prompt_fields(spec),
            strategy_code=code,
            zero_trade_category=diagnostics.zero_trade_category,
            summary=diagnostics.summary or "(no executor summary)",
            diagnostics_block=_format_diagnostics_block(diagnostics),
            coverage_block=coverage_section,
            n_prior_attempts=len(prior_attempts) if prior_attempts else 0,
            prior_attempts_text=prior_text,
            response_schema_json=_ZERO_TRADE_REPAIR_SCHEMA_JSON,
        )

        def _on_failure(exc: Exception) -> ZeroTradeRepairReport:
            logger.exception("Zero-trade repair agent failed to produce parseable JSON")
            return ZeroTradeRepairReport(
                root_cause_category=diagnostics.zero_trade_category,
                evidence=(
                    f"Zero-trade repair skipped: LLM response could not be parsed ({exc}). "
                    "Falling through to generic refinement."
                ),
            )

        ok, parsed = run_single_shot_agent(
            agent_key="strategy_zero_trade_repair",
            phase="zero_trade_repair",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            charge=True,
            logger=logger,
            on_failure=_on_failure,
        )
        if not ok:
            return parsed

        return _coerce_report(parsed, fallback_category=diagnostics.zero_trade_category)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_diagnostics_block(diagnostics: BacktestExecutionDiagnostics) -> str:
    """Render a compact JSON block of the diagnostics envelope.

    Mirrors :func:`strategy_lab.orchestrator._format_execution_diagnostics`
    so the repair-prompt payload matches what the generic refinement
    prompt sees, but always emits the full envelope (the orchestrator
    only routes here when ``zero_trade_category`` is set).
    """
    payload = diagnostics.model_dump(mode="json", exclude_none=True)
    events = payload.get("last_order_events") or []
    if len(events) > _DIAGNOSTICS_LAST_EVENTS_CAP:
        payload["last_order_events"] = events[-_DIAGNOSTICS_LAST_EVENTS_CAP:]
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return f"Envelope: {encoded}"


def _coerce_report(
    parsed: Dict[str, Any], fallback_category: ZeroTradeCategory
) -> ZeroTradeRepairReport:
    """Convert raw LLM JSON into a :class:`ZeroTradeRepairReport`.

    Tolerates loose schemas (missing fields, snake_case vs camelCase
    issues, integer-as-string deltas) so a small format drift in the LLM
    does not abort the specialized repair branch — the caller will fall
    through to generic refinement on a no-op report.
    """
    raw_category = parsed.get("root_cause_category")
    # Derived from the Literal (not hand-copied) so this set can never drift
    # from `models.ZeroTradeCategory` — mirrors the `_VALID_SOURCES` idiom in
    # `quality_gates/code_conformance/ast_helpers.py`.
    valid_categories = frozenset(get_args(ZeroTradeCategory))
    category = raw_category if raw_category in valid_categories else fallback_category

    proposed_code_raw = parsed.get("proposed_code")
    proposed_code = (
        str(proposed_code_raw).strip()
        if isinstance(proposed_code_raw, str) and proposed_code_raw.strip()
        else None
    )

    raw_spec_updates = parsed.get("proposed_spec_updates")
    proposed_spec_updates: Optional[Dict[str, Any]]
    dropped_spec_update_keys: List[str] = []
    if isinstance(raw_spec_updates, dict):
        whitelisted = {k: v for k, v in raw_spec_updates.items() if k in _ALLOWED_SPEC_UPDATE_KEYS}
        # #530: record what the agent filtered so the orchestrator can
        # surface it via logger.warning + a quality gate. Without this,
        # the production agent-to-orchestrator flow would silently drop
        # the drift because the orchestrator only sees the sanitized
        # report.
        dropped_spec_update_keys = sorted(
            k for k in raw_spec_updates if k not in _ALLOWED_SPEC_UPDATE_KEYS
        )
        # Reject prose / invalid structure on the rule-shaped keys so the
        # orchestrator does not propagate a malformed dict into
        # `_apply_updates`. The whitelist still gates which fields make it
        # through (#530); validation only runs on rule-shaped values.
        try:
            validate_structured_rules(whitelisted)
        except StrategySpecParseError as exc:
            logger.warning(
                "Zero-trade repair emitted invalid structured rule shape; dropping "
                "proposed_spec_updates so the orchestrator falls back to generic "
                "refinement: %s",
                exc,
            )
            whitelisted = {}
        proposed_spec_updates = whitelisted or None
    else:
        proposed_spec_updates = None

    def _coerce_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    return ZeroTradeRepairReport(
        root_cause_category=category,  # type: ignore[arg-type]
        evidence=str(parsed.get("evidence", "")).strip(),
        code_issue=_optional_str(parsed.get("code_issue")),
        strategy_rule_issue=_optional_str(parsed.get("strategy_rule_issue")),
        proposed_code=proposed_code,
        expected_order_count_change=_coerce_int(parsed.get("expected_order_count_change", 0)),
        expected_trade_count_change=_coerce_int(parsed.get("expected_trade_count_change", 0)),
        changes_made=str(parsed.get("changes_made", "")).strip(),
        proposed_spec_updates=proposed_spec_updates,
        dropped_spec_update_keys=dropped_spec_update_keys,
    )


def _optional_str(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
