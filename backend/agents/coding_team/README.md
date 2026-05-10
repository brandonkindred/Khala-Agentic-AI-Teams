# Coding Team

The **coding_team** is a **sub-team of the Software Engineering team**. It implements the main implementation path after planning: the SE orchestrator hands off to it; it receives the adapted plan from Planning V3, generates a Task Graph, and executes work through a Tech Lead and multiple stack-specialist Senior Software Engineers. The public API remains at `/api/coding-team` for direct jobs and health checks; logically it sits under Software Engineering in the platform hierarchy.

## Architecture (Mermaid)

### Components and data flow

```mermaid
flowchart TB
  subgraph external [External inputs]
    SEOrch[software_engineering_orchestrator]
    PlanV3[Planning_V3_handoff]
    Repo[repo_path_workspace]
    JobSvc[job_service_client_team_coding_team]
  end

  subgraph api [HTTP_API]
    FastAPI["FastAPI_/api/coding-team"]
    FastAPI --> Run["POST_/run"]
    FastAPI --> Status["GET_/status_job_id"]
    FastAPI --> Jobs["GET_/jobs"]
  end

  subgraph core [Coding_team_core]
    Orch[run_coding_team_orchestrator]
    TL[TechLeadAgent_LLM_tech_lead]
    TG[TaskGraphService]
    SWE1[SeniorSWEAgent_stack_1]
    SWE2[SeniorSWEAgent_stack_N]
  end

  SEOrch -->|"CodingTeamPlanInput"| Orch
  PlanV3 -.->|"requirements_spec"| SEOrch
  Run -->|"background_thread"| Orch
  Orch --> TL
  TL -->|"tasks_stacks_JSON"| TG
  Orch --> SWE1
  Orch --> SWE2
  TG -->|"persist_snapshot"| JobSvc
  Orch -->|"phase_status"| JobSvc
  SWE1 --> Repo
  SWE2 --> Repo
  Orch -->|"merge_branch"| GitUtil[git_utils_merge_branch]
  GitUtil --> Repo
```

### Execution loop inside the orchestrator

Phases: `task_graph` → `coding` → `completed`. The orchestrator runs up to many rounds until no `to_do` tasks remain, no agent holds an active task, and nothing is `in_review`.

```mermaid
flowchart LR
  subgraph init [Init]
    A[TechLead_run_plan_to_task_graph] --> B[TaskGraph_add_tasks]
    B --> C[Create_one_SeniorSWE_per_StackSpec]
  end

  subgraph loop [Per_round_loop]
    D[Find_ready_tasks_and_free_agents]
    D --> E[TechLead_run_assignments]
    E --> F[TaskGraph_assign_task_to_agent]
    F --> G[Each_SWE_run_implement]
    G --> H{status}
    H -->|in_review| I[TaskGraph_set_in_review]
    H -->|failed| J[log_warning]
    I --> K[TechLead_run_code_review]
    K -->|approved| L[git_merge_feature_to_development]
    L --> M[TaskGraph_mark_branch_merged]
    K -->|not_approved| I
  end

  subgraph done [Termination]
    N[No_to_do_no_active_no_in_review]
    N --> O[status_completed]
  end

  init --> loop
  loop --> loop
  loop --> done
```

## Structure

| Component | Role |
|-----------|------|
| **Tech Lead** | Receives plan from Planning team; generates Task Graph (tasks + dependencies); defines tech stacks; creates one Senior SWE agent per stack; grooms backlog (acceptance criteria, out of scope, context, subtasks, priority, dependencies); assigns tasks; code review, UAT, security review; merges feature branches; assigns next task only after current task's branch is merged. |
| **Senior Software Engineer** | One per stack (e.g. frontend, backend, devops). Requests assigned task from Task Graph; implements (code + tests); runs tests and linter until pass; commits with semantic style; marks task In Review; hands off feature branch to Tech Lead. Single task at a time. |
| **Task Graph** | Stores tasks and dependencies per job. Tech Lead adds/updates tasks and assigns; Senior SWEs request their assigned task. Enforces one active task per agent and "next task only after merge." |

## Task Graph semantics

- **Tasks** have id, title, description, dependencies, status (e.g. To Do, in_progress, in_review, merged), assigned_agent_id, feature_branch, merged_at, acceptance_criteria, out_of_scope, priority, and optional **subtasks** (with subtask dependencies).
- **Assign** task T to agent A: allowed only if A has no current task or A's current task has status merged, and T's dependencies are satisfied (all dependency tasks merged).
- **Get task for agent A**: returns the single task assigned to A that is not merged (in_progress or in_review).
- **Mark branch merged** for task T: set T.status = merged, T.merged_at = now; agent A is then free for next assignment.

