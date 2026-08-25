# System Design — Investment Team

Component view of the API router, detail on the orchestrator queues and
promotion gates, and a class diagram of the core Pydantic domain models.

For the container-level view of how this team fits into Khala, see
[`architecture.md`](./architecture.md).

## Component diagram — API router

How each endpoint reaches an agent or orchestrator call and which persistence
bucket it reads/writes. Every route below is declared in
[`api/main.py`](../api/main.py); grep the route path to find its handler. Line
numbers are deliberately omitted here — that file changes several times a day,
and the route path is a stable identifier that never goes stale.

```mermaid
flowchart LR
  subgraph advisor_api[Advisor Endpoints]
    E1["POST /advisor/sessions"]
    E2["POST /advisor/sessions/{id}/messages"]
    E3["GET /advisor/sessions/{id}"]
    E4["POST /advisor/sessions/{id}/complete"]
    E5["POST /profiles"]
    E6["GET /profiles/{user_id}"]
    E7["POST /proposals/create"]
    E8["GET /proposals/{id}"]
    E9["POST /proposals/{id}/validate"]
    E10["POST /memos"]
  end

  subgraph shared_api[Shared Endpoints]
    S1["POST /strategies"]
    S2["POST /strategies/{id}/validate"]
    S3["POST /backtests"]
    S4["GET /backtests"]
    S5["POST /promotions/decide"]
    S6["GET /workflow/status"]
    S7["GET /workflow/queues"]
    S8["GET /health"]
  end

  subgraph lab_api[Strategy Lab Endpoints]
    L1["POST /strategy-lab/run"]
    L2["GET /strategy-lab/results"]
    L3["GET /strategy-lab/jobs"]
    L4["POST /strategy-lab/runs/{id}/resume"]
    L5["POST /strategy-lab/runs/{id}/restart"]
    L6["GET /strategy-lab/runs"]
    L7["GET /strategy-lab/runs/{id}/status"]
    L8["GET /strategy-lab/runs/{id}/stream"]
    L9["DELETE /strategy-lab/records/{id}"]
    L10["DELETE /strategy-lab/storage"]
    L11["POST /strategy-lab/paper-trade"]
    L12["GET /strategy-lab/paper-trade/results"]
    L13["GET /strategy-lab/paper-trade/{id}"]
  end

  subgraph handlers[Agents & Orchestrator]
    FA[FinancialAdvisorAgent]
    PG[PolicyGuardianAgent]
    VA[ValidationAgent]
    PGate[PromotionGateAgent]
    IC[InvestmentCommitteeAgent]
    SIE[SignalIntelligenceExpert<br/>once per batch]
    CycleWF[StrategyLabCycleWorkflow<br/>per-cycle child workflow]
    SLO["StrategyLabOrchestrator<br/>4-phase pipeline — see architecture.md §11<br/>and generation_pipeline.md"]
    Finalize[finalize_cycle_record_activity<br/>paper-trade + persist]
    PTA[PaperTradingAgent<br/>run_session — post-cycle via Finalize,<br/>or standalone via PaperTradingWorkflow]
    ORCH[InvestmentTeamOrchestrator]
    BatchWF[StrategyLabBatchWorkflow<br/>Temporal-only]
    EventBus[job_event_bus]
  end

  subgraph buckets[Persistent Buckets via _PersistentDict]
    B1[(investment_advisor_sessions)]
    B2[(investment_profiles)]
    B3[(investment_proposals)]
    B4[(investment_strategies)]
    B5[(investment_validations)]
    B6[(investment_backtests)]
    B7[(investment_strategy_lab_records)]
    B8[(investment_paper_trading_sessions)]
  end

  subgraph run_store[Run-State Store — direct JobServiceClient, bypasses _PersistentDict]
    B9[(investment_strategy_lab_runs)]
  end

  E1 --> FA --> B1
  E2 --> FA --> B1
  E3 --> B1
  E4 --> FA --> B2
  E5 --> B2
  E6 --> B2
  E7 --> B3
  E8 --> B3
  E9 --> PG --> B3
  E10 --> IC

  S1 --> B4
  S2 --> VA --> B5
  S3 --> RDB[_run_real_data_backtest<br/>fetches OHLCV, executes<br/>strategy_code via trading_service] --> B6
  S4 --> B6
  S5 --> ORCH --> PGate
  PGate -.reads.-> B2
  PGate -.reads.-> B4
  PGate -.reads.-> B5
  S6 --> ORCH
  S7 --> ORCH

  L1 --> BatchWF
  BatchWF --> SIE
  BatchWF --> CycleWF
  CycleWF --> SLO
  BatchWF -->|"after awaiting the wave's<br/>child workflows"| Finalize
  Finalize --> PTA
  Finalize --> B7
  BatchWF -->|"progress writes<br/>(persist_run_state_activity)"| B9
  BatchWF --> EventBus
  L2 --> B7
  L3 -->|"reconciles, then merges<br/>_active_runs + job rows"| B9
  L4 --> BatchWF
  L5 --> BatchWF
  L6 -->|"_active_runs first,<br/>job-service rows merged in"| B9
  L7 -->|"reconcile, then _active_runs;<br/>job service only as fallback"| B9
  L8 -->|"reconciles for the<br/>connect-time snapshot"| B9
  L8 --> EventBus
  L9 --> B7
  L10 --> B7
  L11 -->|"PaperTradingWorkflow →<br/>run_paper_trading_activity"| PTA --> B8
  L12 --> B8
  L13 --> B8
```

