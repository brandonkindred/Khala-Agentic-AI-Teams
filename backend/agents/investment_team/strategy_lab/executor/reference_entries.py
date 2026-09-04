"""Entry-side replay for the reference-ledger simulator.

Step 1 of the reference-ledger simulator described in
``system_design/reference_ledger_trade_model.md`` — replays
``spec.entry_rules`` over a fixed sequence of bars using the shared, pure
``evaluate_entry_rules`` (``predicate_evaluator.py``) and opens a reference
position filled at the next bar's open, matching the live engine's current
entry-fill timing (``_EngineEntryDispatcher.maybe_emit`` submits a MARKET
order on the bar the predicate fires; the fill simulator settles it at the
following bar's open).

This module deliberately implements ONLY entry-fill timing/price mechanics.
Everything else the design doc's §5 "Entries" subsection also covers is out
of scope here and left for a later step: quantity/sizing resolution
(``spec.sizing``), risk-limit admission gates (``spec.risk_limits`` —
``max_open_positions``, ``max_gross_leverage``,
``max_symbol_concentration_pct``, the ``max_position_pct`` clamp),
capital/equity tracking, the cross-symbol merged-``(timestamp, symbol)``
processing order that equity coupling across symbols requires, and the
engine-injected short safety stop. Each symbol in ``bars`` is therefore
walked independently, in ``bars`` iteration order — there is no shared
equity state yet to force a merged timeline.

``ReferenceEntryFill`` (below) is intentionally narrower than the design
doc's ``ReferenceTrade``: a full ``ReferenceTrade`` cannot be constructed
without exit data (``exit_bar``/``exit_price``/``exit_rule_kind``) and a
resolved ``qty``, neither of which this module produces. Its entry-side
fields match ``ReferenceTrade``'s 1:1 in name/type/semantics so a later
exit-side step can extend/wrap one into a full ``ReferenceTrade`` with no
renaming.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Literal, Mapping, Sequence

import pandas as pd

from ...models import StrategySpec
from .predicate_evaluator import PandasHistoryView, evaluate_entry_rules

if TYPE_CHECKING:
    # Deferred: importing any ``trading_service`` submodule at runtime forces
    # ``trading_service/__init__.py`` to run, which imports ``service.py``,
    # which top-level-imports ``engine/{execution_model,fill_simulator,
    # order_book,portfolio}.py`` — all four explicitly forbidden here. Only
    # the type hint below needs the name; ``from __future__ import
    # annotations`` (above) makes every annotation in this file a
    # lazily-evaluated string, so no runtime import happens. Mirrors the
    # identical ``BatchIndicatorCache`` deferral in
    # ``strategy_lab/indicators/streaming.py``.
    from ...trading_service.strategy.contract import Bar


@dataclass(frozen=True)
class ReferenceEntryFill:
    """One reference position opened by entry-side replay.

    Deliberately narrower than the design doc's ``ReferenceTrade``
    (``system_design/reference_ledger_trade_model.md``) — a full
    ``ReferenceTrade`` requires exit data and a resolved ``qty`` this module
    never computes. Field names/types/semantics match ``ReferenceTrade``'s
    entry-side fields exactly, so a later exit-side step can extend one into
    a full ``ReferenceTrade`` without renaming anything.
    """

    symbol: str
    side: Literal["long", "short"]
    entry_bar: int
    entry_date: str
    entry_rule_index: int
    entry_price: float

    def __post_init__(self) -> None:
        """Enforce this record's structural contract at construction.

        Preconditions: none beyond typing.
        Postconditions: raises ``ValueError`` if ``entry_bar < 0``,
        ``entry_rule_index < 0``, ``entry_price`` is not a positive finite
        number, or ``side`` is not ``"long"``/``"short"``; otherwise the
        instance is structurally valid. ``symbol`` and ``entry_date`` are
        recorded as given, not validated — this module has no basis on which
        to reject a symbol string or a date string beyond typing.
        """
        if self.entry_bar < 0:
            raise ValueError(f"entry_bar must be >= 0, got {self.entry_bar!r}")
        if self.entry_rule_index < 0:
            raise ValueError(f"entry_rule_index must be >= 0, got {self.entry_rule_index!r}")
        if not (self.entry_price > 0 and math.isfinite(self.entry_price)):
            raise ValueError(
                f"entry_price must be a positive finite number, got {self.entry_price!r}"
            )
        if self.side not in ("long", "short"):
            raise ValueError(f"side must be 'long' or 'short', got {self.side!r}")


def bars_to_frame(symbol_bars: "Sequence[Bar]") -> pd.DataFrame:
    """Build the OHLCV frame ``PandasHistoryView`` evaluates predicates against.

    Public rather than module-private because the exit side builds its own
    ``PandasHistoryView`` over the same bars to evaluate ``SignalExitRule``
    predicates (:mod:`reference_exits`), and both sides must index identical
    frames: a predicate that fires for an entry on bar ``i`` and one that
    fires for a signal exit on bar ``i`` have to be reading the same row. One
    construction site is the only way to keep that true under later edits.

    Preconditions: none (``symbol_bars`` may be empty).
    Postconditions: returns a DataFrame with one row per bar, row order
    identical to ``symbol_bars`` order — the same bar index
    ``evaluate_entry_rules``/``PandasHistoryView`` index by.
    """
    return pd.DataFrame(
        {
            "open": [b.open for b in symbol_bars],
            "high": [b.high for b in symbol_bars],
            "low": [b.low for b in symbol_bars],
            "close": [b.close for b in symbol_bars],
            "volume": [b.volume for b in symbol_bars],
        }
    )


def replay_entry_rules(
    spec: StrategySpec,
    bars: "Mapping[str, Sequence[Bar]]",
) -> List[ReferenceEntryFill]:
    """Replay ``spec.entry_rules`` over ``bars``, filling each fired entry at
    the next bar's open.

    Preconditions:
        - ``spec`` is a validated ``StrategySpec``.
        - ``bars`` maps symbol to a ``Bar`` sequence, in chronological order,
          for every symbol the caller wants evaluated (a symbol with an
          empty sequence is skipped, not an error — this function does not
          enforce the full ``simulate()`` precondition set the design doc
          specifies for the eventual entries+exits+sizing+risk function,
          since this is a narrower slice of that function).

    Postconditions:
        - Returns one ``ReferenceEntryFill`` per symbol for which
          ``evaluate_entry_rules`` fires and a next bar exists with a
          positive, finite ``open`` — at most one per symbol, since this
          module models no exit and therefore never re-opens a symbol once
          filled.
        - A trigger on the final bar of a symbol's sequence (no next bar to
          fill against) is dropped: no ``ReferenceEntryFill`` is produced for
          it, and no later bar exists to retry it. This is the documented
          final-bar rule.
        - A trigger whose fill-bar ``open`` is <= 0 or non-finite is dropped
          (no record), and scanning continues on subsequent bars for that
          symbol — ``Bar`` does not itself validate OHLC positivity/finiteness.
        - When ``spec.target_symbols`` is non-empty, a symbol outside it is
          never evaluated at all.

    Invariants:
        - No side effects: does not mutate ``spec`` or ``bars``.
        - Deterministic: identical ``(spec, bars)`` always produces an
          identical output list.
        - Imports no module transitively reaching
          ``trading_service/service.py`` or
          ``trading_service/engine/{fill_simulator,order_book,
          execution_model,portfolio}.py`` (verified against the current
          source; see this module's docstring).
    """
    out: List[ReferenceEntryFill] = []
    for symbol, symbol_bars in bars.items():
        if spec.target_symbols and symbol not in spec.target_symbols:
            continue
        n = len(symbol_bars)
        if n == 0:
            continue
        view = PandasHistoryView(bars_to_frame(symbol_bars), {})
        for i in range(n):
            match = evaluate_entry_rules(spec.entry_rules, view, i)
            if match is None:
                continue
            rule, rule_idx = match
            entry_bar = i + 1
            if entry_bar >= n:
                break  # final-bar rule: no next bar to fill against
            fill_bar = symbol_bars[entry_bar]
            open_px = fill_bar.open
            if not (open_px > 0 and math.isfinite(open_px)):
                continue  # fill-bar open guard: drop, keep scanning
            out.append(
                ReferenceEntryFill(
                    symbol=symbol,
                    side=rule.side,
                    entry_bar=entry_bar,
                    # ``Bar.timestamp`` is ISO-8601 (contract.py's own docstring),
                    # so its first 10 characters are always the date, matching
                    # production's identical ``pos.entry_timestamp[:10]`` truncation.
                    entry_date=fill_bar.timestamp[:10],
                    entry_rule_index=rule_idx,
                    entry_price=open_px,
                )
            )
            break  # suppression: never re-open a symbol once filled
    return out
