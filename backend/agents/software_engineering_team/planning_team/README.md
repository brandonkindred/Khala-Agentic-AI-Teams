# Planning Team

Planning agents produce artifacts in the `plan/` folder. They are grouped into two sub-phases:

## Discovery (intake)

Runs before the Tech Lead / Architecture loop. Outputs are consumed by design agents.

| Agent | Role |
|-------|------|
| **Spec Intake** | Validates spec, produces REQ-IDs, glossary, assumptions |
| **Project Planning** | Features/functionality doc from spec; feeds Tech Lead and Architecture |

## Design

Everything that produces plans and task assignments and writes to `plan/`.

| Agent / group | Role |
|---------------|------|
| **Architecture Expert** | System architecture (lives at `../architect-agents/architecture_expert/`; consumes Project Planning output) |
| **Tech Lead** | Task breakdown, execution order, alignment with architecture (lives at `../tech_lead_agent/`) |
| **Domain planning agents** | API Contract, Data Architecture, UI/UX, Frontend Architecture, Infrastructure, DevOps Planning, QA Test Strategy, Security Planning, Observability, Performance Doc |
| **Planning graph agents** (Tech Lead internal) | Backend, Frontend, Data, Test, Performance, Documentation, Quality Gate planning |
| **Planning consolidation** | Master plan, risk register, ship checklist |
