# Collapse Duplicate Deliver Wrappers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fold the byte-identical backend/frontend `phases/deliver.py` wrappers into one shared `make_run_deliver` binder while keeping each team module as the git monkeypatch surface.

**Architecture:** Add `make_run_deliver` beside (not inside) `run_deliver_impl` in `shared/phases/deliver.py`. The factory returns a bound `run_deliver` that builds `DeliverGitOps` from a caller-supplied `git_ns` at call time, then forwards to `run_deliver_impl`. Each team deliver module shrinks to imports + one bind call.

**Tech Stack:** Python 3.10+, pytest, ruff, existing `DeliverGitOps` / `run_deliver_impl` / `PhaseModels`.

**Spec:** `docs/superpowers/specs/2026-07-22-collapse-duplicate-deliver-wrappers-design.md`

## Global Constraints

- Do not change the body or signature of `run_deliver_impl`.
- Team modules must keep importing git helpers / `write_agent_output` into their own namespace so `test_v2_phases.py` patches (`deliver.create_feature_branch`, etc.) keep working.
- Binder must resolve git callables from `git_ns` **inside each `run_deliver` call**, not at bind time.
- Do not edit devops or `ai_agent_development_team` deliver modules.
- Do not retarget existing deliver tests to patch `shared.git_utils` or the shared binder.
- No GitHub issue numbers in code, comments, commit messages, or docs (PR body only).
- DbC: public functions get `Preconditions:` / `Postconditions:` in their docstrings.
- ≥90% line coverage on touched files; `make lint` and relevant pytest must pass from `backend/`.

## File map

| Path | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/shared/phases/deliver.py` | Add `make_run_deliver`; leave `run_deliver_impl` untouched |
| `backend/agents/software_engineering_team/backend_code_v2_team/phases/deliver.py` | Thin bind site |
| `backend/agents/software_engineering_team/frontend_code_v2_team/phases/deliver.py` | Thin bind site (same shape) |
| `backend/agents/software_engineering_team/tests/test_make_run_deliver.py` | Binder unit test (call-time monkeypatch via stubbed `run_deliver_impl`) |

---

### Task 1: `make_run_deliver` binder + unit test

**Files:**
- Create: `backend/agents/software_engineering_team/tests/test_make_run_deliver.py`
- Modify: `backend/agents/software_engineering_team/shared/phases/deliver.py` (append factory after `run_deliver_impl`; do not edit `run_deliver_impl`)

**Interfaces:**
- Consumes: existing `DeliverGitOps`, `run_deliver_impl`, `PhaseModels`
- Produces:

```python
def make_run_deliver(
    *,
    git_ns: Any,
    models: PhaseModels,
    commit_msg_template: str,
    logger: logging.Logger,
) -> Callable[..., Any]:
    ...
```

  Bound callable keyword-only signature (must match today's team `run_deliver`):

```python
def run_deliver(
    *,
    task_id: str,
    repo_path: Path,
    files: Dict[str, str],
    summary: str,
    task_title: str = "",
    tool_agents: Optional[Dict[Any, Any]] = None,
    task_description: str = "",
    feature_branch_name: Optional[str] = None,
    merge_to_development: bool = True,
) -> Any:
    ...
```

- [ ] **Step 1: Write the failing test**

Create `backend/agents/software_engineering_team/tests/test_make_run_deliver.py` with this exact content:

```python
"""Unit tests for shared.phases.deliver.make_run_deliver."""

from __future__ import annotations

import logging
import types
from pathlib import Path
from typing import Any, Dict, Optional

from software_engineering_team.shared.deliver_utils import DeliverGitOps


