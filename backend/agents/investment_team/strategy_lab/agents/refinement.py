"""Strands Agent for refining strategy code after quality gate failures."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from llm_service.interface import LLMSemanticExhaustionError

from ...models import BacktestResult, StrategySpec
from ..budget_config import StrategyLabBudgetConfig
from ..exceptions import StrategyLabLLMError
from . import _structured_output as so
from ._agent_runner import run_json_with_parse_retry
from ._llm_budget import charge_active_budget
from ._parse_helpers import build_json_correction_prompt
from ._prompt_context import render_prior_attempts, spec_prompt_fields
from ._response_schemas import REFINEMENT_SCHEMA

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Loaded once at import — the system prompt is static, so re-reading it from disk
# on every refinement round is wasted I/O.
_SYSTEM_PROMPT = (_PROMPT_DIR / "refinement_system.md").read_text(encoding="utf-8")

# The JSON Schema the LLM response must conform to, rendered once for
# injection into the prompt. The Ollama transport routes through the
# ``llm_service`` client in ``json_object`` wire mode (see ``get_strands_model``),
# which forces a JSON object on the wire but not a specific shape; this
# prompt-embedded schema — together with the pydantic narrowing below — is what
# pins the response to the expected ``strategy_code`` / ``changes_made`` shape.
_REFINEMENT_SCHEMA_JSON = json.dumps(REFINEMENT_SCHEMA, indent=2)

# Spliced into the shared JSON-correction re-prompt so a malformed-output retry
# still names the exact two keys the refinement response must carry. Leading
# space so it reads as a sentence continuation in the shared preamble.
_CORRECTION_KEYS_HINT = (
    " Emit exactly the two keys `strategy_code` (the complete fixed Python "
    "code) and `changes_made` (a 1-2 sentence summary)."
)

# Refinement output is code-only (#543). ``strategy_code`` is consumed
# separately; ``changes_made`` is logged. Any other top-level key in the
# LLM payload is dropped with a warning. ``risk_limits`` is passed through
# to the orchestrator, which applies tighten-only semantics (see
# ``orchestrator._merge_risk_limits_tighten_only``).
_ALLOWED_OUTPUT_KEYS = frozenset({"changes_made"})
_PASSTHROUGH_FOR_ORCHESTRATOR = frozenset({"risk_limits"})

_REFINEMENT_USER_TEMPLATE = """\
Fix the following trading strategy code that failed {failure_phase}.

## Current Strategy
Asset class: {asset_class}
Hypothesis: {hypothesis}
Entry rules: {entry_rules}
Exit rules: {exit_rules}
Sizing rules: {sizing_rules}
Risk limits: {risk_limits}

## Current Code
```python
{strategy_code}
```

## Failure Details
{failure_details}

{metrics_section}

## Prior Refinement Attempts ({n_prior_attempts} so far)
{prior_attempts_text}

## Instructions
1. Diagnose the root cause from the failure details.
2. Fix the code only. Do NOT alter the strategy spec (entry/exit/sizing
   rules, risk limits, hypothesis) — spec changes go through ideation,
   not refinement. The rules above are rendered text views of structured
   DSL objects; do NOT emit them back in your response.
3. Ensure your fix doesn't re-introduce any previously fixed issues.

## Response format — JSON only
Respond with a SINGLE valid JSON object and nothing else: no markdown
fences, no prose before or after, every brace balanced. Your response
MUST conform to this JSON Schema:

```json
{response_schema_json}
```

