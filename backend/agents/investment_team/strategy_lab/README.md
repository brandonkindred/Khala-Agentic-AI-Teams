# Strategy Lab

The Strategy Lab is the Investment Team subsystem that designs, audits, synthesizes, and
backtests quantitative trading strategies from a natural-language spec. A strategy moves
through a **design ↔ review loop** (`DesignAgent → SpecReadinessGate → DesignReviewAgent`,
with an optional designer self-review pass and a deterministic mechanical-repair
pre-flight), a **synthesis loop** that compiles the structured DSL or emits custom code and
runs conformance/alignment gates, and finally a **backtest**. Every LLM call routes through
a shared **fault-tolerance envelope** (`strategy_lab/agents/_llm_envelope.py`) with
per-call timeout, retries, transient/rate-limit backoff, and a total wall-time budget.
Readiness and alignment **gates** keep position-sizing math coherent and ensure generated
code actually trades the spec's target symbols.

The subsystem is tuned entirely through `STRATEGY_LAB_*` environment variables; the full
reference below is the canonical home for those knobs (CLAUDE.md and `docs/ENV_VARS.md`
point here). All numeric vars parse defensively: garbage → documented default,
out-of-range → clamped to the documented floor/ceiling unless noted.

## Environment Variables

### STRATEGY_LAB_MARKET_DATA_*
Strategy Lab market-data cache/timeout/provider tuning.

## TradingView MCP data source

The Strategy Lab can pull OHLCV price data from a **TradingView MCP server** in preference to the
free public providers. Configure it once from the Integrations UI (**Integrations → TradingView**,
backed by `GET/PUT/DELETE /api/integrations/tradingview` — server URL + optional bearer token stored
Fernet-encrypted), or point a container at a server directly with the `TRADINGVIEW_MCP_*`
environment variables (see `docs/ENV_VARS.md`; env overrides the stored config).

When enabled with a URL, `MarketDataService` prepends a `tradingview_mcp` provider to the front of
its per-asset-class chain, so it is tried **before** Yahoo / Twelve Data / CoinGecko / Alpha Vantage
for every symbol; any TradingView error transparently falls back to the next provider. The client
(`investment_team/tradingview_mcp/`) issues a single MCP `tools/call` (default tool `get_ohlcv`) over
streamable-HTTP JSON-RPC and tolerates both `structuredContent` and JSON text-content result shapes.
Unconfigured, the chain is unchanged.

### STRATEGY_LAB_MAX_UNIVERSE_SYMBOLS
Hard ceiling on the asset-class default universe used when `spec.target_symbols` is empty (default
`20`). When the cap actually truncates the default a `logger.warning` fires. Non-empty
`spec.target_symbols` is returned verbatim by `resolve_strategy_symbols` (override semantics) so the
fetched universe matches what `TargetSymbolCoverageGate.check_trades` allows the strategy to trade.

### STRATEGY_LAB_ALIGNMENT_RETRIES
Number of envelope retries for the alignment fix-proposer (`TradeAlignmentAgent.propose_code_fix`)
before it raises `AlignmentAuditError` and `_run_alignment_audit` falls closed with `aligned=False`
(default `2` → 3 attempts total). The retry/backoff now lives inside the shared LLM envelope (see
`STRATEGY_LAB_LLM_*`), so `_run_alignment_audit` makes a single call and adds jittered backoff
between attempts; an exhausted proposer or any unexpected agent exception fails closed (never a green
audit).

### STRATEGY_LAB_ALIGNMENT_ADJUDICATION_CONCURRENCY
Max number of near-miss LLM adjudications the `DeterministicAlignmentChecker` runs concurrently per
`check()` (default `4`; sub-1 floors to `1` = fully serial).
Near-miss candidates are collected during the trade loop and dispatched through a bounded
`ThreadPoolExecutor` (the adjudicator is synchronous) instead of blocking the loop one trade at a
time; verdicts are slotted back in trade order so the output is identical to the serial path
regardless of completion timing. Trades cloud concurrency for wall time without changing the gate's
result.