def test_make_run_deliver_reads_git_ns_at_call_time(monkeypatch, tmp_path: Path) -> None:
    """Patches applied to git_ns after bind must appear in ops passed to impl."""
    import software_engineering_team.shared.phases.deliver as deliver_mod

    def _noop(*_a, **_k):
        return True

    git_ns = types.SimpleNamespace(
        abort_merge=_noop,
        checkout_branch=_noop,
        commit_working_tree=_noop,
        create_feature_branch=_noop,
        delete_branch=_noop,
        merge_branch=_noop,
        write_agent_output=_noop,
    )

    captured: Dict[str, Any] = {}

    def fake_impl(**kwargs):
        captured["ops"] = kwargs["ops"]
        return "ok"

    monkeypatch.setattr(deliver_mod, "run_deliver_impl", fake_impl)

    models = types.SimpleNamespace()  # unused by stubbed impl
    run_deliver = deliver_mod.make_run_deliver(
        git_ns=git_ns,
        models=models,
        commit_msg_template="[{scope}] {summary}",
        logger=logging.getLogger("test_make_run_deliver"),
    )

    def patched_create(*_a, **_k):
        return (True, "feature/patched")

    git_ns.create_feature_branch = patched_create

    result = run_deliver(
        task_id="t1",
        repo_path=tmp_path,
        files={"a.py": "x"},
        summary="s",
    )

    assert result == "ok"
    ops = captured["ops"]
    assert isinstance(ops, DeliverGitOps)
    assert ops.create_feature_branch is patched_create
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd backend && python -m pytest agents/software_engineering_team/tests/test_make_run_deliver.py::test_make_run_deliver_reads_git_ns_at_call_time -v
```

Expected: FAIL with `AttributeError: module ... has no attribute 'make_run_deliver'` (or similar import/attribute error).

- [ ] **Step 3: Implement `make_run_deliver`**

Append the following to `backend/agents/software_engineering_team/shared/phases/deliver.py` **after** the existing `run_deliver_impl` function. Do not modify `run_deliver_impl`. Add `Callable` to the typing imports at the top of the file:

```python
from typing import Any, Callable, Dict, Optional
```

Then append:

```python
def make_run_deliver(
    *,
    git_ns: Any,
    models: PhaseModels,
    commit_msg_template: str,
    logger: logging.Logger,
) -> Callable[..., Any]:
    """Bind a team-module ``run_deliver`` that keeps ``git_ns`` as the patch surface.

    Preconditions:
        ``git_ns`` exposes ``abort_merge``, ``checkout_branch``,
        ``commit_working_tree``, ``create_feature_branch``, ``delete_branch``,
        ``merge_branch``, and ``write_agent_output``; ``models`` satisfies
        ``PhaseModels``; ``commit_msg_template`` has ``{scope}`` and
        ``{summary}`` slots; ``logger`` is a ``logging.Logger``.
    Postconditions:
        Returns a keyword-only ``run_deliver`` matching the code-v2 team public
        signature. Each call builds a fresh ``DeliverGitOps`` from the *current*
        attributes on ``git_ns`` (so monkeypatches after bind still apply) and
        delegates entirely to ``run_deliver_impl``.
    """

    def run_deliver(
        *,
        task_id: str,
        repo_path: Path,
        files: Dict[str, str],
        summary: str,
        task_title: str = "",
        tool_agents: Optional[Dict[Any, Any]] = None,
        task_description: str = "",
        feature_branch_name: Optional[str] = None,
        merge_to_development: bool = True,
    ) -> Any:
        ops = DeliverGitOps(
            abort_merge=git_ns.abort_merge,
            checkout_branch=git_ns.checkout_branch,
            commit_working_tree=git_ns.commit_working_tree,
            create_feature_branch=git_ns.create_feature_branch,
            delete_branch=git_ns.delete_branch,
            merge_branch=git_ns.merge_branch,
            write_agent_output=git_ns.write_agent_output,
        )
        return run_deliver_impl(
            task_id=task_id,
            repo_path=repo_path,
            files=files,
            summary=summary,
            task_title=task_title,
            tool_agents=tool_agents,
            task_description=task_description,
            feature_branch_name=feature_branch_name,
            merge_to_development=merge_to_development,
            ops=ops,
            commit_msg_template=commit_msg_template,
            models=models,
            logger=logger,
        )

    return run_deliver
