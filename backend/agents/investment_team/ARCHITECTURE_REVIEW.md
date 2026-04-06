# Strategy Lab Team -- Architecture Review

**Date**: 2026-04-06
**Reviewers**: Principal Solutions Architect (Security & Distributed Systems), Senior Agentic AI Engineer (Output Quality & Prompt Engineering)
**Scope**: Strategy Lab agents, orchestration, API layer, market data service, prompt quality, output quality gates

---

## Executive Summary

The Strategy Lab implements an automated swing-trading strategy research pipeline: ideation, backtesting, paper trading, and promotion gating. The domain models (Pydantic), trade simulation engine, multi-provider market data service, and safety gates (PolicyGuardian, PromotionGate) are well-engineered.

However, the team suffers from three categories of systemic weakness:

1. **Infrastructure** -- No Strands SDK adoption, LLM-per-bar simulation cost, fragile distributed state, prompt injection vulnerabilities, workflow logic in the HTTP layer.
2. **Agent intelligence** -- Prompts lack decision frameworks, no decomposed reasoning, bar evaluation prompt asks the LLM to evaluate conditions it has no data to assess, arbitrary undocumented temperature choices.
3. **Output quality** -- Zero deterministic validators on strategy output, no backtest anomaly detection, no structured failure-to-feedback pipeline, no convergence detection across batch cycles, self-review only applies to post-backtest narrative (not ideation).

The blogging team has 5 layers of quality gates (self-review, deterministic validators, fact-checking, compliance, convergence detection with human escalation). The architect-agents team has phased constraint propagation, a universal priority framework, and a scrutineer quality gate. The Strategy Lab has a single self-review step and a hard-coded 8% return threshold. This is the primary reason output quality is limited.

This review identifies 16 issues across infrastructure, agent intelligence, and output quality, with a phased migration roadmap including rollback criteria.

---

## Part I: Infrastructure Issues

### CRITICAL-1: LLM-Per-Bar Simulation Is Prohibitively Expensive

| Attribute | Detail |
|-----------|--------|
| **Files** | `trade_simulator.py:88-155` (`evaluate_bar`), `backtesting_agent.py`, `paper_trading_agent.py` |
| **Impact** | Cost, latency, scalability |

`TradeSimulationEngine.run()` calls the LLM for every qualifying bar. The `max_evaluations=5000` cap and `think=True` on every call confirm this is both known and extreme.

**Cost estimate**: A batch of 5 strategy cycles at ~5,000 evaluations each = ~25,000 LLM calls with extended thinking.

**Recommendation -- Tiered rule compilation**:

| Tier | Approach | LLM Calls | Use When |
|------|----------|-----------|----------|
| **Tier 1** | Compile technical/price rules into a `CompiledStrategy` Pydantic model. `CompiledRuleEvaluator` walks bars deterministically. | 1 (compilation) | Rules reference price, volume, technical indicators only |
| **Tier 2** | Batch-evaluate bars in groups of 5-10 with pre-computed indicators injected. | ~100-500 | Rules require non-deterministic signals (sentiment, macro) |
| **Tier 3** | Post-hoc LLM review of the full trade ledger. | 1 | Always (pattern analysis) |

**Security constraint**: The compiled-rule DSL must be a closed-form language with whitelisted operators. No `eval()`. LLM output validated against strict schema before evaluator accepts it.

---

### CRITICAL-2: No Strands SDK Adoption

| Attribute | Detail |
|-----------|--------|
| **Files** | `strategy_ideation_agent.py`, `signal_intelligence_agent.py`, `backtesting_agent.py`, `paper_trading_agent.py` |
| **Impact** | Capability, observability, composability |

Every agent uses raw `self.llm.complete_json()`. The architect-agents team demonstrates the Strands pattern with `Agent()` + `@tool` decorators for tool calling, session persistence, callback handlers, and agent-as-tool composition.

**Recommendation**: Migrate to Strands SDK as a hard dependency. See Part II for tool design requirements.

---

### CRITICAL-3: Prompt Injection Defenses Are Inadequate

| Attribute | Detail |
|-----------|--------|
| **File** | `signal_intelligence_agent.py:44-51` |
| **Impact** | Security |

Single regex defense (`"ignore (all )?(previous|prior) instructions"`) is trivially bypassed. Market data from 4 external providers flows into LLM prompts unsanitized. Strands migration amplifies risk via tool access.

**Recommendations**: Multi-layer defense (strip non-ASCII, structured JSON encoding, tool input validation, output monitoring).

---

