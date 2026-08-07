# Remove code_review Dummy Temporal Disable + Convert Hermetic run() Tests

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the `LLM_PROVIDER=dummy` Temporal-disable branch from `code_review_temporal_enabled()` and convert former hermetic `CodeReviewAgent.run()` unit tests to `force_in_process=True` so CI stays green without a live Temporal server.

**Architecture:** After the gate no longer inspects `LLM_PROVIDER`, Temporal is on whenever an address resolves. Coordinator-intent unit tests opt into the in-process path via the existing `force_in_process=True` constructor flag. Temporal-intent tests keep mocking `execute_code_review_workflow_sync`.

**Tech Stack:** Python 3.10, pytest, `code_review_agent.temporal.config`, `CodeReviewAgent(force_in_process=...)`.

**Spec:** `docs/superpowers/specs/2026-08-07-remove-code-review-dummy-temporal-fallback-design.md`

**Worktree:** `.worktrees/4002-remove-dummy-temporal-fallback` on branch `feature/4002-remove-dummy-temporal-fallback`

## Global Constraints

- Do not remove `CODE_REVIEW_TEMPORAL_FORCE`.
- Do not change `LLM_PROVIDER=dummy` LLM-client harness behavior in conftest (it may remain for LLM selection only).
- Do not add new `WorkflowEnvironment` / integration tests in this PR.
- Do not reference GitHub issue numbers in code, comments, commit messages, or docs (PR body may use `Closes #N`).
- Every public function touched must keep explicit Preconditions/Postconditions/Invariants (Design by Contract).
- Prefer exact, minimal diffs; no drive-by refactors.
- Default (non-integration) CI must not require a live Temporal server.
- PR must note integration-suite runtime is unchanged.

## File Structure

| File | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/code_review_agent/temporal/config.py` | Delete `_dummy_harness`; simplify enablement gate; update docstrings |
| `backend/agents/software_engineering_team/code_review_agent/agent.py` | Fix adjacent comment listing dummy/pytest as disable reasons |
| `backend/agents/software_engineering_team/tests/test_code_review_temporal.py` | Gate + dispatch test updates |
| `backend/agents/software_engineering_team/tests/test_code_review_agent.py` | `force_in_process=True` on coordinator-intent agents |
| `backend/agents/software_engineering_team/tests/test_code_review_e2e.py` | same |
| `backend/agents/software_engineering_team/tests/test_code_review_line_threading.py` | same |
| `backend/agents/software_engineering_team/tests/test_code_review_coordinator.py` | agent.run sites only |
| `backend/agents/software_engineering_team/tests/test_review_profiles.py` | same |
| `docs/ENV_VARS.md` | Drop dummy from code-review Temporal disable docs |

Python for tests (prefer worktree venv if present, else main repo):

`/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python`

Work from worktree root; run pytest with cwd `backend/`.

---

### Task 1: Gate contract tests + remove dummy production branch

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_code_review_temporal.py` (enablement section)
- Modify: `backend/agents/software_engineering_team/code_review_agent/temporal/config.py`
- Modify: `backend/agents/software_engineering_team/code_review_agent/agent.py` (one comment)
- Test: enablement-gate tests in `test_code_review_temporal.py`

**Interfaces:**
- Consumes: `code_review_temporal_enabled() -> bool`, `_clear_env`, `CODE_REVIEW_TEMPORAL_FORCE`, address sentinels
- Produces: Gate with no `_dummy_harness` / no `LLM_PROVIDER` inspect; force + address-only enablement

- [ ] **Step 1: Replace dummy-disable contract with address-only disable (TDD — fails until production change if still asserting dummy)**

Delete `test_dummy_harness_disables` entirely.

Add this test in its place (same enablement section):

```python
def test_dummy_provider_does_not_disable_temporal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # LLM_PROVIDER=dummy selects the no-LLM harness only; it must not force
    # the code-review Temporal gate off when an address resolves.
    _clear_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    assert cfg.code_review_temporal_enabled() is True
```

- [ ] **Step 2: Run the new test to verify it fails under current production code**

From `backend/`:

```bash
../.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_code_review_temporal.py::test_dummy_provider_does_not_disable_temporal \
  -v
```

