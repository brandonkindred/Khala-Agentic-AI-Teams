# Applying AWS Strands Swarm & Graph Patterns to the Software Engineering Team

## Context

The Khala SE team (`backend/agents/software_engineering_team/`) coordinates ~15 specialized agents across a 4-phase pipeline: Product Analysis → Planning V3 → Execution → Integration. Most internal coordination is hand-rolled: `threading.Lock`-guarded shared dicts for parallel workers, `while` loops for review gates, and sequential `.run()` calls even where comments say "parallel". There is no DAG framework and no Strands usage today. A class literally named `CodingTeamSwarm` is a hand-rolled 500-round while loop that reimplements swarm primitives.

The request: review the SE team and identify the highest-value places where Strands `Graph` (deterministic DAG, parallel fan-out, conditional edges) or Strands `Swarm` (autonomous handoffs via `handoff_to_agent`, shared context, built-in safety) would measurably improve outcomes — speed, quality, robustness, or clarity.

What Strands gives us that we lack today:
- **Graph**: `GraphBuilder.add_node / add_edge(condition=fn)`, parallel fan-out with join, bounded cycles, typed state between nodes.
- **Swarm**: `Swarm(agents=[...], entry_point=..., max_handoffs=N)`, auto-injected `handoff_to_agent(agent_name, message, context)` tool, shared `SharedContext`, built-in loop/repetitive-handoff detection and timeout.

## Scope

This is a **review and recommendation document**, not a full implementation. The deliverable is the ranked list of candidate areas below. Each item is independently adoptable and can be implemented/merged on its own branch.

## Recommended Candidates (ranked by impact-per-effort)

### 1. Backend & Frontend Code V2 review gates → `Graph`  *(quick win, highest ROI)*

**Files**
- `backend/agents/software_engineering_team/backend_code_v2_team/phases/execution.py:384-641` — `run_execution_with_review_gates()`
- `backend/agents/software_engineering_team/frontend_code_v2_team/phases/execution.py` — identical structure

**Current state.** A single `while total_cycles < max_total_cycles` loop runs Code Review → QA → Security strictly sequentially. On QA failure (`:570`) and Security failure (`:638`) it calls `run_batch_coding_fixes(...)` and `continue`s, which **re-enters Code Review** on now-mutated files. `max_total_cycles = code_review_retries + qa_retries + security_retries = 9` by default.

**Problem.** Every QA-induced patch forces a redundant Code Review pass; every Security-induced patch forces both Code Review and QA to re-run. The retry budget is consumed by redundant re-runs instead of real fix attempts, and `max_total_cycles` is hit more often than it should be.

**Recommendation: Strands `Graph`.** The control flow is a fixed stage-wise DAG driven by typed `ReviewResult.passed` flags — exactly Graph's sweet spot. Swarm is wrong here because routing is deterministic, not reasoning-based.

**Structure.**
- Nodes: `code_review`, `cr_fix`, `qa`, `qa_fix`, `security`, `sec_fix`, `documentation`, `done`, `failed`
- Edges:
  - `code_review —passed→ qa` / `code_review —failed→ cr_fix → code_review` (cap: `code_review_max_retries`)
  - `qa —passed→ security` / `qa —failed→ qa_fix → qa` (**NOT** back to code_review)
  - `security —passed→ documentation → done` / `security —failed→ sec_fix → security` (**NOT** back to code_review)
- Conditions read `GraphState` fields populated by each reviewer node.

**Expected outcome.** Eliminates redundant re-reviews after QA/Security fixes: ~30–50% fewer LLM calls on failing microtasks and fewer spurious "max cycles exceeded" failures. Existing phase helper functions (`run_code_review_phase`, `run_batch_coding_fixes`) wrap cleanly as graph nodes — no new agents required. Backend change is the template; frontend is a mirror.

---

### 2. DevOps team Phase 4 validation & reviews → `Graph` with parallel fan-out

**File**
- `backend/agents/software_engineering_team/devops_team/orchestrator.py:403-523`

**Current state.** Comment at `:404` says "phase 4 - validation and review" but actually runs IaC validation (`:405`), policy check (`:406`), CI/CD lint (`:407`), deploy dry-run (`:408-410`), then `_run_execution_tools()` (`:421`), then DevSecOps review (`:502`), then Change review (`:509`), then test validation (`:513`) **all sequentially**. None depend on each other. Phase 4.6 debug-patch loop at `:436-499` is bounded `MAX_INFRA_FIX_ITERATIONS = 3`.

**Problem.** 6–8 independent network-bound calls run in series, blocking the main thread. This is the single biggest latency sink in the DevOps pipeline.

**Recommendation: Strands `Graph`** — parallel fan-out + join is the canonical use case. Swarm is inappropriate — the reviewer set is fixed and routing is trivial (always run all of them).

**Structure.**
- Entry: `phase4_fanout`
- Parallel branches: `iac_validation_tool`, `policy_tool`, `cicd_lint_tool`, `deploy_dry_run_tool`, `execution_tools` (internally a sub-graph by tool type: terraform, cdk, compose, helm), `devsecops_review_agent`, `change_review_agent`
- Join: `quality_gate_aggregator` → conditional edge to `debug_patch_subgraph` (bounded cycle: `infra_debug_agent → infra_patch_agent → re-run execution_tools`, max 3) or to `test_validation_agent → done`.

