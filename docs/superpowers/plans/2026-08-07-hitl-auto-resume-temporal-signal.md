# HITL Auto-Resume Temporal Signal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `_try_auto_resume` signal `CodingTeamWorkflow` with `submit_answers` when the job carries a `resume_token`, instead of claim+spawn.

**Architecture:** Early Temporal branch in `_try_auto_resume` after terminal + `waiting_for_user` checks and before heartbeat deferral: signal with the same payload as `/resume`; on success return `True`; on any signal exception log and return `False`. Thread-mode path unchanged.

**Tech Stack:** Python, `signal_workflow_sync`, pytest + monkeypatch.

**Spec:** `docs/superpowers/specs/2026-08-07-hitl-auto-resume-temporal-signal-design.md`

## Global Constraints

- Mode detection: presence of `resume_token` on the job record.
- Workflow id: `f"{WORKFLOW_ID_PREFIX}{job_id}"` with `WORKFLOW_ID_PREFIX = "coding_team-"`.
- Signal: `"submit_answers"` with payload `{"resume_token": ..., "answers": data.get("submitted_answers") or []}`.
- Signal failures: catch, log with `exc_info`, return `False` (preserve never-raises contract).
- Do not extract a shared helper with `/resume` or `/answers`.
- Do not modify `/resume` or the Temporal `/answers` inline signal path.
- Do not delete claim APIs or thread-mode spawn paths.
- Never reference GitHub issue numbers in code, comments, or commit messages.
- Design by Contract: update `_try_auto_resume` docstring preconditions/postconditions.
- Work exclusively in `.worktrees/3995-hitl-auto-resume-signal` on `feature/3995-hitl-auto-resume-temporal-signal`.
- Run tests from worktree `backend/` with main-repo venv:
  `/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest …`

---

## File map

| File | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/api/orchestration.py` | Temporal branch in `_try_auto_resume` + imports + docstring |
| `backend/agents/software_engineering_team/tests/test_coding_team_api_hitl.py` | Unit tests for Temporal auto-resume signal + failure |

No new modules. Patch `signal_workflow_sync` on the `orchestration` module (where it will be imported), not on the route module.

---

### Task 1: Failing tests for Temporal `_try_auto_resume`

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_coding_team_api_hitl.py` (append after `test_auto_resume_refuses_non_paused_job`, ~line 901)
- Test: same file

**Interfaces:**
- Consumes: `_job(...)`, `api._try_auto_resume`, `software_engineering_team.api.orchestration` as patch target
- Produces: `test_auto_resume_temporal_native_signals_workflow`, `test_auto_resume_temporal_signal_failure_returns_false`

- [ ] **Step 1: Write the failing tests**

Insert after `test_auto_resume_refuses_non_paused_job`:

```python
def test_auto_resume_temporal_native_signals_workflow(monkeypatch):
    """A Temporal-native pause (resume_token) must wake CodingTeamWorkflow via
    submit_answers instead of claim+spawn."""
    from software_engineering_team.api import orchestration as orch

    answers = [{"question_id": "q1", "selected_option_id": "strict"}]
    job = _job(
        resume_token="j1:tok-1",
        submitted_answers=answers,
        status="waiting_for_user",
    )

    signaled: Dict[str, Any] = {}
    monkeypatch.setattr(
        orch,
        "signal_workflow_sync",
        lambda workflow_id, signal, payload: signaled.update(
            workflow_id=workflow_id, signal=signal, payload=payload
        ),
    )

    def _must_not_spawn(*_a, **_k):  # pragma: no cover - Temporal path only
        raise AssertionError("claim/spawn path must not run for Temporal-native auto-resume")

    monkeypatch.setattr(orch, "_claim_and_spawn_resume", _must_not_spawn)
    monkeypatch.setattr(api, "_answer_wait_heartbeat_fresh", lambda data: False)
    monkeypatch.setattr(api, "_recover_resume_plan", _must_not_spawn)
    monkeypatch.setattr(api, "_resolve_github_job_token", _must_not_spawn)

    assert api._try_auto_resume("j1", job) is True
    assert signaled["workflow_id"] == "coding_team-j1"
    assert signaled["signal"] == "submit_answers"
    assert signaled["payload"] == {"resume_token": "j1:tok-1", "answers": answers}


def test_auto_resume_temporal_signal_failure_returns_false(monkeypatch):
    """signal_workflow_sync failures must degrade to False (never raise) so /answers
    and the recheck timer can fall back to the manual-resume hint."""
    from software_engineering_team.api import orchestration as orch

    job = _job(resume_token="j1:tok-2", status="waiting_for_user")

    def _boom(*_a, **_k):
        raise RuntimeError("temporal client unavailable")

    monkeypatch.setattr(orch, "signal_workflow_sync", _boom)

    def _must_not_spawn(*_a, **_k):  # pragma: no cover
        raise AssertionError("must not fall through to claim/spawn after a signal failure")

    monkeypatch.setattr(orch, "_claim_and_spawn_resume", _must_not_spawn)
    monkeypatch.setattr(api, "_answer_wait_heartbeat_fresh", lambda data: False)

    assert api._try_auto_resume("j1", job) is False
```

Note: `signal_workflow_sync` is not yet imported in `orchestration.py`. The first test will fail at import/attribute time or AttributeError when the branch is missing — either is RED. After Task 2 adds the import + branch, both go GREEN.

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/3995-hitl-auto-resume-signal/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_api_hitl.py::test_auto_resume_temporal_native_signals_workflow \
  agents/software_engineering_team/tests/test_coding_team_api_hitl.py::test_auto_resume_temporal_signal_failure_returns_false \
  -v
