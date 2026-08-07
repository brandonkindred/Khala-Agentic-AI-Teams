# Delete HITL Claim/Heartbeat/Thread Machinery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make coding-team `/resume` Temporal-only and delete claim/heartbeat/liveness thread-spawn machinery from `orchestration.py`, with tests updated so CI stays green.

**Architecture:** Slim `coding_team_hitl.py` routes first (no callers of claim/spawn), then delete the dead helpers and re-exports, then purge obsolete tests and verify production grep is clean.

**Tech Stack:** FastAPI, pytest, existing `signal_workflow_sync`.

**Spec:** `docs/superpowers/specs/2026-08-07-hitl-delete-claim-machinery-design.md`

## Global Constraints

- `/resume` without `resume_token` → HTTP 400 (Temporal-native only).
- `/answers` without `resume_token` → store answers only; no `_try_auto_resume`, no spawn-oriented status_text.
- Delete: `_claim_and_spawn_resume`, `ResumeSpawnResult`, `_schedule_resume_recheck`, `_RESUME_RECHECK_DELAY_S` (if only used by recheck), `_spawn_run_thread`, `_start_orchestrator_thread`, `_start_github_resume_thread`, `_start_hook_thread`, `_try_auto_resume`.
- Keep: `_running_job_for_issue`; `job_store` claim APIs (sibling).
- Never reference GitHub issue numbers in code, comments, or commit messages.
- Work in `.worktrees/3996-hitl-delete-claim-machinery` on `feature/3996-hitl-delete-claim-machinery`.
- Pytest via main-repo venv from worktree `backend/`:
  `/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest …`
- Rebase onto `origin/main` if behind before final verification.

---

## File map

| File | Responsibility |
|---|---|
| `api/routes/coding_team_hitl.py` | Temporal-only resume; store-only non-Temporal answers |
| `api/orchestration.py` | Delete listed helpers |
| `api/coding_team_main.py` | Drop re-exports |
| `tests/test_coding_team_api_hitl.py` | New route tests; delete obsolete claim/spawn/thread tests |
| Other tests referencing deleted symbols | Fix or delete |

---

### Task 1: Slim HITL routes (Temporal-only resume; store-only answers)

**Files:**
- Modify: `backend/agents/software_engineering_team/api/routes/coding_team_hitl.py`
- Modify: `backend/agents/software_engineering_team/tests/test_coding_team_api_hitl.py`

**Interfaces:**
- Consumes: `signal_workflow_sync`, `WORKFLOW_ID_PREFIX`, `hitl`, `_main` store helpers
- Produces: `/resume` and `/answers` with no calls to `_claim_and_spawn_resume` / `_try_auto_resume`

- [ ] **Step 1: Add failing tests for new route behavior**

Append (or place near Temporal resume tests):

```python
def test_resume_400_when_no_resume_token(monkeypatch):
    """Without resume_token, /resume must not claim/spawn — Temporal-native only."""
    from software_engineering_team.api.routes import coding_team_hitl as hitl_route

    monkeypatch.setattr(api, "get_job", lambda jid: _job(status="waiting_for_user"))
    monkeypatch.setattr(
        hitl_route,
        "signal_workflow_sync",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not signal")),
    )

    r = client.post("/run/j1/resume")
    assert r.status_code == 400
    detail = r.json()["detail"].lower()
    assert "resume_token" in detail or "temporal" in detail


def test_answers_without_resume_token_stores_only_no_auto_resume(monkeypatch):
    """Block-mode /answers stores answers and must not call auto-resume or signal."""
    from software_engineering_team.api.routes import coding_team_hitl as hitl_route

    job = _job()  # no resume_token
    monkeypatch.setattr(api, "get_job", lambda jid: job)
    stored: Dict[str, Any] = {}
    monkeypatch.setattr(
        api, "store_submit_answers", lambda jid, answers: stored.update(answers=answers)
    )
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)

    def _must_not(*_a, **_k):  # pragma: no cover
        raise AssertionError("must not auto-resume or signal for block-mode answers")

    monkeypatch.setattr(api, "_try_auto_resume", _must_not)
    monkeypatch.setattr(hitl_route, "signal_workflow_sync", _must_not)
    monkeypatch.setattr(api, "update_job", _must_not)  # no spawn-oriented status_text

    r = client.post(
        "/run/j1/answers",
        json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]},
    )
    assert r.status_code == 200
    assert stored["answers"][0]["question_id"] == "q1"
```

Note: after Task 1 implementation, `_must_not` on `_try_auto_resume` may become unnecessary if the call is gone; keep the signal/update_job guards. If `update_job` is still used elsewhere in the success path for non-Temporal answers, drop that guard and only assert no `_try_auto_resume` / no signal — prefer asserting `store_submit_answers` was called and `_try_auto_resume` was not.

