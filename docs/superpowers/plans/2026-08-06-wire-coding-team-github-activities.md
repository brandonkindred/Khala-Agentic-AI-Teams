# Wire CodingTeamWorkflow to GitHub Activities Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drive GitHub-issue runs through Temporal: optional `github` start payload → branch prep once → pipeline pause/resume loop → publish (or failure-notice on prep failure / pipeline exception), with `POST /run-from-github` starting `CodingTeamWorkflow` instead of `_start_hook_thread`.

**Architecture:** Workflow-orchestrated activities. The API builds a token-free `github` block on the workflow payload. Activities already resolve tokens activity-side. Plain `/run` (no `github`) stays pipeline-only.

**Tech Stack:** Python 3.10+, Temporal (`temporalio` workflow/activities + `WorkflowEnvironment`), FastAPI route tests, pytest

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-06-wire-coding-team-github-activities-design.md`
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only)
- Design-by-Contract docstrings on changed public functions / workflow `run`
- Never put a plaintext GitHub token in the workflow start payload or activity args
- Failure notice only on branch-prep `ok=False` and pipeline activity exception (`kind="failure"`)
- Branch prep runs once before the first pipeline attempt
- Leave `_run_with_github_hooks` in the tree; stop calling it from the route
- Deferred: start/prep/pause/did-not-complete comments, sibling checkout check, nothing-merged short-circuit
- Work from worktree `.worktrees/issue-3993-wire-github-activities` on `fix/3993-wire-github-activities`
- Pytest: `cd backend && PYTHONPATH=. /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest …`

## File map

| File | Role |
|---|---|
| `temporal/coding_team_start_workflow.py` | Optional `github` on start payload |
| `temporal/coding_team_workflow.py` | GitHub path in `CodingTeamWorkflow.run` |
| `api/routes/github.py` | Start Temporal workflow from `/run-from-github` |
| `tests/test_coding_team_start_workflow.py` | Start-helper payload tests |
| `tests/test_coding_team_temporal_workflow.py` | Unit + integration workflow tests |
| `tests/test_coding_team_github_source.py` | Route dispatch + fixture updates |

---

### Task 1: Extend `start_coding_team_workflow` with optional `github`

**Files:**
- Modify: `backend/agents/software_engineering_team/temporal/coding_team_start_workflow.py`
- Test: `backend/agents/software_engineering_team/tests/test_coding_team_start_workflow.py`

**Interfaces:**
- Consumes: existing `start_coding_team_workflow(job_id, repo_path, plan_input)`
- Produces: `start_coding_team_workflow(job_id: str, repo_path: str, plan_input: Optional[Dict[str, Any]], github: Optional[Dict[str, Any]] = None) -> None` — when `github` is a non-empty dict, include it on the workflow payload under key `"github"`; never include a `"token"` key

- [ ] **Step 1: Write the failing tests**

Append to `test_coding_team_start_workflow.py`:

```python
def test_start_coding_team_workflow_includes_github_block(monkeypatch):
    captured: dict = {}

    def _fake_start_workflow_sync(workflow_run, *args, workflow_id, task_queue):
        captured["args"] = args

    monkeypatch.setattr(sw, "start_workflow_sync", _fake_start_workflow_sync)

    github = {
        "owner": "acme",
        "repo": "widgets",
        "issue_number": 9,
        "issue_title": "Fix it",
        "remote": "origin",
        "base": "main",
        "integration_branch": "khala/issue-9",
        "cleanup_checkout_on_success": False,
    }
    sw.start_coding_team_workflow("job-7", "/repo", {"objective": "x"}, github=github)

    (payload,) = captured["args"]
    assert payload["github"] == github
    assert "token" not in payload
    assert "token" not in payload["github"]


def test_start_coding_team_workflow_omits_github_when_none(monkeypatch):
    captured: dict = {}

    def _fake_start_workflow_sync(workflow_run, *args, workflow_id, task_queue):
        captured["args"] = args

    monkeypatch.setattr(sw, "start_workflow_sync", _fake_start_workflow_sync)

    sw.start_coding_team_workflow("job-7", "/repo", {"objective": "x"}, github=None)
    (payload,) = captured["args"]
    assert "github" not in payload
```

Update the existing `test_start_coding_team_workflow_forwards_run_payload_id_and_queue` expectation only if needed (default `github=None` must keep payload without `"github"`).

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && PYTHONPATH=. /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_start_workflow.py -v
```

Expected: FAIL — `start_coding_team_workflow` does not accept `github=` yet (`TypeError`).