If worktree has no sibling `.venv`, use:

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_code_review_temporal.py::test_dummy_provider_does_not_disable_temporal \
  -v
```

Expected: FAIL — `assert False is True` (old `_dummy_harness` still returns False).

- [ ] **Step 3: Remove `_dummy_harness` and update production docstrings/comments**

In `temporal/config.py`:

1. Module docstring — change the disable sentence to address-sentinels only:

```python
# Setting ``TEMPORAL_ADDRESS`` to an empty /
# ``disabled`` / ``none`` / ``off`` value falls the agent back to the
# in-process thread-mode coordinator.
```

(Keep surrounding sentences about default `temporal:7233` and no per-agent address override.)

2. Update `_force_enabled` docstring:

```python
def _force_enabled() -> bool:
    """Test hook: ``CODE_REVIEW_TEMPORAL_FORCE`` in a truthy spelling.

    Lets an integration test opt into Temporal mode when an address still
    resolves. Never load-bearing outside tests.
    """
    return os.environ.get("CODE_REVIEW_TEMPORAL_FORCE", "").strip().lower() in _TRUE_VALUES
```

3. Delete `_dummy_harness` entirely.

4. Replace `code_review_temporal_enabled` with this exact body (keep
   `_force_enabled` referenced so the FORCE env path stays live and ruff-clean;
   both branches apply the same address-only rule):

```python
def code_review_temporal_enabled() -> bool:
    """Whether ``CodeReviewAgent.run`` should dispatch to Temporal by default.

    Postconditions:
        - Returns ``True`` iff a Temporal address resolves.
        - Never returns ``True`` when :func:`resolve_code_review_temporal_address`
          is ``None``.
        - Never raises.
        - Never inspects ``sys.modules`` or ``LLM_PROVIDER``.
    """
    if _force_enabled():
        return resolve_code_review_temporal_address() is not None
    return resolve_code_review_temporal_address() is not None
```

Existing force tests stay valid: with a resolved address they assert True;
with a disable-sentinel address they assert False.

In `agent.py` near the Temporal dispatch comment, change:

```python
        # explicitly disabled (sentinel / dummy / pytest), force_in_process is set
```

to:

```python
        # explicitly disabled (address sentinel), force_in_process is set
```

- [ ] **Step 4: Run enablement-gate tests**

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_code_review_temporal.py \
  -k "resolve_defaults or resolve_honours or disable_sentinels or enabled_is_true_under_pytest or force_flag or dummy_provider_does_not_disable" \
  -v
```

Expected: all selected PASS. Confirm `test_dummy_harness_disables` is gone (not collected).

Grep:

```bash
rg -n '_dummy_harness|LLM_PROVIDER' \
  agents/software_engineering_team/code_review_agent/temporal/config.py
```

Expected: no `_dummy_harness`; no `LLM_PROVIDER` in that file.

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/code_review_agent/temporal/config.py \
  backend/agents/software_engineering_team/code_review_agent/agent.py \
  backend/agents/software_engineering_team/tests/test_code_review_temporal.py
git commit -m "$(cat <<'EOF'
Remove dummy-provider Temporal disable from code-review enablement.

EOF
)"
```

---

### Task 2: Convert coordinator-intent tests in test_code_review_temporal.py

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_code_review_temporal.py` (dispatch section ~909–966)
- Test: same file, coordinator-intent `run()` tests

**Interfaces:**
- Consumes: `CodeReviewAgent(..., force_in_process: bool = False)`
- Produces: Coordinator-path tests that do not rely on dummy/gate-off

- [ ] **Step 1: Rewrite `test_run_uses_coordinator_when_temporal_disabled`**

Replace:

```python
def test_run_uses_coordinator_when_temporal_disabled() -> None:
    # Under pytest the gate is off, so run() must go through the coordinator.
    assert _code_review_temporal_enabled() is False
    out = CodeReviewAgent(llm_client=DummyLLMClient()).run(_input())
    assert isinstance(out, CodeReviewOutput)
    assert out.approved is True
```

with:

```python
def test_run_uses_coordinator_when_force_in_process() -> None:
    # force_in_process bypasses Temporal even when the gate would enable it.
    out = CodeReviewAgent(llm_client=DummyLLMClient(), force_in_process=True).run(_input())
    assert isinstance(out, CodeReviewOutput)
    assert out.approved is True
```

- [ ] **Step 2: Add `force_in_process=True` to coordinator-intent helpers**

In the same file, update these constructions:

```python
# test_run_rebuilds_reader_from_repo_root_when_no_live_reader
CodeReviewAgent(llm_client=DummyLLMClient(), force_in_process=True).run(_input(repo_root=str(tmp_path)))

# test_run_prefers_live_reader_over_repo_root
CodeReviewAgent(llm_client=DummyLLMClient(), force_in_process=True).run(
    _input(repo_root=str(tmp_path)),
    repo_reader=sentinel,  # type: ignore[arg-type]
)

# test_run_passes_none_reader_without_repo_root
CodeReviewAgent(llm_client=DummyLLMClient(), force_in_process=True).run(_input())
```

Do **not** add `force_in_process=True` to Temporal-path tests that mock `execute_code_review_workflow_sync` or assert Temporal dispatch.

Optional cleanup: where Temporal-path tests patch `_code_review_temporal_enabled` to `lambda: True` and the default gate is already True with cleared/default env, leave the patches in place for this task (YAGNI) unless they break.

- [ ] **Step 3: Run dispatch-section tests**

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_code_review_temporal.py \
  -k "run_uses_coordinator or rebuilds_reader or prefers_live_reader or passes_none_reader or force_in_process_skips or dispatches_to_temporal" \
  -v
```

Expected: all selected PASS.

- [ ] **Step 4: Commit**

```bash
git add backend/agents/software_engineering_team/tests/test_code_review_temporal.py
git commit -m "$(cat <<'EOF'
Opt code-review Temporal dispatch tests into force_in_process coordinator path.

EOF
)"
```

---

### Task 3: Convert hermetic CodeReviewAgent.run() suites

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_code_review_agent.py`
- Modify: `backend/agents/software_engineering_team/tests/test_code_review_e2e.py`
- Modify: `backend/agents/software_engineering_team/tests/test_code_review_line_threading.py`
- Modify: `backend/agents/software_engineering_team/tests/test_code_review_coordinator.py` (only the two `CodeReviewAgent(` + `.run(` sites ~645 and ~3279)
- Modify: `backend/agents/software_engineering_team/tests/test_review_profiles.py`
- Test: those five files

**Interfaces:**
- Consumes: `CodeReviewAgent(llm_client=..., force_in_process=True)`
- Produces: Green hermetic coordinator suites under unconditional Temporal gate

- [ ] **Step 1: Add `force_in_process=True` to every coordinator-intent construction**

Rules:
- Single-arg form `CodeReviewAgent(llm_client=X)` → `CodeReviewAgent(llm_client=X, force_in_process=True)`
- Multi-line `CodeReviewAgent(` → add `force_in_process=True` as a keyword argument on the constructor call
- Positional `CodeReviewAgent(probe)` → `CodeReviewAgent(probe, force_in_process=True)`
- Chained `CodeReviewAgent(...).run(` → put `force_in_process=True` inside the constructor

Exact sites:

`test_code_review_agent.py` — every `CodeReviewAgent(` that later `.run(`s (all current sites in that file are coordinator-intent).

`test_code_review_e2e.py`:

```python
CodeReviewAgent(llm_client=client, force_in_process=True).run(
CodeReviewAgent(llm_client=client, force_in_process=True).run(
CodeReviewAgent(llm_client=_FailOneFile(marker), force_in_process=True).run(
```

`test_code_review_line_threading.py`:

```python
agent = CodeReviewAgent(llm_client=client, force_in_process=True)
```

(three sites)

`test_code_review_coordinator.py`:

```python
agent = CodeReviewAgent(llm_client=client, force_in_process=True)
```

(two sites ~645 and ~3279 only — do not touch unrelated `reviewer.run()` commentary)

`test_review_profiles.py`:

```python
CodeReviewAgent(probe, force_in_process=True).run(...)
CodeReviewAgent(_IssueProbe(), force_in_process=True).run(...)
```