- [ ] **Step 2: Run new tests — expect FAIL**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/3996-hitl-delete-claim-machinery/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_api_hitl.py::test_resume_400_when_no_resume_token \
  agents/software_engineering_team/tests/test_coding_team_api_hitl.py::test_answers_without_resume_token_stores_only_no_auto_resume \
  -v
```

Expected: FAIL (current `/resume` without token still claim/spawns or returns differently; `/answers` still auto-resumes).

- [ ] **Step 3: Implement route changes**

Replace `resume_job` body after the terminal check with Temporal-only logic. Full function shape:

```python
@router.post("/run/{job_id}/resume", response_model=RunResponse)
def resume_job(job_id: str) -> RunResponse:
    """Resume a Temporal-native paused coding-team job by signaling CodingTeamWorkflow.

    Only jobs with a ``resume_token`` (pause_strategy=\"return\") can be resumed here.
    Thread-mode claim/spawn is removed.

    Preconditions:
        - Job exists, is not terminal, has ``resume_token``, and ``status`` is
          ``waiting_for_user``.
    Postconditions:
        - Raises 404 / 400 as documented; on success delivers ``submit_answers`` to
          ``coding_team-{job_id}`` and returns ``\"Job resumed.\"``.
    """
    data = _main.get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")
    if hitl.is_terminal(data):
        raise HTTPException(
            status_code=400,
            detail=f"Job is {data.get('status', 'terminal')} and cannot be resumed.",
        )
    resume_token = data.get("resume_token")
    if not resume_token:
        raise HTTPException(
            status_code=400,
            detail=(
                "Job has no resume_token; only a Temporal-native paused job "
                "(waiting_for_user with resume_token) can be resumed."
            ),
        )
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

Remove `ResumeSpawnResult` import from this module.

Replace the non-Temporal half of `submit_pending_answers` (everything after the `if resume_token:` block) with:

```python
    _main.store_submit_answers(job_id, answers)
    return _main.get_status(job_id)
```

Update `submit_pending_answers` docstring: Temporal path unchanged; non-Temporal path only stores answers (no auto-resume / thread restart).

- [ ] **Step 4: Run new tests + Temporal signal tests — expect PASS**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/3996-hitl-delete-claim-machinery/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_api_hitl.py::test_resume_400_when_no_resume_token \
  agents/software_engineering_team/tests/test_coding_team_api_hitl.py::test_answers_without_resume_token_stores_only_no_auto_resume \
  agents/software_engineering_team/tests/test_coding_team_api_hitl.py::test_resume_temporal_native_signals_workflow \
  agents/software_engineering_team/tests/test_coding_team_api_hitl.py::test_answers_temporal_native_signals_workflow_and_appends_without_clearing \
  -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/agents/software_engineering_team/api/routes/coding_team_hitl.py \
  backend/agents/software_engineering_team/tests/test_coding_team_api_hitl.py
git commit -m "$(cat <<'EOF'
Make coding-team /resume Temporal-only; stop answers auto-spawn.

EOF
)"
```

---

### Task 2: Delete orchestration claim/spawn/thread helpers

**Files:**
- Modify: `backend/agents/software_engineering_team/api/orchestration.py`
- Modify: `backend/agents/software_engineering_team/api/coding_team_main.py`

**Interfaces:**
- Produces: listed symbols gone from production modules; `_running_job_for_issue` retained

- [ ] **Step 1: Delete symbols from `orchestration.py`**

Remove entire definitions of:

- `_spawn_run_thread`
- `_start_orchestrator_thread`
- `_start_github_resume_thread`
- `_schedule_resume_recheck` and `_RESUME_RECHECK_DELAY_S` (and any constant only used by recheck)
- `ResumeSpawnResult`
- `_claim_and_spawn_resume`
- `_try_auto_resume`
- `_start_hook_thread`

Keep `_running_job_for_issue`, `plan_from_input`, `run_orchestrator_wired`, GitHub-hook run flow helpers (`_run_with_github_hooks`, `_record_failure`, etc.), `_recover_resume_plan`, `_resolve_github_job_token` unless a definition becomes unused *and* was only supporting deleted resume spawn (do not delete activity/GitHub helpers still used by Temporal).

Remove now-unused imports (e.g. `RESUME_CLAIM_TTL_S` if only claim path used it; `threading` if no longer needed in this module — verify before removing).

Update module docstring if it still advertises auto-resume/claim.

- [ ] **Step 2: Drop re-exports from `coding_team_main.py`**

From the `orchestration` import block, remove:

```python
_RESUME_RECHECK_DELAY_S,  # if present
_claim_and_spawn_resume,
_schedule_resume_recheck,
_start_github_resume_thread,
_start_hook_thread,
_start_orchestrator_thread,
_try_auto_resume,
```

Keep `_running_job_for_issue`, `_recover_resume_plan`, `_resolve_github_job_token`, etc.

- [ ] **Step 3: Grep production code**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/3996-hitl-delete-claim-machinery
rg -n '_claim_and_spawn_resume|ResumeSpawnResult|_schedule_resume_recheck|_spawn_run_thread|_start_orchestrator_thread|_start_github_resume_thread|_start_hook_thread|_try_auto_resume|_RESUME_RECHECK_DELAY_S' \
  backend/agents/software_engineering_team \
  --glob '!**/tests/**'
```

