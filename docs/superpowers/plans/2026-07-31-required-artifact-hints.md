# Required Artifact Hints Shared Constant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the AI Agent Development team one shared `REQUIRED_ARTIFACT_HINTS` constant used by review, problem-solving, and intake/planning system prompts.

**Architecture:** Add `ai_agent_development_team/constants.py` as the single source of truth for the five artifact-category hint strings. Review and problem-solving import it; intake/planning call prompt builders that inject the joined list into their system prompts. No new hard gates; hint values unchanged.

**Tech Stack:** Python 3.10+, pytest, existing `ai_agent_development_team` phase helpers.

**Spec:** `docs/superpowers/specs/2026-07-31-required-artifact-hints-design.md`

**Worktree:** `.worktrees/required-artifact-hints` on branch `fix/required-artifact-hints`

## Global Constraints

- Keep hint values exactly `("blueprint", "evaluation", "safety", "runbook", "mcp")`.
- Do not add per-run overrides, `TeamContext`, or env-var configuration.
- Do not add deterministic post-planning / post-intake coverage gates.
- Leave `DELIVER_PROMPT` unchanged.
- Preserve FakeLLM keyword phrases (`spec intake specialist`, `AI systems planner`) in the rebuilt system prompts so existing workflow tests keep routing.
- Design by Contract: document `Preconditions` / `Postconditions` on new public builders; update problem-solving docstring for the known-hint guard.
- Do not mention GitHub issue numbers in code, comments, commit messages, or tracked docs.
- Run tests from the checkout's `backend/` directory (worktree or main), using the project venv via `PATH` or an explicit relative path:
  `cd backend && PYTHONPATH=. python -m pytest ...`
  (Activate `.venv` first, or invoke `.venv/bin/python` relative to the main `backend/` checkout.)

## File map

| File | Role |
|------|------|
| `ai_agent_development_team/constants.py` | Single source of truth for `REQUIRED_ARTIFACT_HINTS` |
| `ai_agent_development_team/phases/review.py` | Import constant; drop local tuple |
| `ai_agent_development_team/phases/problem_solving.py` | Import constant; only synthesize placeholders for known hints |
| `ai_agent_development_team/prompts.py` | `intake_system_prompt()` / `planning_system_prompt()` builders |
| `ai_agent_development_team/phases/intake.py` | Call `intake_system_prompt()` |
| `ai_agent_development_team/phases/planning.py` | Call `planning_system_prompt()` |
| `tests/test_ai_agent_development_team.py` | Constant/prompt coverage + unknown-token problem-solving guard |

---

### Task 1: Add `constants.py` and assert the tuple

**Files:**
- Create: `backend/agents/software_engineering_team/ai_agent_development_team/constants.py`
- Modify: `backend/agents/software_engineering_team/tests/test_ai_agent_development_team.py`
- Test: `backend/agents/software_engineering_team/tests/test_ai_agent_development_team.py`

**Interfaces:**
- Consumes: none
- Produces: `REQUIRED_ARTIFACT_HINTS: tuple[str, ...] = ("blueprint", "evaluation", "safety", "runbook", "mcp")`

- [ ] **Step 1: Write the failing test**

Add near the top of `test_ai_agent_development_team.py` (after existing imports):

```python
from software_engineering_team.ai_agent_development_team.constants import (
    REQUIRED_ARTIFACT_HINTS,
)
```

Add this test function (near other unit-style tests, e.g. before `test_run_intake_recovers_fenced_json_response`):

```python
def test_required_artifact_hints_tuple() -> None:
    """Team-level constant is the sole definition of artifact-category hints."""
    assert REQUIRED_ARTIFACT_HINTS == (
        "blueprint",
        "evaluation",
        "safety",
        "runbook",
        "mcp",
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend && PYTHONPATH=. python -m pytest \
  agents/software_engineering_team/tests/test_ai_agent_development_team.py::test_required_artifact_hints_tuple -v
```

Expected: FAIL with `ModuleNotFoundError` / `ImportError` for `constants`.

- [ ] **Step 3: Create `constants.py`**

Create `backend/agents/software_engineering_team/ai_agent_development_team/constants.py`:

```python
"""Team-level configuration constants for AI Agent Development."""

from __future__ import annotations

REQUIRED_ARTIFACT_HINTS: tuple[str, ...] = (
    "blueprint",
    "evaluation",
    "safety",
    "runbook",
    "mcp",
)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd backend && PYTHONPATH=. python -m pytest \
  agents/software_engineering_team/tests/test_ai_agent_development_team.py::test_required_artifact_hints_tuple -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/ai_agent_development_team/constants.py \
  backend/agents/software_engineering_team/tests/test_ai_agent_development_team.py
git commit -m "$(cat <<'EOF'
Add shared REQUIRED_ARTIFACT_HINTS team constant.

EOF
)"
```

