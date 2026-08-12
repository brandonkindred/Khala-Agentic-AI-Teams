# ADR-013 — Agent platform package layout and public import paths

- **Status**: Proposed — design-note only, no code moves. Foundation for the sibling
  implementation sub-issues under the parent epic.
- **Date**: 2026-08-12
- **Owner**: Agent Studio / Agent Platform
- **Related**:
  - Epic #5692 — "Agent Studio: promote in-process agent_platform core (registry, console,
    sandbox, studio)" — this ADR resolves that epic's design-gate story.
  - Issue #5950 — records the explicit non-goals list and the migration order/phasing for the
    follow-on move stories; deliberately out of scope here and not duplicated.
  - Sub-issues gated on this ADR: #5722 (consolidate registry + console), #5723 (consolidate
    sandbox ownership), #5724 (consolidate Studio authoring), #5725 (lifespan cleanup and
    residue-dir removal).
  - `backend/agents/agent_registry/__init__.py`, `backend/agents/agent_console/__init__.py` —
    current façades this ADR relocates without reshaping.
  - `backend/agents/agent_team_studio/agent_provisioning_team/sandbox/README.md` — documents
    the sandbox process-affinity constraint this ADR restates and preserves.
  - `backend/agents/agent_team_studio/agent_studio/runtime.py`,
    `.../agent_studio/temporal/worker.py` — document the Studio process-affinity constraint
    this ADR restates and preserves.
  - `backend/unified_api/main.py` — the lifespan/mount call sites this ADR's import-path
    changes touch.

## Context

Ownership of "discover / author / run / sandbox agents" is split across four independently
evolved locations:

- `backend/agents/agent_registry/` — the manifest catalog (read-only disk-YAML source of
  truth plus an optional Postgres overlay for runtime-registered manifests).
- `backend/agents/agent_console/` — the runs / saved-inputs / diff data layer.
- `backend/agents/agent_team_studio/agent_provisioning_team/sandbox/` — the ephemeral
  per-agent sandbox runner, nested three levels deep inside a directory
  (`agent_team_studio`) whose own `__init__.py` says plainly: "Each subpackage keeps its own
  API app, Temporal workers, and Postgres schema — this package only groups them on disk."
- `backend/agents/agent_team_studio/agent_studio/` — Agent Studio's conversational
  single-agent authoring flow, nested the same way.

Parent epic #5692 wants these four consolidated into one package that is "the cohesive
backend" for that surface, with clear boundaries from Docker/env provisioning infra (which
stays put) and from domain apps that merely consume the platform (agentic compose, persona
runner). It names two concrete contradictions to resolve:

1. **Inconsistent import convention.** `agent_registry` and `agent_console` are imported with
   bare top-level names (`from agent_registry import ...`, `from agent_console import ...`).
   This only works because `backend/agents` itself sits on `PYTHONPATH`
   (`backend/Dockerfile:15 ENV PYTHONPATH=/app:/app/agents`, `backend/pytest.ini:11
   pythonpath = agents .`, `backend/conftest.py`). `sandbox` and `agent_studio` are already
   imported with fully-qualified dotted paths
   (`agent_team_studio.agent_provisioning_team.sandbox`, `agent_team_studio.agent_studio.X`),
   because they are nested and the bare-import trick does not reach them.
2. **Scattered, undocumented sandbox worker ownership.** The epic's own words: "Comments and
   module paths stop saying 'Agent Console sandbox' vs 'provisioning sandbox' for the same
   thing."

### Current state of each subsystem (verified against the live tree)