(all four/five agent constructions)

- [ ] **Step 2: Run the five suites**

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_code_review_agent.py \
  agents/software_engineering_team/tests/test_code_review_e2e.py \
  agents/software_engineering_team/tests/test_code_review_line_threading.py \
  agents/software_engineering_team/tests/test_code_review_coordinator.py \
  agents/software_engineering_team/tests/test_review_profiles.py \
  -v --tb=line
```

Expected: all PASS. If any fail because Temporal was dialed, the missing `force_in_process=True` site must be fixed before commit.

- [ ] **Step 3: Commit**

```bash
git add \
  backend/agents/software_engineering_team/tests/test_code_review_agent.py \
  backend/agents/software_engineering_team/tests/test_code_review_e2e.py \
  backend/agents/software_engineering_team/tests/test_code_review_line_threading.py \
  backend/agents/software_engineering_team/tests/test_code_review_coordinator.py \
  backend/agents/software_engineering_team/tests/test_review_profiles.py
git commit -m "$(cat <<'EOF'
Keep code-review unit suites in-process via force_in_process.

EOF
)"
```

---

### Task 4: ENV_VARS docs + final verification

**Files:**
- Modify: `docs/ENV_VARS.md`
- Test: smoke enablement + one hermetic file

**Interfaces:**
- Consumes: Post-Task-1 address-only gate
- Produces: Docs matching gate; PR-ready verification notes

- [ ] **Step 1: Update TEMPORAL_ADDRESS code-review paragraph**

Change:

```markdown
Setting `TEMPORAL_ADDRESS` to an empty /
`disabled` / `none` / `off` / `0` / `false` / `no` value, or selecting
`LLM_PROVIDER=dummy`, falls back to thread mode.
```

to:

```markdown
Setting `TEMPORAL_ADDRESS` to an empty /
`disabled` / `none` / `off` / `0` / `false` / `no` value falls back to
thread mode.
```

- [ ] **Step 2: Update CODE_REVIEW_TEMPORAL_FORCE paragraph**

Change:

```markdown
### CODE_REVIEW_TEMPORAL_FORCE
Test-only escape hatch (truthy: `1`/`true`/`yes`/`on`) that re-enables code-review
Temporal mode despite the `dummy` guard, provided an address still resolves. Not
load-bearing outside integration tests.
```

to:

```markdown
### CODE_REVIEW_TEMPORAL_FORCE
Test-only escape hatch (truthy: `1`/`true`/`yes`/`on`). Retained for tests that
set the flag explicitly; enablement remains address-only (a disable-sentinel
`TEMPORAL_ADDRESS` still yields thread mode). Not load-bearing outside tests.
```

- [ ] **Step 3: Final greps + smoke tests**

```bash
rg -n '_dummy_harness|"LLM_PROVIDER".*dummy|dummy.*Temporal' \
  agents/software_engineering_team/code_review_agent/ \
  --glob '!**/docs/**'

rg -n 'LLM_PROVIDER=dummy' docs/ENV_VARS.md
```

First grep: no production Temporal-disable hits (agent comment must not reintroduce dummy). Second: `LLM_PROVIDER=dummy` may still appear in general LLM docs; must **not** appear in the code-review TEMPORAL_ADDRESS fallback sentence.

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_code_review_temporal.py \
  -k "dummy_provider_does_not_disable or force_flag or enabled_is_true_under_pytest or run_uses_coordinator" \
  -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add docs/ENV_VARS.md
git commit -m "$(cat <<'EOF'
Drop dummy from code-review Temporal env-var docs.

EOF
)"
```

---

## Self-Review

1. **Spec coverage:** Dummy branch deletion → Task 1. Production-path confirmation greps → Tasks 1+4. force_in_process conversion → Tasks 2–3. ENV_VARS → Task 4. No new WorkflowEnvironment → Global Constraints. Integration runtime note → for PR body at finish time.
2. **Placeholder scan:** No TBD; exact code and commands included. Task 1 locks the two-branch `_force_enabled` form.
3. **Type consistency:** `force_in_process: bool = False` on `CodeReviewAgent.__init__`; `code_review_temporal_enabled() -> bool`.
