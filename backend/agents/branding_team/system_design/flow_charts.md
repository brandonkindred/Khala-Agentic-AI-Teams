# Branding Team — Flow Charts

This document collects the operational flow and sequence diagrams for every
runtime path through the team. Each diagram is accompanied by a short
narrative and citations into the source code.

## 1. 5-phase orchestrator pipeline with phase gates

This is the core flow executed by `BrandingTeamOrchestrator.run()`
(`orchestrator.py:147-356`). Each phase is guarded by a dependency check
on the previous phase's output, and the final `WorkflowStatus` depends on
both `current_phase` and `human_review.approved`.

```mermaid
flowchart TB
    Start([run mission, human_review, brand_checks])
    Start --> ResolveStop["stop_idx = target_phase or last phase<br/>(orchestrator.py:184)"]
    ResolveStop --> P1["Phase 1 — Strategic Core<br/>strategic_core_agent.execute(mission)<br/>(orchestrator.py:187)"]
    P1 --> Legacy["Legacy bridge agents (always run)<br/>codifier · moodboard · refinement ·<br/>guidelines · wiki · compliance<br/>(orchestrator.py:196-203)"]
    Legacy --> G1{"stop_idx >= 1?"}
    G1 -- no --> Finalize
    G1 -- yes --> P2["Phase 2 — Narrative & Messaging<br/>narrative_agent.execute(mission, strategic_core)<br/>(orchestrator.py:210)"]
    P2 --> G2{"stop_idx >= 2 AND narrative?"}
    G2 -- no --> Finalize
    G2 -- yes --> P3["Phase 3 — Visual Identity<br/>visual_identity_agent.execute(<br/>mission, strategic_core, narrative)<br/>(orchestrator.py:216)"]
    P3 --> G3{"stop_idx >= 3 AND visual_identity?"}
    G3 -- no --> Finalize
    G3 -- yes --> P4["Phase 4 — Channel Activation<br/>channel_activation_agent.execute(...)<br/>(orchestrator.py:222)"]
    P4 --> G4{"stop_idx >= 4 AND channel_activation?"}
    G4 -- no --> Finalize
    G4 -- yes --> P5["Phase 5 — Governance & Evolution<br/>governance_agent.execute(mission, strategic_core)<br/>(orchestrator.py:233)"]
    P5 --> MarkComplete["current_phase = COMPLETE<br/>if human_review.approved<br/>(orchestrator.py:235-236)"]
    MarkComplete --> Finalize

    Finalize["Integrations: market research + design assets<br/>(orchestrator.py:273-286)"]
    Finalize --> Book["_build_brand_book(...)<br/>(orchestrator.py:288-298)"]
    Book --> Gates["_build_phase_gates(current_phase, approved)<br/>(orchestrator.py:300)"]
    Gates --> Status{"approved AND<br/>current_phase == COMPLETE?"}
    Status -- yes --> Ready["status = READY_FOR_ROLLOUT"]
    Status -- no --> NeedsHuman["status = NEEDS_HUMAN_DECISION"]
    Ready --> Persist
    NeedsHuman --> Persist
    Persist["store.append_brand_version(...)<br/>if store + brand_id<br/>(orchestrator.py:353-354)"]
    Persist --> Out([TeamOutput])
```

## 2. Synchronous `POST /run`

Used when the caller already has all the information needed and wants a
single response. `run_branding_team` (`api/main.py:685-708`).

