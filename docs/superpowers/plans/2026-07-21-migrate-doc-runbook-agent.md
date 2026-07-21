# Migrate DocumentationRunbookAgent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite `DocumentationRunbookAgent` as a thin `DevOpsSingleShotAgent` subclass that omits `temperature`/`think` kwargs and builds `DevOpsCompletionPackage` in `build_output`.

**Architecture:** Subclass the shared base, set `PROMPT` and `temperature = think = None`, move today's context/output mapping into `build_context` / `build_output`, and add sibling-style DbC docstrings. No test or orchestrator changes.

**Tech Stack:** Python 3.10+, pytest, ruff, existing `DevOpsSingleShotAgent`.

**Spec:** `docs/superpowers/specs/2026-07-21-migrate-doc-runbook-agent-design.md`

## Global Constraints

- Touch only `doc_runbook_agent/agent.py` (plus this plan / design docs if not yet committed).
- Preserve public class name `DocumentationRunbookAgent` and `__init__(llm_client)` / `run(input_data)` contracts (inherited from the base).
- Keep context string, `DevOpsCompletionPackage` field values, and `files`/`summary` defaults byte-identical to pre-migration behavior.
- Do not edit `test_devops_team.py`, orchestrator, phase graphs, prompts, models, or `_agent_template.py`.
- Set `temperature = None` and `think = None` (omit kwargs); do not set them to default-equivalent explicit values.
- No GitHub issue numbers in code, comments, commit messages, or docs (PR body only).
- DbC: class `Invariants:`; `Preconditions:` / `Postconditions:` on `build_context` and `build_output`.
- ≥90% line coverage on the touched `agent.py`; ruff clean on touched files.

## File map

| Path | Responsibility after change |
|---|---|
| `backend/agents/software_engineering_team/devops_team/doc_runbook_agent/agent.py` | `DocumentationRunbookAgent(DevOpsSingleShotAgent)` with `PROMPT`, `temperature = None`, `think = None`, `build_context`, `build_output` |

**Prerequisite:** Branch `refactor/migrate-doc-runbook-agent` from `origin/refactor/migrate-boilerplate-devops-agents` in worktree `.worktrees/refactor/migrate-doc-runbook-agent`.

---

### Task 1: Migrate `DocumentationRunbookAgent`

**Files:**
- Modify: `backend/agents/software_engineering_team/devops_team/doc_runbook_agent/agent.py`
- Test: existing `TestDocumentationRunbookAgent` in `test_devops_team.py` + `test_doc_runbook_agent_recovers_fenced_response`

**Interfaces:**
- Consumes: `DevOpsSingleShotAgent`, `DOC_RUNBOOK_PROMPT`, `DocumentationRunbookInput`, `DocumentationRunbookOutput`, `DevOpsCompletionPackage`, `GitOperationsMetadata`, `HandoffInfo`, `ReleaseReadiness`
- Produces: `class DocumentationRunbookAgent(DevOpsSingleShotAgent)` with `PROMPT`, `temperature = None`, `think = None`, `build_context`, `build_output`

- [ ] **Step 1: Confirm baseline tests pass (refactor — no new failing test)**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py::TestDocumentationRunbookAgent \
  agents/software_engineering_team/tests/test_devops_team.py::TestDevOpsAgentsRecoverFencedJson::test_doc_runbook_agent_recovers_fenced_response \
  -v
```

Expected: PASS.

- [ ] **Step 2: Rewrite `doc_runbook_agent/agent.py`**

Replace the entire file with:

```python
"""Documentation and runbook agent."""

from __future__ import annotations

from typing import Any, Dict, Optional

from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent
from software_engineering_team.devops_team.models import (
    DevOpsCompletionPackage,
    GitOperationsMetadata,
    HandoffInfo,
    ReleaseReadiness,
)

from .models import DocumentationRunbookInput, DocumentationRunbookOutput
from .prompts import DOC_RUNBOOK_PROMPT


