# Investment Team — System Design Documentation

This folder captures the **architectural and design decisions** of the Khala
investment team. It is the diagram-first companion to the two existing docs:

- [`../README.md`](../README.md) — operational overview (tracks, endpoints,
  catalog roles, promotion checklist)
- [`../ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md) — critique and
  phased migration roadmap (known issues, not current design)

The investment team exposes a single HTTP prefix `/api/investment` that hides
**two distinct tracks**:

| Track | Purpose | Needs `user_id` / IPS? |
|---|---|---|
| **Advisor / IPS** | Build an Investment Policy Statement from a user conversation, create and validate portfolio proposals, draft Investment Committee memos | Yes |
| **Strategy Lab** | LLM-driven strategy ideation, historical backtesting, and paper-trading against a free-tier market snapshot | No |

The **universal promotion gate** (`POST /promotions/decide`) is the bridge
between the two: a validated Strategy Lab strategy can be promoted to paper or
live under a specific client only after the six-gate checklist runs against
that client's IPS.

## Reading order

1. **[`architecture.md`](./architecture.md)** — C4-style container view of the
   team and its dependencies. Start here.
2. **[`system_design.md`](./system_design.md)** — component view of the API
   router and class diagram of the core Pydantic domain models.
3. **[`use_cases.md`](./use_cases.md)** — UML-style use-case diagram grouping
   every endpoint by actor and track.
4. **[`flow_charts.md`](./flow_charts.md)** — sequence and state diagrams for
   the four most important end-to-end flows (advisor session, strategy lab
   batch, promotion gate, orchestrator safety).

### Feature-specific design docs

The per-feature design docs below supplement the diagram-first overview above
with implementation detail for individual Strategy Lab capabilities:

- **[`strategy_lab_pipeline.md`](./strategy_lab_pipeline.md)** — full per-cycle
  pipeline shape (`ideating → fetching_data → analyzing → paper_trading? →
  complete`), the complete list of SSE phase events, the winner gate, and the
  paper-trade skip/failure paths.
- **[`paper_trading_integration.md`](./paper_trading_integration.md)** —
  paper trading as an integrated cycle step: winner gate, request-level
  configuration, failure contract, and linkage between `StrategyLabRecord` and
  `PaperTradingSession`.
- **[`trade_record_schema.md`](./trade_record_schema.md)** — every
  `TradeRecord` field including the execution-detail additions
  (`entry_bid_price` / `entry_fill_price` / `exit_bid_price` /
  `exit_fill_price` / `entry_order_type` / `exit_order_type`) used for
  post-hoc analysis of realized vs modeled slippage.

## Where things live

| Concept | Source |
|---|---|
| Two-track API, all 30+ endpoints | [`api/main.py`](../api/main.py) (2063 lines) |
| Core agents (Advisor, PolicyGuardian, ValidationAgent, PromotionGate, InvestmentCommittee) | [`agents.py`](../agents.py) (933 lines) |
| Strategy Lab agents (SignalIntelligenceExpert, StrategyIdeationAgent, BacktestingAgent, PaperTradingAgent, TradeSimulationEngine) | [`agents/`](../agents/) |
| Orchestrator state machine, 6 queues, promotion entry point | [`orchestrator.py`](../orchestrator.py) (133 lines) |
| Pydantic domain models (profile, IPS, strategy, backtest, promotion, advisor, paper trading) | [`models.py`](../models.py) (477 lines) |
| Strategy Lab background worker + per-cycle loop | [`api/main.py`](../api/main.py) `_strategy_lab_worker`, `_run_one_strategy_lab_cycle` |
| SSE fan-out for run progress | [`api/job_event_bus.py`](../api/job_event_bus.py) |
| Persistence wrapper over Khala job service | [`api/main.py`](../api/main.py) `_PersistentDict` (line 85) |
| Multi-provider OHLCV fetcher | [`market_data_service.py`](../market_data_service.py) |
| Strategy Lab market snapshot (Frankfurter / FRED / CoinGecko) | [`market_lab_data/free_tier.py`](../market_lab_data/free_tier.py) |
| QuantConnect / TradingView browser automation | [`tool_agents/web_interfaces/coordinator.py`](../tool_agents/web_interfaces/coordinator.py) |
| Catalog of core + specialist desk agents | [`agent_catalog.py`](../agent_catalog.py) |
| Unified-API mount point | [`backend/unified_api/config.py`](../../../unified_api/config.py) |

## Diagram conventions

All diagrams are authored in **Mermaid**, matching the pattern already used in
[`../README.md`](../README.md). They render natively on GitHub, in VS Code with
the Mermaid extension, and in [mermaid.live](https://mermaid.live).

- **flowchart** — containers, components, decision trees
- **sequenceDiagram** — request / response over time
- **classDiagram** — domain model relationships
- **stateDiagram-v2** — workflow mode transitions
