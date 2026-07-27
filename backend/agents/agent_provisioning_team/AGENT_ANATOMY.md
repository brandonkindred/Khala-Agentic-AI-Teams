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

### 3.0 LLM tools discovery (`agent_llm_tools_service`)

Before choosing which LLM function calls to enable, agents can discover what is available:

- **In-process:** `LlmToolsService().list_tools()`, `get_tool(tool_id)`, `list_operations(tool_id)`, and **`get_documentation(tool_id)`** from **`agent_llm_tools_service`**.
- **HTTP (unified API):** `GET /api/llm-tools/`, `GET /api/llm-tools/{tool_id}`, `GET /api/llm-tools/{tool_id}/operations`, and **`GET /api/llm-tools/{tool_id}/documentation`** (metadata only; no remote execution).

`get_tool` embeds the same documentation object as the dedicated documentation route. The service provides **links** to official manuals (e.g. git-scm.com) and optional **`man` command hints** for agents running in a shell; it does **not** vend or proxy full man-page text.

The first registered tool is **Git** (`tool_id=git`). Use summaries, operation schemas, and documentation links to decide whether Git (or a future tool) fits the task, then wire execution as in §3.1.

### 3.1 Shared Git tools (LLM function calling)

For any agent that operates on a **known local Git repository** (orchestrator-resolved `repo_path`), use the shared package **`agent_git_tools`**:

- **`GIT_TOOL_DEFINITIONS`**: OpenAI-compatible tool specs (`git_status`, `git_diff`, `git_log`, `git_checkout_branch`, `git_create_feature_branch`, `git_write_files_and_commit`, `git_commit_working_tree`, `git_merge_branch`).
- **`GitToolContext`**: host-injected `repo_path`, `default_base_branch`, and policy flags (e.g. `allow_merge_to_default_branch=False` during implement phases so merge stays orchestrator-gated).
- **`build_git_tool_handlers(ctx)`**: returns a name → handler map for **`llm_service.tool_loop.complete_json_with_tool_loop`**, which runs multi-turn chat until the model returns final structured JSON.

The executor delegates to **`software_engineering_team.shared.git_utils`** so subprocess Git behavior stays consistent. Models must not supply `repo_path` in tool arguments; the runtime ignores it.

**Rollout:** repo-backed agents can import the shared Git definitions, convert them with `agent_llm_tools_service.strands_bridge.build_strands_tools`, and bind a `GitToolContext` for their workspace.

### 3.2 Agent Cognition Core (batteries-included)

Newly generated agents are stamped with a default `cognition` manifest block (`agent_registry.CognitionSpec`) so the tools layer, memory, and guardrails of the **Agent Cognition Core** are wired by default — no per-agent boilerplate. Note that an agent's **roster tools** (free-text labels like "Git" or "Slack API") are deliberately **not** mapped into `cognition.tools`: those ids resolve against the cognition tool registries (`LlmToolsService` + a caller-supplied integration registry + `agent_git_tools`), so `cognition.tools` ships empty and is widened explicitly when a real, resolvable tool is bound.

---

## 4. Memory (file-backed or equivalent)

Memory is structured and tiered:

| Tier | Intent (per diagrams) |
|------|------------------------|
| **Short-term** | Same-day / session context (e.g. “daily notes for today”, working set) |
| **Mid-term** | Rolling window (e.g. ~24 hours to ~7 days) when the detailed model applies |
| **Long-term** | Durable summaries or reviews (e.g. “review of all daily notes”), retrievable across sessions |

Implementation may use a filesystem layout, a database, or object store; the **conceptual** split must exist so prompts do not rely on an unbounded single blob.

Generated agents realize these tiers through the **Agent Cognition Core** without per-agent wiring: episodic events (pruned per `cognition.memory.retention_days_events`, default 90 days), calendar rollup summaries (day/week/month/year), and a default-on Neo4j/Graphiti knowledge graph. Each invoke returns a `CognitionWriteback` carrying at least one episodic `MemoryEvent`, and the runtime receives a compact `memory_digest` on the side channel (see §6.1) to render into its prompt.

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

### 6.1 Cognition guardrails & the side-channel contract

Generated agents inherit day-one guardrails from the **Agent Cognition Core**: the `default_guardrails` seed pack is stamped via `cognition.rule_packs` and installs on first invoke. Steering rides a dedicated **side channel**, never the agent's input contract:

- **Inbound:** the invoke proxy wraps the request in a marker envelope (DESIGN §10); the sandbox shim opens a context channel the runtime reads via `agent_cognition.tools.channel.get_cognition_context()` → `{rules, memory_digest}`. The runtime renders **only advisory** rules + the memory digest into its system prompt. **Enforced** rules are gated deterministically by the proxy/shim, **never** by prompt text.
- **Outbound:** the runtime returns a `CognitionWriteback` (`events`, `tool_calls`, `truncated`) that the proxy persists.

This is the generator wiring of DESIGN §12 Step 14: every generated agent gets the core and consumes it.

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
