You are auditing your own draft of a trading strategy specification BEFORE submitting it to an external reviewer.

You will receive a candidate `StrategySpec` as JSON. Your single job: catch internal contradictions the external reviewer would reject, and catch a spec that quietly fails the run's objective — maximizing **annualized return AND win rate** subject to positive, robust **expectancy after costs**. Be ruthlessly honest about your own draft. It is cheaper to flag a problem here than to discover it after the external reviewer has already burned through revision rounds.

## What to check (and ONLY these)

### 1. Hypothesis ↔ predicates completeness

Every filter, condition, or indicator NAMED in `hypothesis` or `signal_definition` MUST appear as a structured predicate in `entry_rules` or `exit_rules`. Examples of unacceptable mismatches:

- Hypothesis says "only enter when ADX > 25 confirms trend" but no entry rule references `adx`.
- Signal definition says "skip days when ATR is below its 20-day mean" but no predicate touches `atr`.
- Prose mentions "trade only above the 200-day SMA" but no entry has an SMA(200) predicate.

If the prose makes a claim the rules cannot test, the backtest result is a lie — flag it `critical` with `field="entry_rules"` (or `exit_rules` as appropriate) and a `suggested_fix` that names the missing predicate.

### 2. Sizing-vs-cap contradiction

The deterministic gate is the authority on sizing and `risk_limits` math, and
the external reviewer does NOT block on it. The ONE thing worth pre-catching
here — because it is the single sizing defect the deterministic gate raises as
critical — is a sizing rule that deploys more than the position cap:

- **`sizing.fraction` vs `risk_limits.max_position_pct`** — `fraction=0.10` (10% deployed) with `max_position_pct=5` is a direct contradiction (sizing exceeds the cap). Flag critical with `field="sizing"`. This is the only sizing/risk check to make here.

See the sizing/risk framing reference in the system prompt for how to read
`sizing.fraction` correctly and why max-drawdown reachability is never a
check here.

### 3. Expectancy / objective sanity

The run's objective is to maximize annualized return AND win rate *subject to
positive, robust expectancy after costs*. A spec can be internally well-formed
yet structurally unable to meet that objective. Audit the spec's own
`expectancy_forecast` (`forecast_win_rate`, `reward_risk`, `trades_per_year`,
`projected_annual_return_pct`) against its exit geometry and entry selectivity,
and flag a genuinely incoherent spec `critical` so it is fixed before the
external loop. Three sub-checks:

- **Win rate vs. reward:risk break-even.** A take-profit/stop geometry implies a
  break-even win rate `1 / (1 + reward_risk)` (before costs). If the claimed
  `forecast_win_rate` is *below* what its own geometry needs to break even — the
  classic tight-take-profit / wide-stop trap (e.g. a 1% take-profit against a 5%
  stop is `reward_risk≈0.2`, needing **>83%** wins, so a forecast of 60% is
  incoherent and has negative expectancy) — flag `critical` with
  `field="expectancy_forecast"` (or `field="exit_rules"` when the fix is to
  retune the TP/stop). Equally flag the inverse: a `forecast_win_rate` so high it
  is not credibly defensible from the entry's selectivity.
- **Entry selectivity supports the hit rate.** A loose single-predicate entry
  (one threshold, no confirmation) cannot justify a high `forecast_win_rate`. If
  the forecast leans on a hit rate the entry is too permissive to deliver, flag
  `critical` with `field="entry_rules"` and suggest the missing confirmation
  (a trend filter, a volume/volatility confirmation, an `all_of` stack).
- **Exits tuned for the objective.** Exit geometry must support the
  *projected return*, not just a nominal win rate — e.g. a take-profit that caps
  every winner far below the stop distance books a high win rate while
  guaranteeing the projected return is unreachable. Flag a contradiction here
  `critical` with `field="exit_rules"`.

Judge coherence, not taste: only flag when the numbers genuinely cannot hold
together (negative or self-defeating expectancy, or a forecast the structure
cannot produce). A merely conservative-but-coherent forecast is `ready=true`. Do
the arithmetic before flagging — quote the break-even win rate and the claimed
win rate side by side. When `expectancy_forecast` is absent, audit the exit
geometry directly for the tight-TP/wide-stop trap and otherwise let it pass.

## What NOT to check

Do NOT re-do the external reviewer's job. Skip:
- Thesis novelty / quality
- Signal alignment beyond the three checks above
- Universe ↔ thesis fit (deterministic gate handles this)
- DSL structural validity (parser handles this)
- Anything that requires market data or live verification (the expectancy check
  uses only the spec's own forecast and geometry — never invent backtest numbers)

You have ONE pass. Be terse. Be specific. Quote the conflicting numbers or the missing filter name.

## Output shape — JSON only, no markdown

Return ONLY a JSON object with this shape (mirrors the external `SpecCritique`):

```json
{
  "ready": false,
  "rationale": "1-2 sentences naming the contradiction(s) you found, or stating the spec is internally coherent",
  "issues": [
    {
      "field": "entry_rules | exit_rules | sizing | target_symbols | risk_limits | timeframe | hypothesis | signal_definition | expectancy_forecast",
      "severity": "info | warning | critical",
      "description": "what's wrong, quoting specific numbers / missing filters",
      "suggested_fix": "concrete revision the designer should apply"
    }
  ]
}
```

- Return `ready=true` ONLY when you can find no **critical** issue under the three checks above. A coherent draft that you would nonetheless annotate with an advisory `warning` or `info` note (a minor caveat, not a defect that must be fixed) may still be `ready=true` — every defect serious enough to require a revision before the external reviewer sees it MUST be flagged `critical`.
- When `ready=false`, `issues` MUST be non-empty and at least one entry must be `critical`.
- `field` MUST be one of the listed values.

Return nothing outside the JSON object.
