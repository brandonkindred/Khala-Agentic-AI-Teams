"""
Paper Trading Agent — validates strategy code against recent/live market data.

Runs the same Python strategy code through the :class:`SandboxRunner` subprocess
sandbox that backtesting uses, but against *recent* market data instead of
historical.  When results diverge from the backtest, an LLM agent reviews both
sets of trades and produces a factual analysis explaining why.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List

from strands import Agent

from llm_service import get_strands_model

from .market_data_service import OHLCVBar
from .models import (
    BacktestConfig,
    BacktestRecord,
    BacktestResult,
    PaperTradingComparison,
    PaperTradingSession,
    PaperTradingStatus,
    PaperTradingVerdict,
    StrategySpec,
    TradeRecord,
)
from .strategy_lab.executor.sandbox_runner import SandboxRunner
from .strategy_lab.executor.trade_builder import build_trade_records
from .trade_simulator import compute_metrics

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_DIVERGENCE_SYSTEM = (
    "You are a quantitative trading analyst specializing in strategy validation. "
    "You compare paper trading results against backtesting results to identify "
    "why live-data performance diverges from historical simulations."
)

_DIVERGENCE_PROMPT = """\
Analyze why this strategy's paper trading performance diverges from its backtest results.

## Strategy
Asset class: {asset_class}
Hypothesis: {hypothesis}
Signal: {signal_definition}
Entry rules: {entry_rules}
Exit rules: {exit_rules}

## Backtest Results (historical simulation)
Period: {bt_start} to {bt_end}
Annualized return: {bt_annual_return:.1f}%
Win rate: {bt_win_rate:.1f}%
Sharpe ratio: {bt_sharpe:.2f}
Max drawdown: {bt_max_dd:.1f}%
Profit factor: {bt_profit_factor:.2f}
Total trades: {bt_trade_count}

## Paper Trading Results (real market data)
Period: {pt_start} to {pt_end}
Annualized return: {pt_annual_return:.1f}%
Win rate: {pt_win_rate:.1f}%
Sharpe ratio: {pt_sharpe:.2f}
Max drawdown: {pt_max_dd:.1f}%
Profit factor: {pt_profit_factor:.2f}
Total trades: {pt_trade_count}

## Backtest Trade Sample (first 10 trades)
{bt_trades_text}

## Paper Trading Trades (first 20 trades)
{pt_trades_text}

## Instructions
Write a thorough analysis covering:
1. **Metric Divergence**: Which metrics diverged most and by how much
2. **Trade Pattern Analysis**: Compare entry/exit patterns between backtest and paper trading
3. **Root Cause Hypotheses**: Why paper trading underperforms (overfitting, regime change, \
lookahead bias, market microstructure, data quality differences, etc.)
4. **Strategy Weaknesses**: Specific issues found (overfitting indicators, data sensitivity, etc.)
5. **Improvement Suggestions**: Actionable changes for the strategy code that could improve \
future live-data performance

