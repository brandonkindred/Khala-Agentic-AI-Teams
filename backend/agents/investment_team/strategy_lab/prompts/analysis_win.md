Draft a rigorous analysis of this WINNING swing-trading strategy (annualized return above 8% threshold).

## Strategy (definition under test)
Asset class: {asset_class}
Hypothesis: {hypothesis}
Signal: {signal_definition}
Intended entry rules (as authored — may not all be machine-enforced): {entry_rules}
Engine-enforced exit rules (structured DSL applied by the parent engine every bar): {exit_rules}
Sizing / risk: {sizing_rules}
Rationale for testing: {rationale}

## Aggregated backtest metrics
Annualized return: {annualized_return_pct:.1f}%
Total return: {total_return_pct:.1f}%
Sharpe ratio: {sharpe_ratio:.2f}
Max drawdown: {max_drawdown_pct:.1f}%
Win rate: {win_rate_pct:.1f}%
Profit factor: {profit_factor:.2f}
Volatility: {volatility_pct:.1f}%

## Simulated trade ledger (evidence)
{simulated_trades_section}

## Instructions
Entry rules above are prose intent only — describe whether trade behaviour was consistent with the intent rather than asserting that trades "violated" them. Exit rules ARE engine-enforced for `StopLossRule` and `TakeProfitRule`: the parent engine emits closes on the strategy's behalf, so attribute observed exit timing to those rules where evidence supports it (e.g. losing trades clustered near the stop-loss floor point to it firing). `SignalExitRule` entries in the list above are NOT yet engine-enforced — treat any predicate-based exit prose the same way as entry intent.
Think step by step: what in the strategy design plausibly produced strong risk-adjusted returns?
Relate the hypothesis and rules to (1) Sharpe/drawdown/volatility, (2) win rate vs profit factor, (3) patterns in the simulated trades (hold periods, win/loss mix, concentration).
Write 5-8 sentences. Be specific — avoid generic praise. Explain *why* this strategy class succeeded in this backtest.

Return ONLY JSON with no markdown:
{{"draft_narrative": "your draft analysis"}}