### HIGH-4: FinancialAdvisorAgent Uses Regex Instead of LLM

| Attribute | Detail |
|-----------|--------|
| **File** | `agents.py:615-881` (`_extract_topic_data` -- 266 lines) |
| **Impact** | Accuracy, maintainability |

**PII constraint**: Must use local model only (Ollama local inference, not Cloud).

---

### HIGH-5: Workflow Orchestration Lives in API Layer

| Attribute | Detail |
|-----------|--------|
| **File** | `api/main.py:898-1244` |
| **Impact** | Testability, reusability, separation of concerns |

Strategy Lab workflow is private functions in FastAPI module. Should be extracted to `strategy_lab_orchestrator.py`. Evaluate Temporal integration for durable execution.

---

### HIGH-6: Distributed State Is Fragile

| Attribute | Detail |
|-----------|--------|
| **Files** | `api/main.py:75-146`, `api/main.py:1051-1076` |
| **Impact** | Reliability, data integrity |

Split-brain between in-memory and persistent state. No idempotency. Single lock blocks all API reads during network calls.

---

### MEDIUM-7: No Market Data Caching or Integrity Checks

`market_data_service.py` -- no caching, no integrity validation. Add TTL cache + outlier detection.

### MEDIUM-8: Generic Exception Handling

~28 bare `except Exception` instances ignoring the well-designed `LLMError` hierarchy.

### MEDIUM-9: SSE Reliability

No `Last-Event-ID`, no backpressure, no event replay on reconnection.

---

## Part II: Agent Intelligence Issues

### HIGH-10: Prompts Lack a Decision-Making Framework

| Attribute | Detail |
|-----------|--------|
| **Files** | All system prompts in `strategy_ideation_agent.py`, `signal_intelligence_agent.py`, `trade_simulator.py`, `paper_trading_agent.py` |
| **Impact** | Output consistency, decision quality |

The architect-agents team gives every specialist an identical 6-tier priority framework (Security > Simplicity > Architecture > Performance > Cost > Scalability) with "never sacrifice a higher priority for a lower one." This creates consistent, principled reasoning across all agents.

The Strategy Lab agents have **no equivalent**. When the LLM must choose between a higher-Sharpe strategy vs. one with better drawdown characteristics, it has no framework. The result is inconsistent decision-making across cycles.

**Recommendation**: Define an Investment Priority Framework and inject it into every agent's system prompt:

```
## Decision Priority Framework (apply in order; never sacrifice a higher priority for a lower one)
1. RISK CONTROL — Drawdown limits, position sizing, stop-losses are non-negotiable
2. SIGNAL QUALITY — Edge must be grounded in identifiable market dynamics, not curve-fitting
3. EVALUABILITY — Rules must be testable against available data (OHLCV + computable indicators)
4. DIVERSIFICATION — Avoid correlated strategies and asset-class concentration
5. RETURN MAGNITUDE — Higher returns preferred only after above priorities are satisfied
6. COMPLEXITY — Simpler strategies preferred when expected return is comparable
```

---

### HIGH-11: Bar Evaluation Prompt Asks the LLM to Hallucinate

| Attribute | Detail |
|-----------|--------|
| **File** | `trade_simulator.py:50-85` (`_EVALUATE_PROMPT`) |
| **Impact** | Trade decision quality |

This is the most-called prompt in the system (thousands of times per backtest) and it has a fundamental design flaw: it sends the strategy's natural-language rules (e.g., "RSI < 30 AND SMA_20 crosses above SMA_50") alongside raw OHLCV bars, but **no computed indicators**. The LLM cannot compute RSI or moving average crossovers from a 20-bar price table. It's forced to *approximate or hallucinate* whether technical conditions are met.

Additionally:
- The full strategy definition (~300 tokens) is re-sent on every call -- redundant context waste.
- The `confidence` score (0.0-1.0) in the output is never used downstream -- the engine acts on any non-"hold" action regardless.
- The prompt doesn't distinguish between entry evaluation (which requires signal detection) and exit evaluation (which often requires only price-vs-stop comparison).

**Recommendation (applies to both Tier 1 compilation and Tier 2 batch evaluation)**:
1. Pre-compute all referenced technical indicators (RSI, SMA, EMA, ATR, volume averages) and inject them as structured data alongside the bar.
2. For Tier 2: send a batch of 5-10 bars with pre-computed indicators in a single call, with the strategy rules sent once at the top.
3. Use the confidence score: require confidence > 0.6 for entries (or make the threshold configurable).
4. Separate entry and exit evaluation prompts with different instructions.