## One task per agent / new task only after merge

- Each Senior SWE has at most one **active** (non-merged) task at a time.
- The Tech Lead (or orchestrator) assigns a **new** task to an agent only after that agent's current task's feature branch has been **merged** into the development branch. The Task Graph and orchestrator enforce this via state.

## Package layout

- `models.py` – Pydantic models (StackSpec, Task, SeniorEngineerSpec, CodingTeamPlanInput, job state).
- `task_graph.py` – Task Graph service (per-job; add_task, assign_task_to_agent, get_task_for_agent, mark_branch_merged, etc.).
- `tech_lead_agent/` – Tech Lead agent (prompts, agent class): plan → Task Graph + stacks; grooming; assignments; review/merge.
- `senior_software_engineer_agent/` – Senior SWE agent (parameterized by StackSpec): request task, implement, tests, linter, commit, In Review, hand off.
- `orchestrator.py` – Coordinates Tech Lead and Senior SWEs; init (plan → Task Graph, create SWEs), loop (assign → implement → review → merge).
- `api/main.py` – FastAPI: POST /run, GET /status/{job_id}, GET /jobs.
- Job store uses the same pattern as software_engineering_team: `JobServiceClient(team="coding_team")` from `job_service_client`.

## Process flows

- **Tech Lead**: Get next task from backlog → Groom task (acceptance criteria, out of scope, context from specs/plans, subtasks, priority, dependencies) → Update to To Do → Assign to team member → repeat until backlog groomed. See plan section 2a.
- **Senior SWE**: Review assigned tasks → Choose next task (deps satisfied) → If subtasks, choose next subtask → Create feature branch → Plan changes → Make changes → Tests (loop until pass) → Linter (loop until pass) → Commit (semantic style) → If more subtasks loop else Mark In Review → Send feature branch to Tech Lead. See plan section 2b.

## GitHub-issue-driven runs

In addition to the planning-team handoff path (`POST /run`), the team accepts work directly from GitHub issues via `POST /run-from-github`. The endpoint reads open issues from the target repo, picks the first whose **GitHub native sub-issues** are all closed, runs the issue through the existing Tech Lead → Senior SWE pipeline on a stable per-issue branch (`khala/issue-<num>`), and reports back on the issue thread.

### Request

```http
POST /api/coding-team/run-from-github
Content-Type: application/json

{
  "owner": "your-org",
  "repo": "your-repo",
  "repo_path": "/abs/path/to/local/checkout",
  "label": "agent-ready",          // optional issue-label filter
  "issue_number": 123,              // optional: verify a specific issue
  "github_token": "...",           // optional: overrides GITHUB_TOKEN env
  "base_branch": "main",           // optional: defaults to repo default branch
  "remote": "origin"               // optional
}
```

Response:

```json
{ "job_id": "...", "issue_number": 7, "issue_url": "https://github.com/...", "status": "pending" }
```

Poll `GET /status/{job_id}` to follow progress; the response includes `github_context` and `github_pr_url` once the PR is opened.

### Dependency model

An issue is **ready** iff it has zero open sub-issues (the official GitHub sub-issues API). Repos that don't use sub-issues treat every open issue as ready. Other conventions (task lists, "depends on #N" body text) are out of scope for now.

### What the team writes back

1. A `Coding team started job <id>` comment on the issue when work begins.
2. A draft PR with `Closes #<num>` against the repo's default branch when work succeeds.
3. A `Draft PR opened: <url>` comment (or `Reusing existing draft PR: <url>` on retry).

If anything fails — branch prep, the orchestrator, fast-forward, or push — the failure is recorded on the job's `error` field and a best-effort comment is posted on the issue.

### Required configuration

| Env var | Purpose |
|---|---|
| `GITHUB_TOKEN` | Fallback when no `github_token` is in the body. Needs `Issues: read/write`, `Pull requests: read/write`, `Contents: read/write`, `Metadata: read` (or classic `repo`). |
| `GITHUB_API_URL` | Optional. Defaults to `https://api.github.com`; override for GitHub Enterprise. |

`repo_path` must be an existing local working tree of the same repo with `origin` configured for write. The team **never** clones for you, and it pushes the integration branch with `--force-with-lease` so partial-failure retries replace the prior branch tip cleanly.

### Concurrency

Only one job per `(owner, repo, issue_number)` may be running at a time; a second concurrent call returns `409 already running for ...`. Sequential retries (after a failed job is terminal) are safe.

## Khala platform

This package is part of the [Khala](../../../README.md) monorepo (Unified API, Angular UI, and full team index).
