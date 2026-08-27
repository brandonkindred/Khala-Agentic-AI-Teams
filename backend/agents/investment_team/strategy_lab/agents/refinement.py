"""Strands Agent for refining strategy code after quality gate failures."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ...models import BacktestResult, StrategySpec
from ..budget_config import StrategyLabBudgetConfig
from . import _structured_output as so
from ._agent_runner import run_json_with_parse_retry
from ._diff_format import diff_or_full
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

# Wired into the "## Current Code" prompt section: round 1 (no previous
# round) always renders the full file, byte-identical to the original
# hardcoded template, so every existing single-round test is unaffected.
# Rounds after the first render a compact unified diff against the previous
# round's code when ``diff_or_full`` finds one smaller than the full text;
# otherwise (near-total rewrite) it still falls back to the full file. This
# preamble is what lets the model tell the two shapes apart without a
# wrapper key in the JSON response schema.
_DIFF_CODE_SECTION_PREAMBLE = (
    "This is a unified diff against the previous round's code, not the "
    "full file. Reconstruct the current file from context, then respond "
    "with the complete fixed file."
)


def _render_code_section(code: str, diffed: str, *, is_diff: bool) -> str:
    """Render the "## Current Code" section body: full code, or an explained diff.

    Preconditions: ``code`` is the current round's full strategy code;
    ``diffed`` is ``diff_or_full(previous_code, code)``'s result; ``is_diff``
    is ``True`` iff ``diffed`` is an actual diff (i.e. ``diffed != code``).

    Postconditions: when ``is_diff`` is ``False``, returns a fenced Python
    code block wrapping ``code`` verbatim — byte-identical to the original
    hardcoded template. When ``is_diff`` is ``True``, returns ``diffed``
    fenced as a ``diff`` block, preceded by an explanatory line so the model
    does not mistake it for the full file.
    """
    if not is_diff:
        return f"```python\n{code}\n```"
    return f"{_DIFF_CODE_SECTION_PREAMBLE}\n\n```diff\n{diffed}\n```"


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
{code_section}

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
    """Refine strategy code based on quality gate or execution failures.

    Invariants:
      * ``_previous_round_code`` tracks the ``code`` argument of the most
        recent ``run()`` call, so consecutive calls on one instance diff
        round-over-round. This is only meaningful across the
        sequential refinement rounds of a *single* strategy's run — callers
        must not share one instance across unrelated strategies or call
        ``run()`` out of round order. ``StrategyLabOrchestrator`` satisfies
        this: it constructs one throwaway orchestrator (and therefore one
        ``RefinementAgent``) per strategy run, and calls ``run()``
        sequentially as refinement rounds progress.
    """

    def __init__(self) -> None:
        # Audit trail of every refinement round where the LLM emitted
        # spec-mutating keys. Used by tests and surfaceable in logs to
        # diagnose prompt drift. Each entry: {"failure_phase": str,
        # "keys": list[str]}.
        self.spec_mutation_history: List[Dict[str, Any]] = []
        # Round-over-round diff state — see class Invariants.
        # ``None`` until the first ``run()`` call completes its prompt build.
        self._previous_round_code: Optional[str] = None

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

        The "## Current Code" prompt section sends ``code`` in full on the
        first call on this instance (no previous round to diff against).
        On later calls, it sends a compact unified diff against the ``code``
        argument of this instance's previous ``run()`` call when
        ``diff_or_full`` finds one smaller than the full text; otherwise it
        falls back to the full ``code``. Either way, this method's
        inputs/outputs are unchanged — the LLM is still asked for, and this
        method still returns, the complete fixed code.

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

        diffed = diff_or_full(self._previous_round_code, code)
        code_section = _render_code_section(code, diffed, is_diff=diffed != code)
        self._previous_round_code = code

        user_prompt = _REFINEMENT_USER_TEMPLATE.format(
            failure_phase=failure_phase,
            **spec_prompt_fields(spec),
            code_section=code_section,
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

        When structured decoding is available, a reasoning pass followed by
        a schema-constrained formatting pass is attempted first via
        :func:`so.try_structured_or_degrade`, which itself encapsulates the
        :func:`so.structured_output_available` check, the two sequential
        calls into :func:`so.invoke_structured_with_schema` under one
        budget/timeout envelope, and the degrade-on-``schema_forced`` logic.
        Any failure OTHER than a ``schema_forced`` starvation signal
        propagates immediately out of that helper (unchanged fail-fast
        semantics for a genuine transport/auth failure) rather than
        degrading — a deliberate, narrow reading of this call site's degrade
        contract, not an oversight. The helper returns the parsed dict on
        success; it returns ``None`` on capability absence, or on
        ``schema_forced`` starvation specifically, to signal degradation —
        at which point this method falls through to the unconstrained
        parse-retry loop below, reproducing today's behavior exactly.

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
        result = so.try_structured_or_degrade(
            "strategy_refinement",
            REFINEMENT_SCHEMA,
            system_prompt,
            user_prompt,
            so.build_reasoning_system_prompt(system_prompt),
            phase="refinement_structured",
            charge=True,
            objective="strategy refinement (structured)",
            logger=logger,
        )
        if result is not None:
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