- [ ] **Step 3: Implement**

In `coding_team_start_workflow.py`, change the signature and payload build to:

```python
def start_coding_team_workflow(
    job_id: str,
    repo_path: str,
    plan_input: Optional[Dict[str, Any]],
    github: Optional[Dict[str, Any]] = None,
) -> None:
    """Start ``CodingTeamWorkflow`` for a coding-team job.

    Preconditions:
        - ``job_id`` is a non-empty str whose job row already exists (the API
          called ``create_job`` before dispatching).
        - ``repo_path`` is a non-empty str; ``plan_input`` is a JSON-serializable
          plan dict (a run with no plan has nothing to execute).
        - ``github``, when provided, is a dict of GitHub-issue run metadata for
          the workflow (owner/repo/issue/base/integration_branch/...). It must
          not contain a plaintext token — activities resolve tokens activity-side.
    Postconditions:
        - A workflow with id ``coding_team-<job_id>`` is started on the coding
          team task queue (fire-and-forget; the caller polls
          ``GET /status/{job_id}``). When ``github`` is a non-empty dict it is
          included on the payload under ``\"github\"``; otherwise that key is
          omitted. Raises ``RuntimeError`` if the worker's Temporal client never
          becomes available within the wait window.
    """
    assert job_id, "start_coding_team_workflow requires a non-empty job_id"
    assert repo_path, "start_coding_team_workflow requires a non-empty repo_path"
    payload: Dict[str, Any] = {
        "job_id": job_id,
        "repo_path": repo_path,
        "plan_input": plan_input,
    }
    if github:
        assert "token" not in github, "github workflow payload must not include a token"
        payload["github"] = github
    workflow_id = f"{WORKFLOW_ID_PREFIX}{job_id}"
    start_workflow_sync(
        CodingTeamWorkflow.run,
        payload,
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started CodingTeamWorkflow id=%s", workflow_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Same pytest command as Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/agents/software_engineering_team/temporal/coding_team_start_workflow.py \
  backend/agents/software_engineering_team/tests/test_coding_team_start_workflow.py
git commit -m "$(cat <<'EOF'
Allow optional github metadata on coding-team workflow start.

EOF
)"
```

---

### Task 2: Wire GitHub activities into `CodingTeamWorkflow.run` (unit TDD)

**Files:**
- Modify: `backend/agents/software_engineering_team/temporal/coding_team_workflow.py`
- Test: `backend/agents/software_engineering_team/tests/test_coding_team_temporal_workflow.py`

**Interfaces:**
- Consumes: `request.get("github")` optional dict; activities `github_branch_prep_activity`, `github_publish_activity`, `github_failure_notice_activity`, `run_pipeline_activity`
- Produces: GitHub control flow per spec — prep → pipeline loop → publish / failure-notice

- [ ] **Step 1: Write failing unit tests**

Add helpers + tests to `test_coding_team_temporal_workflow.py` (after the existing non-integration tests, before the WorkflowEnvironment section):