```mermaid
sequenceDiagram
    autonumber
    participant Client
    participant API as FastAPI /run
    participant Orch as BrandingTeamOrchestrator
    participant P1 as StrategicCoreAgent
    participant P2 as NarrativeMessagingAgent
    participant P3 as VisualIdentityAgent
    participant P4 as ChannelActivationAgent
    participant P5 as GovernanceAgent
    participant Store as BrandingStore

    Client->>API: POST /run (RunBrandingTeamRequest)
    API->>API: Build BrandingMission + HumanReview
    API->>Orch: run(mission, human_review, ..., target_phase)
    Orch->>P1: execute(mission)
    P1-->>Orch: StrategicCoreOutput
    Orch->>P2: execute(mission, strategic_core)
    P2-->>Orch: NarrativeMessagingOutput
    Orch->>P3: execute(mission, strategic_core, narrative)
    P3-->>Orch: VisualIdentityOutput
    Orch->>P4: execute(mission, strategic_core, narrative, visual_identity)
    P4-->>Orch: ChannelActivationOutput
    Orch->>P5: execute(mission, strategic_core)
    P5-->>Orch: GovernanceOutput
    Orch->>Orch: _build_brand_book(...)
    alt client_id and brand_id in request
        Orch->>Store: append_brand_version(client_id, brand_id, output)
    end
    Orch-->>API: TeamOutput
    API-->>Client: 200 OK TeamOutput
```

## 3. Interactive session Q&A loop

Used when the caller has a partial brief and wants the team to ask
focused questions. Creates a `BrandingSession`, extracts open questions
from missing mission fields, and re-runs the orchestrator after each
answer.

```mermaid
flowchart LR
    Start([POST /sessions<br/>RunBrandingTeamRequest])
    Start --> Build[Build BrandingMission]
    Build --> Run1["orchestrator.run(<br/>approved=false)<br/>(api/main.py:729)"]
    Run1 --> Questions["_build_open_questions(mission)<br/>(api/main.py:377-405)"]
    Questions --> Create["session_store.create(mission, output)<br/>(api/main.py:295-306)"]
    Create --> Feed[["Session feed<br/>GET /sessions/{id}/questions<br/>(api/main.py:747)"]]

    Feed --> Answer["POST /sessions/{id}/questions/{qid}/answer<br/>(api/main.py:755)"]
    Answer --> Apply["_apply_answer(mission, question, answer)<br/>(api/main.py:426-437)"]
    Apply --> Mark["question.status = answered"]
    Mark --> OpenCheck{"open questions<br/>remaining?"}
    OpenCheck -- yes --> ReRun["orchestrator.run(<br/>approved=false)<br/>(api/main.py:782)"]
    OpenCheck -- no --> ReRunApproved["orchestrator.run(<br/>approved=true)<br/>(api/main.py:778-782)"]
    ReRun --> Save
    ReRunApproved --> Save
    Save["session_store.save(session_id, session)<br/>(api/main.py:783)"]
    Save --> Feed
    Save --> Done([Return BrandingSessionResponse])
```

## 4. Conversational chat flow

Used when the caller has no structured brief and wants the assistant to
extract a mission from free-form messages. Each turn parses the LLM's
structured output, merges mission fields, and optionally reruns the
orchestrator.

```mermaid
sequenceDiagram
    autonumber
    participant User
    participant API as /conversations/.../messages
    participant ConvStore as BrandingConversationStore
    participant Assistant as BrandingAssistantAgent
    participant LLM as llm_service
    participant Orch as BrandingTeamOrchestrator
    participant BrandStore as BrandingStore

    User->>API: POST message
    API->>ConvStore: get(conversation_id)
    ConvStore-->>API: messages + mission + latest_output
    API->>ConvStore: append_message(user, content)
    API->>Assistant: respond(messages, mission, user_message)
    Assistant->>LLM: complete(prompt, temperature=0.5,<br/>system_prompt=SYSTEM_PROMPT, think=true)
    LLM-->>Assistant: raw completion
    Assistant->>Assistant: _parse_mission_and_suggestions(raw)<br/>(assistant/agent.py:14-66)
    Assistant->>Assistant: _merge_mission_update(current, update)<br/>(assistant/agent.py:69-123)
    Assistant-->>API: reply, updated_mission, suggested_questions
    API->>ConvStore: update_mission(conversation_id, updated_mission)
    API->>ConvStore: append_message(assistant, reply)
    API->>API: _run_orchestrator_if_ready(updated_mission)<br/>(api/main.py:360-367)
    alt mission has minimal required fields
        API->>Orch: run(mission, HumanReview(approved=false))
        Orch-->>API: TeamOutput
        API->>ConvStore: update_output(conversation_id, output)
    end
    alt no brand yet and company_name present
        API->>BrandStore: create_brand(default_client, mission)<br/>(api/main.py:924-937)
        API->>ConvStore: set_brand(conversation_id, brand_id)
        API->>BrandStore: append_brand_version(...)
    end
    API-->>User: ConversationStateResponse
```

