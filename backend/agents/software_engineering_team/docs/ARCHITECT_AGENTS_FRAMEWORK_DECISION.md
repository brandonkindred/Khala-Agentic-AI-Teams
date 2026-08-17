# Decision: `architect_agents`' Orchestration Framework

## Status

**Decided.** This document decides whether the Enterprise Orchestrator
subsystem in `architect_agents/agents/orchestrator.py` should be migrated
onto `BaseTeamLead` (`shared/team_lead_base.py`) or documented as a
justified, permanent exception. It does **not** cover
`architecture_expert.ArchitectureExpertAgent`, the other, unrelated agent
that also lives under `architect_agents/` — that agent already follows
this team's standard LLM-calling conventions and was never an outlier (see
Background).

## Background

`architect_agents/` contains two independent implementations that happen
to share a directory:

- **`architecture_expert.ArchitectureExpertAgent`** — the agent actually
  wired into the product. `software_engineering_team`'s top-level
  orchestrator calls it during the Design phase, and it's reachable via
  `POST /architect/design`. It builds its model through
  `llm_service.get_strands_model()`, the same shared adapter every other
  Strands-based agent in this codebase uses — provider-agnostic, routed
  through `LLMClient`, with the usual rate-limiting/telemetry/attribution.
  It is not part of this decision.
- **The Enterprise Orchestrator** — `agents/orchestrator.py`'s
  `create_orchestrator()`, its nine specialist modules
  (`agents/security.py`, `agents/application.py`, `agents/data.py`,
  `agents/api_design.py`, `agents/cloud_infra.py`,
  `agents/data_streaming.py`, `agents/devops.py`,
  `agents/observability.py`, `agents/architecture_scrutineer.py`), and
  `agents/session.py`. This is the subsystem this decision covers.