**Expected outcome.** 50–70% wall-clock latency reduction on Phase 4. Also gives Phase 4 explicit per-node observability (currently buried in comments). The existing debug-patch loop maps onto a bounded Graph cycle.

---

### 3. DevOps team Phase 2 change design → `Graph` (tiny 3-way fan-out)  *(quickest win)*

**File**
- `backend/agents/software_engineering_team/devops_team/orchestrator.py:369-395`

**Current state.** Three design agents called strictly sequentially at `:374-378`:
```
iac_result    = self.iac_agent.run(...)
cicd_result   = self.cicd_agent.run(...)
deploy_result = self.deployment_agent.run(...)
```
Then merged into `aggregated_artifacts` (`:380-383`). No cross-dependencies between the three.

**Problem.** Each blocks the next despite producing disjoint artifact files.

**Recommendation: Strands `Graph`** — trivial 3-way fan-out + join.

**Structure.**
- `repo_navigator_tool` → fan-out to `iac_agent`, `cicd_agent`, `deployment_agent` in parallel → join at `artifacts_aggregator` → `write_agent_output`.

**Expected outcome.** Phase 2 latency ≈ 3× faster (becomes `max(iac, cicd, deploy)` instead of sum). **The cheapest change on this list** — can ship in the same PR as candidate #2 as a single devops Graph.

---

### 4. Integration conflict resolution → `Swarm`  *(new capability, highest quality uplift)*

**Files**
- `backend/agents/software_engineering_team/integration_team/agent.py:15-92` — single `IntegrationAgent`
- `backend/agents/software_engineering_team/orchestrator.py:2898-2971` — integration invocation

**Current state.** `IntegrationAgent` makes one LLM call, returns `IntegrationIssue` records plus `fix_task_suggestions`. There is **no resolver** — the suggestions list is produced by the detection prompt and nothing consumes it. Detection-only.

**Problem.** A critical contract mismatch either fails the run or produces advisory text. The decision "fix backend vs fix frontend vs update the contract" genuinely depends on reasoning over context (which side is authoritative? which is simpler? what does the spec say?) — it's not a fixed flowchart.

**Recommendation: Strands `Swarm`.** Handoffs are reasoning-driven, the agent set is small, and `SharedContext` is ideal for carrying issues + backend/frontend snippets + spec excerpt across handoffs. A Graph would force us to encode a decision tree this domain does not have.

**Structure.**
- Entry: `integration_triage` (wraps existing `IntegrationAgent` for detection + routing)
- Specialists:
  - `contract_arbiter` — reads spec; decides authoritative side
  - `backend_fix_agent` — proposes backend patch to match contract
  - `frontend_fix_agent` — proposes frontend patch to match contract
  - `contract_update_agent` — proposes spec/openapi update
  - `integration_verifier` — re-runs detection on patched snippets; hands back to arbiter if issues remain
- `Swarm(agents=[...], entry_point=integration_triage, max_handoffs=8)`

**Expected outcome.** Integration phase becomes **actionable** instead of advisory — self-healing contracts. Closes a concrete gap in the current pipeline. Larger refactor (needs new agent classes), but arguably the largest *quality* improvement per unit of scope.

---

### 5. `CodingTeamSwarm` → real Strands `Swarm`  *(eliminates tech debt, lowest priority)*

**Files**
- `backend/agents/coding_team/orchestrator.py:167` — `CodingTeamSwarm` class
- `backend/agents/coding_team/orchestrator.py:334` — `run()`, `max_rounds=500`
- `backend/agents/coding_team/orchestrator.py:34` — `MAX_TASK_REVISIONS = 3`

**Current state.** A class named `CodingTeamSwarm` is a hand-rolled `while` loop that sequences Assign → Implement+Quality Gates → Review+Merge, capped at 500 rounds. It reimplements concepts Strands `Swarm` already provides: inter-agent shared state, handoff limits, loop detection, iteration caps.

**Problem.** Reinventing swarm primitives means reinventing bugs. The 500-round cap is a symptom — a real Swarm ships with `max_handoffs`, `max_iterations`, repetitive-handoff detection and timeout. Mixing control flow into business logic also makes dynamic routing (e.g., "skip review for trivial changes") harder to add.

**Recommendation: Strands `Swarm`.** The agent set already matches the Swarm mental model: Tech Lead (assigner), Implementer, Quality Gates, Reviewer — each legitimately decides who acts next based on state. `handoff_to_agent` replaces the custom dispatcher. `MAX_TASK_REVISIONS` becomes a per-microtask key in `SharedContext` gated by a reviewer precondition.

**Structure.**
- `Swarm(agents=[tech_lead_assigner, implementer, quality_gate_runner, reviewer_merger], entry_point=tech_lead_assigner, max_handoffs=50, max_iterations=500)`
- `SharedContext` keys: `pending_tasks`, `current_task_id`, `revision_count`, `latest_diff`, `gate_results`

