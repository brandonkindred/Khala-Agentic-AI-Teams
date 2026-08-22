"""Strands Agent that authors a ``StrategySpec`` — spec only, no code.

The design phase replaces the legacy single-call ``IdeationAgent``: the
designer here emits only the structured spec (entry/exit/sizing rules,
target symbols, risk limits, hypothesis). Code generation is a separate
phase, gated by ``SpecReadinessGate`` and the design-review loop, so the
designer cannot soften the spec to fit broken code.

Invariants:
  * ``run`` and ``revise`` both return a ``(strategy_dict, rationale)``
    tuple; ``strategy_dict`` never contains a ``strategy_code`` key —
    if the LLM emitted one anyway, it is stripped with a warning
    before return.
  * Both methods raise :class:`StrategySpecParseError` when the LLM
    returns prose / off-shape rules; the orchestrator surfaces this as
    a critical design-phase failure rather than constructing a half-
    valid ``StrategySpec``.
"""

from __future__ import annotations

import functools
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from strands import Agent

from ...models import StrategyLabRecord
from ...signal_intelligence_agent import brief_to_prompt_block
from ...signal_intelligence_models import SignalIntelligenceBriefV1
from ...strategy_lab_context import (
    asset_class_mix_hint,
    format_prior_attribution,
    format_prior_results,
)
from ..budget_config import StrategyLabBudgetConfig
from ..market_regime import RegimeSummary, regime_to_prompt_block
from . import _structured_output as so
from ._agent_runner import run_json_with_parse_retry
from ._llm_budget import DesignBudgetExhausted, charge_active_budget
from ._llm_envelope import run_structured_agent
from ._parse_helpers import (
    StrategySpecParseError,
    build_json_correction_prompt,
    extract_json_object,
    validate_structured_rules,
)
from ._response_schemas import CRITIQUE_SCHEMA, DESIGN_SPEC_SCHEMA
from .design_review import (
    _coerce_critique,
    _sizing_owned_by_gate,
    format_prior_critiques,
)
from .model_factory import get_strands_model

if TYPE_CHECKING:
    from ...models import StrategySpec
    from .design_review import SpecCritique

logger = logging.getLogger(__name__)

_DIVERSITY_MODES = ("exploit", "explore")


def _resolve_diversity_mode() -> str:
    """Resolve the asset-class diversity-steering mode for this run.

    Reads ``STRATEGY_LAB_DIVERSITY_MODE``: ``exploit`` (default) steers toward
    the highest-scoring asset-class buckets — the run's return/win-rate
    objective — while ``explore`` keeps the portfolio-rotation nudge. An unset /
    empty value resolves silently to ``exploit``; a *set but unrecognized* value
    also resolves to ``exploit`` but logs a warning so a misconfiguration is
    visible rather than silently masked.

    Post: returns a value in :data:`_DIVERSITY_MODES`.
    """
    val = os.getenv("STRATEGY_LAB_DIVERSITY_MODE", "").strip().lower()
    if not val:
        return "exploit"
    if val in _DIVERSITY_MODES:
        return val
    logger.warning(
        "Unrecognized STRATEGY_LAB_DIVERSITY_MODE=%r; defaulting to 'exploit' (valid values: %s).",
        val,
        ", ".join(_DIVERSITY_MODES),
    )
    return "exploit"


_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


# Shared stop-order semantics reference (stop-market / stop-limit / trailing
# stop). Appended to the designer's system prompts so a trailing stop's
# above-entry ratchet is understood as intended gain-locking behavior, and so
# the designer does not author exits that contradict the engine's mechanics.
@functools.lru_cache(maxsize=None)
def _get_stop_order_semantics() -> str:
    """Load and cache shared stop-order semantics markdown.

    Preconditions: ``_PROMPT_DIR / "_stop_order_semantics.md"`` exists and is
    readable UTF-8 text when first invoked.
    Postconditions: returns a non-empty ``str``; subsequent calls return the
    same cached value without re-reading the file.
    Invariants: module import does not invoke this helper.
    """
    text = (_PROMPT_DIR / "_stop_order_semantics.md").read_text(encoding="utf-8")
    if not text:
        raise ValueError("_stop_order_semantics.md must be non-empty")
    return text


# Shared sizing/drawdown risk-framing reference (deployed size IS the
# per-trade loss cap; no max-drawdown constraint exists). Appended to the
# designer's system prompt so the canonical wording lives in one place
# instead of drifting inline copies.
@functools.lru_cache(maxsize=None)
def _get_sizing_risk_framing() -> str:
    """Load and cache shared sizing/drawdown risk-framing markdown.

    Preconditions: ``_PROMPT_DIR / "_sizing_risk_framing.md"`` exists and is
    readable UTF-8 text when first invoked.
    Postconditions: returns a non-empty ``str``; subsequent calls return the
    same cached value without re-reading the file.
    Invariants: module import does not invoke this helper.
    """
    text = (_PROMPT_DIR / "_sizing_risk_framing.md").read_text(encoding="utf-8")
    if not text:
        raise ValueError("_sizing_risk_framing.md must be non-empty")
    return text


