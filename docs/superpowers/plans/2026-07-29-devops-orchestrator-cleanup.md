# DevOps Orchestrator Consolidated Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close DbC docstring gaps, contract enforcement, negation-aware legacy env classification, unused `run_workflow` kwargs, and E402 late-import suppressions in `devops_team/orchestrator.py` as one surgical pass.

**Architecture:** Keep all behavioral/docs changes inside the DevOps team lead orchestrator. Prefer hoisting module imports above constants to clear `# noqa: E402`; fall back to lazy imports in `__init__` plus `__getattr__` re-exports only if a circular import appears. Update devops unit tests for the classifier, signature, and contracts. Do not rewrite `quality_gate.py` / `tool_dispatch.py`.

**Tech Stack:** Python 3.10+, pytest, existing `DevOpsTaskSpec` / `DevOpsTeamLeadAgent` APIs

## Global Constraints

- File focus: `backend/agents/software_engineering_team/devops_team/orchestrator.py` + devops tests under `backend/agents/software_engineering_team/tests/`
- Design-by-Contract docstrings required (Preconditions / Postconditions / Invariants) on in-scope methods
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only)
- Ruff line-length 120; keep existing `# noqa` removals intentional
- Coverage: new/changed code ≥ 90%
- Preserve re-export of `MAX_INFRA_FIX_ITERATIONS` and `_DebugPatchState` from `orchestrator` (tests import them there)
- Preserve callable shape `self._run_execution_tools(...)` / `self._debug_patch_once(...)` (plain function stored on class, agent passed as first arg)

## File map

| File | Role |
|---|---|
| `devops_team/orchestrator.py` | All production changes |
| `tests/test_devops_team.py` | Legacy classifier + `run_workflow` signature + optional env-policy tests |
| `tests/test_devops_debug_patch.py` | Must keep importing re-exports; no required logic change if re-exports preserved |
| `tests/test_devops_status_hook.py` | Smoke that agent still constructs |

---

### Task 1: Negation-aware legacy environment classifier (TDD)

**Files:**
- Modify: `backend/agents/software_engineering_team/devops_team/orchestrator.py` (`_build_legacy_spec` ~252–284)
- Test: `backend/agents/software_engineering_team/tests/test_devops_team.py` (`TestBackwardCompatibility`)

**Interfaces:**
- Consumes: existing `_build_legacy_spec(*, task_id, task_description, requirements, target_repo) -> DevOpsTaskSpec`
- Produces: private helper `_legacy_environment_from_text(combined_text: str) -> str` returning `"production"` or `"staging"`

- [ ] **Step 1: Write the failing tests**

Add to `TestBackwardCompatibility` in `test_devops_team.py` (after existing prod/staging/produce tests):

```python
    def test_build_legacy_spec_ignores_non_production(self) -> None:
        spec = DevOpsTeamLeadAgent._build_legacy_spec(
            task_id="devops-neg-1",
            task_description="Target non-production only",
            requirements="Keep staging",
        )
        assert spec.environment == "staging"

    def test_build_legacy_spec_ignores_not_prod(self) -> None:
        spec = DevOpsTeamLeadAgent._build_legacy_spec(
            task_id="devops-neg-2",
            task_description="Do not prod deploy",
            requirements="not prod",
        )
        assert spec.environment == "staging"

    def test_build_legacy_spec_ignores_no_production(self) -> None:
        spec = DevOpsTeamLeadAgent._build_legacy_spec(
            task_id="devops-neg-3",
            task_description="no production traffic",
            requirements="staging only",
        )
        assert spec.environment == "staging"
```

Keep existing `test_build_legacy_spec_prod_detection` and `test_build_legacy_spec_does_not_match_produce_as_prod` green after the change.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py::TestBackwardCompatibility::test_build_legacy_spec_ignores_non_production \
  agents/software_engineering_team/tests/test_devops_team.py::TestBackwardCompatibility::test_build_legacy_spec_ignores_not_prod \
  agents/software_engineering_team/tests/test_devops_team.py::TestBackwardCompatibility::test_build_legacy_spec_ignores_no_production \
  -v