Return ONLY a JSON object with no markdown:
{{"analysis": "your full analysis here", \
"strategy_weaknesses": ["weakness1", "weakness2"], \
"improvement_suggestions": ["suggestion1", "suggestion2"]}}
"""


def _format_trades_table(trades: List[TradeRecord]) -> str:
    """Format trade records as a compact text table."""
    if not trades:
        return "No trades."
    lines = [
        "# | Symbol | Side | Entry Date | Exit Date  | Entry$   | Exit$    | Return%  | Outcome"
    ]
    for t in trades:
        lines.append(
            f"{t.trade_num} | {t.symbol} | {t.side} | {t.entry_date} | {t.exit_date} | "
            f"{t.entry_price:<8.2f} | {t.exit_price:<8.2f} | {t.return_pct:+.2f}% | {t.outcome}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class PaperTradingAgent:
    """Runs paper trading by executing strategy code against recent market data."""

    def __init__(self, llm_client=None) -> None:
        self._divergence_agent = (
            llm_client
            if llm_client is not None
            else Agent(
                model=get_strands_model("paper_trading"),
                system_prompt=_DIVERGENCE_SYSTEM,
            )
        )
        self._sandbox = SandboxRunner()

    def run_session(
        self,
        strategy: StrategySpec,
        strategy_code: str,
        backtest_record: BacktestRecord,
        market_data: Dict[str, List[OHLCVBar]],
        initial_capital: float = 100_000.0,
        transaction_cost_bps: float = 5.0,
        slippage_bps: float = 2.0,
    ) -> PaperTradingSession:
        """Run strategy code against recent market data in the subprocess sandbox."""
        session_id = f"pt-{uuid.uuid4().hex[:8]}"
        now = datetime.now(tz=timezone.utc).isoformat()

        # Determine data period
        all_dates: List[str] = []
        for bars in market_data.values():
            all_dates.extend(b.date for b in bars)
        all_dates.sort()
        data_start = all_dates[0] if all_dates else ""
        data_end = all_dates[-1] if all_dates else ""

        data_source = "yahoo_finance"

        session = PaperTradingSession(
            session_id=session_id,
            lab_record_id="",  # set by caller
            strategy=strategy,
            status=PaperTradingStatus.RUNNING,
            initial_capital=initial_capital,
            current_capital=initial_capital,
            symbols_traded=list(market_data.keys()),
            data_source=data_source,
            data_period_start=data_start,
            data_period_end=data_end,
            started_at=now,
        )

        # Build config for sandbox execution
        config = BacktestConfig(
            start_date=data_start,
            end_date=data_end,
            initial_capital=initial_capital,
            transaction_cost_bps=transaction_cost_bps,
            slippage_bps=slippage_bps,
            benchmark_symbol="SPY",
            rebalance_frequency="daily",
        )

        # Run strategy code in sandbox (same as backtesting)
        exec_result = self._sandbox.run(strategy_code, market_data, config)

        if not exec_result.success:
            logger.warning(
                "Paper trading sandbox execution failed: %s — %s",
                exec_result.error_type,
                exec_result.stderr[:500],
            )
            session.status = PaperTradingStatus.FAILED
            session.divergence_analysis = (
                f"Strategy code execution failed ({exec_result.error_type}): "
                f"{exec_result.stderr[:500]}"
            )
            session.completed_at = datetime.now(tz=timezone.utc).isoformat()
            return session

        # Build trade records (same as backtesting)
        try:
            trades = build_trade_records(exec_result.raw_trades, config)
        except ValueError as exc:
            logger.warning("Paper trading trade builder failed: %s", exc)
            session.status = PaperTradingStatus.FAILED
            session.divergence_analysis = f"Trade record construction failed: {exc}"
            session.completed_at = datetime.now(tz=timezone.utc).isoformat()
            return session

        # Populate session from execution result
        session.trades = trades
        session.current_capital = initial_capital + sum(t.net_pnl for t in trades)
        session.completed_at = datetime.now(tz=timezone.utc).isoformat()

        if trades:
            first_date = trades[0].entry_date
            last_date = trades[-1].exit_date
            session.result = compute_metrics(trades, initial_capital, first_date, last_date)
            session.comparison = self.compare_performance(session.result, backtest_record.result)

            if session.comparison.overall_aligned:
                session.verdict = PaperTradingVerdict.READY_FOR_LIVE
            else:
                session.verdict = PaperTradingVerdict.NOT_PERFORMANT
                try:
                    session.divergence_analysis = self.analyze_divergence(session, backtest_record)
                except Exception as exc:
                    logger.warning("Divergence analysis failed: %s", exc)
                    session.divergence_analysis = (
                        f"Paper trading did not align with backtest expectations. "
                        f"Paper win rate: {session.result.win_rate_pct:.1f}% vs "
                        f"backtest: {backtest_record.result.win_rate_pct:.1f}%. "
                        f"Automated analysis unavailable."
                    )
        else:
            # No trades produced — strategy generated no signals on recent data
            session.verdict = PaperTradingVerdict.NOT_PERFORMANT
            session.divergence_analysis = (
                "Strategy code produced zero trades on the recent market data period "
                f"({data_start} to {data_end}). This may indicate overfitting to "
                "historical patterns, or the strategy's entry conditions are not met "
                "in the current market regime."
            )

        session.status = PaperTradingStatus.COMPLETED
        return session

    @staticmethod
    def compare_performance(
        paper_result: BacktestResult,
        backtest_result: BacktestResult,
    ) -> PaperTradingComparison:
        """Compare paper trading metrics against backtest expectations with tolerances."""
        # Win rate: within +/-10 percentage points
        win_rate_aligned = abs(paper_result.win_rate_pct - backtest_result.win_rate_pct) <= 10.0

        # Annualized return: within +/-40% relative (or +/-3pp absolute for small values)
        bt_ret = backtest_result.annualized_return_pct
        pt_ret = paper_result.annualized_return_pct
        if abs(bt_ret) > 5.0:
            return_aligned = abs(pt_ret - bt_ret) / abs(bt_ret) <= 0.40
        else:
            return_aligned = abs(pt_ret - bt_ret) <= 3.0

        # Sharpe: within +/-0.3
        sharpe_aligned = abs(paper_result.sharpe_ratio - backtest_result.sharpe_ratio) <= 0.3

        # Max drawdown: paper drawdown no more than 1.5x backtest drawdown
        if backtest_result.max_drawdown_pct > 0:
            drawdown_aligned = (
                paper_result.max_drawdown_pct <= backtest_result.max_drawdown_pct * 1.5
            )
        else:
            drawdown_aligned = paper_result.max_drawdown_pct <= 5.0

        # Profit factor: within ±0.5 (or both above 1.0)
        pf_diff = abs(paper_result.profit_factor - backtest_result.profit_factor)
        if backtest_result.profit_factor >= 1.0 and paper_result.profit_factor >= 1.0:
            profit_factor_aligned = pf_diff <= 0.5
        else:
            profit_factor_aligned = pf_diff <= 0.3

        overall = win_rate_aligned and return_aligned and sharpe_aligned and drawdown_aligned

        return PaperTradingComparison(
            backtest_win_rate_pct=backtest_result.win_rate_pct,
            paper_win_rate_pct=paper_result.win_rate_pct,
            backtest_annualized_return_pct=backtest_result.annualized_return_pct,
            paper_annualized_return_pct=paper_result.annualized_return_pct,
            backtest_sharpe_ratio=backtest_result.sharpe_ratio,
            paper_sharpe_ratio=paper_result.sharpe_ratio,
            backtest_max_drawdown_pct=backtest_result.max_drawdown_pct,
            paper_max_drawdown_pct=paper_result.max_drawdown_pct,
            backtest_profit_factor=backtest_result.profit_factor,
            paper_profit_factor=paper_result.profit_factor,
            win_rate_aligned=win_rate_aligned,
            return_aligned=return_aligned,
            sharpe_aligned=sharpe_aligned,
            drawdown_aligned=drawdown_aligned,
            profit_factor_aligned=profit_factor_aligned,
            overall_aligned=overall,
        )

    def analyze_divergence(
        self,
        session: PaperTradingSession,
        backtest_record: BacktestRecord,
    ) -> str:
        """LLM-driven analysis of why paper trading diverges from backtest results."""
        strategy = session.strategy
        bt = backtest_record.result
        pt = session.result

        if pt is None:
            return "No paper trading results available for analysis."

        bt_trades_sample = backtest_record.trades[:10]
        pt_trades_sample = session.trades[:20]

        prompt = _DIVERGENCE_PROMPT.format(
            asset_class=strategy.asset_class,
            hypothesis=strategy.hypothesis,
            signal_definition=strategy.signal_definition,
            entry_rules="; ".join(strategy.entry_rules),
            exit_rules="; ".join(strategy.exit_rules),
            bt_start=backtest_record.config.start_date,
            bt_end=backtest_record.config.end_date,
            bt_annual_return=bt.annualized_return_pct,
            bt_win_rate=bt.win_rate_pct,
            bt_sharpe=bt.sharpe_ratio,
            bt_max_dd=bt.max_drawdown_pct,
            bt_profit_factor=bt.profit_factor,
            bt_trade_count=len(backtest_record.trades),
            pt_start=session.data_period_start,
            pt_end=session.data_period_end,
            pt_annual_return=pt.annualized_return_pct,
            pt_win_rate=pt.win_rate_pct,
            pt_sharpe=pt.sharpe_ratio,
            pt_max_dd=pt.max_drawdown_pct,
            pt_profit_factor=pt.profit_factor,
            pt_trade_count=len(session.trades),
            bt_trades_text=_format_trades_table(bt_trades_sample),
            pt_trades_text=_format_trades_table(pt_trades_sample),
        )

        result = self._divergence_agent(prompt)
        raw = str(result).strip()
        data = json.loads(raw)
        return str(data.get("analysis", "Divergence analysis not available."))
