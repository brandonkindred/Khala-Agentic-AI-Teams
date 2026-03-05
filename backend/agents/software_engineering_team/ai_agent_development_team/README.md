# AI Agent Development Team

Contract-first sub-orchestration for building AI agent systems from a spec.

This team mirrors the **backend_code_v2_team** lifecycle style (phase-based orchestration + domain tool agents) while targeting agent-system delivery artifacts instead of application code.

## Workflow phases

1. **Intake** — normalize mission goals, constraints, risks, and KPIs.
2. **Planning** — decompose into microtasks and assign specialist tool agents.
3. **Execution** — run tool agents to generate blueprint artifacts.
4. **Review** — enforce completeness and execution quality gates.
5. **Problem-solving** — apply deterministic remediation for failed gates.
6. **Deliver** — package final handoff summary and runbook notes.

## Tool agents

- `prompt_engineering` — system prompts, role prompts, handoff prompts.
- `memory_rag` — memory tiers, retrieval policy, context strategy.
- `safety_governance` — policy constraints, approval gates, risk controls.
- `evaluation_harness` — acceptance/adversarial tests and KPI instrumentation.
- `agent_runtime` — orchestration runtime wiring, retries, and observability hooks.
- `mcp_server_connectivity` — MCP server discovery, setup, auth/config wiring, and connectivity checks.

## Package layout

```
ai_agent_development_team/
├── orchestrator.py
├── models.py
├── prompts.py
├── phases/
│   ├── intake.py
│   ├── planning.py
│   ├── execution.py
│   ├── review.py
│   ├── deliver.py
│   └── problem_solving.py
└── tool_agents/
    ├── prompt_engineering/
    ├── memory_rag/
    ├── safety_governance/
    ├── evaluation_harness/
    ├── agent_runtime/
    └── mcp_server_connectivity/
```


## Tracking parity with backend/front-end v2

- Iterative review loop with bounded retries (`MAX_REVIEW_ITERATIONS`).
- Explicit `current_phase`, `iterations_used`, `needs_followup`, and `failure_reason` fields on workflow result.
- Per-microtask status tracking (`pending/in_progress/completed/failed`) with emitted files and notes.
- Workflow trace events per phase for job-stream/status integrations.
