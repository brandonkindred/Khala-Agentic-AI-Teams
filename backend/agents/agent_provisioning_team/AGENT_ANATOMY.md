# Standard anatomy of an AI agent (normative)

This document is the **contract** for how the Agent Provisioning team defines, scaffolds, and delivers **AI agents** in this repository. Every new agent **must** implement the structure summarized below and reflected in the design diagrams under `design_assets/`.

## Design references (source of truth visuals)

| Diagram | File |
|--------|------|
| High-level: Input, Agent, Tools, Memory, Prompt, Security Guardrails, Subagents | [`design_assets/Agent-Architecture-High-Level.png`](design_assets/Agent-Architecture-High-Level.png) |
| Detailed: tools taxonomy, memory tiers, prompt roles, guardrails, subagents | [`design_assets/Agent-Architecture-Detailed.png`](design_assets/Agent-Architecture-Detailed.png) |
| Coordinator pattern: central Agent with Input/Output and a Subagents pool | [`design_assets/Agent with sub agents.png`](design_assets/Agent%20with%20sub%20agents.png) |
| Recursive chaining: nested sub-agents with repeated INPUT/OUTPUT between levels | [`design_assets/Agent-subagent-chaining.png`](design_assets/Agent-subagent-chaining.png) |

If code and these diagrams disagree, **update the code** or **update the diagrams** in lockstep—never ship an agent that only partially matches this anatomy.

---

## 1. External boundary: Input and Output

Every agent exposes a clear **Input** (requests, tasks, context from upstream) and **Output** (results, artifacts, messages downstream). Orchestrators, APIs, and parent agents depend on this boundary.

- **Input**: typed or documented request model (e.g. Pydantic), validation at the edge, explicit error surfaces.
- **Output**: typed or documented response model; stable enough for composition and testing.

Nested agents use the **same** input/output contract pattern at each level (see §6).

---

## 2. Core: Agent (orchestrator)

The **Agent** is the coordinator: it plans work, delegates to subagents and tools, aggregates results, and enforces policy. Business logic for “what to do next” lives here unless it is intentionally isolated in a subagent.

---

## 3. Tools

Agents **must** declare which external capabilities they use. Group as in the detailed diagram:

- **Standalone tools** (examples: Git, GitHub, Figma, databases, HTTP APIs)—implemented as explicit clients, adapters, or tool functions with clear interfaces.
- **Browsers** (e.g. Chrome, Firefox, Brave) when automation applies—wrapped in a single browser abstraction where possible; credentials and sessions follow platform security rules.

No hidden side channels: if the agent can affect the outside world, it goes through a named tool path.

---

## 4. Memory (file-backed or equivalent)

Memory is structured and tiered:

| Tier | Intent (per diagrams) |
|------|------------------------|
| **Short-term** | Same-day / session context (e.g. “daily notes for today”, working set) |
| **Mid-term** | Rolling window (e.g. ~24 hours to ~7 days) when the detailed model applies |
| **Long-term** | Durable summaries or reviews (e.g. “review of all daily notes”), retrievable across sessions |

Implementation may use a filesystem layout, a database, or object store; the **conceptual** split must exist so prompts do not rely on an unbounded single blob.

---

## 5. Prompts (message roles)

LLM-driven agents **must** separate:

- **System**: identity, constraints, writing/brand rules, tool-use policy.
- **User**: the human or upstream task payload.
- **Assistant**: prior model turns in multi-turn flows.

Avoid stuffing everything into a single undifferentiated string unless the runtime only supports that—then document the equivalent split in code comments.

---

## 6. Security guardrails

Every agent **must** define enforceable guardrails:

- Input validation and output checks where feasible.
- Alignment with the unified security gateway / team policies when exposed via HTTP.
- No credential logging; secrets via env or secure stores only.

The diagram shows an explicit **Security Guardrails** box—implement as middleware, validators, or post-conditions, not only as prompt text.

---

## 7. Subagents

When work is decomposed, use **Subagents** (Agent 1 … Agent N):

- Each subagent is itself an agent that **conforms to this same anatomy** (recursive).
- Parent ↔ child communication uses explicit **INPUT/OUTPUT** at each layer (chaining diagram).
- The parent Agent remains accountable for ordering, failure handling, and merging results.

---

## 8. Compliance checklist (before merge / release)

Use this for PR review of any new or substantially refactored agent:

- [ ] **Input / Output** models (or equivalent) documented and stable.
- [ ] **Tools** enumerated; no undeclared external effects.
- [ ] **Memory** strategy documented (short / mid / long as applicable).
- [ ] **Prompts** use System / User / Assistant (or documented equivalent).
- [ ] **Security guardrails** implemented beyond prompt-only instructions.
- [ ] **Subagents** (if any) each satisfy this checklist recursively.
- [ ] **Diagrams** in `design_assets/` updated if the canonical structure changes.

---

## Relationship to this Python package

The `agent_provisioning_team` package implements **infrastructure provisioning** (containers, tool accounts, manifests). New **application agents** (LLM agents, orchestrators) added elsewhere in `backend/agents/` should still follow this anatomy so provisioning, documentation, and runtime behavior stay aligned.
