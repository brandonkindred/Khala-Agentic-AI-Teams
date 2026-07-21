# DevOps Single-Shot Closeout Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the full regression checklist on the migrated devops single-shot stack and open a PR whose body is the closeout note (evidence + confirmation), closing the closeout and parent issues.

**Architecture:** Verification-only on branch `refactor/devops-single-shot-closeout` from the fully migrated stack tip. No production code changes. Design/plan docs already on the branch may be the only file diffs. Capture command output for the PR body.

**Tech Stack:** Python 3.10+, pytest, pytest-cov, ruff, `gh`.

**Spec:** `docs/superpowers/specs/2026-07-21-devops-single-shot-closeout-design.md`

## Global Constraints

- No production code changes to agents, template, orchestrator, or tests.
- Closeout note lives in the PR body only (not a separate closeout markdown artifact beyond this design/plan).
- No GitHub issue numbers in code, comments, commit messages, or docs (PR body / issue comments only).
- Work from the closeout worktree based on `refactor/migrate-boilerplate-devops-agents` tip.
- Use main-repo `backend/.venv/bin/python` if the worktree has no local venv.

## File map

| Path | Responsibility |
|---|---|
| (none required) | Verification artifacts go in the PR body |
| `docs/superpowers/specs/2026-07-21-devops-single-shot-closeout-design.md` | Already committed if present |
| `docs/superpowers/plans/2026-07-21-devops-single-shot-closeout.md` | This plan |

**Touched modules for coverage/lint (migration stack):**

- `software_engineering_team.devops_team._agent_template`
- `…iac_agent.agent`, `…cicd_pipeline_agent.agent`, `…deployment_strategy_agent.agent`
- `…infra_patch_agent.agent`, `…infra_debug_agent.agent`
- `…devsecops_review_agent.agent`, `…doc_runbook_agent.agent`
- `agents/software_engineering_team/tests/test_devops_agent_template.py` (lint only)

---

### Task 1: Run regression suite and static checks

**Files:**
- None (read-only verification)
- Evidence: capture into `.superpowers/sdd/closeout-evidence.md` in the worktree (gitignored scratch OK) for PR drafting

**Interfaces:**
- Consumes: migrated agents on current HEAD
- Produces: pass/fail evidence for tests, imports, lint, coverage

- [ ] **Step 1: Full devops test suites**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py \
  agents/software_engineering_team/tests/test_devops_debug_patch.py \
  -v --tb=short
```

Expected: PASS (all collected tests).

- [ ] **Step 2: Orchestrator + phase2 import smoke**

```bash
cd backend && python - <<'PY'
from llm_service import DummyLLMClient
from software_engineering_team.devops_team.orchestrator import DevOpsTeamLeadAgent
from software_engineering_team.devops_team.phase2_graph import run_phase2_parallel
from software_engineering_team.devops_team.iac_agent import InfrastructureAsCodeAgent, IaCAgentInput
from software_engineering_team.devops_team.cicd_pipeline_agent import CICDPipelineAgent, CICDPipelineAgentInput
from software_engineering_team.devops_team.deployment_strategy_agent import (
    DeploymentStrategyAgent,
    DeploymentStrategyAgentInput,
)

assert callable(run_phase2_parallel)
lead = DevOpsTeamLeadAgent(DummyLLMClient())
for name in (
    "iac_agent",
    "cicd_agent",
    "deployment_agent",
    "devsecops_review_agent",
    "doc_runbook_agent",
    "infra_debug_agent",
    "infra_patch_agent",
):
    assert getattr(lead, name) is not None, name
    print(name, type(getattr(lead, name)).__name__)
