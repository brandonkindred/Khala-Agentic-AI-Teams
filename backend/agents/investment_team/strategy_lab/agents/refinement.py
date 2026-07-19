"""Strands Agent for refining strategy code after quality gate failures."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from strands import Agent

from llm_service import provider_supports_structured_output
from llm_service.config import resolve_provider
from llm_service.interface import LLMSemanticExhaustionError

from ...models import BacktestResult, StrategySpec
from ..exceptions import StrategyLabLLMError
from ._llm_envelope import run_structured_agent
from ._parse_helpers import (
    build_json_correction_prompt,
    extract_json_object,
    parse_retry_budget,
)
from ._prompt_context import render_prior_attempts, spec_prompt_fields
from ._response_schemas import REFINEMENT_SCHEMA
from .model_factory import get_strands_model

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


def _structured_output_available() -> bool:
    """Whether the active LLM provider supports provider-enforced schema-conformant decoding.

    A dedicated seam (rather than inlining the two-call chain at each use
    site) so tests can force either branch deterministically without
    depending on ambient ``LLM_PROVIDER`` env state — see
    ``test_strategy_lab_refinement_parse_retry.py``'s autouse fixture.

    Preconditions: none.
    Postconditions: synchronous, no network call, never raises.
    """
    return provider_supports_structured_output(resolve_provider())


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

    def _invoke_structured(
        self, system_prompt: str, user_prompt: str, failure_phase: str
    ) -> Dict[str, Any]:
        """Request provider-enforced schema-conformant decoding for one refinement round.

        Bypasses ``strands.Agent`` (whose ``stream()``/``structured_output()``
        do not forward a ``schema`` to the backing client) and calls
        ``LLMClient.complete_json(..., schema=REFINEMENT_SCHEMA)`` directly,
        still routed through :func:`run_structured_agent` for the same
        charge/invoke/timeout/parse envelope every other call site uses.

        Preconditions: ``_structured_output_available()`` is True (checked by
        the caller, :meth:`_invoke_and_parse`, before calling this — only the
        Ollama provider answers True today, and only Ollama's branch of
        ``get_strands_model`` returns an adapter exposing ``.client``);
        ``system_prompt`` / ``user_prompt`` are non-empty strings.
        Postconditions: returns the parsed JSON dict on success. Raises
        :class:`~..exceptions.StrategyLabLLMError` on any transport/parse
        failure — including a ``schema_forced`` semantic-exhaustion
        starvation signal (``LLMSemanticExhaustionError.schema_forced``),
        which the caller inspects to decide whether to degrade to the
        unconstrained parse-retry loop. This method never falls back itself
        and never retries: a schema-conformant decode either succeeds or
        signals starvation — there is no "malformed JSON" middle state for a
        correction re-prompt to recover from.
        """
        client = get_strands_model("strategy_refinement").client

        def _call(prompt: str) -> str:
            result = client.complete_json(
                prompt,
                objective="strategy refinement (structured)",
                system_prompt=system_prompt,
                schema=REFINEMENT_SCHEMA,
            )
            # invoke_agent unconditionally does str(result) on whatever this
            # callable returns before handing it to `parse` — a raw dict
            # would come back as Python repr (single-quoted, True/False/None),
            # which extract_json_object cannot parse. json.dumps re-renders
            # it as valid JSON so the round trip is exact.
            return json.dumps(result)

        return run_structured_agent(
            _call,
            user_prompt,
            agent_key="strategy_refinement",
            phase="refinement_structured",
            parse=extract_json_object,
            charge=True,
            logger=logger,
        )

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
        decoding (:func:`_structured_output_available`), a single
        schema-constrained call is attempted first via
        :meth:`_invoke_structured` — no parse-retry loop needed, since a
        conformant decode cannot emit unparseable JSON. Any failure OTHER
        than a ``schema_forced`` starvation signal propagates immediately
        (unchanged fail-fast semantics for a genuine transport/auth
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
        if _structured_output_available():
            try:
                return self._invoke_structured(system_prompt, user_prompt, failure_phase)
            except StrategyLabLLMError as exc:
                cause = exc.cause
                if not (isinstance(cause, LLMSemanticExhaustionError) and cause.schema_forced):
                    raise
                logger.warning(
                    "structured refinement decode starved (schema_forced) for "
                    "failure_phase=%s; degrading to unconstrained parse-retry loop.",
                    failure_phase,
                )

        retries = parse_retry_budget("STRATEGY_LAB_REFINEMENT_PARSE_RETRIES")
        prompt = user_prompt
        for attempt in range(retries + 1):
            agent = Agent(
                model=get_strands_model("strategy_refinement"),
                system_prompt=system_prompt,
                tools=[],
            )
            try:
                return run_structured_agent(
                    agent,
                    prompt,
                    agent_key="strategy_refinement",
                    phase="refinement",
                    parse=extract_json_object,
                    charge=True,
                    logger=logger,
                )
            except ValueError as exc:
                logger.warning(
                    "RefinementAgent emitted unparseable JSON (attempt %d/%d) "
                    "for failure_phase=%s: %s",
                    attempt + 1,
                    retries + 1,
                    failure_phase,
                    exc,
                )
                if attempt >= retries:
                    raise
                prompt = build_json_correction_prompt(
                    user_prompt, exc, keys_hint=_CORRECTION_KEYS_HINT
                )

        # Unreachable: the loop returns on success or re-raises on the final
        # attempt. Kept so type-checkers see a definite return.
        raise AssertionError("unreachable: _invoke_and_parse loop exited without return")
