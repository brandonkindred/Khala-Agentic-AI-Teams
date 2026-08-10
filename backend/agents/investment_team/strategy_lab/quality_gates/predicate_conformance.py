"""Pre-execution predicate conformance shadow check.

For every ``EntryRule`` and ``SignalExitRule`` whose predicate the LLM
translated into ``on_bar`` logic, the gate:

1. Generates ~50-80 synthetic bars that exercise both true and false
   predicate states (via :mod:`predicate_conformance_fixtures`).
2. Runs the strategy's ``on_bar`` in-process via a lightweight
   ``_ShadowContext`` that records every ``submit_order`` call with its
   bar index and side.
3. Runs the engine's ``evaluate_predicate()`` on the same bars.
4. Compares per-bar: any disagreement (strategy orders on a
   predicate-false bar, or misses a predicate-true bar when no
   position blocks the entry) is a conformance failure.

The gate only runs for ``requires_custom_code=True`` strategies — the
compiled path (engine-managed) emits zero ``submit_order`` calls and
has no inline predicate logic to drift.

When ``spec.exit_rules`` cover every entered side, ``SignalExitRule``
predicates are engine-owned (enforced via ``_EngineExitDispatcher``) and
conforming custom code authors no manual close — those fixtures are
skipped so the shadow check does not demand a close the conformance
contract forbids. Entry predicates are always checked (entries remain
inline for the custom-code path).

Routing on failure: critical results join the synthesis loop's
``critical_failures`` collection and route through
``_refine_or_exhaust(failure_phase="validation", ...)``.  The
``rule_id`` and per-bar diff are surfaced in ``failure_details`` so the
refinement agent can target the exact branch.

Retry demotion: after ``attempt >= _code_conformance_retries()``,
criticals are demoted to warnings so the pipeline can proceed to
backtest rather than looping indefinitely on an edge case the LLM
cannot resolve.
"""

from __future__ import annotations

import builtins
import logging
import math
import re
import statistics
from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar, Dict, List, Optional, Type

from ..budget_config import StrategyLabBudgetConfig
from ..runtime_window import STREAMING_WINDOW_BARS
from ..spec_dsl import EntryRule as _EntryRule
from ..spec_dsl import Predicate as _Predicate
from ..spec_dsl import SignalExitRule as _SignalExitRule
from ..spec_dsl import format_predicate_tree
from .code_safety import _engine_exits_cover_sides
from .models import GateResultsMixin, QualityGateResult, StrategyLabPhase
from .predicate_conformance_fixtures import ConformanceFixture, generate_conformance_fixtures

logger = logging.getLogger(__name__)

GATE: str = "predicate_conformance"


def _normalize_side(side: Any) -> str:
    """Extract the lowercase side string, handling ``str(Enum)`` values."""
    if isinstance(side, Enum):
        return str(side.value).lower()
    return str(side).lower()


def _code_conformance_retries() -> int:
    """Resolved per call so tests can override via env or monkeypatch.

    Preconditions:
      Env value, when set, parses to ``int``.
    Postconditions:
      Returns a non-negative integer. Default 2; garbage values fall
      back to 2.
    """
    return StrategyLabBudgetConfig.from_env().code_conformance_retries


@dataclass
class _OrderRecord:
    """One ``submit_order`` call captured by the shadow context."""

    bar_index: int
    symbol: str
    side: str
    qty: float
    reason: str = ""


