# Wire Phase 4 Evidence into Criterion Traces Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop hard-coding `tests=[{"validation": "pass"}]` on every DevOps acceptance-criterion trace; populate traces from Phase 4’s `acceptance_trace`, with honest `tests=[]` when evidence is missing.

**Architecture:** Add a pure module-level mapper `_criterion_traces_from_phase4` in the DevOps orchestrator. Promote `val.acceptance_trace` to `_run_pipeline` scope (same pattern as `quality_gates`). Phase 5 calls the mapper instead of inventing pass evidence.

**Tech Stack:** Python 3.10+, Pydantic models (`CriterionTrace`), pytest.

**Spec:** `docs/superpowers/specs/2026-07-24-acceptance-criterion-tests-trace-design.md`

## Global Constraints

- No schema changes to `CriterionTrace` or `DevOpsCompletionPackage`.
- Never invent a `"pass"` (or any other) validation status.
- Do not mention tracker issue numbers in commit messages, comments, or docs.
- Work only in `.worktrees/fix-2260-acceptance-criterion-tests-trace` on branch `fix/2260-acceptance-criterion-tests-trace`.
- DbC: every new public/module helper docstring must include Preconditions and Postconditions.
- Coverage floor remains 90% on touched code.

## File map

| File | Responsibility |
|------|----------------|
| `backend/agents/software_engineering_team/devops_team/orchestrator.py` | Mapper helper; promote `acceptance_trace`; Phase 5 uses mapper |
| `backend/agents/software_engineering_team/tests/test_devops_team.py` | Unit tests for mapper; happy-path script + integration assertions |

---

### Task 1: Pure mapper helper (TDD)

**Files:**
- Create (add function): `backend/agents/software_engineering_team/devops_team/orchestrator.py` (module-level, after `_git_ops` / near other module helpers — before `DevOpsTeamLeadAgent`)
- Test: `backend/agents/software_engineering_team/tests/test_devops_team.py` (new `TestCriterionTracesFromPhase4` class near other model/helper unit tests, before the large integration class)

**Interfaces:**
- Consumes: `criteria: List[str]`, `acceptance_trace: List[Dict[str, object]]`, `artifact_keys: List[str]`
- Produces: `List[CriterionTrace]` via `_criterion_traces_from_phase4(...)`

- [ ] **Step 1: Write the failing unit tests**

Add this import if not already present (file already imports `CriterionTrace` / models — confirm; add only what is missing):

```python
from software_engineering_team.devops_team.orchestrator import (
    _criterion_traces_from_phase4,
)
```

Add class (place after model tests, before integration):

```python
class TestCriterionTracesFromPhase4:
    """Unit tests for the Phase 4 → CriterionTrace mapper."""

    def test_match_uses_phase4_entry(self) -> None:
        traces = _criterion_traces_from_phase4(
            criteria=["c1", "c2"],
            acceptance_trace=[
                {
                    "criterion": "c1",
                    "implementation_refs": ["infra/main.tf"],
                    "tests": [{"iac_validate": "pass"}],
                }
            ],
            artifact_keys=["infra/main.tf", "deploy/values.yaml"],
        )
        assert len(traces) == 2
        assert traces[0].criterion == "c1"
        assert traces[0].implementation_refs == ["infra/main.tf"]
        assert traces[0].tests == [{"iac_validate": "pass"}]
        assert traces[1].criterion == "c2"
        assert traces[1].tests == []
        assert traces[1].implementation_refs == [
            "deploy/values.yaml",
            "infra/main.tf",
        ]

    def test_no_match_uses_empty_tests_and_artifact_keys(self) -> None:
        traces = _criterion_traces_from_phase4(
            criteria=["lonely"],
            acceptance_trace=[],
            artifact_keys=["a.py"],
        )
        assert traces == [
            CriterionTrace(
                criterion="lonely",
                implementation_refs=["a.py"],
                tests=[],
            )
        ]

    def test_coerces_bad_shapes(self) -> None:
        traces = _criterion_traces_from_phase4(
            criteria=["c1"],
            acceptance_trace=[
                {
                    "criterion": "c1",
                    "implementation_refs": "not-a-list",
                    "tests": [{"ok": 1}, "skip-me", {"gate": True}],
                }
            ],
            artifact_keys=["fallback.py"],
        )
        assert traces[0].implementation_refs == []
        assert traces[0].tests == [{"ok": "1"}, {"gate": "True"}]

    def test_never_invents_validation_pass(self) -> None:
        traces = _criterion_traces_from_phase4(
            criteria=["c1"],
            acceptance_trace=[],
            artifact_keys=[],
        )
        assert traces[0].tests == []
        assert {"validation": "pass"} not in traces[0].tests
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend
PYTHONPATH=. /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py::TestCriterionTracesFromPhase4 -q
```

