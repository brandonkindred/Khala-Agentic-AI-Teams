# Team-Lead `run_workflow` Template Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the duplicated backend/frontend team-lead `run_workflow` body into `BaseTeamLead._run_setup_and_delegate`, leaving each team lead as a thin wrapper that passes late-bound module globals.

**Architecture:** One protected helper on `BaseTeamLead` owns setup → lint/test gates → development-agent delegation → `copy_development_result_fields`. Subclass `run_workflow` methods pass `result_cls`, `run_setup_fn`, and `development_agent_cls` looked up in their own orchestrator module so existing monkeypatches keep working. Frontend adopts backend’s setup progress 2/3 `status_text` strings.

**Tech Stack:** Python 3.10+, pytest, ruff, existing `BaseTeamLead` / `copy_development_result_fields`.

**Spec:** `docs/superpowers/specs/2026-07-21-team-lead-run-workflow-template-design.md`

## Global Constraints

- Preserve monkeypatch surface: wrappers must pass module-level `run_setup` / `*DevelopmentAgent` looked up in each team’s orchestrator module (not class attributes bound at import time).
- Canonical setup `status_text`: progress 2 = `"Setting up repository and development environment"`; progress 3 = `"Repository setup complete"`; progress 5 unchanged.
- `_update_job` failures: DEBUG log (no silent `pass`).
- Do not change `copy_development_result_fields` field list or semantics.
- Do not migrate devops/coding_team onto this template.
- No GitHub issue numbers in code, comments, commit messages, or docs (PR body only).
- DbC: `_run_setup_and_delegate` gets `Preconditions:` / `Postconditions:` in its docstring; update `BaseTeamLead` module/class docs.
- ≥90% line coverage on touched files; `make lint` and relevant tests must pass from `backend/`.

## File map

| Path | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/shared/team_lead_base.py` | Add `_run_setup_and_delegate`; refresh module/class docstrings |
| `backend/agents/software_engineering_team/backend_code_v2_team/orchestrator.py` | Thin `BackendCodeV2TeamLead.run_workflow` wrapper |
| `backend/agents/software_engineering_team/frontend_code_v2_team/orchestrator.py` | Thin `FrontendCodeV2TeamLead.run_workflow` wrapper |
| `backend/agents/software_engineering_team/tests/test_team_lead_base.py` | Unit tests for `_run_setup_and_delegate` |

---

### Task 1: Failing tests for `_run_setup_and_delegate`

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_team_lead_base.py`

**Interfaces:**
- Consumes: (nothing yet — tests call `BaseTeamLead._run_setup_and_delegate` which does not exist)
- Produces: failing tests that lock helper contract (happy path, setup failure, lint/test gates, job_updater DEBUG, status_text)

- [ ] **Step 1: Append the new tests**

Append the following to `backend/agents/software_engineering_team/tests/test_team_lead_base.py` (keep existing tests unchanged). Add any missing imports at the top (`logging`, `SimpleNamespace` already present, plus `Task` / `TaskStatus` / `TaskType` / `SetupResult` / `Phase` / `caplog` via pytest):