@functools.lru_cache(maxsize=None)
def _get_design_system_prompt() -> str:
    """Build and cache the designer system prompt (body + shared reference blocks).

    Preconditions: ``design_system.md``, stop-order semantics, and sizing/risk
    framing files exist when first invoked.
    Postconditions: returned string contains the design system body followed
    by the stop-order semantics text and the sizing/risk framing text, each
    separated by a blank line; subsequent calls return the same cached
    composed prompt without re-reading any file.
    Invariants: module import does not invoke this helper.
    """
    body = (_PROMPT_DIR / "design_system.md").read_text(encoding="utf-8")
    if not body:
        raise ValueError("design_system.md must be non-empty")
    return body + "\n\n" + _get_stop_order_semantics() + "\n\n" + _get_sizing_risk_framing()


@functools.lru_cache(maxsize=None)
def _get_self_review_system_prompt() -> str:
    """Build and cache the self-review system prompt (body + shared reference blocks).

    Preconditions: ``design_self_review_system.md``, stop-order semantics, and
    sizing/risk framing files exist when first invoked.
    Postconditions: returned string contains the self-review body followed by
    the stop-order semantics text and the sizing/risk framing text, each
    separated by a blank line; subsequent calls return the same cached
    composed prompt without re-reading any file.
    Invariants: module import does not invoke this helper.
    """
    body = (_PROMPT_DIR / "design_self_review_system.md").read_text(encoding="utf-8")
    if not body:
        raise ValueError("design_self_review_system.md must be non-empty")
    return body + "\n\n" + _get_stop_order_semantics() + "\n\n" + _get_sizing_risk_framing()


# The JSON Schema the LLM response must conform to, rendered once for
# injection into the prompt (mirrors ``refinement._REFINEMENT_SCHEMA_JSON``).
# The Ollama transport routes through the ``llm_service`` client in
# ``json_object`` wire mode, which forces a JSON object on the wire but not a
# specific shape; this prompt-embedded schema is what pins the response to
# the wire shape ``_DesignSpecWire`` documents, on both the initial-generation
# and revision paths.
_DESIGN_SPEC_SCHEMA_JSON = json.dumps(DESIGN_SPEC_SCHEMA, indent=2)