```

Also update the module docstring's last paragraph to mention the binder (keep the note that this module never imports git functions directly):

```python
"""
Shared Deliver-phase implementation for the code-v2 teams.

The backend and frontend deliver phases differed only in docstrings. The real
git work already lives in ``shared/deliver_utils.py`` (via ``DeliverGitOps``);
this collapses the remaining orchestration wrapper into one place.

Git callables are supplied by the caller through ``ops`` (a ``DeliverGitOps``)
so the team module remains the monkeypatch boundary for tests — this module
never imports git functions directly. ``make_run_deliver`` builds that ``ops``
bundle from a caller-supplied ``git_ns`` at call time and returns the bound
team-facing ``run_deliver``.
"""
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
cd backend && python -m pytest agents/software_engineering_team/tests/test_make_run_deliver.py::test_make_run_deliver_reads_git_ns_at_call_time -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/shared/phases/deliver.py \
  backend/agents/software_engineering_team/tests/test_make_run_deliver.py
git commit -m "$(cat <<'EOF'
Add make_run_deliver binder for code-v2 deliver wrappers.

EOF
)"
```

---

### Task 2: Collapse backend and frontend team deliver modules

**Files:**
- Modify: `backend/agents/software_engineering_team/backend_code_v2_team/phases/deliver.py` (replace entire file)
- Modify: `backend/agents/software_engineering_team/frontend_code_v2_team/phases/deliver.py` (replace entire file)

**Interfaces:**
- Consumes: `make_run_deliver` from Task 1
- Produces: team `run_deliver` and `DEVELOPMENT_BRANCH` with unchanged public import paths

- [ ] **Step 1: Rewrite backend team deliver module**

Replace `backend/agents/software_engineering_team/backend_code_v2_team/phases/deliver.py` with this exact content:

```python
"""
Deliver phase: write files, commit, and merge to development.

Uses only ``shared.git_utils`` and ``shared.repo_writer`` — no team-specific code.

The orchestration is shared (``shared/phases/deliver.py``); this module keeps the
git-function imports so tests can monkeypatch git operations at this module
boundary, and binds team models / commit template via ``make_run_deliver``.
"""

from __future__ import annotations

import logging
import sys

from software_engineering_team.shared.git_utils import (  # noqa: F401  # re-exported patch surface
    DEVELOPMENT_BRANCH,
    abort_merge,
    checkout_branch,
    commit_working_tree,
    create_feature_branch,
    delete_branch,
    merge_branch,
)
from software_engineering_team.shared.phases.deliver import make_run_deliver
from software_engineering_team.shared.repo_writer import write_agent_output  # noqa: F401

from .. import models as _models
from ..prompts import DELIVER_COMMIT_MSG_TEMPLATE

logger = logging.getLogger(__name__)
__all__ = ["DEVELOPMENT_BRANCH", "run_deliver"]

run_deliver = make_run_deliver(
    git_ns=sys.modules[__name__],
    models=_models,
    commit_msg_template=DELIVER_COMMIT_MSG_TEMPLATE,
    logger=logger,
)
```

- [ ] **Step 2: Rewrite frontend team deliver module**

Replace `backend/agents/software_engineering_team/frontend_code_v2_team/phases/deliver.py` with this exact content (identical to backend except the relative imports resolve to the frontend package):

```python
"""
Deliver phase: write files, commit, and merge to development.

Uses only ``shared.git_utils`` and ``shared.repo_writer`` — no team-specific code.

The orchestration is shared (``shared/phases/deliver.py``); this module keeps the
git-function imports so tests can monkeypatch git operations at this module
boundary, and binds team models / commit template via ``make_run_deliver``.
"""

from __future__ import annotations

import logging
import sys

from software_engineering_team.shared.git_utils import (  # noqa: F401  # re-exported patch surface
    DEVELOPMENT_BRANCH,
    abort_merge,
    checkout_branch,
    commit_working_tree,
    create_feature_branch,
    delete_branch,
    merge_branch,
)
from software_engineering_team.shared.phases.deliver import make_run_deliver
from software_engineering_team.shared.repo_writer import write_agent_output  # noqa: F401

