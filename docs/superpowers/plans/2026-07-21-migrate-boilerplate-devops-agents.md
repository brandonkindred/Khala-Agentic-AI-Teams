# Migrate Boilerplate DevOps Agents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite `InfrastructureAsCodeAgent`, `CICDPipelineAgent`, and `DeploymentStrategyAgent` as thin `DevOpsSingleShotAgent` subclasses with identical behavior.

**Architecture:** Each `agent.py` subclasses the shared base, sets `PROMPT` to the existing prompt constant, and moves today's context/output mapping into `build_context` / `build_output`. No test or orchestrator changes.

**Tech Stack:** Python 3.10+, pytest, ruff, existing `DevOpsSingleShotAgent`.

**Spec:** `docs/superpowers/specs/2026-07-21-migrate-boilerplate-devops-agents-design.md`

## Global Constraints

- Touch only the three `agent.py` files listed in the file map (plus this plan's commits).
- Preserve public class names and `__init__(llm_client)` / `run(input_data)` contracts (inherited from the base).
- Keep context f-strings and `data.get(...)` defaults byte-identical to pre-migration behavior.
- Do not edit `test_devops_team.py`, orchestrator, phase2 graph, prompts, or models.
- Do not migrate the four special-case agents.
- No GitHub issue numbers in code, comments, commit messages, or docs (PR body only).
- DbC: add `Preconditions:` / `Postconditions:` on `build_context` and `build_output` (and class `Invariants:` where a class docstring is added).
- ≥90% line coverage on each touched `agent.py`; `make lint` must pass.

## File map

| Path | Responsibility after change |
|---|---|
| `backend/agents/software_engineering_team/devops_team/iac_agent/agent.py` | `InfrastructureAsCodeAgent(DevOpsSingleShotAgent)` |
| `backend/agents/software_engineering_team/devops_team/cicd_pipeline_agent/agent.py` | `CICDPipelineAgent(DevOpsSingleShotAgent)` |
| `backend/agents/software_engineering_team/devops_team/deployment_strategy_agent/agent.py` | `DeploymentStrategyAgent(DevOpsSingleShotAgent)` |

**Prerequisite:** Local checkout must include `devops_team/_agent_template.py` (merged via the prior template PR). If missing, `git fetch origin && git merge origin/main` (or rebase) before Task 1.

---

### Task 1: Migrate `InfrastructureAsCodeAgent`

**Files:**
- Modify: `backend/agents/software_engineering_team/devops_team/iac_agent/agent.py`
- Test: existing `TestInfrastructureAsCodeAgent` + `test_iac_agent_recovers_fenced_response` in `backend/agents/software_engineering_team/tests/test_devops_team.py`

**Interfaces:**
- Consumes: `DevOpsSingleShotAgent`, `IAC_AGENT_PROMPT`, `IaCAgentInput`, `IaCAgentOutput`
- Produces: `class InfrastructureAsCodeAgent(DevOpsSingleShotAgent)` with `PROMPT`, `build_context`, `build_output`

- [ ] **Step 1: Confirm baseline tests pass (refactor — no new failing test)**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py::TestInfrastructureAsCodeAgent \
  agents/software_engineering_team/tests/test_devops_team.py::TestDevOpsAgentsRecoverFencedJson::test_iac_agent_recovers_fenced_response \
  -v
```

Expected: PASS.

- [ ] **Step 2: Rewrite `iac_agent/agent.py`**

Replace the entire file with:

```python
"""Infrastructure as Code agent."""

from __future__ import annotations

from typing import Any, Dict

from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent

from .models import IaCAgentInput, IaCAgentOutput
from .prompts import IAC_AGENT_PROMPT


class InfrastructureAsCodeAgent(DevOpsSingleShotAgent):
    """Produce IaC artifacts for a devops task via a single structured LLM call.

    Invariants: instance state is limited to ``llm`` and ``_model`` from the
    base; ``run`` is stateless across calls.
    """

    PROMPT = IAC_AGENT_PROMPT

    def build_context(self, input_data: IaCAgentInput) -> str:
        """Build the IaC prompt context from the task spec and repo summary.

        Preconditions: ``input_data`` is a valid ``IaCAgentInput``.
        Postconditions: returns the same context string shape the pre-migration
        agent appended after the prompt separator.
        """
        spec = input_data.task_spec
        return (
            f"task_id={spec.task_id}\n"
            f"title={spec.title}\n"
            f"constraints={spec.constraints.model_dump()}\n"
            f"included={spec.scope.included}\n"
            f"excluded={spec.scope.excluded}\n"
            f"repo_summary={input_data.repo_summary}\n"
        )

    def build_output(self, input_data: IaCAgentInput, data: Dict[str, Any]) -> IaCAgentOutput:
        """Map the LLM JSON dict onto ``IaCAgentOutput``.

        Preconditions: ``data`` is the dict from ``complete_json_with_continuation``.
        Postconditions: returns ``IaCAgentOutput`` with the same field defaults as
        the pre-migration agent (``artifacts``/``blast_radius_notes`` empty
        collections, empty string summaries, bool destructive flag).
        """
        return IaCAgentOutput(
            artifacts=data.get("artifacts") or {},
            summary=data.get("summary", ""),
            plan_summary=data.get("plan_summary", ""),
            destructive_changes_detected=bool(data.get("destructive_changes_detected", False)),
            blast_radius_notes=data.get("blast_radius_notes") or [],
        )
```

- [ ] **Step 3: Re-run IaC tests**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py::TestInfrastructureAsCodeAgent \
  agents/software_engineering_team/tests/test_devops_team.py::TestDevOpsAgentsRecoverFencedJson::test_iac_agent_recovers_fenced_response \
  -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add backend/agents/software_engineering_team/devops_team/iac_agent/agent.py
git commit -m "$(cat <<'EOF'
Migrate InfrastructureAsCodeAgent onto DevOpsSingleShotAgent.

Keep the same context and output mapping; drop duplicated LLM scaffolding.
EOF
)"
```

---

### Task 2: Migrate `CICDPipelineAgent`

**Files:**
- Modify: `backend/agents/software_engineering_team/devops_team/cicd_pipeline_agent/agent.py`
- Test: existing `TestCICDPipelineAgent` + `test_cicd_pipeline_agent_recovers_fenced_response`

**Interfaces:**
- Consumes: `DevOpsSingleShotAgent`, `CICD_PIPELINE_PROMPT`, `CICDPipelineAgentInput`, `CICDPipelineAgentOutput`
- Produces: `class CICDPipelineAgent(DevOpsSingleShotAgent)` with `PROMPT`, `build_context`, `build_output`

- [ ] **Step 1: Confirm baseline CICD tests pass**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py::TestCICDPipelineAgent \
  agents/software_engineering_team/tests/test_devops_team.py::TestDevOpsAgentsRecoverFencedJson::test_cicd_pipeline_agent_recovers_fenced_response \
  -v
```

Expected: PASS.

- [ ] **Step 2: Rewrite `cicd_pipeline_agent/agent.py`**

Replace the entire file with:

```python
"""CI/CD pipeline agent."""

from __future__ import annotations

from typing import Any, Dict

from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent

from .models import CICDPipelineAgentInput, CICDPipelineAgentOutput
from .prompts import CICD_PIPELINE_PROMPT


class CICDPipelineAgent(DevOpsSingleShotAgent):
    """Produce CI/CD pipeline artifacts via a single structured LLM call.

    Invariants: instance state is limited to ``llm`` and ``_model`` from the
    base; ``run`` is stateless across calls.
    """

    PROMPT = CICD_PIPELINE_PROMPT

    def build_context(self, input_data: CICDPipelineAgentInput) -> str:
        """Build the CI/CD prompt context from the task spec and existing pipeline.

        Preconditions: ``input_data`` is a valid ``CICDPipelineAgentInput``.
        Postconditions: returns the same context string shape the pre-migration
        agent appended after the prompt separator.
        """
        spec = input_data.task_spec
        return (
            f"task_id={spec.task_id}\n"
            f"title={spec.title}\n"
            f"environments={spec.platform_scope.environments}\n"
            f"constraints={spec.constraints.model_dump()}\n"
            f"acceptance_criteria={spec.acceptance_criteria}\n"
            f"existing_pipeline={input_data.existing_pipeline}\n"
        )

    def build_output(
        self, input_data: CICDPipelineAgentInput, data: Dict[str, Any]
    ) -> CICDPipelineAgentOutput:
        """Map the LLM JSON dict onto ``CICDPipelineAgentOutput``.

        Preconditions: ``data`` is the dict from ``complete_json_with_continuation``.
        Postconditions: returns ``CICDPipelineAgentOutput`` with the same field
        defaults as the pre-migration agent.
        """
        return CICDPipelineAgentOutput(
            artifacts=data.get("artifacts") or {},
            pipeline_job_graph_summary=data.get("pipeline_job_graph_summary", ""),
            required_gates_present=bool(data.get("required_gates_present", False)),
            summary=data.get("summary", ""),
            risks=data.get("risks") or [],
        )
```

- [ ] **Step 3: Re-run CICD tests**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py::TestCICDPipelineAgent \
  agents/software_engineering_team/tests/test_devops_team.py::TestDevOpsAgentsRecoverFencedJson::test_cicd_pipeline_agent_recovers_fenced_response \
  -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add backend/agents/software_engineering_team/devops_team/cicd_pipeline_agent/agent.py
git commit -m "$(cat <<'EOF'
Migrate CICDPipelineAgent onto DevOpsSingleShotAgent.

Preserve pipeline context and output mapping while using the shared base.
EOF
)"
```

---

### Task 3: Migrate `DeploymentStrategyAgent`

**Files:**
- Modify: `backend/agents/software_engineering_team/devops_team/deployment_strategy_agent/agent.py`
- Test: existing `TestDeploymentStrategyAgent` + `test_deployment_strategy_agent_recovers_fenced_response`

**Interfaces:**
- Consumes: `DevOpsSingleShotAgent`, `DEPLOYMENT_STRATEGY_PROMPT`, `DeploymentStrategyAgentInput`, `DeploymentStrategyAgentOutput`
- Produces: `class DeploymentStrategyAgent(DevOpsSingleShotAgent)` with `PROMPT`, `build_context`, `build_output`

- [ ] **Step 1: Confirm baseline deployment tests pass**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py::TestDeploymentStrategyAgent \
  agents/software_engineering_team/tests/test_devops_team.py::TestDevOpsAgentsRecoverFencedJson::test_deployment_strategy_agent_recovers_fenced_response \
  -v
```

Expected: PASS.

- [ ] **Step 2: Rewrite `deployment_strategy_agent/agent.py`**

Replace the entire file with:

```python
"""Deployment strategy agent."""

from __future__ import annotations

from typing import Any, Dict

from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent

from .models import DeploymentStrategyAgentInput, DeploymentStrategyAgentOutput
from .prompts import DEPLOYMENT_STRATEGY_PROMPT


class DeploymentStrategyAgent(DevOpsSingleShotAgent):
    """Produce deployment strategy artifacts via a single structured LLM call.

    Invariants: instance state is limited to ``llm`` and ``_model`` from the
    base; ``run`` is stateless across calls.
    """

    PROMPT = DEPLOYMENT_STRATEGY_PROMPT

    def build_context(self, input_data: DeploymentStrategyAgentInput) -> str:
        """Build the deployment prompt context from the task spec.

        Preconditions: ``input_data`` is a valid ``DeploymentStrategyAgentInput``.
        Postconditions: returns the same context string shape the pre-migration
        agent appended after the prompt separator.
        """
        spec = input_data.task_spec
        return (
            f"task_id={spec.task_id}\n"
            f"constraints={spec.constraints.model_dump()}\n"
            f"environments={spec.platform_scope.environments}\n"
            f"acceptance_criteria={spec.acceptance_criteria}\n"
            f"nfr={spec.non_functional_requirements}\n"
        )

    def build_output(
        self, input_data: DeploymentStrategyAgentInput, data: Dict[str, Any]
    ) -> DeploymentStrategyAgentOutput:
        """Map the LLM JSON dict onto ``DeploymentStrategyAgentOutput``.

        Preconditions: ``data`` is the dict from ``complete_json_with_continuation``.
        Postconditions: returns ``DeploymentStrategyAgentOutput`` with the same
        field defaults as the pre-migration agent, including
        ``rollout_timeout_minutes=int(data.get(..., 15) or 15)``.
        """
        return DeploymentStrategyAgentOutput(
            artifacts=data.get("artifacts") or {},
            strategy=data.get("strategy", ""),
            rollback_plan=data.get("rollback_plan") or [],
            health_checks=data.get("health_checks") or [],
            rollout_timeout_minutes=int(data.get("rollout_timeout_minutes", 15) or 15),
            summary=data.get("summary", ""),
        )
```

- [ ] **Step 3: Re-run deployment tests**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py::TestDeploymentStrategyAgent \
  agents/software_engineering_team/tests/test_devops_team.py::TestDevOpsAgentsRecoverFencedJson::test_deployment_strategy_agent_recovers_fenced_response \
  -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add backend/agents/software_engineering_team/devops_team/deployment_strategy_agent/agent.py
git commit -m "$(cat <<'EOF'
Migrate DeploymentStrategyAgent onto DevOpsSingleShotAgent.

Keep rollout timeout and strategy field defaults identical to before.
EOF
)"
```

---

### Task 4: Lint and coverage verification

**Files:**
- Verify: the three rewritten `agent.py` files (edit only if lint/coverage requires)

**Interfaces:**
- Consumes: Tasks 1–3
- Produces: lint-clean files; ≥90% coverage on each touched `agent.py`

- [ ] **Step 1: Ruff check + format**

```bash
cd backend && python -m ruff check \
  agents/software_engineering_team/devops_team/iac_agent/agent.py \
  agents/software_engineering_team/devops_team/cicd_pipeline_agent/agent.py \
  agents/software_engineering_team/devops_team/deployment_strategy_agent/agent.py \
&& python -m ruff format --check \
  agents/software_engineering_team/devops_team/iac_agent/agent.py \
  agents/software_engineering_team/devops_team/cicd_pipeline_agent/agent.py \
  agents/software_engineering_team/devops_team/deployment_strategy_agent/agent.py
```

Expected: exit 0. If format fails, run `python -m ruff format` on those paths and re-check.

- [ ] **Step 2: Coverage on the three agents**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py::TestInfrastructureAsCodeAgent \
  agents/software_engineering_team/tests/test_devops_team.py::TestCICDPipelineAgent \
  agents/software_engineering_team/tests/test_devops_team.py::TestDeploymentStrategyAgent \
  agents/software_engineering_team/tests/test_devops_team.py::TestDevOpsAgentsRecoverFencedJson::test_iac_agent_recovers_fenced_response \
  agents/software_engineering_team/tests/test_devops_team.py::TestDevOpsAgentsRecoverFencedJson::test_cicd_pipeline_agent_recovers_fenced_response \
  agents/software_engineering_team/tests/test_devops_team.py::TestDevOpsAgentsRecoverFencedJson::test_deployment_strategy_agent_recovers_fenced_response \
  -v \
  --cov=software_engineering_team.devops_team.iac_agent.agent \
  --cov=software_engineering_team.devops_team.cicd_pipeline_agent.agent \
  --cov=software_engineering_team.devops_team.deployment_strategy_agent.agent \
  --cov-report=term-missing \
  --cov-fail-under=90
```

Expected: PASS with ≥90% on each module.

- [ ] **Step 3: Confirm no unintended file changes**

```bash
git status --short backend/agents/software_engineering_team/devops_team/
```

Expected: only the three `*/agent.py` files modified (committed); no edits under `infra_*`, `devsecops_*`, `doc_runbook_*`, `orchestrator.py`, or `test_devops_team.py`.

- [ ] **Step 4: Commit any lint/format fixes** (skip if clean)

```bash
git add backend/agents/software_engineering_team/devops_team/iac_agent/agent.py \
  backend/agents/software_engineering_team/devops_team/cicd_pipeline_agent/agent.py \
  backend/agents/software_engineering_team/devops_team/deployment_strategy_agent/agent.py
git commit -m "$(cat <<'EOF'
Apply ruff formatting to migrated devops agent modules.
EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| IaC subclass migration | Task 1 |
| CICD subclass migration | Task 2 |
| Deployment subclass migration | Task 3 |
| No test/orchestrator/prompt/model edits | Tasks 1–3 (out of scope) + Task 4 Step 3 |
| Lint + ≥90% coverage | Task 4 |
| Byte-identical field mapping | Inlined in each Task 2 rewrite |

## Self-review notes

- No TBD placeholders; full file contents inlined.
- Class/method names match the shipped `DevOpsSingleShotAgent` API (`PROMPT`, `build_context`, `build_output`).
- Deployment `rollout_timeout_minutes` expression preserved exactly.