---

### HIGH-12: Ideation Prompt Lacks Decomposed Reasoning

| Attribute | Detail |
|-----------|--------|
| **File** | `strategy_ideation_agent.py:32-66` (`_IDEATION_PROMPT`) |
| **Impact** | Strategy novelty and quality |

The prompt asks for everything at once: "Generate ONE novel swing-style strategy" with 9 JSON fields. It front-loads up to 15,000 tokens of prior results and a signal brief, then expects a single-shot JSON response. There is no decomposition.

The architect-agents prompts break complex tasks into explicit phases. The ideation prompt should do the same.

**Recommendation**: Restructure ideation as a multi-step reasoning chain (can be done within a single prompt with explicit sections, or as separate Strands tool calls):

```
Step 1: ANALYZE — What edges did prior strategies miss? What failure modes recurred?
Step 2: HYPOTHESIZE — What exploitable market dynamic could generate alpha?
Step 3: DESIGN — What entry/exit rules capture this edge using available data?
Step 4: STRESS-TEST — What would cause this strategy to fail? What confounders exist?
Step 5: OUTPUT — Produce the strategy JSON only after completing steps 1-4.
```

This forces the LLM to reason before producing output, rather than generating a formulaic strategy that fits the JSON template.

---

### MEDIUM-13: Temperature Choices Are Arbitrary and Undocumented

| Agent | Temperature | Documented Rationale? |
|-------|------------|----------------------|
| Ideation | 0.85 | No |
| Signal intelligence | 0.58 | No |
| Analysis draft | 0.35 | No |
| Self-review | 0.15 | No |
| Bar evaluation | 0.2 | No |
| Divergence analysis | 0.3 | No |

Six different temperatures with no documented reasoning. These will need re-tuning after any model change.

**Recommendation**: Consolidate to 3 documented tiers:
- **Creative** (0.7-0.8): Ideation, signal intelligence
- **Analytical** (0.3-0.4): Analysis, divergence, batch evaluation
- **Deterministic** (0.1-0.2): Self-review, compilation

---

### MEDIUM-14: Tool Design Specifications Missing

The plan lists tool names (`fetch_market_snapshot`, `compute_indicators`, `compile_strategy`, `run_backtest`) but provides no guidance on tool description quality. The architect-agents' tools have detailed docstrings explaining when to use them, what they return, and their fallback behavior. Poor tool descriptions are the #1 source of agentic failures.

**Recommendation**: Each `@tool` function must specify:
- **When to call**: Trigger conditions (e.g., "Call before generating entry rules to verify indicator availability")
- **When NOT to call**: Anti-patterns (e.g., "Do not call run_backtest during ideation -- use compile_strategy to validate first")
- **Return format**: What the agent receives and how to interpret it
- **Failure modes**: What happens when the tool fails and what the agent should do

---

## Part III: Output Quality Issues

### CRITICAL-15: No Output Quality Gates

| Attribute | Detail |
|-----------|--------|
| **Files** | `strategy_ideation_agent.py`, `api/main.py:898-1033` |
| **Impact** | Strategy quality, backtest reliability |

**Comparison with blogging team** (gold standard for quality gates):

| Gate Type | Blogging Team | Strategy Lab |
|-----------|--------------|--------------|
| **Self-review loop** | Planning agent validates with `plan_acceptable` + `scope_feasible` | Self-review on post-backtest narrative only (not ideation) |
| **Deterministic validators** | Banned phrases, reading level, section structure, claims policy | None |
| **Fact-checking** | Claims verified against allowed claims list | None |
| **Compliance gate** | Brand/style gate, fail-closed on LLM failure | None |
| **Convergence detection** | FeedbackTracker with Jaccard similarity, human escalation after 10 iterations | None |
| **Structured failure feedback** | Gate failures converted to `FeedbackItem` objects with severity levels | Raw prior results text only |

**What's missing**:

**A. No deterministic validation of ideated strategies**

When `ideate_strategy()` returns a strategy dict, the only check is JSON validity. No verification of:
- Rule logical consistency (contradictory entry/exit rules)
- Parameter bounds (e.g., `max_position_pct: 200` should be rejected)
- Rule evaluability (do the rules reference data the backtest engine can provide?)
- Asset class alignment (forex strategy with stock-specific rules)

**B. No backtest result anomaly detection**

The 8% annualized return threshold is the only quality gate. No detection of:
- Suspiciously high returns (>200% annualized = likely bug or data issue)
- Too few trades (<5 in a year = statistically meaningless)
- Unrealistic win rates (>90% on swing trades = almost certainly overfitting)
- Extreme profit factors (>10 = usually data snooping)