```python
_GITHUB = {
    "owner": "acme",
    "repo": "widgets",
    "issue_number": 9,
    "issue_title": "Fix the widget",
    "remote": "origin",
    "base": "main",
    "integration_branch": "khala/issue-9",
    "cleanup_checkout_on_success": False,
}


def _github_request(**overrides):
    req = {
        "job_id": "job-1",
        "repo_path": "/repo",
        "plan_input": {"objective": "ship"},
        "github": dict(_GITHUB),
    }
    req.update(overrides)
    return req


def test_github_run_calls_prep_then_pipeline_then_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    from software_engineering_team.temporal.coding_team_github_activities import (
        github_branch_prep_activity,
        github_publish_activity,
    )
    from software_engineering_team.temporal.coding_team_workflow import run_pipeline_activity

    workflow_obj = CodingTeamWorkflow()
    results = [
        {"ok": True, "error": None, "notes": []},
        {"job_id": "job-1", "status": "completed"},
        {"job_id": "job-1", "status": "completed", "github_pr_url": "https://example/pull/1"},
    ]
    calls, snapshots = _patch_execute(monkeypatch, results)

    async def _no_wait(*_a, **_kw):
        raise AssertionError("wait_condition must not be called")

    monkeypatch.setattr("temporalio.workflow.wait_condition", _no_wait)

    result = asyncio.run(workflow_obj.run(_github_request()))

    assert [c[0] for c in calls] == [
        github_branch_prep_activity,
        run_pipeline_activity,
        github_publish_activity,
    ]
    assert snapshots[0]["job_id"] == "job-1"
    assert snapshots[0]["default_branch"] == "main"
    assert snapshots[0]["integration_branch"] == "khala/issue-9"
    assert "token" not in snapshots[0]
    assert snapshots[2]["issue_title"] == "Fix the widget"
    assert "token" not in snapshots[2]
    assert result["github_pr_url"] == "https://example/pull/1"


def test_github_prep_failure_calls_failure_notice_skips_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from software_engineering_team.temporal.coding_team_github_activities import (
        github_branch_prep_activity,
        github_failure_notice_activity,
    )

    workflow_obj = CodingTeamWorkflow()
    results = [
        {"ok": False, "error": "unsafe ref", "notes": []},
        {"job_id": "job-1", "status": "failed"},
    ]
    calls, snapshots = _patch_execute(monkeypatch, results)
    monkeypatch.setattr(
        "temporalio.workflow.wait_condition",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("no wait")),
    )

    result = asyncio.run(workflow_obj.run(_github_request()))

    assert [c[0] for c in calls] == [
        github_branch_prep_activity,
        github_failure_notice_activity,
    ]
    assert snapshots[1]["kind"] == "failure"
    assert "unsafe" in snapshots[1]["message"]
    assert snapshots[1]["number"] == 9
    assert "token" not in snapshots[1]
    assert result["status"] == "failed"


def test_github_pipeline_exception_calls_failure_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    from software_engineering_team.temporal.coding_team_github_activities import (
        github_branch_prep_activity,
        github_failure_notice_activity,
    )
    from software_engineering_team.temporal.coding_team_workflow import run_pipeline_activity

    workflow_obj = CodingTeamWorkflow()
    calls: list = []

    async def _fake_exec(fn, request, **_kw):
        calls.append(fn)
        if fn is github_branch_prep_activity:
            return {"ok": True, "error": None, "notes": []}
        if fn is run_pipeline_activity:
            raise RuntimeError("orchestrator boom")
        if fn is github_failure_notice_activity:
            return {"job_id": "job-1", "status": "failed"}
        raise AssertionError(f"unexpected activity {fn}")

    monkeypatch.setattr("temporalio.workflow.execute_activity", _fake_exec)
    monkeypatch.setattr(
        "temporalio.workflow.wait_condition",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("no wait")),
    )

    with pytest.raises(RuntimeError, match="orchestrator boom"):
        asyncio.run(workflow_obj.run(_github_request()))

    assert calls == [
        github_branch_prep_activity,
        run_pipeline_activity,
        github_failure_notice_activity,
    ]


def test_github_failed_pipeline_status_skips_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    from software_engineering_team.temporal.coding_team_github_activities import (
        github_branch_prep_activity,
    )
    from software_engineering_team.temporal.coding_team_workflow import run_pipeline_activity

    workflow_obj = CodingTeamWorkflow()
    results = [
        {"ok": True, "error": None, "notes": []},
        {"job_id": "job-1", "status": "failed", "error": "timed out"},
    ]
    calls, _ = _patch_execute(monkeypatch, results)
    monkeypatch.setattr(
        "temporalio.workflow.wait_condition",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("no wait")),
    )

    result = asyncio.run(workflow_obj.run(_github_request()))

    assert [c[0] for c in calls] == [github_branch_prep_activity, run_pipeline_activity]
    assert result["status"] == "failed"
```

- [ ] **Step 2: Run the new tests to verify they fail**

```bash
cd backend && PYTHONPATH=. /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_temporal_workflow.py::test_github_run_calls_prep_then_pipeline_then_publish \
  agents/software_engineering_team/tests/test_coding_team_temporal_workflow.py::test_github_prep_failure_calls_failure_notice_skips_pipeline \
  agents/software_engineering_team/tests/test_coding_team_temporal_workflow.py::test_github_pipeline_exception_calls_failure_notice \
  agents/software_engineering_team/tests/test_coding_team_temporal_workflow.py::test_github_failed_pipeline_status_skips_publish \
  -v
```

Expected: FAIL — workflow still only calls `run_pipeline_activity`.

- [ ] **Step 3: Implement workflow GitHub path**

In `coding_team_workflow.py`, replace `CodingTeamWorkflow.run` body with logic equivalent to:

```python
    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        # ... update docstring Preconditions/Postconditions for optional github ...
        github = request.get("github")
        activity_timeout = timedelta(hours=4)
        github_timeout = timedelta(minutes=30)

        if isinstance(github, dict) and github:
            prep = await workflow.execute_activity(
                github_branch_prep_activity,
                {
                    "job_id": request["job_id"],
                    "repo_path": request["repo_path"],
                    "remote": github.get("remote") or "origin",
                    "default_branch": github["base"],
                    "integration_branch": github["integration_branch"],
                    "issue_number": github.get("issue_number"),
                },
                start_to_close_timeout=github_timeout,
            )
            if not prep.get("ok"):
                return await workflow.execute_activity(
                    github_failure_notice_activity,
                    {
                        "job_id": request["job_id"],
                        "owner": github["owner"],
                        "repo": github["repo"],
                        "number": github["issue_number"],
                        "message": f"branch prep failed: {prep.get('error')}",
                        "kind": "failure",
                    },
                    start_to_close_timeout=github_timeout,
                )

        try:
            result = await workflow.execute_activity(
                run_pipeline_activity,
                request,
                start_to_close_timeout=activity_timeout,
            )
            while result.get("outcome") == "paused":
                # ... existing pause/resume loop unchanged ...
                pass
        except Exception as exc:
            if isinstance(github, dict) and github:
                await workflow.execute_activity(
                    github_failure_notice_activity,
                    {
                        "job_id": request["job_id"],
                        "owner": github["owner"],
                        "repo": github["repo"],
                        "number": github["issue_number"],
                        "message": str(exc),
                        "kind": "failure",
                    },
                    start_to_close_timeout=github_timeout,
                )
            raise

        if isinstance(github, dict) and github:
            status = result.get("status")
            if status in ("failed", "cancelled", "waiting_for_user"):
                return result
            return await workflow.execute_activity(
                github_publish_activity,
                {
                    "job_id": request["job_id"],
                    "owner": github["owner"],
                    "repo": github["repo"],
                    "repo_path": request["repo_path"],
                    "issue_number": github["issue_number"],
                    "issue_title": github["issue_title"],
                    "base": github["base"],
                    "integration_branch": github["integration_branch"],
                    "remote": github.get("remote") or "origin",
                    "cleanup_checkout_on_success": bool(
                        github.get("cleanup_checkout_on_success")
                    ),
                },
                start_to_close_timeout=github_timeout,
            )
        return result
```

Keep the existing pause/resume loop body intact inside the `try` (do not leave a `pass` stub — copy the current while-loop verbatim). Update the `run` docstring to document the GitHub path and that `"token"` must never appear on activity args.

Ensure existing unit tests that call `run` without `github` still pass (single pipeline activity).

- [ ] **Step 4: Run unit workflow tests**

```bash
cd backend && PYTHONPATH=. /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_temporal_workflow.py -v -m "not integration"
```

Expected: PASS (all non-integration tests in the file).

- [ ] **Step 5: Commit**

```bash
git add backend/agents/software_engineering_team/temporal/coding_team_workflow.py \
  backend/agents/software_engineering_team/tests/test_coding_team_temporal_workflow.py
git commit -m "$(cat <<'EOF'
Wire GitHub hook activities into CodingTeamWorkflow.

EOF
)"
```

---

