# Extract QA + Security Testing-Phase Unit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the QA + security testing-phase unit (`_AgentTestingPhaseSpec`, both specs, `_run_agent_testing_phase`, and both dispatchers) into `shared/phases/review.py` as one piece, with backend public APIs as thin wrappers that inject team constructors.

**Architecture:** Mirror `run_code_review_phase_impl`: shared owns `run_qa_testing_phase_impl` / `run_security_testing_phase_impl` plus the frozen-spec helper; backend wrappers inject `phase_review_result_cls`, `tool_phase_input_factory` (from `REVIEW_CONFIG`), and `agent_runner=partial(_run_qa_agent|_run_security_agent, ...)`. Spec `tool_kind` values are the string enum values `"testing_qa"` / `"security"` so shared does not import backend models.

**Tech Stack:** Python 3.10+, pytest, ruff, existing `ReviewConfig` / `Phase` / `ReviewIssue` / `AgentReviewCache`.

**Spec:** `docs/superpowers/specs/2026-07-22-extract-qa-security-testing-phase-design.md`

## Global Constraints

- Move `_AgentTestingPhaseSpec`, `_QA_TESTING_PHASE_SPEC`, `_SECURITY_TESTING_PHASE_SPEC`, `_run_agent_testing_phase`, and both phase dispatchers together — no partial extraction.
- Preserve frozen-dataclass parameterization and containment semantics exactly (QA missing severity `high`, security `critical`; agent failure → synthetic issue; tool failure → log-and-skip).
- Shared must not import `backend_code_v2_team.models`; inject `phase_review_result_cls` and `tool_phase_input_factory`; type `tool_kind` as `Any` (store string values).
- Backend keeps public `run_qa_testing_phase` / `run_security_testing_phase` signatures; `_run_qa_agent` / `_run_security_agent` stay on the backend review module (patch surface).
- Do not add frontend entry points (tracked separately).
- Do not change `run_code_review_phase_impl` behavior.
- No GitHub issue numbers in code, comments, commit messages, or docs (PR body only).
- DbC: public/`_impl` functions get `Preconditions:` / `Postconditions:` in docstrings.
- ≥90% line coverage on touched files; `make lint` and relevant pytest must pass from `backend/`.

## File map

| Path | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/shared/phases/review.py` | Own testing-phase unit + `*_impl` dispatchers beside existing code-review impl |
| `backend/agents/software_engineering_team/backend_code_v2_team/phases/review.py` | Thin wrappers; keep `_run_qa_agent` / `_run_security_agent` |
| `backend/agents/software_engineering_team/tests/test_shared_agent_testing_phase.py` | Focused unit tests for shared helper (injection + containment) |
| `backend/agents/software_engineering_team/tests/test_v2_review_phase.py` | Untouched regression (imports backend public APIs) |
| `backend/agents/software_engineering_team/tests/test_microtask_review_gates.py` | Untouched regression |

---

### Task 1: Shared testing-phase unit + unit tests

**Files:**
- Create: `backend/agents/software_engineering_team/tests/test_shared_agent_testing_phase.py`
- Modify: `backend/agents/software_engineering_team/shared/phases/review.py` (append after `run_code_review_phase_impl`; update module docstring)

**Interfaces:**
- Consumes: `Task`, shared `Phase`, shared `ReviewIssue`, `is_blocking`, `AgentReviewCache`
- Produces:

```python
@dataclass(frozen=True)
class _AgentTestingPhaseSpec:
    phase_name: str
    phase_label: str
    next_step: str
    detail_run_msg: str
    tool_kind: Any  # string enum value, e.g. "testing_qa" / "security"
    tool_detail_msg: str
    tool_label: str
    missing_agent_label: str
    gate_label: str
    missing_severity: str
    missing_description: str
    missing_recommendation: str