class _ShadowContext:
    """Lightweight mock of ``StrategyContext`` for in-process shadow execution.

    Records ``submit_order`` calls per bar without any fill simulation.
    Position state is tracked optimistically so the strategy's
    ``if position is None:`` guards evaluate correctly.

    Invariants:
      - ``_history[sym]`` never exceeds 500 bars (matching the real context).
      - ``orders`` is append-only during the shadow run.
    """

    def __init__(self, *, initial_capital: float = 100_000.0) -> None:
        self._history: Dict[str, list] = {}
        self._current_symbol: Optional[str] = None
        self._positions: Dict[str, _SimplePosition] = {}
        self._capital: float = initial_capital
        self._equity: float = initial_capital
        self._now: str = ""
        self._is_warmup: bool = False
        self._current_bar_index: int = -1
        self.orders: List[_OrderRecord] = []
        # indicator() shares one IndicatorRegistry per (symbol, source) across
        # this instance's calls for performance (see
        # strategy_indicators._shared_registry). Owned here — not a
        # module/thread-level cache — so this execution's indicator state is
        # never visible to any other _ShadowContext. This matters because this
        # class runs in-process on worker threads (the Temporal worker's
        # activity thread pool, executing ``run_design_attempt_activity`` for
        # a wave's cycles concurrently, and similar) that can process
        # many shadow executions over their lifetime, including — if two
        # contexts for the same symbol are ever constructed before either
        # runs — executions whose bar ingestion interleaves rather than one
        # running to completion before the next starts; a thread-local cache
        # can't tell those apart, a fresh dict per instance doesn't need to.
        self._indicator_registries: dict = {}
        # This dict only covers indicator() calls. _build_indicators_stub
        # below binds the 16 standalone wrapper functions (sma/ema/...)
        # straight from strategy_indicators onto the shadow indicators
        # module with no registries argument, so generated code that does
        # `from indicators import sma` (a documented, supported call shape —
        # see strategy_indicators' module docstring) never sees this dict
        # directly either. _check_fixture instead brackets every call into
        # strategy code with strategy_indicators._active_registries.set(self.
        # _indicator_registries) / .reset(token), so a standalone wrapper
        # called from inside on_start/on_bar resolves to this dict too — see
        # _check_fixture and _shared_registry's docstring for the mechanism.

    @property
    def capital(self) -> float:
        return self._capital

    @property
    def equity(self) -> float:
        return self._equity

    @property
    def now(self) -> str:
        return self._now

    @property
    def is_warmup(self) -> bool:
        return self._is_warmup

    def position(self, symbol: str) -> Optional[_SimplePosition]:
        return self._positions.get(symbol)

    def history(self, symbol: str, n: int) -> list:
        bars = self._history.get(symbol, [])
        if n <= 0:
            return []
        return bars[-n:]

    def indicator(
        self,
        name: str,
        *,
        symbol: Optional[str] = None,
        source: str = "close",
        **params,
    ) -> Optional[float]:
        """Shadow mirror of ``StrategyContext.indicator``.

        Routes through the same shared ``indicator_value`` accessor the real
        context uses, so a strategy that reads indicators via ``ctx.indicator``
        evaluates identically under shadow execution.

        Preconditions:
            Same as ``StrategyContext.indicator``.
        Postconditions:
            Returns the latest indicator value as ``float``, or ``None`` during
            warm-up / when no bars for ``symbol`` have arrived yet.
        """
        sym = symbol if symbol is not None else self._current_symbol
        if sym is None:
            # Mirror the real StrategyContext.indicator: calling it before any
            # bar is dispatched (e.g. from on_start) with no explicit symbol is
            # a contract error, not a None — so shadow execution takes the same
            # branch the live runtime would.
            raise ValueError("indicator() needs a symbol when no bar has been dispatched yet")
        bars = self._history.get(sym, [])
        if not bars:
            return None
        from ..executor.strategy_indicators import indicator_value

        return indicator_value(
            name, bars, source=source, registries=self._indicator_registries, **params
        )

    def submit_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        order_type: Any = None,
        limit_price: Any = None,
        stop_price: Any = None,
        trail_offset: Any = None,
        trail_offset_kind: str = "abs",
        tif: Any = None,
        reason: str = "",
        unfilled_policy: Any = None,
        twap_slices: Any = None,
        attached_stop_loss: Any = None,
        attached_take_profit: Any = None,
        parent_order_id: Any = None,
        oco_group_id: Any = None,
    ) -> str:
        side_lower = _normalize_side(side)
        self.orders.append(
            _OrderRecord(
                bar_index=self._current_bar_index,
                symbol=symbol,
                side=side_lower,
                qty=qty,
                reason=reason,
            )
        )
        if side_lower in ("buy", "long"):
            if symbol in self._positions and self._positions[symbol].side == "short":
                del self._positions[symbol]
            else:
                self._positions[symbol] = _SimplePosition(
                    symbol=symbol,
                    side=_SideStr("long"),
                    entry_price=self._last_close(symbol),
                )
        elif side_lower in ("sell", "short"):
            if symbol in self._positions and self._positions[symbol].side == "long":
                del self._positions[symbol]
            else:
                self._positions[symbol] = _SimplePosition(
                    symbol=symbol,
                    side=_SideStr("short"),
                    entry_price=self._last_close(symbol),
                )
        return f"shadow_{self._current_bar_index}"

    def cancel(self, order_id: str) -> None:
        pass

    def _ingest_bar(self, bar: Any, index: int) -> None:
        self._current_bar_index = index
        self._history.setdefault(bar.symbol, []).append(bar)
        hist = self._history[bar.symbol]
        if len(hist) > STREAMING_WINDOW_BARS:
            del hist[:-STREAMING_WINDOW_BARS]
        self._current_symbol = bar.symbol
        self._now = bar.timestamp

    def _last_close(self, symbol: str) -> float:
        hist = self._history.get(symbol, [])
        if hist:
            return hist[-1].close
        return 0.0


