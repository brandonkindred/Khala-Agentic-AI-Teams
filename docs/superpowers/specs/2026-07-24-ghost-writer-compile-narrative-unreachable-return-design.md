# Design: Remove unreachable `return None` in `_compile_narrative`

Date: 2026-07-24

## Goal

Delete the unreachable trailing `return None` after the retry loop in
`GhostWriterElicitationAgent._compile_narrative`, removing the remaining live
instance of that dead-code pattern in the ghost writer agent.

## Context

- An automated review flagged unreachable post-loop returns in
  `backend/agents/blogging/ghost_writer_agent/agent.py` after
  `for attempt in range(2)` loops (a `return default` and a `return None`).
- Those JSON-retry sites were later migrated to `call_json_with_retry`, so the
  cited `return default` / evaluator-loop returns are already gone on `main`.
- The same pattern still exists once: in `_compile_narrative`, every loop branch
  returns (success path, blank narrator text, first-attempt `continue`, or
  second-attempt `return None`), so the statement after the loop is dead.

## Decisions

| Topic | Choice |
|---|---|
| Approach | Delete the trailing `return None` only |
| Scope | `_compile_narrative` in `ghost_writer_agent/agent.py` only |
| Retry loop structure | Unchanged (`for attempt in range(2)` with sleep-on-first-failure) |
| Shared retry helper migration | Out of scope |
| Docstrings / DbC | Unchanged (postconditions already describe returning `None` after retry) |
| Tests | No new tests; existing compile-narrative tests remain the regression net |
| Broader blogging dead-code scan | Out of scope |

## Change surface

Single-file, single-line deletion:

```
backend/agents/blogging/ghost_writer_agent/agent.py
```

Remove the `return None` that sits immediately after the narrator retry loop.
Leave the loop body, logging, sleep, and in-loop returns intact.

## Behavior

No runtime behavior change. After deletion:

- Success / blank result still returns inside the `try` block.
- First failure still logs, sleeps, and `continue`s.
- Second failure still logs and `return None`s inside the `except` block.
- Type checkers / coverage tools stop treating the post-loop return as reachable.

## Verification

1. Confirm the file no longer has a statement after the `_compile_narrative`
   retry loop other than the next method / section divider.
2. Run:
   `pytest backend/agents/blogging/tests/test_ghost_writer_and_more.py -k compile_narrative`
   Expect existing happy-path, empty-user-content, and double-failure tests to
   pass unchanged.

## Out of scope

- Rewriting the retry loop or migrating it onto a shared helper
- Changing narrator prompts, sleep duration, or exception handling
- Scanning other teams for identical post-loop dead returns
- Any broader blogging-agent refactors beyond this deletion
