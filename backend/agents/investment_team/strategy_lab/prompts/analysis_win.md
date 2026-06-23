Draft a rigorous analysis of this WINNING swing-trading strategy — its annualized return is at or above the 8% S&P-500 benchmark (>= 8.0%), so it beat simply holding the index. That benchmark comparison IS the verdict; robustness diagnostics are risk context, never grounds to reclassify it.

## Strategy (definition under test)
Asset class: {asset_class}
Hypothesis: {hypothesis}
Signal: {signal_definition}
Intended entry rules (as authored — may not all be machine-enforced): {entry_rules}
Engine-enforced exit rules (structured DSL applied by the parent engine every bar): {exit_rules}
Sizing / risk: {sizing_rules}
Rationale for testing: {rationale}

{sizing_line_reading}

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
{robustness_caveats_section}## Instructions
This strategy is classified WINNING because its annualized return is at or above the 8% S&P-500 benchmark. State that verdict plainly; do NOT reframe it as a loss, a "questionable" result, or a non-winner on the basis of Sharpe, drawdown, win rate, or robustness. If a "Robustness caveats" section appears above, weave its findings in as honest forward-looking risk caveats a winner must still be watched for (single-asset concentration, regime dependence, weak out-of-sample / deflated Sharpe, overfitting risk) — alongside the verdict, never instead of it.
Entry rules above are prose intent only — describe whether trade behaviour was consistent with the intent rather than asserting that trades "violated" them. Exit rules ARE engine-enforced for `StopLossRule` and `TakeProfitRule`: the parent engine emits closes on the strategy's behalf, so attribute observed exit timing to those rules where evidence supports it (e.g. losing trades clustered near the stop-loss floor point to it firing). `SignalExitRule` entries in the list above are NOT yet engine-enforced — treat any predicate-based exit prose the same way as entry intent.
Think step by step: what in the strategy design plausibly produced strong risk-adjusted returns?
Relate the hypothesis and rules to (1) Sharpe/drawdown/volatility, (2) win rate vs profit factor, (3) patterns in the simulated trades (hold periods, win/loss mix, concentration).
Analyze stop-loss, trailing-stop, and take-profit as a SEPARATE per-trade-outcome dimension — how those within-position safeguards plausibly shaped exits and the reward/risk geometry — distinct from how much capital was deployed per trade. Attribute an exit to a specific rule using the per-trade exit reason in the ledger when it is recorded; where none is given, attribute only where other evidence supports it and otherwise describe the outcome without asserting a cause.
Write 5-8 sentences. Be specific — avoid generic praise. Explain *why* this strategy class succeeded in this backtest.
If an "Alignment status" section above marks the run as misaligned, you MUST open with the disclaimer verbatim and treat the strategy design as untested. Do not claim it worked or failed because of any design choice; describe the execution gaps factually and recommend re-running once aligned.

Return ONLY JSON with no markdown:
{{"draft_narrative": "your draft analysis"}}
