# Extract Documentation-Phase Block Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the duplicated documentation-phase block from both code-v2 orchestrators onto `BaseV2DevelopmentAgent._run_documentation_phase`, parameterized by `status_text`.

**Architecture:** Static helper on `BaseV2DevelopmentAgent` mirrors `_run_preflight` / `_run_planning_and_branch_setup`: inject `run_documentation_phase` + `update_job` so team monkeypatches keep working; mutate `result`/`current_files` in place; return updated `current_files`.

**Tech Stack:** Python 3.10, pytest, existing SE code-v2 models (`Phase`, `DocumentationPhaseResult`).

## Global Constraints

- Behavior-preserving: status strings, log messages, exception swallow, file merge must match today.
- No `_TEAM_LABEL` in this change (deferred to the deliver-phase extraction).
- Do not mention GitHub issue numbers in code, comments, docs, or commit messages.
- 90% line-coverage floor for touched files; `make test` and `make lint` from `backend/`.

## File map

| File | Role |
|------|------|
| `backend/agents/software_engineering_team/shared/v2_orchestrator.py` | Add `_run_documentation_phase` |
| `backend/agents/software_engineering_team/backend_code_v2_team/orchestrator.py` | Replace inline block; pass backend status string |
| `backend/agents/software_engineering_team/frontend_code_v2_team/orchestrator.py` | Replace inline block; pass frontend status string |
| `backend/agents/software_engineering_team/tests/test_v2_orchestrator_helpers.py` | Add `TestRunDocumentationPhase` |

---

### Task 1: Unit tests for `_run_documentation_phase`

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_v2_orchestrator_helpers.py` (append after `TestRunPlanningAndBranchSetup`)
- Test: same file

**Interfaces:**
- Produces: failing tests that call `BaseV2DevelopmentAgent._run_documentation_phase` with signature:

```python
@staticmethod
def _run_documentation_phase(
    *,
    task_id: str,
    task: Any,
    repo_path: Path,
    llm: LLMClient,
    exec_result: Any,
    planning_result: Any,
    tool_agents: Dict[Any, Any],
    result: Any,
    current_files: Dict[str, str],
    run_documentation_phase: Callable[..., Any],
    update_job: Callable[..., None],
    logger: logging.Logger,
    status_text: str,
) -> Dict[str, str]:
```

- [ ] **Step 1: Append failing tests**

```python
class _FakeDocResult:
    def __init__(self, files=None, summary="ok"):
        self.files = files or {}
        self.summary = summary


class _FakeWorkflowResult:
    def __init__(self):
        self.current_phase = None
        self.documentation_result = None
        self.final_files = None


