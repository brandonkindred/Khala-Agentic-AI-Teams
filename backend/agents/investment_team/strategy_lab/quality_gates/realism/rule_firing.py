"""Spec-rule firing rate realism gate.

A strategy whose entry rules never fire in the backtest is one where
the rule was dead code — the predicate was unreachable given the data,
or the conditions never aligned at runtime. Such a strategy is less
than what the spec describes and should not be published as implementing
the full spec.

This gate has two independent signal sources for the same verdict
(critical for a dead entry rule, warning for a dead signal-exit rule):

* **Compiled path** (``spec.requires_custom_code=False``): reads
  ``TradeRecord.entry_reason`` / ``exit_reason`` (populated by the fill
  simulator from the engine dispatchers' reason annotations) and counts
  how many trades cite each spec rule. Reason prefixes matched:
    - Entries: ``engine_entry:entry[N]`` (engine-managed) or
      ``compiled_entry:entry[N]`` (legacy/custom-code).
    - Signal exits: ``engine_exit:signal_exit[N]`` (engine-managed) or
      ``compiled_signal_exit:exit[N]`` (legacy/custom-code).
* **Custom-code path** (``spec.requires_custom_code=True``): the reason
  annotation is absent/unpredictable for LLM-authored ``on_bar`` code, so
  this path instead consumes ``alignment_findings`` — the deterministic
  :class:`~investment_team.strategy_lab.quality_gates.alignment_checks.TradeAlignmentGate`
  ledger, which re-evaluates every spec rule's ``PredicateTree`` against
  the REAL market bars at each trade's actual signal bar and already runs
  unconditionally (compiled or custom-code). Counting
  ``AlignmentFinding(check_name="entry_signal", rule_id="entry[N]",
  passed=True)`` / ``AlignmentFinding(check_name="signal_exit",
  rule_id="exit:signal_exit[N]", passed=True)`` hits gives a rule-firing
  signal with the same semantics as the compiled path's reason-string
  counts, but derived from re-executed predicates instead of an
  annotation the custom code never reliably produces. When the caller
  has no ``alignment_findings`` to offer (``None``), the gate falls back
  to an **info-skip** — there is genuinely nothing to evaluate.

Wired from
:meth:`StrategyLabOrchestrator._run_realism_gates`.
"""

from __future__ import annotations

import re
from typing import ClassVar, Dict, List, Optional, Sequence

from ....models import StrategySpec, TradeRecord
from ...alignment_findings import AlignmentFinding
from ..models import GateResultsMixin, QualityGateResult, StrategyLabPhase

GATE = "rule_firing_rate_realism"

_ENTRY_REASON_RE = re.compile(r"^(?:compiled_entry|engine_entry):entry\[(\d+)]$")
_EXIT_REASON_RE = re.compile(r"^(?:compiled_signal_exit:exit|engine_exit:signal_exit)\[(\d+)]$")