```python
from software_engineering_team.shared.models import Task, TaskStatus, TaskType
from software_engineering_team.shared.v2_models import Phase, SetupResult


def _make_task(task_id: str = "t1") -> Task:
    return Task(
        id=task_id,
        type=TaskType.BACKEND,
        assignee="backend-code-v2",
        status=TaskStatus.PENDING,
        title="T",
        description="D",
    )


def _fake_result_cls(*, task_id: str):
    return SimpleNamespace(
        task_id=task_id,
        success=False,
        current_phase=Phase.SETUP,
        iterations_used=0,
        setup_result=None,
        planning_result=None,
        execution_result=None,
        review_result=None,
        problem_solving_result=None,
        documentation_result=None,
        deliver_result=None,
        final_files={},
        summary="",
        failure_reason="",
        needs_followup=False,
    )


def test_run_setup_and_delegate_happy_path_copies_fields(tmp_path):
    lead = _make_lead()
    task = _make_task()
    inner = _fake_result_cls(task_id=task.id)
    inner.success = True
    inner.current_phase = Phase.DELIVER
    inner.iterations_used = 2
    inner.summary = "done"
    inner.needs_followup = True
    inner.final_files = {"a.py": "x"}

    class _DevAgent:
        def __init__(self, _llm):
            pass

        def run_workflow(self, **_kwargs):
            return inner

    job_calls: list[dict] = []

    result = lead._run_setup_and_delegate(
        repo_path=tmp_path,
        task=task,
        result_cls=_fake_result_cls,
        run_setup_fn=lambda **_k: SetupResult(linting_configured=True, testing_configured=True),
        development_agent_cls=_DevAgent,
        job_updater=lambda **kwargs: job_calls.append(kwargs),
        merge_to_development=False,
    )

    assert result.success is True
    assert result.current_phase == Phase.DELIVER
    assert result.iterations_used == 2
    assert result.summary == "done"
    assert result.needs_followup is True
    assert result.final_files == {"a.py": "x"}
    assert result.setup_result is not None
    assert result.setup_result.linting_configured is True
    assert any(c.get("progress") == 2 for c in job_calls)
    assert any(c.get("progress") == 3 for c in job_calls)
    assert any(c.get("progress") == 5 for c in job_calls)


def test_run_setup_and_delegate_setup_exception_returns_early(tmp_path):
    lead = _make_lead()

    def boom(**_k):
        raise RuntimeError("disk full")

    result = lead._run_setup_and_delegate(
        repo_path=tmp_path,
        task=_make_task(),
        result_cls=_fake_result_cls,
        run_setup_fn=boom,
        development_agent_cls=type("NoAgent", (), {"__init__": lambda self, llm: None}),
    )

    assert result.success is False
    assert "Setup failed: disk full" in result.failure_reason


def test_run_setup_and_delegate_rejects_missing_linting(tmp_path):
    lead = _make_lead()
    result = lead._run_setup_and_delegate(
        repo_path=tmp_path,
        task=_make_task(),
        result_cls=_fake_result_cls,
        run_setup_fn=lambda **_k: SetupResult(linting_configured=False, testing_configured=True),
        development_agent_cls=type("NoAgent", (), {"__init__": lambda self, llm: None}),
    )
    assert "linting is not configured" in result.failure_reason.lower()


def test_run_setup_and_delegate_rejects_missing_testing(tmp_path):
    lead = _make_lead()
    result = lead._run_setup_and_delegate(
        repo_path=tmp_path,
        task=_make_task(),
        result_cls=_fake_result_cls,
        run_setup_fn=lambda **_k: SetupResult(linting_configured=True, testing_configured=False),
        development_agent_cls=type("NoAgent", (), {"__init__": lambda self, llm: None}),
    )
    assert "testing is not configured" in result.failure_reason.lower()


def test_run_setup_and_delegate_job_updater_failure_is_debug_logged(tmp_path, caplog):
    import logging

    lead = _make_lead()

    class _DevAgent:
        def __init__(self, _llm):
            pass

        def run_workflow(self, **_kwargs):
            return _fake_result_cls(task_id="t1")

    def bad_updater(**_kwargs):
        raise RuntimeError("job service down")

    with caplog.at_level(logging.DEBUG):
        result = lead._run_setup_and_delegate(
            repo_path=tmp_path,
            task=_make_task(),
            result_cls=_fake_result_cls,
            run_setup_fn=lambda **_k: SetupResult(linting_configured=True, testing_configured=True),
            development_agent_cls=_DevAgent,
            job_updater=bad_updater,
        )

    assert result is not None
    assert any("job_updater failed" in r.message for r in caplog.records)


def test_run_setup_and_delegate_emits_canonical_status_text(tmp_path):
    lead = _make_lead()
    job_calls: list[dict] = []

    class _DevAgent:
        def __init__(self, _llm):
            pass

        def run_workflow(self, **_kwargs):
            return _fake_result_cls(task_id="t1")

    lead._run_setup_and_delegate(
        repo_path=tmp_path,
        task=_make_task(),
        result_cls=_fake_result_cls,
        run_setup_fn=lambda **_k: SetupResult(linting_configured=True, testing_configured=True),
        development_agent_cls=_DevAgent,
        job_updater=lambda **kwargs: job_calls.append(kwargs),
    )

    by_progress = {c["progress"]: c for c in job_calls if "progress" in c}
    assert by_progress[2]["status_text"] == "Setting up repository and development environment"
    assert by_progress[3]["status_text"] == "Repository setup complete"
    assert by_progress[5]["status_text"] == "Linting and testing verified; ready for development"
```

