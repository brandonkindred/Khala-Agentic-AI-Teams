---
name: README and DevOps Triggers
overview: Fix why backend and frontend README.md files stay empty (documentation agent trigger and repo path behavior), and change the orchestrator so the DevOps agent is triggered by the tech lead after all backend tasks and after all frontend tasks, writing containerization/deployment code into each repo.
todos: []
isProject: false
---

# README documentation fix and DevOps trigger after backend/frontend

## Part 1: Why backend and frontend README.md stay empty

### Current behavior

- **Backend**: When a backend task completes, the documentation agent is triggered from inside the backend workflow (`[backend_agent/agent.py](software_engineering_team/backend_agent/agent.py)` ~691–701) with `repo_path=repo_path` (i.e. the **backend repo** `path/backend`). The tech lead first decides via LLM (`should_update_docs`) in `[trigger_documentation_update](software_engineering_team/tech_lead_agent/agent.py)` (tech_lead_agent/agent.py); only if `should_update_docs` is true does the doc agent run.
- **Frontend**: When a frontend task completes, `[_run_tech_lead_review](software_engineering_team/orchestrator.py)` is called with `repo_path=frontend_dir` ([orchestrator.py](software_engineering_team/orchestrator.py) ~734–737). Again, the tech lead’s `should_update_docs` gates the doc agent; when it runs, it uses `path = frontend_dir`.

In both cases the doc agent sees a **single repo** (backend or frontend only). So `path/frontend` and `path/backend` do not exist inside that repo, and the agent only updates the **root** README of that repo (`backend_dir/README.md` or `frontend_dir/README.md`). The design is “one README per repo”; the problem is that those READMEs are still empty.

### Likely causes

1. **Tech Lead gate**
  `[TECH_LEAD_TRIGGER_DOCS_PROMPT](software_engineering_team/tech_lead_agent/prompts.py)` lets the LLM return `should_update_docs: false`. If it often says “no”, the doc agent is never run, so READMEs are never written.
2. **No “always update when empty” rule**
  There is no logic that forces a doc run when the **repo’s** README is missing or empty. So even for the first backend/frontend task, the tech lead can skip docs.
3. **Doc agent only runs per task**
  If every task gets `should_update_docs: false`, there is no later pass that runs the doc agent once per repo to fill READMEs.
4. **Possible doc-agent failures**
  When the doc agent does run, branch creation, LLM output (empty or invalid JSON), or write/merge can fail; these are non-blocking and only logged, so README could still be empty even when the agent is triggered.

### Recommended fixes (Part 1)

- **Force trigger when README is empty**  
In `[trigger_documentation_update](software_engineering_team/tech_lead_agent/agent.py)`: before or after calling the LLM, check the repo’s `README.md` (at `repo_path`). If it is missing or empty and the completed task is `backend` or `frontend`, set `should_update_docs = True` regardless of the LLM (or treat the LLM as advisory only when README is empty). This guarantees at least one doc run per repo when there’s no README yet.
- **Optional: bias the Tech Lead prompt**  
In `[TECH_LEAD_TRIGGER_DOCS_PROMPT](software_engineering_team/tech_lead_agent/prompts.py)`: add an instruction such as: “If the repository’s README.md is missing or empty, you MUST set should_update_docs to true so that documentation can be created.”
- **Optional: final documentation pass**  
In the orchestrator, after both backend and frontend workers have finished (same place where security runs, ~793–824): run the documentation agent once for the backend repo and once for the frontend repo with a dedicated “update all project documentation / ensure README and key sections exist” task. Use the same `repo_path` and codebase/spec/architecture context as today. This ensures READMEs are at least attempted even if every per-task decision was “no” or some runs failed.
- **Logging**  
Add or keep clear logs when skipping doc update (e.g. “Tech Lead: should_update_docs=false”) and when doc agent fails (branch/create/write/merge). This will make it obvious if empty READMEs are due to “never triggered” vs “triggered but failed.”

---

## Part 2: Trigger DevOps after backend and frontend completion

### Current behavior

- DevOps runs in the **prefix queue** with git_setup ([orchestrator.py](software_engineering_team/orchestrator.py) ~~457–472): `prefix_queue` includes all `devops` tasks, and they run **before** any backend or frontend tasks. DevOps output is written to the **work path** with `write_agent_output(path, result, subdir="devops")` (~~526), i.e. `path/devops/` (e.g. `path/devops/Dockerfile`, `path/devops/.github/workflows/ci.yml`). So:
  - There is no backend or frontend application code yet when DevOps runs.
  - All DevOps artifacts live under the work path’s `devops/` folder, not inside the backend or frontend **repos**.

### Desired behavior

- **Trigger 1**: When **all backend tasks** are done, the tech lead (orchestrator) triggers the DevOps agent to add **backend** containerization and deployment (e.g. Dockerfile, CI, deploy) for the backend app, and write that into the **backend repo** (and optionally keep/update shared bits under work path if needed).
- **Trigger 2**: When **all frontend tasks** are done, trigger the DevOps agent to add **frontend** containerization and deployment and write into the **frontend repo**.