_DESIGN_USER_TEMPLATE = """\
Design ONE novel swing-style strategy (typical holds ~2-14 days unless the asset class implies shorter).
Objective: maximize annualized return AND win rate, subject to positive, robust expectancy after costs. Clearing ~8% annualized is a necessary floor, not the target — push higher while keeping post-cost expectancy positive. Do NOT chase win rate alone: a tight take-profit against a wide stop posts a high win rate with negative expectancy.

## Prior Strategy Results ({n_prior} tested so far, chronological)
{prior_results_text}

## What has worked so far (performance attribution)
Mean win rate and mean annualized return per design-space bucket, across executed prior runs. Treat this as your edge map: **prefer the historically high-scoring buckets** (high annual return AND win rate) when they fit a coherent thesis. Weigh each bucket by its sample size `n` — a single-record bucket flagged `(thin sample)` is a weak prior, not a mandate — and read it together with the asset-class guidance below.
{prior_attribution}

## Asset-class selection
{asset_class_mix_hint}

{regime_section}

{signal_section}

{convergence_directives}

## Instructions
Follow your decomposed reasoning process: ANALYZE → HYPOTHESIZE → DESIGN → FORECAST → STRESS-TEST → OUTPUT.

In the FORECAST step you MUST estimate, before committing the spec, your expected win rate, the reward:risk implied by your take-profit/stop geometry, the expected trades per year, and the resulting projected annual return — and show they are mutually consistent (e.g. a 1% take-profit against a 5% stop must defend the ~84% win rate it needs to break even before costs). Record these as the structured `expectancy_forecast` object AND summarize the reasoning in `rationale`.

Each prior entry includes outcome, metrics, rationale, and post-backtest analysis. Generate a strategy that **differs** from prior ones and learns from their failures.

Return ONLY a JSON object with no markdown. `entry_rules`, `exit_rules`,
and `sizing` MUST be the structured DSL objects described in the system
prompt — prose strings will be rejected. `timeframe` is REQUIRED and
must be one of `"1m"`, `"5m"`, `"15m"`, `"1h"`, `"1d"`.

DO NOT emit a `strategy_code` field. Code synthesis is a separate phase.

Your response MUST conform to this JSON Schema:

```json
{response_schema_json}
```

Concretely (a representative example — the schema above is authoritative):
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
  "expectancy_forecast": {{
    "forecast_win_rate": 0.0,            /* fraction in [0,1], e.g. 0.55 for 55% */
    "reward_risk": 0.0,                  /* avg win : avg loss implied by TP/stop geometry, e.g. 2.0 */
    "trades_per_year": 0.0,              /* expected trade frequency */
    "projected_annual_return_pct": 0.0,  /* projected annualized return, percent, e.g. 14.0 */
    "consistency_note": "One line showing win_rate × reward:risk × frequency support the projected return"
  }},
  "rationale": "Why this strategy and asset class now, given priors and the diversity hint — including the expectancy reasoning"
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

## Regressions — issues you previously resolved that have reappeared
{regression_notice_block}

## Instructions

1. For every issue above, apply the `suggested_fix` (or a tighter equivalent if the suggested fix conflicts with a critical rule of the DSL).
2. Preserve every aspect of the spec that was NOT criticised — do not redesign what the reviewer accepted.
3. If any regressions are listed above, you MUST NOT reintroduce those defects — they were already fixed on an earlier round; keep them fixed while addressing the current issues.
4. Return ONLY a JSON object with no markdown, matching the same shape as the original spec (structured DSL rules, timeframe, target_symbols, risk_limits, etc.). DO NOT emit a `strategy_code` field.

Your response MUST conform to this JSON Schema:

```json
{response_schema_json}
```
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
        regime_summary: Optional[RegimeSummary] = None,
    ) -> Tuple[Dict[str, Any], str]:
        """Design a fresh strategy spec from priors + brief + regime.

        Returns: ``(strategy_dict, rationale)``. ``strategy_dict`` has no
        ``strategy_code`` key.

        ``regime_summary`` (optional) is the current market-regime read for the
        candidate asset classes (trend direction / strength, volatility regime).
        When present and non-empty it is injected as a ``## Market Regime``
        section so the designer can pick a setup archetype that fits the regime
        (see the "Setup playbook" in the system prompt). When ``None`` / empty
        the section renders blank — fully backward compatible.

        Every underlying LLM call (generation, each parse-retry, and the
        optional self-review / self-revision) charges the active design-phase
        budget via :func:`charge_active_budget` and raises
        :class:`DesignBudgetExhausted` when the per-cycle cap is hit. When
        structured output is available, generation itself is a reasoning-then-
        formatting pair of provider calls (see :meth:`_invoke_and_parse` /
        :meth:`_structured_preflight`) charged as two budget units up front,
        not one.
        """
        prior_text = (
            format_prior_results(prior_records)
            if prior_records
            else "No prior strategies tested yet."
        )
        # Both calls below independently window/aggregate the same
        # ``prior_records`` list; sharing one cache dict across them lets
        # ``_executed_records`` memoize its sort/filter pass instead of
        # repeating it per call (see strategy_lab_context._executed_records).
        prior_results_cache: Dict[int, Any] = {}
        # ``format_prior_attribution`` returns its own "not enough history"
        # sentinel for an empty / all-non-executed list, so no guard is needed.
        prior_attribution = format_prior_attribution(prior_records, cache=prior_results_cache)
        mode = _resolve_diversity_mode()
        # ``asset_class_mix_hint`` handles the empty-records case itself (a
        # neutral menu), so a single call covers both the with- and no-priors
        # paths. When a category restriction is active it already supplies the
        # positive allowed-class menu; only the hard negative rule is appended.
        mix_hint = asset_class_mix_hint(
            prior_records, exclude=exclude_asset_classes, mode=mode, cache=prior_results_cache
        )
        if exclude_asset_classes:
            mix_hint += (
                "\nMANDATORY EXCLUSION: Do NOT use these asset classes: "
                f"{', '.join(exclude_asset_classes)}."
            )

        # Only inject the regime block when there is something classified to
        # inject — a degraded/empty summary renders blank so the designer is
        # never handed a "no regime available" placeholder as if it were signal.
        regime_section = ""
        if regime_summary and regime_summary.entries:
            regime_section = f"## Market Regime\n{regime_to_prompt_block(regime_summary)}"

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
            prior_attribution=prior_attribution,
            asset_class_mix_hint=mix_hint,
            regime_section=regime_section,
            signal_section=signal_section,
            convergence_directives=directives_text,
            response_schema_json=_DESIGN_SPEC_SCHEMA_JSON,
        )

        strategy_dict, rationale = self._invoke_and_parse(_get_design_system_prompt(), user_prompt)
        return self._with_self_review(strategy_dict, rationale)

    def revise(
        self,
        prior_spec: "StrategySpec",
        critique: "SpecCritique",
        prior_critiques: Optional[List["SpecCritique"]] = None,
        regression_notice: str = "",
    ) -> Tuple[Dict[str, Any], str]:
        """Revise ``prior_spec`` to address every issue raised in ``critique``.

        Returns: ``(strategy_dict, rationale)``. ``strategy_dict`` has no
        ``strategy_code`` key. Charges the active budget per underlying LLM
        call (see :meth:`run`).

        ``regression_notice`` (optional) is a pre-rendered block naming
        issues the designer had previously resolved and has now reintroduced.
        When non-empty it is surfaced to the model with an explicit
        "do not reintroduce these" instruction so the loop escalates a
        regression rather than silently oscillating on it. Empty by default
        so existing callers are unaffected.

        The accumulated external lineage (``prior_critiques``, which already
        includes the current ``critique``) is forwarded into the internal
        self-review pre-flight so a self-revision cannot regress a fix an
        earlier external round extracted.
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
            regression_notice_block=regression_notice or "None.",
            response_schema_json=_DESIGN_SPEC_SCHEMA_JSON,
        )

        strategy_dict, rationale = self._invoke_and_parse(_get_design_system_prompt(), user_prompt)
        # Thread the external critique lineage AND the regression notice into the
        # internal self-review so a self-revision cannot regress a fix (or undo a
        # prior-round defect the ledger is keeping fixed) that an earlier external
        # round extracted. ``prior_critiques`` already includes the current
        # critique (the orchestrator appends it to the history before calling
        # ``revise``), so it is forwarded verbatim — appending ``critique`` again
        # would double-count it.
        return self._with_self_review(
            strategy_dict,
            rationale,
            prior_critiques=prior_critiques,
            regression_notice=regression_notice,
        )

    def _invoke_and_parse(self, system_prompt: str, user_prompt: str) -> Tuple[Dict[str, Any], str]:
        """Call the LLM, parse JSON, strip any stray ``strategy_code``, validate rules.

        Pre: ``system_prompt`` and ``user_prompt`` are non-empty strings.
        Post: returns ``(parsed, rationale)`` with no ``strategy_code`` key
        and rule fields that pass :func:`validate_structured_rules`. Raises
        ``ValueError`` on malformed JSON,
        :class:`StrategySpecParseError` on prose/off-shape rules after the
        retry budget is exhausted, or
        :class:`~..exceptions.StrategyLabLLMError` when the LLM envelope
        exhausts its transport retries / budget.

        Delegates to :meth:`_structured_preflight` (the provider-enforced
        schema-constrained call, with its own DSL-correction re-prompt on a
        shape miss) and :meth:`_legacy_parse_retry_loop` (the unconstrained
        JSON-and-DSL retry loop) — see their docstrings for the full degrade
        contract. This method's own contract — inputs, outputs, exceptions
        — is unchanged by the split, but under the hood
        ``_structured_preflight`` now issues two sequential LLM calls
        (reasoning then formatting) via :func:`so.invoke_structured_with_schema`,
        which charges the active design-phase budget twice up front — one
        unit per call — and runs both under a single timeout/retry envelope.
        It degrades to ``_legacy_parse_retry_loop`` (a single-call path) on
        either pass's ``schema_forced`` semantic exhaustion.
        """
        finalized, prompt = self._structured_preflight(system_prompt, user_prompt)
        if finalized is not None:
            return finalized

        return self._legacy_parse_retry_loop(system_prompt, prompt, user_prompt)

    def _structured_preflight(
        self, system_prompt: str, user_prompt: str
    ) -> Tuple[Optional[Tuple[Dict[str, Any], str]], str]:
        """Attempt one provider-enforced schema-constrained design call —
        a reasoning pass followed by a formatting pass, via
        :func:`so.invoke_structured_with_schema`.

        Pre: :func:`so.structured_output_available` is True (checked by the
        caller, :meth:`_invoke_and_parse`); ``system_prompt`` / ``user_prompt``
        are non-empty strings.
        Post: returns ``(finalized, prompt)``.
          * On success: ``finalized`` is the ``(parsed, rationale)`` tuple
            the caller should return immediately; ``prompt`` is
            ``user_prompt`` (unused in that case).
          * On a DSL-shape rejection (:class:`StrategySpecParseError`):
            ``finalized`` is ``None`` and ``prompt`` is a correction prompt
            built via :func:`_build_correction_prompt` — the caller must
            fall through to :meth:`_legacy_parse_retry_loop` with it.
          * On ``schema_forced`` starvation: ``finalized`` is ``None`` and
            ``prompt`` is ``user_prompt`` unchanged.
        Any :class:`~..exceptions.StrategyLabLLMError` OTHER than a
        ``schema_forced`` semantic-exhaustion signal propagates immediately
        (unchanged fail-fast semantics for a genuine transport/auth
        failure) rather than degrading — a deliberate, narrow reading of
        this call site's degrade contract, mirroring
        :meth:`RefinementAgent._invoke_and_parse`.

        Structured decoding constrains JSON *shape*, not the DSL semantic
        rules :func:`validate_structured_rules` enforces — a schema-valid
        response can still fail DSL validation (e.g. a bar-field literal
        wrapped incorrectly). That miss re-prompts with
        :func:`_build_correction_prompt` and hands off to
        :meth:`_legacy_parse_retry_loop`, which gets its own full
        ``STRATEGY_LAB_DESIGN_PARSE_RETRIES`` budget — the DSL-correction
        retry path stays in place regardless of structured-output
        availability; only the unparseable-JSON
        (``build_json_correction_prompt``) resend is eliminated on the
        structured happy path.

        Timeout/retry tradeoff: ``so.invoke_structured_with_schema`` keeps
        both of its sub-calls (reasoning then formatting) under the SAME
        single charge/timeout/retry envelope as the pre-split single call —
        a transport-level retry re-runs both sub-calls together, not just
        the one that (may have) failed. This is a deliberate tradeoff to
        keep the existing envelope plumbing untouched rather than threading
        two independent retry budgets through it; see that function's own
        docstring for the timeout-doubling math this implies.
        """
        # ``user_prompt`` (built from ``_DESIGN_USER_TEMPLATE``) ends with a
        # "Return ONLY a JSON object" directive that would otherwise outrank
        # the reasoning-pass system prompt's prose-only instruction. That
        # conflict is resolved centrally, not here: ``invoke_structured_with_schema``
        # appends ``_REASONING_USER_PROMPT_SUFFIX`` to this same ``user_prompt``
        # for its internal reasoning-pass call only, re-asserting prose-only
        # last (see that constant's docstring in ``_structured_output.py``).
        parsed = so.try_structured_or_degrade(
            "strategy_design",
            DESIGN_SPEC_SCHEMA,
            system_prompt,
            user_prompt,
            so.build_reasoning_system_prompt(system_prompt),
            phase="design_generate_structured",
            charge=True,
            objective="strategy design (structured)",
            logger=logger,
        )
        if parsed is None:
            return None, user_prompt

        try:
            finalized = _finalize_parsed(parsed)
        except StrategySpecParseError as exc:
            logger.warning(
                "DesignAgent (structured) emitted invalid rule shape; "
                "re-prompting via the DSL-correction path: %s",
                exc,
            )
            return None, _build_correction_prompt(user_prompt, exc)

        return finalized, user_prompt

    def _legacy_parse_retry_loop(
        self, system_prompt: str, prompt: str, user_prompt: str
    ) -> Tuple[Dict[str, Any], str]:
        """Unconstrained JSON-and-DSL retry loop, delegated to the shared parse-retry driver.

        Pre: ``system_prompt`` / ``user_prompt`` are non-empty strings;
        ``prompt`` is the prompt to send on the first attempt — either
        ``user_prompt`` unchanged, or a DSL-correction prompt handed off by
        :meth:`_structured_preflight`.
        Post: returns ``(parsed, rationale)`` on success. Raises
        ``ValueError`` on malformed JSON,
        :class:`StrategySpecParseError` on prose/off-shape rules once the
        retry budget (``STRATEGY_LAB_DESIGN_PARSE_RETRIES``, default 2
        retries → 3 attempts total; ``0`` disables retry) is exhausted, or
        :class:`~..exceptions.StrategyLabLLMError` when the LLM envelope
        exhausts its transport retries / budget.

        On :class:`StrategySpecParseError` the agent re-prompts the LLM
        with the offending field and pydantic error as feedback (the model
        often slips the DSL by exactly one field — wrapping a bar-field
        literal in an IndicatorRef, or naming an indicator in ``source``).

        Delegates the attempt loop itself — fresh history-free ``Agent`` per
        attempt, per-attempt budget charging, retry-on-``ValueError``,
        retry-on-validation-failure — to
        :func:`_agent_runner.run_json_with_parse_retry`; see its module
        docstring for that contract. The correction-prompt builders below
        deliberately ignore the ``base_user_prompt`` the driver echoes back
        to them and close over this method's own ``user_prompt`` instead:
        the driver always re-sends corrections built from whatever prompt it
        was given first (here, ``prompt``, which on the
        :meth:`_structured_preflight` DSL-rejection fallback path is
        already a correction prompt, not the pristine original) — building
        *further* corrections on top of that would nest correction text
        instead of re-deriving cleanly from ``user_prompt``, as this loop
        has always done.
        """
        retries = StrategyLabBudgetConfig.from_env().design_parse_retries
        # ``run_json_with_parse_retry`` only returns the validated dict; the
        # rationale is smuggled out via this mutable box because ``_validate``
        # must still return a plain ``Dict`` to match the driver's callback
        # contract. Do not read ``rationale_box`` until after the driver returns.
        rationale_box: Dict[str, str] = {}

        def _on_parse_error(_base_prompt: str, exc: ValueError) -> str:
            return build_json_correction_prompt(user_prompt, exc)

        def _on_validation_error(_base_prompt: str, exc: Exception) -> str:
            if not isinstance(exc, StrategySpecParseError):
                raise TypeError(f"expected StrategySpecParseError, got {type(exc)}") from exc
            return _build_correction_prompt(user_prompt, exc)

        def _validate(parsed: Dict[str, Any]) -> Dict[str, Any]:
            finalized, rationale = _finalize_parsed(parsed)
            rationale_box["value"] = rationale
            return finalized

        parsed = run_json_with_parse_retry(
            agent_key="strategy_design",
            phase="design_generate",
            system_prompt=system_prompt,
            base_user_prompt=prompt,
            retry_budget=retries,
            logger=logger,
            before_attempt=charge_active_budget,
            on_parse_error=_on_parse_error,
            validate=_validate,
            on_validation_error=_on_validation_error,
        )
        return parsed, rationale_box["value"]

    def _with_self_review(
        self,
        strategy_dict: Dict[str, Any],
        rationale: str,
        *,
        prior_critiques: Optional[List["SpecCritique"]] = None,
        regression_notice: str = "",
        skip_self_review: bool = False,
    ) -> Tuple[Dict[str, Any], str]:
        """Audit a freshly emitted spec and self-revise (then re-audit) if needed.

        Pre: ``strategy_dict`` is a validated spec dict and ``rationale``
        is its accompanying rationale (the tuple returned by
        :meth:`_invoke_and_parse`). ``prior_critiques`` is the external
        design-review lineage threaded into the self-revision prompt
        (``None`` on initial generation, where no external round has run
        yet); callers must not append the current critique themselves — the
        orchestrator already appends it to the history before :meth:`revise`
        receives it. ``regression_notice`` is the external loop's
        "do not reintroduce" block (empty on the :meth:`run` path); it is
        threaded into the self-revision prompt so a self-revision cannot undo
        a prior-round defect the regression machinery is keeping fixed.
        ``skip_self_review`` is a caller-controlled fast path for specs
        already known to be structurally clean (the decision logic for
        when to set it lives with the caller, not here): when ``True`` the
        method returns immediately, making no LLM call at all — not even
        :meth:`_self_review`.
        Post: returns ``(strategy_dict, rationale)`` — either the input
        unchanged (``skip_self_review`` requested, self-review disabled,
        the spec is already ready, or a best-effort failure) or a
        self-revised spec. Whenever a self-revision fires, the revised spec
        has been re-audited through self-review at least once. Never raises
        except :class:`DesignBudgetExhausted`, which propagates so the
        cycle can stop; the external review loop is still authoritative —
        this is purely an internal pre-flight.
        Invariant: at most ``_design_self_revision_rounds()`` self-revisions
        fire per call, each followed by a re-audit; the loop cannot run
        unbounded.

        The self-review fires on every spec the designer emits — both
        initial generation from :meth:`run` and each external-loop
        revision from :meth:`revise` — because the recurring failure
        mode is the designer slipping the same prose ↔ predicate or
        risk-math contradiction on round after round of revisions. Re-
        auditing the self-revised spec closes the gap where a self-revision
        introduced a *new* contradiction that then reached the external
        reviewer unchecked.
        """
        if skip_self_review:
            return strategy_dict, rationale

        if not _design_self_review_enabled():
            return strategy_dict, rationale

        max_revisions = _design_self_revision_rounds()
        revisions_done = 0
        # audits == revisions + 1; ``range`` is a hard ceiling on top of the
        # explicit ``revisions_done`` guard below.
        for _ in range(max_revisions + 1):
            try:
                critique = self._self_review(strategy_dict)
            except DesignBudgetExhausted:
                # A budget trip is a cycle-level stop, not a self-review hiccup —
                # it must propagate to ``_run_design_loop`` rather than being
                # swallowed by the best-effort guard below.
                raise
            except Exception as exc:
                logger.warning(
                    "DesignAgent self-review failed (%s: %s); returning current spec",
                    type(exc).__name__,
                    exc,
                )
                return strategy_dict, rationale

            if critique.ready:
                return strategy_dict, rationale

            if revisions_done >= max_revisions:
                # Bounded: the (re-)audit still flags issues but we have spent
                # the self-revision budget. Defer the residual to the
                # authoritative external ``DesignReviewAgent`` loop rather than
                # churning here.
                logger.warning(
                    "DesignAgent self-review still flags issues after %d self-revision(s); "
                    "deferring to the external review loop",
                    revisions_done,
                )
                return strategy_dict, rationale

            # Self-revision: reuse the same revision template the external loop
            # uses so the LLM sees a familiar prompt shape, and thread the
            # external critique lineage so the self-revision cannot regress a
            # fix an earlier external round already extracted. Build the
            # ``prior_spec_json`` directly from the dict (no need to round-trip
            # through ``StrategySpec`` construction).
            spec_json = json.dumps(strategy_dict, indent=2, sort_keys=True)
            issues_block = _format_issues(critique)
            revision_prompt = _REVISION_USER_TEMPLATE.format(
                prior_spec_json=spec_json,
                ready="false",
                rationale=critique.rationale or "(self-review flagged issues)",
                issues_block=issues_block,
                n_prior_critiques=len(prior_critiques) if prior_critiques else 0,
                prior_critiques_block=format_prior_critiques(prior_critiques),
                # Thread the external regression notice (empty on the run() path)
                # so a self-revision cannot undo a prior-round defect the external
                # loop is keeping fixed.
                regression_notice_block=regression_notice or "None.",
                response_schema_json=_DESIGN_SPEC_SCHEMA_JSON,
            )

            try:
                strategy_dict, rationale = self._invoke_and_parse(
                    _get_design_system_prompt(), revision_prompt
                )
            except DesignBudgetExhausted:
                # As above: propagate budget exhaustion rather than falling back.
                raise
            except Exception as exc:
                # Best-effort contract: any self-revision failure — DSL parse
                # rejection (``StrategySpecParseError``), malformed JSON
                # (``ValueError`` from ``extract_json_object``), or LLM
                # transport error — must fall back to the last valid spec. We
                # return here rather than re-auditing, so a failed revision
                # never triggers a re-audit on an unrevised spec.
                logger.warning(
                    "DesignAgent self-revision failed (%s: %s); returning pre-revision spec",
                    type(exc).__name__,
                    exc,
                )
                return strategy_dict, rationale

            revisions_done += 1

        # Unreachable in practice: the ``revisions_done >= max_revisions`` guard
        # returns before the ``range`` ceiling is exhausted. Kept so a
        # type-checker sees a definite return.
        return strategy_dict, rationale  # pragma: no cover

    def _self_review(self, strategy_dict: Dict[str, Any]) -> "SpecCritique":
        """Audit ``strategy_dict`` for prose↔predicate + risk-math contradictions.

        Pre: ``strategy_dict`` is a parsed, DSL-valid spec dict (no
        ``strategy_code`` key).
        Post: returns a :class:`SpecCritique`. Best-effort — JSON parse
        failures and most LLM transport failures raise and the caller falls
        back to the original spec. When structured output is enabled, a
        reasoning-pass ``LLMSemanticExhaustionError`` with
        ``schema_forced=True`` does not raise: it degrades to the legacy
        single-shot call instead, which only then raises if it also fails.
        """
        spec_json = json.dumps(strategy_dict, indent=2, sort_keys=True)
        user_prompt = (
            "Audit the following candidate StrategySpec for the two "
            "failure modes named in the system prompt. Return ONLY the JSON "
            "verdict, no markdown.\n\n"
            f"```json\n{spec_json}\n```\n"
        )

        def _invoke_legacy() -> Dict[str, Any]:
            agent = Agent(
                model=get_strands_model("strategy_design"),
                system_prompt=_get_self_review_system_prompt(),
                tools=[],
            )
            return run_structured_agent(
                agent,
                user_prompt,
                agent_key="strategy_design",
                phase="design_self_review",
                parse=extract_json_object,
                charge=True,
                logger=logger,
            )

        parsed = so.try_structured_or_degrade(
            "strategy_design",
            CRITIQUE_SCHEMA,
            _get_self_review_system_prompt(),
            user_prompt,
            so.build_reasoning_system_prompt(_get_self_review_system_prompt()),
            phase="design_self_review_structured",
            charge=True,
            objective="strategy design review (structured)",
            logger=logger,
        )
        if parsed is None:
            parsed = _invoke_legacy()
        # Self-review tolerates advisory warnings on an otherwise-ready
        # verdict: the self-review LLM routinely flags minor notes as
        # warnings while still satisfied with the spec. Only a *critical*
        # finding is a real prose↔predicate contradiction worth a
        # self-revision round, so demote only on critical here. (The
        # external DesignReviewAgent keeps the stricter default, where any
        # non-info issue demotes.)
        #
        # The sizing carve-out only applies to gate-owned sizing kinds; a
        # ``volatility_target`` plausibility objection (which the deterministic
        # gate abstains on) must keep blocking, so resolve ``sizing_owned`` from
        # the draft's sizing kind rather than assuming it.
        sizing_kind = (strategy_dict.get("sizing") or {}).get("kind")
        return _coerce_critique(
            parsed,
            readiness_findings=[],
            demote_min_severity="critical",
            sizing_owned=_sizing_owned_by_gate(sizing_kind),
        )


