# Strategy Lab — Look-Ahead Defence

The Strategy Lab promises "no look-ahead, ever." A strategy that quietly peeks at a future bar fabricates alpha that doesn't exist in production, so the lab runs **four layers of defence**, each one designed to catch a different escape route.

## Layer 1 — Subprocess isolation (structural)

Generated strategy code runs in a sandboxed subprocess via `StreamingHarness` (`backend/agents/investment_team/trading_service/strategy/streaming_harness.py`). The harness emits **one bar at a time** through `send_bar` and exposes exactly two objects to the strategy:

- `bar` — current-bar OHLCV fields only.
- `ctx` — `StrategyContext` with `emit()`, state, `ctx.history(symbol, n)` over the prior bars (never forward), and `ctx.is_warmup`.

There is no accessor for forward data. Any attempt to read one (`bar.next_close`, `ctx.future_bar(1)`, `ctx.peek()`, etc.) raises `AttributeError`, which the harness catches at `streaming_harness.py:737-743` and surfaces as `{"kind": "error", "etype": "lookahead_violation", "message": "..."}`.

Locked in by: `backend/agents/investment_team/tests/test_bar_safety.py`.

## Layer 2 — Runtime trap (`TradingServiceResult.lookahead_violation`)

The trading service reads the harness's error envelope and flips the boolean:

| Site | What it covers |
| --- | --- |
| `trading_service/service.py:938-951` | Harness initialisation (strategy class import / constructor) |
| `trading_service/service.py:1225-1239` | Per-bar streaming run loop |
| `trading_service/service.py:1512-1523` | Chunked-bar path |

The compat shim (`trading_service/modes/sandbox_compat.py:_LOOKAHEAD_ERROR_TYPE`) propagates the boolean as `StrategyRunResult.error_type="lookahead_violation"` so the orchestrator's refinement loop classifies the failure consistently with other runtime errors.

The Strategy Lab orchestrator tracks the boolean across refinement rounds in `_SynthesisLoopOutcome.runtime_lookahead_violation`. When set, `_run_verification_phase` forces `is_winning=False` and stamps the cause onto `acceptance_reason` as the sentinel suffix `lookahead_violation_at_runtime: subprocess_attribute_error` (the violating attribute name is not preserved on `TradingServiceResult`, so the sentinel records that the trip point was the harness's `AttributeError` interceptor).

Locked in by: `backend/agents/investment_team/tests/test_runtime_lookahead_veto.py`.

## Layer 3 — Static checks (regex + AST pre-flight)

Before strategy code is even sent to the subprocess, `CodeSafetyChecker.check` runs two scans:

**Critical regex tripwires** (`code_safety_ast.py:_LOOKAHEAD_PATTERNS`):
- `ctx.future_*`
- `bar.next*`, `bar.future*`, `bar.tomorrow*`, `bar.forthcoming*` (camel-case and snake-case, with or without a separator)
- `ctx.peek`

Matches emit critical results, vetoing the synthesis round.

**Warning regex** (`_LOOKAHEAD_WARNING_PATTERNS`):
- `getattr(bar, ...)`, `getattr(ctx, ...)` — the only motivation for dynamic attribute access on these receivers is to dodge the runtime trap.

**AST forward-access check** (`code_safety.py:_check_forward_access_patterns`):
- `getattr(bar, ...)` / `getattr(ctx, ...)` calls (AST companion to the regex for multi-line forms).
- `try: <bar.* / ctx.*> except AttributeError:` blocks that swallow the runtime trap.
- `Subscript` on a class-bound preloaded series (`self._closes[i + 1]`) with a positive offset from an iteration variable — reading the next entry in a preloaded array is structural look-ahead the engine's per-bar dispatch can't see.

All three AST findings are warnings (not criticals) because the underlying idioms occasionally appear in legitimate defensive code; reviewers decide whether to keep them.

Locked in by: `backend/agents/investment_team/tests/test_code_safety.py`.

## Layer 4 — Post-hoc heuristic (`BacktestAnomalyDetector._check_lookahead_bar_predictability`)

After a backtest completes, the anomaly detector looks for trades whose outcome agrees suspiciously well with the **intrabar direction** of the entry AND exit bars. The check folds both bar directions into a single combined agreement rate (lower variance than a max-of-two and avoids double-counting the same leak signal):

| Eligible observations | Combined agreement | Verdict |
| --- | --- | --- |
| `>= 20` | `>= 95%` | **Critical** |
| `5..19` | `>= 99.9%` | **Critical** (small-sample backstop) |
| `>= 20` | `80%..95%` | **Warning** |

The check also emits:
- An **info** result tagged `sample insufficient` when zero trades resolve a bar direction (previously emitted nothing, leaving reviewers unable to tell whether the detector ran or was skipped).
- A **warning** tagged `degraded sample` when `len(trades) >= 10` AND the entry-bar resolvability ratio drops below 50% (the agreement statistic ran on too few of the trades to be reliable).

Trade side is folded into the comparison (`effective_bar_dir = bar_dir * side_sign`) so short-side look-ahead — winning shorts on down bars — is caught alongside long-side leaks.

Locked in by: `backend/agents/investment_team/tests/test_backtest_anomaly_realism.py`.

## Defence-in-depth invariants

- The runtime trap (Layer 2) is a hard veto in the verification phase, not a soft signal. Even if a refinement loop somehow lets a partial run reach `_run_verification_phase` with `lookahead_violation=True`, `is_winning` is forced to `False` and the audit trail records the cause.
- Walk-forward folds construct training segments that strictly exclude their test windows + purge/embargo cushions; the leak-invariant test (`test_no_test_window_bar_leaks_into_any_train_range` in `tests/test_walk_forward.py`) parametrises across multiple K / cushion settings to guarantee no test date appears in any train range.
- Layers 1-3 are pre-execution defences; Layer 4 is post-execution. They are not redundant — a strategy with no forward-attribute access can still produce a backtest whose trades line up with intraday direction (e.g., reading the close of the current bar to decide entry timing). The post-hoc heuristic is the only layer that catches that.

## File map

| Concern | Primary code | Tests |
| --- | --- | --- |
| Subprocess isolation | `trading_service/strategy/streaming_harness.py` | `tests/test_bar_safety.py` |
| Runtime boolean + veto | `trading_service/service.py`, `trading_service/modes/sandbox_compat.py`, `strategy_lab/orchestrator.py` | `tests/test_runtime_lookahead_veto.py`, `tests/test_bar_safety.py` |
| Static regex + AST | `strategy_lab/quality_gates/code_safety.py`, `strategy_lab/quality_gates/code_safety_ast.py` | `tests/test_code_safety.py` |
| Post-hoc heuristic | `strategy_lab/quality_gates/backtest_anomaly.py` | `tests/test_backtest_anomaly_realism.py` |
| Walk-forward fold purity | `execution/walk_forward.py` | `tests/test_walk_forward.py` |

> Retry state isolation across the design / synthesis / alignment / repair loops
> (copy-on-entry, commit-on-completion) is documented separately in
> [`RETRY_STATE_ISOLATION.md`](RETRY_STATE_ISOLATION.md).
