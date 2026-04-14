# Flow Charts — Investment Team

Four diagrams covering the most important end-to-end paths through the
investment team:

1. [Advisor session → IPS](#1-advisor-session--ips-flow) (sequence diagram)
2. [Strategy Lab batch run](#2-strategy-lab-batch-run-flow) (sequence diagram)
3. [Promotion-gate decision tree](#3-promotion-gate-decision-tree) (flowchart)
4. [Orchestrator workflow mode](#4-orchestrator-workflow-mode) (state diagram)

Line references point to [`api/main.py`](../api/main.py),
[`agents.py`](../agents.py), and [`orchestrator.py`](../orchestrator.py).

---

## 1. Advisor session → IPS flow

Conversational flow that walks a user through topics to accumulate a
`CollectedProfileData` object, converts it to an `InvestmentProfile`, and
wraps it in an `IPS`.

```mermaid
sequenceDiagram
    autonumber
    participant UI as Angular UI
    participant API as api/main.py
    participant FA as FinancialAdvisorAgent
    participant LLM as LLM Service
    participant Store as _PersistentDict

    UI->>API: POST /advisor/sessions (L1981)
    API->>FA: start_session() (agents.py:433)
    FA->>FA: create AdvisorSession<br/>topic = GREETING
    FA-->>API: session + opening question
    API->>Store: _advisor_sessions[id] = session
    API-->>UI: StartAdvisorSessionResponse

    loop While session.status == active
        UI->>API: POST /advisor/sessions/{id}/messages (L1997)
        API->>Store: load session
        API->>FA: handle_message(session, user_msg) (agents.py:449)
        FA->>FA: _extract_topic_data(current_topic, msg)<br/>(regex-heavy, agents.py:615-881)
        alt extraction succeeded
            FA->>FA: append to collected, advance topic<br/>(_next_topic, agents.py:408)
        else needs clarification
            FA->>LLM: optional clarification prompt
            LLM-->>FA: follow-up question
        end
        FA-->>API: updated session + next question
        API->>Store: _advisor_sessions[id] = session
        API-->>UI: SendAdvisorMessageResponse
    end

    UI->>API: POST /advisor/sessions/{id}/complete (L2033)
    API->>FA: build_ips(session) (agents.py:509)
    FA->>FA: CollectedProfileData → InvestmentProfile → IPS
    FA-->>API: IPS
    API->>Store: _profiles[user_id] = ips
    API-->>UI: CompleteAdvisorSessionResponse
```

**Key notes**

- Regex extraction (≈266 lines in `agents.py`:615-881) is deliberately local /
  deterministic so profile data never leaves the process when building the
  IPS. This is flagged as HIGH-4 in
  [`../ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md) — a future local-LLM
  extractor is planned.
- Topic order is a strict DAG driven by `_next_topic`
  ([`agents.py`](../agents.py):408): `GREETING → RISK → HORIZON → INCOME →
  NET_WORTH → SAVINGS → TAX → LIQUIDITY → GOALS → PREFERENCES → CONSTRAINTS →
  REVIEW`.
- `build_ips` sets `IPS.default_mode` from collected preferences; typically
  `WorkflowMode.MONITOR_ONLY` so promotion is always opt-in.

---

## 2. Strategy Lab batch run flow

The long-running flow kicked off by `POST /strategy-lab/run`. The API returns
immediately with a `run_id`; the worker thread runs the per-cycle loop and
publishes SSE events that the UI subscribes to.

```mermaid
sequenceDiagram
    autonumber
    participant Client
    participant API as api/main.py
    participant Worker as _strategy_lab_worker<br/>(daemon thread)
    participant MLDP as FreeTierMarketDataProvider
    participant SIE as SignalIntelligenceExpert
    participant SIA as StrategyIdeationAgent
    participant BTA as BacktestingAgent
    participant MDS as MarketDataService
    participant LLM as LLM Service
    participant Store as _PersistentDict
    participant Bus as job_event_bus

    Client->>API: POST /strategy-lab/run (L1251)
    API->>API: allocate run_id, init state
    API->>Store: _active_runs[run_id] persisted
    API->>Worker: spawn thread(_strategy_lab_worker, L1083)
    API-->>Client: StrategyLabRunStartResponse (run_id)

    opt Real-time subscription
        Client->>API: GET /strategy-lab/runs/{run_id}/stream (L1550)
        API->>Bus: subscribe(run_id)
    end

    Worker->>MLDP: fetch_context()<br/>(Frankfurter + FRED + CoinGecko)
    MLDP-->>Worker: MarketLabContext
    Worker->>Store: load prior StrategyLabRecords

    loop for each cycle in batch
        alt Signal expert enabled
            Worker->>SIE: produce_signal_brief(priors, context)
            SIE->>LLM: complete_json(temp=0.58)
            LLM-->>SIE: SignalIntelligenceBriefV1
            SIE-->>Worker: brief
        end

        Worker->>SIA: ideate_strategy(brief, priors)
        SIA->>LLM: complete_json(temp=0.85)
        LLM-->>SIA: StrategySpec (creative)
        SIA-->>Worker: strategy

        Worker->>BTA: run_backtest(strategy, config)
        BTA->>MDS: fetch OHLCV per symbol
        MDS-->>BTA: bars
        loop per qualifying bar
            BTA->>LLM: complete_json(temp=0.2)
            LLM-->>BTA: enter/exit/hold decision
        end
        BTA-->>Worker: BacktestResult + trades

        Worker->>SIA: _analyze_strategy(win/lose)
        SIA->>LLM: complete_json(temp=0.35)
        LLM-->>SIA: draft narrative
        Worker->>SIA: _self_review_analysis(draft)
        SIA->>LLM: complete_json(temp=0.15)
        LLM-->>SIA: validated narrative
        SIA-->>Worker: narrative

        Worker->>Store: persist StrategyLabRecord
        Worker->>Bus: publish(run_id, cycle_complete event)
        Bus-->>Client: SSE event
    end

    Worker->>Store: mark run complete
    Worker->>Bus: publish(run_id, run_complete)
    Bus-->>Client: SSE event (close)
```

**Key notes**

- The per-bar LLM call inside `BacktestingAgent.run_backtest` is expensive —
  it's the CRITICAL-1 issue in
  [`../ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md). The long-term plan
  is rule compilation + batched Tier-2 evaluation.
- Worker state lives in both an in-memory `_active_runs` dict **and** the
  `_PersistentDict` bucket so restarts can reload in-flight runs via
  `_load_run_from_job_service`.
- `STRATEGY_LAB_SIGNAL_EXPERT_ENABLED` toggles the signal-expert step off for
  A/B comparison or cost control.
- Polling clients can use `GET /strategy-lab/runs/{run_id}/status` (L1534)
  instead of SSE.

---

## 3. Promotion-gate decision tree

Six-gate checklist from `PromotionGateAgent.decide`
([`agents.py`](../agents.py):131-302). Each gate either short-circuits to a
terminal outcome (`reject`), falls through to a softer outcome (`revise` /
`paper`), or continues to the next gate. Rejects and revises auto-enqueue to
the `escalation` queue in
[`orchestrator.py`](../orchestrator.py):113-117.

```mermaid
flowchart TD
    START([POST /promotions/decide<br/>L719]) --> G1{Gate 1<br/>Separation of duties<br/>proposer_id ≠ approver.agent_id?}
    G1 -- No --> REJ1[outcome = reject<br/>gate: separation_of_duties = fail]
    G1 -- Yes --> G2{Gate 2<br/>Risk veto?}
    G2 -- Yes --> REJ2[outcome = reject<br/>gate: risk_veto = fail]
    G2 -- No --> G3{Gate 3<br/>ValidationReport<br/>complete &<br/>all required passed?}
    G3 -- No --> REV[outcome = revise<br/>gate: validation = fail]
    G3 -- Yes --> G4{Gate 4<br/>IPS.live_trading_enabled?}
    G4 -- No --> PAP1[outcome = paper<br/>gate: ips_live = warn]
    G4 -- Yes --> G5{Gate 5<br/>IPS.human_approval_required_for_live<br/>&amp; human_live_approval?}
    G5 -- approval pending --> PAP2[outcome = paper<br/>gate: human_approval = warn]
    G5 -- approval granted<br/>or not required --> LIVE[outcome = live<br/>gate: live_promote = pass]

    REJ1 --> ESC[[Enqueue to escalation queue<br/>priority=high<br/>orchestrator.py L113-117]]
    REJ2 --> ESC
    REV --> ESC
    PAP1 --> AUD[(Record PromotionDecision<br/>+ AuditContext)]
    PAP2 --> AUD
    LIVE --> AUD
    ESC --> AUD
```

**Key notes**

- Every gate writes a `GateCheckResult(gate, result, details)` to
  `PromotionDecision.gate_results`, so the full trace survives in persistence
  (`investment_strategies` bucket) for audit.
- `PromotionDecision.audit: AuditContext` captures the snapshot ID,
  assumptions, and agent version that produced the decision.
- The gate is the **only** path between a validated strategy and paper/live
  execution — all tracks funnel through here.

---

## 4. Orchestrator workflow mode

`WorkflowMode` is the orchestrator's safety throttle. The diagram below
distinguishes **what is live today** (solid transitions implemented in
`api/main.py`) from **what is designed but not yet wired** (dashed
transitions defined on the orchestrator but only exercised by tests).

```mermaid
stateDiagram-v2
    [*] --> monitor_only : WorkflowState() default<br/>(orchestrator.py:50)<br/>instantiated at api/main.py:78

    monitor_only --> monitor_only : GET /workflow/status<br/>(read-only)

    monitor_only --> paper : designed: operator raises mode<br/>(not wired to any endpoint)
    paper --> live : designed: operator raises mode<br/>+ passed 6-gate promotion<br/>+ ips.live_trading_enabled
    live --> paper : designed: operator lowers mode
    paper --> monitor_only : designed: operator lowers mode
    paper --> monitor_only : designed: handle_data_integrity(False)<br/>(orchestrator.py:77-80, tests only)
    live --> monitor_only : designed: handle_data_integrity(False)<br/>(orchestrator.py:77-80, tests only)

    note right of monitor_only
        Today: the process always
        stays here. No code path
        mutates _workflow_state.mode.
    end note
    note right of live
        Requires IPS permission,
        human approval, and risk
        officer sign-off by design —
        no endpoint exposes the
        transition yet.
    end note
```

**Key notes**

- **Current behavior.** `_workflow_state = WorkflowState()` is created once
  at module import ([`api/main.py`](../api/main.py):78) and the dataclass
  default pins `mode = WorkflowMode.MONITOR_ONLY`
  ([`orchestrator.py`](../orchestrator.py):50). `GET /workflow/status`
  ([`api/main.py`](../api/main.py):756) and `GET /workflow/queues`
  ([`api/main.py`](../api/main.py):767) are the only endpoints that touch
  it, and both are read-only.
- **What is defined but not called.** `InvestmentTeamOrchestrator.bootstrap`
  ([`orchestrator.py`](../orchestrator.py):69-71) (which would copy
  `ips.default_mode` into the state) and `handle_data_integrity`
  ([`orchestrator.py`](../orchestrator.py):77-80) (which would degrade the
  mode and write `data_integrity_failed:degrade_to_monitor_only` to the
  audit log) are only invoked from
  [`tests/test_investment_team.py`](../tests/test_investment_team.py). No
  production path calls them, and there is no FastAPI `lifespan` wiring them
  to startup.
- **Why the dashed transitions still appear.** They document the orchestrator
  contract that downstream code is expected to adopt in Phase 0 of the
  migration roadmap in
  [`../ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md). Once wiring lands,
  the dashed edges become solid and the safe-degrade invariant is enforced at
  runtime rather than only in tests.
