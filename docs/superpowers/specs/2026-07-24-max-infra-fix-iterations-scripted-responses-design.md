# Design: Derive debug/patch scripted responses from MAX_INFRA_FIX_ITERATIONS

Date: 2026-07-24

## Goal

Make `test_loop_terminates_after_max_iterations` stay correct when
`MAX_INFRA_FIX_ITERATIONS` changes, by building the repeated debug/patch
`_ScriptedClient` responses from that constant instead of hardcoding three
pairs. Also remove hardcoded `3` from the test's docstring and inline comment.

## Context

`TestDevOpsPipelineDebugPatchLoop.test_loop_terminates_after_max_iterations` in
`backend/agents/software_engineering_team/tests/test_devops_debug_patch.py`
drives `DevOpsTeamLeadAgent` with a FIFO `_ScriptedClient`. After the early
pipeline agents (clarifier, IaC, CI/CD, deploy), the Phase 4.6 debug/patch loop
consumes one debug response and one patch response per iteration, up to
`MAX_INFRA_FIX_ITERATIONS` (currently `3` in
`software_engineering_team.devops_team.orchestrator`).

The test already imports and asserts against `MAX_INFRA_FIX_ITERATIONS`, but the
scripted response list still repeats three identical debug/patch dict pairs by
hand. The docstring mentions ``iteration N/3`` and a comment says "up to 3
times". If the constant changes, the client supplies the wrong number of
responses and the test breaks or becomes misleading.

## Decisions

| Topic | Choice |
|---|---|
| Approach | Inline `debug_patch_pair` + list splat over `MAX_INFRA_FIX_ITERATIONS` |
| Scope | This one test method only (responses + comment + docstring) |
| Production code | Unchanged |
| Helper extraction | No — single call site |

## Change

In `test_loop_terminates_after_max_iterations`:

1. Keep the existing import of `MAX_INFRA_FIX_ITERATIONS`.
2. Define a two-element `debug_patch_pair` (debug dict, then patch dict) matching
   the current canned payloads.
3. Build `_ScriptedClient` responses as:

   - clarifier → IaC → CI/CD → deploy (unchanged)
   - `*[response for _ in range(MAX_INFRA_FIX_ITERATIONS) for response in debug_patch_pair]`
   - DevSecOps → change review → test validation → doc (unchanged)

4. Docstring: replace the hardcoded ``iteration N/3`` wording with language that
   references `MAX_INFRA_FIX_ITERATIONS` (e.g. that status details contain
   ``iteration {i}/{MAX_INFRA_FIX_ITERATIONS}`` for each attempt).
5. Comment above the debug/patch section: remove "up to 3 times"; tie the note
   to `MAX_INFRA_FIX_ITERATIONS`.

Assertions (`debug_calls`, `phase46_details`, and the
``iteration {i}/{MAX_INFRA_FIX_ITERATIONS}`` substring checks) already use the
constant and stay as-is.

## Out of scope

- Changing `MAX_INFRA_FIX_ITERATIONS` itself
- Refactoring other `_ScriptedClient` fixtures in the file
- Extracting a shared response-builder helper

## Verification

```bash
cd backend
python -m pytest agents/software_engineering_team/tests/test_devops_debug_patch.py::TestDevOpsPipelineDebugPatchLoop::test_loop_terminates_after_max_iterations -q
```

Optional: run the full `TestDevOpsPipelineDebugPatchLoop` class for regression.