class DocumentationRunbookAgent(DevOpsSingleShotAgent):
    """Produce runbook docs and a completion package for a devops task.

    Invariants: instance state is limited to ``llm`` and ``_model`` from the
    base; ``run`` is stateless across calls.
    """

    PROMPT = DOC_RUNBOOK_PROMPT
    temperature: Optional[float] = None
    think: Optional[bool] = None

    def build_context(self, input_data: DocumentationRunbookInput) -> str:
        """Build the runbook prompt context from task metadata and gates.

        Preconditions: ``input_data`` is a valid ``DocumentationRunbookInput``.
        Postconditions: returns the same context string shape the pre-migration
        agent appended after the prompt separator.
        """
        return (
            f"task_id={input_data.task_id}\n"
            f"task_title={input_data.task_title}\n"
            f"artifacts={list(input_data.artifacts.keys())}\n"
            f"quality_gates={input_data.quality_gates}\n"
            f"notes={input_data.notes}\n"
        )

    def build_output(
        self, input_data: DocumentationRunbookInput, data: Dict[str, Any]
    ) -> DocumentationRunbookOutput:
        """Map the LLM JSON dict onto ``DocumentationRunbookOutput``.

        Preconditions: ``data`` is the dict from ``complete_json_with_continuation``.
        Postconditions: returns ``DocumentationRunbookOutput`` with the same
        non-LLM ``DevOpsCompletionPackage`` construction and the same
        ``files`` / ``summary`` defaults as the pre-migration agent.
        """
        completion = DevOpsCompletionPackage(
            task_id=input_data.task_id,
            status="completed",
            files_changed=sorted(input_data.artifacts.keys()),
            quality_gates={k: v for k, v in input_data.quality_gates.items()},
            release_readiness=ReleaseReadiness(
                deployment_strategy="rolling",
                rollback_available=True,
                alerting_configured=True,
            ),
            notes=input_data.notes,
            git_operations=GitOperationsMetadata(),
            handoff=HandoffInfo(prod_approval_required=True, runbook_updated=True),
        )
        return DocumentationRunbookOutput(
            files=data.get("files") or {},
            completion_package=completion,
            summary=data.get("summary", ""),
        )
```

- [ ] **Step 3: Re-run doc runbook tests**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py::TestDocumentationRunbookAgent \
  agents/software_engineering_team/tests/test_devops_team.py::TestDevOpsAgentsRecoverFencedJson::test_doc_runbook_agent_recovers_fenced_response \
  -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add backend/agents/software_engineering_team/devops_team/doc_runbook_agent/agent.py
git commit -m "$(cat <<'EOF'
Migrate DocumentationRunbookAgent onto DevOpsSingleShotAgent.

Omit temperature/think kwargs; keep DevOpsCompletionPackage construction in build_output.
EOF
)"
```

---

### Task 2: Final verification

**Files:**
- None (verification only)

**Interfaces:**
- Consumes: migrated agent from Task 1
- Produces: confirmation that focused tests, coverage, and lint meet the acceptance floor

- [ ] **Step 1: Confirm ≥90% line coverage on the rewritten file**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py::TestDocumentationRunbookAgent \
  agents/software_engineering_team/tests/test_devops_team.py::TestDevOpsAgentsRecoverFencedJson::test_doc_runbook_agent_recovers_fenced_response \
  --cov=software_engineering_team.devops_team.doc_runbook_agent.agent \
  --cov-report=term-missing \
  --cov-fail-under=90 \
  -v
```

Expected: PASS with ≥90% line coverage.

- [ ] **Step 2: Run ruff on the touched file**

```bash
cd backend && python -m ruff check \
  agents/software_engineering_team/devops_team/doc_runbook_agent/agent.py \
  && python -m ruff format --check \
  agents/software_engineering_team/devops_team/doc_runbook_agent/agent.py
```

Expected: All checks passed; already formatted.

- [ ] **Step 3: No commit unless Step 1/2 required fixes** — if lint or coverage forced edits, commit those fixes with a message describing why (not “fix lint”).
