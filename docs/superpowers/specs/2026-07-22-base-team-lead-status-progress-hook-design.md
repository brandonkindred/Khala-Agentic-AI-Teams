# Add a status/progress hook to BaseTeamLead

**Date:** 2026-07-22  
**Status:** Approved for implementation planning  

## Goal

Give `BaseTeamLead` an optional per-run status/progress hook that subclasses can invoke to report phase progress, without wiring any consumer yet. This is prerequisite infrastructure for migrating `devops_team` onto the shared base.

## Motivation

`devops_team/orchestrator.py`'s `_run_pipeline` today reports status only via interleaved `logger.info` calls, with no structured status/progress callback. `BaseTeamLead` currently provides constructor/state-sharing and repo-briefing cache plumbing only. A shared hook lets a later devops migration replace those log lines (and eventually bind a job updater) without inventing a one-off pattern.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Callback lifetime | Per `run_workflow` call — subclass assigns `self._status_callback` for that run (and should clear it when the run ends) |
| Report signature | Hybrid: `_report_status(phase, detail="", progress=None, **extra)` |
| Payload shape | Callback receives kwargs: `phase`, `detail`, and `progress` only when not `None`, plus any `**extra` |
| Missing callback | No-op |
| Callback failures | Catch, `logger.warning`, never re-raise into the pipeline |
| Constructor change | None — do not add `status_callback=` to `__init__` |
| Context manager | Out of scope (can be added later if a consumer wants auto clear) |
| Consumer migration | Out of scope — no `devops_team/orchestrator.py` changes; no coding_team `update_fn`/`persist_fn` changes |

## Architecture

### Files touched

| Path | Change |
|---|---|
| `backend/agents/software_engineering_team/shared/team_lead_base.py` | Add `_status_callback` slot + `_report_status`; update module/class docs and invariants |
| `backend/agents/software_engineering_team/tests/test_team_lead_base.py` | Unit tests for no-op, payload forwarding, omitted progress, swallowed errors |

### Files not touched

- `devops_team/orchestrator.py` and its tests
- `backend_code_v2_team/` / `frontend_code_v2_team/` orchestrators
- coding_team progress/`update_fn`/`persist_fn` paths

### API

```python
class BaseTeamLead:
    def __init__(self, llm_client: LLMClient, *, extensions, exclude_dirs, max_chars) -> None:
        # ...existing fields...
        self._status_callback: Optional[Callable[..., None]] = None

    def _report_status(
        self,
        phase: str,
        detail: str = "",
        progress: Optional[float] = None,
        **extra: Any,
    ) -> None:
        """Report phase progress via the optional per-run status callback.

        Preconditions: ``phase`` is a non-empty str.
        Postconditions: if ``_status_callback`` is set, it is invoked once with
          kwargs ``phase``, ``detail``, optional ``progress`` (omitted when
          None), and ``**extra``; callback exceptions are logged and swallowed;
          if the callback is None, this is a no-op. Never raises into the caller.
        """
```

Subclass usage (future; not in this change):

```python
self._status_callback = update_job  # or a thin adapter
try:
    self._report_status("phase2", detail="change design", progress=0.4)
finally:
    self._status_callback = None
```

### Error handling

Mirror the tech-lead review progress pattern: on callback failure, log  
`logger.warning("team lead status callback failed (ignored): %s", e)` and continue.

## Testing

In `test_team_lead_base.py`:

1. **No-op when unset** — `_report_status(...)` does not raise when `_status_callback` is `None`.
2. **Forwards hybrid payload** — mock callback receives `phase`, `detail`, `progress`, and any `**extra` (e.g. `status_text`).
3. **Omits None progress** — when `progress` is omitted/`None`, callback kwargs do not include `progress`.
4. **Swallows callback errors** — a raising callback does not propagate from `_report_status`.

Coverage floor: 90% on touched files. Verification: `make test` and `make lint` from `backend/`.

## Out of scope

- Migrating devops `logger.info` status points onto the hook
- Any status/progress redesign for coding_team
- Context-manager binder / constructor injection
- Changing `BaseTeamLead`'s repo-briefing constructor contract so devops can subclass it (separate migration work)
