# BaseTeamLead Status/Progress Hook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional per-run status/progress hook to `BaseTeamLead` so subclasses can report phase progress, covered by unit tests, with no consumer wiring yet.

**Architecture:** `BaseTeamLead` gains a `_status_callback` instance slot (default `None`) and a `_report_status(phase, detail="", progress=None, **extra)` method that no-ops when unset, forwards a normalized kwargs payload when set, and swallows callback exceptions after a warning log. Subclasses assign the callback per `run_workflow` call; this plan does not migrate devops or code-v2 consumers.

**Tech Stack:** Python 3.10, pytest, unittest.mock, Ruff (via `make lint`)

**Spec:** `docs/superpowers/specs/2026-07-22-base-team-lead-status-progress-hook-design.md`

## Global Constraints

- Callback lifetime is per `run_workflow` call (assign/clear on the instance); do not add `status_callback=` to `__init__`.
- Report signature is hybrid: `_report_status(phase, detail="", progress=None, **extra)`.
- Callback kwargs include `phase`, `detail`, `progress` only when not `None`, plus `**extra`.
- Missing callback → no-op; callback failures → `logger.warning` and never re-raise.
- No changes to `devops_team/orchestrator.py`, code-v2 orchestrators, or coding_team `update_fn`/`persist_fn`.
- 90% coverage floor on touched files; `make test` and `make lint` must pass from `backend/`.
- Design-by-Contract: document Preconditions/Postconditions on `_report_status`; assert non-empty `phase`.

## File Structure

| Path | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/shared/team_lead_base.py` | `_status_callback` slot + `_report_status` implementation; module/class doc updates |
| `backend/agents/software_engineering_team/tests/test_team_lead_base.py` | Unit tests for no-op, payload forward, omitted progress, swallowed errors |

---

### Task 1: Status/progress hook (TDD)

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_team_lead_base.py`
- Modify: `backend/agents/software_engineering_team/shared/team_lead_base.py`

**Interfaces:**
- Consumes: existing `BaseTeamLead` constructor (`llm_client`, `extensions`, `exclude_dirs`, `max_chars`)
- Produces: `BaseTeamLead._status_callback: Optional[Callable[..., None]]`; `BaseTeamLead._report_status(phase: str, detail: str = "", progress: Optional[float] = None, **extra: Any) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `backend/agents/software_engineering_team/tests/test_team_lead_base.py` (keep existing helpers/imports; add `Callable` only if needed — prefer a plain list/MagicMock):

```python
def test_report_status_noop_when_callback_unset():
    lead = _make_lead()
    assert lead._status_callback is None
    lead._report_status("phase1", detail="starting")  # must not raise


def test_report_status_forwards_hybrid_payload():
    lead = _make_lead()
    calls = []

    def _cb(**kwargs):
        calls.append(kwargs)

    lead._status_callback = _cb
    lead._report_status(
        "phase2",
        detail="change design",
        progress=0.4,
        status_text="DevOps phase 2",
    )
    assert calls == [
        {
            "phase": "phase2",
            "detail": "change design",
            "progress": 0.4,
            "status_text": "DevOps phase 2",
        }
    ]


def test_report_status_omits_none_progress():
    lead = _make_lead()
    calls = []
    lead._status_callback = lambda **kwargs: calls.append(kwargs)
    lead._report_status("phase3", detail="validation")
    assert calls == [{"phase": "phase3", "detail": "validation"}]
    assert "progress" not in calls[0]


def test_report_status_swallows_callback_errors():
    lead = _make_lead()

    def _boom(**_kwargs):
        raise RuntimeError("callback exploded")

    lead._status_callback = _boom
    lead._report_status("phase4", detail="review")  # must not raise


def test_report_status_rejects_empty_phase():
    lead = _make_lead()
    with pytest.raises(AssertionError):
        lead._report_status("")
```

Also extend `test_init_stores_llm_and_starts_with_empty_cache_dict` to assert the new slot starts clear:

```python
assert lead._status_callback is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run from `backend/`:

```bash
python -m pytest agents/software_engineering_team/tests/test_team_lead_base.py -v
```

Expected: FAIL — `AttributeError: '_status_callback'` and/or `'_report_status'` missing on `BaseTeamLead`.

- [ ] **Step 3: Implement the minimal hook**

In `backend/agents/software_engineering_team/shared/team_lead_base.py`:

1. Update imports:

```python
import logging
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Optional
```

2. Add module logger after imports:

```python
logger = logging.getLogger(__name__)
```

3. Mention the hook briefly in the module docstring (one sentence: base also provides an optional per-run status/progress callback via `_report_status`).

4. Update class docstring invariants to include `_status_callback`:

```text
Invariants: instance state is limited to ``llm``, the injected
extensions/exclude_dirs/max_chars, ``_repo_context_caches``, and
``_status_callback`` (optional per-run status hook; default None).
```

5. In `__init__`, after `_repo_context_caches` init:

```python
# Optional per-run status/progress callback. Subclasses assign this at the
# start of run_workflow (and clear it when the run ends); BaseTeamLead does
# not accept it via the constructor.
self._status_callback: Optional[Callable[..., None]] = None
```

6. Add method after `_repo_context_cache_for`:

```python
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
    assert isinstance(phase, str) and phase, "phase must be a non-empty str"
    callback = self._status_callback
    if callback is None:
        return
    payload: Dict[str, Any] = {"phase": phase, "detail": detail, **extra}
    if progress is not None:
        payload["progress"] = progress
    try:
        callback(**payload)
    except Exception as e:
        logger.warning("team lead status callback failed (ignored): %s", e)
```

Do **not** edit `devops_team/orchestrator.py` or any code-v2/coding_team consumer.

- [ ] **Step 4: Run unit tests to verify they pass**

```bash
python -m pytest agents/software_engineering_team/tests/test_team_lead_base.py -v --cov=agents/software_engineering_team/shared/team_lead_base --cov-report=term-missing
```

Expected: all tests PASS; `team_lead_base.py` line coverage ≥ 90%.

- [ ] **Step 5: Lint and broader SE-team sanity check**

From `backend/`:

```bash
make lint
python -m pytest agents/software_engineering_team/tests/test_team_lead_base.py agents/software_engineering_team/tests/test_backend_code_v2_team.py agents/software_engineering_team/tests/test_frontend_code_v2_team.py -q
```

Expected: Ruff clean; existing team-lead subclass tests still pass (constructor invariant unchanged aside from the new default `None` slot).

Full suite when ready:

```bash
make test
```

- [ ] **Step 6: Commit**

Only if the working tree is not mid-merge / has no unrelated staged files. Stage just:

```bash
git add \
  backend/agents/software_engineering_team/shared/team_lead_base.py \
  backend/agents/software_engineering_team/tests/test_team_lead_base.py \
  docs/superpowers/specs/2026-07-22-base-team-lead-status-progress-hook-design.md \
  docs/superpowers/plans/2026-07-22-base-team-lead-status-progress-hook.md
git commit -m "$(cat <<'EOF'
Add optional status/progress hook to BaseTeamLead.

Gives subclasses a per-run _report_status callback for phase progress
without wiring devops or code-v2 consumers yet.
EOF
)"
```

If the repo is still mid-merge, stop and resolve/abort the merge before committing; do not fold this into an unrelated merge commit.

---

## Self-review

1. **Spec coverage:** Hook API, payload rules, no-op, swallow-errors, tests, no devops edits, lint/test/coverage — all covered by Task 1.
2. **Placeholders:** None.
3. **Type consistency:** `_status_callback` / `_report_status` names and signatures match the approved spec.
