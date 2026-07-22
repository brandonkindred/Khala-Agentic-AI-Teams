# Migrate infra_patch and infra_debug agents onto DevOpsSingleShotAgent

**Date:** 2026-07-21  
**Status:** Approved for implementation planning

## Goal

Migrate `InfraPatchAgent` and `InfraDebugAgent` onto `DevOpsSingleShotAgent` as thin subclasses that exercise the base's `pre_call` and `build_output` (post-call) hooks, preserving public class names, constructor/run contracts, prompts, and output behavior byte-for-byte.

## Motivation

These two agents are the first production consumers of the template's special-case hooks:

- **Patch** — early-return before any LLM call when `debug_output.fixable` is false
- **Debug** — post-call construction of nested `IaCExecutionError` objects plus `_FIXABLE_TYPES`-based `fixable` derivation

Migrating them after the pure-boilerplate trio validates both hook paths on real logic with higher regression risk than name/prompt/output-model mapping alone.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Approach | Inline subclass hooks (same shape as the boilerplate migration) |
| Base branch | `refactor/migrate-boilerplate-devops-agents` (includes the template + three migrated agents) |
| Post-call surface | `build_output` — there is no separate `post_call` method on the base |
| Temperature / think | Inherit base defaults (`0.1` / `True`) — same as today |
| `_FIXABLE_TYPES` | Keep as a module-level frozenset on `infra_debug_agent/agent.py` |
| Test / monkeypatch changes | None — existing `_StubClient`, `_TripWire`, and `_patch_fenced_response` paths still work |
| Scope | These two `agent.py` files only |

## Architecture

### Files touched

| Path | Change |
|---|---|
| `devops_team/infra_patch_agent/agent.py` | `InfraPatchAgent(DevOpsSingleShotAgent)` with `pre_call` + `build_context` + `build_output` |
| `devops_team/infra_debug_agent/agent.py` | `InfraDebugAgent(DevOpsSingleShotAgent)` with `build_context` + `build_output` |

### Files not touched

- `prompts.py`, `models.py`, package `__init__.py` for each agent
- `orchestrator.py`, `phase2_graph.py`, `_agent_template.py`
- `test_devops_debug_patch.py`, `test_devops_team.py`
- `devsecops_review_agent`, `doc_runbook_agent`, and the three already-migrated boilerplate agents

### Per-agent contract

Each class:

1. Subclasses `DevOpsSingleShotAgent`
2. Sets `PROMPT` to the existing prompt constant from `prompts.py`
3. Implements `build_context(self, input_data) -> str` with the **same** string shape as today
4. Implements `build_output(self, input_data, data: dict)` with the **same** field mapping / defaults as today
5. Drops local imports of `resolve_strands_model`, `get_strands_model`, and `complete_json_with_continuation` (owned by the base)
6. Does not override `temperature` or `think`

Public surface stays:

- Class names `InfraPatchAgent` / `InfraDebugAgent` unchanged
- `__init__(llm_client)` from the base
- `run(input_data) -> Output` from the base

### Hook mapping (must remain byte-identical)

**InfraPatchAgent**

- `pre_call`: if `not input_data.debug_output.fixable`, return `IaCPatchOutput(summary="Errors are not fixable via code changes")`; otherwise return `None`
- Context: join `debug_output.errors` as `- [{error_type}] {file_path or '?'}:{line_number or '?'} — {error_message}`, then append each `original_artifacts` entry as `### {fname} ###\n{content}`; wrap as `--- Errors ---\n...\n\n--- Current Artifacts ---\n...\n`
- Output: `patched = data.get("patched_artifacts") or {}`, then `{k: v for k, v in patched.items() if v and v.strip()}`; `summary=data.get("summary", "")`; `edits_applied=data.get("edits_applied", len(patched))`

**InfraDebugAgent**

- No `pre_call` override
- Context: `Tool` / `Command`, full `execution_output`, artifacts snippet from `list(artifacts.items())[:5]` with `content[:2000]` each
- Output: for each entry in `data.get("errors") or []`, build `IaCExecutionError` with `error_type` default `"unknown"`, `tool` default `input_data.tool_name`, optional `file_path` / `line_number`, `error_message` default `""`, `raw_output=input_data.execution_output`
- Derived: `fixable = bool(errors) and all(e.error_type in _FIXABLE_TYPES for e in errors)` where `_FIXABLE_TYPES = frozenset({"syntax", "validation"})`
- Return `fixable=data.get("fixable", fixable)` and `summary=data.get("summary", "")`

## Monkeypatchability

The base calls `complete_json_with_continuation` bound on `_agent_template`. Current tests for these two agents do **not** monkeypatch the per-agent module import; they use `_StubClient` / `_TripWire` or patch `shared.llm.Agent` via `_patch_fenced_response`. Therefore this migration makes **no** test-file edits. If a future test patches the helper by name, it must target `software_engineering_team.devops_team._agent_template.complete_json_with_continuation`.

## Testing

- Rely on existing `TestInfraDebugAgent`, `TestInfraPatchAgent` (including the not-fixable early-return trip-wire), pipeline loop tests in `test_devops_debug_patch.py`, and the two fence-recovery tests in `test_devops_team.py`.
- Run focused pytest for those classes / fence tests plus `make lint` from `backend/`.
- Confirm ≥90% line coverage on each rewritten `agent.py`.

## Out of scope

- Migrating `devsecops_review_agent` or `doc_runbook_agent`
- Changing prompts, models, orchestrator wiring, or `DevOpsSingleShotAgent` itself
- Renaming classes (public names remain `InfraPatchAgent` / `InfraDebugAgent`)

## Acceptance criteria mapping

| Criterion | How satisfied |
|---|---|
| Patch early-return via pre-call | `pre_call` returns the same `IaCPatchOutput(summary=...)` |
| Debug derived fields via post-call | Error list + `_FIXABLE_TYPES` derivation live in `build_output` |
| Public `__init__` / `run` / class names unchanged | Inherit base; keep `InfraPatchAgent` / `InfraDebugAgent` |
| Monkeypatch targets updated if needed | N/A for current tests; documented for future |
| Output byte-identical for representative inputs | Same context strings, filters, and `data.get` defaults |
| `make test` / `make lint`; 90% on touched files | Implementation plan verifies |
