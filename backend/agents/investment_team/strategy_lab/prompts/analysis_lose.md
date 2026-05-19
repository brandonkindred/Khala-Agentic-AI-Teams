Draft a rigorous analysis of this LOSING swing-trading strategy (annualized return below 8% threshold).

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

{alignment_status_section}
## Instructions
Entry rules above are prose intent only — describe whether trade behaviour was consistent with the intent rather than asserting that trades "violated" them. Exit rules ARE engine-enforced for `StopLossRule` and `TakeProfitRule`: the parent engine emits closes on the strategy's behalf, so attribute observed exit timing to those rules where evidence supports it (e.g. losing trades clustered near the stop-loss floor point to it firing). `SignalExitRule` entries in the list above are NOT yet engine-enforced — treat any predicate-based exit prose the same way as entry intent.
Think step by step: what failure modes explain weak performance — signal timing, risk/reward asymmetry, cost drag, or rules misaligned with the market regime implied by the results?
Use the trade-level evidence where it supports your reasoning.
Write 5-8 sentences. Be specific about *why* this strategy underperformed.
If an "Alignment status" section above marks the run as misaligned, you MUST open with the disclaimer verbatim and treat the strategy design as untested. Do not attribute the weak performance to any design choice; describe the execution gaps factually and recommend re-running once aligned.

Return ONLY JSON with no markdown:
{{"draft_narrative": "your draft analysis"}}