_ENTRY_FINDING_RE = re.compile(r"^entry\[(\d+)]$")
_EXIT_FINDING_RE = re.compile(r"^exit:signal_exit\[(\d+)]$")


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
        alignment_findings: Optional[Sequence[AlignmentFinding]] = None,
        phase: StrategyLabPhase = "verification",
    ) -> List[QualityGateResult]:
        """Preconditions:
          ``spec`` is a :class:`StrategySpec`; ``trades`` is the closed-trade
          ledger for this run. ``alignment_findings`` (when given) is the
          :class:`~investment_team.strategy_lab.agents.alignment.TradeAlignmentReport`
          ledger for the same run — required to evaluate a
          ``requires_custom_code=True`` spec; ignored on the compiled path,
          which always has ``entry_reason``/``exit_reason`` to work with.

        Postconditions:
          Returns one or more :class:`QualityGateResult`s: ``critical`` for
          an entry rule with zero citations/hits, ``warning`` for a zero-hit
          ``SignalExitRule``, ``info`` when every rule fired or the gate
          self-skips (``requires_custom_code=True`` and
          ``alignment_findings is None``). Never returns an empty list.

        Invariants: deterministic; the same hits-per-rule-index verdict
        rendering (:func:`_entry_rule_results` / :func:`_exit_rule_results`)
        is shared by both signal sources.
        """
        from ...spec_dsl import SignalExitRule

        with self._using_phase(phase):
            if spec.requires_custom_code:
                if alignment_findings is None:
                    return [
                        self._info(
                            "Rule firing rate check skipped: spec.requires_custom_code "
                            "is True and no alignment findings were supplied — the "
                            "compiler's reason annotation is absent in LLM-authored "
                            "code, so per-rule firing rates can't be evaluated."
                        )
                    ]
                entry_hits = _count_entry_finding_hits(alignment_findings)
                exit_hits = _count_exit_finding_hits(alignment_findings)
            else:
                entry_hits = _count_entry_hits(trades, open_position_entry_reasons or ())
                exit_hits = _count_exit_hits(trades)

            results: List[QualityGateResult] = []
            results.extend(self._entry_rule_results(spec, entry_hits, len(trades)))
            results.extend(self._exit_rule_results(spec, exit_hits, len(trades), SignalExitRule))

            if not results:
                results.append(
                    self._info(
                        f"All {len(spec.entry_rules)} entry rule(s) and "
                        f"applicable exit rules fired at least once across "
                        f"{len(trades)} trades."
                    )
                )

            return results

    def _entry_rule_results(
        self, spec: StrategySpec, entry_hits: Dict[int, int], trade_count: int
    ) -> List[QualityGateResult]:
        """critical per entry rule with zero citations/hits in ``entry_hits``."""
        results: List[QualityGateResult] = []
        for idx, rule in enumerate(spec.entry_rules):
            rule_key = f"entry[{idx}]"
            if entry_hits.get(idx, 0) == 0:
                results.append(
                    self._critical(
                        f"Entry rule {rule_key} (side={rule.side}) never "
                        f"fired across {trade_count} trades — the "
                        "predicate was dead code in this backtest window.",
                        rule_id=rule_key,
                    )
                )
        return results

    def _exit_rule_results(
        self,
        spec: StrategySpec,
        exit_hits: Dict[int, int],
        trade_count: int,
        signal_exit_rule_cls: type,
    ) -> List[QualityGateResult]:
        """warning per ``SignalExitRule`` with zero citations/hits in ``exit_hits``."""
        results: List[QualityGateResult] = []
        for idx, rule in enumerate(spec.exit_rules):
            if not isinstance(rule, signal_exit_rule_cls):
                continue
            rule_key = f"exit[{idx}]"
            if exit_hits.get(idx, 0) == 0:
                results.append(
                    self._warning(
                        f"Signal-exit rule {rule_key} never fired across "
                        f"{trade_count} trades — the predicate may be "
                        "unreachable or superseded by stop-loss / "
                        "take-profit exits.",
                        rule_id=rule_key,
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


def _count_entry_finding_hits(alignment_findings: Sequence[AlignmentFinding]) -> Dict[int, int]:
    """Return ``{rule_index: count}`` from satisfied entry-signal findings.

    Preconditions: ``alignment_findings`` is the deterministic
    ``TradeAlignmentGate`` ledger for this run (one finding per trade per
    check it ran).
    Postconditions: counts findings where ``check_name == "entry_signal"``,
    ``passed is True``, and ``rule_id`` matches ``entry[N]`` — i.e. the
    trade's actual signal bar satisfied entry rule N's predicate when
    re-evaluated against the real market data. Findings for any other
    check, a failed/near-miss entry check, or a non-rule-indexed
    ``rule_id`` (e.g. ``"entry:side_mismatch"``, ``"entry:bars_missing"``)
    are silently skipped.
    """
    hits: Dict[int, int] = {}
    for finding in alignment_findings:
        if finding.check_name != "entry_signal" or not finding.passed or not finding.rule_id:
            continue
        m = _ENTRY_FINDING_RE.match(finding.rule_id)
        if m:
            idx = int(m.group(1))
            hits[idx] = hits.get(idx, 0) + 1
    return hits


def _count_exit_finding_hits(alignment_findings: Sequence[AlignmentFinding]) -> Dict[int, int]:
    """Return ``{rule_index: count}`` from satisfied signal-exit findings.

    Postconditions: counts findings where ``check_name == "signal_exit"``,
    ``passed is True``, and ``rule_id`` matches ``exit:signal_exit[N]`` —
    the trade's actual exit signal bar satisfied ``SignalExitRule`` N's
    predicate. The engine-attributed skip row (``rule_id="exit:signal_exit"``,
    unindexed) and failed findings don't match and are skipped.
    """
    hits: Dict[int, int] = {}
    for finding in alignment_findings:
        if finding.check_name != "signal_exit" or not finding.passed or not finding.rule_id:
            continue
        m = _EXIT_FINDING_RE.match(finding.rule_id)
        if m:
            idx = int(m.group(1))
            hits[idx] = hits.get(idx, 0) + 1
    return hits


__all__ = ["GATE", "RuleFiringRateGate"]