print("phase2 imports OK:", InfrastructureAsCodeAgent, CICDPipelineAgent, DeploymentStrategyAgent)
print("input models OK:", IaCAgentInput, CICDPipelineAgentInput, DeploymentStrategyAgentInput)
print("SMOKE_OK")
PY
```

Expected: prints all seven class names and `SMOKE_OK`.

Also confirm `owner=` strings in `orchestrator.py` still reference `InfrastructureAsCodeAgent`, `CICDPipelineAgent`, `DeploymentStrategyAgent` (read-only `rg`).

- [ ] **Step 3: Ruff on migration-touched paths**

```bash
cd backend && python -m ruff check \
  agents/software_engineering_team/devops_team/_agent_template.py \
  agents/software_engineering_team/devops_team/iac_agent/agent.py \
  agents/software_engineering_team/devops_team/cicd_pipeline_agent/agent.py \
  agents/software_engineering_team/devops_team/deployment_strategy_agent/agent.py \
  agents/software_engineering_team/devops_team/infra_patch_agent/agent.py \
  agents/software_engineering_team/devops_team/infra_debug_agent/agent.py \
  agents/software_engineering_team/devops_team/devsecops_review_agent/agent.py \
  agents/software_engineering_team/devops_team/doc_runbook_agent/agent.py \
  agents/software_engineering_team/tests/test_devops_agent_template.py \
  && python -m ruff format --check \
  agents/software_engineering_team/devops_team/_agent_template.py \
  agents/software_engineering_team/devops_team/iac_agent/agent.py \
  agents/software_engineering_team/devops_team/cicd_pipeline_agent/agent.py \
  agents/software_engineering_team/devops_team/deployment_strategy_agent/agent.py \
  agents/software_engineering_team/devops_team/infra_patch_agent/agent.py \
  agents/software_engineering_team/devops_team/infra_debug_agent/agent.py \
  agents/software_engineering_team/devops_team/devsecops_review_agent/agent.py \
  agents/software_engineering_team/devops_team/doc_runbook_agent/agent.py \
  agents/software_engineering_team/tests/test_devops_agent_template.py
```

Expected: All checks passed; already formatted.

- [ ] **Step 4: Coverage ≥90% on migration-touched modules**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py \
  agents/software_engineering_team/tests/test_devops_debug_patch.py \
  agents/software_engineering_team/tests/test_devops_agent_template.py \
  --cov=software_engineering_team.devops_team._agent_template \
  --cov=software_engineering_team.devops_team.iac_agent.agent \
  --cov=software_engineering_team.devops_team.cicd_pipeline_agent.agent \
  --cov=software_engineering_team.devops_team.deployment_strategy_agent.agent \
  --cov=software_engineering_team.devops_team.infra_patch_agent.agent \
  --cov=software_engineering_team.devops_team.infra_debug_agent.agent \
  --cov=software_engineering_team.devops_team.devsecops_review_agent.agent \
  --cov=software_engineering_team.devops_team.doc_runbook_agent.agent \
  --cov-report=term-missing \
  --cov-fail-under=90 \
  -q
```

Expected: PASS with total coverage ≥90%.

- [ ] **Step 5: Save evidence** — write a short summary of steps 1–4 results to `.superpowers/sdd/closeout-evidence.md` (do not commit this scratch file).

- [ ] **Step 6: Commit only if this plan file was not yet committed**

```bash
git add docs/superpowers/plans/2026-07-21-devops-single-shot-closeout.md
git commit -m "$(cat <<'EOF'
Add implementation plan for devops single-shot closeout verification.

Document the regression commands and PR-body closeout evidence requirements.
EOF
)"
```

Skip if already committed.

---

### Task 2: Open closeout PR

**Files:**
- None (or already-committed design/plan only)

**Interfaces:**
- Consumes: evidence from Task 1
- Produces: GitHub PR URL closing the closeout and parent issues

- [ ] **Step 1: Push branch**

```bash
git push -u origin HEAD
```

- [ ] **Step 2: Create PR** against `refactor/migrate-boilerplate-devops-agents` with body including:

1. Closeout note: all 7 agents are thin `DevOpsSingleShotAgent` subclasses; `complete_json_with_continuation` is documented as canonical in `_agent_template.py`.
2. Evidence table: test counts, smoke OK, ruff OK, coverage %.
3. Auto-close keywords for the closeout sub-issue and the parent standardization issue.

- [ ] **Step 3: After PR is open, comment on the parent issue** linking the PR and stating verification passed (optional if `Closes` on PR is enough; still add a short comment for human-readable closeout).

No further commits unless the PR needs a fix from failed verification (then stop and escalate).