```

Expected: FAIL — Temporal jobs currently fall through to claim+spawn (or hit `_must_not_spawn`).

- [ ] **Step 3: Commit the failing tests**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/3995-hitl-auto-resume-signal
git add backend/agents/software_engineering_team/tests/test_coding_team_api_hitl.py
git commit -m "$(cat <<'EOF'
Add failing tests for Temporal-native auto-resume signaling.

EOF
)"
```

---

### Task 2: Implement Temporal branch in `_try_auto_resume`

**Files:**
- Modify: `backend/agents/software_engineering_team/api/orchestration.py`
- Test: `backend/agents/software_engineering_team/tests/test_coding_team_api_hitl.py`

**Interfaces:**
- Consumes: `signal_workflow_sync`, `WORKFLOW_ID_PREFIX`
- Produces: Temporal early return `True`/`False` from `_try_auto_resume`

- [ ] **Step 1: Add imports**

Near the top of `orchestration.py` (with other imports), add:

```python
from shared.temporal.runner import signal_workflow_sync
from software_engineering_team.temporal.coding_team_constants import WORKFLOW_ID_PREFIX
```

- [ ] **Step 2: Replace `_try_auto_resume` docstring and insert Temporal branch**

Replace the existing docstring with:

```python
    """Best-effort resume of a paused job after answers arrived (or a deferred recheck).

    Two paths, told apart by whether ``data`` carries a ``resume_token`` (set only by a
    ``pause_strategy="return"`` pause):

    - **Temporal-native pause** (``resume_token`` present): signal the running
      ``CodingTeamWorkflow`` with ``submit_answers``, carrying ``resume_token`` and
      already-stored ``submitted_answers`` (or ``[]``). No heartbeat deferral, plan
      recovery, GitHub-token resolution, or claim+spawn. Signal delivery failures are
      logged and become ``False`` so this function's never-raises contract holds.
    - **Thread-mode / GitHub-hook pause** (``resume_token`` absent): unchanged — defer to
      a fresh answer-wait heartbeat (with recheck), or claim and spawn the orchestrator
      (hook path for GitHub-issue jobs).

    Preconditions:
        - ``data`` is the job record for ``job_id`` and the caller observed the run thread
          as not alive in this process (thread-mode callers); Temporal callers may invoke
          this with a waiting Temporal-native job as a safety net / recheck.
    Postconditions:
        - Returns True when the run is resuming (Temporal signal accepted; a live wait loop
          heartbeated recently — with a deferred recheck scheduled; a thread was spawned
          here; or another caller holds the start claim).
        - Returns False when the job is terminal, not paused, a Temporal signal failed, the
          record lacks a usable ``repo_path``/``plan_input``, a GitHub-issue job has no
          token, or the thread could not be started.
        - Never raises for Temporal signal failures or any documented ``ResumeSpawnResult``
          outcome; raises ``RuntimeError`` only if ``_claim_and_spawn_resume`` returns an
          unrecognized outcome (exhaustiveness guard).
    """
```

Insert **immediately after** the `waiting_for_user` status check / warning block and **before** `if _main._answer_wait_heartbeat_fresh(data):`:

```python
    resume_token = data.get("resume_token")
    if resume_token:
        try:
            signal_workflow_sync(
                f"{WORKFLOW_ID_PREFIX}{job_id}",
                "submit_answers",
                {
                    "resume_token": resume_token,
                    "answers": data.get("submitted_answers") or [],
                },
            )
        except Exception:
            logger.error(
                "Auto-resume for job %s skipped: Temporal submit_answers signal failed.",
                job_id,
                exc_info=True,
            )
            return False
        return True
```

Leave the remainder of `_try_auto_resume` unchanged.

- [ ] **Step 3: Run the new tests — expect PASS**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/3995-hitl-auto-resume-signal/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_api_hitl.py::test_auto_resume_temporal_native_signals_workflow \
  agents/software_engineering_team/tests/test_coding_team_api_hitl.py::test_auto_resume_temporal_signal_failure_returns_false \
  agents/software_engineering_team/tests/test_coding_team_api_hitl.py::test_answers_temporal_native_signals_workflow_and_appends_without_clearing \
  -v
```

Expected: all PASS (including existing `/answers` Temporal AC coverage).

- [ ] **Step 4: Run the full HITL API module — expect PASS**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/3995-hitl-auto-resume-signal/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_api_hitl.py -q
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/3995-hitl-auto-resume-signal
git add \
  backend/agents/software_engineering_team/api/orchestration.py \
  backend/agents/software_engineering_team/tests/test_coding_team_api_hitl.py
git commit -m "$(cat <<'EOF'
Signal CodingTeamWorkflow from Temporal-native auto-resume.

EOF
)"
```

---

## Spec coverage self-review

| Spec requirement | Task |
|---|---|
| Mode-branch on `resume_token` in `_try_auto_resume` | Task 2 |
| Signal after terminal + waiting, before heartbeat | Task 2 insertion point |
| Payload matches `/resume` | Task 1 + Task 2 |
| Catch signal failure → False | Task 1 failure test + Task 2 |
| Skip claim/spawn/heartbeat/plan/token on Temporal path | Task 1 guards + Task 2 early return |
| Thread-mode unchanged | Task 2 leaves remainder; Task 2 Step 4 full module |
| Docstring dual-mode contract | Task 2 |
| `/answers` Temporal AC still covered | Task 2 Step 3 includes existing test |
| No shared helper / no `/resume` edits | Global constraints |

Placeholder scan: none. Patch target is `orchestration.signal_workflow_sync` (local import), consistent across tasks.
