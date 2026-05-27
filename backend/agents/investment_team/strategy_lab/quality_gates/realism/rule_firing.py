"""Spec-rule firing rate realism gate.

A strategy whose entry rules never fire in the backtest is one where
the rule was dead code — the predicate was unreachable given the data,
or the conditions never aligned at runtime. Such a strategy is less
than what the spec describes and should not be published as implementing
the full spec.

This gate reads ``TradeRecord.entry_reason`` and ``exit_reason``
(populated by the fill simulator from the engine dispatchers' reason
annotations) and counts how many trades cite each spec rule.

Reason prefixes matched:
  - Entries: ``engine_entry:entry[N]`` (engine-managed) or
    ``compiled_entry:entry[N]`` (legacy/custom-code).
  - Signal exits: ``engine_exit:signal_exit[N]`` (engine-managed) or
    ``compiled_signal_exit:exit[N]`` (legacy/custom-code).

Rules with zero citations are flagged:

* **critical** — an entry rule that never fired.
* **warning** — a ``SignalExitRule`` that never fired (signal exits
  compete with stop-loss/take-profit which fire first in the engine).
* **info-skip** — when ``spec.requires_custom_code=True`` (LLM-authored
  code path), the reason annotation format is unpredictable, so the
  gate has no signal to evaluate.

Wired from
:meth:`StrategyLabOrchestrator._run_realism_gates`.
"""

from __future__ import annotations

import re
from typing import ClassVar, List, Optional, Sequence

from ....models import StrategySpec, TradeRecord
from ..models import GateResultsMixin, QualityGateResult, StrategyLabPhase

GATE = "rule_firing_rate_realism"

_ENTRY_REASON_RE = re.compile(r"^(?:compiled_entry|engine_entry):entry\[(\d+)]$")
_EXIT_REASON_RE = re.compile(r"^(?:compiled_signal_exit:exit|engine_exit:signal_exit)\[(\d+)]$")


class RuleFiringRateGate(GateResultsMixin):
    """Verification-phase gate over per-rule trade citation counts.

    Contract:
      Pre: ``spec`` is a :class:`StrategySpec`; ``trades`` is a list of
      :class:`TradeRecord` with ``entry_reason`` / ``exit_reason``
      populated by the fill simulator.
      Post: returns one or more :class:`QualityGateResult`s. ``critical``
      for unfired entry rules; ``warning`` for unfired signal-exit
      rules; ``info`` when all rules fired or the gate self-skips.
      Invariants: deterministic; never returns an empty list.
    """

    GATE: ClassVar[str] = GATE

    def check(
        self,
        spec: StrategySpec,
        trades: List[TradeRecord],
        *,
        open_position_entry_reasons: Optional[Sequence[str]] = None,
        phase: StrategyLabPhase = "verification",
    ) -> List[QualityGateResult]:
        with self._using_phase(phase):
            if spec.requires_custom_code:
                return [
                    self._info(
                        "Rule firing rate check skipped: spec.requires_custom_code "
                        "is True — the compiler's reason annotation is absent in "
                        "LLM-authored code, so per-rule firing rates can't be "
                        "evaluated."
                    )
                ]

            results: List[QualityGateResult] = []

            entry_hits = _count_entry_hits(trades, open_position_entry_reasons or ())
            for idx, rule in enumerate(spec.entry_rules):
                rule_key = f"entry[{idx}]"
                count = entry_hits.get(idx, 0)
                if count == 0:
                    results.append(
                        self._critical(
                            f"Entry rule {rule_key} (side={rule.side}) never "
                            f"fired across {len(trades)} trades — the "
                            "predicate was dead code in this backtest window.",
                            rule_id=rule_key,
                        )
                    )

            from ...spec_dsl import SignalExitRule

            exit_hits = _count_exit_hits(trades)
            for idx, rule in enumerate(spec.exit_rules):
                if not isinstance(rule, SignalExitRule):
                    continue
                rule_key = f"exit[{idx}]"
                count = exit_hits.get(idx, 0)
                if count == 0:
                    results.append(
                        self._warning(
                            f"Signal-exit rule {rule_key} never fired across "
                            f"{len(trades)} trades — the predicate may be "
                            "unreachable or superseded by stop-loss / "
                            "take-profit exits.",
                            rule_id=rule_key,
                        )
                    )

            if not results:
                results.append(
                    self._info(
                        f"All {len(spec.entry_rules)} entry rule(s) and "
                        f"applicable exit rules fired at least once across "
                        f"{len(trades)} trades."
                    )
                )

            return results


def _count_entry_hits(
    trades: List[TradeRecord],
    open_position_entry_reasons: Sequence[str] = (),
) -> dict:
    """Return ``{rule_index: count}`` from ``entry_reason`` annotations.

    Unions closed-trade ``entry_reason`` fields with
    ``open_position_entry_reasons`` (positions still held at
    end-of-stream) so a rule whose only firing left an unclosed
    position is not misreported as dead code.

    Postconditions:
      - Trades with ``entry_reason=None`` or non-matching strings are
        silently skipped (they don't contribute to any rule's count).
    """
    hits: dict = {}
    reasons = [t.entry_reason for t in trades if t.entry_reason]
    reasons.extend(open_position_entry_reasons)
    for reason in reasons:
        m = _ENTRY_REASON_RE.match(reason)
        if m:
            idx = int(m.group(1))
            hits[idx] = hits.get(idx, 0) + 1
    return hits


def _count_exit_hits(trades: List[TradeRecord]) -> dict:
    """Return ``{rule_index: count}`` from ``exit_reason`` annotations.

    Postconditions:
      - Matches ``engine_exit:signal_exit[N]`` (engine-managed) and
        ``compiled_signal_exit:exit[N]`` (legacy/custom-code). Other
        engine exits (``engine_exit:stop_loss[N]``, etc.) are ignored
        — they're not signal-exit rules.
    """
    hits: dict = {}
    for t in trades:
        if not t.exit_reason:
            continue
        m = _EXIT_REASON_RE.match(t.exit_reason)
        if m:
            idx = int(m.group(1))
            hits[idx] = hits.get(idx, 0) + 1
    return hits


__all__ = ["GATE", "RuleFiringRateGate"]
