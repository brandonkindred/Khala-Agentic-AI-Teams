---
name: Logging and Code Review Fix
overview: Improve orchestrator logging to provide full visibility into task lifecycle and agent activity, and fix the code review agent so it always returns actionable issues when rejecting code.
todos:
  - id: orchestrator-lifecycle-logging
    content: "Add task lifecycle logging to orchestrator: task start, phase transitions, queue state, completion/failure, and fix blocked task re-queue bug"
    status: completed
  - id: code-review-safety-net
    content: Add safety net in code_review_agent/agent.py for approved=False with 0 issues (synthesize from summary or auto-approve)
    status: completed
  - id: code-review-prompt
    content: Enhance code_review_agent/prompts.py with enforcement rule for issue quality and mandatory issues on rejection
    status: completed
  - id: code-review-logging-orchestrator
    content: Add detailed code review issue logging in orchestrator when review rejects code
    status: completed
  - id: dummy-llm-code-review
    content: Add code review pattern to DummyLLMClient in shared/llm.py
    status: completed
  - id: logging-config-agents
    content: Register missing logger names (orchestrator, code_review_agent, git_utils, repo_writer) in logging_config.py
    status: completed
isProject: false
---

# Improve Logging and Fix Code Review Agent

## Problem Analysis

From the terminal output, three interrelated issues cause the "stuck" appearance:

1. **Code review rejects with no issues**: The `DummyLLMClient` has no code review pattern, so it returns `{"output": "Dummy response"}`. The code review agent interprets this as `approved=False, issues=[]` -- an unresolvable loop.
2. **Blocked tasks dropped from queue**: Blocked tasks are `pop()`ed but never re-queued (`[orchestrator.py` line 339-346](software_engineering_team/orchestrator.py)).
3. **Silent failures**: Feature branch creation failures cause tasks to be skipped with no log, and there's no overall progress reporting.

## Changes

### 1. Orchestrator Logging Overhaul (`[orchestrator.py](software_engineering_team/orchestrator.py)`)

Add structured lifecycle logging at every critical point:

- **Task start**: `"[5/11] Starting task {task_id} (type={type}, assignee={assignee})"`
- **Queue state**: `"Queue: 4 completed, 7 remaining (3 blocked)"`  
- **Branch creation failure**: `"Task {task_id}: branch creation failed: {msg} - skipping"`
- **Phase transitions**: `"[{task_id}] Phase 2/5: Build verification"`, `"[{task_id}] Phase 3/5: Code review (round 1/3)"`
- **Code review detail**: Log each issue individually: `"  [critical] naming: File name looks like task description (file: foo.ts) -- Suggestion: rename to..."`
- **Task completion**: `"Task {task_id} completed in {duration}s"`
- **Task failure/skip**: `"Task {task_id} SKIPPED: branch creation failed"` or `"Task {task_id} FAILED: {reason}"`

Also fix the **blocked task re-queue bug**: instead of silently `continue`-ing, re-append blocked tasks to the end of the queue (with a max-pass guard to avoid infinite loops):

```python
# Before the main loop
max_passes = len(execution_queue) * 2  # safety limit
pass_count = 0

while execution_queue and pass_count < max_passes:
    pass_count += 1
    task_id = execution_queue.pop(0)
    ...
    if missing_deps:
        logger.warning("Task %s blocked: missing deps %s - re-queuing", task_id, missing_deps)
        execution_queue.append(task_id)  # re-queue instead of dropping
        continue
```

### 2. Code Review Agent: Enforce Actionable Issues (`[code_review_agent/agent.py](software_engineering_team/code_review_agent/agent.py)`)

Add a safety net after LLM response parsing (after line 105):

```python
# Safety net: if rejected but no issues, synthesize from summary or auto-approve
if not approved and not critical_or_major:
    summary_text = data.get("summary", "")
    if issues:
        # Has minor/nit issues only -- auto-approve since no critical/major
        logger.info("CodeReview: overriding to approved=True (only minor/nit issues)")
        approved = True
    elif summary_text:
        # Rejected with no issues at all -- synthesize an issue from summary
        logger.warning("CodeReview: rejected with 0 issues -- synthesizing from summary")
        issues.append(CodeReviewIssue(
            severity="major", category="general",
            file_path="", description=summary_text,
            suggestion="Address the concerns described in the review summary.",
        ))
    else:
        # No issues AND no summary -- auto-approve (LLM gave no useful feedback)
        logger.warning("CodeReview: rejected with no issues and no summary -- auto-approving")
        approved = True
```

### 3. Code Review Prompt Enhancement (`[code_review_agent/prompts.py](software_engineering_team/code_review_agent/prompts.py)`)

Add an explicit enforcement rule to the prompt:

```
**CRITICAL RULE: If approved=false, the issues list MUST contain at least one critical or major issue 
with a detailed description and concrete suggestion. An empty issues list with approved=false is INVALID 
and will be treated as an approval.**
```

Also add instructions for issue quality:

```
Each issue MUST include:
- "file_path": The exact file where the problem exists
- "description": A specific, actionable description (NOT vague like "code needs work"). 
  Include the problematic code/pattern and explain WHY it's wrong.
- "suggestion": A concrete fix showing WHAT to change (include code snippets if possible)
```

### 4. Code Review Logging in Orchestrator (`[orchestrator.py](software_engineering_team/orchestrator.py)`)

After code review runs, log the full results so they're visible in the output:

```python
if not review_result.approved:
    code_review_issues = _code_review_issues_to_dicts(review_result.issues)
    for issue in review_result.issues:
        logger.info(
            "  [%s] %s: %s (file: %s)\n    Suggestion: %s",
            issue.severity, issue.category, issue.description,
            issue.file_path or "n/a", issue.suggestion or "none",
        )
    if review_result.summary:
        logger.info("  Review summary: %s", review_result.summary[:300])
```

### 5. DummyLLMClient: Add Code Review Pattern (`[shared/llm.py](software_engineering_team/shared/llm.py)`)

Add a pattern for code review prompts that returns proper structured feedback:

```python
# Code review prompt
if "senior code reviewer" in lowered and "approved" in lowered:
    return {
        "approved": True,
        "issues": [],
        "summary": "Code review passed (dummy). Code meets basic standards.",
        "spec_compliance_notes": "Code aligns with task requirements.",
        "suggested_commit_message": "",
    }
```

### 6. Logging Config: Register Missing Loggers (`[shared/logging_config.py](software_engineering_team/shared/logging_config.py)`)

Add missing logger names to `AGENT_LOGGERS`:

```python
AGENT_LOGGERS = [
    "orchestrator",
    "code_review_agent.agent",
    "shared.git_utils",
    "shared.repo_writer",
    "shared.job_store",
    # ... existing entries ...
]
```

## Files to Modify

- `[software_engineering_team/orchestrator.py](software_engineering_team/orchestrator.py)` -- lifecycle logging, blocked task re-queue, code review detail logging
- `[software_engineering_team/code_review_agent/agent.py](software_engineering_team/code_review_agent/agent.py)` -- safety net for rejected-with-no-issues
- `[software_engineering_team/code_review_agent/prompts.py](software_engineering_team/code_review_agent/prompts.py)` -- enforce issue reporting rule
- `[software_engineering_team/shared/llm.py](software_engineering_team/shared/llm.py)` -- add code review dummy pattern
- `[software_engineering_team/shared/logging_config.py](software_engineering_team/shared/logging_config.py)` -- register missing loggers