**C. No structured failure-to-feedback pipeline**

When a strategy fails, the flow is: record it, move on, hope `format_prior_results` helps the next cycle. The blogging team converts every gate failure into structured `FeedbackItem` objects with severity levels, then feeds them back for targeted revision.

**D. Self-review only applies to narrative, not to ideation or metrics**

The `_SELF_REVIEW_PROMPT` checks narrative faithfulness to metrics. But it doesn't check whether the metrics themselves are plausible. And ideation has zero self-review.

**Recommendation**: Implement a multi-layer quality gate system:

```
Ideation → [Deterministic Strategy Validator] → [Ideation Self-Review]
    → Backtest → [Anomaly Detector] → [Analysis + Self-Review]
    → [Structured Failure Feedback → next cycle's ideation prompt]
```

---

### HIGH-16: No Convergence Detection or Batch Intelligence

| Attribute | Detail |
|-----------|--------|
| **File** | `api/main.py:1176-1222` (batch loop) |
| **Impact** | Batch effectiveness |

The batch worker runs N cycles sequentially with no mechanism to detect:
- The ideation agent is generating the same category of failing strategy repeatedly
- The signal brief is actively misleading ideation (correlation between brief content and backtest failure)
- The system is stuck in a local optimum (all recent strategies are slight variations on the same theme)
- Asset class distribution has collapsed (e.g., 8 of last 10 strategies are stocks despite the mix hint)

The blogging team has a `FeedbackTracker` with Jaccard similarity to detect stalled loops, occurrence counting for persistent issues, and human escalation thresholds.

**Recommendation**: Implement a `StrategyFeedbackTracker` that monitors:
- Asset class distribution over last N cycles
- Failure mode repetition (same failure pattern recurring)
- Strategy similarity detection (are ideated strategies converging?)
- Adaptive signal brief refresh trigger (refresh when failure rate > threshold)
- Structured failure directives that get stronger when the same issue repeats ("MANDATORY: do not generate another stock-based momentum strategy -- the last 3 all failed due to transaction cost drag")

---

## Migration Roadmap

### Phase 0: Foundation
*Backward-compatible, no Strands dependency*

- [ ] Extract `strategy_lab_orchestrator.py` from `api/main.py`
- [ ] Replace single `_lock` with per-entity concurrency primitives
- [ ] Make `_active_runs` a read-through cache backed by JobServiceClient
- [ ] Add idempotent cycle IDs with duplicate detection
- [ ] Add market data cache with integrity checks
- [ ] Replace bare `except Exception` with specific `LLMError` subtypes
- [ ] Switch `evaluate_bar` to `think=False`
- [ ] Add `Last-Event-ID` support to SSE event bus
- [ ] Define and inject Investment Priority Framework into all system prompts
- [ ] Document and consolidate temperature choices to 3 tiers

**Rollback gate**: `make test` error rate must not increase.

### Phase 0.5: Security Hardening

- [ ] Replace `sanitize_brief_for_injection` with multi-layer defense
- [ ] Add input sanitization at all data boundaries
- [ ] Define compiled-rule DSL schema with whitelisted operators
- [ ] Security review of all prompt templates for injection vectors
- [ ] Assess PII handling for FinancialAdvisorAgent
- [ ] Add auth to sensitive endpoints (`DELETE /strategy-lab/clear`, `POST /profiles`)

**Rollback gate**: Security review must pass.

### Phase 0.75: Output Quality Gates (NEW)

- [ ] Deterministic strategy validator (rule consistency, parameter bounds, evaluability, asset class alignment)
- [ ] Backtest anomaly detector (outlier returns, insufficient trades, unrealistic win rates, extreme profit factors)
- [ ] Ideation self-review loop (draft strategy -> verify against priors and failure modes -> revise or accept)
- [ ] Structured failure-to-feedback pipeline (convert backtest failures to severity-tagged directives for next ideation)
- [ ] `StrategyFeedbackTracker` for convergence detection (asset class distribution, failure repetition, strategy similarity, adaptive brief refresh)
- [ ] Establish output quality benchmark: run current system on 10 fixed scenarios, record baseline metrics (strategy diversity score, novel-vs-repeated ratio, metric plausibility)

**Rollback gate**: Quality gates must not reject >50% of previously-successful strategies (avoid over-filtering).

### Phase 1: Rule Compilation Engine

