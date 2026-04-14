Draft a rigorous analysis of this LOSING swing-trading strategy (annualized return below 8% threshold).

## Strategy (definition under test)
Asset class: {asset_class}
Hypothesis: {hypothesis}
Signal: {signal_definition}
Entry rules: {entry_rules}
Exit rules: {exit_rules}
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
Think step by step: what failure modes explain weak performance — signal timing, risk/reward asymmetry, cost drag, or rules misaligned with the market regime implied by the results?
Use the trade-level evidence where it supports your reasoning.
Write 5-8 sentences. Be specific about *why* this strategy underperformed.

Return ONLY JSON with no markdown:
{{"draft_narrative": "your draft analysis"}}