Note: `_fake_result_cls` is a factory function used as `result_cls` — it must be callable as `result_cls(task_id=...)`.

- [ ] **Step 2: Run the new tests to verify they fail**

Run from `backend/`:

```bash
python -m pytest agents/software_engineering_team/tests/test_team_lead_base.py -k run_setup_and_delegate -v
```

Expected: FAIL with `AttributeError: 'BaseTeamLead' object has no attribute '_run_setup_and_delegate'` (or similar).

- [ ] **Step 3: Commit**

```bash
git add backend/agents/software_engineering_team/tests/test_team_lead_base.py
git commit -m "$(cat <<'EOF'
Add failing tests for BaseTeamLead setup-and-delegate helper.

EOF
)"
```

---

### Task 2: Implement `_run_setup_and_delegate` on `BaseTeamLead`

**Files:**
- Modify: `backend/agents/software_engineering_team/shared/team_lead_base.py`

**Interfaces:**
- Consumes: `copy_development_result_fields`, shared `Phase`, `Task`
- Produces: `BaseTeamLead._run_setup_and_delegate(...)` with the signature below

- [ ] **Step 1: Replace the module docstring and extend imports**

Replace the module docstring with:

```python
"""
Shared base for the code-v2 Team Leads (backend + frontend).

``BackendCodeV2TeamLead`` and ``FrontendCodeV2TeamLead`` share their
constructor, their per-repo incremental briefing cache lookup
(:meth:`BaseTeamLead._repo_context_cache_for`), the field-copy tail that
overlays their inner ``*DevelopmentAgent`` result onto their own result
object, and the setup → lint/test-gate → delegate sequence
(:meth:`BaseTeamLead._run_setup_and_delegate`).

Each team subclasses this base and supplies a thin ``run_workflow`` that
passes its module-level ``run_setup``, ``*DevelopmentAgent``, and
``*WorkflowResult`` into the shared helper. Those names are looked up in the
subclass orchestrator module at call time so tests can monkeypatch
``orchestrator.run_setup`` / ``orchestrator.*DevelopmentAgent`` as module-level
attributes (see ``test_team_lead_propagates_development_handoff_fields``).
"""
```

Update imports to:

```python
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Optional, Tuple

from llm_service import LLMClient
from software_engineering_team.shared.models import SystemArchitecture, Task
from software_engineering_team.shared.repo_context_cache import RepoContextCache
from software_engineering_team.shared.v2_models import Phase

logger = logging.getLogger(__name__)
```

- [ ] **Step 2: Update the `BaseTeamLead` class docstring**

Replace the class docstring with:

```python
    """Shared base for the code-v2 Team Leads.

    Subclasses provide the per-team repo-briefing filter constants (via
    ``__init__``) and a thin ``run_workflow`` that delegates to
    :meth:`_run_setup_and_delegate` with late-bound module globals.

    Invariants: instance state is limited to ``llm``, the injected
    extensions/exclude_dirs/max_chars, and ``_repo_context_caches``.
    """
```

- [ ] **Step 3: Add `_run_setup_and_delegate` after `_repo_context_cache_for`**

