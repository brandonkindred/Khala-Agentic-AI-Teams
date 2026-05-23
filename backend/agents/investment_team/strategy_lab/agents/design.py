"""Strands Agent that authors a ``StrategySpec`` — spec only, no code.

The design phase replaces the legacy single-call ``IdeationAgent``: the
designer here emits only the structured spec (entry/exit/sizing rules,
target symbols, risk limits, hypothesis). Code generation is a separate
phase, gated by ``SpecReadinessGate`` and the design-review loop, so the
designer cannot soften the spec to fit broken code.

Invariants:
  * ``run`` and ``revise`` both return a parsed JSON dict whose
    ``strategy_code`` key (if the LLM emitted one anyway) is stripped
    with a warning before return.
  * Both methods raise :class:`StrategySpecParseError` when the LLM
    returns prose / off-shape rules; the orchestrator surfaces this as
    a critical design-phase failure rather than constructing a half-
    valid ``StrategySpec``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from strands import Agent

from ...models import StrategyLabRecord
from ...signal_intelligence_agent import brief_to_prompt_block
from ...signal_intelligence_models import SignalIntelligenceBriefV1
from ...strategy_lab_context import asset_class_mix_hint, format_prior_results
from ._parse_helpers import (
    StrategySpecParseError,
    extract_json_object,
    validate_structured_rules,
)
from .design_review import format_prior_critiques
from .model_factory import get_strands_model

if TYPE_CHECKING:
    from ...models import StrategySpec
    from .design_review import SpecCritique

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
_SYSTEM_PROMPT = (_PROMPT_DIR / "design_system.md").read_text(encoding="utf-8")

_DEFAULT_PARSE_RETRIES = 2


def _parse_retry_budget() -> int:
    """Resolve the retry budget for malformed LLM output in ``_invoke_and_parse``.

    LLMs occasionally emit prose, truncated JSON, or off-shape rule objects
    even with a strict system prompt. A bounded retry — replaying the prompt
    with a corrective addendum — recovers cleanly the majority of the time
    and prevents a single bad response from burning a Strategy Lab cycle.

    Pre: env value, when set, parses to ``int`` and is ``>= 0`` after the
    ``max(.., 0)`` clamp.
    Post: returns a non-negative integer. Total attempts = budget + 1.
    Default 2 → 3 attempts. Garbage values fall back to the default.
    """
    raw = os.environ.get("STRATEGY_LAB_DESIGN_PARSE_RETRIES")
    if raw is None:
        return _DEFAULT_PARSE_RETRIES
    try:
        return max(int(raw), 0)
    except ValueError:
        return _DEFAULT_PARSE_RETRIES

_DESIGN_USER_TEMPLATE = """\
Design ONE novel swing-style strategy (typical holds ~2-14 days unless the asset class implies shorter).
Goal: exceed 8% annualized in principle, with explicit risk controls.

## Prior Strategy Results ({n_prior} tested so far, chronological)
{prior_results_text}

## Asset-class diversity (mandatory)
{asset_class_mix_hint}

{signal_section}

{convergence_directives}

## Instructions
Follow your decomposed reasoning process: ANALYZE → HYPOTHESIZE → DESIGN → STRESS-TEST → OUTPUT.

Each prior entry includes outcome, metrics, rationale, and post-backtest analysis. Generate a strategy that **differs** from prior ones and learns from their failures.

Return ONLY a JSON object with no markdown. `entry_rules`, `exit_rules`,
and `sizing` MUST be the structured DSL objects described in the system
prompt — prose strings will be rejected. `timeframe` is REQUIRED and
must be one of `"1m"`, `"5m"`, `"15m"`, `"1h"`, `"1d"`.

DO NOT emit a `strategy_code` field. Code synthesis is a separate phase.

{{
  "asset_class": "stocks" | "crypto" | "forex" | "futures" | "commodities",
  "hypothesis": "1-3 sentence investment thesis tying multiple signals to edge",
  "signal_definition": "Describe the ensemble of signals and how they combine",
  "timeframe": "1d",
  "entry_rules": [ /* structured DSL */ ],
  "exit_rules":  [ /* structured DSL */ ],
  "sizing":      {{ /* structured DSL */ }},
  "target_symbols": ["UPPERCASE tickers if your hypothesis names specific ones; else []"],
  "risk_limits": {{"max_position_pct": 5, "stop_loss_pct": 3}},
  "speculative": false,
  "rationale": "Why this strategy and asset class now, given priors and the diversity hint"
}}
"""


_REVISION_USER_TEMPLATE = """\
Revise the following strategy specification to address every issue raised by the design reviewer.