class TestRunDocumentationPhase:
    """Tests for BaseV2DevelopmentAgent._run_documentation_phase."""

    @staticmethod
    def _logger():
        import logging

        return logging.getLogger("test_run_documentation_phase")

    def test_success_merges_files_and_updates_job(self, tmp_path: Path):
        from software_engineering_team.shared.v2_models import Phase
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        update_calls = []
        result = _FakeWorkflowResult()
        current_files = {"a.py": "a"}
        doc = _FakeDocResult(files={"docs/readme.md": "# hi"}, summary="docs done")

        out = BaseV2DevelopmentAgent._run_documentation_phase(
            task_id="t1",
            task=MagicMock(),
            repo_path=tmp_path,
            llm=MagicMock(),
            exec_result=MagicMock(),
            planning_result=MagicMock(),
            tool_agents={},
            result=result,
            current_files=current_files,
            run_documentation_phase=lambda **kw: doc,
            update_job=lambda **kw: update_calls.append(kw),
            logger=self._logger(),
            status_text="Generating documentation and API specs",
        )

        assert result.current_phase == Phase.DOCUMENTATION
        assert result.documentation_result is doc
        assert out == {"a.py": "a", "docs/readme.md": "# hi"}
        assert result.final_files == out
        assert update_calls == [
            {
                "current_phase": "documentation",
                "progress": 80,
                "status_text": "Generating documentation and API specs",
            }
        ]

    def test_success_without_files_leaves_current_files_unchanged(self, tmp_path: Path):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        result = _FakeWorkflowResult()
        current_files = {"a.py": "a"}
        doc = _FakeDocResult(files={}, summary="nothing")

        out = BaseV2DevelopmentAgent._run_documentation_phase(
            task_id="t1",
            task=MagicMock(),
            repo_path=tmp_path,
            llm=MagicMock(),
            exec_result=MagicMock(),
            planning_result=MagicMock(),
            tool_agents={},
            result=result,
            current_files=current_files,
            run_documentation_phase=lambda **kw: doc,
            update_job=lambda **kw: None,
            logger=self._logger(),
            status_text="Generating documentation and API docs...",
        )

        assert result.documentation_result is doc
        assert out is current_files
        assert out == {"a.py": "a"}
        assert result.final_files is None

    def test_exception_is_swallowed_and_logged(self, tmp_path: Path, caplog):
        from software_engineering_team.shared.v2_models import Phase
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        result = _FakeWorkflowResult()
        current_files = {"a.py": "a"}

        def _raise(**kwargs):
            raise RuntimeError("doc boom")

        with caplog.at_level("WARNING", logger="test_run_documentation_phase"):
            out = BaseV2DevelopmentAgent._run_documentation_phase(
                task_id="t1",
                task=MagicMock(),
                repo_path=tmp_path,
                llm=MagicMock(),
                exec_result=MagicMock(),
                planning_result=MagicMock(),
                tool_agents={},
                result=result,
                current_files=current_files,
                run_documentation_phase=_raise,
                update_job=lambda **kw: None,
                logger=self._logger(),
                status_text="Generating documentation and API specs",
            )

        assert result.current_phase == Phase.DOCUMENTATION
        assert result.documentation_result is None
        assert out is current_files
        assert "Documentation phase failed: doc boom" in caplog.text
        assert "Continuing to Deliver phase" in caplog.text
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && .venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_v2_orchestrator_helpers.py::TestRunDocumentationPhase -v
```

Expected: FAIL — `AttributeError: type object 'BaseV2DevelopmentAgent' has no attribute '_run_documentation_phase'`

(Use the main checkout's `backend/.venv` if the worktree has no venv:  
`/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python`)

---

### Task 2: Implement `_run_documentation_phase`

**Files:**
- Modify: `backend/agents/software_engineering_team/shared/v2_orchestrator.py`
- Test: `test_v2_orchestrator_helpers.py::TestRunDocumentationPhase`

**Interfaces:**
- Consumes: signature from Task 1
- Produces: working `_run_documentation_phase`

- [ ] **Step 1: Add import for `Phase`**

In `v2_orchestrator.py`, add:

```python
from software_engineering_team.shared.v2_models import Phase
```

- [ ] **Step 2: Add the helper after `_run_planning_and_branch_setup` (before `_read_repo_code`)**

```python
    @staticmethod
    def _run_documentation_phase(
        *,
        task_id: str,
        task: Any,
        repo_path: Path,
        llm: LLMClient,
        exec_result: Any,
        planning_result: Any,
        tool_agents: Dict[Any, Any],
        result: Any,
        current_files: Dict[str, str],
        run_documentation_phase: Callable[..., Any],
        update_job: Callable[..., None],
        logger: logging.Logger,
        status_text: str,
    ) -> Dict[str, str]:
        """Run the documentation phase and merge any new files into ``current_files``.

        Extracted from ``run_workflow`` so the documentation status update +
        phase invocation + file-merge + exception-swallow block is defined once;
        ``run_documentation_phase`` is injected (the caller's late-imported
        module-level name) so tests that monkeypatch
        ``phases.documentation.run_documentation_phase`` keep working.

        Preconditions: ``status_text`` is the team-specific job status string
          (backend and frontend differ). ``result`` exposes ``current_phase``,
          ``documentation_result``, and ``final_files`` attributes.
        Postconditions: ``result.current_phase`` is ``Phase.DOCUMENTATION``.
          On success, ``result.documentation_result`` is set and any
          ``doc_result.files`` are merged into ``current_files`` /
          ``result.final_files``. On failure, logs a warning and leaves
          ``documentation_result`` unset. Never raises; returns the (possibly
          updated) ``current_files`` dict.
        """
        logger.info("[%s] Next step -> Starting Phase: Documentation", task_id)
        result.current_phase = Phase.DOCUMENTATION
        update_job(
            current_phase="documentation",
            progress=80,
            status_text=status_text,
        )

        try:
            doc_result = run_documentation_phase(
                llm=llm,
                task=task,
                repo_path=repo_path,
                execution_result=exec_result,
                planning_result=planning_result,
                tool_agents=tool_agents,
            )
            result.documentation_result = doc_result
            if doc_result.files:
                current_files.update(doc_result.files)
                result.final_files = current_files
            logger.info("[%s] Documentation phase complete: %s", task_id, doc_result.summary)
        except Exception as exc:
            logger.warning(
                "[%s] Documentation phase failed: %s. Next step -> Continuing to Deliver phase",
                task_id,
                exc,
            )

        return current_files
```

Also update the module docstring's "remainder of run_workflow" sentence so documentation is no longer listed as still-divergent (optional one-line tweak).

- [ ] **Step 3: Run unit tests**

```bash
cd backend && ../path/to/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_v2_orchestrator_helpers.py::TestRunDocumentationPhase -v
```

Expected: PASS

---

### Task 3: Wire both orchestrators

**Files:**
- Modify: `backend/agents/software_engineering_team/backend_code_v2_team/orchestrator.py` (documentation block ~340–370)
- Modify: `backend/agents/software_engineering_team/frontend_code_v2_team/orchestrator.py` (documentation block ~348–378)

**Interfaces:**
- Consumes: `_run_documentation_phase` from Task 2

- [ ] **Step 1: Replace backend block with**

```python
        from .phases.documentation import run_documentation_phase

        current_files = self._run_documentation_phase(
            task_id=task_id,
            task=task,
            repo_path=repo_path,
            llm=self.llm,
            exec_result=exec_result,
            planning_result=planning_result,
            tool_agents=tool_agents,
            result=result,
            current_files=current_files,
            run_documentation_phase=run_documentation_phase,
            update_job=_update_job,
            logger=logger,
            status_text="Generating documentation and API specs",
        )
```

- [ ] **Step 2: Replace frontend block with the same call, but**

```python
            status_text="Generating documentation and API docs...",
```

- [ ] **Step 3: Run focused regression suite**

```bash
cd backend && .venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_v2_orchestrator_helpers.py \
  agents/software_engineering_team/tests/test_backend_code_v2_team.py \
  agents/software_engineering_team/tests/test_frontend_code_v2_team.py -q
```

Expected: all pass

- [ ] **Step 4: Lint + broader SE tests if time permits**

```bash
cd backend && make lint
```

---

## Spec coverage self-review

| Spec requirement | Task |
|---|---|
| Shared helper on `BaseV2DevelopmentAgent` with parameterized status text | Task 2 |
| Both orchestrators call shared helper | Task 3 |
| Existing team/helper tests pass | Task 3 Step 3 |
| No `_TEAM_LABEL` / no other blocks | Global constraints + Non-goals |
| Coverage / lint | Task 3 Step 4 |
