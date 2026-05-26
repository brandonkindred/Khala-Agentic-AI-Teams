"""Deterministic ``StrategySpec`` → Python compiler.

``compile_strategy(spec)`` turns a structured ``StrategySpec`` into the
``Strategy`` subclass the streaming harness expects.

Contract:
  Pre:  ``spec`` exposes ``target_symbols``, ``entry_rules``,
        ``exit_rules``, and ``sizing`` per the DSL.
  Post: returns a non-empty Python source string with exactly one
        ``Strategy`` subclass; the same spec always produces
        byte-identical output (modulo a deterministic content-hash
        header). Output is shaped to pass ``CodeSafetyChecker`` and
        ``CodeConformanceGate`` by construction. Stop-loss /
        take-profit enforcement is delegated to the engine's
        ``evaluate_exit_rules``; ``on_bar`` is entries + signal-exits
        only.
  Raises: :class:`CompilerError` when the spec falls outside the
        compiler's expressible subset (caller falls back to LLM
        synthesis by setting ``spec.requires_custom_code = True``).
"""

from .compiler import CompilerError, compile_strategy

__all__ = ["CompilerError", "compile_strategy"]