### STRATEGY_LAB_LLM_TIMEOUT
Per-call wall-clock timeout (seconds) for every Strategy Lab LLM call routed through the shared
fault-tolerance envelope (`strategy_lab/agents/_llm_envelope.py`), enforced via a daemon-thread guard.
On the **Bedrock** path it is also forwarded as the transport-level read timeout in
`get_strands_model`. On the **Ollama** path (routed through `llm_service`) the transport read timeout
is owned by the `llm_service` client (`resolve_timeout` / `LLM_TIMEOUT`); this var still bounds the
call via the envelope's wall-clock guard on top. Falls back to `LLM_TIMEOUT` / the platform default
(900).

### STRATEGY_LAB_LLM_MAX_RETRIES
Retries (attempts = retries + 1) the envelope makes on a *retriable* (transient transport / 5xx /
connection / timeout / throttle) failure before raising `StrategyLabLLMError`. Fatal failures (4xx /
auth / malformed, or a weekly rate cap) are never retried. Falls back to `LLM_MAX_RETRIES`, else `2`.

> **Layered retries on the Ollama path.** Because the Ollama provider now routes through the
> `llm_service` client (which has its *own* transient-fault, rate-limit, and thinking-downgrade retry
> loops), the envelope's macro-retries sit *on top of* the client's. Wall-clock is still bounded —
> `STRATEGY_LAB_LLM_TOTAL_BUDGET` and the per-call guard cap elapsed time, and a true
> `LLMSemanticExhaustionError` is fatal (not macro-retried) — but the worst-case *attempt count*
> against a persistently-slow endpoint is the product of the two layers. Set `STRATEGY_LAB_LLM_MAX_RETRIES=0`
> to disable the envelope's macro-retries and rely on the client's loops alone when running against
> Ollama. (The **Bedrock** native path has no client-level retry, so it relies on the envelope's.)

### STRATEGY_LAB_LLM_BACKOFF_BASE / STRATEGY_LAB_LLM_BACKOFF_MAX
Jittered exponential backoff between envelope retries for **transient** (5xx / connection / timeout)
failures: `min(base**attempt + uniform(0,1), max)` seconds. Fall back to `LLM_BACKOFF_BASE` /
`LLM_BACKOFF_MAX`, else `2.0` / `60.0`. HTTP 429 rate limits use the separate rate-limit schedule
below.

### STRATEGY_LAB_LLM_RATE_LIMIT_BACKOFF_INITIAL / STRATEGY_LAB_LLM_RATE_LIMIT_BACKOFF_MAX
Slow **429** rate-limit backoff for the envelope: first rate-limit retry waits `…_INITIAL` seconds
(default cascade `STRATEGY_LAB_LLM_RATE_LIMIT_BACKOFF_INITIAL` → `LLM_RATE_LIMIT_BACKOFF_INITIAL` →
`300`), doubling with additive jitter up to `…_MAX` (→ `LLM_RATE_LIMIT_BACKOFF_MAX` → `3600`). The
cap is floored at the initial so the 300s floor always holds. A weekly-usage cap
(`OLLAMA_WEEKLY_LIMIT_MESSAGE`) stays **fatal** (never retried). **Interplay:** the envelope keeps
its single attempt counter (`STRATEGY_LAB_LLM_MAX_RETRIES`) and the total-budget deadline as the
terminator — each rate-limit sleep is clamped to the remaining `STRATEGY_LAB_LLM_TOTAL_BUDGET`
(default `(max_attempts × timeout) × 1.5` ≈ 4050s), so under defaults a 429-storm realistically gets
the 300s + 600s waits before budget exhaustion. Raise `STRATEGY_LAB_LLM_MAX_RETRIES` and
`STRATEGY_LAB_LLM_TOTAL_BUDGET` to ride the full schedule. There is no separate rate-limit retry
count for the envelope.

### STRATEGY_LAB_LLM_TOTAL_BUDGET
Hard cap (seconds) on cumulative wall time across all attempts of a single envelope call (per-call
timeout and each backoff sleep are clamped to the remaining budget). On exhaustion the envelope
raises `StrategyLabLLMError` with `outcome="budget_exhausted"`. Defaults to
`(max_attempts × timeout) × 1.5`.

### STRATEGY_LAB_DESIGN_REVIEW_ROUNDS
Cap on the design ↔ design-review loop inside `_run_design_attempt` (default `20`, sub-1 values
floored to `1`). The loop runs
`DesignAgent → SpecReadinessGate → DesignReviewAgent → DesignAgent.revise` until the reviewer marks
the spec ready or this cap is reached; exhaustion short-circuits the cycle with
`status="failed: design_not_ready"` rather than running code against a spec that never converged.

### STRATEGY_LAB_DESIGN_REVIEW_STALL_ROUNDS
Within-loop stall threshold (default `3`, sub-1 values floored to `1`). A `CritiqueLedger` assigns
each reviewer critique a deterministic, content-derived `issue_id`
and tracks the blocking (warning/critical) open-issue set round over round. When that set is
non-empty and **unchanged for this many consecutive rounds** the loop short-circuits early with
`status="failed: design_stalled"` — distinct from honest round-cap exhaustion
(`failed: design_not_ready`) — instead of churning to `STRATEGY_LAB_DESIGN_REVIEW_ROUNDS`. The ledger
also flags **regressions** (an issue resolved on an earlier round that reappears): the reintroduced
issue is surfaced to `DesignAgent.revise` as an explicit "do not reintroduce" notice
(flag-and-escalate; the round is not hard-blocked). Per-cycle generation-funnel telemetry
(design-review round count + stop reason, critique-ledger resolved/regressed/open totals, per-gate
pass/fail histograms, compiled-vs-custom share) is emitted live on the `on_phase` callback as
`"telemetry"` events and persisted on `StrategyLabRecord.loop_telemetry`;
`scripts/audit_recent_runs.py` surfaces the aggregate post-hoc.

### STRATEGY_LAB_MECHANICAL_REPAIR_ENABLED
Master toggle for the deterministic mechanical-repair pre-flight inside the design ↔ review loop
(default `true`; accepted truthy values `true`/`1`/`yes`, case-insensitive; anything else disables).
Before **every** review round (regardless of the readiness verdict),
`strategy_lab/mechanical_repair.py:repair_spec` applies fully-determined, semantics-preserving fixes
so they never cost an LLM `DesignAgent.revise` round; mechanical fixes re-validate and only fall
through to the revise path for criticals the machine cannot fix. Scope is intentionally minimal —
(1) coerce an intraday `timeframe`→`"1d"` for asset classes with no intraday data (readiness Rule 7),
(2) clamp `risk_limits.max_position_pct` to the shared `MAX_POSITION_PCT_CEILING` (Rule 8) — plus a
trial `compile_strategy()` that flips `requires_custom_code=True` on `CompilerError`, so even a
readiness-clean spec outside the deterministic-compiler envelope (e.g. a `volatility_target` spec
without an ATR predicate — readiness only *warns* on that sizing mode) selects the custom-code path
during design rather than discovering it later in synthesis. Each edit is recorded on the `on_phase`
callback as a `"design_repair"` event and counted in `loop_telemetry.mechanical_repairs`; substantive
defects (empty entry/exit rules, thesis coherence) are left to the LLM. Disable to restore the pure
LLM-revise behaviour.

### STRATEGY_LAB_CODE_CONFORMANCE_RETRIES
Number of predicate-conformance gate retries before demoting criticals to warnings (default `2`).
The gate runs in the synthesis loop after `CodeConformanceGate`,
only for `requires_custom_code=True` strategies. Each retry feeds the per-bar diff back through
`_refine_or_exhaust`; after exhaustion the pipeline proceeds to backtest with the best-effort code.

### STRATEGY_LAB_SIZING_COHERENCE_TOLERANCE
Relative tolerance for `SpecReadinessGate`'s position-sizing coherence rule (default `0.05` = 5%).
Risk model: `sizing.fraction` / `max_position_pct`
is capital *deployed* per position as a % of the account — and **is** the per-trade loss cap, because
an entered position can lose up to ~100% of what was deployed (there is no separate
`max_loss_per_trade_pct`; it was a duplicate of `max_position_pct` and has been removed).
`stop_loss.pct` is a price move off entry (% of the *trade*) — an independent, optional safeguard that
limits a position's realised loss *below* a full wipeout; it is decoupled from sizing and never
multiplied into the cap. Two deterministic checks: (A) `sizing.fraction` ≤ `max_position_pct`
(critical; skipped when the deployed fraction is unknown — `volatility_target`, unconfigured
`fixed_notional`); (C) a prose-stated per-trade deployment % ("deploy/allocate/risk X% per trade")
reconciled against the **actual** deployed fraction (`sizing.fraction`) when known — the cap is an
upper bound, not the deployed amount, so matching it alone does not satisfy the claim (warning);
`volatility_target` deployment is dynamic so this check abstains. This tolerance applies **only** to
the prose check (C) — the hard cap check (A) is strict (a negligible float-noise epsilon only) so a
real limit breach can never pass readiness. A SHORT can lose more than 100% of deployed capital, so
the runtime (`TradingService`) auto-injects a 100%-adverse-move stop for any short lacking an
effective stop, bounding a short's modeled worst-case loss at the deployed amount too. A critical
routes through the synthetic-critique path and, if the reviser cannot resolve it, trips the existing
`design_stalled` early-terminate.

### STRATEGY_LAB_DESIGN_PARSE_RETRIES
Number of times `DesignAgent._invoke_and_parse` re-prompts the LLM when its JSON parses but fails
structured-DSL validation (default `2` → 3 attempts total; `0` disables retry). The re-prompt quotes
the offending field and the pydantic error so the model can
self-correct one-off slips (e.g. wrapping `bar.close` in an `IndicatorRef`, or setting `source` to
an indicator name). Exhaustion still raises `StrategySpecParseError` — the cycle short-circuits
exactly as before.

### STRATEGY_LAB_REFINEMENT_PARSE_RETRIES
Number of times `RefinementAgent._invoke_and_parse` re-prompts the LLM when its response carries no
recoverable JSON object (default `2` → 3 attempts total; `0` disables retry; sub-zero clamps to `0`).
Refinement asks the model to emit the *complete* fixed program as
a JSON string — a long generation that occasionally comes back empty, thinking-only, or prose-only.
That is not a transport fault (the fault-tolerance envelope sees a "successful" string), so without
this retry a single such response wastes the whole refinement round and the orchestrator falls back
to the unchanged code. The re-prompt quotes the parse error and re-attaches the original task,
instructing the model to re-emit a single fence-free JSON object with exactly `strategy_code` +
`changes_made`. Each attempt builds a fresh history-free `Agent`. Exhaustion raises `ValueError`,
which `_refine` catches and falls back to the original code as before. Mirrors
`STRATEGY_LAB_DESIGN_PARSE_RETRIES`.

### STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED
Toggle for the internal self-review pass inside `DesignAgent.run()` and `DesignAgent.revise()`
(default `true`; accepted truthy values `true`/`1`/`yes`, case-insensitive; anything else disables).
When enabled, every spec the designer emits goes through a second LLM call
(`design_self_review_system.md`) that audits prose ↔ predicate completeness and risk-math coherence;
if the self-review marks the spec not-ready the designer self-revises (up to
`STRATEGY_LAB_DESIGN_SELF_REVISION_ROUNDS` rounds, default once) with the self-critique as feedback
and then **re-audits** the revised spec through self-review before returning — so a self-revision
that introduces a fresh contradiction is caught rather than reaching the external reviewer. When
invoked from `revise()`, the external critique lineage and regression notice are threaded into the
self-revision so prior-round fixes are not regressed. Best-effort: any self-review failure logs a
warning and returns the current spec — the external `DesignReviewAgent` loop remains authoritative.
Disable to restore the pre-change single-call behaviour.

### STRATEGY_LAB_DESIGN_SELF_REVISION_ROUNDS
Cap on internal self-revision rounds inside `DesignAgent._with_self_review` (default `1`, sub-0
floored to `0`). Each round is one self-revision LLM call followed
by a re-audit through self-review; the re-audit closes the gap where a self-revision could introduce
a fresh prose↔predicate / risk-math contradiction that then reached the external `DesignReviewAgent`
loop unchecked. `0` disables self-revision (audit-only — the spec is audited but never internally
revised). When `_with_self_review` runs inside `revise()` the external critique lineage and
regression notice are threaded into the self-revision prompt so prior-round fixes are not regressed.
Each enabled round adds two charged LLM calls to the design-phase budget — the self-revision plus its
re-audit (more if the self-revision hits parse-retries) — so size `STRATEGY_LAB_DESIGN_MAX_LLM_CALLS`
accordingly.

### STRATEGY_LAB_REGIME_SUMMARY_ENABLED
Toggle for injecting a current **market-regime summary** into `DesignAgent.run()` (default `true`;
truthy `true`/`1`/`yes`, case-insensitive; anything else disables). When enabled, `run_cycle`
computes a lightweight regime read once per cycle via `market_regime.compute_regime_summary` over the
orchestrator's live `MarketDataService` — for a small fixed set of asset-class benchmarks (`SPY` for
stocks, `BTC-USD` for crypto) it classifies **trend direction** (close vs SMA50/SMA200), **trend
strength** (ADX(14) buckets), and **volatility regime** (latest ATR% ranked against its own trailing
distribution). The summary is rendered into a `## Market Regime` prompt section so the designer can
pick the setup archetype that fits the regime (see the "Setup playbook" in `design_system.md`).
Fully **fail-open**: any data-fetch or compute error skips that benchmark and degrades the summary
rather than raising; a degraded/empty summary renders no prompt section, so the design cycle never
depends on market data being reachable. The regime read is shared across every design re-entry in a
cycle. Disable to restore the pre-change context-free designer prompt.