So the DevOps code that containerizes and deploys each app should live in each repo in a way that can successfully build and run images (e.g. `backend/Dockerfile`, `frontend/Dockerfile`).

### Implementation outline

1. **Remove or avoid early “full devops” in prefix**
  - Either remove the single devops task from the prefix queue when it’s meant to be “containerize everything” (so we don’t run DevOps with no app code), or keep a minimal prefix devops task only for things that don’t depend on app code (e.g. repo-level .gitignore, optional base CI skeleton). The main containerization/deployment work should move to the two new trigger points below.
2. **Detect “all backend tasks done” and “all frontend tasks done”**
  - Backend and frontend run in parallel workers. When the **backend worker** finishes its loop (no more tasks in `backend_queue`), all backend tasks are “done” for that run. When the **frontend worker** finishes its loop, all frontend tasks are done.
  - Trigger points:
    - **Backend**: Right after `_backend_worker()` returns (backend queue empty). Run DevOps once for “backend” and write to `backend_dir`.
    - **Frontend**: Right after `_frontend_worker()` returns. Run DevOps once for “frontend” and write to `frontend_dir`.
  - Because the two workers run in parallel, you have two options:
    - **Option A**: After both threads join (~757), run two sequential DevOps invocations: one for backend (with backend codebase context), one for frontend (with frontend codebase context). Simple and avoids races; order can be backend then frontend.
    - **Option B**: From inside each worker, when that worker is about to exit (queue empty), trigger DevOps for that repo only. That requires care so only one “backend devops” and one “frontend devops” run (e.g. a “devops for backend already run” flag), and may duplicate orchestrator logic. Option A is simpler and recommended.
3. **DevOps agent input/output for “target repo”**
  - Extend `[DevOpsInput](software_engineering_team/devops_agent/models.py)` (or equivalent) with a clear way to indicate **target**: e.g. `target_repo: "backend" | "frontend"` (and optionally `"shared"` if you keep a shared devops folder). This tells the agent which app it is containerizing (Python/FastAPI vs Node/Angular).
  - Update `[DEVOPS_PROMPT](software_engineering_team/devops_agent/prompts.py)` so the agent:
    - Produces a **Dockerfile** (and any app-specific CI/deploy steps) appropriate for that target (e.g. backend: Python, run with uvicorn; frontend: Node build, serve with nginx or Node).
    - Optionally produces a small **docker-compose** or instructions for the **single** service when target is backend or frontend; or a separate “full stack” compose can live at work path later.
  - When writing output:
    - For **backend** run: call `write_agent_output(backend_dir, result, subdir="")` so that e.g. `Dockerfile`, `.github/workflows/ci.yml` land in the backend repo root (or a subdir the agent specifies via artifacts). That way the backend repo is self-contained for build/deploy.
    - For **frontend** run: same with `frontend_dir` and `subdir=""` so the frontend repo gets its own Dockerfile and CI.
4. **Shared/orchestration (optional)**
  - If you want one place that orchestrates both services (e.g. `docker-compose` at work path that builds backend + frontend images), that can be a separate follow-up DevOps run or a small generator after both backend and frontend DevOps have run, using the Dockerfiles already in each repo.
5. **Tech Lead “trigger”**
  - You can keep the tech lead in the loop by having the orchestrator call a small method on the tech lead (e.g. `tech_lead.trigger_devops_for_backend(...)` / `trigger_devops_for_frontend(...)`) that builds the DevOpsInput (task description, requirements, architecture, existing pipeline, tech_stack, `target_repo="backend"` or `"frontend"`) and then invokes the DevOps agent and `write_agent_output`. That way “triggered by the tech lead” is reflected in the control flow and prompts (e.g. “Tech Lead requested backend containerization after all backend tasks completed”).

---

## Summary


| Area                 | Change                                                                                                                                                                                                                               |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **README empty**     | Force `should_update_docs = true` when the repo’s README is missing/empty for backend/frontend tasks; optionally tighten Tech Lead prompt and add a final doc pass per repo.                                                         |
| **DevOps timing**    | Stop relying on a single early DevOps run in the prefix; trigger DevOps **after all backend tasks** and **after all frontend tasks**.                                                                                                |
| **DevOps placement** | Run DevOps twice (backend, frontend), writing output into **backend repo** and **frontend repo** respectively (e.g. `backend_dir/Dockerfile`, `frontend_dir/Dockerfile`), so each repo can be containerized and deployed on its own. |
| **DevOps input**     | Add a `target_repo` (or equivalent) to DevOps input and prompt so the agent produces the right stack (Python/FastAPI vs Node/Angular) and deployable artifacts.                                                                      |


This keeps documentation and DevOps behavior aligned with the actual repo layout (separate backend and frontend repos) and ensures READMEs are created when empty and DevOps runs when app code exists, with artifacts in the right repos for containerization and deployment.