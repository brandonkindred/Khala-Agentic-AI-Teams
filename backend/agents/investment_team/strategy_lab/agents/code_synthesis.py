"""Strands Agent that synthesises Python code from a frozen ``StrategySpec``.

This agent runs **after** the design review loop has converged
(``SpecCritique.ready`` is true) and **only** when the deterministic
compiler cannot synthesise code for the spec — either because the spec
explicitly carries ``requires_custom_code=True``, or because
``compile_strategy`` raised :class:`CompilerError`.

Invariants:
  * Input ``spec`` is treated as read-only. The agent never returns
    spec fields; the result is a single Python source string.
  * On LLM transport failure the agent raises
    :class:`CodeSynthesisError`; the orchestrator routes that to the
    same short-circuit path the rest of the design pipeline uses.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from strands import Agent

from ...models import StrategySpec
from ..spec_dsl import format_rules_for_prompt, format_sizing_rule
from ._llm_envelope import invoke_agent
from .model_factory import get_strands_model

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
_SYSTEM_PROMPT = (_PROMPT_DIR / "code_synthesis_system.md").read_text(encoding="utf-8")

_CODE_SYNTHESIS_USER_TEMPLATE = """\
Implement the strategy specification below as a single Python module
targeting the event-driven ``contract.Strategy`` interface.

## Frozen Strategy Specification (read-only)
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

## Instructions

1. Implement every entry rule, exit rule, and sizing decision from the
   spec exactly. Do NOT add new rules; do NOT drop or weaken existing
   ones.
2. The ``UNIVERSE`` constant MUST equal the spec's ``target_symbols``
   set; if ``target_symbols`` is empty, use ``frozenset()`` and remove
   the symbol-universe guard inside ``on_bar``.
3. Output ONLY the Python module — no JSON envelope, no markdown
   fences, no prose preamble.
"""


class CodeSynthesisError(Exception):
    """Raised when :class:`CodeSynthesisAgent` cannot produce code.

    The orchestrator catches this and short-circuits the cycle (rather
    than waving the design phase through into a synthesis loop with no
    code), keeping the design-time contract intact.
    """


class CodeSynthesisAgent:
    """Generate Python code that implements a frozen ``StrategySpec``.

    Contract:
      Pre — ``spec`` has already passed design review; the orchestrator
            only invokes this agent when the deterministic compiler
            cannot produce code for it.
      Post — returns a non-empty Python source string. The spec object
            passed in is unchanged on return (caller's responsibility
            to copy if needed).
      Raises — :class:`CodeSynthesisError` on LLM transport failure
            or an empty/whitespace-only response.
    """

    def run(self, spec: StrategySpec) -> str:
        """Synthesise strategy code from a frozen spec."""
        assert isinstance(spec, StrategySpec), "spec must be a StrategySpec"

        user_prompt = _CODE_SYNTHESIS_USER_TEMPLATE.format(
            asset_class=spec.asset_class,
            hypothesis=spec.hypothesis,
            signal_definition=spec.signal_definition,
            timeframe=spec.timeframe,
            entry_rules=format_rules_for_prompt(spec.entry_rules),
            exit_rules=format_rules_for_prompt(spec.exit_rules),
            sizing_rules=format_sizing_rule(spec.sizing),
            target_symbols=list(spec.target_symbols),
            risk_limits=spec.risk_limits.model_dump_json(),
        )

        agent = Agent(
            model=get_strands_model("strategy_code_synthesis"),
            system_prompt=_SYSTEM_PROMPT,
            tools=[],
        )

        try:
            raw = invoke_agent(
                agent,
                user_prompt,
                agent_key="strategy_code_synthesis",
                phase="code_synthesis",
                logger=logger,
            )
        except Exception as exc:  # noqa: BLE001 — wrap any transport fault
            logger.warning("CodeSynthesisAgent transport failure: %s", exc)
            raise CodeSynthesisError(f"{type(exc).__name__}: {exc}") from exc

        code = _strip_code_fence(raw).strip()
        if not code:
            raise CodeSynthesisError("CodeSynthesisAgent returned empty code")

        return code


def _strip_code_fence(text: str) -> str:
    """Remove a leading/trailing markdown code fence if the LLM added one.

    Pre: ``text`` is a string. Post: if ``text`` is wrapped in a single
    ```python ... ``` fence, returns the fenced contents; otherwise
    returns ``text`` unchanged.
    """
    fence_match = re.match(r"^\s*```(?:python|py)?\s*\n(.*?)\n?```\s*$", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1)
    return text


__all__ = ["CodeSynthesisAgent", "CodeSynthesisError"]
