# HITL `/resume` Temporal Signal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `POST /run/{job_id}/resume` signal `CodingTeamWorkflow` with `submit_answers` when the job carries a `resume_token`, instead of claim+spawn.

**Architecture:** Early mode-branch in `resume_job` mirroring `/answers`: after 404/terminal checks, a truthy `resume_token` takes the Temporal path (waiting-status gate → `signal_workflow_sync` → `"Job resumed."`). Jobs without `resume_token` keep today's claim+spawn path unchanged.

**Tech Stack:** FastAPI route, `signal_workflow_sync` (`shared.temporal.runner`), pytest + `TestClient`, monkeypatch.

**Spec:** `docs/superpowers/specs/2026-08-07-hitl-resume-temporal-signal-design.md`

## Global Constraints

- Mode detection: presence of `resume_token` on the job record (same as `/answers`).
- Workflow id: `f"{WORKFLOW_ID_PREFIX}{job_id}"` with `WORKFLOW_ID_PREFIX = "coding_team-"`.
- Signal: `"submit_answers"` with payload `{"resume_token": ..., "answers": data.get("submitted_answers") or []}`.
- Terminal / missing-job error responses must stay unchanged.
- Do not extract a shared helper with `/answers` in this change.
- Do not modify `_try_auto_resume`, claim APIs, or delete thread-mode spawn paths.
- Never reference GitHub issue numbers in code, comments, or commit messages.
- Design by Contract: update `resume_job`'s docstring preconditions/postconditions for dual-mode behavior.
- Work exclusively in the worktree `.worktrees/3994-hitl-resume-signal` on branch `feature/3994-hitl-resume-temporal-signal`.
- Run tests from `backend/` with the project venv: `../backend/.venv/bin/python -m pytest …` if this worktree has no `.venv`, or `make test` targets once deps are available. Prefer:
  `cd backend && .venv/bin/python -m pytest agents/software_engineering_team/tests/test_coding_team_api_hitl.py::TEST -v`
  using the main-repo venv at `/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv` when the worktree lacks one (set cwd to the worktree's `backend/`).

---

## File map

| File | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/api/routes/coding_team_hitl.py` | `resume_job` dual-mode branch + docstring |
| `backend/agents/software_engineering_team/tests/test_coding_team_api_hitl.py` | Route tests for Temporal signal path + terminal-with-token |

No new modules.

---

### Task 1: Failing route tests for Temporal `/resume`

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_coding_team_api_hitl.py` (append near the existing `/answers: Temporal-native pause` section at end of file, after `test_answers_thread_mode_unaffected_when_no_resume_token`)
- Test: same file

**Interfaces:**
- Consumes: `_job(...)`, `client`, `api`, `hitl_route.signal_workflow_sync` monkeypatch pattern from `test_answers_temporal_native_signals_workflow_and_appends_without_clearing`
- Produces: `test_resume_temporal_native_signals_workflow`, `test_resume_400_when_terminal_even_with_resume_token`

- [ ] **Step 1: Write the failing tests**

Append to `backend/agents/software_engineering_team/tests/test_coding_team_api_hitl.py`:

```python
# --------------------------------------------------------------------------- /resume: Temporal-native pause


def test_resume_temporal_native_signals_workflow(monkeypatch):
    """A Temporal-native pause (resume_token on the job) must wake CodingTeamWorkflow
    via submit_answers instead of claim+spawn."""
    from software_engineering_team.api.routes import coding_team_hitl as hitl_route

    answers = [{"question_id": "q1", "selected_option_id": "strict"}]
    job = _job(
        resume_token="j1:tok-1",
        submitted_answers=answers,
        status="waiting_for_user",
    )
    monkeypatch.setattr(api, "get_job", lambda jid: job)

    signaled: Dict[str, Any] = {}
    monkeypatch.setattr(
        hitl_route,
        "signal_workflow_sync",
        lambda workflow_id, signal, payload: signaled.update(
            workflow_id=workflow_id, signal=signal, payload=payload
        ),
    )

    def _must_not_spawn(*_a, **_k):  # pragma: no cover - Temporal path only
        raise AssertionError("claim/spawn path must not run for a Temporal-native resume")

    monkeypatch.setattr(api, "_claim_and_spawn_resume", _must_not_spawn)
    monkeypatch.setattr(api, "_is_run_thread_alive", _must_not_spawn)
    monkeypatch.setattr(api, "_answer_wait_heartbeat_fresh", _must_not_spawn)
    monkeypatch.setattr(api, "_recover_resume_plan", _must_not_spawn)
    monkeypatch.setattr(api, "_resolve_github_job_token", _must_not_spawn)

    r = client.post("/run/j1/resume")

    assert r.status_code == 200
    assert r.json()["message"] == "Job resumed."
    assert r.json()["status"] == "running"
    assert signaled["workflow_id"] == "coding_team-j1"
    assert signaled["signal"] == "submit_answers"
    assert signaled["payload"] == {"resume_token": "j1:tok-1", "answers": answers}


def test_resume_temporal_native_signals_empty_answers_when_none_stored(monkeypatch):
    """If submitted_answers is missing, the signal still carries answers: []."""
    from software_engineering_team.api.routes import coding_team_hitl as hitl_route

    job = _job(resume_token="j1:tok-2", status="waiting_for_user")
    # _job has no submitted_answers key
    monkeypatch.setattr(api, "get_job", lambda jid: job)

    signaled: Dict[str, Any] = {}
    monkeypatch.setattr(
        hitl_route,
        "signal_workflow_sync",
        lambda workflow_id, signal, payload: signaled.update(
            workflow_id=workflow_id, signal=signal, payload=payload
        ),
    )
    monkeypatch.setattr(
        api,
        "_claim_and_spawn_resume",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("must not claim/spawn")
        ),
    )

    r = client.post("/run/j1/resume")

    assert r.status_code == 200
    assert signaled["payload"] == {"resume_token": "j1:tok-2", "answers": []}


def test_resume_400_when_terminal_even_with_resume_token(monkeypatch):
    """Terminal check runs before the Temporal branch; resume_token must not change the 400."""
    from software_engineering_team.api.routes import coding_team_hitl as hitl_route

    def _must_not_signal(*_a, **_k):  # pragma: no cover
        raise AssertionError("must not signal a terminal job")

    monkeypatch.setattr(hitl_route, "signal_workflow_sync", _must_not_signal)
    monkeypatch.setattr(
        api,
        "_claim_and_spawn_resume",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )

    for status in ("completed", "completed_with_failures", "failed", "cancelled"):
        monkeypatch.setattr(
            api, "get_job", lambda jid, s=status: _job(status=s, resume_token="j1:tok-x")
        )
        r = client.post("/run/j1/resume")
        assert r.status_code == 400
        assert "cannot be resumed" in r.json()["detail"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run from worktree backend (use main-repo venv if needed):

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/3994-hitl-resume-signal/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_api_hitl.py::test_resume_temporal_native_signals_workflow \
  agents/software_engineering_team/tests/test_coding_team_api_hitl.py::test_resume_temporal_native_signals_empty_answers_when_none_stored \
  agents/software_engineering_team/tests/test_coding_team_api_hitl.py::test_resume_400_when_terminal_even_with_resume_token \
  -v
```

Expected: FAIL — Temporal cases hit `_must_not_spawn` / claim path (or fail asserting `signaled`), because `resume_job` does not yet branch on `resume_token`. Terminal-with-token may already PASS (terminal check is first); that is OK — it locks the acceptance criterion.

- [ ] **Step 3: Commit the failing tests**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/3994-hitl-resume-signal
git add backend/agents/software_engineering_team/tests/test_coding_team_api_hitl.py
git commit -m "$(cat <<'EOF'
Add failing route tests for Temporal-native /resume signaling.

EOF
)"
```

---

### Task 2: Implement Temporal early branch in `resume_job`

**Files:**
- Modify: `backend/agents/software_engineering_team/api/routes/coding_team_hitl.py` (`resume_job`, after the terminal check ~lines 128–132)
- Test: `backend/agents/software_engineering_team/tests/test_coding_team_api_hitl.py`

**Interfaces:**
- Consumes: `signal_workflow_sync` (already imported), `WORKFLOW_ID_PREFIX` (already imported), `hitl.WAITING_STATUS`, `RunResponse`
- Produces: Temporal path returns `RunResponse(job_id, status="running", message="Job resumed.")`

- [ ] **Step 1: Insert the Temporal branch and update the docstring**

Replace the `resume_job` docstring and insert the branch immediately after the terminal check (before the thread-liveness check).

Docstring (full replacement of the current `resume_job` docstring):

```python
    """Resume a paused coding-team job.

    Two paths, told apart by whether the job record carries a ``resume_token``
    (set only by a ``pause_strategy="return"`` pause):

    - **Temporal-native pause** (``resume_token`` present): signal the running
      ``CodingTeamWorkflow`` with ``submit_answers``, carrying the job's
      ``resume_token`` and already-stored ``submitted_answers`` (or ``[]``).
      No claim, thread spawn, plan recovery, or GitHub-token resolution.
    - **Thread-mode / GitHub-hook pause** (``resume_token`` absent): unchanged —
      no-op when a live thread or fresh wait-loop heartbeat exists; otherwise
      claim and spawn the orchestrator (hook path for GitHub-issue jobs).

    Authentication/authorization is enforced by the unified API security gateway
    in front of all team mounts; like every other coding-team route, this
    endpoint assumes that perimeter.

    Preconditions:
        - The job exists and is not terminal.
        - For the Temporal path: ``status`` is ``waiting_for_user``.
        - For the thread-mode path: once liveness can't be proven, the job is
          paused in ``waiting_for_user``, has recoverable plan/repo_path, and
          (for GitHub-issue jobs) a usable token.
    Postconditions:
        - Raises 404 (unknown job) or 400 (terminal / not paused / thread-mode
          plan or token failures) with the same details as before this branch.
        - Temporal path: delivers ``submit_answers`` to
          ``coding_team-{job_id}`` and returns ``"Job resumed."``.
        - Thread-mode path: returns ``"already running"`` without spawning when
          a live thread, fresh heartbeat, or concurrent claim exists; otherwise
          spawns and returns ``"Job resumed."``.
    """