Expected: FAIL with `ImportError` / `cannot import name '_criterion_traces_from_phase4'`.

- [ ] **Step 3: Implement the mapper**

In `orchestrator.py`, after `_git_ops` (around line 72), add:

```python
def _criterion_traces_from_phase4(
    criteria: List[str],
    acceptance_trace: List[Dict[str, object]],
    artifact_keys: List[str],
) -> List[CriterionTrace]:
    """Map acceptance criteria onto Phase 4 validation evidence.

    Preconditions:
        - ``criteria`` is an iterable of criterion strings (may be empty).
        - ``acceptance_trace`` is an iterable of dict-like Phase 4 entries
          (may be empty); non-dict entries are ignored.
        - ``artifact_keys`` is an iterable of artifact path strings used as
          fallback ``implementation_refs`` when no Phase 4 match exists.

    Postconditions:
        - Returns one ``CriterionTrace`` per entry in ``criteria``, in order.
        - A Phase 4 match (first entry whose ``criterion`` string-equals the
          criterion) supplies coerced ``implementation_refs`` and ``tests``.
        - Unmatched criteria get ``implementation_refs=sorted(artifact_keys)``
          and ``tests=[]``.
        - Never invents a fabricated ``{"validation": "pass"}`` entry.
    """
    by_criterion: Dict[str, Dict[str, object]] = {}
    for entry in acceptance_trace:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("criterion", ""))
        if key and key not in by_criterion:
            by_criterion[key] = entry

    fallback_refs = sorted(artifact_keys)
    traces: List[CriterionTrace] = []
    for criterion in criteria:
        match = by_criterion.get(criterion)
        if match is None:
            traces.append(
                CriterionTrace(
                    criterion=criterion,
                    implementation_refs=list(fallback_refs),
                    tests=[],
                )
            )
            continue

        raw_refs = match.get("implementation_refs", [])
        refs = [str(r) for r in raw_refs] if isinstance(raw_refs, list) else []

        raw_tests = match.get("tests", [])
        tests: List[Dict[str, str]] = []
        if isinstance(raw_tests, list):
            for item in raw_tests:
                if isinstance(item, dict):
                    tests.append({str(k): str(v) for k, v in item.items()})

        traces.append(
            CriterionTrace(
                criterion=criterion,
                implementation_refs=refs,
                tests=tests,
            )
        )
    return traces
```

- [ ] **Step 4: Run unit tests to verify they pass**

```bash
cd backend
PYTHONPATH=. /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py::TestCriterionTracesFromPhase4 -q
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/devops_team/orchestrator.py \
  backend/agents/software_engineering_team/tests/test_devops_team.py
git commit -m "$(cat <<'EOF'
Add Phase 4 acceptance-trace mapper for criterion evidence.

EOF
)"
```

---

### Task 2: Wire mapper into Phase 4/5 and update integration coverage

**Files:**
- Modify: `backend/agents/software_engineering_team/devops_team/orchestrator.py` (`_run_pipeline` shared state ~643–648; `_phase4_validation_review` nonlocal + assign after `val`; Phase 5 `acceptance_criteria_trace` assignment ~956–963)
- Modify: `backend/agents/software_engineering_team/tests/test_devops_team.py` (`_scripted_llm_for_happy_path` validation stub ~163–168; `test_completion_package_has_acceptance_trace` ~1374–1382)

**Interfaces:**
- Consumes: `_criterion_traces_from_phase4` from Task 1; `val.acceptance_trace` from Phase 4
- Produces: `completion.acceptance_criteria_trace` built from real Phase 4 evidence

- [ ] **Step 1: Write the failing integration assertions**

Update `_scripted_llm_for_happy_path` validation response from empty trace to:

```python
            {
                "approved": True,
                "quality_gates": {"iac_validate": "pass", "policy_checks": "pass"},
                "acceptance_trace": [
                    {
                        "criterion": "Pipeline runs tests and scan before deploy",
                        "implementation_refs": ["infra/main.tf"],
                        "tests": [{"iac_validate": "pass"}],
                    }
                ],
                "summary": "validation ok",
            },
```

Replace `test_completion_package_has_acceptance_trace` body with:

```python
    def test_completion_package_has_acceptance_trace(self) -> None:
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec()
        pkg = agent.run(spec)
        assert len(pkg.acceptance_criteria_trace) == len(spec.acceptance_criteria)

        by_criterion = {t.criterion: t for t in pkg.acceptance_criteria_trace}
        matched = by_criterion["Pipeline runs tests and scan before deploy"]
        assert matched.implementation_refs == ["infra/main.tf"]
        assert matched.tests == [{"iac_validate": "pass"}]

        unmatched = by_criterion["Prod deploy requires explicit approval"]
        assert unmatched.tests == []
        assert len(unmatched.implementation_refs) > 0

        for trace in pkg.acceptance_criteria_trace:
            assert {"validation": "pass"} not in trace.tests
```

- [ ] **Step 2: Run the integration test to verify it fails (still hard-coded)**

```bash
cd backend
PYTHONPATH=. /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py::TestDevOpsTeamLeadAgentIntegration::test_completion_package_has_acceptance_trace -q
```

Expected: FAIL — matched criterion still has `tests=[{"validation": "pass"}]` (or `{"validation": "pass"}` is found in traces), not the Phase 4 `iac_validate` evidence.

- [ ] **Step 3: Promote `acceptance_trace` in `_run_pipeline`**

Beside the existing shared Phase outputs (~643–648):

```python
        # Phase outputs shared with Phase 4+ (set by the gated phase callables).
        iac_result: Any = None
        cicd_result: Any = None
        deploy_result: Any = None
        aggregated_artifacts: Dict[str, str] = {}
        quality_gates: Dict[str, str] = {}
        acceptance_trace: List[Dict[str, object]] = []
```

In `_phase4_validation_review`, change the nonlocal line from:

```python
            nonlocal aggregated_artifacts, quality_gates
```

to:

```python
            nonlocal aggregated_artifacts, quality_gates, acceptance_trace
```

Immediately after `val = self.test_validation_agent.run(...)` (and before quality-gates assembly is fine):

```python
            acceptance_trace = list(val.acceptance_trace)
```

- [ ] **Step 4: Replace Phase 5 hard-coded trace assembly**

Replace:

```python
        completion.acceptance_criteria_trace = [
            CriterionTrace(
                criterion=c,
                implementation_refs=sorted(aggregated_artifacts.keys()),
                tests=[{"validation": "pass"}],
            )
            for c in task_spec.acceptance_criteria
        ]
```

with:

```python
        completion.acceptance_criteria_trace = _criterion_traces_from_phase4(
            list(task_spec.acceptance_criteria),
            acceptance_trace,
            list(aggregated_artifacts.keys()),
        )
```

- [ ] **Step 5: Run focused devops acceptance-trace tests**

```bash
cd backend
PYTHONPATH=. /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py \
  -k "acceptance_trace or CriterionTracesFromPhase4 or completion_package_has_acceptance" -q
```

Expected: all selected tests PASS. Also confirm no remaining hard-code:

```bash
rg -n 'tests=\[\{"validation": "pass"\}\]' \
  backend/agents/software_engineering_team/devops_team/orchestrator.py
```

Expected: no matches.

- [ ] **Step 6: Commit**

```bash
git add \
  backend/agents/software_engineering_team/devops_team/orchestrator.py \
  backend/agents/software_engineering_team/tests/test_devops_team.py
git commit -m "$(cat <<'EOF'
Wire Phase 4 acceptance evidence into completion criterion traces.

EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|------------------|------|
| Promote Phase 4 `acceptance_trace` to pipeline scope | Task 2 |
| Map match → Phase 4 entry; no match → artifact keys + `tests=[]` | Task 1 + 2 |
| Coerce bad shapes; never invent pass | Task 1 |
| Helper with DbC docstring | Task 1 |
| Update happy-path script + integration assertions | Task 2 |
| Remove hard-coded `tests=[{"validation": "pass"}]` | Task 2 |
| No schema / QA prompt / doc-agent changes | (non-goals — untouched) |