class _SideStr(str):
    """String subclass with a ``.value`` attribute matching ``OrderSide`` enum behavior."""

    @property
    def value(self) -> str:
        return str(self)


@dataclass
class _SimplePosition:
    """Minimal stand-in for ``_PositionSnapshot``."""

    symbol: str
    side: _SideStr = _SideStr("long")
    entry_price: float = 0.0
    qty: float = 1.0


class PredicateConformanceGate(GateResultsMixin):
    """Shadow check that compares per-bar ``submit_order`` decisions against
    the engine's ``evaluate_predicate()`` verdicts on synthetic fixtures.

    Contract:
      Pre: ``code`` is non-empty Python source that passed
      ``CodeSafetyChecker``; ``spec`` has ``entry_rules`` / ``exit_rules``.
      Post: returned list has one ``QualityGateResult`` per fixture.
      ``rule_id`` is set on every result.
    """

    GATE: ClassVar[str] = GATE

    def check(
        self,
        code: str,
        spec: Any,
        *,
        phase: StrategyLabPhase = "synthesis",
        attempt: int = 0,
    ) -> List[QualityGateResult]:
        """Run the predicate conformance shadow check.

        Preconditions:
          ``code`` is Python source; ``spec`` is a ``StrategySpec``.
          ``attempt`` is the zero-based retry counter from the orchestrator.
        Postconditions:
          One ``QualityGateResult`` per checked fixture. ``SignalExitRule``
          fixtures are skipped when ``spec.exit_rules`` cover every entered
          side (the engine owns the exit, so conforming code submits no
          manual close); entry fixtures are always checked. After ``attempt
          >= _code_conformance_retries()``, criticals are demoted to
          warnings.
        """
        with self._using_phase(phase):
            if not code or not code.strip():
                return [self._critical("Predicate conformance gate received empty strategy_code.")]

            if not getattr(spec, "requires_custom_code", False):
                return [
                    self._info("Skipped: engine-managed strategy has no inline predicate logic.")
                ]

            entry_rules = getattr(spec, "entry_rules", []) or []
            exit_rules = getattr(spec, "exit_rules", []) or []
            has_predicates = any(isinstance(r, _EntryRule) for r in entry_rules) or any(
                isinstance(r, _SignalExitRule) for r in exit_rules
            )
            if not has_predicates:
                return [self._info("No predicate-bearing rules to check.")]

            strategy_cls = _exec_strategy(code)
            if strategy_cls is None:
                return [
                    self._critical(
                        "Could not extract Strategy subclass from code.",
                    )
                ]

            fixtures = generate_conformance_fixtures(spec, compiled_code=code)
            # Signal exits are engine-owned: when spec.exit_rules cover every
            # entered side, the engine enforces the SignalExitRule via
            # _EngineExitDispatcher and conforming custom code submits no manual
            # close. The shadow context only sees strategy-submitted orders, so
            # keeping those fixtures would demand a close the conformance
            # contract now forbids. Drop them; entry fixtures still run
            # (entries remain inline for custom code).
            entered_sides = {
                r.side for r in entry_rules if isinstance(r, _EntryRule) and r.side is not None
            }
            if entered_sides and _engine_exits_cover_sides(spec, entered_sides):
                fixtures = [f for f in fixtures if f.rule_kind != "signal_exit"]
            if not fixtures:
                return [self._info("No conformance fixtures generated.")]

            max_retries = _code_conformance_retries()
            demote = attempt >= max_retries
            results: List[QualityGateResult] = []

            for fixture in fixtures:
                result = self._check_fixture(strategy_cls, fixture, spec=spec, demote=demote)
                results.append(result)

            return results

    def _check_fixture(
        self,
        strategy_cls: Type,
        fixture: ConformanceFixture,
        *,
        spec: Any = None,
        demote: bool = False,
    ) -> QualityGateResult:
        """Shadow-run one fixture and classify the result.

        Preconditions:
          ``fixture`` describes a single predicate-bearing rule; ``spec`` is
          the owning ``StrategySpec`` (or ``None`` in narrow unit tests).
        Postconditions:
          Returns exactly one ``QualityGateResult`` with ``rule_id`` set.
          When the strategy's per-bar orders diverge from the engine
          verdicts, the failure detail names the rendered predicate and a
          bounded per-bar ``lhs``/``rhs``/verdict trace for the offending
          bars (see :func:`_build_conformance_detail`). The critical→warning
          demotion is governed solely by ``demote`` and is unchanged by the
          enriched detail.
        """
        if not fixture.synthesizable:
            return self._warning(
                f"Fixture unsynthesizable: {fixture.unsynthesizable_reason}",
                rule_id=fixture.rule_id,
            )

        ctx = _ShadowContext()
        try:
            strategy = strategy_cls()
        except Exception as exc:
            return self._critical(
                f"Strategy instantiation failed: {exc}",
                rule_id=fixture.rule_id,
            )

        # Bracket every call into strategy code with this fixture's own
        # registries dict as the ambient _active_registries context (see
        # strategy_indicators._shared_registry): a strategy that reads
        # indicators via the standalone wrappers (`from indicators import
        # bollinger_bands`) instead of ctx.indicator() would otherwise share
        # a registry with whatever other execution last ran on this worker
        # thread. Resetting via the token (not a blind clear) means this is
        # correct even if a future caller nests or interleaves _check_fixture
        # calls, not just for today's strictly-sequential fixture loop.
        from ..executor.strategy_indicators import _active_registries

        token = _active_registries.set(ctx._indicator_registries)
        try:
            if callable(getattr(strategy, "on_start", None)):
                try:
                    strategy.on_start(ctx)
                except Exception:
                    pass

            if fixture.rule_kind == "signal_exit":
                pos_side = _SideStr("short") if _infer_short_from_spec(spec) else _SideStr("long")
                ctx._positions[fixture.symbol] = _SimplePosition(
                    symbol=fixture.symbol,
                    side=pos_side,
                    entry_price=100.0,
                )

            for i, bar in enumerate(fixture.bars):
                shadow_bar = _to_shadow_bar(bar, fixture.symbol)
                ctx._ingest_bar(shadow_bar, i)
                try:
                    strategy.on_bar(ctx, shadow_bar)
                except Exception:
                    pass
        finally:
            _active_registries.reset(token)

        orders_at: Dict[int, List[_OrderRecord]] = {}
        for o in ctx.orders:
            orders_at.setdefault(o.bar_index, []).append(o)

        is_short_entry = fixture.side == "short"
        if fixture.rule_kind == "signal_exit" and fixture.side is None:
            is_short_entry = _infer_short_from_spec(spec)
        entry_sides = ("sell", "short") if is_short_entry else ("buy", "long")
        exit_sides = ("buy", "long") if is_short_entry else ("sell", "short", "flat")

        other_rule_verdicts = _compute_other_rule_verdicts(spec, fixture) if spec else None

        false_positives: List[int] = []
        false_negatives: List[int] = []
        position_open = fixture.rule_kind == "signal_exit"

        for i, verdict in enumerate(fixture.expected_verdicts):
            bar_orders = [o for o in orders_at.get(i, []) if o.symbol == fixture.symbol]
            has_entry = any(o.side in entry_sides for o in bar_orders)
            has_exit = any(o.side in exit_sides for o in bar_orders)

            if verdict is not None:
                if fixture.rule_kind == "entry":
                    if verdict and not position_open and not has_entry:
                        false_negatives.append(i)
                    elif not verdict and has_entry and not position_open:
                        other_fires = (
                            other_rule_verdicts is not None and other_rule_verdicts.get(i) is True
                        )
                        if not other_fires:
                            false_positives.append(i)
                else:
                    if verdict and position_open and not has_exit:
                        false_negatives.append(i)
                    elif not verdict and has_exit and position_open:
                        false_positives.append(i)

            if verdict is not None:
                if has_entry and not position_open:
                    position_open = True
                if has_exit and position_open:
                    position_open = False

        if not false_positives and not false_negatives:
            return self._info(
                f"Predicate conformance OK ({len(fixture.bars)} bars checked).",
                rule_id=fixture.rule_id,
            )

        detail = _build_conformance_detail(fixture, spec, false_positives, false_negatives)

        if demote:
            return self._warning(detail, rule_id=fixture.rule_id)
        return self._critical(detail, rule_id=fixture.rule_id)


