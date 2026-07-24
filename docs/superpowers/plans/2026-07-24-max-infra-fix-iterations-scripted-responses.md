# Constant-Driven Debug/Patch Scripted Responses Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `test_loop_terminates_after_max_iterations` build its repeated debug/patch `_ScriptedClient` responses from `MAX_INFRA_FIX_ITERATIONS`, and remove hardcoded `3` from that test's docstring and comment.

**Architecture:** Single-file test maintainability fix. Define a two-element `debug_patch_pair` (debug response, then patch response) and splat `MAX_INFRA_FIX_ITERATIONS` copies into the existing FIFO response list. No production code changes.

**Tech Stack:** Python 3.10+, pytest, existing `_ScriptedClient` / `DummyLLMClient` test doubles in the software engineering team suite.

**Spec:** `docs/superpowers/specs/2026-07-24-max-infra-fix-iterations-scripted-responses-design.md`

## Global Constraints

- Touch only `backend/agents/software_engineering_team/tests/test_devops_debug_patch.py` for the functional change.
- Do not change `MAX_INFRA_FIX_ITERATIONS` in production code.
- Do not extract a shared response-builder helper.
- Preserve existing canned payload values for debug and patch responses.
- Work in the existing worktree at `.worktrees/fix-2384-max-infra-fix-iterations` on branch `fix/2384-max-infra-fix-iterations`.

## File Structure

| File | Role |
|---|---|
| `backend/agents/software_engineering_team/tests/test_devops_debug_patch.py` | Modify `TestDevOpsPipelineDebugPatchLoop.test_loop_terminates_after_max_iterations` only (docstring, comment, response list construction) |

No new files.

---

### Task 1: Derive scripted debug/patch pairs from MAX_INFRA_FIX_ITERATIONS

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_devops_debug_patch.py` (method `test_loop_terminates_after_max_iterations`, roughly lines 295–415)
- Test: same file / same method (this is a fixture-construction refactor of an existing test)

**Interfaces:**
- Consumes: `MAX_INFRA_FIX_ITERATIONS` and `DevOpsTeamLeadAgent` from `software_engineering_team.devops_team.orchestrator` (already imported inside the test)
- Produces: `_ScriptedClient` response list whose debug/patch section length is `2 * MAX_INFRA_FIX_ITERATIONS`

- [ ] **Step 1: Confirm the test passes on the current baseline**

Run from the worktree:

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/fix-2384-max-infra-fix-iterations/backend
python -m pytest agents/software_engineering_team/tests/test_devops_debug_patch.py::TestDevOpsPipelineDebugPatchLoop::test_loop_terminates_after_max_iterations -q
```

Expected: PASS (1 passed)

- [ ] **Step 2: Replace the docstring, comment, and hardcoded response pairs**

In `test_loop_terminates_after_max_iterations`, replace the method body from the docstring through the `_ScriptedClient([...])` construction with the following. Leave everything after `agent = DevOpsTeamLeadAgent(llm_client=client)` unchanged.

```python
    def test_loop_terminates_after_max_iterations(self) -> None:
        """Always-failing execution runs exactly MAX_INFRA_FIX_ITERATIONS debug attempts.

        Also spot-checks Phase 4.6 status details contain
        ``iteration {i}/{MAX_INFRA_FIX_ITERATIONS}`` for each attempt
        (i from 1 through ``MAX_INFRA_FIX_ITERATIONS``).
        """
        from software_engineering_team.devops_team.orchestrator import (
            MAX_INFRA_FIX_ITERATIONS,
            DevOpsTeamLeadAgent,
        )

        debug_patch_pair = [
            {
                "errors": [{"error_type": "syntax", "error_message": "bad"}],
                "summary": "err",
                "fixable": True,
            },
            {
                "patched_artifacts": {"main.tf": "resource { }"},
                "summary": "fix",
                "edits_applied": 1,
            },
        ]
        client = _ScriptedClient(
            [
                # Task clarifier
                {"approved_for_execution": True, "clarification_requests": []},
                # IaC agent
                {"artifacts": {"main.tf": "resource {}"}, "summary": "infra"},
                # CICD
                {"artifacts": {}, "summary": "cicd", "pipeline_yaml": ""},
                # Deployment
                {"artifacts": {}, "summary": "deploy", "strategy": "rolling", "rollback_plan": ""},
                # Debug + patch agents (one pair per MAX_INFRA_FIX_ITERATIONS)
                *[
                    response
                    for _ in range(MAX_INFRA_FIX_ITERATIONS)
                    for response in debug_patch_pair
                ],
                # DevSecOps review
                {"approved": True, "summary": "ok", "findings": []},
                # Change review
                {"approved": True, "summary": "ok"},
                # Test validation
                {"quality_gates": {}, "summary": "ok"},
                # Doc runbook
                {"files": {}, "summary": "doc ok"},
            ]
        )

        agent = DevOpsTeamLeadAgent(llm_client=client)
```

Checklist for this edit:
- Docstring no longer says ``iteration N/3``.
- Comment no longer says "up to 3 times".
- Three duplicated debug/patch dict pairs are gone.
- Clarifier / IaC / CI/CD / deploy / DevSecOps / change review / test validation / doc responses are unchanged.
- Assertions below the edit are untouched.

- [ ] **Step 3: Re-run the test and confirm it still passes**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/fix-2384-max-infra-fix-iterations/backend
python -m pytest agents/software_engineering_team/tests/test_devops_debug_patch.py::TestDevOpsPipelineDebugPatchLoop::test_loop_terminates_after_max_iterations -q
```

Expected: PASS (1 passed)

- [ ] **Step 4: Optional regression — run the full pipeline loop class**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/fix-2384-max-infra-fix-iterations/backend
python -m pytest agents/software_engineering_team/tests/test_devops_debug_patch.py::TestDevOpsPipelineDebugPatchLoop -q
```

Expected: all tests in the class PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/fix-2384-max-infra-fix-iterations
git add backend/agents/software_engineering_team/tests/test_devops_debug_patch.py
git commit -m "$(cat <<'EOF'
Derive debug/patch scripted responses from MAX_INFRA_FIX_ITERATIONS.

EOF
)"
```

---

## Spec coverage (self-review)

| Spec requirement | Task / step |
|---|---|
| Build repeated debug/patch responses from `MAX_INFRA_FIX_ITERATIONS` | Task 1 Step 2 |
| Keep clarifier → IaC → CI/CD → deploy prefix | Task 1 Step 2 |
| Keep DevSecOps → change review → test validation → doc suffix | Task 1 Step 2 |
| Update docstring off hardcoded `N/3` | Task 1 Step 2 |
| Update comment off "up to 3 times" | Task 1 Step 2 |
| No production code changes | Global Constraints + File Structure |
| No helper extraction | Global Constraints |
| Verify with pytest on this test | Task 1 Steps 1, 3, 4 |