```

Implementation to insert after the terminal `HTTPException` block and **before**
`if _main._is_run_thread_alive(...)`:

```python
    resume_token = data.get("resume_token")
    if resume_token:
        if data.get("status") != hitl.WAITING_STATUS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Job is {data.get('status', 'in an unknown state')}, not paused waiting for "
                    "answers; only a paused (waiting_for_user) job can be resumed."
                ),
            )
        signal_workflow_sync(
            f"{WORKFLOW_ID_PREFIX}{job_id}",
            "submit_answers",
            {
                "resume_token": resume_token,
                "answers": data.get("submitted_answers") or [],
            },
        )
        return RunResponse(job_id=job_id, status="running", message="Job resumed.")
```

Leave the remainder of `resume_job` (liveness → claim+spawn) unchanged.

- [ ] **Step 2: Run the new tests — expect PASS**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/3994-hitl-resume-signal/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_api_hitl.py::test_resume_temporal_native_signals_workflow \
  agents/software_engineering_team/tests/test_coding_team_api_hitl.py::test_resume_temporal_native_signals_empty_answers_when_none_stored \
  agents/software_engineering_team/tests/test_coding_team_api_hitl.py::test_resume_400_when_terminal_even_with_resume_token \
  agents/software_engineering_team/tests/test_coding_team_api_hitl.py::test_resume_400_when_terminal \
  -v
```