# ---------------------------------------------------------------------------
# Failure-detail rendering (targeted-repair trace)
# ---------------------------------------------------------------------------

_RULE_ID_ENTRY = re.compile(r"^entry\[(\d+)\]$")
_RULE_ID_SIGNAL_EXIT = re.compile(r"^exit\[(\d+)\]:signal_exit$")

# Cap on the number of enriched per-bar trace rows appended to a failure
# detail. The bare index lists (truncated to 10) already convey breadth; the
# enriched rows exist to give the refinement agent concrete values for a few
# representative bars without making the detail string unbounded.
_MAX_TRACE_ROWS = 5


def _predicate_for_rule_id(spec: Any, fixture: ConformanceFixture) -> Optional[_Predicate]:
    """Recover the ``Predicate`` behind ``fixture.rule_id`` from ``spec``.

    Preconditions:
      ``fixture.rule_id`` follows the documented format emitted by
      :mod:`predicate_conformance_fixtures` — ``entry[{idx}]`` for entry
      rules or ``exit[{idx}]:signal_exit`` for signal-exit rules, where
      ``idx`` indexes ``spec.entry_rules`` / ``spec.exit_rules`` respectively.
    Postconditions:
      Returns the matching rule's ``when`` predicate, or ``None`` when
      ``spec`` is missing, the id does not match the format, the index is out
      of range, or the targeted rule is not the expected variant. Never
      raises on a malformed id.
    """
    if spec is None:
        return None
    rule_id = fixture.rule_id
    m = _RULE_ID_ENTRY.match(rule_id)
    if m is not None:
        rules = getattr(spec, "entry_rules", []) or []
        idx = int(m.group(1))
        if 0 <= idx < len(rules) and isinstance(rules[idx], _EntryRule):
            return rules[idx].when
        return None
    m = _RULE_ID_SIGNAL_EXIT.match(rule_id)
    if m is not None:
        rules = getattr(spec, "exit_rules", []) or []
        idx = int(m.group(1))
        if 0 <= idx < len(rules) and isinstance(rules[idx], _SignalExitRule):
            return rules[idx].when
        return None
    return None


