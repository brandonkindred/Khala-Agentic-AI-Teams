# Extract QA + security testing-phase unit into shared/phases/review.py

**Date:** 2026-07-22  
**Status:** Approved for implementation planning  
**Issue:** #2019 (parent #1983)

## Goal

Move the QA and security testing-phase unit from
`backend_code_v2_team/phases/review.py` into `shared/phases/review.py` as one
indivisible piece, following the same shared/`_impl` + team-wrapper split used
by `run_code_review_phase_impl` (#2018 / PR #2197). Backend public APIs stay
thin wrappers; frontend entry points remain out of scope (#2022).

## Motivation

`run_qa_testing_phase` and `run_security_testing_phase` are thin dispatchers
into one shared helper, `_run_agent_testing_phase`, parameterized by a frozen
`_AgentTestingPhaseSpec` and two module-level specs. Because both phase
functions share the entire helper body, they must move together — extracting
one without the other leaves a broken call site. Frontend has no equivalent
phases today, so this extraction relocates backend’s implementation to a shared
home; parity is a dependent follow-up.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Injection strategy | Inject `phase_review_result_cls` + `tool_phase_input_factory`; type `tool_kind` as `Any`; keep `agent_runner` as a callable |
| Naming | Shared exposes `run_qa_testing_phase_impl` / `run_security_testing_phase_impl` (mirror `run_code_review_phase_impl`) |
| Specs + helper | `_AgentTestingPhaseSpec`, `_QA_TESTING_PHASE_SPEC`, `_SECURITY_TESTING_PHASE_SPEC`, and `_run_agent_testing_phase` live in shared as one unit |
| Team wrappers | Backend keeps public `run_qa_testing_phase` / `run_security_testing_phase` with today’s signatures |
| Patch surface | `_run_qa_agent` / `_run_security_agent` stay on the backend review module |
| `Phase.REVIEW` | Use shared `software_engineering_team.shared.v2_models.Phase` (same as `v2_review`) |
| Spec `tool_kind` values | Keep today’s backend `ToolAgentKind.TESTING_QA` / `SECURITY` values in the shared specs; frontend kinds wait for #2022 |
| Behavior | Preserve frozen-dataclass parameterization and containment semantics exactly — no QA vs security behavior change |

## Architecture

### Files touched

| Path | Change |
|---|---|
| `shared/phases/review.py` | Add the testing-phase unit (`_AgentTestingPhaseSpec`, both specs, `_run_agent_testing_phase`, both `*_impl` dispatchers) alongside existing `run_code_review_phase_impl` |
| `backend_code_v2_team/phases/review.py` | Replace inline helper/specs/bodies with thin wrappers that inject team deps and call the `*_impl` functions |
| Tests | No rewrites expected; keep importing backend public APIs |

### Data flow

```text
execution / tests
  → backend.phases.review.run_qa_testing_phase / run_security_testing_phase
      → run_*_testing_phase_impl(
            agent_runner=partial(_run_qa_agent|_run_security_agent, …),
            phase_review_result_cls=PhaseReviewResult,
            tool_phase_input_factory=REVIEW_CONFIG.tool_phase_input_factory,
            …)
          → _run_agent_testing_phase(spec=_QA|_SECURITY_TESTING_PHASE_SPEC, …)
```

### Shared helper contract (`_run_agent_testing_phase`)

Behavior unchanged from today’s backend implementation:

1. If `review_agent` is set → call `agent_runner` → extend issues; on exception,
   append a synthetic issue at `spec.missing_severity` (never raise).
2. If `tool_agents` contains `spec.tool_kind` with a `.review` method → build
   input via `tool_phase_input_factory` (`phase=Phase.REVIEW`, microtask, files,
   prior issues, task metadata, …) → fold tool issues; on exception, log and
   skip (do not abort the phase).
3. If neither agent nor tool is available → append the spec’s “gate skipped”
   issue (`missing_description` / `missing_recommendation` / `missing_severity`).
4. `passed` = no blocking severities (`is_blocking`); return
   `phase_review_result_cls(passed=…, issues=…, summary=…, phase_name=spec.phase_name)`.

Preconditions:

- When `review_agent` is not `None`, `agent_runner` runs it over `files` and
  returns a list of `ReviewIssue`s (shared `v2_models.ReviewIssue`).
- `tool_phase_input_factory` accepts the kwargs built for tool-agent review
  (same shape `REVIEW_CONFIG.tool_phase_input_factory` / `ToolAgentPhaseInput`
  already accept).
- `phase_review_result_cls` constructs the team’s phase-result type from
  `passed` / `issues` / `summary` / `phase_name`.

Postconditions:

- Returns a `phase_review_result_cls` instance that fails on any critical/high
  issue, including a synthesised “gate skipped” issue when neither
  `review_agent` nor the spec’s tool agent is available.
- An outright `agent_runner` failure never propagates: it is reported as a
  synthetic issue at `spec.missing_severity`.

### Backend wrapper shape

Each public function keeps its current keyword-only signature and becomes:

```python
return run_qa_testing_phase_impl(  # or run_security_testing_phase_impl
    task=task,
    microtask=microtask,
    files=files,
    review_agent=qa_agent,  # or security_agent
    agent_runner=partial(_run_qa_agent, qa_agent=qa_agent),  # or security
    tool_agents=tool_agents,
    repo_path=repo_path,
    detail_callback=detail_callback,
    language=language,
    cache=cache,
    phase_review_result_cls=PhaseReviewResult,
    tool_phase_input_factory=REVIEW_CONFIG.tool_phase_input_factory,
)
```

Call-time resolution of `_run_qa_agent` / `_run_security_agent` via `partial` at
the wrapper boundary preserves the existing monkeypatch surface on the backend
review module.

## Error handling

Preserve current containment exactly:

| Failure | Behavior |
|---|---|
| `agent_runner` exception | Synthetic issue at `spec.missing_severity` (QA=`high`, security=`critical`); phase continues |
| Tool-agent `.review` exception | Warning log only; do not abort |
| Both agent and tool missing | Spec “gate skipped” issue |
| Blocking severities | `passed=False` via `is_blocking` |

## Testing

- `test_v2_review_phase.py` and `test_microtask_review_gates.py` continue to
  import backend `run_qa_testing_phase` / `run_security_testing_phase` and pass
  unchanged.
- No requirement to retarget patches onto shared; backend wrappers remain the
  public and patch surface.
- `make test` and `make lint` from `backend/`; ≥90% line coverage on touched
  files.

## Out of scope

- `run_code_review_phase` — already extracted (#2018 / PR #2197).
- Frontend `run_qa_testing_phase` / `run_security_testing_phase` entry points
  (#2022).
- Changing QA vs security labels, severities, tool kinds, or containment
  semantics.
- Moving `_run_qa_agent` / `_run_security_agent` out of the backend review
  module.

## Complexity

**3** — larger unit than the code-review extraction (~250 combined lines across
the helper, dataclass, two specs, and two dispatcher functions), must move as a
single indivisible piece, and the frozen-dataclass parameterization must be
preserved exactly to avoid silently changing QA vs security behavior.