---

### Task 2: Point review at the shared constant

**Files:**
- Modify: `backend/agents/software_engineering_team/ai_agent_development_team/phases/review.py`
- Test: existing workflow tests in `test_ai_agent_development_team.py` (no new review-only test required if behavior is identical)

**Interfaces:**
- Consumes: `REQUIRED_ARTIFACT_HINTS` from `..constants`
- Produces: unchanged `run_review(*, execution_result: ExecutionResult) -> ReviewResult`

- [ ] **Step 1: Write a failing assertion that review no longer defines the tuple locally**

Add to `test_ai_agent_development_team.py`:

```python
import software_engineering_team.ai_agent_development_team.phases.review as review_mod
from software_engineering_team.ai_agent_development_team import constants as team_constants


def test_review_uses_shared_required_artifact_hints() -> None:
    """Review must import the team constant, not redefine the five-string tuple."""
    assert review_mod.REQUIRED_ARTIFACT_HINTS is team_constants.REQUIRED_ARTIFACT_HINTS
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend && PYTHONPATH=. python -m pytest \
  agents/software_engineering_team/tests/test_ai_agent_development_team.py::test_review_uses_shared_required_artifact_hints -v
```

Expected: FAIL — `assert ... is ...` is False (review still has its own tuple object).

- [ ] **Step 3: Update `review.py`**

Replace the top of `phases/review.py` so it imports instead of defining:

```python
"""Review phase: quality gate for generated AI-agent artifacts."""

from __future__ import annotations

from ..constants import REQUIRED_ARTIFACT_HINTS
from ..models import ExecutionResult, MicrotaskStatus, ReviewIssue, ReviewResult


def run_review(*, execution_result: ExecutionResult) -> ReviewResult:
```

Leave the body of `run_review` unchanged (it already iterates `REQUIRED_ARTIFACT_HINTS`).

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd backend && PYTHONPATH=. python -m pytest \
  agents/software_engineering_team/tests/test_ai_agent_development_team.py::test_review_uses_shared_required_artifact_hints \
  agents/software_engineering_team/tests/test_ai_agent_development_team.py::test_ai_agent_development_workflow_success \
  agents/software_engineering_team/tests/test_ai_agent_development_team.py::test_ai_agent_development_workflow_problem_solving -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/ai_agent_development_team/phases/review.py \
  backend/agents/software_engineering_team/tests/test_ai_agent_development_team.py
git commit -m "$(cat <<'EOF'
Wire review phase to shared REQUIRED_ARTIFACT_HINTS.

EOF
)"
```

---

### Task 3: Prompt builders for intake and planning

**Files:**
- Modify: `backend/agents/software_engineering_team/ai_agent_development_team/prompts.py`
- Modify: `backend/agents/software_engineering_team/ai_agent_development_team/phases/intake.py`
- Modify: `backend/agents/software_engineering_team/ai_agent_development_team/phases/planning.py`
- Modify: `backend/agents/software_engineering_team/tests/test_ai_agent_development_team.py`

**Interfaces:**
- Consumes: `REQUIRED_ARTIFACT_HINTS` from `.constants`
- Produces:
  - `intake_system_prompt() -> str`
  - `planning_system_prompt() -> str`
  - `DELIVER_PROMPT` remains a module-level `str` (unchanged)

- [ ] **Step 1: Write the failing test**

Add imports:

```python
from software_engineering_team.ai_agent_development_team.prompts import (
    intake_system_prompt,
    planning_system_prompt,
)
```

Add:

```python
def test_intake_and_planning_prompts_include_required_artifact_hints() -> None:
    """Intake/planning system prompts list every shared artifact-category hint."""
    intake = intake_system_prompt()
    planning = planning_system_prompt()
    for hint in REQUIRED_ARTIFACT_HINTS:
        assert hint in intake
        assert hint in planning
    assert "spec intake specialist" in intake
    assert "AI systems planner" in planning
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend && PYTHONPATH=. python -m pytest \
  agents/software_engineering_team/tests/test_ai_agent_development_team.py::test_intake_and_planning_prompts_include_required_artifact_hints -v
```

Expected: FAIL with `ImportError` for `intake_system_prompt` / `planning_system_prompt`.

- [ ] **Step 3: Implement builders and switch callers**

Replace `prompts.py` content with:

```python
"""Prompt templates for AI Agent Development Team phases."""

from __future__ import annotations

from .constants import REQUIRED_ARTIFACT_HINTS

DELIVER_PROMPT = """You are an expert delivery coordinator.
Given generated artifacts and review findings, produce final delivery notes.
Respond JSON:
{
  "summary": "...",
  "handoff_notes": ["..."],
  "runbook": ["..."]
}
"""