On LLM failure the assistant returns a canned reply and suggested
questions without propagating the exception
(`assistant/agent.py:188-195`), so chat remains responsive even when the
LLM backend is down.

## 5. Phase-gated approval workflow

Used when stakeholders approve phases one at a time. Each call advances
the `target_phase` and the orchestrator replays prior phases
deterministically before executing the new one.

```mermaid
flowchart TB
    S1([POST .../run/strategic_core<br/>approved=false])
    S1 --> R1["orchestrator.run(target_phase=STRATEGIC_CORE)<br/>(api/main.py:617-639)"]
    R1 --> Gates1["Gates:<br/>P1 PENDING_REVIEW<br/>P2-P5 NOT_STARTED"]
    Gates1 --> Review1{Stakeholder approves?}
    Review1 -- revise --> S1
    Review1 -- yes --> S2([POST .../run/strategic_core<br/>approved=true])
    S2 --> Gates2["Gates:<br/>P1 APPROVED"]
    Gates2 --> S3([POST .../run/narrative_messaging<br/>approved=true])
    S3 --> R3["orchestrator.run(target_phase=NARRATIVE_MESSAGING)"]
    R3 --> Gates3["Gates:<br/>P1 APPROVED<br/>P2 APPROVED<br/>P3-P5 NOT_STARTED"]
    Gates3 --> S4([POST .../run/visual_identity])
    S4 --> S5([POST .../run/channel_activation])
    S5 --> S6([POST .../run/governance<br/>approved=true])
    S6 --> R6["orchestrator.run(target_phase=GOVERNANCE)"]
    R6 --> Final["current_phase = COMPLETE<br/>(orchestrator.py:235-236)"]
    Final --> Rollout[[status = READY_FOR_ROLLOUT]]
```

The key insight is that `orchestrator.run` is deterministic: replaying
prior phases is cheap because they do not call LLMs in the current
implementation. Stakeholders can therefore progress one phase at a
time without the orchestrator losing state between calls.

## 6. Multi-brand agency lifecycle

This is the persistence / CRUD view of the agency model. A `Client` owns
many `Brand` entities, each of which accumulates versions over its
lifetime.

```mermaid
stateDiagram-v2
    [*] --> ClientCreated: POST /clients
    ClientCreated --> BrandDraft: POST /clients/{id}/brands
    BrandDraft --> BrandActive: first successful run (status via PUT)
    BrandActive --> BrandActive: POST .../run<br/>(version++ each call)
    BrandActive --> BrandEvolving: PUT status=evolving
    BrandEvolving --> BrandActive: PUT status=active
    BrandActive --> BrandArchived: PUT status=archived
    BrandEvolving --> BrandArchived: PUT status=archived
    BrandArchived --> [*]
```

Each run appends a new `BrandVersionSummary` to `Brand.history` via
`append_brand_version` (`store.py:218-252`). The new version carries the
`WorkflowStatus` from that run so the history entry records *how* each
version ended.

## 7. Market research adapter call

Triggered either implicitly (`include_market_research=true` on a run) or
explicitly
(`POST /clients/{id}/brands/{brand_id}/request-market-research`).