**`agent_registry`** — `__init__.py` exports `AgentRegistry`, `get_registry`, `AgentDetail`,
`AgentManifest`, `AgentStateSpec`, `AgentSummary`, `CognitionKnowledgeGraphSpec`,
`CognitionMemorySpec`, `CognitionSpec`, `InvokeSpec`, `IOSchema`, `SandboxSpec`, `SourceInfo`,
`TeamGroup`; `schema_resolver.py` separately exposes `resolve_schema`/`SchemaResolutionError`.
Docstring: "Read-only; no Postgres, no Temporal, no LLM" for the disk-YAML catalog;
`dynamic_store.py` adds an optional Postgres overlay for runtime-registered manifests
(Pattern B — `agent_registry/postgres.py::SCHEMA`, registered at `unified_api/main.py:661-666`).
No Temporal directory. Not present in `TEAM_CONFIGS`; mounted only via
`unified_api/routes/agents.py`'s `app.include_router(agents_router)`. Roughly 140 repo-wide
call sites depend on the bare-import form, including `backend/agent_sandbox_runtime/entrypoint.py`
(the sandbox container's own boot path), `agent_cognition/manifest_scope.py`,
`shared/agent_invoke/shim.py`, and heavy use throughout `agent_team_studio/agent_studio/**`
and `agent_team_studio/agentic_team_provisioning/**`.

**`agent_console`** — `__init__.py` exports `resolve_author`, `unified_json_diff`,
`DiffRequest`, `DiffResult`, `DiffSide`, `RunRecord`, `RunSummary`, `SavedInput`,
`AgentConsoleStorageUnavailable`, `AgentConsoleStore`, `get_store`. Docstring: "No FastAPI app
of its own — runs in-process inside the unified API (same pattern as agent_registry)."
Pattern B only (`agent_console/postgres/__init__.py::SCHEMA`, registered at `main.py:653-658`);
runs a background pruner asyncio task from lifespan step 5
(`from agent_console.prune import run_pruner`). Not present in `TEAM_CONFIGS`; mounted via
`unified_api/routes/{agents.py, agent_console_saved_inputs.py, agent_console_diff.py,
cognition.py}`. Zero import dependency in either direction with `agent_registry`, and zero
consumers anywhere under `agent_team_studio/**` — its fan-out is `unified_api/routes/*` plus
one guarded optional import in `software_engineering_team/api/pr_review.py` and one direct
import in `product_delivery/author.py`.

**`sandbox`** (`agent_team_studio/agent_provisioning_team/sandbox/`) — `__init__.py`
re-exports from `lifecycle.py`/`state.py`: `Lifecycle`, `get_lifecycle`, `acquire`, `status`,
`teardown`, `list_active`, `metrics`, `note_activity`, `run_idle_reaper`,
`DockerUnavailableError`, `SandboxAcquireFailedError`, `UnknownAgentError`, `SandboxHandle`,
`SandboxState`, `SandboxStatus`, `SandboxMetrics`, `AgeStats`, `BootMsStats`, `ReaperStats`,
`state_file_path`. `provisioner.py`'s `DockerError` is deliberately **not** re-exported —
callers that need it import `sandbox.provisioner` directly. Not present in `TEAM_CONFIGS`, and
not gated by any `TeamConfig.enabled`/`in_process` flag at all — it is a plain library
consumed only by `unified_api/routes/{sandboxes.py, agents.py}` and `unified_api/main.py`'s
lifespan. Its `Lifecycle` singleton is process-affine to the unified-API process (documented in
`sandbox/README.md`) — Decision §4 restates this as the ADR's authoritative, citable
worker-boot-ownership statement, per this issue's acceptance criterion.

**`agent_studio`** (`agent_team_studio/agent_studio/`) — has **no** package-level façade;
callers import submodules directly (`agent_team_studio.agent_studio.temporal.worker`,
`.postgres`, `.drafts_runtime`, `.models`, `.temporal.dispatch`, `.registration`, `.runtime`).
Unlike the other three, it **is** present in `TEAM_CONFIGS`, with `in_process=True` and prefix
`/api/agent-studio` — already correctly modeled as an in-process team, not a proxied one. It is
Pattern A, Temporal-only (`agent_studio/temporal/__init__.py` exports `WORKFLOWS`,
`ACTIVITIES`, `TASK_QUEUE = "agent-studio-queue"`, with no non-Temporal fallback), plus Pattern
B (`agent_studio/postgres.py::SCHEMA`, registered at `main.py:672-679`, gated on
`TEAM_CONFIGS["agent_studio"].enabled`). Its worker activities delegate to a process-local
`AgentStudioService`/`drafts_runtime` singleton (documented in `agent_studio/runtime.py` and
`agent_studio/temporal/worker.py`) — Decision §4 restates this as the ADR's authoritative,
citable worker-boot-ownership statement. Studio depends on `agent_registry.models.AgentManifest`;
registry does not depend on Studio.

**Adjacent, distinct concern that stays put.** The sibling directory
`agent_team_studio/agent_provisioning_team/` also holds an unrelated, onboarding-style
subsystem: long-lived Docker/Postgres/Redis/Git tool-account provisioning for specialist
agents (`tool_agents/`, `phases/`, `orchestrator.py`, `graphs/provisioning_graph.py`,
`models.py`, `prompts.py`, `anatomy_assets.py`, `api/main.py`, `shared/*.py`,
`manifests/*.yaml`, and the infra half of `temporal/{workflows.py,activities.py}` +
`constants.py:TASK_QUEUE`). This is already proxied via its own `TEAM_CONFIGS["agent_provisioning"]`
entry (`prefix="/api/agent-provisioning"`, `in_process` unset) and is Docker/env
provisioning infra per the epic — it is not part of the platform surface this ADR maps, and
does not move.

This ADR documents the target shape only. It authorizes no change to any `.py` file by
itself; the code moves are gated on this ADR existing, per #5722–#5725.

## Decision

### 1. Target package tree

`agent_platform` becomes a new top-level package directly under `backend/agents/` — sibling
to `agent_registry`, `blogging`, and every other existing top-level package today, **not**
nested under `agent_team_studio`.

It gets a real `__init__.py`, but a **thin** one: a module docstring stating the boundary
rules (registry, console, sandbox, and studio are the only members of this package; Docker/env
provisioning infra is explicitly not a member) with no symbol re-export fan-in. Each subsystem
keeps its own `__init__.py` façade one level down, unchanged in shape and export list, only
relocated. A deep façade that re-exports all ~50+ symbols directly off `agent_platform` was
considered and rejected: the four subsystems already have independent, actively-consumed
public APIs serving roughly 140 call sites, and collapsing them into one flat namespace would
create collisions (registry and console could each plausibly want a `models` submodule) for
no benefit callers asked for.

```
backend/agents/agent_platform/
  __init__.py              # thin façade: boundary-rules docstring only, no re-exports
  README.md                 # what is / isn't in this package, linking each subpackage README
  registry/                 # was agent_registry/                — façade unchanged
    __init__.py
    loader.py
    dynamic_store.py
    models.py
    postgres.py
    pydantic_examples.py
    schema_resolver.py
    manifest_projection.py
    scripts/generate_sample_skeletons.py
    tests/
    README.md
  console/                  # was agent_console/                 — façade unchanged
    __init__.py
    author.py
    diff.py
    models.py
    postgres/__init__.py
    prune.py
    store.py
    tests/
    README.md
  sandbox/                  # was agent_team_studio/agent_provisioning_team/sandbox/
    __init__.py              # façade unchanged
    lifecycle.py
    provisioner.py
    state.py
    temporal/                 # NEW subpackage — sandbox-only Temporal wiring, split out of
      __init__.py              # agent_provisioning_team/temporal/ (see §5)
      sandbox_workflows.py
      sandbox_activities.py
      worker.py                # start_agent_platform_sandbox_temporal_worker_thread
      constants.py              # SANDBOX_TASK_QUEUE
    tests/
    README.md
  studio/                   # was agent_team_studio/agent_studio/
    __init__.py               # NEW facade-of-convenience (studio has none today) — exports
                               # only the handful of names most callers need (the service
                               # accessor, build_studio_agent_manifest, clone_from_manifest);
                               # temporal.*, postgres, drafts_runtime stay reachable as
                               # submodules, matching sandbox's DockerError precedent
    agent_states.py
    assistant.py
    drafts_pg_store.py / drafts_runtime.py / drafts_store.py
    models.py
    pg_store.py / store.py
    registration.py
    runtime.py
    service.py
    postgres.py
    temporal/
    testing.py
    tests/
    README.md
```

### 2. Public import paths

Target convention for all four subsystems is **fully-qualified dotted**
(`agent_platform.registry`, `agent_platform.console`, `agent_platform.sandbox`,
`agent_platform.studio`) — resolving today's bare-vs-dotted split in favor of dotted. Bare
top-level imports work today only because `backend/agents` itself sits on `PYTHONPATH`; that
does not extend to grandchildren of a new `agent_platform` package, and dotted imports are
already proven at scale by `sandbox` and `agent_studio`. This ADR does not prescribe a
transitional compatibility shim for the old bare-import paths — whether the move stories use
one as an execution tactic is left to #5950 (migration order); this ADR fixes the destination,
not the path to it.

Representative old → new symbol map (the move stories should treat this as authoritative for
naming; it is not exhaustive of every symbol):

| Symbol(s) | Old import | New import |
|---|---|---|
| `get_registry`, `AgentRegistry`, `AgentManifest`, `AgentDetail`, `AgentSummary`, `IOSchema`, `SandboxSpec`, `TeamGroup`, ... | `from agent_registry import ...` | `from agent_platform.registry import ...` |
| `resolve_schema`, `SchemaResolutionError` | `from agent_registry.schema_resolver import ...` | `from agent_platform.registry.schema_resolver import ...` |
| `resolve_author`, `get_store`, `AgentConsoleStore`, `AgentConsoleStorageUnavailable`, `unified_json_diff`, `DiffRequest`/`DiffResult`/`DiffSide`, `RunRecord`/`RunSummary`, `SavedInput` | `from agent_console import ...` | `from agent_platform.console import ...` |
| `run_pruner` | `from agent_console.prune import run_pruner` | `from agent_platform.console.prune import run_pruner` |
| `Lifecycle`, `get_lifecycle`, `acquire`, `teardown`, `status`, `list_active`, `metrics`, `note_activity`, `run_idle_reaper`, `SandboxHandle`, `SandboxState`, `SandboxStatus`, `SandboxMetrics` | `from agent_team_studio.agent_provisioning_team.sandbox import ...` | `from agent_platform.sandbox import ...` |
| `DockerError` (not re-exported) | `from agent_team_studio.agent_provisioning_team.sandbox import provisioner` | `from agent_platform.sandbox import provisioner` |
| `SANDBOX_WORKFLOWS`, `SANDBOX_ACTIVITIES`, `SANDBOX_TASK_QUEUE` | `agent_team_studio.agent_provisioning_team.temporal` / `.temporal.constants` | `agent_platform.sandbox.temporal` |
| sandbox worker starter | `start_agent_provisioning_sandbox_temporal_worker_thread` (`agent_team_studio.agent_provisioning_team.temporal.worker`) | `start_agent_platform_sandbox_temporal_worker_thread` (`agent_platform.sandbox.temporal.worker`) — renamed to drop the now-misleading "provisioning" segment |
| Studio service/registration/temporal/postgres/drafts | `agent_team_studio.agent_studio.{runtime,registration,temporal,postgres,drafts_runtime,models}` | `agent_platform.studio.{runtime,registration,temporal,postgres,drafts_runtime,models}` |

```
backend/agents/agent_registry/                                    →  backend/agents/agent_platform/registry/
backend/agents/agent_console/                                     →  backend/agents/agent_platform/console/
backend/agents/agent_team_studio/agent_provisioning_team/sandbox/  →  backend/agents/agent_platform/sandbox/
backend/agents/agent_team_studio/agent_provisioning_team/temporal/
  {sandbox_workflows.py, sandbox_activities.py, SANDBOX_WORKFLOWS/
   SANDBOX_ACTIVITIES/SANDBOX_TASK_QUEUE exports}                  →  backend/agents/agent_platform/sandbox/temporal/
backend/agents/agent_team_studio/agent_studio/                    →  backend/agents/agent_platform/studio/
```

### 3. Unified-API mount points

No `TEAM_CONFIGS` schema changes result from this move. Registry, console, and sandbox stay
bare `app.include_router(...)` mounts — none of them has an independent HTTP prefix of its
own (`unified_api/routes/agents.py` already mixes all three under `/api/agents`), so giving
them synthetic `TeamConfig` entries would misrepresent them on team-discovery surfaces as
something with an independent, proxyable surface. Only their internal import paths change at
the existing `main.py` schema-registration and router-import call sites (e.g. the
`agent_console.postgres`/`agent_registry.postgres` imports at `main.py:653` and `:661`, the
`agent_console.prune.run_pruner` import at `main.py:767`, and the sandbox-router imports in
`unified_api/routes/{agents.py, sandboxes.py}`). Studio **keeps** its existing
`TEAM_CONFIGS["agent_studio"]` entry with `in_process=True` and prefix `/api/agent-studio`
unchanged — that was already correctly modeled; only its internal import paths change (e.g.
`main.py:604`, `:674`, `unified_api/routes/agent_studio.py`).

The "obvious-looking but wrong" alternative to reject explicitly: adding `TEAM_CONFIGS`
entries for registry/console/sandbox for symmetry with studio. They are not proxyable teams
and have no independent prefix; a `TeamConfig` entry for them would be misleading, not
clarifying.

### 4. Sandbox and Studio worker boot ownership

**Sandbox.** After the move, the renamed worker starter
(`start_agent_platform_sandbox_temporal_worker_thread`, living at
`agent_platform/sandbox/temporal/worker.py`) continues to be started **only** from
`backend/unified_api/main.py`'s own lifespan (`_maybe_start_sandbox_reaper`, gated on
`UNIFIED_API_SANDBOX_TEMPORAL_WORKER`) — never by `team_service`'s worker bootstrap and never
by whatever general team-worker registry starts `agent_provisioning_team`'s main
`WORKFLOWS`/`ACTIVITIES`. `SANDBOX_WORKFLOWS`/`SANDBOX_ACTIVITIES` must stay excluded from
that general registry's scan — an exclusion that already exists today and must be preserved
verbatim, not newly introduced. Why: the `Lifecycle` singleton
(`agent_platform/sandbox/lifecycle.py`) is in-memory, per-process state. A second process
serving `SANDBOX_TASK_QUEUE` would dispatch activities against its own unsynchronized
`Lifecycle` instance, silently diverging state — the documented failure mode is the reaper
tearing down a sandbox it wrongly believes idle, because it never saw the activity that most
recently touched it. Running the sandbox worker only inside the unified-API process guarantees
exactly one `Lifecycle` instance backs every activity dispatched on `SANDBOX_TASK_QUEUE`.

