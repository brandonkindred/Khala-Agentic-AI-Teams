"""Map a :class:`StrategyRunResult` to a :class:`QualityGateResult` per probe.

The gate calls :func:`assess_probe` once per :class:`ProbeRun`. Outcomes:

- Unprobeable run → ``severity="warning"`` with the unprobeable reason in
  ``details``. Surfaces the limitation in refinement prompts without
  blocking the synthesis loop.
- Sandbox-level failure (``result.success=False``) → ``severity="critical"``;
  refinement will see the engine error and re-author the code.
- Probeable but no matching trade → ``severity="critical"`` with a compact
  trade summary so the refinement agent can target the right code branch.
- Match found → ``severity="info"``, ``passed=True``.

The asserter does not own the :class:`GateResultsMixin` helpers — it
returns plain :class:`QualityGateResult` values that the gate layer
constructs through the mixin so the gate name / phase are stamped
consistently.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from ..models import GateResultsMixin, QualityGateResult
from .synthesizer import ProbeRun

# Max number of trade entries we render into ``details`` on a failure.
_MAX_TRADES_IN_DETAILS = 5
# Max stderr characters we propagate from a failed sandbox run.
_MAX_STDERR_CHARS = 400


def assess_probe(
    probe: ProbeRun,
    result: object,
    *,
    emitter: GateResultsMixin,
) -> QualityGateResult:
    """Translate one probe's outcome into a :class:`QualityGateResult`.

    Pre:
      - ``probe`` carries ``rule_id`` and ``expected`` (or is unprobeable).
      - ``emitter`` is inside a ``with self._using_phase(...)`` block.
      - ``result`` exposes ``success: bool``, ``trades: list[TradeRecord]``,
        ``error_type: Optional[str]``, ``stderr: str``. The asserter accepts
        any duck-typed object so tests can pass a lightweight stand-in.
    """
    if not probe.synthesizable:
        return emitter._warning(
            _format_unprobeable(probe),
            rule_id=probe.rule_id,
        )

    success = bool(getattr(result, "success", False))
    if not success:
        return emitter._critical(
            _format_sandbox_failure(probe, result),
            rule_id=probe.rule_id,
        )

    trades = list(getattr(result, "trades", []) or [])
    trigger_date = _trigger_date(probe)

    if probe.expected is None:
        return emitter._critical(
            f"rule_id={probe.rule_id}: probe has no expected outcome configured.",
            rule_id=probe.rule_id,
        )

    if probe.expected.kind == "entry":
        ok, why = _check_entry(probe, trades, trigger_date)
    else:
        ok, why = _check_exit(probe, trades, trigger_date)

    if ok:
        return emitter._info(
            f"rule_id={probe.rule_id}: probe passed ({why}).",
            rule_id=probe.rule_id,
        )
    return emitter._critical(
        f"rule_id={probe.rule_id}: {why}. trades={_summarise_trades(trades)}",
        rule_id=probe.rule_id,
    )


def _trigger_date(probe: ProbeRun) -> Optional[str]:
    if not probe.market_data:
        return None
    idx = probe.trigger_bar_index
    if idx < 0 or idx >= len(probe.market_data):
        return None
    return probe.market_data[idx].date


def _check_entry(
    probe: ProbeRun, trades: List[object], trigger_date: Optional[str]
) -> Tuple[bool, str]:
    expected_side = probe.expected.side if probe.expected else None
    if not trades:
        return False, (
            f"expected at least one {expected_side} trade to open on/after "
            f"trigger bar {trigger_date}, but no trades were recorded"
        )
    early_trades_seen = 0
    for trade in trades:
        side = getattr(trade, "side", None)
        entry_date = getattr(trade, "entry_date", None)
        if expected_side is not None and side != expected_side:
            continue
        if trigger_date is not None and entry_date is not None and str(entry_date) < trigger_date:
            # The opening trade happened *before* the synthesised trigger bar.
            # The recipe verified the predicate becomes True at trigger_date,
            # so a trade opened earlier is unrelated activity (e.g. an
            # always-on entry on bar 0) and is not evidence the rule under
            # test actually fires. Skip and keep scanning for a later
            # correctly-sided trade.
            early_trades_seen += 1
            continue
        return True, f"trade opened {side} on {entry_date}"
    if early_trades_seen:
        return False, (
            f"found {early_trades_seen} {expected_side} trade(s) but all opened "
            f"before trigger bar {trigger_date}; rule predicate likely not the "
            "actual entry signal"
        )
    return False, f"no trade with side={expected_side} found"


def _check_exit(
    probe: ProbeRun, trades: List[object], trigger_date: Optional[str]
) -> Tuple[bool, str]:
    expected_substr = probe.expected.exit_reason_contains if probe.expected else None
    if expected_substr is None:
        return False, "exit probe missing exit_reason_contains expectation"
    if not trades:
        return False, (
            f"expected one trade closed with exit_reason containing "
            f"'{expected_substr}' but no trades were recorded"
        )
    for trade in trades:
        exit_reason = getattr(trade, "exit_reason", None) or ""
        if expected_substr in str(exit_reason):
            return True, f"trade closed with exit_reason='{exit_reason}'"
    return False, (
        f"no trade closed with exit_reason containing '{expected_substr}'"
    )


def _format_unprobeable(probe: ProbeRun) -> str:
    return (
        f"rule_id={probe.rule_id}: unprobeable ({probe.unprobeable_reason or 'unknown'}). "
        "The synthesiser could not engineer a bar sequence to fire this rule's predicate; "
        "the probe was skipped."
    )


def _format_sandbox_failure(probe: ProbeRun, result: object) -> str:
    error_type = getattr(result, "error_type", None) or "unknown"
    stderr = (getattr(result, "stderr", "") or "")[:_MAX_STDERR_CHARS]
    return (
        f"rule_id={probe.rule_id}: sandbox failed (error_type={error_type}). stderr={stderr!r}"
    )


def _summarise_trades(trades: List[object]) -> str:
    rendered: List[str] = []
    for trade in trades[:_MAX_TRADES_IN_DETAILS]:
        side = getattr(trade, "side", "?")
        entry_date = getattr(trade, "entry_date", "?")
        exit_date = getattr(trade, "exit_date", "?")
        exit_reason = getattr(trade, "exit_reason", None)
        rendered.append(f"(side={side}, entry={entry_date}, exit={exit_date}, reason={exit_reason!r})")
    extra = "" if len(trades) <= _MAX_TRADES_IN_DETAILS else f" ...+{len(trades) - _MAX_TRADES_IN_DETAILS} more"
    return "[" + ", ".join(rendered) + "]" + extra