```python
    def _run_setup_and_delegate(
        self,
        *,
        repo_path: Path,
        task: Task,
        result_cls: Callable[..., Any],
        run_setup_fn: Callable[..., Any],
        development_agent_cls: Callable[..., Any],
        architecture: Optional[SystemArchitecture] = None,
        spec_content: str = "",
        qa_agent: Any = None,
        security_agent: Any = None,
        code_review_agent: Any = None,
        build_verifier: Optional[Callable[..., Tuple[bool, str]]] = None,
        doc_agent: Any = None,
        linting_tool_agent: Any = None,
        job_updater: Optional[Callable[..., None]] = None,
        review_config: Any = None,
        merge_to_development: bool = True,
    ) -> Any:
        """Run setup, verify lint/test readiness, then delegate to the development agent.

        Preconditions:
          - ``repo_path`` is a directory the setup phase can operate on.
          - ``task`` has a non-empty ``id``.
          - ``result_cls`` is callable as ``result_cls(task_id=...)`` and returns an
            object exposing the development-handoff fields plus ``setup_result`` /
            ``failure_reason`` / ``current_phase``.
          - ``run_setup_fn`` is callable as ``run_setup_fn(repo_path=..., task_title=...)``.
          - ``development_agent_cls`` is callable as ``development_agent_cls(self.llm)``
            and returns an object with ``run_workflow(**kwargs)``.
        Postconditions:
          - On setup failure or missing lint/test config: returns a result with
            ``failure_reason`` set and without calling the development agent.
          - On success: returns the team-lead result with ``setup_result`` preserved
            and the 13 development-handoff fields copied from the inner agent result.
          - Progress 2/3/5 ``job_updater`` calls include the canonical ``status_text``
            strings when ``job_updater`` is provided; updater exceptions are logged
            at DEBUG and do not abort the workflow.
        """
        assert repo_path.is_dir(), "repo_path must be a directory"
        assert task.id, "task.id is required"

        task_id = task.id
        result = result_cls(task_id=task_id)

        def _update_job(**kwargs: Any) -> None:
            if job_updater:
                try:
                    job_updater(**kwargs)
                except Exception as exc:
                    logger.debug("[%s] job_updater failed: %s", task_id, exc)

        result.current_phase = Phase.SETUP
        _update_job(
            current_phase="setup",
            progress=2,
            status_text="Setting up repository and development environment",
        )
        try:
            setup_result = run_setup_fn(repo_path=repo_path, task_title=task.title or "")
            result.setup_result = setup_result
        except Exception as exc:
            result.failure_reason = f"Setup failed: {exc}"
            logger.error("[%s] %s", task_id, result.failure_reason)
            return result
        _update_job(current_phase="setup", progress=3, status_text="Repository setup complete")

        if not getattr(setup_result, "linting_configured", False):
            logger.warning(
                "[%s] Linting not configured after setup — coding cannot proceed without linting",
                task_id,
            )
            result.failure_reason = (
                "Setup completed but linting is not configured. "
                "Linting must be set up before any coding tasks can begin."
            )
            return result

        if not getattr(setup_result, "testing_configured", False):
            logger.warning(
                "[%s] Testing not configured after setup — coding cannot proceed without testing",
                task_id,
            )
            result.failure_reason = (
                "Setup completed but testing is not configured. "
                "Testing must be set up before any coding tasks can begin."
            )
            return result

        logger.info("[%s] Linting and testing verified — proceeding to coding phase", task_id)
        _update_job(
            current_phase="setup",
            progress=5,
            status_text="Linting and testing verified; ready for development",
        )

        dev_agent = development_agent_cls(self.llm)
        inner = dev_agent.run_workflow(
            repo_path=repo_path,
            task=task,
            architecture=architecture,
            spec_content=spec_content,
            qa_agent=qa_agent,
            security_agent=security_agent,
            code_review_agent=code_review_agent,
            build_verifier=build_verifier,
            doc_agent=doc_agent,
            linting_tool_agent=linting_tool_agent,
            job_updater=job_updater,
            review_config=review_config,
            merge_to_development=merge_to_development,
            repo_context_cache=self._repo_context_cache_for(repo_path),
        )
        copy_development_result_fields(result, inner)
        return result
```