**Studio.** After the move, the Agent Studio Temporal worker continues to be started **only**
from `unified_api/main.py`'s lifespan (`_start_agent_studio_temporal_worker`, gated on
`UNIFIED_API_AGENT_STUDIO_TEMPORAL_WORKER`), as the sole worker for
`agent_platform.studio.temporal.TASK_QUEUE` ("agent-studio-queue"). This constraint does not
change post-move — Studio was never a standalone container; `TEAM_CONFIGS["agent_studio"].in_process
= True` already guarantees there is none — this section only re-anchors the statement at the
new import path. Why: worker activities delegate to the process-local `AgentStudioService`
and `drafts_runtime` singletons that the same process's HTTP handlers populate; a worker
running elsewhere would act on in-flight state it never saw created.

Moving these packages under `agent_platform/` is a pure rename with respect to worker
ownership: it changes where the code lives on disk and what it is imported as, not which
process boots the worker or why.

### 5. Boundary with Docker/env provisioning infra

Full non-goals recording is #5950's job; this section states the boundary only as far as it's
needed to define what `agent_platform/sandbox/` contains.

**Does not move** — stays under `agent_team_studio/agent_provisioning_team/` as infra:
`tool_agents/`, `phases/`, `orchestrator.py`, `graphs/provisioning_graph.py`, `models.py`,
`prompts.py`, `anatomy_assets.py`, `api/main.py`, `shared/{credential_store,
environment_store,fencing,job_store,tool_manifest,...}.py`, `manifests/*.yaml`, and the infra
half of `temporal/{workflows.py,activities.py}` + `constants.py:TASK_QUEUE` (served by the
standalone `agent_provisioning_team` team_service container's own main worker, unaffected by
this ADR).