### Task 3: Integration test — branch prep → pipeline → publish

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_coding_team_temporal_workflow.py`

**Interfaces:**
- Consumes: `_workflow_environment_worker`, fake activities under real Temporal names
- Produces: integration test proving acceptance criterion

- [ ] **Step 1: Write the failing integration test**

Append after the existing pause/resume integration test:

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_workflow_github_path_prep_pipeline_publish() -> None:
    """Acceptance: GitHub-issue job runs branch prep → pipeline → publish via Temporal."""
    from temporalio import activity
    from temporalio.worker import Replayer

    from software_engineering_team.temporal.coding_team_constants import TASK_QUEUE
    from software_engineering_team.temporal.coding_team_workflow import CodingTeamWorkflow

    calls: list[str] = []

    @activity.defn(name="coding_team_github_branch_prep")
    def _fake_prep(request: dict) -> dict:
        calls.append("prep")
        assert request["job_id"] == "gh-job-1"
        assert "token" not in request
        return {"ok": True, "error": None, "notes": []}

    @activity.defn(name="coding_team_run_pipeline")
    def _fake_pipeline(request: dict) -> dict:
        calls.append("pipeline")
        assert request.get("github")
        return {"job_id": "gh-job-1", "status": "completed"}

    @activity.defn(name="coding_team_github_publish")
    def _fake_publish(request: dict) -> dict:
        calls.append("publish")
        assert "token" not in request
        assert request["integration_branch"] == "khala/issue-9"
        return {
            "job_id": "gh-job-1",
            "status": "completed",
            "github_pr_url": "https://example/pull/9",
        }

    @activity.defn(name="coding_team_github_failure_notice")
    def _fake_failure(request: dict) -> dict:  # pragma: no cover - must not run
        calls.append("failure")
        raise AssertionError("failure notice must not run on happy path")

    request = {
        "job_id": "gh-job-1",
        "repo_path": "/tmp/repo",
        "plan_input": {"objective": "ship it"},
        "github": {
            "owner": "acme",
            "repo": "widgets",
            "issue_number": 9,
            "issue_title": "Fix the widget",
            "remote": "origin",
            "base": "main",
            "integration_branch": "khala/issue-9",
            "cleanup_checkout_on_success": False,
        },
    }

    async with _workflow_environment_worker(
        activities=[_fake_prep, _fake_pipeline, _fake_publish, _fake_failure]
    ) as env:
        handle = await env.client.start_workflow(
            CodingTeamWorkflow.run,
            request,
            id="coding-team-workflow-github-happy-path",
            task_queue=TASK_QUEUE,
        )
        result = await asyncio.wait_for(handle.result(), timeout=30)
        history = await handle.fetch_history()

    assert calls == ["prep", "pipeline", "publish"]
    assert result["github_pr_url"] == "https://example/pull/9"
    await Replayer(workflows=[CodingTeamWorkflow]).replay_workflow(history)
```

- [ ] **Step 2: Run integration test**

```bash
cd backend && PYTHONPATH=. /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_temporal_workflow.py::test_workflow_github_path_prep_pipeline_publish \
  -v
```

If Task 2 is done, this should PASS. If run before Task 2, expect FAIL / wrong call order. Prefer implementing after Task 2 so Step 2 is GREEN; if RED due to missing activity registration in worker list, fix by ensuring all four fakes are passed (worker uses only the substitute list).

- [ ] **Step 3: Commit**

```bash
git add backend/agents/software_engineering_team/tests/test_coding_team_temporal_workflow.py
git commit -m "$(cat <<'EOF'
Add WorkflowEnvironment test for GitHub prep-pipeline-publish path.

EOF
)"
```

---

### Task 4: Switch `/run-from-github` to Temporal + update tests

**Files:**
- Modify: `backend/agents/software_engineering_team/api/routes/github.py`
- Modify: `backend/agents/software_engineering_team/tests/test_coding_team_github_source.py`

**Interfaces:**
- Consumes: `start_coding_team_workflow(..., github=...)` from Task 1
- Produces: route no longer calls `_start_hook_thread`

- [ ] **Step 1: Update route tests first (TDD)**

In `test_coding_team_github_source.py`, the `patched_app` fixture currently forces `_start_hook_thread` → sync `_run_with_github_hooks`. Change the fixture so `/run-from-github` exercises Temporal dispatch:

1. Replace the `_start_hook_thread` monkeypatch with a capture of `start_coding_team_workflow` (or patch `software_engineering_team.api.routes.github` / `coding_team_main` where the route imports it).

Route imports `_main` and will need to call start via `_main` re-export or a direct import. Prefer importing in the route:

```python
from software_engineering_team.temporal.coding_team_start_workflow import (
    start_coding_team_workflow,
)
```

Then in the fixture, patch that symbol on the routes module (or on `_main` if re-exported).

Add / update a focused test:

```python
def test_run_from_github_starts_coding_team_workflow(patched_app, monkeypatch):
    started = {}

    def _capture(job_id, repo_path, plan_input, github=None):
        started["job_id"] = job_id
        started["repo_path"] = repo_path
        started["plan_input"] = plan_input
        started["github"] = github

    # Patch wherever the route resolves start_coding_team_workflow
    import software_engineering_team.api.routes.github as gh_routes

    monkeypatch.setattr(gh_routes, "start_coding_team_workflow", _capture)
    # Ensure hook thread is NOT used:
    monkeypatch.setattr(
        patched_app["api"],
        "_start_hook_thread",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("hook thread must not run")),
    )

    # FakeClient must expose get_repo().default_branch for base resolution
    ...
    resp = patched_app["client"].post("/run-from-github", json=_body(...))
    assert resp.status_code == 200
    assert started["github"]["integration_branch"] == "khala/issue-1"
    assert started["github"]["base"] == "main"  # or fake default
    assert "token" not in started["github"]
```

