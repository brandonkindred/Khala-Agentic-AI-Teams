"""Per-rule synthetic-bar behavioural probes.

The structural :class:`CodeConformanceGate` checks that the compiled
strategy code *looks* like it implements the spec (right indicators,
right exit branches, right sizing math). It can still be fooled by code
that's shaped right but wired wrong — a swapped comparator, a negated
exit predicate, an entry branch whose order submission is silently
unreachable.

This package adds the behavioural complement: for every rule in the
spec we synthesise a deterministic bar sequence designed to force that
rule's predicates to evaluate ``True``, run the compiled code through
the existing :func:`run_strategy_code` sandbox, and assert the resulting
``TradeRecord`` envelope matches the expected order / exit. Probe
failures route back to synthesis with the failing ``rule_id`` so the
refinement agent can target the right code branch.

The gate slots in after :class:`CodeConformanceGate` in the synthesis
loop, before the real backtest — failing fast on rules that don't fire,
without paying for a full backtest cycle to find out.

Public surface:

- :class:`RuleProbesGate` — the quality gate the orchestrator invokes.
- :class:`ProbeRun` — one probe's synthetic input + expected outcome.
- :class:`ExpectedOutcome` — what the assertion layer looks for.
"""

from __future__ import annotations

from .asserter import assess_probe
from .gate import RuleProbesGate
from .synthesizer import ExpectedOutcome, ProbeRun, generate_rule_probe_runs

__all__ = [
    "ExpectedOutcome",
    "ProbeRun",
    "RuleProbesGate",
    "assess_probe",
    "generate_rule_probe_runs",
]
