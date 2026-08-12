# Sizing & risk framing — reference (read before evaluating sizing / risk_limits)

These are the two canonical rules governing how deployed capital, stops, and
drawdown relate to risk in this system.

- **The deployed position size IS the per-trade loss cap.** A line like
  "risk 5% per trade" (the system's rendering of a `fixed_fraction` sizing
  rule) names the capital **DEPLOYED** into the position — a fraction of the
  account — not a stop-multiplied loss budget. An entered position can lose
  up to ~100% of the capital deployed, so the capital committed IS the most
  a single trade can lose; there is no separate per-trade-loss field.
  `stop_loss.pct` is a **separate, optional** safeguard — a price move off
  entry, measured against the trade — that tries to limit a position's
  realised loss *below* a full wipeout. Do **NOT** compute per-trade risk as
  `fraction × stop`, and never treat the stop as part of sizing: "risk 5%
  per trade" with a 5% stop is **not** "0.25% per trade" — it is a 5%
  deployment with an optional within-position safeguard. (For a `$100`
  account with `max_position_pct = 5`, you deploy up to `$5`; an optional
  20% stop on that `$5` caps the position's loss at ~`$1`, independent of
  the sizing decision.) Shorts without a declared stop are auto-protected
  at runtime with a 100%-adverse-move stop, so a short's worst case is also
  bounded by the deployed size.

- **There is NO max-drawdown constraint.** Max drawdown is not a limit in
  this system. A strategy is an experiment (backtest / paper trading, no
  real capital) and may lose up to 100% of the account by design; realised
  drawdown is reported as a metric, never enforced. Do not add a
  `max_drawdown_pct` to `risk_limits`, do not size positions to "stay
  under" a drawdown number, do not let a drawdown figure shape the thesis,
  and do not flag drawdown reachability as a defect — it is never a
  blocker.