```

Expected: FAIL — current regex treats these as production.

- [ ] **Step 3: Implement classifier helper + wire it**

In `orchestrator.py`, near the legacy defaults (above `_build_legacy_spec`), add:

```python
_NEGATION_TOKENS = frozenset({"not", "no", "non"})


def _legacy_environment_from_text(combined_text: str) -> str:
    """Infer ``production`` vs ``staging`` from legacy free text.

    Preconditions:
        - ``combined_text`` is a str (may be empty); caller lowercases input.
    Postconditions:
        - Returns ``\"production\"`` iff some token equals ``prod`` or ``production``
          (punctuation stripped) and the immediately preceding token is not a
          negation token (``not``, ``no``, ``non``). Hyphenated forms like
          ``non-production`` count as negated (leading ``non`` segment).
        - Otherwise returns ``\"staging\"``. Does not treat ``produce`` as prod.
    """
    assert isinstance(combined_text, str), "combined_text must be a str"
    # Split on whitespace and hyphens so "non-production" -> ["non", "production"].
    raw_tokens = combined_text.replace("-", " ").split()
    tokens = [t.strip(",.!?;:") for t in raw_tokens]
    for i, token in enumerate(tokens):
        if token not in ("prod", "production"):
            continue
        if i > 0 and tokens[i - 1] in _NEGATION_TOKENS:
            continue
        return "production"
    return "staging"
```

Replace the regex line in `_build_legacy_spec` with:

```python
        combined_text = f"{task_description} {requirements}".lower()
        env = _legacy_environment_from_text(combined_text)
```

Remove the now-unused `re` import only if nothing else in the file uses `re` (grep first; if unused, drop `import re`).

- [ ] **Step 4: Run classifier tests to verify they pass**

Run:

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py::TestBackwardCompatibility \
  -v -k "legacy_spec"
```

Expected: PASS for prod, staging, produce, and new negation cases.

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/devops_team/orchestrator.py \
  backend/agents/software_engineering_team/tests/test_devops_team.py
git commit -m "$(cat <<'EOF'
fix: ignore negated production phrases in legacy DevOps env inference

Tokenize legacy free text so non-production / not prod / no production stay staging while explicit prod tokens still map to production.
EOF
)"
```

---

### Task 2: Title constant, DbC docstrings, env-policy asserts, RuntimeError

**Files:**
- Modify: `backend/agents/software_engineering_team/devops_team/orchestrator.py`
- Test: `backend/agents/software_engineering_team/tests/test_devops_team.py`

**Interfaces:**
- Consumes: `_legacy_environment_from_text` from Task 1
- Produces: `MAX_LEGACY_TITLE_LENGTH: int = 120`; strengthened method contracts; `_enforce_env_policy` raises `AssertionError` on bad included items

- [ ] **Step 1: Write failing contract tests**

Add:

```python
    def test_build_legacy_spec_truncates_title_to_constant(self) -> None:
        long_desc = "x" * (MAX_LEGACY_TITLE_LENGTH + 40)
        spec = DevOpsTeamLeadAgent._build_legacy_spec(
            task_id="devops-title",
            task_description=long_desc,
            requirements="staging",
        )
        assert len(spec.title) == MAX_LEGACY_TITLE_LENGTH

    def test_enforce_env_policy_rejects_non_string_included(self) -> None:
        spec = DevOpsTeamLeadAgent._build_legacy_spec(
            task_id="devops-policy",
            task_description="Deploy to production",
            requirements="approval gate and rollback",
        )
        # Bypass model validation by mutating after construction if needed;
        # prefer constructing with a minimal object that has bad included.
        object.__setattr__(spec.scope, "included", [None])  # type: ignore[arg-type]
        with pytest.raises(AssertionError, match="scope.included"):
            DevOpsTeamLeadAgent._enforce_env_policy(spec)
```

Import `MAX_LEGACY_TITLE_LENGTH` from `orchestrator` in the test module once the constant exists (or write the test after adding the constant in step 3 if import fails first). Prefer red-green: add constant stub first if needed.

If `DevOpsTaskSpec` / Pydantic rejects `None` in `included`, instead pass a SimpleNamespace duck type:

```python
    def test_enforce_env_policy_rejects_non_string_included(self) -> None:
        from types import SimpleNamespace

        task_spec = SimpleNamespace(
            platform_scope=SimpleNamespace(environments=["production"]),
            scope=SimpleNamespace(included=[None]),
            rollback_requirements=["Rollback"],
        )
        with pytest.raises(AssertionError, match="scope.included"):
            DevOpsTeamLeadAgent._enforce_env_policy(task_spec)  # type: ignore[arg-type]
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py::TestBackwardCompatibility::test_enforce_env_policy_rejects_non_string_included \
  -v