**Expected outcome.** Not primarily a speed win — **robustness and clarity**. Eliminates hand-rolled loop-detection, gets free Strands swarm telemetry, opens the door to true dynamic routing. Also fixes the misleading class name (today `CodingTeamSwarm` is neither a swarm nor uses Strands). Larger refactor; ship last.

---

## Ranking

| # | Candidate | Pattern | Effort | Impact |
|---|---|---|---|---|
| 1 | Backend/Frontend V2 review gates | Graph | Low | High — 30–50% fewer LLM calls on failing microtasks |
| 2 | DevOps Phase 4 validation & reviews | Graph | Medium | High — 50–70% Phase 4 latency reduction |
| 3 | DevOps Phase 2 change design | Graph | Very low | Medium — ~3× faster Phase 2 |
| 4 | Integration conflict resolution | Swarm | Medium-high | High — net-new self-healing capability |
| 5 | `CodingTeamSwarm` → Strands Swarm | Swarm | High | Medium — robustness, observability, tech debt |

Quick wins: **#1 + #3** (a few hours each). Largest single latency improvement: **#2**. Largest quality improvement: **#4**. Structural tech debt: **#5**.

## When to pick Graph vs Swarm (decision heuristic)

Use **Graph** when all hold:
1. Next-step targets after each node are a small known set chosen by a typed value (`passed`, `failure_class`, `severity`) — not free-form reasoning.
2. The topology is authored once and reviewed like a flowchart; debugging is "which edge fired?".
3. You want parallel fan-out with join and deterministic output aggregation.
4. Cycles are bounded counters an engineer can point at, not emergent from agent choice.

Use **Swarm** when at least one holds:
1. "Who acts next" depends on reading shared state and reasoning about it.
2. Possible handoff targets are large/open-ended; encoding as edges would duplicate domain logic.
3. Agents escalate back and forth with a peer until a condition is met.
4. Termination is best expressed as "no agent chose to hand off".

Applied: #1 #2 #3 are flowcharts with typed outcomes → **Graph**. #4 is authoritative-side reasoning over shared context → **Swarm**. #5 sits on the boundary — a Swarm because we want the reviewer to itself decide "needs another revision" rather than bounce through a central dispatcher.

## Critical files to be modified (if proceeding with implementation)

- `backend/agents/software_engineering_team/backend_code_v2_team/phases/execution.py` (#1)
- `backend/agents/software_engineering_team/frontend_code_v2_team/phases/execution.py` (#1)
- `backend/agents/software_engineering_team/devops_team/orchestrator.py` (#2, #3)
- `backend/agents/software_engineering_team/integration_team/agent.py` + siblings (#4 — also needs new resolver agent files)
- `backend/agents/coding_team/orchestrator.py` (#5)
- `backend/requirements.txt` — add `strands-agents` dependency

## Existing helpers/utilities to reuse

- `run_code_review_phase`, `run_batch_coding_fixes`, `run_qa_phase`, `run_security_phase` in `backend_code_v2_team/phases/` — wrap as Graph nodes for #1 without rewriting them.
- `IaCAgent`, `CICDPipelineAgent`, `DeploymentStrategyAgent`, validation tools, `InfraDebugAgent`, `InfraPatchAgent` in `devops_team/` — already encapsulated; drop into Graph nodes for #2 and #3 unchanged.
- `IntegrationAgent` for #4 — reused as the triage/verifier node; only the resolver specialists are new.
- `TechLeadAgent`, `SeniorSWEAgent`, `TaskGraphService` in `coding_team/` — reused as Swarm participants for #5; the hand-rolled while loop is deleted.

## Verification

For each candidate that gets implemented:

1. **Unit**: Existing sub-team pytest suites must pass unchanged (`cd backend && make test`). The Graph/Swarm wrappers must not change the external contracts of the sub-team lead (`run_workflow()` signatures, `DevOpsTeamResult`, `IntegrationOutput`).
2. **Characterization**: Pick a representative task the existing pipeline handles, capture the current sequence of LLM calls (count, ordering, tokens) via the existing LLM service logs, then re-run under the Strands pattern and compare. Expect:
   - #1: fewer total LLM calls on failing microtasks; same results on passing ones.
   - #2 / #3: fewer wall-clock seconds per Phase 4 / Phase 2; same quality gate map.
   - #4: new `fix_task_suggestions` become actual applied patches; integration re-detection passes on second run.
   - #5: same task-graph outcomes (completed/merged counts) with fewer `max_rounds` bailouts and with Strands swarm events visible in telemetry.
3. **End-to-end**: Boot the Unified API via `make run` and trigger an SE team job from the UI (or `curl`); confirm the job reaches the Integration phase and the DevOps containerization step as today, and that progress status updates still fire.
4. **Lint**: `cd backend && make lint` (Ruff, 120-col, Python 3.10 target) must stay clean.
5. **Observability**: Confirm Strands graph/swarm events surface in the existing `job_store` progress stream so the UI continues to render phase-level status.
