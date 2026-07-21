# Migrate Infra Patch/Debug Agents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite `InfraPatchAgent` and `InfraDebugAgent` as thin `DevOpsSingleShotAgent` subclasses that preserve early-return and derived-field behavior via `pre_call` / `build_output`.

**Architecture:** Each `agent.py` subclasses the shared base, sets `PROMPT` to the existing prompt constant, and moves today's context/output mapping into `build_context` / `build_output`. Patch adds `pre_call` for the not-fixable early return; debug keeps module-level `_FIXABLE_TYPES` and derives `fixable` inside `build_output`. No test or orchestrator changes.

**Tech Stack:** Python 3.10+, pytest, ruff, existing `DevOpsSingleShotAgent`.

**Spec:** `docs/superpowers/specs/2026-07-21-migrate-infra-patch-debug-agents-design.md`

## Global Constraints

- Touch only the two `agent.py` files listed in the file map (plus this plan's commits / design+plan docs if not yet committed).
- Preserve public class names `InfraPatchAgent` / `InfraDebugAgent` and `__init__(llm_client)` / `run(input_data)` contracts (inherited from the base).
- Keep context strings, empty-artifact filtering, `IaCExecutionError` construction, and `data.get(...)` defaults byte-identical to pre-migration behavior.
- Do not edit `test_devops_debug_patch.py`, `test_devops_team.py`, orchestrator, phase2 graph, prompts, models, or `_agent_template.py`.
- Do not migrate `devsecops_review_agent` or `doc_runbook_agent`.
- No GitHub issue numbers in code, comments, commit messages, or docs (PR body only).
- DbC: add `Preconditions:` / `Postconditions:` on `pre_call` (patch), `build_context`, and `build_output` (and class `Invariants:` where a class docstring is added).
- ≥90% line coverage on each touched `agent.py`; `make lint` must pass.

## File map

| Path | Responsibility after change |
|---|---|
| `backend/agents/software_engineering_team/devops_team/infra_patch_agent/agent.py` | `InfraPatchAgent(DevOpsSingleShotAgent)` with `pre_call` + `build_context` + `build_output` |
| `backend/agents/software_engineering_team/devops_team/infra_debug_agent/agent.py` | `InfraDebugAgent(DevOpsSingleShotAgent)` with `build_context` + `build_output`; module-level `_FIXABLE_TYPES` |

**Prerequisite:** Local checkout must be based on `refactor/migrate-boilerplate-devops-agents` (or equivalent) so `devops_team/_agent_template.py` exists and the three boilerplate agents are already migrated. Create a new branch from that tip before Task 1, e.g.:

```bash
git fetch origin
git checkout -b refactor/migrate-infra-patch-debug-agents refactor/migrate-boilerplate-devops-agents
```

If working in an isolated worktree, create it from that base (via `superpowers:using-git-worktrees` at execution time).

---

### Task 1: Migrate `InfraPatchAgent`

**Files:**
- Modify: `backend/agents/software_engineering_team/devops_team/infra_patch_agent/agent.py`
- Test: existing `TestInfraPatchAgent` in `backend/agents/software_engineering_team/tests/test_devops_debug_patch.py` + `test_infra_patch_agent_recovers_fenced_response` in `test_devops_team.py`

**Interfaces:**
- Consumes: `DevOpsSingleShotAgent`, `INFRA_PATCH_PROMPT`, `IaCPatchInput`, `IaCPatchOutput`
- Produces: `class InfraPatchAgent(DevOpsSingleShotAgent)` with `PROMPT`, `pre_call`, `build_context`, `build_output`

- [ ] **Step 1: Confirm baseline tests pass (refactor — no new failing test)**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_debug_patch.py::TestInfraPatchAgent \
  agents/software_engineering_team/tests/test_devops_team.py::TestDevOpsAgentsRecoverFencedJson::test_infra_patch_agent_recovers_fenced_response \
  -v
```

Expected: PASS.

- [ ] **Step 2: Rewrite `infra_patch_agent/agent.py`**

Replace the entire file with:

```python
"""Infrastructure Patch agent -- produces minimal IaC artifact patches."""

from __future__ import annotations

from typing import Any, Dict, Optional

from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent

from .models import IaCPatchInput, IaCPatchOutput
from .prompts import INFRA_PATCH_PROMPT


class InfraPatchAgent(DevOpsSingleShotAgent):
    """Produce minimal IaC artifact patches from classified debug errors.

    Invariants: instance state is limited to ``llm`` and ``_model`` from the
    base; ``run`` is stateless across calls.
    """

    PROMPT = INFRA_PATCH_PROMPT

    def pre_call(self, input_data: IaCPatchInput) -> Optional[IaCPatchOutput]:
        """Skip the LLM when errors are not fixable via code changes.

        Preconditions: ``input_data`` is a valid ``IaCPatchInput``.
        Postconditions: returns ``IaCPatchOutput(summary=...)`` when
        ``debug_output.fixable`` is false; otherwise ``None`` so ``run``
        continues to the LLM call.
        """
        if not input_data.debug_output.fixable:
            return IaCPatchOutput(
                summary="Errors are not fixable via code changes",
            )
        return None

    def build_context(self, input_data: IaCPatchInput) -> str:
        """Build the patch prompt context from errors and current artifacts.

        Preconditions: ``pre_call`` returned ``None``; ``input_data`` is valid.
        Postconditions: returns the same context string shape the pre-migration
        agent appended after the prompt separator.
        """
        errors_text = "\n".join(
            f"- [{e.error_type}] {e.file_path or '?'}:{e.line_number or '?'} — {e.error_message}"
            for e in input_data.debug_output.errors
        )

        artifacts_text = ""
        for fname, content in input_data.original_artifacts.items():
            artifacts_text += f"\n### {fname} ###\n{content}\n"

        return f"--- Errors ---\n{errors_text}\n\n--- Current Artifacts ---\n{artifacts_text}\n"

    def build_output(self, input_data: IaCPatchInput, data: Dict[str, Any]) -> IaCPatchOutput:
        """Map the LLM JSON dict onto ``IaCPatchOutput``.

        Preconditions: ``data`` is the dict from ``complete_json_with_continuation``.
        Postconditions: returns ``IaCPatchOutput`` with empty patched entries
        filtered out and the same ``summary`` / ``edits_applied`` defaults as
        the pre-migration agent.
        """
        patched = data.get("patched_artifacts") or {}
        patched = {k: v for k, v in patched.items() if v and v.strip()}

        return IaCPatchOutput(
            patched_artifacts=patched,
            summary=data.get("summary", ""),
            edits_applied=data.get("edits_applied", len(patched)),
        )
```

- [ ] **Step 3: Re-run patch tests**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_debug_patch.py::TestInfraPatchAgent \
  agents/software_engineering_team/tests/test_devops_team.py::TestDevOpsAgentsRecoverFencedJson::test_infra_patch_agent_recovers_fenced_response \
  -v
```

Expected: PASS (including `test_returns_empty_when_not_fixable` trip-wire).

- [ ] **Step 4: Commit**

```bash
git add backend/agents/software_engineering_team/devops_team/infra_patch_agent/agent.py
git commit -m "$(cat <<'EOF'
Migrate InfraPatchAgent onto DevOpsSingleShotAgent.

Preserve the not-fixable early return via pre_call; drop duplicated LLM scaffolding.
EOF
)"
```

---

### Task 2: Migrate `InfraDebugAgent`

**Files:**
- Modify: `backend/agents/software_engineering_team/devops_team/infra_debug_agent/agent.py`
- Test: existing `TestInfraDebugAgent` in `test_devops_debug_patch.py` + `test_infra_debug_agent_recovers_fenced_response` in `test_devops_team.py`

**Interfaces:**
- Consumes: `DevOpsSingleShotAgent`, `INFRA_DEBUG_PROMPT`, `IaCDebugInput`, `IaCDebugOutput`, `IaCExecutionError`
- Produces: `class InfraDebugAgent(DevOpsSingleShotAgent)` with `PROMPT`, `build_context`, `build_output`; module-level `_FIXABLE_TYPES`

- [ ] **Step 1: Confirm baseline debug tests pass**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_debug_patch.py::TestInfraDebugAgent \
  agents/software_engineering_team/tests/test_devops_team.py::TestDevOpsAgentsRecoverFencedJson::test_infra_debug_agent_recovers_fenced_response \
  -v
```

Expected: PASS.

- [ ] **Step 2: Rewrite `infra_debug_agent/agent.py`**

Replace the entire file with:

```python
"""Infrastructure Debug agent -- classifies IaC execution errors."""

from __future__ import annotations

from typing import Any, Dict

from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent

from .models import IaCDebugInput, IaCDebugOutput, IaCExecutionError
from .prompts import INFRA_DEBUG_PROMPT

_FIXABLE_TYPES = frozenset({"syntax", "validation"})


class InfraDebugAgent(DevOpsSingleShotAgent):
    """Classify IaC execution errors and derive whether they are fixable.

    Invariants: instance state is limited to ``llm`` and ``_model`` from the
    base; ``run`` is stateless across calls. ``_FIXABLE_TYPES`` remains a
    module-level frozenset used by ``build_output``.
    """

    PROMPT = INFRA_DEBUG_PROMPT

    def build_context(self, input_data: IaCDebugInput) -> str:
        """Build the debug prompt context from execution output and artifacts.

        Preconditions: ``input_data`` is a valid ``IaCDebugInput``.
        Postconditions: returns the same context string shape the pre-migration
        agent appended after the prompt separator (first five artifacts,
        2000-char snippets).
        """
        artifacts_snippet = ""
        for fname, content in list(input_data.artifacts.items())[:5]:
            artifacts_snippet += f"\n### {fname} ###\n{content[:2000]}\n"

        return (
            f"Tool: {input_data.tool_name}\n"
            f"Command: {input_data.command}\n\n"
            f"--- Execution Output ---\n{input_data.execution_output}\n\n"
            f"--- Artifacts ---\n{artifacts_snippet}\n"
        )

    def build_output(self, input_data: IaCDebugInput, data: Dict[str, Any]) -> IaCDebugOutput:
        """Map the LLM JSON dict onto ``IaCDebugOutput`` with derived fixable.

        Preconditions: ``data`` is the dict from ``complete_json_with_continuation``.
        Postconditions: returns ``IaCDebugOutput`` with the same
        ``IaCExecutionError`` field defaults, ``raw_output`` from the input,
        and ``fixable=data.get("fixable", derived)`` where derived is true
        iff every error type is in ``_FIXABLE_TYPES`` and the list is non-empty.
        """
        errors = []
        for err_data in data.get("errors") or []:
            errors.append(
                IaCExecutionError(
                    error_type=err_data.get("error_type", "unknown"),
                    tool=err_data.get("tool", input_data.tool_name),
                    file_path=err_data.get("file_path"),
                    line_number=err_data.get("line_number"),
                    error_message=err_data.get("error_message", ""),
                    raw_output=input_data.execution_output,
                )
            )

        fixable = bool(errors) and all(e.error_type in _FIXABLE_TYPES for e in errors)

        return IaCDebugOutput(
            errors=errors,
            summary=data.get("summary", ""),
            fixable=data.get("fixable", fixable),
        )
```

- [ ] **Step 3: Re-run debug tests**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_debug_patch.py::TestInfraDebugAgent \
  agents/software_engineering_team/tests/test_devops_team.py::TestDevOpsAgentsRecoverFencedJson::test_infra_debug_agent_recovers_fenced_response \
  -v
```

Expected: PASS (including `_FIXABLE_TYPES` derivation cases).

- [ ] **Step 4: Commit**

```bash
git add backend/agents/software_engineering_team/devops_team/infra_debug_agent/agent.py
git commit -m "$(cat <<'EOF'
Migrate InfraDebugAgent onto DevOpsSingleShotAgent.

Keep IaCExecutionError construction and fixable derivation in build_output.
EOF
)"
```

---

### Task 3: Final verification

**Files:**
- None (verification only)

**Interfaces:**
- Consumes: both migrated agents from Tasks 1–2
- Produces: confirmation that focused tests, pipeline loop coverage, lint, and line coverage meet the acceptance floor

- [ ] **Step 1: Run the full related test suites**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_debug_patch.py \
  agents/software_engineering_team/tests/test_devops_team.py::TestDevOpsAgentsRecoverFencedJson::test_infra_debug_agent_recovers_fenced_response \
  agents/software_engineering_team/tests/test_devops_team.py::TestDevOpsAgentsRecoverFencedJson::test_infra_patch_agent_recovers_fenced_response \
  -v
```

Expected: PASS.

- [ ] **Step 2: Confirm ≥90% line coverage on the two rewritten files**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_debug_patch.py::TestInfraPatchAgent \
  agents/software_engineering_team/tests/test_devops_debug_patch.py::TestInfraDebugAgent \
  agents/software_engineering_team/tests/test_devops_team.py::TestDevOpsAgentsRecoverFencedJson::test_infra_debug_agent_recovers_fenced_response \
  agents/software_engineering_team/tests/test_devops_team.py::TestDevOpsAgentsRecoverFencedJson::test_infra_patch_agent_recovers_fenced_response \
  --cov=software_engineering_team.devops_team.infra_patch_agent.agent \
  --cov=software_engineering_team.devops_team.infra_debug_agent.agent \
  --cov-report=term-missing \
  --cov-fail-under=90 \
  -v
```

Expected: PASS with each file ≥90% line coverage.

- [ ] **Step 3: Run lint**

```bash
cd backend && make lint
```

Expected: PASS (ruff check + format clean).

- [ ] **Step 4: No commit unless Step 2/3 required fixes** — if lint or coverage forced edits, commit those fixes with a message describing why (not “fix lint”).
