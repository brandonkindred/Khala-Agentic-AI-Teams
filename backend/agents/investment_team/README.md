# Investment Team

A multi-agent investment organization covering equities, bonds/Treasuries, options, real estate, FX, crypto, and optional commodities with IPS-first constraints.

## What this package implements

- **Core data model** for portfolio, strategy, validation, promotion, execution, and private-deal workflows.
- **Agent roles** with separation-of-duties and risk-veto mechanics.
- **Universal promotion checklist** that decides `reject | revise | paper | live`.
- **Orchestration state machine** with queueing, escalation, and safe degradation to `monitor_only` when data integrity fails.
- **Audit context fields** for snapshot IDs, assumptions, gate traces, and agent versions on key artifacts.

## Agent roles and interfaces

- **PolicyGuardianAgent**
  - Input: `IPS`, `PortfolioProposal`
  - Output: list of IPS violations
  - Invariant: IPS caps and exclusions are hard constraints.
  - Checks: position caps, aggregated asset-class caps, options/crypto permissions, speculative sleeve cap.

- **ValidationAgent**
  - Input: `ValidationReport`
  - Output: missing/failed checklist items
  - Invariant: required checks include backtest quality, walk-forward, stress, costs, and liquidity impact.

- **PromotionGateAgent**
  - Input: `StrategySpec`, `ValidationReport`, `IPS`, proposer/approver identities, risk veto flag, human live approval flag
  - Output: `PromotionDecision`
  - Invariants:
    - proposer cannot self-approve,
    - risk veto always rejects,
    - missing validation forces revise,
    - live promotion requires explicit IPS live enablement,
    - if IPS requires human approval for live, promotion remains paper until approved.

- **InvestmentCommitteeAgent**
  - Input: recommendation context and dissenting views
  - Output: `InvestmentCommitteeMemo`

## Universal promotion checklist

The gate runs these checks in order:
1. Separation of duties (reject on violation)
2. Risk veto (reject)
3. Required validation completeness and pass criteria (revise if incomplete/failing)
4. IPS live-trading permission (paper if not enabled)
5. Human live approval (paper if pending)
6. Promote to live only if all gates pass

Each decision records per-gate outcomes in `PromotionDecision.gate_results` and captures audit context.

## Orchestration and safety

`InvestmentTeamOrchestrator` manages queues:
- `research`
- `portfolio_design`
- `validation`
- `promotion`
- `execution`
- `escalation`

Safety defaults:
- Default workflow mode is controlled by `IPS.default_mode` and should typically be `monitor_only`.
- If data integrity fails, orchestrator degrades to `monitor_only` and logs the event.
- Reject/revise decisions auto-enqueue escalation.

## JSON schemas

- `schemas/investment_profile.schema.json` contains the implementation-ready schema for the user profile object.
- Remaining contract objects are represented as typed Pydantic models in `models.py`.

## Spec coverage additions

- `spec_models.py` now includes codex-friendly Pydantic representations for the full v1 spec entities, including `IPSV1`, `StrategySpecV1`, `ValidationReportV1`, `PromotionDecisionV1`, deal underwriting, diligence, and IC memo artifacts.
- `agent_catalog.py` defines the core cross-asset agent catalog and specialist desk lineup (equities, bonds/treasuries, options, crypto, FX, real estate) for orchestration and UI introspection.


## Backtesting workflow

The Investment Team API includes first-class backtesting endpoints so trading agents can submit strategy specs for evaluation and persist outcomes for future learning:

- `POST /strategies` creates a strategy specification.
- `POST /backtests` runs a deterministic backtest simulation for a strategy and records the result alongside configuration, timestamps, and submitter identity.
- `GET /backtests` returns recorded backtests (optionally filter with `?strategy_id=<id>`).

Stored `BacktestRecord` objects include strategy details, run configuration, and performance metrics (`total_return_pct`, `annualized_return_pct`, `volatility_pct`, `sharpe_ratio`, `max_drawdown_pct`, `win_rate_pct`, and `profit_factor`) so agents can compare what has been tried over time.