```

Expected: FAIL (AttributeError or missing AssertionError) until asserts exist.

- [ ] **Step 3: Implement constant + docs + asserts + RuntimeError**

1. Beside legacy defaults:

```python
MAX_LEGACY_TITLE_LENGTH = 120
```

2. Title line:

```python
            title=task_description[:MAX_LEGACY_TITLE_LENGTH] or task_id,
```

3. `__init__` docstring:

```python
    def __init__(self, llm_client: LLMClient) -> None:
        """Initialize the DevOps team lead and its specialist agents/tools.

        Preconditions:
            - ``llm_client`` is non-None.
        Postconditions:
            - All specialist agents and execution/validation tools are
              constructed and bound on ``self``.
            - ``_status_callback`` remains the mixin default (``None``) until
              a caller assigns it for a run.
        """
```

4. `_build_legacy_spec` docstring covering params, `_legacy_environment_from_text`, and module defaults.

5. `_build_subtask_contracts` docstring:

```python
        """Create the IaC, CI/CD, and deployment subtask contracts for a run.

        Preconditions:
            - ``task_spec.task_id`` is a non-empty string.
        Postconditions:
            - Returns exactly three ``SubtaskContract`` objects owned by
              ``InfrastructureAsCodeAgent``, ``CICDPipelineAgent``, and
              ``DeploymentStrategyAgent`` respectively.
        """
```

(Optionally `assert task_spec.task_id` at the top.)

6. Expand `_run_pipeline` docstring: keep the existing narrative, append formal Preconditions / Postconditions / Invariants (does not mutate `task_spec`; success sets `completion_package`; failure sets `failure_reason`).

7. `_report_status` postcondition wording — replace ambiguous “swallows callback errors” with: errors are logged and swallowed by `TeamLeadSharedState._report_status`.

8. `_enforce_env_policy` body start:

```python
        assert task_spec.platform_scope.environments is not None, (
            "task_spec.platform_scope.environments must be set"
        )
        assert hasattr(task_spec.platform_scope.environments, "__iter__"), (
            "task_spec.platform_scope.environments must be iterable"
        )
        assert task_spec.scope.included is not None, "task_spec.scope.included must be set"
        assert all(isinstance(item, str) for item in task_spec.scope.included), (
            "task_spec.scope.included must be an iterable of strings"
        )
```

9. Replace success-path assert:

```python
        if completion is None:
            raise RuntimeError("Phase 5 did not assign a completion package")
```

- [ ] **Step 4: Run related tests**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py::TestBackwardCompatibility \
  agents/software_engineering_team/tests/test_devops_status_hook.py \
  -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/devops_team/orchestrator.py \
  backend/agents/software_engineering_team/tests/test_devops_team.py
git commit -m "$(cat <<'EOF'
docs: harden DevOps orchestrator DbC contracts and runtime checks

Document constructor and pipeline helpers, name the title truncation constant, validate env-policy inputs, and fail hard if Phase 5 omits a completion package even under python -O.
EOF
)"
```

---

### Task 3: Remove unused `run_workflow` parameters

**Files:**
- Modify: `backend/agents/software_engineering_team/devops_team/orchestrator.py` (`run_workflow` ~304–340)
- Modify: `backend/agents/software_engineering_team/tests/test_devops_team.py` (`test_run_workflow_accepts_legacy_args`)

**Interfaces:**
- Consumes: `_build_legacy_spec`, `_run_pipeline`
- Produces: narrowed signature:

```python
def run_workflow(
    self,
    *,
    repo_path: Path,
    task_description: str,
    requirements: str,
    target_repo: Optional[Any] = None,
    build_verifier: Optional[Any] = None,
    task_id: str = "devops",
    subdir: str = "",
) -> DevOpsTeamResult:
```