def _design_self_review_enabled() -> bool:
    """Resolve the on/off toggle for :meth:`DesignAgent._with_self_review`.

    Reads ``STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED`` (default ``true``;
    accepted truthy values are ``"true"`` / ``"1"`` / ``"yes"``, case-
    insensitive; anything else is treated as ``false``). When disabled,
    the designer skips the internal self-review and self-revision loop in
    ``_with_self_review``; the external review loop and the main
    generation/revision calls (``run()`` / ``revise()`` via
    ``_invoke_and_parse``) remain unchanged.
    """
    raw = os.environ.get("STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED", "true")
    return raw.strip().lower() in {"true", "1", "yes"}


def _design_self_revision_rounds() -> int:
    """Resolve the cap on internal self-revision rounds in ``_with_self_review``.

    Reads ``STRATEGY_LAB_DESIGN_SELF_REVISION_ROUNDS`` (default ``1``,
    sub-zero values floored to ``0``). One round is one self-revision LLM
    call followed by a re-audit through self-review; ``0`` disables
    self-revision entirely (audit-only). Garbage values fall back to the
    default rather than raising — the surrounding cycle is best-effort.

    Pre: none.
    Post: returns an ``int >= 0``.
    """
    return StrategyLabBudgetConfig.from_env().design_self_revision_rounds


