# shared.run_thread_registry

A single implementation of the process-local "which thread, if any, is running this job's
orchestrator" registry. `coding_team` and `software_engineering_team` had independently hand-rolled
this pattern; this module owns the algorithm so they don't diverge again.

## Model

The state lives in a **`RunThreadRegistry`** instance (a lock, a thread map, and a claim set), owned
by the hosting team — not a shared singleton. A team module instantiates its own registry and, if
its call sites need module-level function names (e.g. for `monkeypatch.setattr(module, "name", ...)`
compatibility), binds thin wrappers over it:

```python
from shared.run_thread_registry import RunThreadRegistry

_registry = RunThreadRegistry()
_register_run_thread = _registry.register
_clear_run_thread = _registry.clear
_is_run_thread_alive = _registry.is_alive
_claim_run_thread = _registry.claim
```

A team whose call sites don't need named-function monkeypatch compatibility can just call the
instance's methods directly (`_registry.register(job_id)`, etc.).

## Methods

- `register(job_id)` — records the current thread as `job_id`'s owner; releases any pending claim.
- `clear(job_id)` — drops the owner and any pending claim (call in a `finally`).
- `is_alive(job_id)` — True iff a registered thread for `job_id` is still running.
- `claim(job_id)` — atomically claims the right to start an orchestrator thread for `job_id`;
  returns False if one is already alive or another claim is outstanding. Closes the
  check-then-spawn race where two concurrent `/resume` calls could otherwise both observe "no
  thread running" and both spawn an orchestrator for the same job.

## Back-compat aliases

`.threads`, `.starting_jobs`, and `.lock` are properties returning live references to the internal
dict/set/lock, for callers (typically tests) that poke the registry's state directly instead of
going through the methods above.

## Current consumers

- `coding_team/api/state.py` — uses the full API, including `claim`.
- `software_engineering_team/api/state.py` — uses `register`/`clear`/`is_alive` only; `claim` is
  available but not yet wired into any route.
