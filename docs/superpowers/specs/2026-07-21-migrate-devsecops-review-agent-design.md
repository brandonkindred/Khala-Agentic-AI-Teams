# Migrate DevSecOpsReviewAgent onto DevOpsSingleShotAgent

**Date:** 2026-07-21  
**Status:** Approved for implementation planning

## Goal

Migrate `DevSecOpsReviewAgent` onto `DevOpsSingleShotAgent` as a thin subclass that exercises the base's `temperature` override and `build_output` (post-call) hook, preserving public class name, constructor/run contracts, prompts, docstrings (relocated), and output behavior byte-for-byte.

## Motivation

This agent is the only devops single-shot consumer that:

- Sets `temperature=0.0` explicitly (others use `0.1` or omit)
- Calls `derive_approved()` while distinguishing an absent vs present-but-null `"approved"` key
- Already carries full Preconditions/Postconditions docstrings

Migrating it after the boilerplate and infra patch/debug agents validates the temperature override path on production logic with the absent-vs-null nuance covered by existing tests.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Approach | Inline subclass (same shape as sibling migrations) |
| Base branch | `refactor/migrate-boilerplate-devops-agents` (includes template + boilerplate + infra patch/debug) |
| Post-call surface | `build_output` — there is no separate `post_call` method on the base |
| Temperature / think | `temperature = 0.0`; inherit `think = True` |
| Docstrings | Keep class docstring; move today's `run` Preconditions/Postconditions onto `build_output`; drop redundant `__init__` docstring (base owns it) |
| Test / monkeypatch changes | None — existing `_StubClient` and `_patch_fenced_response` paths still work |
| Scope | `devsecops_review_agent/agent.py` only (plus this design/plan when written) |

## Architecture

### Files touched

| Path | Change |
|---|---|
| `devops_team/devsecops_review_agent/agent.py` | `DevSecOpsReviewAgent(DevOpsSingleShotAgent)` with `PROMPT`, `temperature = 0.0`, `build_context`, `build_output` |

### Files not touched

- `prompts.py`, `models.py`, package `__init__.py`
- `orchestrator.py`, phase graphs, `_agent_template.py`
- `shared/security_service.py` (`derive_approved` unchanged)
- `test_devops_team.py`
- `doc_runbook_agent` and already-migrated agents

### Per-agent contract

The class:

1. Subclasses `DevOpsSingleShotAgent`
2. Sets `PROMPT = DEVSECOPS_REVIEW_PROMPT`
3. Sets `temperature = 0.0` (does not override `think`)
4. Implements `build_context` with the **same** f-string fields as today
5. Implements `build_output` with the **same** findings mapping, absent-vs-null `approved` handling, and `derive_approved` call as today
6. Drops local imports of `resolve_strands_model`, `get_strands_model`, and `complete_json_with_continuation` (owned by the base)
7. Does not override `pre_call`

Public surface stays:

- Class name `DevSecOpsReviewAgent` unchanged
- `__init__(llm_client)` from the base
- `run(input_data) -> DevSecOpsReviewOutput` from the base

### Hook mapping (must remain byte-identical)

**Context:**

```text
task={task_description}
requirements={requirements}
artifacts={list(artifacts.keys())}
```

**Output:**

- `findings = [ReviewFinding(**f) for f in (data.get("findings") or []) if isinstance(f, dict)]`
- `llm_approved = bool(data["approved"]) if "approved" in data else None`  
  (present-but-null → `False` / fail closed; absent → `None` / defer to findings)
- `approved = derive_approved(findings, llm_approved=llm_approved)`
- `summary = data.get("summary", "")`

## Monkeypatchability

The base calls `complete_json_with_continuation` bound on `_agent_template`. Current tests for this agent do **not** monkeypatch the per-agent module import; they use `_StubClient` or patch `shared.llm.Agent` via `_patch_fenced_response`. Therefore this migration makes **no** test-file edits. If a future test patches the helper by name, it must target `software_engineering_team.devops_team._agent_template.complete_json_with_continuation`.

## Testing

- Rely on existing `TestDevSecOpsReviewAgent` (high severity block, clean approve, explicit null fail-closed, absent defer) and `test_devsecops_review_agent_recovers_fenced_response` in `test_devops_team.py`.
- Run focused pytest for those cases plus ruff on the touched file.
- Confirm ≥90% line coverage on the rewritten `agent.py`.

## Out of scope

- Migrating `doc_runbook_agent`
- Changing prompts, models, orchestrator wiring, or `DevOpsSingleShotAgent` itself
- Any change to `derive_approved()` in `shared/security_service.py`
- Renaming the public class

## Acceptance criteria mapping

| Criterion | How satisfied |
|---|---|
| Post-call preserves `derive_approved` + absent-vs-null | Logic lives verbatim in `build_output` |
| `temperature=0.0` and docstring content preserved | Class attr `temperature = 0.0`; contract text on `build_output` + class docstring |
| Public `__init__` / `run` / class name unchanged | Inherit base; keep `DevSecOpsReviewAgent` |
| Monkeypatch targets updated if needed | N/A for current tests; documented for future |
| Output byte-identical for representative inputs | Same context string and output mapping |
| `make test` / `make lint`; 90% on touched files | Implementation plan verifies |