### Ollama LLM transport (routed through `llm_service`)
For the default **Ollama** provider, `get_strands_model` (`strategy_lab/agents/model_factory.py`)
routes every Strategy Lab LLM call through the platform's hardened `llm_service` client (via
`llm_service.strands_adapter.get_strands_model`) instead of constructing strands' native
`OllamaModel`. This closes the failure class where a thinking-enabled model returns an empty /
thinking-only / prose-only turn on a long code-emitting generation: the strands-native path returned
that as a "successful" empty string, which the parser then rejected with
`No JSON object found in LLM response`, wasting the whole round. The `llm_service` client instead
**detects** an empty response, **retries once with reduced thinking** ("proof-of-change"), and raises
`LLMSemanticExhaustionError` only when the payload truly yields nothing — and it resolves the model's
`num_ctx` from `/api/show` (so a large refinement prompt is not silently truncated), caps
`max_tokens`, and adds rate-limit / JSON-repair handling. Agents that recover their result with
`extract_json_object` (design, design-review, refinement, zero-trade-repair, alignment, analysis) use
`response_format="json"` (forces a JSON object on the wire); `CodeSynthesisAgent`, which emits a raw
Python file, uses `response_format="text"`. The **Bedrock** path is unchanged (native `BedrockModel`).

The JSON *shape* contract on the Ollama path is enforced by `json_object` wire mode plus pydantic
validation downstream (and, for `RefinementAgent`, a schema embedded verbatim in its prompt). This
replaced the strands-native decoder-level `format=<schema>` constraint — which, paired with thinking
on long generations, was itself a contributor to the empty-response failure this routing fixes. The
former `STRATEGY_LAB_STRUCTURED_OUTPUT_ENABLED` toggle and the per-call `response_schema=` argument
have been retired. The canonical wire-shape definitions still live in `agents/_response_schemas.py`
(validated for well-formedness by the test suite).

### STRATEGY_LAB_DESIGN_MAX_LLM_CALLS
Per-cycle hard cap on the total number of LLM calls the design phase may make within a single
`run_cycle`, spanning all `MAX_DESIGN_REENTRIES` re-entries (default `120`, sub-1 values floored to
`1`). A `LLMCallBudget` is created once per cycle and charged
before every design/review LLM call (generation, each parse-retry, the self-review verdict, each
self-revision, and each `DesignReviewAgent` round); when it trips the cycle short-circuits with
`status="failed: budget_exhausted"` (distinct from `failed: design_not_ready`) before runaway cloud
spend. **Worst-case sizing:** at default settings one design round can cost up to ~9 LLM calls —
`revise` is up to 8 (3 parse-retries + 1 self-review verdict + 3 self-revision parse-retries + 1
re-audit verdict) plus 1 `DesignReviewAgent` round — so the uncapped worst case is
`~9 calls × STRATEGY_LAB_DESIGN_REVIEW_ROUNDS (20) × 3 attempts ≈ 540` calls per design phase; this
budget ceilings that. Raise it for genuinely hard-but-converging specs; lower it to tighten the
cost/quota ceiling.
