# Migrate DevSecOpsReviewAgent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite `DevSecOpsReviewAgent` as a thin `DevOpsSingleShotAgent` subclass that preserves `temperature=0.0` and absent-vs-null `derive_approved` behavior via class attrs and `build_output`.

**Architecture:** Subclass the shared base, set `PROMPT` and `temperature = 0.0`, move today's context/output mapping into `build_context` / `build_output`, and relocate the former `run` Preconditions/Postconditions onto `build_output`. No test or orchestrator changes.

**Tech Stack:** Python 3.10+, pytest, ruff, existing `DevOpsSingleShotAgent`, `derive_approved`.

**Spec:** `docs/superpowers/specs/2026-07-21-migrate-devsecops-review-agent-design.md`

## Global Constraints

- Touch only `devsecops_review_agent/agent.py` (plus this plan / design docs if not yet committed).
- Preserve public class name `DevSecOpsReviewAgent` and `__init__(llm_client)` / `run(input_data)` contracts (inherited from the base).
- Keep context string, findings mapping, absent-vs-null `approved` handling, and `derive_approved` call byte-identical to pre-migration behavior.
- Do not edit `test_devops_team.py`, orchestrator, phase graphs, prompts, models, `_agent_template.py`, or `shared/security_service.py`.
- Do not migrate `doc_runbook_agent`.
- No GitHub issue numbers in code, comments, commit messages, or docs (PR body only).
- DbC: class `Invariants:`; `Preconditions:` / `Postconditions:` on `build_context` and `build_output` (carry over today's `run` contract text onto `build_output`).
- ≥90% line coverage on the touched `agent.py`; ruff clean on touched files.

## File map

| Path | Responsibility after change |
|---|---|
| `backend/agents/software_engineering_team/devops_team/devsecops_review_agent/agent.py` | `DevSecOpsReviewAgent(DevOpsSingleShotAgent)` with `PROMPT`, `temperature = 0.0`, `build_context`, `build_output` |

**Prerequisite:** Local checkout must be based on `refactor/migrate-boilerplate-devops-agents` (or equivalent tip that includes `_agent_template.py` and prior migrations). Branch already created as `refactor/migrate-devsecops-review-agent` in worktree `.worktrees/refactor/migrate-devsecops-review-agent`.

---

### Task 1: Migrate `DevSecOpsReviewAgent`

**Files:**
- Modify: `backend/agents/software_engineering_team/devops_team/devsecops_review_agent/agent.py`
- Test: existing `TestDevSecOpsReviewAgent` in `backend/agents/software_engineering_team/tests/test_devops_team.py` + `test_devsecops_review_agent_recovers_fenced_response`

**Interfaces:**
- Consumes: `DevOpsSingleShotAgent`, `DEVSECOPS_REVIEW_PROMPT`, `DevSecOpsReviewInput`, `DevSecOpsReviewOutput`, `ReviewFinding`, `derive_approved`
- Produces: `class DevSecOpsReviewAgent(DevOpsSingleShotAgent)` with `PROMPT`, `temperature = 0.0`, `build_context`, `build_output`

- [ ] **Step 1: Confirm baseline tests pass (refactor — no new failing test)**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py::TestDevSecOpsReviewAgent \
  agents/software_engineering_team/tests/test_devops_team.py::TestDevOpsAgentsRecoverFencedJson::test_devsecops_review_agent_recovers_fenced_response \
  -v
```

Expected: PASS (including null fail-closed and absent defer cases).

- [ ] **Step 2: Rewrite `devsecops_review_agent/agent.py`**

Replace the entire file with:

```python
"""DevSecOps review agent."""

from __future__ import annotations

from typing import Any, Dict

from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent
from software_engineering_team.devops_team.models import ReviewFinding
from software_engineering_team.shared.security_service import derive_approved

from .models import DevSecOpsReviewInput, DevSecOpsReviewOutput
from .prompts import DEVSECOPS_REVIEW_PROMPT


class DevSecOpsReviewAgent(DevOpsSingleShotAgent):
    """Infra security reviewer for DevOps artifacts (IAM/secrets/network).

    Invariants: instance state is limited to ``llm`` and the resolved Strands
    ``_model`` from the base; ``run`` is stateless across calls.
    """

    PROMPT = DEVSECOPS_REVIEW_PROMPT
    temperature = 0.0

    def build_context(self, input_data: DevSecOpsReviewInput) -> str:
        """Build the review prompt context from task, requirements, and artifacts.

        Preconditions: ``input_data`` is a valid ``DevSecOpsReviewInput``.
        Postconditions: returns the same context string shape the pre-migration
        agent appended after the prompt separator.
        """
        return (
            f"task={input_data.task_description}\n"
            f"requirements={input_data.requirements}\n"
            f"artifacts={list(input_data.artifacts.keys())}\n"
        )

    def build_output(
        self, input_data: DevSecOpsReviewInput, data: Dict[str, Any]
    ) -> DevSecOpsReviewOutput:
        """Map the LLM JSON dict onto ``DevSecOpsReviewOutput``.

        Preconditions:
            ``data`` is the dict from ``complete_json_with_continuation``; it may
            include optional ``findings``/``approved``/``summary``.
        Postconditions:
            Returns a ``DevSecOpsReviewOutput`` whose ``approved`` follows the
            unified rule (:func:`derive_approved`): any blocking finding
            (critical/high severity or an explicit ``blocking`` flag) forces
            ``approved=False``; otherwise the model's ``approved`` is honored. An
            ``approved`` key that is present but null is treated as a non-approval
            (fail closed), matching the legacy contract; an absent key defers to
            the finding-derived default.
        """
        findings = [
            ReviewFinding(**f) for f in (data.get("findings") or []) if isinstance(f, dict)
        ]
        # Distinguish an absent ``approved`` key (no opinion -> defer to findings)
        # from a present-but-null value (an explicit non-approval -> fail closed).
        llm_approved = bool(data["approved"]) if "approved" in data else None
        approved = derive_approved(findings, llm_approved=llm_approved)
        return DevSecOpsReviewOutput(
            approved=approved,
            findings=findings,
            summary=data.get("summary", ""),
        )
```

- [ ] **Step 3: Re-run DevSecOps tests**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py::TestDevSecOpsReviewAgent \
  agents/software_engineering_team/tests/test_devops_team.py::TestDevOpsAgentsRecoverFencedJson::test_devsecops_review_agent_recovers_fenced_response \
  -v
```

Expected: PASS (all four unit cases + fence recovery).

- [ ] **Step 4: Commit**

```bash
git add backend/agents/software_engineering_team/devops_team/devsecops_review_agent/agent.py
git commit -m "$(cat <<'EOF'
Migrate DevSecOpsReviewAgent onto DevOpsSingleShotAgent.

Preserve temperature=0.0 and derive_approved absent-vs-null handling in build_output.
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
  agents/software_engineering_team/tests/test_devops_team.py::TestDevSecOpsReviewAgent \
  agents/software_engineering_team/tests/test_devops_team.py::TestDevOpsAgentsRecoverFencedJson::test_devsecops_review_agent_recovers_fenced_response \
  --cov=software_engineering_team.devops_team.devsecops_review_agent.agent \
  --cov-report=term-missing \
  --cov-fail-under=90 \
  -v
```

Expected: PASS with ≥90% line coverage.

- [ ] **Step 2: Run ruff on the touched file**

```bash
cd backend && python -m ruff check \
  agents/software_engineering_team/devops_team/devsecops_review_agent/agent.py \
  && python -m ruff format --check \
  agents/software_engineering_team/devops_team/devsecops_review_agent/agent.py
```

Expected: All checks passed; already formatted.

- [ ] **Step 3: No commit unless Step 1/2 required fixes** — if lint or coverage forced edits, commit those fixes with a message describing why (not “fix lint”).
