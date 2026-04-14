# Agentic team architecture (normative)

This document is the **contract** for the structure of every agentic team created or refined through the Agentic Team Provisioning service. The Process Designer assistant and all code paths that save teams/processes **must** produce artifacts that match this architecture.

## Design references

| Diagram | File |
|---------|------|
| API-layer interactions (Actor → UI → API → Agentic Team → File System / Database) | [`design_assets/Agentic-Team-API-Interactions.png`](design_assets/Agentic-Team-API-Interactions.png) |
| Internal architecture (Orchestrator Agent, Agents pool, Processes pool, Job/Question Tracking) | [`design_assets/Agentic-Team-Architecture.png`](design_assets/Agentic-Team-Architecture.png) |

---

## 1. API layer

The API layer is the only external surface. It exposes five interaction categories to the Actor (human or upstream system) through a UI or direct HTTP:

| Category | Direction | Purpose |
|----------|-----------|---------|
| **User Requests / Chat** | Actor → Team | Start conversations, send messages, submit process designs |
| **Questions for User** | Team → Actor | Pending questions that need human input before work continues |
| **Job Status** | Team ↔ Actor | Create/poll/list jobs and their progress |
| **Assets** | Actor ↔ File System | Upload/download files for the team (stored on disk) |
| **Form Information** | Actor ↔ Database | Structured data records per form key (stored in Khala Postgres, partitioned by team_id) |

The API layer does **not** contain business logic; it delegates to the Orchestrator Agent.

---

## 2. Orchestrator Agent

The Orchestrator Agent is the central coordinator inside every agentic team. It:

- Receives user requests and chat messages from the API layer.
- Manages **Job Tracking** — creating, updating, and completing jobs.
- Manages **Question Tracking** — surfacing questions for the user and processing answers.
- Delegates work to **Agents** and executes **Processes**.

The orchestrator is the **single point of control** for the team. No agent or process runs without the orchestrator's knowledge.

---

## 3. Roster / Agents pool (Agent 1 … Agent N)

Each agentic team maintains a **Roster** — a named pool of agents. The roster is the single source of truth for who is on the team and what they bring. Each agent declares:

| Field | Purpose |
|-------|---------|
| **agent_name** | Stable, unique within the team; used for provisioning and step assignment |
| **role** | Primary role on the team |
| **skills** | Specific skills (e.g. "data analysis", "copywriting") |
| **capabilities** | Functional capabilities (e.g. "code generation", "web search") |
| **tools** | Tools or integrations the agent can use (e.g. "Git", "Slack API") |
| **expertise** | Domain expertise areas (e.g. "customer support", "HIPAA compliance") |

### Roster validation

The roster is validated automatically to ensure the team is **fully staffed**. Validation checks:

1. **Unrostered agents** — every agent referenced in a process step must exist on the roster.
2. **Unused agents** — every rostered agent should be assigned to at least one process step.
3. **Unstaffed steps** — every process step must have at least one assigned agent.
4. **Incomplete profiles** — agents missing skills, capabilities, tools, or expertise are flagged so coverage cannot be assumed.

A team is considered fully staffed only when all checks pass. The validation endpoint is `GET /teams/{team_id}/roster/validation`.

Agents are provisioned through the Agent Provisioning team (sandboxed environments per the canonical agent anatomy: Input/Output, Tools, Memory, Prompts, Security Guardrails, Subagents). The Orchestrator Agent assigns work to these named agents.

---

## 4. Processes pool (Process 1 … Process N)

Each team defines one or more **Processes**. A process is a workflow with:

- **Trigger** — what starts the process (message, event, schedule, manual).
- **Steps** — ordered units of work, each assigned to one or more agents from the team's agent pool.
- **Output** — the deliverable and its destination when the process completes.

Processes reference agents **by name** — every agent mentioned in a step must exist in the team's agents pool.

---

## 5. Infrastructure backing

| Resource | Backing | Purpose |
|----------|---------|---------|
| **File System** | `$AGENT_CACHE/provisioned_teams/{team_id}/assets/` | Team assets (uploaded files, generated artifacts) |
| **Database** | Shared Khala Postgres `agentic_form_data` table (partitioned by `team_id`) | Form data records |
| **Job Service** | `JobServiceClient(team="provisioned_{team_id}")` | Job lifecycle tracking |

---

## 6. Compliance checklist

Before a team definition is considered valid:

- [ ] **Orchestrator Agent** role is implied (the service itself acts as orchestrator).
- [ ] **Roster** is explicitly defined — every agent has a name, role, skills, capabilities, tools, and expertise.
- [ ] **Roster validation** passes (`GET /teams/{team_id}/roster/validation` returns `is_fully_staffed: true`).
- [ ] **Processes** reference only agents that exist on the roster.
- [ ] **Each process** has a trigger, at least one step, and an output.
- [ ] **Infrastructure** (assets dir, database, job client) is provisioned for the team.
- [ ] Each agent in the pool is provisioned via Agent Provisioning team (canonical agent anatomy).
- [ ] API surface exposes all five categories: User Requests, Questions, Job Status, Assets, Forms.

---

## Relationship to individual agent anatomy

Each agent in the agents pool follows the canonical structure from `agent_provisioning_team/AGENT_ANATOMY.md` (Input/Output, Tools, Memory tiers, Prompt roles, Security Guardrails, Subagents). The agentic team architecture describes how those agents are **composed** into a team with an orchestrator, processes, and shared infrastructure.