def _required_artifact_hints_line() -> str:
    """Format the shared hint list for injection into system prompts.

    Preconditions: ``REQUIRED_ARTIFACT_HINTS`` is a non-empty sequence of strings.
    Postconditions: returns one line that includes every hint joined by ``", "``.
    """
    joined = ", ".join(REQUIRED_ARTIFACT_HINTS)
    return (
        "Required artifact path categories (each must appear in at least one "
        f"generated artifact filename later): {joined}."
    )


def intake_system_prompt() -> str:
    """Build the intake specialist system prompt with shared artifact hints.

    Preconditions: none beyond importable ``REQUIRED_ARTIFACT_HINTS``.
    Postconditions: returns a system prompt that retains the intake JSON schema
      and includes every entry of ``REQUIRED_ARTIFACT_HINTS``.
    """
    return f"""You are an expert spec intake specialist for building AI agent systems.
Extract a normalized mission brief from the task and spec.
{_required_artifact_hints_line()}
Respond with JSON:
{{
  "system_goal": "...",
  "constraints": ["..."],
  "risks": ["..."],
  "success_metrics": ["..."],
  "summary": "..."
}}
"""


def planning_system_prompt() -> str:
    """Build the planning specialist system prompt with shared artifact hints.

    Preconditions: none beyond importable ``REQUIRED_ARTIFACT_HINTS``.
    Postconditions: returns a system prompt that retains the planning JSON schema,
      tool-agent list, and includes every entry of ``REQUIRED_ARTIFACT_HINTS``.
    """
    return f"""You are an AI systems planner.
Create microtasks to deliver a production-ready agent system blueprint.
Use available tool agents: prompt_engineering, memory_rag, safety_governance, evaluation_harness, agent_runtime, mcp_server_connectivity, general.
{_required_artifact_hints_line()}
Respond with JSON:
{{
  "microtasks": [{{"id":"mt-1","title":"...","description":"...","tool_agent":"prompt_engineering","depends_on":[]}}],
  "summary": "..."
}}
"""
```

In `phases/intake.py`, change the import and call site:

```python
from ..prompts import intake_system_prompt
# ...
    raw = complete_json_with_continuation(llm, prompt, system_prompt=intake_system_prompt())
```

In `phases/planning.py`, change the import and call site:

```python
from ..prompts import planning_system_prompt
# ...
    raw = complete_json_with_continuation(llm, prompt, system_prompt=planning_system_prompt())
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd backend && PYTHONPATH=. python -m pytest \
  agents/software_engineering_team/tests/test_ai_agent_development_team.py::test_intake_and_planning_prompts_include_required_artifact_hints \
  agents/software_engineering_team/tests/test_ai_agent_development_team.py::test_run_intake_recovers_fenced_json_response \
  agents/software_engineering_team/tests/test_ai_agent_development_team.py::test_run_planning_recovers_fenced_json_response \
  agents/software_engineering_team/tests/test_ai_agent_development_team.py::test_ai_agent_development_workflow_success -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/ai_agent_development_team/prompts.py \
  backend/agents/software_engineering_team/ai_agent_development_team/phases/intake.py \
  backend/agents/software_engineering_team/ai_agent_development_team/phases/planning.py \
  backend/agents/software_engineering_team/tests/test_ai_agent_development_team.py
git commit -m "$(cat <<'EOF'
Inject shared artifact hints into intake and planning prompts.

EOF
)"
```

---

### Task 4: Problem-solving known-hint guard

**Files:**
- Modify: `backend/agents/software_engineering_team/ai_agent_development_team/phases/problem_solving.py`
- Modify: `backend/agents/software_engineering_team/tests/test_ai_agent_development_team.py`

**Interfaces:**
- Consumes: `REQUIRED_ARTIFACT_HINTS` from `..constants`; existing `run_problem_solving(*, execution_result, review_result) -> ProblemSolvingResult`
- Produces: same signature; placeholders only when extracted token ∈ `REQUIRED_ARTIFACT_HINTS`

- [ ] **Step 1: Write the failing test**

Add import if missing:

```python
from software_engineering_team.ai_agent_development_team.phases.problem_solving import (
    run_problem_solving,
)
```

Add:

```python
def test_problem_solving_ignores_unknown_artifact_gate_token() -> None:
    """artifact_gate issues whose category is not a shared hint create no placeholder."""
    execution = ExecutionResult(files={"ai_system/system_blueprint.md": "# blueprint"})
    review = ReviewResult(
        passed=False,
        issues=[
            ReviewIssue(
                source="artifact_gate",
                severity="high",
                description="Missing expected artifact category: not_a_real_hint",
                recommendation="Add at least one artifact path containing 'not_a_real_hint'.",
            )
        ],
        required_artifacts_ok=False,
        summary="Review failed.",
    )
    result = run_problem_solving(execution_result=execution, review_result=review)
    assert result.resolved is False
    assert result.fixes_applied == []
    assert result.files == execution.files
    assert "not_a_real_hint_placeholder.md" not in result.files
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend && PYTHONPATH=. python -m pytest \
  agents/software_engineering_team/tests/test_ai_agent_development_team.py::test_problem_solving_ignores_unknown_artifact_gate_token -v