- [ ] **Step 1: Update the compatibility test (expect TypeError if kwargs remain in call before signature change—or update call + assert signature after)**

Replace `test_run_workflow_accepts_legacy_args` body kwargs with only kept parameters, and add:

```python
    def test_run_workflow_signature_excludes_unused_kwargs(self) -> None:
        import inspect

        params = inspect.signature(DevOpsTeamLeadAgent.run_workflow).parameters
        for name in (
            "architecture",
            "existing_pipeline",
            "tech_stack",
            "max_iterations",
            "devops_review_agent",
        ):
            assert name not in params
```

- [ ] **Step 2: Run signature test (fails while params still present)**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py::TestBackwardCompatibility::test_run_workflow_signature_excludes_unused_kwargs \
  -v
```

Expected: FAIL until params removed.

- [ ] **Step 3: Narrow signature + docstring**

Remove the five unused parameters and the `_ = (...)` discard block. Update docstring to state this is the legacy adapter for repo/task free-text → `_build_legacy_spec` → `_run_pipeline` with `write_changes=True`.

Grep for callers that pass the removed names into DevOps specifically:

```bash
rg -n "architecture=|existing_pipeline=|tech_stack=|max_iterations=|devops_review_agent=" \
  backend/agents/software_engineering_team --glob '*.py'
```

Update only direct DevOps test call sites. Do **not** change `v2_team_worker` keyword probing — it already skips unsupported kwargs.

Note: `api/background.py` / `temporal/activities.py` may pass `architecture=` into a polymorphic `team_lead.run_workflow`. If that path can hit DevOpsTeamLeadAgent, either keep **kwargs forwarding (not preferred) or confirm those call sites use teams that still accept `architecture`. If DevOps is selected with `architecture=` today, change those call sites to only pass kwargs accepted by the lead (mirror `v2_team_worker._accepts_keyword`) **or** keep `**unused` only if a live path breaks. Preferred: grep construction + confirm whether devops team ever receives `architecture` from those modules; update accordingly so tests/integration still pass.

- [ ] **Step 4: Run devops + polymorphic smoke tests**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py::TestBackwardCompatibility \
  agents/software_engineering_team/tests/test_devops_team.py -k "run_workflow" \
  agents/software_engineering_team/tests/test_devops_status_hook.py \
  -v
```

If backend API tests cover devops `run_workflow`, include them. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/devops_team/orchestrator.py \
  backend/agents/software_engineering_team/tests/test_devops_team.py
# include any caller fixes if required
git commit -m "$(cat <<'EOF'
refactor: drop unused DevOps run_workflow compatibility kwargs

Remove architecture/existing_pipeline/tech_stack/max_iterations/devops_review_agent from the legacy adapter so the public signature matches behavior.
EOF
)"
```

---

### Task 4: Clear `# noqa: E402` late imports

**Files:**
- Modify: `backend/agents/software_engineering_team/devops_team/orchestrator.py` (imports ~120–143 and class aliases ~164–172)
- Verify: `backend/agents/software_engineering_team/tests/test_devops_debug_patch.py` (re-export imports)

**Interfaces:**
- Produces: no `# noqa: E402` in this file; module still exports `MAX_INFRA_FIX_ITERATIONS`, `_DebugPatchState`, `DevOpsTeamLeadAgent`

- [ ] **Step 1: Prefer import hoist (no behavior change)**

Move these imports to the top import block (with the other relative imports), **before** module-level constants / `ENV_POLICY`:

- `.tool_dispatch`
- `.infra_debug_agent`, `.infra_patch_agent`
- `.task_clarifier`, `.test_validation_agent`
- `.tool_agents` (all currently late names)
- `.debug_patch` + `MAX_INFRA_FIX_ITERATIONS`, `_DebugPatchState`

Keep `logger = logging.getLogger(__name__)` after imports (or immediately after the early stdlib/third-party block).

Then run:

```bash
cd backend && python -c "from software_engineering_team.devops_team.orchestrator import DevOpsTeamLeadAgent, MAX_INFRA_FIX_ITERATIONS, _DebugPatchState; print('ok', MAX_INFRA_FIX_ITERATIONS)"
```

