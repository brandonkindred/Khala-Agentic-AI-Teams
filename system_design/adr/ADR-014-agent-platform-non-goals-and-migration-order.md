# ADR-014 — Agent platform non-goals and migration order

- **Status**: Accepted — design-note only, no code moves. Unblocks the sibling move stories
  under the parent epic.
- **Date**: 2026-08-12
- **Owner**: Agent Studio / Agent Platform
- **Related**:
  - Issue #5950 — "Record agent_platform non-goals and migration order" — this ADR is the
    design-doc deliverable that closes it.
  - ADR-013 (`system_design/adr/ADR-013-agent-platform-package-layout.md`) — the sibling ADR
    this document completes. ADR-013 designed the target package tree, import map, unified-API
    mount points, and sandbox/Studio worker-boot-ownership constraints, and explicitly deferred
    the non-goals list, the #5722→#5723→#5724→#5725 ordering, and the transitional-import-shim
    question to #5950.
  - Epic #5692 — "Agent Studio: promote in-process agent_platform core (registry, console,
    sandbox, studio)" — source of the phase labels and "blocked by" graph this ADR's migration
    order restates with rationale.
  - Issue #5721 — "agent_platform (1/5): design package layout and import map" — resolved by
    ADR-013, already merged.
  - Issue #5722 — "agent_platform (2/5): consolidate registry and console under platform"
    (phase-2) — blocked by #5721.
  - Issue #5723 — "agent_platform (3/5): consolidate sandbox ownership under platform"
    (phase-3) — blocked by #5721.
  - Issue #5724 — "agent_platform (4/5): consolidate Studio authoring under platform"
    (phase-4) — blocked by #5721.
  - Issue #5725 — "agent_platform (5/5): lifespan cleanup, residue dirs, and docs" (phase-5) —
    blocked by #5722, #5723, #5724.
  - `backend/agents/agent_team_studio/__init__.py` — evidence for the consumer-apps non-goal
    below; its docstring currently groups `agent_studio`, `agentic_team_provisioning`,
    `agent_provisioning_team`, and `user_agent_founder` under one disk-only namespace.

## Context

ADR-013 designed the target `agent_platform` package tree, import map, unified-API mount
points, and sandbox/Studio worker-boot-ownership constraints for consolidating
`agent_registry`, `agent_console`, the sandbox runner
(`agent_team_studio/agent_provisioning_team/sandbox/`), and `agent_studio` into a new
`backend/agents/agent_platform/` package. It explicitly left three things undecided, in its own
words: "Explicitly not decided by this ADR, and tracked in #5950 instead: the non-goals list;
the ordering/phasing of #5722 → #5723 → #5724 → #5725; whether a transitional import shim is
used during the move."

Without an explicit non-goals list, "consolidate the agent platform" reads as open-ended. It
could be misread as also covering Docker/env provisioning — which lives in the very same
parent directory today, `agent_team_studio/agent_provisioning_team/` — or as license to touch
the agentic-compose and persona-runner domain apps that merely consume these four subsystems.
Epic #5692 and issue #5950 both already name the intended boundary; this ADR is where that
boundary becomes a citable decision instead of prose scattered across two issue bodies.

Separately, #5722–#5725 are already sequenced on GitHub via "blocked by" edges and phase
labels on epic #5692. That records *that* the order is fixed, not *why* this particular order
minimizes risk, and it isn't visible to a reader of `system_design/adr/` who doesn't also read
GitHub issue metadata. This ADR records the order as a durable design decision, with the
dependency and risk reasoning behind it, and resolves the shim question ADR-013 left open so
#5722 can start without re-litigating scope.

## Decision

### 1. Non-goals

- **Docker/environment provisioning infrastructure stays where it is, out of
  `agent_platform/`.** This is not a new boundary — ADR-013 §5 already drew it at file
  granularity: `agent_provisioning_team`'s `tool_agents/`, `phases/`, `orchestrator.py`,
  `graphs/provisioning_graph.py`, `models.py`, `prompts.py`, `anatomy_assets.py`,
  `api/main.py`, `shared/*.py`, `manifests/*.yaml`, and the infra half of
  `temporal/{workflows.py,activities.py}` + `constants.py:TASK_QUEUE` stay under
  `agent_team_studio/agent_provisioning_team/`, served by the standalone
  `agent_provisioning_team` team_service container. Only `lifecycle.py`, `provisioner.py`,
  `state.py`, the sandbox `__init__.py` façade, and the sandbox-only Temporal wiring move, per
  ADR-013 §1/§5. This ADR elevates that boundary from "scoped to defining what the sandbox
  subpackage contains" (ADR-013 §5's own framing) to an explicit non-goal of the whole epic:
  no move story under #5692 relocates, rewrites, or reshapes any part of the onboarding-style
  provisioning subsystem.
- **`agentic_team_provisioning/` (agentic compose) and `user_agent_founder/` (persona runner)
  stay consumers of `agent_platform`, not members of it.** Today's dependency direction is
  already one-way and verified: `user_agent_founder` imports none of the four platform
  subsystems; `agentic_team_provisioning` imports only `agent_registry` — never `agent_console`,
  the sandbox, or `agent_studio` — at sites in `models.py`, `roster_resolve.py`,
  `agent_env_provisioning.py`, `manifest_generation.py`, and
  `api/services/teams.py` (plus its test suite). No platform subsystem imports back from either.
  #5722's registry move updates these call sites' import paths mechanically
  (`agent_registry` → `agent_platform.registry`, per ADR-013 §2); their domain logic and
  ownership do not move, and no move story rewrites either subsystem's behavior.