## Orchestrator — six queues

Defined in [`orchestrator.py`](../orchestrator.py):38-51 as
`WorkflowState.queues`. Each queue is a FIFO of `QueueItem(queue, payload_id,
priority)`:

| Queue | Purpose | Enqueue source |
|---|---|---|
| `research` | Strategy ideation / discovery work waiting for bandwidth | Ad hoc via `orchestrator.enqueue` |
| `portfolio_design` | Proposals being assembled against an IPS | Ad hoc |
| `validation` | Strategies awaiting `ValidationAgent` checks | Ad hoc |
| `promotion` | Validated strategies awaiting promotion decision | Ad hoc |
| `execution` | Accepted strategies awaiting execution routing | Ad hoc |
| `escalation` | Rejected / revised strategies needing human review | **Automatic**: any `PromotionDecision` with outcome `reject` or `revise` is enqueued here with `priority="high"` ([`orchestrator.py`](../orchestrator.py):113-117) |

`GET /workflow/queues` exposes the
current contents of every queue; `GET /workflow/status`
returns the current `WorkflowMode` and the audit log.

## Orchestrator — promotion gates

`PromotionGateAgent.decide` ([`agents.py`](../agents.py):131-302) runs the
six gates in strict order. Short-circuit semantics: any `reject` terminates
the checklist; missing validation forces `revise`; failure to unlock a live
precondition falls back to `paper`.

| # | Gate | Fails when | Outcome on failure |
|---|---|---|---|
| 1 | Separation of duties | `proposer_agent_id == approver.agent_id` | `reject` |
| 2 | Risk veto | `risk_veto == True` | `reject` |
| 3 | Validation completeness & pass criteria | Any required check missing or failed | `revise` |
| 4 | IPS live-trading permission | `ips.live_trading_enabled == False` | fall back to `paper` |
| 5 | Human live approval | `ips.human_approval_required_for_live and not human_live_approval` | fall back to `paper` |
| 6 | Promote to live | All gates pass | `live` (recorded as `human_approval = pass`; there is no separate gate member for this step) |

Every gate records a `GateCheckResult(gate, result, details)` in
`PromotionDecision.gate_results` and the decision carries an `AuditContext`
for traceability.

## Domain model — class diagram

Core Pydantic models from [`models.py`](../models.py). Only the
most important fields are shown; enums are in the lower block.