The Enterprise Orchestrator is a single `strands.Agent` built with the
**Agents-as-Tools** pattern: each specialist is exposed to the orchestrator
as an LLM-callable tool, and the orchestrator's own system prompt
(`agents/prompts.py`'s `ORCHESTRATOR_PROMPT`) — not Python control flow — tells the LLM what
order to call them in and when to run them in parallel. The LLM decides,
at runtime, which tools to invoke. It also constructs its model from a
bare Bedrock model-ID string
(`os.environ.get("ARCHITECT_MODEL_ORCHESTRATOR", "anthropic.claude-opus-4-6-v1")`
passed straight to `strands.Agent(model=...)`), bypassing `llm_service`
entirely, and optionally attaches Strands' own `FileSessionManager` /
`S3SessionManager` (`agents/session.py`) for cross-call conversation
persistence keyed by `session_id`.

**Reachability.** The Enterprise Orchestrator is not in
`unified_api/config.py`'s `TEAM_CONFIGS`, and it has no caller anywhere
else in `backend/`. Its one intended integration point,
`integration.py::run_enterprise_architect()` (gated by
`SW_USE_ENTERPRISE_ARCHITECT=true`, shelling out via `subprocess.run` to
`main.py`), is never referenced by `software_engineering_team`'s own
top-level orchestrator or anywhere else in the tree — it exists but is
never invoked. The only reachable entry points are its own CLI (`main.py`)
and its own standalone AWS Bedrock AgentCore HTTP service
(`agentcore_main.py`), both run out-of-band from the Unified API.

**What `BaseTeamLead` is.** `shared/team_lead_base.py` defines
`TeamLeadSharedState` and `BaseTeamLead`, a thin control-flow/state helper
for engineer-authored, deterministic orchestration: `_run_gated_phases` /
`_run_phase_gates` run a fixed sequence of phase callables and stop at the
first failure, `_run_bounded_retry_loop` drives a bounded retry cycle, and
`_run_setup_and_delegate` runs setup, checks config gates, and delegates to
a development agent's `run_workflow`. It is used by exactly four
orchestrators, all inside `software_engineering_team/`:
`BackendCodeV2TeamLead`, `FrontendCodeV2TeamLead`,
`AIAgentDevelopmentTeamLead` (a complete but currently dormant team with no
production consumer), and `DevOpsTeamLeadAgent` (which also layers
LangGraph graphs on top of it for its gate pipeline). This team's own
README already documents these as **deliberately different shapes**, not
convergent drift (`README.md`, "Sub-team shapes (deliberate, not drift)").
`BaseTeamLead` has no analog for cross-call session/conversation
persistence — its instances are constructed fresh per activity invocation,
and job status/progress instead goes through the orthogonal
`job_store`/`JobServiceClient` layer.

## Decision

**Keep the Enterprise Orchestrator (`agents/orchestrator.py`, its nine
specialist modules under `agents/*.py`, and `agents/session.py`) as a
justified exception. Do not migrate it to `BaseTeamLead`.**

## Rationale

- **Paradigm mismatch, not base-class mismatch.** `BaseTeamLead`'s helpers
  are for a human-authored, deterministic phase/gate sequence — the
  engineer decides control flow in Python and the LLM only fills in
  content within each phase. The Enterprise Orchestrator's control flow is
  the opposite: the LLM decides which specialist tool to call and when,
  guided by prose in `agents/prompts.py`'s `ORCHESTRATOR_PROMPT`. Porting this to
  `BaseTeamLead` isn't swapping a base class — it's replacing the
  orchestrator's actual delegation mechanism with a different one.
- **`BaseTeamLead` is one of several already-coexisting shapes, not a
  universal norm being violated.** It's adopted by four
  `software_engineering_team`-internal orchestrators, one of them dormant,
  and even `devops_team` combines it with LangGraph rather than using it
  alone. The team's README already states these shapes are deliberate. The
  Enterprise Orchestrator isn't breaking a single hard convention; it's
  another deliberately different shape for a genuinely different
  delegation model.
- **Session persistence has no `BaseTeamLead` equivalent, and would have
  to be dropped or rebuilt.** Strands' `FileSessionManager`/
  `S3SessionManager` gives an `Agent` instance-level conversation history
  that can be resumed across process restarts by `session_id`.
  `BaseTeamLead` offers nothing comparable — team leads are stateless
  objects reconstructed per call. Migrating would force an explicit choice
  between losing multi-turn conversation continuity or building new
  persistence machinery this base class doesn't provide.
- **There is no live caller to gain consistency for.** The Enterprise
  Orchestrator is unreachable from the Unified API today (see
  Background). Migrating it to match `BaseTeamLead` would not make any
  currently-observable product behavior more consistent — the value of
  "framework consistency" applies here to code with no execution path in
  production, which is a materially weaker case for spending migration
  effort than it would be for a live subsystem.
- **Migration cost is substantial relative to that value.** It would
  require rewriting nine `strands.Agent`-as-tool specialists into
  imperative calls; authoring new Pydantic request/result models matching
  `BaseTeamLead`'s handoff contract; re-expressing
  `agents/prompts.py`'s `ORCHESTRATOR_PROMPT` natural-language sequencing as an explicit
  Python phase/gate order (including its sequential/parallel groupings);
  re-routing model resolution from bare Bedrock model IDs onto
  `llm_service`; and resolving the session-persistence gap above — real
  engineering cost for a subsystem nothing currently calls.

## Why this is justified, not merely grandfathered

This is a justified exception, not legacy code left alone because touching
it is out of scope: the Agents-as-Tools + session-manager shape is the
*correct* fit for what this subsystem is trying to do (LLM-driven
multi-specialist delegation with optional multi-turn continuity), and
forcing it onto `BaseTeamLead`'s deterministic-phase model would be a
strictly worse fit for that goal, not merely a different-looking
equivalent. The exception is scoped narrowly: it covers
`agents/orchestrator.py`, its nine specialist modules, and
`agents/session.py` specifically, because those are the pieces built on
the Agents-as-Tools/session-manager shape. It explicitly does **not**
extend to `architecture_expert.ArchitectureExpertAgent`, which already
uses this team's standard `llm_service`-backed Strands pattern and was
never a framework outlier.

This decision is conditional on the Enterprise Orchestrator's current
reachability. If a future change wires `SW_USE_ENTERPRISE_ARCHITECT` (or
any other path) into live production traffic, "no live caller" stops being
part of the rationale, and this decision should be revisited — the other
four rationale points would still apply, but the calculus of migration
cost versus payoff would change materially once real traffic depends on
this subsystem.

## Known caveats

- **Session persistence behaves differently, but is not doing anything
  useful, on either reachable path today.** `main.py` is one process per
  CLI invocation: it calls `get_session_manager()` with no `session_id`,
  gets a fresh random UUID, and exits — nothing is ever resumed by ID.
  `agentcore_main.py` is different and worth calling out precisely:
  `_create_app()` builds the session manager and orchestrator **once**,
  before `invoke()` is defined, so that single session/orchestrator pair
  is reused for every HTTP request the process handles for its lifetime —
  and `invoke()` never reads the payload's documented `session_id` field,
  so a caller cannot select or resume a specific session either. In
  practice this means the AgentCore path doesn't give per-request
  isolation: concurrent or sequential callers to the same running process
  share one conversation history, not that persistence is simply off.
  `integration.py`'s subprocess bridge sidesteps this entirely by setting
  `ARCHITECT_SESSION_DISABLED=1`. A future reader should not assume either
  path gives a caller controlled, resumable multi-turn continuity in
  production today — it does not, for two different reasons.
- **The Enterprise Orchestrator's dead-code reachability is a separate,
  unresolved question.** Whether the dormant `SW_USE_ENTERPRISE_ARCHITECT`
  bridge should be wired up, left as-is, or removed is a product/scoping
  decision independent of the framework question this document answers,
  and is explicitly out of scope here.

## If migration is revisited later

Kept short, since this is not the chosen path: a future migration epic
would need to (a) confirm there is an actual product need driving the
work, since none exists today; (b) design an equivalent for
session/conversation continuity under `BaseTeamLead`'s stateless-per-call
model, or make an explicit decision to drop it; (c) convert the nine
specialist agents from `strands.Agent`-as-tool functions into directly
callable Python objects; (d) replace `agents/prompts.py`'s `ORCHESTRATOR_PROMPT`
LLM-driven sequencing with an explicit, hand-authored phase/gate order,
including its sequential-vs-parallel groupings; (e) move model resolution
off bare Bedrock model-ID strings onto `llm_service`; (f) decide whether
the result is wired into Temporal activities/`job_store` like the other
four `BaseTeamLead` orchestrators, or intentionally stays out-of-band; and
(g) decide the fate of `agentcore_main.py`'s standalone Bedrock AgentCore
HTTP runtime, which has no `BaseTeamLead` analog.

## Out of scope

- Performing the migration, regardless of which way this decision goes.
- Any code change to `architect_agents`.
- Deciding whether the Enterprise Orchestrator should be wired into a live
  execution path — a separate product question this document raises but
  does not resolve.

## See also

- [`architect_agents/README.md`](../architect_agents/README.md) — states
  the split between `ArchitectureExpertAgent` and the Enterprise
  Orchestrator.
- This team's [`README.md`](../README.md), "Sub-team shapes (deliberate,
  not drift)" section — the existing precedent for documenting deliberate
  shape divergence among `BaseTeamLead`-adjacent orchestrators.
- [`LLM_CALLING_PATTERN_DECISION.md`](LLM_CALLING_PATTERN_DECISION.md) —
  the sibling decision document this one's structure and exception-writeup
  style are modeled on.