from .. import models as _models
from ..prompts import DELIVER_COMMIT_MSG_TEMPLATE

logger = logging.getLogger(__name__)
__all__ = ["DEVELOPMENT_BRANCH", "run_deliver"]

run_deliver = make_run_deliver(
    git_ns=sys.modules[__name__],
    models=_models,
    commit_msg_template=DELIVER_COMMIT_MSG_TEMPLATE,
    logger=logger,
)
```

- [ ] **Step 3: Run existing deliver-phase regression tests plus binder test**

Run:

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_make_run_deliver.py \
  agents/software_engineering_team/tests/test_v2_phases.py -k deliver -v
```

Expected: all selected tests PASS. In particular, tests that monkeypatch
`deliver.create_feature_branch` / `write_agent_output` / etc. on the team
module must still pass.

- [ ] **Step 4: Confirm the two team files no longer duplicate wrapper bodies**

Run:

```bash
diff \
  backend/agents/software_engineering_team/backend_code_v2_team/phases/deliver.py \
  backend/agents/software_engineering_team/frontend_code_v2_team/phases/deliver.py
```

Expected: no output (files remain byte-identical bind sites — that is fine; the
wrapper *body* lives only in `make_run_deliver`).

Also confirm neither file defines `_git_ops` or a local `def run_deliver`:

```bash
rg -n '_git_ops|def run_deliver' \
  backend/agents/software_engineering_team/backend_code_v2_team/phases/deliver.py \
  backend/agents/software_engineering_team/frontend_code_v2_team/phases/deliver.py
```

Expected: no matches.

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/backend_code_v2_team/phases/deliver.py \
  backend/agents/software_engineering_team/frontend_code_v2_team/phases/deliver.py
git commit -m "$(cat <<'EOF'
Collapse backend/frontend deliver wrappers onto make_run_deliver.

EOF
)"
```

---

### Task 3: Lint and coverage gate

**Files:**
- Verify only (no new production code unless lint/coverage forces a tiny fix)

**Interfaces:**
- Consumes: Tasks 1–2
- Produces: green `make lint` and coverage ≥90% on touched modules

- [ ] **Step 1: Run lint**

Run:

```bash
cd backend && make lint
```

Expected: exit 0. If ruff flags unused imports on the team deliver modules,
keep the `# noqa: F401` comments from Task 2 (those names are the intentional
monkeypatch surface). Do not delete the git helper imports.

- [ ] **Step 2: Run focused coverage on touched files**

Run:

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_make_run_deliver.py \
  agents/software_engineering_team/tests/test_v2_phases.py -k deliver \
  --cov=agents/software_engineering_team/shared/phases/deliver \
  --cov=agents/software_engineering_team/backend_code_v2_team/phases/deliver \
  --cov=agents/software_engineering_team/frontend_code_v2_team/phases/deliver \
  --cov-report=term-missing \
  --cov-fail-under=90
```

Expected: PASS with ≥90% line coverage on each listed module. If a line is
uncovered, extend `test_make_run_deliver.py` (preferred) rather than adding
`# pragma: no cover` unless the line is genuinely unreachable.

- [ ] **Step 3: Commit only if Step 1–2 required fixes**

If lint/coverage required code or test edits:

```bash
git add <touched files>
git commit -m "$(cat <<'EOF'
Fix lint/coverage for deliver wrapper collapse.

EOF
)"
```

If no fixes were needed, skip this commit.

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| `make_run_deliver` in `shared/phases/deliver.py` | Task 1 |
| `run_deliver_impl` untouched | Task 1 (explicit) |
| Call-time `git_ns` attribute lookup | Task 1 test + impl |
| Backend/frontend thin bind sites | Task 2 |
| No duplicated `_git_ops` / `def run_deliver` body | Task 2 Step 4 |
| Existing `test_v2_phases` deliver tests unchanged & green | Task 2 Step 3 |
| Binder unit test stubs `run_deliver_impl` | Task 1 |
| `make lint` + ≥90% coverage | Task 3 |
| Out of scope: devops / ai_agent / review / impl edits | Global Constraints |
