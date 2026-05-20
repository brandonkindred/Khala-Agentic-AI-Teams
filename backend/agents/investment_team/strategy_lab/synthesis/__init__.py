"""Deterministic StrategySpec → canonical Python compiler (issue #538).

``compile_strategy(spec)`` turns a structured ``StrategySpec`` into the
``Strategy`` subclass the streaming harness expects. Pure function: the
same spec always produces byte-identical output (modulo a deterministic
content-hash header). Output is shaped to pass ``CodeSafetyChecker`` and
``CodeConformanceGate`` by construction; stop-loss / take-profit
enforcement is left to the engine's ``evaluate_exit_rules`` rather than
inlined in ``on_bar``.
"""

from .compiler import CompilerError, compile_strategy

__all__ = ["CompilerError", "compile_strategy"]
