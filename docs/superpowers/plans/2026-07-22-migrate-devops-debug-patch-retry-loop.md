# Migrate DevOps Debug-Patch Loop to Bounded Retry Helper — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `DevOpsTeamLeadAgent`'s Phase 4.6 inline debug-patch `for` loop with an unbound call to `BaseTeamLead._run_bounded_retry_loop`, extracting the iteration body into `_debug_patch_once` over a mutable `_DebugPatchState`.

**Architecture:** Keep `DevOpsTeamLeadAgent` on `TeamLeadSharedState`. Invoke the helper as `BaseTeamLead._run_bounded_retry_loop(self, …)` (same pattern as `_run_gated_phases`). Soft-abort by returning `None` from `_debug_patch_once`; succeed when `not state.exec_failures`. Skip the helper when there are no initial execution failures.

**Tech Stack:** Python 3.10, pytest, Ruff (via `make lint`)

**Spec:** `docs/superpowers/specs/2026-07-22-migrate-devops-debug-patch-retry-loop-design.md`

## Global Constraints

- Unbound helper call only — do not make `DevOpsTeamLeadAgent` subclass `BaseTeamLead`.
- Preserve 3-iteration bound (`MAX_INFRA_FIX_ITERATIONS = 3`) and soft-fail semantics (exception / not-fixable / empty patches → abort).
- Do not change `infra_debug_agent` / `infra_patch_agent` internals.
- Existing `test_devops_team.py` and `test_devops_debug_patch.py` must pass unchanged in intent (behavior-preserving).
- 90% coverage floor on touched files; `make test` and `make lint` must pass from `backend/`.
- Design-by-Contract: document Preconditions/Postconditions on `_debug_patch_once`.
- Never reference GitHub issue numbers in code, comments, docs, or commit messages.

## File Structure

| Path | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/devops_team/orchestrator.py` | `_DebugPatchState`, `_debug_patch_once`, Phase 4.6 call-site rewrite |
| `backend/agents/software_engineering_team/tests/test_devops_debug_patch.py` | Focused unit tests for `_debug_patch_once` abort/success/continue |

---

### Task 1: Extract `_debug_patch_once` and wire the bounded retry helper

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_devops_debug_patch.py`
- Modify: `backend/agents/software_engineering_team/devops_team/orchestrator.py`

**Interfaces:**
- Consumes: `BaseTeamLead._run_bounded_retry_loop(*, max_iterations, attempt, is_success) -> Tuple[bool, Optional[T]]`
- Produces:
  - `@dataclass _DebugPatchState` with fields `exec_results`, `exec_failures`, `exec_gate_map`, `exec_findings`
  - `DevOpsTeamLeadAgent._debug_patch_once(self, fix_iter: int, *, state: _DebugPatchState, aggregated_artifacts: Dict[str, str], repo_path: Path, repo_str: str, write_changes: bool, subdir: str, max_iterations: int) -> Optional[_DebugPatchState]`

- [ ] **Step 1: Write the failing unit tests for `_debug_patch_once`**

Append to `backend/agents/software_engineering_team/tests/test_devops_debug_patch.py` (after `TestDevOpsPipelineDebugPatchLoop`):