**Does move** — into `agent_platform/sandbox/`: `lifecycle.py`, `provisioner.py`, `state.py`,
the sandbox `__init__.py` façade, plus the sandbox-only Temporal wiring
(`sandbox_workflows.py`, `sandbox_activities.py`, the `SANDBOX_WORKFLOWS`/
`SANDBOX_ACTIVITIES`/`SANDBOX_TASK_QUEUE` exports, and the renamed worker starter).

**File-level split required.** `agent_provisioning_team/temporal/worker.py` today defines both
`start_agent_provisioning_temporal_worker_thread` (infra — stays) and
`start_agent_provisioning_sandbox_temporal_worker_thread` (sandbox — moves) in the same file.
Post-move this becomes two files: `agent_provisioning_team/temporal/worker.py` keeps only the
infra starter; `agent_platform/sandbox/temporal/worker.py` gets the renamed sandbox starter.
The export layer already anticipates this split — `agent_provisioning_team/temporal/__init__.py`
already separates plain `WORKFLOWS`/`ACTIVITIES` (infra) from `SANDBOX_WORKFLOWS`/
`SANDBOX_ACTIVITIES` (sandbox) on distinct task queues — so only `worker.py` itself needs
dividing.

### 6. Residue-dir cleanup implication (informational only)

After registry, console, sandbox, and studio move out, `agent_team_studio/` retains only
`agentic_team_provisioning/`, `user_agent_founder/`, and the infra remainder of
`agent_provisioning_team/`. Its grouping rationale — already weak, per its own "disk-only
grouping" docstring — weakens further, since two of its four original members are gone.
Whether to rename or dissolve `agent_team_studio` further is left as an open question for a
future ADR or issue; it is not decided here, and this ADR does not assume #5950 decides it
either. The epic's acceptance criterion that empty top-level `backend/agents/agent_*` residue
dirs (the old `agent_registry/`, `agent_console/` locations) get removed is #5725's job; this
ADR's target tree is what makes that removal well-defined — nothing should remain at the old
paths once the move completes.

## Consequences

- A documented, collision-free target tree and import map exists before #5722–#5725 start, so
  those stories execute a mechanical move instead of re-litigating naming or location during
  implementation.
- Both contradictions the epic calls out — inconsistent import conventions, and
  scattered/undocumented sandbox worker ownership — get an explicit, citable resolution.
- Trade-off to record: the thin `agent_platform/__init__.py` means "cohesive package" holds at
  the directory/ownership level, not the symbol-import level — callers write
  `agent_platform.registry.X`, not `agent_platform.X`. This is deliberate, to avoid renaming
  ~140 call sites' symbol names and not just their path prefix; recorded here so a future
  reader doesn't assume a deeper façade was intended.
- Explicitly not decided by this ADR, and tracked in #5950 instead: the non-goals list; the
  ordering/phasing of #5722 → #5723 → #5724 → #5725; whether a transitional import shim is
  used during the move.
- No code changes result from this ADR. Implementation is gated on it, per this repo's
  existing convention for design-only ADRs (see ADR-009's and ADR-012's Context sections for
  the same "authorizes no code change by itself" framing).