Concretely, emit exactly these keys (omit `risk_limits` unless you are
tightening a risk limit):
{{
  "strategy_code": "the complete fixed Python code",
  "changes_made": "1-2 sentence summary of what you changed and why"
}}
"""


class RefinementAgent:
    """Refine strategy code based on quality gate or execution failures."""

    def __init__(self) -> None:
        # Audit trail of every refinement round where the LLM emitted
        # spec-mutating keys. Used by tests and surfaceable in logs to
        # diagnose prompt drift. Each entry: {"failure_phase": str,
        # "keys": list[str]}.
        self.spec_mutation_history: List[Dict[str, Any]] = []

    def run(
        self,
        spec: StrategySpec,
        code: str,
        failure_phase: str,
        failure_details: str,
        metrics: Optional[BacktestResult] = None,
        prior_attempts: Optional[List[str]] = None,
    ) -> Tuple[Dict[str, Any], str]:
        """Refine the strategy code.

        Returns:
            (updated_fields_dict, updated_code)

        ``updated_fields_dict`` is narrowed to ``{"changes_made"}`` plus an
        optional ``"risk_limits"`` passthrough (the orchestrator applies
        tighten-only semantics). Any other top-level keys in the LLM
        response are logged and discarded.

        Raises:
            :class:`~._llm_budget.DesignBudgetExhausted`: If the active per-cycle design
                LLM budget is spent (raised by :func:`charge_active_budget` on
                the legacy parse-retry path, or by the structured path when
                ``charge=True``).
            StrategyLabLLMError: If the LLM envelope exhausts transport retries
                or hits a fatal LLM error.
            ValueError: If the parse-retry budget is exhausted without recovering
                a balanced JSON object.
        """
        system_prompt = _SYSTEM_PROMPT

        metrics_section = ""
        if metrics:
            metrics_section = (
                f"## Backtest Metrics (for context)\n"
                f"Annualized: {metrics.annualized_return_pct:.1f}% | "
                f"Total: {metrics.total_return_pct:.1f}% | "
                f"Sharpe: {metrics.sharpe_ratio:.2f} | "
                f"Max DD: {metrics.max_drawdown_pct:.1f}% | "
                f"Win rate: {metrics.win_rate_pct:.1f}% | "
                f"Profit factor: {metrics.profit_factor:.2f}"
            )

        prior_text = render_prior_attempts(prior_attempts)

        user_prompt = _REFINEMENT_USER_TEMPLATE.format(
            failure_phase=failure_phase,
            **spec_prompt_fields(spec),
            strategy_code=code,
            failure_details=failure_details,
            metrics_section=metrics_section,
            n_prior_attempts=len(prior_attempts) if prior_attempts else 0,
            prior_attempts_text=prior_text,
            response_schema_json=_REFINEMENT_SCHEMA_JSON,
        )

        parsed = self._invoke_and_parse(system_prompt, user_prompt, failure_phase)

        updated_code = parsed.pop("strategy_code", code)

        stray = set(parsed) - _ALLOWED_OUTPUT_KEYS - _PASSTHROUGH_FOR_ORCHESTRATOR
        if stray:
            logger.warning(
                "RefinementAgent emitted spec-mutating keys %s for failure_phase=%s; "
                "discarding (refinement is code-only post-#543).",
                sorted(stray),
                failure_phase,
            )
            self.spec_mutation_history.append(
                {"failure_phase": failure_phase, "keys": sorted(stray)}
            )

        narrowed: Dict[str, Any] = {
            k: parsed[k]
            for k in (_ALLOWED_OUTPUT_KEYS | _PASSTHROUGH_FOR_ORCHESTRATOR)
            if k in parsed
        }
        return narrowed, updated_code

    def _invoke_and_parse(
        self, system_prompt: str, user_prompt: str, failure_phase: str
    ) -> Dict[str, Any]:
        """Call the LLM and recover its JSON object, retrying on unparseable output.

        Preconditions: ``system_prompt`` / ``user_prompt`` are non-empty
        strings; ``failure_phase`` is a non-empty diagnostic label.
        Postconditions: returns the parsed JSON dict for the LLM's response.
        Raises ``ValueError`` when the retry budget is exhausted and no
        balanced JSON object could be recovered, or
        :class:`~..exceptions.StrategyLabLLMError` when the envelope exhausts
        its transport retries / budget.

        When the active provider supports provider-enforced structured
        decoding (:func:`so.structured_output_available`), a reasoning pass
        followed by a schema-constrained formatting pass is attempted first
        via :func:`so.invoke_structured_with_schema` (two sequential calls
        under one budget/timeout envelope) — no parse-retry loop needed,
        since a conformant decode cannot emit unparseable JSON. Any
        failure OTHER than a ``schema_forced`` starvation signal propagates
        immediately (unchanged fail-fast semantics for a genuine transport/auth
        failure) rather than degrading — a deliberate, narrow reading of
        this call site's degrade contract, not an oversight. On capability
        absence, or on ``schema_forced`` starvation specifically, this falls
        through to the unconstrained parse-retry loop below, reproducing
        today's behavior exactly.

        Why retry here: the LLM occasionally returns an empty, thinking-only,
        or prose-only response with no JSON object. That is not a transport
        fault — ``invoke_agent`` sees a "successful" string and returns it — so
        the envelope cannot recover it. A code-only refinement asks the model
        to emit the *complete* fixed program as a JSON string, which is exactly
        the kind of long generation prone to this slip. Without a retry, a
        single such response wastes the whole refinement round (the orchestrator
        falls back to the unchanged code). Re-prompting with the parse error as
        feedback recovers the common transient case; the budget bounds the rare
        persistent one. Mirrors :meth:`DesignAgent._invoke_and_parse`.

        Each attempt builds a fresh ``Agent`` deliberately: ``strands.Agent``
        accumulates conversation history, so reusing one instance would feed
        the model its own unparseable output back as context. The correction
        re-prompt must be read as "reissue the whole object correctly", not
        "continue from what you emitted".
        """
        structured_available = so.structured_output_available()
        if structured_available:
            try:
                result = so.invoke_structured_with_schema(
                    "strategy_refinement",
                    system_prompt,
                    user_prompt,
                    phase="refinement_structured",
                    schema=REFINEMENT_SCHEMA,
                    charge=True,
                    objective="strategy refinement (structured)",
                    logger=logger,
                    reasoning_system_prompt=so.build_reasoning_system_prompt(system_prompt),
                )
            except StrategyLabLLMError as exc:
                cause = exc.cause
                if not (isinstance(cause, LLMSemanticExhaustionError) and cause.schema_forced):
                    raise
                logger.warning(
                    "structured refinement decode starved (schema_forced) for "
                    "failure_phase=%s; degrading to unconstrained parse-retry loop.",
                    failure_phase,
                )
            else:
                logger.info(
                    "strategy_lab structured_output outcome=succeeded "
                    "agent=strategy_refinement phase=refinement_structured "
                    "failure_phase=%s",
                    failure_phase,
                )
                return result

        retries = StrategyLabBudgetConfig.from_env().refinement_parse_retries
        attempt_box = {"n": 0}

        def _on_parse_error(_base_prompt: str, exc: ValueError) -> str:
            attempt_box["n"] += 1
            logger.warning(
                "RefinementAgent emitted unparseable JSON (attempt %d/%d) "
                "for failure_phase=%s (structured_available=%s): %s",
                attempt_box["n"],
                retries + 1,
                failure_phase,
                structured_available,
                exc,
            )
            return build_json_correction_prompt(user_prompt, exc, keys_hint=_CORRECTION_KEYS_HINT)

        return run_json_with_parse_retry(
            agent_key="strategy_refinement",
            phase="refinement",
            system_prompt=system_prompt,
            base_user_prompt=user_prompt,
            retry_budget=retries,
            logger=logger,
            before_attempt=charge_active_budget,
            on_parse_error=_on_parse_error,
        )