```mermaid
sequenceDiagram
    autonumber
    participant Caller
    participant API as Branding API
    participant Orch as BrandingTeamOrchestrator
    participant Adapter as adapters/market_research.py
    participant MR as Market Research Team API

    alt Explicit request
        Caller->>API: POST .../request-market-research
        API->>Adapter: request_market_research(brand.mission)
    else Included in run
        Caller->>API: POST .../run (include_market_research=true)
        API->>Orch: run(..., include_market_research=true)
        Orch->>Adapter: request_market_research(mission)<br/>(orchestrator.py:273-280)
    end

    Adapter->>Adapter: base = UNIFIED_API_BASE_URL or<br/>BRANDING_MARKET_RESEARCH_URL
    alt base is unset
        Adapter-->>API: None
    else base is set
        Adapter->>MR: POST /api/market-research/market-research/run<br/>(timeout 120s)
        MR-->>Adapter: Market Research TeamOutput JSON
        Adapter->>Adapter: _map_to_competitive_snapshot(data)<br/>(adapters/market_research.py:53-74)
        Adapter-->>API: CompetitiveSnapshot
    end

    alt Explicit request path
        alt success
            API-->>Caller: 200 CompetitiveSnapshot
        else adapter raised
            API-->>Caller: 503 Market research service unavailable
        end
    else Inside run
        API-->>Caller: TeamOutput with<br/>competitive_snapshot populated (or None on failure)
    end
```

Failure is deliberately asymmetric: the direct endpoint surfaces a 503
(`api/main.py:659-662`), while the run path tolerates failure silently
and continues (`orchestrator.py:278-280`).

## 8. Design asset adapter call

Today the design adapter is a stub: it never makes an outbound call and
always returns a placeholder `DesignAssetRequestResult` with
`status="pending"` (`adapters/design_assets.py:30-37`). The flow is still
wired through the same paths as the real integration will use.

```mermaid
sequenceDiagram
    autonumber
    participant Caller
    participant API as Branding API
    participant Orch as BrandingTeamOrchestrator
    participant Adapter as adapters/design_assets.py

    alt Explicit request
        Caller->>API: POST .../request-design-assets
        API->>Orch: orchestrator.codifier.codify(mission)
        Orch-->>API: BrandCodification
        API->>Adapter: request_design_assets(codification, brand_name)
    else Included in run
        Caller->>API: POST .../run (include_design_assets=true)
        API->>Orch: run(..., include_design_assets=true)
        Orch->>Adapter: request_design_assets(codification, company_name)<br/>(orchestrator.py:282-286)
    end

    Adapter->>Adapter: base = BRANDING_DESIGN_SERVICE_URL or<br/>UNIFIED_API_BASE_URL
    Note over Adapter: Currently a stub — no outbound POST.<br/>Planned: POST to design service when contract defined.
    Adapter-->>API: DesignAssetRequestResult(status=pending, ...)
    API-->>Caller: Result returned directly or via TeamOutput.design_asset_result
```

## 9. Temporal workflow wrapping

When `TEMPORAL_ADDRESS` is set, `temporal/__init__.py:37-40` registers
`BrandingWorkflow` on the `"branding-queue"` task queue via
`shared_temporal.start_team_worker`. This provides durable execution for
long-running brand builds.

```mermaid
sequenceDiagram
    autonumber
    participant Client
    participant TClient as Temporal Client
    participant Worker as Temporal Worker<br/>("branding-queue")
    participant Workflow as BrandingWorkflow
    participant Activity as run_pipeline_activity
    participant Orch as BrandingTeamOrchestrator

    Client->>TClient: Start BrandingWorkflow(request dict)
    TClient->>Worker: Schedule workflow
    Worker->>Workflow: run(request)
    Workflow->>Activity: execute_activity(<br/>run_pipeline_activity,<br/>start_to_close_timeout=2h)
    Activity->>Orch: BrandingTeamOrchestrator().run(...)<br/>(temporal/__init__.py:11-20)
    Orch-->>Activity: TeamOutput
    Activity-->>Workflow: result.model_dump()
    Workflow-->>Worker: dict
    Worker-->>TClient: Workflow completed
    TClient-->>Client: result
```

The 2-hour `start_to_close_timeout` is generous enough that any plausible
brand run — including slow sibling team calls — stays well within the
budget. Durable execution means a worker crash mid-run does not lose
state: Temporal reschedules the activity.