- [ ] Define `CompiledStrategy` Pydantic model with closed-form DSL
- [ ] Create `CompiledRuleEvaluator` (deterministic, no LLM)
- [ ] Pre-compute technical indicators (RSI, SMA, EMA, ATR, volume averages) and inject into Tier 2 batch evaluation prompts
- [ ] Create `compile_strategy()` with schema validation
- [ ] Implement Tier 2 batch evaluation with separated entry/exit prompts
- [ ] Use confidence threshold (>0.6) for trade entries
- [ ] A/B regression comparison against LLM-per-bar path
- [ ] When compiled vs. LLM divergence exceeds threshold: analyze disagreement bars, feed insights back into DSL design

**Rollback gate**: Win rate divergence <15%, annualized return divergence <25% on 10 reference strategies.

### Phase 2: Strands SDK Migration + Prompt Rewrite

- [ ] Add `strands-agents>=1.0.0` as hard dependency
- [ ] Rewrite ideation prompt with decomposed reasoning (analyze -> hypothesize -> design -> stress-test -> output)
- [ ] Write detailed `@tool` docstrings with trigger conditions, anti-patterns, return formats, and failure modes
- [ ] Convert `StrategyIdeationAgent` to Strands `Agent` with `@tool` functions
- [ ] Convert `SignalIntelligenceExpert` to Strands `Agent`
- [ ] Convert backtesting/paper trading to compiled rules + Tier 2
- [ ] Wire `SSECallbackHandler` to `job_event_bus`
- [ ] A/B test: compare strategy quality (diversity, novelty, plausibility) between old and new prompts on 10 fixed scenarios vs. Phase 0.75 baseline

**Rollback gate**: End-to-end error rate increase <5% across 50 test runs. Strategy quality metrics must meet or exceed Phase 0.75 baseline.

### Phase 3: Session Persistence & Temporal Evaluation

- [ ] Create `session.py` (FileSessionManager local, S3SessionManager prod)
- [ ] Encrypt session data at rest (financial PII)
- [ ] Evaluate Temporal activities for cycle phases
- [ ] If viable, migrate `_strategy_lab_worker` to Temporal workflow

**Rollback gate**: Temporal crash recovery within 30 seconds on process kill.

### Phase 4: FinancialAdvisorAgent Migration

- [ ] Convert to Strands `Agent` with structured extraction tools
- [ ] **Hard requirement**: Local model only (PII)
- [ ] Property-based tests: 100+ edge-case inputs
- [ ] Regression test against current regex parser

**Rollback gate**: LLM extraction accuracy must be >=95%.

### Phase 5: Orchestration Enhancement (OPTIONAL)
*Gated on 30+ days stable operation of Phases 0-4*

- [ ] Register agents as `@tool` functions for lead orchestrator
- [ ] **Hard constraint**: Deterministic state machine bounds all decisions. LLM can advise transitions but hard gates (validation, promotion, policy guardian, quality gates) are mandatory and non-skippable.

---

## Verification Matrix

| Category | Test | Phase |
|----------|------|-------|
| **Functional** | `make test` passes | All |
| **Functional** | A/B backtest comparison (10 strategies) | 1 |
| **Functional** | LLM call count reduction measurement | 1 |
| **Functional** | Advisor edge-case accuracy (100+ inputs) | 4 |
| **Output Quality** | Baseline benchmark on 10 fixed scenarios | 0.75 |
| **Output Quality** | Strategy diversity score (asset class distribution, signal family variety) | 0.75, 2 |
| **Output Quality** | Metric plausibility score (% of backtests passing anomaly detector) | 0.75 |
| **Output Quality** | Ideation novelty ratio (unique-vs-repeated strategy patterns) | 0.75, 2 |
| **Output Quality** | Prompt A/B test: old vs. decomposed ideation prompt quality comparison | 2 |
| **Output Quality** | Convergence detection: verify tracker catches repeated failure patterns | 0.75 |
| **Security** | Prompt injection fuzzing at all data boundaries | 0.5 |
| **Security** | Compiled-rule DSL fuzz testing (no code execution) | 1 |
| **Security** | PII audit (advisor LLM calls stay local) | 4 |
| **Reliability** | Chaos testing: kill process mid-cycle x10, verify no duplicates | 0, 3 |
| **Reliability** | Lock contention: 3 concurrent readers during batch, p99 <500ms | 0 |
| **Reliability** | SSE reconnection: no events lost with Last-Event-ID | 0 |
| **Regression** | Each phase has explicit rollback criteria (see above) | All |
