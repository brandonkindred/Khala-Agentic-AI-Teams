# Migrate DocumentationRunbookAgent onto DevOpsSingleShotAgent

**Date:** 2026-07-21  
**Status:** Approved for implementation planning

## Goal

Migrate `DocumentationRunbookAgent` onto `DevOpsSingleShotAgent` as a thin subclass that exercises the base's omit-kwargs path (`temperature`/`think` = `None`) and `build_output` post-call hook for non-LLM `DevOpsCompletionPackage` construction, preserving public class name, constructor/run contracts, prompts, and output behavior byte-for-byte.

## Motivation

This agent is the only devops single-shot consumer that:

- Omits `temperature`/`think` kwargs entirely (relies on `complete_json_with_continuation` defaults)
- Builds a second, non-LLM object (`DevOpsCompletionPackage`) purely from `input_data` fields before assembling the output

Migrating it last validates the omit-kwargs path and secondary-object construction on production logic after the boilerplate, infra patch/debug, and DevSecOps migrations.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Approach | Inline subclass (same shape as sibling migrations) |
| Base branch | `refactor/migrate-boilerplate-devops-agents` (includes template + all prior migrations through DevSecOps) |
| Post-call surface | `build_output` — there is no separate `post_call` method on the base |
| Temperature / think | `temperature = None`, `think = None` so kwargs are omitted (not set to values that happen to match defaults) |
| Docstrings | Sibling style: class `Invariants:` + Preconditions/Postconditions on `build_context` / `build_output` |
| Test / monkeypatch changes | None — existing `_StubClient` and `_patch_fenced_response` paths still work |
| Scope | `doc_runbook_agent/agent.py` only (plus this design/plan when written) |

## Architecture

### Files touched

| Path | Change |
|---|---|
| `devops_team/doc_runbook_agent/agent.py` | `DocumentationRunbookAgent(DevOpsSingleShotAgent)` with `PROMPT`, `temperature = None`, `think = None`, `build_context`, `build_output` |

### Files not touched

- `prompts.py`, `models.py`, package `__init__.py`
- `orchestrator.py`, phase graphs, `_agent_template.py`
- `test_devops_team.py`
- Already-migrated agents

### Per-agent contract

The class:

1. Subclasses `DevOpsSingleShotAgent`
2. Sets `PROMPT = DOC_RUNBOOK_PROMPT`
3. Sets `temperature = None` and `think = None`
4. Implements `build_context` with the **same** f-string fields as today
5. Implements `build_output` with the **same** `DevOpsCompletionPackage` construction and `files`/`summary` mapping as today
6. Drops local imports of `resolve_strands_model`, `get_strands_model`, and `complete_json_with_continuation` (owned by the base)
7. Does not override `pre_call`

Public surface stays:

- Class name `DocumentationRunbookAgent` unchanged
- `__init__(llm_client)` from the base
- `run(input_data) -> DocumentationRunbookOutput` from the base

### Hook mapping (must remain byte-identical)

**Context:**

```text
task_id={task_id}
task_title={task_title}
artifacts={list(artifacts.keys())}
quality_gates={quality_gates}
notes={notes}
```

**Output:**

- Build `DevOpsCompletionPackage` from `input_data` with the same hardcoded fields as today (`status="completed"`, `ReleaseReadiness(deployment_strategy="rolling", ...)`, empty `GitOperationsMetadata()`, `HandoffInfo(prod_approval_required=True, runbook_updated=True)`, sorted artifact keys, copied quality gates, notes)
- Return `DocumentationRunbookOutput(files=data.get("files") or {}, completion_package=completion, summary=data.get("summary", ""))`

## Monkeypatchability

The base calls `complete_json_with_continuation` bound on `_agent_template`. Current tests for this agent do **not** monkeypatch the per-agent module import; they use `_StubClient` or patch `shared.llm.Agent` via `_patch_fenced_response`. Therefore this migration makes **no** test-file edits. If a future test patches the helper by name, it must target `software_engineering_team.devops_team._agent_template.complete_json_with_continuation`.

## Testing

- Rely on existing `TestDocumentationRunbookAgent::test_produces_completion_package` and `test_doc_runbook_agent_recovers_fenced_response` in `test_devops_team.py`.
- Run focused pytest for those cases plus ruff on the touched file.
- Confirm ≥90% line coverage on the rewritten `agent.py`.

## Out of scope

- Changing prompts, models, orchestrator wiring, or `DevOpsSingleShotAgent` itself
- Renaming the public class
- Setting `temperature`/`think` to explicit default-equivalent values

## Acceptance criteria mapping

| Criterion | How satisfied |
|---|---|
| Post-call preserves `DevOpsCompletionPackage` construction | Logic lives verbatim in `build_output` |
| `temperature`/`think` omitted | Class attrs set to `None` so base skips those kwargs |
| Public `__init__` / `run` / class name unchanged | Inherit base; keep `DocumentationRunbookAgent` |
| Monkeypatch targets updated if needed | N/A for current tests; documented for future |
| Output byte-identical including completion package | Same context string and package field values |
| `make test` / `make lint`; 90% on touched files | Implementation plan verifies |