Expected: **no matches** (or only acceptable historical comments you already updated). Fix any remaining production hits before committing.

- [ ] **Step 4: Commit**

```bash
git add backend/agents/software_engineering_team/api/orchestration.py \
  backend/agents/software_engineering_team/api/coding_team_main.py
git commit -m "$(cat <<'EOF'
Delete coding-team resume claim and thread-spawn helpers.

EOF
)"
```

---

### Task 3: Purge obsolete tests and verify suite

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_coding_team_api_hitl.py`
- Modify: any other test files that import/call deleted symbols (search with `rg`)

- [ ] **Step 1: Find remaining test references**

```bash
rg -n '_claim_and_spawn_resume|ResumeSpawnResult|_schedule_resume_recheck|_spawn_run_thread|_start_orchestrator_thread|_start_github_resume_thread|_start_hook_thread|_try_auto_resume|_RESUME_RECHECK_DELAY_S' \
  backend/agents/software_engineering_team/tests
```

- [ ] **Step 2: Delete or rewrite obsolete tests**

In `test_coding_team_api_hitl.py`, **delete** tests whose sole purpose is claim/spawn/recheck/thread-resume behavior. Known candidates (verify against Step 1 output; delete each listed if still present):

- `test_start_orchestrator_thread_clears_claim_if_registration_fails`
- `test_answers_dead_thread_auto_resumes`
- `test_answers_dead_thread_adds_resume_hint_when_unresumable`
- `test_answers_dead_thread_claim_store_error_falls_back_to_hint`
- `test_answers_does_not_resume_job_cancelled_after_get` (if only about auto-resume cancel race)
- `test_answers_deferred_claim_schedules_post_ttl_recheck`
- `test_resume_400_when_plan_input_corrupted` / `invalid` / claim 500s / thread_claim_lost / unhandled spawn / github hook resume paths / noop thread alive / spawns orchestrator / claim release on activity clear / post_claim abort / github_resume_thread_advances… — any test that requires thread-mode `/resume`
- All `test_auto_resume_*` except none remain (Temporal auto-resume tests go away with `_try_auto_resume`)
- Recheck/heartbeat-resume coupling tests that only exist to drive `_schedule_resume_recheck` + spawn

**Keep:** status tests, answers validation tests, Temporal `/answers` + `/resume` signal tests, `test_resume_400_when_terminal*`, `test_resume_404`, heartbeat helper unit tests that do not call deleted APIs, format/status surface tests unrelated to spawn.

**Rewrite:** `test_answers_thread_mode_unaffected_when_no_resume_token` → align with store-only behavior (may already match Task 1’s new test; dedupe if redundant).

Also fix `test_coding_team_github_source.py` (or similar) if it monkeypatches `_start_hook_thread`.

Remove autouse fixture `_default_resume_claim` if nothing left needs `claim_resume` monkeypatches in this file — only if all remaining tests do not use claim.

- [ ] **Step 3: Run full HITL API module**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/3996-hitl-delete-claim-machinery/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_api_hitl.py -q
```

Expected: all PASS.

- [ ] **Step 4: Broader grep + related tests**

```bash
rg -n '_claim_and_spawn_resume|ResumeSpawnResult|_schedule_resume_recheck|_spawn_run_thread|_start_orchestrator_thread|_start_github_resume_thread|_start_hook_thread|_try_auto_resume' \
  backend/agents/software_engineering_team
# Fix any remaining test hits; production must stay clean.

# Run any other test files you had to edit:
# /Users/brandonkindred/.../backend/.venv/bin/python -m pytest <those files> -q
```

- [ ] **Step 5: Commit**

```bash
git add backend/agents/software_engineering_team/tests
git commit -m "$(cat <<'EOF'
Remove obsolete claim/spawn/auto-resume HITL route tests.

EOF
)"
```

---

## Spec coverage self-review

| Spec requirement | Task |
|---|---|
| `/resume` Temporal-only; no token → 400 | Task 1 |
| `/answers` store-only without token | Task 1 |
| Delete listed helpers (+ `_try_auto_resume`) | Task 2 |
| Keep `_running_job_for_issue` | Task 2 |
| Drop re-exports | Task 2 |
| Production grep clean | Task 2 Step 3 + Task 3 |
| Tests updated; Temporal tests kept | Task 1 + Task 3 |
| job_store claim APIs untouched | Global constraints |

Placeholder scan: none. Exact 400 detail string for missing `resume_token` is specified in Task 1 Step 3.
