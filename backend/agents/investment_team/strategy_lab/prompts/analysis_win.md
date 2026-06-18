Draft a rigorous analysis of this WINNING swing-trading strategy (annualized return above 8% threshold).

## Strategy (definition under test)
Asset class: {asset_class}
Hypothesis: {hypothesis}
Signal: {signal_definition}
Intended entry rules (as authored — may not all be machine-enforced): {entry_rules}
Engine-enforced exit rules (structured DSL applied by the parent engine every bar): {exit_rules}
Sizing / risk: {sizing_rules}
Rationale for testing: {rationale}

## How to read the sizing line
"Sizing / risk" above is the capital DEPLOYED into each position, and that deployed size IS the per-trade capital at risk and the per-trade loss cap; a position can lose up to ~100% of what it deploys, and capital committed to one position cannot enter another. It renders by sizing rule: "risk X% per trade" deploys X% of the account; "vol-target X%" sizes the deployment dynamically to a target volatility (so the deployed fraction varies per trade); "$Y per trade" targets a fixed $Y per position, capped by the position limit (so the deployed amount can be lower). Do NOT multiply the stop into the deployment (deployed-fraction × stop is wrong) and do NOT treat the stop as part of sizing — "risk 5% per trade" with a 5% stop is NOT "0.25% per trade", it is a 5% deployment with an optional within-position safeguard. Never derive a stop-multiplied "effective risk" figure or treat one as the capital in play; the deployed amount itself is the capital at risk.

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
Think step by step: what in the strategy design plausibly produced strong risk-adjusted returns?
Relate the hypothesis and rules to (1) Sharpe/drawdown/volatility, (2) win rate vs profit factor, (3) patterns in the simulated trades (hold periods, win/loss mix, concentration).
Analyze stop-loss, trailing-stop, and take-profit as a SEPARATE per-trade-outcome dimension — how those within-position safeguards shaped where trades exited and the reward/risk geometry — distinct from how much capital was deployed per trade.
Write 5-8 sentences. Be specific — avoid generic praise. Explain *why* this strategy class succeeded in this backtest.
If an "Alignment status" section above marks the run as misaligned, you MUST open with the disclaimer verbatim and treat the strategy design as untested. Do not claim it worked or failed because of any design choice; describe the execution gaps factually and recommend re-running once aligned.

Return ONLY JSON with no markdown:
{{"draft_narrative": "your draft analysis"}}