def _format_scalar(v: Optional[float]) -> str:
    """Render an evaluator scalar for a stable, bounded detail string.

    Postconditions:
      ``None`` (warmup / unresolved) renders as ``"None"``; finite floats use
      a fixed ``%.4g`` format so the detail string is deterministic.
    """
    if v is None:
        return "None"
    return f"{v:.4g}"


def _enriched_trace_lines(
    pred: _Predicate,
    fixture: ConformanceFixture,
    false_positives: List[int],
    false_negatives: List[int],
) -> List[str]:
    """Render up to :data:`_MAX_TRACE_ROWS` per-bar diagnostic lines.

    Preconditions:
      ``pred`` is the predicate behind ``fixture.rule_id``;
      ``false_positives`` / ``false_negatives`` are valid indices into
      ``fixture.bars``.
    Postconditions:
      Returns at most ``_MAX_TRACE_ROWS`` lines covering the lowest-indexed
      offending bars, each naming the resolved ``lhs``/``rhs``, the engine
      verdict, and whether the bar is a false positive or false negative.
      Returns ``[]`` when there are no offending bars.
    """
    from ..executor.predicate_evaluator import PandasHistoryView, evaluate_predicate
    from .conformance_bars import _bars_to_df

    fp = set(false_positives)
    fn = set(false_negatives)
    ordered = sorted(fp | fn)[:_MAX_TRACE_ROWS]
    if not ordered:
        return []

    view = PandasHistoryView(_bars_to_df(fixture.bars), {})
    lines: List[str] = []
    for i in ordered:
        if i in fp:
            kind = "false positive (ordered on predicate-false bar)"
        else:
            kind = "false negative (no order on predicate-true bar)"
        res = evaluate_predicate(pred, view, i)
        lines.append(
            f"  bar {i}: lhs={_format_scalar(res.lhs)} {pred.op} "
            f"rhs={_format_scalar(res.rhs)} -> engine={res.status}; {kind}"
        )
    return lines