_CORRECTION_PREAMBLE = """\
Your previous JSON response was rejected by the DSL validator. Reissue
the ENTIRE JSON object with the offending field fixed; do not change any
other rule that was not flagged.

Offending field: {field}
Rejected payload: {payload}
Validator error:
{pydantic_error}

Read the system prompt's DSL section before retrying. Common drifts that
match what you just emitted:

- Bar-field literals ("bar.close", "bar.high", "bar.low", "bar.volume")
  appear as BARE STRINGS on a Predicate's `lhs` or `rhs`. They must NOT
  be wrapped in IndicatorRef shape — `{{"name": "bar.close"}}` is invalid
  because `bar.close` is not an IndicatorName.

- `IndicatorRef.source` accepts only price/volume bar fields ("close",
  "high", "low", "open", "volume", "hl2", "ohlc4"). It cannot be an
  indicator name (e.g. `source: "atr"`). The DSL has no
  indicator-of-indicator form — express the same idea with a primitive
  indicator from the catalogue or by comparing the indicator against a
  numeric constant or a bar-field literal.

- `source` is a TOP-LEVEL field on `IndicatorRef`, not a member of
  `params`. Each indicator's `params` schema accepts only the keys listed
  in the catalogue (e.g. `sma`/`ema` accept only `period`); putting
  `source` inside `params` trips the "unexpected param" validator.
  Correct shape: `{{"name": "sma", "params": {{"period": 20}}, "source": "volume"}}`.

--- ORIGINAL TASK BELOW ---
{original_prompt}
"""