```mermaid
classDiagram
    class InvestmentProfile {
      +user_id: str
      +risk_tolerance: RiskTolerance
      +time_horizon_years: int
      +income: IncomeProfile
      +net_worth: NetWorth
      +savings: SavingsRate
      +tax: TaxProfile
      +liquidity: LiquidityNeeds
      +goals: List~UserGoal~
      +preferences: UserPreferences
      +constraints: PortfolioConstraints
    }
    class IPS {
      +profile: InvestmentProfile
      +live_trading_enabled: bool
      +human_approval_required_for_live: bool
      +speculative_sleeve_cap_pct: float
      +default_mode: WorkflowMode
    }
    class PortfolioProposal {
      +proposal_id: str
      +user_id: str
      +positions: List~PortfolioPosition~
      +asset_universe: AssetUniverse
      +audit: AuditContext
    }
    class PortfolioPosition {
      +symbol: str
      +weight_pct: float
      +asset_class: str
    }
    class StrategySpec {
      +strategy_id: str
      +authored_by: str
      +asset_class: str
      +hypothesis: str
      +signal_definition: str
      +timeframe: "1m|5m|15m|1h|1d"
      +entry_rules: List~EntryRule~
      +exit_rules: List~ExitRule~
      +sizing: SizingRule
      +target_symbols: List~str~
      +risk_limits: RiskLimits
      +speculative: bool
      +strategy_code?: str
      +requires_redesign: bool
      +requires_custom_code: bool
      +unparsed_rules: List~str~
      +expectancy_forecast?: ExpectancyForecast
      +audit: AuditContext
    }
    class ValidationReport {
      +strategy_id: str
      +checks: List~ValidationCheck~
      +summary: str
    }
    class BacktestConfig {
      +start_date: str
      +end_date: str
      +initial_capital: float
      +benchmark_symbol: str
      +rebalance_frequency: str
      +transaction_cost_bps: float
      +slippage_bps: float
      +risk_free_rate?: float
    }
    class BacktestResult {
      +total_return_pct: float
      +annualized_return_pct: float
      +volatility_pct: float
      +sharpe_ratio: float
      +max_drawdown_pct: float
      +win_rate_pct: float
      +profit_factor: float
    }
    class BacktestRecord {
      +backtest_id: str
      +strategy: StrategySpec
      +config: BacktestConfig
      +result: BacktestResult
      +trades: List~TradeRecord~
      +submitted_by: str
      +created_at: str
    }
    class StrategyLabRecord {
      +lab_record_id: str
      +strategy: StrategySpec
      +backtest: BacktestRecord
      +is_winning: bool
      +is_publishable: bool
      +publishability_skip_reason?: str
      +strategy_rationale: str
      +analysis_narrative: str
      +design_rounds: int
      +refinement_rounds: int
      +spec_implementability_phase_backs: int
      +critiques: List~dict~
      +quality_gate_results: List~dict~
      +signal_intelligence_brief?: dict
      +spec_history: List~SpecRevision~
      +code_history: List~CodeRevision~
      +gate_timeline: List~GateEvent~
      +rule_implementation_map: List~RuleImplementationMap~
      +loop_telemetry: dict
      +paper_trading_session_id?: str
      +paper_trading_status?: "skipped|completed|failed"
      +paper_trading_skipped_reason?: str
      +paper_trading_verdict?: PaperTradingVerdict
      +ran_on_non_conforming_code: bool
    }
    class SpecRevision {
      +phase: str
      +agent: str
      +timestamp: str
      +before_hash: str
      +after_hash: str
      +diff: str
      +reason: str
      +gate_failures: List~str~
    }
    class CodeRevision {
      +phase: str
      +agent: str
      +timestamp: str
      +before_hash: str
      +after_hash: str
      +diff: str
      +reason: str
      +gate_failures: List~str~
    }
    class GateEvent {
      +phase: str
      +gate_name: str
      +passed: bool
      +severity: "info|warning|critical"
      +details: str
      +timestamp: str
    }
    class RuleImplementationMap {
      +rule_id: str
      +code_line_refs: "List[List[int]]"
      +traded_count: int
    }
    class PromotionDecision {
      +strategy_id: str
      +outcome: PromotionStage
      +gate_results: List~GateCheckResult~
      +audit: AuditContext
    }
    class GateCheckResult {
      +gate: PromotionGate
      +result: GateResult
      +details: str
    }
    class AdvisorSession {
      +session_id: str
      +status: AdvisorSessionStatus
      +current_topic: AdvisorTopic
      +messages: List~ChatMessage~
      +collected: CollectedProfileData
    }
    class ChatMessage {
      +role: str
      +content: str
      +timestamp: str
    }
    class CollectedProfileData {
      +risk_tolerance?: RiskTolerance
      +time_horizon_years?: int
      +income?: IncomeProfile
      +net_worth?: NetWorth
      +...
    }
    class PaperTradingSession {
      +session_id: str
      +lab_record_id: str
      +strategy: StrategySpec
      +status: PaperTradingStatus
      +verdict?: PaperTradingVerdict
      +initial_capital: float
      +current_capital: float
      +trades: List~TradeRecord~
      +data_period_start: str
      +data_period_end: str
      +result?: BacktestResult
      +comparison?: PaperTradingComparison
    }
    class PaperTradingComparison {
      +backtest_sharpe: float
      +paper_sharpe: float
      +divergence_pct: float
      +analysis: str
    }
    class AuditContext {
      +data_snapshot_id: str
      +assumptions: List~str~
      +calc_artifacts: List~str~
      +gate_trace: List~str~
      +agent_versions: Dict~str, str~
    }

    InvestmentProfile --> IPS : wraps
    IPS --> PortfolioProposal : constrains
    PortfolioProposal o-- PortfolioPosition
    PortfolioProposal --> AuditContext
    StrategySpec --> ValidationReport : validated by
    StrategySpec --> BacktestConfig : run with
    BacktestConfig --> BacktestResult : produces
    BacktestResult --> BacktestRecord : wrapped in
    StrategySpec --> StrategyLabRecord : ideated into
    BacktestRecord --> StrategyLabRecord : wrapped in
    StrategySpec --> PromotionDecision : decided by
    ValidationReport --> PromotionDecision : input to
    IPS --> PromotionDecision : input to
    PromotionDecision o-- GateCheckResult
    PromotionDecision --> AuditContext
    AdvisorSession o-- ChatMessage
    AdvisorSession --> CollectedProfileData
    CollectedProfileData ..> InvestmentProfile : builds
    StrategySpec --> PaperTradingSession : simulated in
    PaperTradingSession --> PaperTradingComparison
    StrategyLabRecord o-- SpecRevision : spec_history
    StrategyLabRecord o-- CodeRevision : code_history
    StrategyLabRecord o-- GateEvent : gate_timeline
    StrategyLabRecord o-- RuleImplementationMap : rule_implementation_map
```