Expected: all PASS.

- [ ] **Step 3: Run the full HITL API test module — expect PASS**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/3994-hitl-resume-signal/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_api_hitl.py -v
```

Expected: all PASS (thread-mode `/resume` tests unchanged).

- [ ] **Step 4: Commit**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/3994-hitl-resume-signal
git add \
  backend/agents/software_engineering_team/api/routes/coding_team_hitl.py \
  backend/agents/software_engineering_team/tests/test_coding_team_api_hitl.py
git commit -m "$(cat <<'EOF'
Signal CodingTeamWorkflow from Temporal-native /resume.

EOF
)"
```

---

## Spec coverage self-review

| Spec requirement | Task |
|---|---|
| Mode-branch on `resume_token` | Task 2 |
| `signal_workflow_sync` to `coding_team-{job_id}` / `submit_answers` | Task 2 |
| Payload uses stored `submitted_answers` or `[]` | Task 1 tests + Task 2 |
| Skip claim/spawn/liveness/plan/GitHub on Temporal path | Task 1 asserts + Task 2 early return |
| Terminal 400 unchanged | Task 1 `test_resume_400_when_terminal_even_with_resume_token` + existing `test_resume_400_when_terminal` |
| Thread-mode path unchanged | Task 2 leaves remainder intact; Task 2 Step 3 full module run |
| Docstring dual-mode contract | Task 2 |
| No shared helper / no auto-resume / no claim deletion | Global constraints; not in any task |

Placeholder scan: none. Type/name consistency: `signal_workflow_sync`, `WORKFLOW_ID_PREFIX`, `RunResponse`, `hitl.WAITING_STATUS` match existing imports in `coding_team_hitl.py`.
