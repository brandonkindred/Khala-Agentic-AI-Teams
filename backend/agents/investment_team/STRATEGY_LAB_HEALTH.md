# Strategy Lab Health Audit

Post-hoc acceptance-criteria audit for recent Strategy Lab runs. Evaluates
10 objective checks against persisted `StrategyLabRecord` data and reports
a pass/fail/skip verdict per check per record.

## Quick Start

```bash
cd backend
PYTHONPATH=agents python3 -m investment_team.scripts.audit_recent_runs \
    --since=30d --sample=10
```

Requires `JOB_SERVICE_URL` to point at a running job service instance.

### Arguments

| Flag | Default | Description |
|---|---|---|
| `--since` | all records | Duration (`30d`) or ISO date (`2024-06-01`) cutoff |
| `--sample` | all | Audit only the N most recent records |
| `--min-pass-rate` | `0.8` | Minimum pass rate for exit-code 0 |

Exit code is 0 when pass rate meets the threshold, 1 otherwise.

## The 10 Acceptance Criteria

### 1. Spec Stability

The strategy spec must not mutate after the design phase exits, except to
tighten risk-limit fields (`max_drawdown_pct`, `max_position_pct`, etc.).

- **PASS**: No post-design `spec_history` entries, or all post-design diffs
  touch only `risk_limits` keys.
- **FAIL**: A post-design revision modifies non-risk-limits fields.
- **SKIP**: `spec_history` missing (legacy record).

### 2. Rule Implementation

Every entry/exit rule in the final spec must have a `rule_implementation_map`
entry with non-empty `code_line_refs`, confirming AST-verified code coverage.

- **PASS**: All rules have code references.
- **FAIL**: One or more rules have empty `code_line_refs`.
- **SKIP**: `requires_custom_code=True` or `rule_implementation_map` missing.

### 3. Universe Fidelity

Every trade must execute in a symbol listed in `spec.target_symbols`.

- **PASS**: All trade symbols are in the target set (case-insensitive).
- **FAIL**: Off-spec trade symbols detected.
- **SKIP**: `target_symbols` empty (universe-agnostic run) or no trades.

### 4. Exit-Rule Alignment

The exit-rule conformance gate must not report critical failures. Falls back
to `alignment_findings` when gate results are unavailable.

- **PASS**: No critical exit-rule violations.
- **FAIL**: Critical stop-loss or take-profit alignment failure.
- **SKIP**: No exit_rule_conformance gate results or alignment findings.

### 5. Cost Robustness

Annualized return must be non-negative at the 2x cost-stress multiplier.

- **PASS**: 2x cost-stress row has `annualized_return_pct >= 0`.
- **FAIL**: Negative annualized return at 2x cost.
- **SKIP**: No `cost_stress_results` or no 2x multiplier row.

### 6. Regime Coverage

Every market regime with observations must have non-negative cumulative
return, and the deflated Sharpe ratio must be non-negative.

- **PASS**: All regimes non-negative and `deflated_sharpe >= 0`.
- **FAIL**: Regime with negative cumret or negative deflated Sharpe.
- **SKIP**: No `regime_results`.

### 7. Narrative Fidelity

The `analysis_narrative` must only reference indicator names actually used
in the spec's entry/exit rule predicates.

- **PASS**: No phantom indicators in the narrative.
- **FAIL**: Narrative mentions indicators not in the spec.
- **SKIP**: Empty narrative.

### 8. Trade Adequacy

Trade count must meet the expected floor based on the backtest window and
average hold period (or per-timeframe default).

- **PASS**: `n_trades >= expected_count`.
- **FAIL**: Insufficient trades for the window.
- **SKIP**: Missing dates, trades, or hold data.

### 9. Liquidity Realism

The liquidity-realism gate must not report critical failures (position size
exceeding 1% of ADV).

- **PASS**: No critical liquidity gate failures.
- **FAIL**: Critical oversized-position violation.
- **SKIP**: No `liquidity_realism` gate results (ADV data unavailable).

### 10. No Dead-Code Rules

Every rule in `rule_implementation_map` must have `traded_count > 0`,
confirming the rule was actually exercised during backtesting.

- **PASS**: All rules have non-zero trade counts.
- **FAIL**: Dead-code rules detected (never traded).
- **SKIP**: `requires_custom_code=True` or `rule_implementation_map` missing.

## Pass-Rate Target

The initial threshold is **80%** (`--min-pass-rate=0.8`). As coverage and
data quality improve, ratchet to **95%**.

## Adding New Checks

1. Write a function `check_<name>(record: dict) -> CheckResult` in
   `audit_recent_runs.py`. Return PASS, FAIL, or SKIP with details.
2. Append it to the `ALL_CHECKS` list.
3. Add a short name to `_SHORT_NAMES` for tabular output.
4. Add PASS, FAIL, and SKIP test cases in `test_audit_recent_runs.py`.