def _run_agent_testing_phase(
    *,
    spec: _AgentTestingPhaseSpec,
    task: Task,
    microtask: Any,
    files: Dict[str, str],
    review_agent: Any,
    agent_runner: Callable[..., List[ReviewIssue]],
    tool_agents: Optional[Dict[Any, Any]],
    repo_path: Optional[Path],
    detail_callback: Optional[Callable[[str], None]],
    language: str,
    cache: Optional[AgentReviewCache] = None,
    phase_review_result_cls: Callable[..., Any],
    tool_phase_input_factory: Callable[..., Any],
) -> Any: ...

def run_qa_testing_phase_impl(
    *,
    task: Task,
    microtask: Any,
    files: Dict[str, str],
    review_agent: Any = None,
    agent_runner: Callable[..., List[ReviewIssue]],
    tool_agents: Optional[Dict[Any, Any]] = None,
    repo_path: Optional[Path] = None,
    detail_callback: Optional[Callable[[str], None]] = None,
    language: str = "python",
    cache: Optional[AgentReviewCache] = None,
    phase_review_result_cls: Callable[..., Any],
    tool_phase_input_factory: Callable[..., Any],
) -> Any: ...

def run_security_testing_phase_impl(
    # same keyword-only signature as run_qa_testing_phase_impl
) -> Any: ...
```

- [ ] **Step 1: Write the failing tests**

Create `backend/agents/software_engineering_team/tests/test_shared_agent_testing_phase.py`:

```python
"""Unit tests for shared QA/security testing-phase helpers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

from software_engineering_team.shared.models import Task
from software_engineering_team.shared.v2_models import ReviewIssue


def _task() -> Task:
    return Task(id="t-1", title="T", description="desc")


def _microtask() -> Any:
    return SimpleNamespace(id="mt-1", title="MT", description="do thing")


class _PhaseResult:
    def __init__(self, *, passed, issues, summary, phase_name, **_kwargs):
        self.passed = passed
        self.issues = issues
        self.summary = summary
        self.phase_name = phase_name


class _PhaseInput:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def test_run_qa_testing_phase_impl_contains_agent_failure():
    from software_engineering_team.shared.phases.review import run_qa_testing_phase_impl

    def _boom(**_kw):
        raise RuntimeError("qa agent exploded")

    result = run_qa_testing_phase_impl(
        task=_task(),
        microtask=_microtask(),
        files={"x.py": "code"},
        review_agent=object(),
        agent_runner=_boom,
        phase_review_result_cls=_PhaseResult,
        tool_phase_input_factory=_PhaseInput,
    )

    assert result.passed is False
    assert result.phase_name == "qa"
    assert any(
        i.source == "qa" and i.severity == "high" and "qa agent exploded" in i.description
        for i in result.issues
    )


def test_run_security_testing_phase_impl_contains_agent_failure():
    from software_engineering_team.shared.phases.review import run_security_testing_phase_impl

    def _boom(**_kw):
        raise RuntimeError("security agent exploded")

    result = run_security_testing_phase_impl(
        task=_task(),
        microtask=_microtask(),
        files={"x.py": "code"},
        review_agent=object(),
        agent_runner=_boom,
        phase_review_result_cls=_PhaseResult,
        tool_phase_input_factory=_PhaseInput,
    )

    assert result.passed is False
    assert result.phase_name == "security"
    assert any(
        i.source == "security"
        and i.severity == "critical"
        and "security agent exploded" in i.description
        for i in result.issues
    )


def test_run_agent_testing_phase_skips_gate_when_no_agents():
    from software_engineering_team.shared.phases.review import (
        _QA_TESTING_PHASE_SPEC,
        _run_agent_testing_phase,
    )

    def _unused(**_kw) -> List[ReviewIssue]:
        raise AssertionError("agent_runner must not run when review_agent is None")

    result = _run_agent_testing_phase(
        spec=_QA_TESTING_PHASE_SPEC,
        task=_task(),
        microtask=_microtask(),
        files={"x.py": "code"},
        review_agent=None,
        agent_runner=_unused,
        tool_agents=None,
        repo_path=None,
        detail_callback=None,
        language="python",
        phase_review_result_cls=_PhaseResult,
        tool_phase_input_factory=_PhaseInput,
    )

    assert result.passed is False
    assert any(
        i.source == "qa"
        and i.severity == "high"
        and "QA agent not available" in i.description
        for i in result.issues
    )


def test_run_agent_testing_phase_invokes_tool_agent_via_factory():
    from software_engineering_team.shared.phases.review import (
        _QA_TESTING_PHASE_SPEC,
        _run_agent_testing_phase,
    )

    captured: Dict[str, Any] = {}

    class _Tool:
        def review(self, phase_inp):
            captured["phase_inp"] = phase_inp
            return SimpleNamespace(
                issues=[
                    ReviewIssue(
                        source="qa",
                        severity="medium",
                        description="tool finding",
                        recommendation="",
                    )
                ]
            )

    def _agent_runner(**_kw) -> List[ReviewIssue]:
        return []

    result = _run_agent_testing_phase(
        spec=_QA_TESTING_PHASE_SPEC,
        task=_task(),
        microtask=_microtask(),
        files={"x.py": "code"},
        review_agent=object(),
        agent_runner=_agent_runner,
        tool_agents={_QA_TESTING_PHASE_SPEC.tool_kind: _Tool()},
        repo_path=None,
        detail_callback=None,
        language="python",
        phase_review_result_cls=_PhaseResult,
        tool_phase_input_factory=_PhaseInput,
    )

    assert result.passed is True
    assert any(i.description == "tool finding" for i in result.issues)
    assert captured["phase_inp"].kwargs["phase"].value == "review"
    assert captured["phase_inp"].kwargs["current_files"] == {"x.py": "code"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run from `backend/`:

```bash
cd backend && python -m pytest agents/software_engineering_team/tests/test_shared_agent_testing_phase.py -v
```

Expected: FAIL with `ImportError` / `AttributeError` for `run_qa_testing_phase_impl` (or related names) — not assertion failures on existing code.

- [ ] **Step 3: Implement the shared unit**

Update the module docstring of `shared/phases/review.py` to mention both the code-review and QA/security testing-phase units.

Append these imports (merge with existing — do not duplicate):

```python
from dataclasses import dataclass

from software_engineering_team.shared.agent_review import AgentReviewCache
from software_engineering_team.shared.v2_models import Phase, ReviewIssue
```

(`ReviewIssue` is already imported; keep a single import. Add `Phase` to that import. Add `dataclass` and `AgentReviewCache`.)

Then append the full unit. Move the body from
`backend_code_v2_team/phases/review.py` with these exact adaptations:

1. `_AgentTestingPhaseSpec.tool_kind: Any` (not `ToolAgentKind`).
2. Spec instances use string values:

```python
_QA_TESTING_PHASE_SPEC = _AgentTestingPhaseSpec(
    phase_name="qa",
    phase_label="QA testing",
    next_step="Running QA agent analysis",
    detail_run_msg="Running QA testing...",
    tool_kind="testing_qa",
    tool_detail_msg="Running QA tool agent review...",
    tool_label="QA",
    missing_agent_label="QA agent",
    gate_label="QA gate",
    missing_severity="high",
    missing_description="QA agent not available — QA review was skipped. This is a quality risk.",
    missing_recommendation="Ensure QA agent is configured before running the pipeline.",
)

_SECURITY_TESTING_PHASE_SPEC = _AgentTestingPhaseSpec(
    phase_name="security",
    phase_label="Security testing",
    next_step="Running security scan",
    detail_run_msg="Running security scan...",
    tool_kind="security",
    tool_detail_msg="Running security tool agent review...",
    tool_label="Security",
    missing_agent_label="Security agent",
    gate_label="security gate",
    missing_severity="critical",
    missing_description="Security agent not available — security review was skipped. This is a critical risk.",
    missing_recommendation="Ensure security agent is configured before running the pipeline.",
)
```

3. `_run_agent_testing_phase` takes `phase_review_result_cls` and `tool_phase_input_factory`; replace:

```python
# BEFORE (backend-coupled)
phase_inp = ToolAgentPhaseInput(phase=Phase.REVIEW, ...)
return PhaseReviewResult(...)

# AFTER
phase_inp = tool_phase_input_factory(
    phase=Phase.REVIEW,
    microtask=microtask,
    repo_path=str(repo_path) if repo_path else "",
    existing_code="",
    spec_context=task.description or "",
    language=language,
    current_files=files,
    review_issues=issues,
    task_title=task.title or "",
    task_description=f"Microtask: {microtask.description or microtask.title}",
    task_id=task_id,
)
...
return phase_review_result_cls(
    passed=passed,
    issues=issues,
    summary=summary,
    phase_name=spec.phase_name,
)
```

Keep every other line of the helper (logging, containment, skip-gate, `is_blocking`) identical.

4. Dispatchers:

```python
def run_qa_testing_phase_impl(
    *,
    task: Task,
    microtask: Any,
    files: Dict[str, str],
    review_agent: Any = None,
    agent_runner: Callable[..., List[ReviewIssue]],
    tool_agents: Optional[Dict[Any, Any]] = None,
    repo_path: Optional[Path] = None,
    detail_callback: Optional[Callable[[str], None]] = None,
    language: str = "python",
    cache: Optional[AgentReviewCache] = None,
    phase_review_result_cls: Callable[..., Any],
    tool_phase_input_factory: Callable[..., Any],
) -> Any:
    """Run QA testing phase: bug detection, test coverage, quality assurance.

    Preconditions:
        - ``agent_runner`` matches the shared helper's runner contract when
          ``review_agent`` is not None.
        - ``phase_review_result_cls`` / ``tool_phase_input_factory`` construct
          the team's result / tool-phase input types.
    Postconditions:
        - Delegates to ``_run_agent_testing_phase`` with ``_QA_TESTING_PHASE_SPEC``;
          never raises (containment is the helper's).
    """
    return _run_agent_testing_phase(
        spec=_QA_TESTING_PHASE_SPEC,
        task=task,
        microtask=microtask,
        files=files,
        review_agent=review_agent,
        agent_runner=agent_runner,
        tool_agents=tool_agents,
        repo_path=repo_path,
        detail_callback=detail_callback,
        language=language,
        cache=cache,
        phase_review_result_cls=phase_review_result_cls,
        tool_phase_input_factory=tool_phase_input_factory,
    )


def run_security_testing_phase_impl(
    *,
    task: Task,
    microtask: Any,
    files: Dict[str, str],
    review_agent: Any = None,
    agent_runner: Callable[..., List[ReviewIssue]],
    tool_agents: Optional[Dict[Any, Any]] = None,
    repo_path: Optional[Path] = None,
    detail_callback: Optional[Callable[[str], None]] = None,
    language: str = "python",
    cache: Optional[AgentReviewCache] = None,
    phase_review_result_cls: Callable[..., Any],
    tool_phase_input_factory: Callable[..., Any],
) -> Any:
    """Run security testing phase: vulnerability scanning, security best practices.

    Preconditions / Postconditions: same as ``run_qa_testing_phase_impl``, but
    binds ``_SECURITY_TESTING_PHASE_SPEC``.
    """
    return _run_agent_testing_phase(
        spec=_SECURITY_TESTING_PHASE_SPEC,
        task=task,
        microtask=microtask,
        files=files,
        review_agent=review_agent,
        agent_runner=agent_runner,
        tool_agents=tool_agents,
        repo_path=repo_path,
        detail_callback=detail_callback,
        language=language,
        cache=cache,
        phase_review_result_cls=phase_review_result_cls,
        tool_phase_input_factory=tool_phase_input_factory,
    )
```

Do **not** delete the backend copies yet (Task 2). Leaving both briefly is fine; Task 2 removes the backend originals.

- [ ] **Step 4: Run shared unit tests — expect PASS**

```bash
cd backend && python -m pytest agents/software_engineering_team/tests/test_shared_agent_testing_phase.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/shared/phases/review.py \
  backend/agents/software_engineering_team/tests/test_shared_agent_testing_phase.py
git commit -m "$(cat <<'EOF'
Add shared QA/security testing-phase helpers beside code-review impl.

EOF
)"
```

---

### Task 2: Thin backend wrappers; delete local copies

**Files:**
- Modify: `backend/agents/software_engineering_team/backend_code_v2_team/phases/review.py`

**Interfaces:**
- Consumes: `run_qa_testing_phase_impl`, `run_security_testing_phase_impl` from shared; local `_run_qa_agent` / `_run_security_agent`; `REVIEW_CONFIG`; `PhaseReviewResult`
- Produces: unchanged public signatures for `run_qa_testing_phase` / `run_security_testing_phase`

- [ ] **Step 1: Update imports**

In `backend_code_v2_team/phases/review.py`:

1. Change:

```python
from software_engineering_team.shared.phases.review import run_code_review_phase_impl
```

to:

```python
from software_engineering_team.shared.phases.review import (
    run_code_review_phase_impl,
    run_qa_testing_phase_impl,
    run_security_testing_phase_impl,
)
```

2. Remove now-unused imports once the local unit is deleted:
   - `from dataclasses import dataclass`
   - `from software_engineering_team.shared.security_service import is_blocking`
   - `Phase` and `ToolAgentPhaseInput` from the `..models` import (keep `PhaseReviewResult`, `ToolAgentKind`, `Microtask`, `ReviewIssue`, etc. as still needed)

Keep `from functools import partial` — wrappers still use it.

- [ ] **Step 2: Replace the local unit with thin wrappers**

Delete the entire block from `@dataclass(frozen=True) class _AgentTestingPhaseSpec` through the end of `run_security_testing_phase` (inclusive), and replace with:

```python
def run_qa_testing_phase(
    *,
    task: Task,
    microtask: Microtask,
    files: Dict[str, str],
    qa_agent: Any = None,
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    repo_path: Optional[Path] = None,
    detail_callback: Optional[Callable[[str], None]] = None,
    language: str = "python",
    cache: Optional[AgentReviewCache] = None,
) -> PhaseReviewResult:
    """
    Run QA testing phase: bug detection, test coverage, quality assurance.

    Thin wrapper over the shared parametrised implementation
    (:func:`software_engineering_team.shared.phases.review.run_qa_testing_phase_impl`).
    ``_run_qa_agent`` is referenced by bare module-global name inside ``partial``
    at call time so this module stays the test patch surface.

    Preconditions:
        - ``microtask`` exposes ``.id`` / ``.title`` / ``.description``.
    Postconditions:
        - Returns a :class:`PhaseReviewResult`; never raises (shared containment).
    """
    return run_qa_testing_phase_impl(
        task=task,
        microtask=microtask,
        files=files,
        review_agent=qa_agent,
        agent_runner=partial(_run_qa_agent, qa_agent=qa_agent),
        tool_agents=tool_agents,
        repo_path=repo_path,
        detail_callback=detail_callback,
        language=language,
        cache=cache,
        phase_review_result_cls=PhaseReviewResult,
        tool_phase_input_factory=REVIEW_CONFIG.tool_phase_input_factory,
    )


def run_security_testing_phase(
    *,
    task: Task,
    microtask: Microtask,
    files: Dict[str, str],
    security_agent: Any = None,
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    repo_path: Optional[Path] = None,
    detail_callback: Optional[Callable[[str], None]] = None,
    language: str = "python",
    cache: Optional[AgentReviewCache] = None,
) -> PhaseReviewResult:
    """
    Run security testing phase: vulnerability scanning, security best practices.

    Thin wrapper over the shared parametrised implementation
    (:func:`software_engineering_team.shared.phases.review.run_security_testing_phase_impl`).
    ``_run_security_agent`` is referenced by bare module-global name inside
    ``partial`` at call time so this module stays the test patch surface.

    Preconditions:
        - ``microtask`` exposes ``.id`` / ``.title`` / ``.description``.
    Postconditions:
        - Returns a :class:`PhaseReviewResult`; never raises (shared containment).
    """
    return run_security_testing_phase_impl(
        task=task,
        microtask=microtask,
        files=files,
        review_agent=security_agent,
        agent_runner=partial(_run_security_agent, security_agent=security_agent),
        tool_agents=tool_agents,
        repo_path=repo_path,
        detail_callback=detail_callback,
        language=language,
        cache=cache,
        phase_review_result_cls=PhaseReviewResult,
        tool_phase_input_factory=REVIEW_CONFIG.tool_phase_input_factory,
    )
```

Leave `run_documentation_self_review` and everything above `run_code_review_phase` untouched.

- [ ] **Step 3: Run shared + backend regression tests — expect PASS**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_shared_agent_testing_phase.py \
  agents/software_engineering_team/tests/test_v2_review_phase.py \
  agents/software_engineering_team/tests/test_microtask_review_gates.py \
  -v
```

Expected: all previously-passing tests still pass, including
`test_run_qa_testing_phase_agent_failure_is_contained` and
`test_run_security_testing_phase_agent_failure_is_contained` (they patch
`review_mod._run_qa_agent` / `_run_security_agent` on the backend module).

- [ ] **Step 4: Commit**

```bash
git add backend/agents/software_engineering_team/backend_code_v2_team/phases/review.py
git commit -m "$(cat <<'EOF'
Delegate backend QA/security testing phases to shared impls.

EOF
)"
```

---

### Task 3: Lint + coverage gate

**Files:**
- Verify only (no intentional edits unless ruff/coverage forces a fix)

- [ ] **Step 1: Lint**

```bash
cd backend && make lint
```

Expected: ruff check + format clean for touched files.

- [ ] **Step 2: Coverage on touched modules**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_shared_agent_testing_phase.py \
  agents/software_engineering_team/tests/test_v2_review_phase.py \
  agents/software_engineering_team/tests/test_microtask_review_gates.py \
  --cov=agents/software_engineering_team/shared/phases/review \
  --cov=agents/software_engineering_team/backend_code_v2_team/phases/review \
  --cov-report=term-missing \
  --cov-fail-under=90
```

Expected: ≥90% line coverage on both modules. If a branch is uncovered, extend
`test_shared_agent_testing_phase.py` (prefer covering the tool-agent exception
log-and-skip path and/or detail_callback) rather than adding
`# pragma: no cover`.

- [ ] **Step 3: Full backend test suite (acceptance)**

```bash
cd backend && make test
```

Expected: pass.

- [ ] **Step 4: Commit any coverage/lint fixes** (skip if none)

```bash
git add backend/agents/software_engineering_team/tests/test_shared_agent_testing_phase.py
git commit -m "$(cat <<'EOF'
Raise shared testing-phase coverage for tool-agent failure paths.

EOF
)"
```

---

## Spec coverage check

| Spec requirement | Task |
|---|---|
| Move helper + dataclass + both specs + both dispatchers as one unit | Task 1 |
| Inject `phase_review_result_cls` + `tool_phase_input_factory` | Task 1–2 |
| Shared `*_impl` naming (mirror code-review) | Task 1 |
| Backend thin wrappers; public signatures unchanged | Task 2 |
| `_run_qa_agent` / `_run_security_agent` stay on backend (patch surface) | Task 2 |
| Preserve containment / severities / labels | Task 1 (copy body) + Task 2 regression |
| Existing `test_v2_review_phase` / `test_microtask_review_gates` unchanged | Task 2 Step 3 |
| No frontend entry points | Out of scope (no task) |
| `make test` / `make lint` / 90% coverage | Task 3 |