```python
# ---------------------------------------------------------------------------
# _debug_patch_once unit tests
# ---------------------------------------------------------------------------


class TestDebugPatchOnce:
    """Unit coverage for DevOpsTeamLeadAgent._debug_patch_once."""

    def _make_lead_with_stubs(self):
        from software_engineering_team.devops_team.orchestrator import (
            DevOpsTeamLeadAgent,
            _DebugPatchState,
        )

        lead = DevOpsTeamLeadAgent(llm_client=DummyLLMClient())
        return lead, _DebugPatchState

    def test_returns_none_when_debug_not_fixable(self) -> None:
        from software_engineering_team.devops_team.infra_debug_agent import IaCDebugOutput
        from software_engineering_team.devops_team.orchestrator import (
            DevOpsTeamLeadAgent,
            _DebugPatchState,
        )

        lead = DevOpsTeamLeadAgent(llm_client=DummyLLMClient())

        class _Debug:
            def run(self, *_a, **_k):
                return IaCDebugOutput(errors=[], summary="nope", fixable=False)

        lead.infra_debug_agent = _Debug()  # type: ignore[assignment]
        state = _DebugPatchState(
            exec_results=[{"success": False, "tool": "terraform", "command": "validate", "findings": ["e"]}],
            exec_failures=[{"success": False, "tool": "terraform", "command": "validate", "findings": ["e"]}],
            exec_gate_map={"terraform_validate": "fail"},
            exec_findings=["e"],
        )
        out = lead._debug_patch_once(
            0,
            state=state,
            aggregated_artifacts={"main.tf": "x"},
            repo_path=__import__("pathlib").Path("."),
            repo_str=".",
            write_changes=False,
            subdir="",
            max_iterations=3,
        )
        assert out is None

    def test_returns_none_when_debug_agent_raises(self) -> None:
        from software_engineering_team.devops_team.orchestrator import (
            DevOpsTeamLeadAgent,
            _DebugPatchState,
        )

        lead = DevOpsTeamLeadAgent(llm_client=DummyLLMClient())

        class _Debug:
            def run(self, *_a, **_k):
                raise RuntimeError("debug boom")

        lead.infra_debug_agent = _Debug()  # type: ignore[assignment]
        state = _DebugPatchState(
            exec_results=[{"success": False, "tool": "t", "command": "c", "findings": ["e"]}],
            exec_failures=[{"success": False, "tool": "t", "command": "c", "findings": ["e"]}],
            exec_gate_map={},
            exec_findings=["e"],
        )
        out = lead._debug_patch_once(
            0,
            state=state,
            aggregated_artifacts={"main.tf": "x"},
            repo_path=__import__("pathlib").Path("."),
            repo_str=".",
            write_changes=False,
            subdir="",
            max_iterations=3,
        )
        assert out is None

    def test_returns_state_with_cleared_failures_on_success(self) -> None:
        from software_engineering_team.devops_team.infra_debug_agent import IaCDebugOutput
        from software_engineering_team.devops_team.infra_patch_agent import IaCPatchOutput
        from software_engineering_team.devops_team.orchestrator import (
            DevOpsTeamLeadAgent,
            _DebugPatchState,
        )

        lead = DevOpsTeamLeadAgent(llm_client=DummyLLMClient())

        class _Debug:
            def run(self, *_a, **_k):
                return IaCDebugOutput(errors=[], summary="fixable", fixable=True)

        class _Patch:
            def run(self, *_a, **_k):
                return IaCPatchOutput(
                    patched_artifacts={"main.tf": "fixed"},
                    summary="patched",
                    edits_applied=1,
                )

        lead.infra_debug_agent = _Debug()  # type: ignore[assignment]
        lead.infra_patch_agent = _Patch()  # type: ignore[assignment]
        lead._run_execution_tools = (  # type: ignore[assignment]
            lambda _repo, _arts: [
                {
                    "tool": "terraform",
                    "command": "validate",
                    "success": True,
                    "checks": {"terraform_validate": "pass"},
                    "findings": [],
                    "failure_class": "",
                }
            ]
        )
        artifacts = {"main.tf": "broken"}
        state = _DebugPatchState(
            exec_results=[{"success": False, "tool": "terraform", "command": "validate", "findings": ["e"]}],
            exec_failures=[{"success": False, "tool": "terraform", "command": "validate", "findings": ["e"]}],
            exec_gate_map={"terraform_validate": "fail"},
            exec_findings=["e"],
        )
        out = lead._debug_patch_once(
            0,
            state=state,
            aggregated_artifacts=artifacts,
            repo_path=__import__("pathlib").Path("."),
            repo_str=".",
            write_changes=False,
            subdir="",
            max_iterations=3,
        )
        assert out is state
        assert out.exec_failures == []
        assert artifacts["main.tf"] == "fixed"
        assert out.exec_gate_map.get("terraform_validate") == "pass"
```

If `IaCPatchOutput` is not the real export name, open
`backend/agents/software_engineering_team/devops_team/infra_patch_agent/models.py`
(or the package `__init__`) and use the actual patch-output model / constructor
fields already used by production code.

- [ ] **Step 2: Run the new tests to verify they fail**

Run from `backend/`:

```bash
python -m pytest agents/software_engineering_team/tests/test_devops_debug_patch.py::TestDebugPatchOnce -v
```

Expected: FAIL with `ImportError` / `AttributeError` for `_DebugPatchState` or `_debug_patch_once`.

- [ ] **Step 3: Add imports and `_DebugPatchState` to orchestrator.py**