Expected: `ok` printed. If `ImportError` / circular import:

- [ ] **Step 1b (fallback): lazy imports**

1. Remove late import block and E402 comments.
2. Inside `__init__`, import agent/tool classes locally before constructing them.
3. Replace class-level aliases with methods that preserve the unbound-function call shape:

```python
    def _run_execution_tools(self, repo_str: str, artifacts: Dict[str, str]) -> List[Dict[str, Any]]:
        from . import tool_dispatch

        return tool_dispatch.run_execution_tools(self, repo_str, artifacts)

    def _debug_patch_once(self, *args: Any, **kwargs: Any) -> Any:
        from . import debug_patch

        return debug_patch.debug_patch_once(self, *args, **kwargs)
```

(Match exact `debug_patch_once` signature from `debug_patch.py` when wiring args.)

4. Preserve re-exports via module `__getattr__`:

```python
def __getattr__(name: str) -> Any:
    if name in {"MAX_INFRA_FIX_ITERATIONS", "_DebugPatchState"}:
        from .debug_patch import MAX_INFRA_FIX_ITERATIONS, _DebugPatchState

        mapping = {
            "MAX_INFRA_FIX_ITERATIONS": MAX_INFRA_FIX_ITERATIONS,
            "_DebugPatchState": _DebugPatchState,
        }
        value = mapping[name]
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
```

- [ ] **Step 2: Confirm no E402 left**

```bash
rg "noqa: E402" backend/agents/software_engineering_team/devops_team/orchestrator.py
```

Expected: no matches.

- [ ] **Step 3: Run devops suites that construct the lead / import re-exports**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py \
  agents/software_engineering_team/tests/test_devops_status_hook.py \
  agents/software_engineering_team/tests/test_devops_debug_patch.py \
  -v --tb=short
```

Expected: PASS (or pre-existing failures only — do not proceed with new failures).

- [ ] **Step 4: Commit**

```bash
git add backend/agents/software_engineering_team/devops_team/orchestrator.py
git commit -m "$(cat <<'EOF'
refactor: clear DevOps orchestrator late-import E402 suppressions

Hoist (or lazily bind) agent/tool_dispatch/debug_patch imports so module load order no longer needs noqa: E402 while keeping debug-patch re-exports.
EOF
)"
```

---

### Task 5: Verification closeout

**Files:** none new — verify only

- [ ] **Step 1: Confirm already-resolved findings still hold**

- `tool_gate_map` returned from `phases/quality_gate.py` (not a dead store).
- `_phase4_validation_review` docstring lists `acceptance_trace`.
- `TeamLeadSharedState._report_status` still swallows callback errors; DevOps docstring names that owner.

- [ ] **Step 2: Full devops regression**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_devops_team.py \
  agents/software_engineering_team/tests/test_devops_status_hook.py \
  agents/software_engineering_team/tests/test_devops_debug_patch.py \
  -q
```

Expected: all passed.

- [ ] **Step 3: Lint the touched file**

```bash
cd backend && ruff check agents/software_engineering_team/devops_team/orchestrator.py \
  agents/software_engineering_team/tests/test_devops_team.py
```

Expected: clean.

- [ ] **Step 4: Final commit only if verification produced leftover fixes; otherwise done**

If fixes needed, commit them with a focused message (`fix:` / `test:`).

---

## Spec coverage checklist

| Spec item | Task |
|---|---|
| Negation-aware classifier + tests | Task 1 |
| `MAX_LEGACY_TITLE_LENGTH` | Task 2 |
| DbC docstrings (`__init__`, legacy, contracts, pipeline, status wording) | Task 2 |
| `_enforce_env_policy` asserts | Task 2 |
| Phase 5 `RuntimeError` | Task 2 |
| Remove unused `run_workflow` params | Task 3 |
| Clear E402 / lazy fallback | Task 4 |
| Verify #3403/#3548/#3410 already resolved | Task 5 |
| Out of scope quality_gate/tool_dispatch rewrites | Honored |

## Placeholder / consistency self-review

- No TBD/TODO left in tasks.
- Helper name `_legacy_environment_from_text` used consistently.
- Signature after Task 3 is the single source of truth for callers.
- Re-export preservation required for debug-patch tests.