- [ ] **Step 4: Run the helper unit tests**

```bash
python -m pytest agents/software_engineering_team/tests/test_team_lead_base.py -v
```

Expected: PASS (all existing + new tests).

- [ ] **Step 5: Commit**

```bash
git add backend/agents/software_engineering_team/shared/team_lead_base.py \
        backend/agents/software_engineering_team/tests/test_team_lead_base.py
git commit -m "$(cat <<'EOF'
Add BaseTeamLead._run_setup_and_delegate shared workflow helper.

EOF
)"
```

---

### Task 3: Thin wrappers on backend and frontend team leads

**Files:**
- Modify: `backend/agents/software_engineering_team/backend_code_v2_team/orchestrator.py` (replace `BackendCodeV2TeamLead.run_workflow` body)
- Modify: `backend/agents/software_engineering_team/frontend_code_v2_team/orchestrator.py` (replace `FrontendCodeV2TeamLead.run_workflow` body)

**Interfaces:**
- Consumes: `BaseTeamLead._run_setup_and_delegate`
- Produces: thin `run_workflow` methods that preserve public signatures and monkeypatch surface

- [ ] **Step 1: Replace `BackendCodeV2TeamLead.run_workflow`**

In `backend_code_v2_team/orchestrator.py`, replace the entire `run_workflow` method on `BackendCodeV2TeamLead` with:

```python
    def run_workflow(
        self,
        *,
        repo_path: Path,
        task: Task,
        architecture: Optional[SystemArchitecture] = None,
        spec_content: str = "",
        qa_agent: Any = None,
        security_agent: Any = None,
        code_review_agent: Any = None,
        build_verifier: Optional[Callable[..., Tuple[bool, str]]] = None,
        doc_agent: Any = None,
        linting_tool_agent: Any = None,
        job_updater: Optional[Callable[..., None]] = None,
        review_config: Optional[MicrotaskReviewConfig] = None,
        merge_to_development: bool = True,
    ) -> BackendCodeV2WorkflowResult:
        """Run setup, verify lint/test readiness, then execute the backend 5-phase workflow.

        merge_to_development defaults to True. When False, delivery prepares a
        feature branch for external review instead of merging it.
        """
        return self._run_setup_and_delegate(
            repo_path=repo_path,
            task=task,
            result_cls=BackendCodeV2WorkflowResult,
            run_setup_fn=run_setup,
            development_agent_cls=BackendDevelopmentAgent,
            architecture=architecture,
            spec_content=spec_content,
            qa_agent=qa_agent,
            security_agent=security_agent,
            code_review_agent=code_review_agent,
            build_verifier=build_verifier,
            doc_agent=doc_agent,
            linting_tool_agent=linting_tool_agent,
            job_updater=job_updater,
            review_config=review_config,
            merge_to_development=merge_to_development,
        )
```

Remove the now-unused `copy_development_result_fields` import from this module if nothing else references it (keep `BaseTeamLead`).

- [ ] **Step 2: Replace `FrontendCodeV2TeamLead.run_workflow`**

In `frontend_code_v2_team/orchestrator.py`, replace the entire `run_workflow` method on `FrontendCodeV2TeamLead` with the same shape, substituting frontend names:

```python
    def run_workflow(
        self,
        *,
        repo_path: Path,
        task: Task,
        architecture: Optional[SystemArchitecture] = None,
        spec_content: str = "",
        qa_agent: Any = None,
        security_agent: Any = None,
        code_review_agent: Any = None,
        build_verifier: Optional[Callable[..., Tuple[bool, str]]] = None,
        doc_agent: Any = None,
        linting_tool_agent: Any = None,
        job_updater: Optional[Callable[..., None]] = None,
        review_config: Optional[MicrotaskReviewConfig] = None,
        merge_to_development: bool = True,
    ) -> FrontendCodeV2WorkflowResult:
        """Run setup, verify lint/test readiness, then execute the frontend 5-phase workflow.

        merge_to_development defaults to True. When False, delivery prepares a
        feature branch for external review instead of merging it.
        """
        return self._run_setup_and_delegate(
            repo_path=repo_path,
            task=task,
            result_cls=FrontendCodeV2WorkflowResult,
            run_setup_fn=run_setup,
            development_agent_cls=FrontendDevelopmentAgent,
            architecture=architecture,
            spec_content=spec_content,
            qa_agent=qa_agent,
            security_agent=security_agent,
            code_review_agent=code_review_agent,
            build_verifier=build_verifier,
            doc_agent=doc_agent,
            linting_tool_agent=linting_tool_agent,
            job_updater=job_updater,
            review_config=review_config,
            merge_to_development=merge_to_development,
        )
```

Remove unused `copy_development_result_fields` import if applicable.

- [ ] **Step 3: Run handoff + team-lead tests**

```bash
python -m pytest \
  agents/software_engineering_team/tests/test_team_lead_base.py \
  agents/software_engineering_team/tests/test_backend_code_v2_team.py::TestBackendCodeV2TeamLead \
  agents/software_engineering_team/tests/test_frontend_code_v2_team.py::TestFrontendCodeV2TeamLead \
  -v
```

Expected: PASS (including both `test_team_lead_propagates_development_handoff_fields`).

- [ ] **Step 4: Commit**

```bash
git add backend/agents/software_engineering_team/backend_code_v2_team/orchestrator.py \
        backend/agents/software_engineering_team/frontend_code_v2_team/orchestrator.py
git commit -m "$(cat <<'EOF'
Delegate backend and frontend team-lead run_workflow to shared helper.

EOF
)"
```

---

### Task 4: Full verification gate

**Files:**
- (none — verification only)

**Interfaces:**
- Consumes: Tasks 1–3 deliverables
- Produces: green `make lint` + focused/full test evidence

- [ ] **Step 1: Lint**

From `backend/`:

```bash
make lint
```

Expected: PASS (ruff check + format).

- [ ] **Step 2: Full test suite**

```bash
make test
```

Expected: PASS. If a failure is clearly pre-existing and unrelated, document it before proceeding; do not expand scope to fix unrelated failures without asking.

- [ ] **Step 3: Coverage spot-check on touched files**

```bash
python -m pytest \
  agents/software_engineering_team/tests/test_team_lead_base.py \
  agents/software_engineering_team/tests/test_backend_code_v2_team.py::TestBackendCodeV2TeamLead \
  agents/software_engineering_team/tests/test_frontend_code_v2_team.py::TestFrontendCodeV2TeamLead \
  --cov=software_engineering_team.shared.team_lead_base \
  --cov-report=term-missing
```

Expected: ≥90% line coverage on `team_lead_base.py`.

- [ ] **Step 4: Final commit only if Step 1–3 left uncommitted fixes**

If lint/format auto-changed files:

```bash
git add -u
git commit -m "$(cat <<'EOF'
Fix lint after team-lead run_workflow template extraction.

EOF
)"
```

Otherwise skip.

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| `_run_setup_and_delegate` on `BaseTeamLead` | Task 2 |
| Thin backend/frontend wrappers | Task 3 |
| Canonical status_text on progress 2/3 | Task 2 (+ test in Task 1) |
| DEBUG `_update_job` failures | Task 2 (+ test in Task 1) |
| Monkeypatch surface preserved | Task 3 (+ existing handoff tests) |
| New helper unit tests | Task 1 |
| Docstring updates | Task 2 |
| `make test` / `make lint` / 90% coverage | Task 4 |
| No `copy_development_result_fields` changes | Global constraint |
| No devops/coding_team migration | Global constraint |