def _build_conformance_detail(
    fixture: ConformanceFixture,
    spec: Any,
    false_positives: List[int],
    false_negatives: List[int],
) -> str:
    """Build the failure-detail string for a non-conforming fixture.

    Preconditions:
      At least one of ``false_positives`` / ``false_negatives`` is non-empty.
    Postconditions:
      Returns a multi-line string that always carries the ``rule_id`` header
      and the (≤10) offending-bar index lists. When the predicate is
      recoverable from ``spec`` (see :func:`_predicate_for_rule_id`), it also
      carries the rendered predicate, a bounded per-bar ``lhs``/``rhs``/verdict
      trace, and a one-line directive for the refinement agent. Falls back to
      the index-only form when the predicate cannot be recovered.
    """
    parts = [f"rule_id={fixture.rule_id}: predicate conformance failed."]
    pred = _predicate_for_rule_id(spec, fixture)  # leaf or all_of/any_of tree
    if pred is not None:
        parts.append(f"  Predicate: {format_predicate_tree(pred)}")
    if false_positives:
        parts.append(
            f"  False positives (order on predicate-false bar): bars {false_positives[:10]}"
        )
    if false_negatives:
        parts.append(
            f"  False negatives (no order on predicate-true bar): bars {false_negatives[:10]}"
        )
    if pred is not None:
        parts.extend(_enriched_trace_lines(pred, fixture, false_positives, false_negatives))
        parts.append(
            f"  Fix the on_bar branch implementing '{fixture.rule_id}' so it submits on the "
            "predicate-true bars above and not on predicate-false bars."
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Multi-rule attribution helpers
# ---------------------------------------------------------------------------


def _infer_short_from_spec(spec: Any) -> bool:
    """True when the spec's entry rules are all short-side."""
    entry_rules = getattr(spec, "entry_rules", []) or []
    shorts = [r for r in entry_rules if isinstance(r, _EntryRule) and r.side == "short"]
    return len(shorts) > 0 and len(shorts) == len(
        [r for r in entry_rules if isinstance(r, _EntryRule)]
    )


def _compute_other_rule_verdicts(
    spec: Any, fixture: ConformanceFixture
) -> Optional[Dict[int, bool]]:
    """For multi-entry specs, check whether ANY other entry rule fires per bar.

    Returns a dict mapping bar index → True when another rule's predicate
    is satisfied. Used to suppress false-positive reports when the strategy
    correctly fires for a different entry rule on that bar.
    """
    if fixture.rule_kind != "entry":
        return None
    entry_rules = [r for r in (getattr(spec, "entry_rules", []) or []) if isinstance(r, _EntryRule)]
    if len(entry_rules) <= 1:
        return None

    from ..executor.predicate_evaluator import PandasHistoryView, evaluate_tree
    from .conformance_bars import _bars_to_df

    df = _bars_to_df(fixture.bars)
    cache: dict = {}
    view = PandasHistoryView(df, cache)
    other_fires: Dict[int, bool] = {}
    for i in range(len(fixture.bars)):
        for idx, rule in enumerate(entry_rules):
            rid = f"entry[{idx}]"
            if rid == fixture.rule_id:
                continue
            if fixture.side is not None and rule.side != fixture.side:
                continue
            # ``evaluate_tree`` handles a leaf predicate or an ``all_of`` /
            # ``any_of`` tree uniformly, so a multi-confirmation sibling rule is
            # evaluated correctly here too.
            result = evaluate_tree(rule.when, view, i)
            if result.status == "satisfied":
                other_fires[i] = True
                break
    return other_fires


# ---------------------------------------------------------------------------
# Strategy class extraction
# ---------------------------------------------------------------------------


def _exec_strategy(code: str) -> Optional[Type]:
    """``exec()`` the strategy code and extract the Strategy subclass.

    Preconditions:
      ``code`` has passed ``CodeSafetyChecker``.
    Postconditions:
      Returns the Strategy subclass, or ``None`` on failure.
    """
    _BLOCKED = frozenset({"exec", "eval", "compile", "__import__", "open", "input", "breakpoint"})
    safe_builtins = {k: v for k, v in vars(builtins).items() if k not in _BLOCKED}

    contract_module = _build_contract_stub()
    indicators_module = _build_indicators_stub()
    stub_strategy_cls = contract_module.Strategy
    safe_builtins = dict(safe_builtins)

    _ALLOWED_STDLIB = frozenset(
        {
            "math",
            "datetime",
            "collections",
            "itertools",
            "functools",
            "typing",
            "dataclasses",
            "enum",
            "abc",
            "re",
            "copy",
            "statistics",
            "operator",
            "decimal",
            "fractions",
            "json",
        }
    )
    _real_import = builtins.__import__

    def _restricted_import(name, *args, **kwargs):
        if name == "contract":
            return contract_module
        if name == "indicators":
            return indicators_module
        top = name.split(".")[0]
        if top in _ALLOWED_STDLIB:
            return _real_import(name, *args, **kwargs)
        raise ImportError(f"import of {name!r} is not allowed in the shadow harness")

    safe_builtins["__import__"] = _restricted_import

    namespace: Dict[str, Any] = {
        "__builtins__": safe_builtins,
        "math": math,
        "statistics": statistics,
    }

    try:
        exec(code, namespace)  # noqa: S102
    except Exception:
        return None

    for obj in namespace.values():
        if (
            isinstance(obj, type)
            and obj is not stub_strategy_cls
            and issubclass(obj, stub_strategy_cls)
            and _has_on_bar(obj)
        ):
            return obj

    for obj in namespace.values():
        if isinstance(obj, type) and obj is not stub_strategy_cls and _has_on_bar(obj):
            return obj

    return None


def _build_contract_stub():
    """Build a fake ``contract`` module with the types LLM-generated code imports.

    The code synthesis prompt instructs the LLM to write
    ``from contract import OrderSide, OrderType, Strategy, TimeInForce``.
    This stub provides those names so ``exec()`` succeeds without importing
    the real trading-service contract (which would pull in Pydantic,
    subprocess protocol, etc.).
    """
    import enum
    import types

    mod = types.ModuleType("contract")

    class _OrderSide(str, enum.Enum):
        LONG = "long"
        SHORT = "short"

    class _OrderType(str, enum.Enum):
        # Must mirror ``trading_service.strategy.contract.OrderType`` — a custom
        # strategy that references an order type missing here raises
        # ``AttributeError`` under the shadow run and is mis-reported as not
        # submitting on predicate-true bars (the gate swallows the exception).
        MARKET = "market"
        LIMIT = "limit"
        STOP = "stop"
        STOP_LIMIT = "stop_limit"
        TRAILING_STOP = "trailing_stop"

    class _TimeInForce(str, enum.Enum):
        DAY = "day"
        GTC = "gtc"
        IOC = "ioc"
        FOK = "fok"

    class _UnfilledPolicy(str, enum.Enum):
        DROP = "drop"
        REQUEUE_NEXT_BAR = "requeue_next_bar"
        TWAP_N = "twap_n"

    class _StopAttachment:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    class _LimitAttachment:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    class _Strategy:
        def on_bar(self, ctx, bar):
            pass

    mod.OrderSide = _OrderSide
    mod.OrderType = _OrderType
    mod.TimeInForce = _TimeInForce
    mod.UnfilledPolicy = _UnfilledPolicy
    mod.StopAttachment = _StopAttachment
    mod.LimitAttachment = _LimitAttachment
    mod.Strategy = _Strategy
    return mod


def _build_indicators_stub():
    """Build a shadow ``indicators`` module with real computations.

    Strategy code calls ``from indicators import sma, ema, ...`` and expects
    scalar returns (the latest value), used directly in branch conditions. The
    wrappers live in ``executor.strategy_indicators`` — the same scalar API the
    streaming sandbox exposes — so the conformance shadow and the live sandbox
    evaluate identical indicator values. Input coercion (``list[Bar]``,
    ``deque``, …) is handled by the underlying ``executor.indicators`` helpers,
    so no pre-wrapping is needed here.

    Postconditions:
        Returns a module object whose ``sma``/``ema``/``rsi``/``macd``/
        ``bollinger_bands``/``atr``/``adx``/``stochastic``/``vwap`` attributes
        are the scalar-returning helpers from ``executor.strategy_indicators``.
    """
    import types

    from ..executor import strategy_indicators as _scalar

    mod = types.ModuleType("indicators")
    for _name in (
        "sma",
        "ema",
        "rsi",
        "macd",
        "bollinger_bands",
        "atr",
        "adx",
        "stochastic",
        "vwap",
        "donchian_channels",
        "keltner_channels",
        "obv",
        "mfi",
        "roc",
        "cci",
        "williams_r",
    ):
        setattr(mod, _name, getattr(_scalar, _name))
    return mod


def _has_on_bar(cls: type) -> bool:
    return callable(getattr(cls, "on_bar", None))


def _to_shadow_bar(ohlcv: Any, symbol: str) -> _ShadowBar:
    """Convert an ``OHLCVBar`` to a bar object the strategy's ``on_bar`` can consume."""
    return _ShadowBar(
        symbol=symbol,
        timestamp=ohlcv.date,
        timeframe="1d",
        open=ohlcv.open,
        high=ohlcv.high,
        low=ohlcv.low,
        close=ohlcv.close,
        volume=ohlcv.volume,
    )


class _ShadowBar:
    """Lightweight bar matching the ``Bar`` protocol the strategy reads."""

    __slots__ = ("symbol", "timestamp", "timeframe", "open", "high", "low", "close", "volume")

    def __init__(
        self,
        *,
        symbol: str,
        timestamp: str,
        timeframe: str,
        open: float,
        high: float,
        low: float,
        close: float,
        volume: float,
    ) -> None:
        self.symbol = symbol
        self.timestamp = timestamp
        self.timeframe = timeframe
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


__all__ = ["GATE", "PredicateConformanceGate"]
