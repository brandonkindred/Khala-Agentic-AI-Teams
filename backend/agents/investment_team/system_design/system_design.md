# System Design — Investment Team

Component view of the API router, detail on the orchestrator queues and
promotion gates, and a class diagram of the core Pydantic domain models.

For the container-level view of how this team fits into Khala, see
[`architecture.md`](./architecture.md).

## Component diagram — API router

How each endpoint reaches an agent or orchestrator call and which persistence
bucket it reads/writes. Every line number below is from
[`api/main.py`](../api/main.py).

```mermaid
flowchart LR
  subgraph advisor_api[Advisor Endpoints]
    E1["POST /advisor/sessions<br/>L1981"]
    E2["POST /advisor/sessions/{id}/messages<br/>L1997"]
    E3["GET /advisor/sessions/{id}<br/>L2022"]
    E4["POST /advisor/sessions/{id}/complete<br/>L2033"]
    E5["POST /profiles<br/>L395"]
    E6["GET /profiles/{user_id}<br/>L474"]
    E7["POST /proposals/create<br/>L484"]
    E8["GET /proposals/{id}<br/>L524"]
    E9["POST /proposals/{id}/validate<br/>L534"]
    E10["POST /memos<br/>L783"]
  end

  subgraph shared_api[Shared Endpoints]
    S1["POST /strategies<br/>L557"]
    S2["POST /strategies/{id}/validate<br/>L581"]
    S3["POST /backtests<br/>L654"]
    S4["GET /backtests<br/>L704"]
    S5["POST /promotions/decide<br/>L719"]
    S6["GET /workflow/status<br/>L756"]
    S7["GET /workflow/queues<br/>L767"]
    S8["GET /health<br/>L389"]
  end

  subgraph lab_api[Strategy Lab Endpoints]
    L1["POST /strategy-lab/run<br/>L1251"]
    L2["GET /strategy-lab/results<br/>L1294"]
    L3["GET /strategy-lab/jobs<br/>L1340"]
    L4["POST /strategy-lab/runs/{id}/resume<br/>L1402"]
    L5["POST /strategy-lab/runs/{id}/restart<br/>L1467"]
    L6["GET /strategy-lab/runs<br/>L1526"]
    L7["GET /strategy-lab/runs/{id}/status<br/>L1534"]
    L8["GET /strategy-lab/runs/{id}/stream<br/>L1550"]
    L9["DELETE /strategy-lab/records/{id}<br/>L1692"]
    L10["DELETE /strategy-lab/storage<br/>L1732"]
    L11["POST /strategy-lab/paper-trade<br/>L1792"]
    L12["GET /strategy-lab/paper-trade/results<br/>L1901"]
    L13["GET /strategy-lab/paper-trade/{id}<br/>L1929"]
  end

  subgraph handlers[Agents & Orchestrator]
    FA[FinancialAdvisorAgent]
    PG[PolicyGuardianAgent]
    VA[ValidationAgent]
    PGate[PromotionGateAgent]
    IC[InvestmentCommitteeAgent]
    SIE[SignalIntelligenceExpert]
    SIA[StrategyIdeationAgent]
    BTA[BacktestingAgent]
    PTA[PaperTradingAgent]
    ORCH[InvestmentTeamOrchestrator]
    Worker[_strategy_lab_worker]
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
  S3 --> BTA --> B6
  S4 --> B6
  S5 --> ORCH --> PGate
  PGate -.reads.-> B2
  PGate -.reads.-> B4
  PGate -.reads.-> B5
  S6 --> ORCH
  S7 --> ORCH

  L1 --> Worker
  Worker --> SIE
  Worker --> SIA
  Worker --> BTA
  Worker --> B7
  Worker --> EventBus
  L2 --> B7
  L3 --> B7
  L4 --> Worker
  L5 --> Worker
  L6 --> Worker
  L7 --> Worker
  L8 --> EventBus
  L9 --> B7
  L10 --> B7
  L11 --> PTA --> B8
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

`GET /workflow/queues` ([`api/main.py`](../api/main.py):767) exposes the
current contents of every queue; `GET /workflow/status` ([`api/main.py`](../api/main.py):756)
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
| 6 | Promote to live | All gates pass | `live` |

Every gate records a `GateCheckResult(gate, result, details)` in
`PromotionDecision.gate_results` and the decision carries an `AuditContext`
for traceability.

## Domain model — class diagram

Core Pydantic models from [`models.py`](../models.py) (477 lines). Only the
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
      +asset_class: str
      +hypothesis: str
      +signal_definition: str
      +entry_rules: str
      +exit_rules: str
      +sizing_rules: str
      +risk_limits: dict
      +speculative: bool
    }
    class ValidationReport {
      +strategy_id: str
      +checks: List~ValidationCheck~
      +summary: str
    }
    class BacktestConfig {
      +start_date: date
      +end_date: date
      +initial_capital: float
      +benchmark: str
      +rebalance: str
      +costs_bps: float
      +slippage_bps: float
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
      +narrative: str
      +signal_brief: dict
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
      +strategy_id: str
      +status: PaperTradingStatus
      +verdict: PaperTradingVerdict
      +start_date: date
      +end_date: date
      +result: BacktestResult
      +comparison: PaperTradingComparison
    }
    class PaperTradingComparison {
      +backtest_sharpe: float
      +paper_sharpe: float
      +divergence_pct: float
      +analysis: str
    }
    class AuditContext {
      +snapshot_id: str
      +assumptions: List~str~
      +agent_version: str
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
```

### Enums

| Enum | Values | Defined |
|---|---|---|
| `RiskTolerance` | `conservative`, `moderate_conservative`, `moderate`, `moderate_aggressive`, `aggressive` | [`models.py`](../models.py):11 |
| `WorkflowMode` | `monitor_only`, `paper`, `live` | [`models.py`](../models.py):55 |
| `PromotionStage` | `reject`, `revise`, `paper`, `live` | [`models.py`](../models.py):42 |
| `PromotionGate` | `separation_of_duties`, `risk_veto`, `validation`, `ips_live`, `human_approval`, `live_promote` | [`models.py`](../models.py):62 |
| `GateResult` | `pass`, `fail`, `warn` | [`models.py`](../models.py):70 |
| `AdvisorTopic` | `greeting`, `risk`, `horizon`, `income`, `net_worth`, `savings`, `tax`, `liquidity`, `goals`, `preferences`, `constraints`, `review` | [`models.py`](../models.py):24 |
| `AdvisorSessionStatus` | `active`, `completed`, `abandoned` | [`models.py`](../models.py):18 |
| `PaperTradingStatus` | `running`, `completed`, `failed` | [`models.py`](../models.py):367 |
| `PaperTradingVerdict` | `ready_for_live`, `not_performant`, `requires_review` | [`models.py`](../models.py):373 |

## Persistence strategy (recap)

Instead of owning a `shared_postgres` schema, the team pushes every artifact
through the `_PersistentDict` wrapper ([`api/main.py`](../api/main.py):85-132).
Reads and writes look like a normal Python dict but the backing store is the
Khala job service (`JobServiceClient`), which persists to the `khala_jobs`
Postgres database. The bucket names double as the job-service `team` field so
operators can clean up with SQL filters like
`WHERE team = 'investment_strategy_lab_records'` — see
[`../README.md`](../README.md):77-86.