## Current Specification (the spec under review)
```json
{prior_spec_json}
```

## Latest Review (must address every issue)
Ready: {ready}
Rationale: {rationale}

Issues:
{issues_block}

## Prior Critiques on this lineage ({n_prior_critiques} so far)
{prior_critiques_block}

## Instructions

1. For every issue above, apply the `suggested_fix` (or a tighter equivalent if the suggested fix conflicts with a critical rule of the DSL).
2. Preserve every aspect of the spec that was NOT criticised — do not redesign what the reviewer accepted.
3. Return ONLY a JSON object with no markdown, matching the same shape as the original spec (structured DSL rules, timeframe, target_symbols, risk_limits, etc.). DO NOT emit a `strategy_code` field.
"""


class DesignAgent:
    """Generate (and revise) a structured trading strategy specification.

    Contract:
      Pre — ``prior_records`` is iterable; LLM is reachable via the
            configured Strands model.
      Post — ``run`` returns ``(strategy_dict, rationale)``. ``strategy_dict``
            never contains a ``strategy_code`` key.
      Post — ``revise`` returns ``(strategy_dict, rationale)``. The result
            addresses every issue in the supplied critique; ``strategy_code``
            is stripped on the way out.
      Invariant — neither method emits code. Rule shapes that fail
            structured-DSL validation surface as :class:`StrategySpecParseError`.
    """

    def run(
        self,
        prior_records: List[StrategyLabRecord],
        signal_brief: Optional[SignalIntelligenceBriefV1] = None,
        convergence_directives: Optional[List[str]] = None,
        exclude_asset_classes: Optional[List[str]] = None,
    ) -> Tuple[Dict[str, Any], str]:
        """Design a fresh strategy spec from priors + brief.

        Returns: ``(strategy_dict, rationale)``. ``strategy_dict`` has no
        ``strategy_code`` key.
        """
        prior_text = (
            format_prior_results(prior_records)
            if prior_records
            else "No prior strategies tested yet."
        )
        mix_hint = (
            asset_class_mix_hint(prior_records) if prior_records else "No history — choose freely."
        )
        if exclude_asset_classes:
            mix_hint += (
                f"\nMANDATORY EXCLUSION: Do NOT use these asset classes: "
                f"{', '.join(exclude_asset_classes)}."
            )

        signal_section = ""
        if signal_brief:
            block = brief_to_prompt_block(signal_brief)
            signal_section = f"## Signal Intelligence Brief\n{block}"

        directives_text = ""
        if convergence_directives:
            directives_text = "## Mandatory Directives\n" + "\n".join(convergence_directives)

        user_prompt = _DESIGN_USER_TEMPLATE.format(
            n_prior=len(prior_records),
            prior_results_text=prior_text,
            asset_class_mix_hint=mix_hint,
            signal_section=signal_section,
            convergence_directives=directives_text,
        )

        return self._invoke_and_parse(_SYSTEM_PROMPT, user_prompt)

    def revise(
        self,
        prior_spec: "StrategySpec",
        critique: "SpecCritique",
        prior_critiques: Optional[List["SpecCritique"]] = None,
    ) -> Tuple[Dict[str, Any], str]:
        """Revise ``prior_spec`` to address every issue raised in ``critique``.

        Returns: ``(strategy_dict, rationale)``. ``strategy_dict`` has no
        ``strategy_code`` key.
        """
        spec_json = prior_spec.model_dump_json(indent=2, exclude={"strategy_code"})
        issues_block = _format_issues(critique)
        prior_critiques_block = format_prior_critiques(prior_critiques)

        user_prompt = _REVISION_USER_TEMPLATE.format(
            prior_spec_json=spec_json,
            ready=str(critique.ready).lower(),
            rationale=critique.rationale or "(no rationale supplied)",
            issues_block=issues_block,
            n_prior_critiques=len(prior_critiques) if prior_critiques else 0,
            prior_critiques_block=prior_critiques_block,
        )

        return self._invoke_and_parse(_SYSTEM_PROMPT, user_prompt)

    def _invoke_and_parse(self, system_prompt: str, user_prompt: str) -> Tuple[Dict[str, Any], str]:
        """Call the LLM, parse JSON, strip any stray ``strategy_code``, validate rules.

        On JSON-extract failure or structured-DSL validation failure, the
        call is retried up to :func:`_parse_retry_budget` times with a
        corrective addendum appended to the user prompt so the model sees
        what went wrong. On final exhaustion the last exception is
        re-raised so the existing contract (``ValueError`` for malformed
        JSON, :class:`StrategySpecParseError` for prose/off-shape rules)
        is preserved.

        Pre: ``system_prompt`` and ``user_prompt`` are non-empty strings.
        Post: returns ``(parsed, rationale)`` with no ``strategy_code`` key
        and rule fields that pass :func:`validate_structured_rules`. Raises
        ``ValueError`` on persistently malformed JSON or
        :class:`StrategySpecParseError` on persistently prose/off-shape
        rules.
        """
        max_retries = _parse_retry_budget()
        attempts = max_retries + 1

        agent = Agent(
            model=get_strands_model("strategy_design"),
            system_prompt=system_prompt,
            tools=[],
        )

        attempt_prompt = user_prompt
        last_exc: Optional[Exception] = None

        for attempt in range(attempts):
            result = agent(attempt_prompt)

            try:
                parsed = extract_json_object(str(result))
            except ValueError as exc:
                last_exc = exc
                logger.warning(
                    "DesignAgent failed to extract JSON (attempt %d/%d): %s",
                    attempt + 1,
                    attempts,
                    exc,
                )
                attempt_prompt = _retry_prompt(
                    user_prompt,
                    reason=f"The previous response was not valid JSON: {exc}",
                )
                continue

            if "strategy_code" in parsed:
                parsed.pop("strategy_code", None)
                logger.warning(
                    "DesignAgent stripped stray strategy_code field from LLM response "
                    "(code synthesis is a separate phase)."
                )

            rationale = parsed.pop("rationale", "")

            try:
                validate_structured_rules(parsed)
            except StrategySpecParseError as exc:
                last_exc = exc
                logger.warning(
                    "DesignAgent emitted invalid rule shape (attempt %d/%d): %s",
                    attempt + 1,
                    attempts,
                    exc,
                )
                attempt_prompt = _retry_prompt(
                    user_prompt,
                    reason=(
                        f"The previous response had invalid rule structure at "
                        f"`{exc.field}`. Re-read the system prompt's structured "
                        f"DSL section and retry. Validator error: {exc}"
                    ),
                )
                continue

            return parsed, rationale

        assert last_exc is not None, (
            "_invoke_and_parse retry loop exited without success or exception; "
            "this is a bug in the loop condition."
        )
        raise last_exc


def _retry_prompt(original_user_prompt: str, *, reason: str) -> str:
    """Append a short corrective addendum so the LLM sees what went wrong.

    The original prompt is preserved verbatim; the addendum is appended
    rather than substituted so the design directives, prior-results
    context, and JSON shape spec all stay in view on the retry attempt.
    """
    return (
        f"{original_user_prompt}\n\n"
        "## Retry — the previous attempt failed validation\n"
        f"{reason}\n"
        "Return ONLY a single JSON object matching the shape described above. "
        "Every `entry_rules` / `exit_rules` item MUST be a structured DSL object "
        "(NOT a prose string)."
    )


def _format_issues(critique: "SpecCritique") -> str:
    """Render critique issues as a short, deterministic block."""
    if not critique.issues:
        return "(no specific issues — see rationale)"
    lines = []
    for i, issue in enumerate(critique.issues, start=1):
        lines.append(
            f"  {i}. [{issue.severity}] {issue.field}: {issue.description}"
            + (f"  Fix: {issue.suggested_fix}" if issue.suggested_fix else "")
        )
    return "\n".join(lines)


__all__ = ["DesignAgent"]