Existing tests that relied on synchronous `_run_with_github_hooks` completing publish inside the HTTP request will break. For each such test either:
- Point it at Temporal start capture + assert job row / start args (preferred for route-level tests), or
- Keep a separate unit/integration path for `_run_with_github_hooks` if those tests specifically target hook behavior (leave them calling `_run_with_github_hooks` directly, not via the route).

Run the github_source suite after fixture edits and fix failures until green with the new dispatch (implement route in Step 3 in the same task cycle: RED on new test → implement route → fix remaining suite).

- [ ] **Step 2: Run route test to verify RED (before route change) or confirm target**

```bash
cd backend && PYTHONPATH=. /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_github_source.py::test_run_from_github_starts_coding_team_workflow \
  -v
```

Expected before implementation: FAIL (still calls hook thread / no capture).

- [ ] **Step 3: Implement route change**

In `api/routes/github.py`, after `update_job`:

```python
    # Resolve base before dispatch so the workflow payload is complete and
    # activities never need a plaintext token from the workflow args.
    with _main.GitHubClient(token=token) as client_for_base:
        default_branch = client_for_base.get_repo(request.owner, request.repo).default_branch
    base = request.base_branch or default_branch
    integration_branch = f"khala/issue-{issue.number}"

    from software_engineering_team.temporal.coding_team_start_workflow import (
        start_coding_team_workflow,
    )

    start_coding_team_workflow(
        job_id,
        request.repo_path,
        plan.model_dump(),
        github={
            "owner": request.owner,
            "repo": request.repo,
            "issue_number": issue.number,
            "issue_title": issue.title,
            "remote": request.remote,
            "base": base,
            "integration_branch": integration_branch,
            "cleanup_checkout_on_success": request.cleanup_checkout_on_success,
        },
    )
```

Remove the `_start_hook_thread(...)` call. Prefer a top-of-file import for `start_coding_team_workflow` if it does not create import cycles (match existing route import style). Reuse the existing `with GitHubClient` block earlier in the function for `get_repo` if one is already open — avoid a second client session when the first block can yield `default_branch` before exit. Concrete approach: inside the existing `with _main.GitHubClient(token=token) as client:` that already fetches the issue, also read `default_branch = client.get_repo(...).default_branch` before leaving the block, then use it after job create.

- [ ] **Step 4: Run github_source + start_workflow + temporal unit tests**

```bash
cd backend && PYTHONPATH=. /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_github_source.py \
  agents/software_engineering_team/tests/test_coding_team_start_workflow.py \
  agents/software_engineering_team/tests/test_coding_team_temporal_workflow.py -m "not integration" \
  -v --tb=short
```

Expected: PASS. Fix any fixture/tests still asserting hook-thread side effects on the HTTP path.

- [ ] **Step 5: Commit**

```bash
git add backend/agents/software_engineering_team/api/routes/github.py \
  backend/agents/software_engineering_team/tests/test_coding_team_github_source.py
git commit -m "$(cat <<'EOF'
Start CodingTeamWorkflow from run-from-github instead of hook thread.

EOF
)"
```

---

### Task 5: Final verification

**Files:** (none new)

- [ ] **Step 1: Run full in-scope suite including integration**

```bash
cd backend && PYTHONPATH=. /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_start_workflow.py \
  agents/software_engineering_team/tests/test_coding_team_temporal_workflow.py \
  agents/software_engineering_team/tests/test_coding_team_github_source.py \
  -v --tb=short
```

Expected: PASS (integration may skip if Temporal test server unavailable — that skip is acceptable; do not treat skip as failure unless the happy-path unit tests also fail).

- [ ] **Step 2: Ruff changed files**

```bash
cd backend && /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/ruff check \
  agents/software_engineering_team/temporal/coding_team_start_workflow.py \
  agents/software_engineering_team/temporal/coding_team_workflow.py \
  agents/software_engineering_team/api/routes/github.py \
  agents/software_engineering_team/tests/test_coding_team_start_workflow.py \
  agents/software_engineering_team/tests/test_coding_team_temporal_workflow.py \
  agents/software_engineering_team/tests/test_coding_team_github_source.py
```

Expected: clean. Fix + new commit only if needed.

- [ ] **Step 3: Confirm acceptance**

- Integration (or unit equivalent if server skipped) proves prep → pipeline → publish order.
- Route starts Temporal with `github` and no `token`.
- No plaintext token in workflow/activity args from this path.
