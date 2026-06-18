Draft a rigorous analysis of this LOSING swing-trading strategy (annualized return below 8% threshold).

## Strategy (definition under test)
Asset class: {asset_class}
Hypothesis: {hypothesis}
Signal: {signal_definition}
Intended entry rules (as authored — may not all be machine-enforced): {entry_rules}
Engine-enforced exit rules (structured DSL applied by the parent engine every bar): {exit_rules}
Sizing / risk: {sizing_rules}
Rationale for testing: {rationale}

## How to read the sizing line
"Sizing / risk" above names the sizing rule; the capital DEPLOYED that the rule produces — not the rendered number — IS the per-trade capital at risk and the per-trade loss cap; a position can lose up to ~100% of what it deploys, and capital committed to one position cannot enter another. Read it by rule: "risk X% per trade" targets X% of the account (nominal deployed fraction, before whole-share lot rounding); "vol-target X%" sets a target annual volatility (X% is NOT a deployed fraction), so the deployed fraction varies per trade and is not shown; "$Y per trade" targets a fixed $Y per position, capped by the position limit (so the deployed amount can be lower). Do NOT multiply the stop into the deployment (deployed-fraction × stop is wrong) and do NOT treat the stop as part of sizing — "risk 5% per trade" with a 5% stop is NOT "0.25% per trade", it is a 5% deployment with an optional within-position safeguard. Never derive a stop-multiplied "effective risk" figure or treat one as the capital in play; the deployed amount itself is the capital at risk. The rendered sizing line is only the nominal rule; realised deployment can differ (dynamic vol-target sizing, the position cap, and whole-share rounding that can round a sub-share order up to one share). The trade ledger reports `position_value` per trade — the realised deployed capital at risk — so use those figures for exact per-trade capital-at-risk rather than the nominal sizing line.

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
Do NOT derive a stop-multiplied "effective risk" figure (deployed-fraction × stop) and blame weak or negative returns on it being low — that conflation is the specific error to avoid. The deployed size itself is the capital at risk; if it is genuinely small, stating that limited deployment constrained returns is a legitimate, accurate explanation — weigh it alongside signal quality, exit geometry, costs, and regime fit.
Analyze stop-loss, trailing-stop, and take-profit as a SEPARATE per-trade-outcome dimension — how those within-position safeguards plausibly shaped exits and the reward/risk geometry — distinct from how much capital was deployed per trade. Attribute an exit to a specific rule using the per-trade exit reason in the ledger when it is recorded; where none is given, attribute only where other evidence supports it and otherwise describe the outcome without asserting a cause.
Use the trade-level evidence where it supports your reasoning.
Write 5-8 sentences. Be specific about *why* this strategy underperformed.
If an "Alignment status" section above marks the run as misaligned, you MUST open with the disclaimer verbatim and treat the strategy design as untested. Do not attribute the weak performance to any design choice; describe the execution gaps factually and recommend re-running once aligned.

Return ONLY JSON with no markdown:
{{"draft_narrative": "your draft analysis"}}