- Also out of scope for the whole epic, per #5692's own out-of-scope list (restated here for
  completeness, not re-argued): deleting frontend legacy routes (a separate UI cutover epic),
  rewriting persona founder or agentic pipeline domain logic, changing LLM provider resolution,
  and a full merge of proxied agentic/persona HTTP surfaces into `/api/agent-studio`.
- Also out of scope for this ADR specifically, per #5950's own out-of-scope list: the package
  tree and import map (ADR-013's job, already merged) and the frontend cutover.

### 2. Migration order for #5722 → #5723 → #5724 → #5725

The order is already fixed by "blocked by" edges on epic #5692. This section records the risk
and dependency reasoning behind that specific order, as the design-of-record follow-on stories
should execute against:

- **#5722 first** (registry + console). Zero cross-dependency on the sandbox or Studio, and
  minimal external consumer surface — console has no consumers under `agent_team_studio/` at
  all; registry has exactly the one-way `agentic_team_provisioning` edge named in §1. This is
  the lowest-blast-radius move of the four, and it is the one that establishes the dotted
  `agent_platform.registry` / `agent_platform.console` import convention (ADR-013 §2) that the
  remaining phases follow, rather than each phase choosing its own.
- **#5723 second** (sandbox). No import dependency forces an order between sandbox and Studio —
  neither imports the other. Sandbox is sequenced second so the
  `agent_provisioning_team/temporal/worker.py` file split (ADR-013 §5: separating the
  infra-owned worker starter from the sandbox-owned one) lands as its own single-purpose PR,
  decoupled from Studio's larger consumer surface.
- **#5724 third** (Studio). Studio depends on `agent_registry.models.AgentManifest` — ADR-013's
  one confirmed cross-subsystem edge among the four — so it cannot move before #5722 renames
  that import path. It also carries the widest HTTP/consumer blast radius of the three move
  phases: it is the only one of the four already mounted as its own `TEAM_CONFIGS` entry
  (`in_process=True`, prefix `/api/agent-studio`). Sequencing it last among the moves keeps the
  riskiest, most externally-visible change last, informed by the first two phases' execution
  experience.
- **#5725 last** (lifespan cleanup, residue dirs, docs). Removing the now-empty top-level
  `agent_registry/`, `agent_console/`, and related residue directories, and cleaning up
  lifespan wiring, is only safe once nothing anywhere in the repo still references the old
  import paths — a precondition only guaranteed true after #5722, #5723, and #5724 have all
  landed. This is the ordering's terminal constraint, not an arbitrary "do it last."

### 3. Transitional import shim

No repo-wide compatibility shim is used during the move. Each move story (#5722, #5723,
#5724) performs a hard, mechanical rename of its own call sites in the same PR that relocates
the code, verified by the existing test suite that already exercises those call sites (ADR-013
notes roughly 140 repo-wide sites depend on today's four subsystems).

Rationale:

- **Scope fit.** A shim is an execution tactic internal to a single move story, not a
  non-goal or an ordering decision — deciding one here would exceed this issue's declared
  scope (a short design-doc section recording non-goals and order).
- **Precedent.** Two of the four subsystems (`sandbox`, `agent_studio`) already use
  fully-qualified dotted imports at scale today (ADR-013 §2); a hard rename to
  `agent_platform.X` is the same kind of change, not a new pattern.
- **No external consumers.** These are in-repo, in-process Python import paths with no
  external package consumers — there is no compatibility contract to preserve across a release
  boundary.
- **Keeps #5725 well-defined.** With no shim, #5725's precondition check ("nothing references
  old paths") is a simple zero-hit grep for the old prefix. A shim would make that check
  ambiguous — re-exports at the old path would need to be distinguished from genuine leftover
  usage.

This is a strong default, not an unconditional mandate: if a specific move story hits an
unexpected sequencing problem (e.g., a long-lived PR window with active concurrent edits to
the same call sites), that story's own PR description should call out and justify the
deviation rather than silently introducing a shim.

## Consequences

- All three items ADR-013 deferred to #5950 — non-goals, migration order, and the shim
  question — are now recorded in one place, so #5722 can start without re-litigating scope or
  ordering rationale mid-implementation.
- The non-goals list gives every move story, and any future scope-creep discussion, a citable
  boundary: Docker/env provisioning infra and the two domain-app consumers are touched only by
  mechanical import-path updates where they already depend on `agent_registry`, never by logic
  changes.
- The migration-order rationale gives a future reader — including anyone auditing why #5723
  shipped before #5724 — the risk reasoning behind the sequence, not just the fact of it.
- The "no shim by default" decision means #5722, #5723, and #5724 are each expected to be a
  single hard-rename PR; any story that deviates should say so and why in its own PR
  description, not add a shim silently.
- No code changes result from this ADR, matching this repo's existing convention for
  design-only ADRs (see ADR-013's and ADR-009's Context sections for the same
  "authorizes no code change by itself" framing).