In `backend/agents/software_engineering_team/devops_team/orchestrator.py`:

1. Change the top imports to include `dataclass`:

```python
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
```

2. Immediately before `class DevOpsTeamLeadAgent(TeamLeadSharedState):`, add:

```python
@dataclass
class _DebugPatchState:
    """Mutable bag for one Phase 4.6 debug-patch retry session.

    Invariants: ``exec_failures`` is always derived from ``exec_results``
    (entries where ``success`` is falsy); ``exec_gate_map`` / ``exec_findings``
    mirror the latest execution-tool aggregation.
    """

    exec_results: List[Dict[str, Any]]
    exec_failures: List[Dict[str, Any]]
    exec_gate_map: Dict[str, str]
    exec_findings: List[str]
```

- [ ] **Step 4: Implement `_debug_patch_once` on `DevOpsTeamLeadAgent`**

Add this method on `DevOpsTeamLeadAgent` (near `_run_execution_tools` is fine):

```python
def _debug_patch_once(
    self,
    fix_iter: int,
    *,
    state: _DebugPatchState,
    aggregated_artifacts: Dict[str, str],
    repo_path: Path,
    repo_str: str,
    write_changes: bool,
    subdir: str,
    max_iterations: int,
) -> Optional[_DebugPatchState]:
    """Run one infra debug → patch → re-exec iteration.

    Preconditions:
      - ``fix_iter`` is a 0-based index from the bounded-retry helper
      - ``max_iterations >= 1``
      - ``state.exec_failures`` is non-empty when invoked by the helper
    Postconditions:
      - Soft abort (debug/patch exception, not fixable, empty patches) →
        log and return ``None``
      - Otherwise update ``aggregated_artifacts`` and ``state`` from the
        patch + re-exec, then return ``state``
    """
    assert max_iterations >= 1, "max_iterations must be >= 1"
    if not state.exec_failures:
        return state

    self._report_status(
        "phase4.6",
        detail=(
            "DevOps team pipeline: phase 4.6 - debug-patch iteration "
            f"{fix_iter + 1}/{max_iterations} ({len(state.exec_failures)} failures)"
        ),
    )
    combined_output = "\n---\n".join(
        "\n".join(ef.get("findings", [])) for ef in state.exec_failures
    )
    first_tool = state.exec_failures[0].get("tool", "unknown")
    first_cmd = state.exec_failures[0].get("command", "unknown")
    try:
        debug_out = self.infra_debug_agent.run(
            IaCDebugInput(
                execution_output=combined_output,
                tool_name=first_tool,
                command=first_cmd,
                artifacts=aggregated_artifacts,
            )
        )
    except Exception as dbg_err:
        logger.warning("DevOps debug agent failed: %s", dbg_err)
        return None
    if not debug_out.fixable:
        logger.info("DevOps debug agent: errors are not fixable via code changes")
        return None
    try:
        patch_out = self.infra_patch_agent.run(
            IaCPatchInput(
                debug_output=debug_out,
                original_artifacts=aggregated_artifacts,
                repo_path=repo_str,
            )
        )
    except Exception as patch_err:
        logger.warning("DevOps patch agent failed: %s", patch_err)
        return None
    if not patch_out.patched_artifacts:
        logger.info("DevOps patch agent returned no patches")
        return None
    aggregated_artifacts.update(patch_out.patched_artifacts)
    if write_changes:
        write_agent_output(
            repo_path=repo_path,
            output={
                "files": patch_out.patched_artifacts,
                "commit_message": f"fix(devops): patch iteration {fix_iter + 1}",
            },
            subdir=subdir,
        )
    state.exec_results = self._run_execution_tools(repo_str, aggregated_artifacts)
    state.exec_failures = [er for er in state.exec_results if not er.get("success", True)]
    state.exec_gate_map = {}
    state.exec_findings = []
    for er in state.exec_results:
        state.exec_gate_map.update(er.get("checks", {}))
        state.exec_findings.extend(er.get("findings", []))
    return state
```

- [ ] **Step 5: Replace the Phase 4.6 inline loop with the helper call**

In `_run_pipeline`, replace the block from `# Phase 4.6: Debug-patch loop…` through the end of the `for fix_iter in range(...)` loop (the block that ends just before `tool_gate_map.update(exec_gate_map)`) with:

```python
        # Phase 4.6: Debug-patch loop for fixable execution failures.
        # Consume BaseTeamLead's bounded retry helper without inheriting the
        # code-v2 BaseTeamLead constructor (DevOps uses TeamLeadSharedState).
        MAX_INFRA_FIX_ITERATIONS = 3
        exec_failures = [er for er in exec_results if not er.get("success", True)]
        state = _DebugPatchState(
            exec_results=exec_results,
            exec_failures=exec_failures,
            exec_gate_map=exec_gate_map,
            exec_findings=exec_findings,
        )
        if state.exec_failures:
            BaseTeamLead._run_bounded_retry_loop(
                self,
                max_iterations=MAX_INFRA_FIX_ITERATIONS,
                attempt=lambda i: self._debug_patch_once(
                    i,
                    state=state,
                    aggregated_artifacts=aggregated_artifacts,
                    repo_path=repo_path,
                    repo_str=repo_str,
                    write_changes=write_changes,
                    subdir=subdir,
                    max_iterations=MAX_INFRA_FIX_ITERATIONS,
                ),
                is_success=lambda s: not s.exec_failures,
            )

        tool_gate_map.update(state.exec_gate_map)
```

Do **not** leave a second `tool_gate_map.update(exec_gate_map)` — the update must read from `state.exec_gate_map` only.

- [ ] **Step 6: Run new unit tests to verify they pass**

```bash
python -m pytest agents/software_engineering_team/tests/test_devops_debug_patch.py::TestDebugPatchOnce -v
```

Expected: PASS (all three tests).

- [ ] **Step 7: Run existing debug-patch / devops regression tests**

```bash
python -m pytest \
  agents/software_engineering_team/tests/test_devops_debug_patch.py \
  agents/software_engineering_team/tests/test_devops_team.py \
  -v
```

Expected: PASS (including `TestDevOpsPipelineDebugPatchLoop`).

- [ ] **Step 8: Commit**

```bash
git add \
  backend/agents/software_engineering_team/devops_team/orchestrator.py \
  backend/agents/software_engineering_team/tests/test_devops_debug_patch.py
git commit -m "$(cat <<'EOF'
Refactor devops debug-patch loop onto BaseTeamLead retry helper.

EOF
)"
```

(Commit from the worktree root; adjust paths if `git status` shows them relative to repo root without the `backend/` prefix — in this repo the paths above are correct from the worktree root.)

---

### Task 2: Full backend verification

**Files:**
- Verify only (no intentional code changes)

**Interfaces:**
- Consumes: Task 1 deliverables
- Produces: green `make test` / `make lint` evidence

- [ ] **Step 1: Lint**

From `backend/`:

```bash
make lint
```

Expected: exit 0.

- [ ] **Step 2: Full test suite**

From `backend/`:

```bash
make test
```

Expected: exit 0. If coverage on `orchestrator.py` is reported below 90%, add another focused `_debug_patch_once` case (e.g. empty patches abort) in `test_devops_debug_patch.py`, re-run, and amend only if the commit has not been pushed and the amend rules in user git protocol are fully satisfied — otherwise make a new commit.

- [ ] **Step 3: Commit any coverage/lint fixes (only if needed)**

```bash
git add -u
git commit -m "$(cat <<'EOF'
Tighten devops debug-patch coverage after retry-loop migration.

EOF
)"
```

Skip this step if Step 2 needed no further changes.

---

## Spec Coverage Self-Review

| Spec requirement | Task |
|---|---|
| Unbound `BaseTeamLead._run_bounded_retry_loop` call | Task 1 Step 5 |
| Private `_debug_patch_once` | Task 1 Step 4 |
| Mutable `_DebugPatchState` | Task 1 Step 3 |
| Soft abort via `None` | Task 1 Step 4 |
| `is_success=lambda s: not s.exec_failures` | Task 1 Step 5 |
| Skip helper when no initial failures | Task 1 Step 5 (`if state.exec_failures`) |
| Preserve 3-iteration bound + agent sequence | Task 1 Steps 4–5 |
| Existing devops tests pass | Task 1 Step 7, Task 2 |
| No agent-internal / inheritance / helper API changes | Global Constraints |
| 90% coverage + `make test` / `make lint` | Task 2 |

No placeholders remain. Types (`_DebugPatchState`, `_debug_patch_once` signature) are consistent across tasks.