```

Expected: FAIL — current code synthesizes a placeholder (`resolved is True`).

- [ ] **Step 3: Implement the guard**

Update `phases/problem_solving.py` to:

```python
"""Problem-solving phase: attempt targeted remediation after review failures."""

from __future__ import annotations

from ..constants import REQUIRED_ARTIFACT_HINTS
from ..models import ExecutionResult, ProblemSolvingResult, ReviewResult


def run_problem_solving(
    *, execution_result: ExecutionResult, review_result: ReviewResult
) -> ProblemSolvingResult:
    """Synthesize placeholder artifacts for missing known artifact-category issues.

    Preconditions: ``review_result.issues`` may be empty. This is a purely
      deterministic, non-LLM fix — it only ever addresses ``artifact_gate``-
      sourced issues whose category token is in ``REQUIRED_ARTIFACT_HINTS``;
      it cannot resolve ``execution``-sourced issues (failed microtasks).
    Postconditions: returns a ``ProblemSolvingResult`` where ``resolved`` is
      True iff at least one placeholder file was synthesized; ``files`` is a
      new dict — ``execution_result.files`` merged with the placeholder
      patches — and ``execution_result`` itself is not mutated. ``fixes_applied``
      lists each synthesized placeholder and is empty when ``resolved`` is False.
      Unknown-token ``artifact_gate`` issues produce no placeholder.
    """
    fixes_applied = []
    patched_files = {}

    for issue in review_result.issues:
        if issue.source != "artifact_gate":
            continue
        token = issue.description.split(":")[-1].strip()
        if token not in REQUIRED_ARTIFACT_HINTS:
            continue
        path = f"ai_system/{token}_placeholder.md"
        patched_files[path] = (
            f"# Placeholder {token}\n\nAuto-generated during problem-solving to satisfy artifact gate."
        )
        fixes_applied.append(f"Added placeholder artifact for missing category '{token}'.")

    resolved = len(patched_files) > 0
    summary = (
        "Applied targeted artifact-gap fixes."
        if resolved
        else "No deterministic fixes were available."
    )
    merged_files = dict(execution_result.files)
    merged_files.update(patched_files)

    return ProblemSolvingResult(
        resolved=resolved, fixes_applied=fixes_applied, files=merged_files, summary=summary
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd backend && PYTHONPATH=. python -m pytest \
  agents/software_engineering_team/tests/test_ai_agent_development_team.py::test_problem_solving_ignores_unknown_artifact_gate_token \
  agents/software_engineering_team/tests/test_ai_agent_development_team.py::test_ai_agent_development_workflow_problem_solving -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/ai_agent_development_team/phases/problem_solving.py \
  backend/agents/software_engineering_team/tests/test_ai_agent_development_team.py
git commit -m "$(cat <<'EOF'
Guard problem-solving placeholders to shared artifact hints.

EOF
)"
```

---

### Task 5: Full suite regression

**Files:**
- Test only: `backend/agents/software_engineering_team/tests/test_ai_agent_development_team.py`

**Interfaces:**
- Consumes: all Task 1–4 deliverables
- Produces: green suite confirming no local five-string duplicate remains in review

- [ ] **Step 1: Confirm no local duplicate literals in consumer modules**

```bash
rg -n 'REQUIRED_ARTIFACT_HINTS\s*=\s*\(' \
  backend/agents/software_engineering_team/ai_agent_development_team && \
rg -n '"blueprint", "evaluation", "safety", "runbook", "mcp"' \
  backend/agents/software_engineering_team/ai_agent_development_team
```

Expected: the assignment appears only in `constants.py`; the five-string literal appears only there (not in `review.py` / `prompts.py` / `problem_solving.py`).

- [ ] **Step 2: Run the full AI agent development team test file**

```bash
cd backend && PYTHONPATH=. python -m pytest \
  agents/software_engineering_team/tests/test_ai_agent_development_team.py -v
```

Expected: all tests PASS (14 existing + 4 new = 18).

- [ ] **Step 3: Commit only if Step 1–2 required cleanup; otherwise skip**

If the suite is already green and no further edits were needed, do not create an empty commit.

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| `constants.py` with five unchanged hints | Task 1 |
| Review imports shared constant | Task 2 |
| Intake/planning builders inject hints | Task 3 |
| Problem-solving known-hint guard | Task 4 |
| Unit tests for constant + prompts + unknown token | Tasks 1, 3, 4 |
| Existing workflow tests remain green | Tasks 2–5 |
| No deliver prompt / no new hard gates / no overrides | Global Constraints |