`BacktestResult` carries substantially more than the core metrics shown above
in the current model — walk-forward/deflated-Sharpe fields
(`deflated_sharpe`, `sharpe_ci_low/high`, `is_sharpe`, `oos_sharpe`,
`is_oos_degradation_pct`, `oos_trade_count`, `n_trials_when_accepted`,
`acceptance_reason`, `regime_results`, `fold_results`), a `coverage_report`
(zero/low-trade diagnostics from `coverage_probe/`), and
`execution_diagnostics`. These aren't drawn above to keep the diagram
readable; the full field list is in `models.py`:939-1003, and their role in
the verification/publication decision is documented in
[`generation_pipeline.md`](./generation_pipeline.md).

### Enums

| Enum | Values | Defined |
|---|---|---|
| `RiskTolerance` | `low`, `medium`, `high`, `very_high` | [`models.py`](../models.py):72 |
| `WorkflowMode` | `advisory`, `paper`, `live`, `monitor_only` | [`models.py`](../models.py):116 |
| `PromotionStage` | `reject`, `revise`, `paper`, `live` | [`models.py`](../models.py):103 |
| `PromotionGate` | `separation_of_duties`, `risk_veto`, `validation`, `ips_permission`, `human_approval` — five members; the sixth checklist step ("promote to live") records `human_approval = pass` rather than a gate of its own | [`models.py`](../models.py):123 |
| `GateResult` | `pass`, `fail`, `warn` | [`models.py`](../models.py):131 |
| `AdvisorTopic` | `greeting`, `risk_tolerance`, `time_horizon`, `income`, `net_worth`, `savings`, `tax`, `liquidity`, `goals`, `preferences`, `constraints`, `trading_preferences`, `review` (13) | [`models.py`](../models.py):85 |
| `AdvisorSessionStatus` | `active`, `completed`, `abandoned` | [`models.py`](../models.py):79 |
| `PaperTradingStatus` | `running`, `completed`, `failed` (legacy), plus live-mode `opening`, `warming_up`, `live` | [`models.py`](../models.py):1198 |
| `PaperTradingVerdict` | `ready_for_live`, `not_performant` | [`models.py`](../models.py):1210 |

`PaperTradingStatus` (above) and `StrategyLabRecord.paper_trading_status`
(in the class diagram) are **not** the same vocabulary despite the name and
two overlapping values — `PaperTradingStatus` is the runtime state of an
active `PaperTradingSession` (the legacy `running`/`completed`/`failed`
trio, plus `opening`/`warming_up`/`live` once live-streaming paper trading
starts), while
`StrategyLabRecord.paper_trading_status` is a plain `Optional[str]`
recording the *persisted cycle outcome* (`skipped`/`completed`/`failed`,
`None` for legacy rows) — a cycle that never started paper trading has no
`PaperTradingSession` and therefore no `running` state to report. They are
deliberately separate fields, not one enum reused inconsistently.

## Persistence strategy (recap)

Instead of owning a `shared.postgres` schema, the team pushes every artifact
through the `_PersistentDict` wrapper (`api/main.py` — search for `class
_PersistentDict`).
Reads and writes look like a normal Python dict but the backing store is the
Khala job service (`JobServiceClient`), which persists to the `khala_jobs`
Postgres database. The bucket names double as the job-service `team` field so
operators can clean up with SQL filters like
`WHERE team = 'investment_strategy_lab_records'` — see "Clearing strategy lab
data in Postgres directly" in [`../README.md`](../README.md).