def _build_correction_prompt(user_prompt: str, exc: "StrategySpecParseError") -> str:
    """Render a re-prompt that quotes the offending field and pydantic error.

    Pre: ``exc`` carries ``field``, ``payload``, and a chained
    ``ValidationError`` accessible via ``exc.__cause__``.
    Post: returns a string that the LLM can read as "your last attempt
    failed for this specific reason; reissue the corrected JSON."
    """
    cause = exc.__cause__ or exc
    payload = (
        exc.payload
        if isinstance(exc.payload, str)
        else json.dumps(exc.payload, indent=2, default=str)
    )
    return _CORRECTION_PREAMBLE.format(
        field=exc.field,
        payload=payload,
        pydantic_error=str(cause),
        original_prompt=user_prompt,
    )


def _finalize_parsed(parsed: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    """Strip stray ``strategy_code``, pop ``rationale``, and validate DSL rule shape.

    Shared by both :meth:`DesignAgent._structured_preflight` and
    :meth:`DesignAgent._legacy_parse_retry_loop` so the post-processing
    contract is identical regardless of how ``parsed`` was obtained.

    Pre: ``parsed`` is a JSON-shape-valid dict (JSON-schema-conformant via
    structured decoding, or brace-extracted via :func:`extract_json_object`).
    Post: returns ``(parsed, rationale)`` where ``parsed`` is the **same dict
    instance** mutated in place (``strategy_code`` and ``rationale`` popped)
    and ``rationale`` defaults to ``""`` when absent. Callers that reuse the
    input dict elsewhere will observe those keys removed.
    Raises :class:`StrategySpecParseError` when a rule-shaped field fails
    :func:`validate_structured_rules` — JSON-shape validity is a distinct
    guarantee from DSL semantic validity, and this is the one gate that
    enforces the latter regardless of path.
    """
    # Logged-and-dropped (not raised): a stray ``strategy_code`` is
    # prompt drift, not a usable-spec failure.
    if "strategy_code" in parsed:
        parsed.pop("strategy_code", None)
        logger.warning(
            "DesignAgent stripped stray strategy_code field from LLM response "
            "(code synthesis is a separate phase)."
        )

    rationale = parsed.pop("rationale", "")
    validate_structured_rules(parsed)
    return parsed, rationale


def _format_issues(critique: "SpecCritique") -> str:
    """Render critique issues as a short, deterministic block.

    Pre: ``critique`` is a :class:`SpecCritique` whose ``issues`` sequence
    (possibly empty) should be rendered for a revision or self-audit prompt.
    Post: returns a newline-joined string of numbered issue lines
    (``N. [severity] field: description`` with an optional ``Fix:``
    suffix when ``suggested_fix`` is set), or
    ``"(no specific issues — see rationale)"`` when ``critique.issues``
    is empty.
    """
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
