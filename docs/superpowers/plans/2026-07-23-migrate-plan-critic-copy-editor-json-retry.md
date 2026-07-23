# Migrate plan-critic / copy-editor onto call_json_with_retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hand-rolled JSON-retry loops in `blog_plan_critic_agent` and `blog_copy_editor_agent` with the shared `call_json_with_retry()` helper, preserving copy-editor `EventLoopException` unwrapping and aligning plan-critic transient errors with re-raise.

**Architecture:** Follow the compliance/fact-check migration pattern: bake the soft JSON instruction into the base prompt, pass an agent-specific `strict_json_suffix`, wire fallbacks via `on_exhausted` / `on_unexpected_error`, and use `unwrap_exception` only for the copy editor. Plan critic builds a fresh Agent per attempt; copy editor reuses one.

**Tech Stack:** Python 3.10+, `agents.blogging.shared.json_retry.call_json_with_retry`, strands `Agent` / `EventLoopException`, pytest, existing blogging test fakes.

## Global Constraints

- Work only in the worktree at `.worktrees/issue-2081-migrate-plan-critic-copy-editor` on branch `refactor/2081-migrate-plan-critic-copy-editor`.
- Do not modify `backend/agents/blogging/shared/json_retry.py`.
- Do not migrate ghost writer, blog writer, or publication agent.
- Never reference GitHub issue numbers in code, comments, commit messages, or docs (PR body only later).
- Every new/changed function keeps DbC docstring sections (`Preconditions:` / `Postconditions:` / `Invariants:` where relevant).
- `make lint` clean; 90% line coverage floor holds for touched agent modules.
- Prefer the main-repo venv for pytest: `/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python`.

## File map

| File | Role |
|---|---|
| `backend/agents/blogging/blog_plan_critic_agent/agent.py` | Remove local retry constants/loop; call shared helper |
| `backend/agents/blogging/blog_copy_editor_agent/agent.py` | Replace `_invoke_editor_llm` loop; pass unwrap hook |
| `backend/agents/blogging/tests/test_blog_plan_critic_agent.py` | Add transient re-raise coverage |
| `backend/agents/blogging/tests/test_blog_copy_editor_agent.py` | Add `EventLoopException` unwrap coverage |
| `docs/superpowers/specs/2026-07-23-migrate-plan-critic-copy-editor-json-retry-design.md` | Spec (already committed; do not change unless behavior diverges) |

---

### Task 1: Plan critic — failing transient test, then migrate

**Files:**
- Modify: `backend/agents/blogging/tests/test_blog_plan_critic_agent.py`
- Modify: `backend/agents/blogging/blog_plan_critic_agent/agent.py`
- Test: `backend/agents/blogging/tests/test_blog_plan_critic_agent.py`

**Interfaces:**
- Consumes: `call_json_with_retry(agent_factory, prompt, *, max_attempts=2, strict_json_suffix=..., fresh_agent_per_attempt=True, on_exhausted=..., on_unexpected_error=..., logger=...) -> dict`
- Produces: `BlogPlanCriticAgent.run(...)` still returns `PlanCriticReport`; transient `LLMRateLimitError` / `LLMTemporaryError` propagate

- [ ] **Step 1: Write the failing transient re-raise test**

Append to `backend/agents/blogging/tests/test_blog_plan_critic_agent.py` (near the other agent unit tests, after `test_critic_parse_failure_falls_back_to_fail`):

```python
@pytest.mark.parametrize("err_cls", [LLMRateLimitError, LLMTemporaryError])
def test_critic_transient_error_reraises(err_cls) -> None:
    """Transient LLM errors propagate so the job runner / Temporal owns retry."""

    class _BoomAgent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise err_cls("transient outage")

    critic = BlogPlanCriticAgent(llm_client=DummyLLMClient())
    with patch("agents.blogging.blog_plan_critic_agent.agent.Agent", _BoomAgent):
        with pytest.raises(err_cls):
            critic.run(
                plan=_minimal_plan(),
                brand_spec_prompt="b",
                writing_guidelines="g",
            )
```

Also add these imports at the top of the test file (keep existing imports; add what is missing):

```python
import pytest

from llm_service import DummyLLMClient, LLMRateLimitError, LLMTemporaryError
```

(`DummyLLMClient` is already imported — extend that import line rather than duplicating.)

- [ ] **Step 2: Run the new test to verify it fails**

Run from `backend/` in the worktree:

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/blogging/tests/test_blog_plan_critic_agent.py::test_critic_transient_error_reraises -v
```

Expected: FAIL — current agent swallows the exception and returns a FAIL fallback report, so `pytest.raises` does not fire.

- [ ] **Step 3: Migrate plan critic onto `call_json_with_retry`**

In `backend/agents/blogging/blog_plan_critic_agent/agent.py`:

1. Add import:

```python
from agents.blogging.shared.json_retry import call_json_with_retry
```

2. Delete module-level `_JSON_RETRY_SUFFIX` and `_MAX_CRITIC_LLM_ATTEMPTS`.

3. Replace the hand-rolled loop inside `run` (the block that initializes `data` / `last_err` and loops attempts) with:

```python
        soft_json_instruction = "\n\nRespond with valid JSON only, no markdown fences."
        strict_json_suffix = (
            "\n\nRespond with a single JSON object only (no markdown, no code fences). "
            'Keys: "status", "approved", "violations", "notes", "rubric_version".'
        )

        def _agent_factory():
            return Agent(model=self._model, system_prompt=PLAN_CRITIC_SYSTEM)

        def _fallback_dict(exc: Exception) -> dict[str, Any]:
            return _fallback_report(str(exc)).model_dump(mode="json")

        data = call_json_with_retry(
            _agent_factory,
            user_prompt + soft_json_instruction,
            max_attempts=2,
            strict_json_suffix=strict_json_suffix,
            fresh_agent_per_attempt=True,
            on_exhausted=_fallback_dict,
            on_unexpected_error=_fallback_dict,
            logger=logger,
        )
        report = self._coerce_report(data)
```

4. Remove the old `if data is None: report = _fallback_report(...) else: report = self._coerce_report(data)` branch — the helper always returns a dict (parse success or fallback hooks).

5. Keep the approved-invariant enforcement and artifact write unchanged after `report` is assigned.

6. Drop unused imports if any (`LLMJsonParseError` / `extract_json_from_response` are no longer needed in this file once the loop is gone). Keep `json`, `Agent`, models, prompts, `write_artifact`.

7. Update the module docstring / `run` docstring only if they claim swallow-all exception behavior; document that transient LLM errors re-raise.

- [ ] **Step 4: Run plan-critic tests**

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/blogging/tests/test_blog_plan_critic_agent.py -q
```

Expected: all PASS (including the new transient test and `test_critic_parse_failure_falls_back_to_fail`).

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/blogging/blog_plan_critic_agent/agent.py \
  backend/agents/blogging/tests/test_blog_plan_critic_agent.py
git commit -m "$(cat <<'EOF'
Migrate blog plan critic onto shared call_json_with_retry helper.

EOF
)"
```

---

### Task 2: Copy editor — unwrap test, then migrate

**Files:**
- Modify: `backend/agents/blogging/tests/test_blog_copy_editor_agent.py`
- Modify: `backend/agents/blogging/blog_copy_editor_agent/agent.py`
- Test: `backend/agents/blogging/tests/test_blog_copy_editor_agent.py`

**Interfaces:**
- Consumes: `call_json_with_retry(..., unwrap_exception=_unwrap, on_exhausted=..., on_unexpected_error=...) -> dict`
- Produces: `_invoke_editor_llm` still returns `Dict[str, Any]`; `EventLoopException` with transient cause re-raises the unwrapped cause

- [ ] **Step 1: Write the EventLoopException unwrap test**

Append near `test_copy_editor_transient_error_reraises` in `backend/agents/blogging/tests/test_blog_copy_editor_agent.py`:

```python
@pytest.mark.parametrize("kind", ["rate_limit", "temporary"])
def test_copy_editor_event_loop_exception_unwraps_transient(monkeypatch, kind) -> None:
    """strands EventLoopException must re-raise the unwrapped transient cause."""
    from agents.blogging.blog_copy_editor_agent import agent as ce_mod
    from strands.types.exceptions import EventLoopException

    from llm_service import LLMRateLimitError, LLMTemporaryError

    err_cls = LLMRateLimitError if kind == "rate_limit" else LLMTemporaryError
    cause = err_cls("transient outage")

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise EventLoopException(cause)

    monkeypatch.setattr(ce_mod, "Agent", _Agent)
    agent = BlogCopyEditorAgent(
        llm_client=DummyLLMClient(), writing_style_guide_content="", brand_spec_content=""
    )
    with pytest.raises(err_cls) as exc_info:
        agent.run(CopyEditorInput(draft="# d\n\nsome body text here"))
    assert exc_info.value is cause
```

- [ ] **Step 2: Run the new test (should pass on current code)**

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/blogging/tests/test_blog_copy_editor_agent.py::test_copy_editor_event_loop_exception_unwraps_transient -v
```

Expected: PASS — current hand-rolled loop already unwraps. This locks the acceptance criterion before the migration.

- [ ] **Step 3: Migrate copy editor `_invoke_editor_llm` onto the helper**

In `backend/agents/blogging/blog_copy_editor_agent/agent.py`:

1. Add:

```python
from agents.blogging.shared.json_retry import call_json_with_retry
```

2. Delete `_MAX_JSON_PARSE_ATTEMPTS`.

3. Replace the body of `_invoke_editor_llm` after the `on_llm_request` callback with:

```python
        soft_json_instruction = "\n\nRespond with valid JSON only, no markdown fences."
        strict_json_suffix = (
            "\n\nRespond with a single JSON object only (no markdown, no code fence). "
            "Keys: approved (boolean), summary (string), feedback_items (array of objects with "
            "category, severity, location?, issue, suggestion?)."
        )

        def _agent_factory():
            return Agent(model=self._model, system_prompt=COPY_EDITOR_PROMPT)

        def _unwrap(exc: Exception) -> Exception:
            return exc.original_exception if isinstance(exc, EventLoopException) else exc

        return call_json_with_retry(
            _agent_factory,
            prompt + soft_json_instruction,
            max_attempts=2,
            strict_json_suffix=strict_json_suffix,
            unwrap_exception=_unwrap,
            on_exhausted=lambda e: _fallback_editor_data(
                "Copy editor could not parse the model response. Please review the draft manually."
            ),
            on_unexpected_error=lambda e: _fallback_editor_data(
                "Copy editor could not complete review. Please review the draft manually."
            ),
            logger=logger,
        )
```

4. Remove the old local loop, the trailing `if not data:` guard, and unused imports (`LLMJsonParseError`, `extract_json_from_response`). Keep `EventLoopException`, `LLMRateLimitError` / `LLMTemporaryError` only if still referenced in docstrings — if not referenced in code, remove those two imports too (docstrings can name them as plain text).

5. Keep the `_invoke_editor_llm` docstring contract (transient re-raise unwrapped; fallback on JSON exhaustion / unexpected error).

- [ ] **Step 4: Run copy-editor tests**

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/blogging/tests/test_blog_copy_editor_agent.py \
  agents/blogging/tests/test_copy_editor_helpers.py \
  agents/blogging/tests/test_copy_editor_length.py -q
```

Expected: all PASS, including `test_copy_editor_event_loop_exception_unwraps_transient` and existing transient / fallback tests.

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/blogging/blog_copy_editor_agent/agent.py \
  backend/agents/blogging/tests/test_blog_copy_editor_agent.py
git commit -m "$(cat <<'EOF'
Migrate blog copy editor onto shared call_json_with_retry helper.

EOF
)"
```

---

### Task 3: Lint + coverage gate

**Files:**
- Verify only (no intentional code changes unless lint/coverage forces a fix)
- Touched: the four files from Tasks 1–2

**Interfaces:**
- Consumes: Task 1–2 migrations complete on the branch
- Produces: lint-clean tree; coverage ≥ 90% on the two agent modules

- [ ] **Step 1: Run ruff on touched paths**

From `backend/` in the worktree:

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m ruff check \
  agents/blogging/blog_plan_critic_agent/agent.py \
  agents/blogging/blog_copy_editor_agent/agent.py \
  agents/blogging/tests/test_blog_plan_critic_agent.py \
  agents/blogging/tests/test_blog_copy_editor_agent.py
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m ruff format --check \
  agents/blogging/blog_plan_critic_agent/agent.py \
  agents/blogging/blog_copy_editor_agent/agent.py \
  agents/blogging/tests/test_blog_plan_critic_agent.py \
  agents/blogging/tests/test_blog_copy_editor_agent.py
```

Expected: no issues. If format check fails, run `ruff format` on those paths and amend only if the previous commit was yours and unpushed — otherwise make a new commit.

- [ ] **Step 2: Coverage on the two agent modules**

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/blogging/tests/test_blog_plan_critic_agent.py \
  agents/blogging/tests/test_blog_copy_editor_agent.py \
  agents/blogging/tests/test_json_retry.py \
  --cov=agents.blogging.blog_plan_critic_agent.agent \
  --cov=agents.blogging.blog_copy_editor_agent.agent \
  --cov-report=term-missing \
  --cov-fail-under=90 -q
```

Expected: PASS with line coverage ≥ 90% for both modules. If a branch is uncovered solely because the helper now owns it, that is fine; if agent-local fallback/unwrap lines are missing, extend tests rather than lowering the floor.

- [ ] **Step 3: Full related suite smoke**

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/blogging/tests/test_json_retry.py \
  agents/blogging/tests/test_blog_plan_critic_agent.py \
  agents/blogging/tests/test_blog_copy_editor_agent.py \
  agents/blogging/tests/test_compliance.py \
  agents/blogging/tests/test_fact_check.py -q
```

Expected: all PASS.

- [ ] **Step 4: Commit any lint/coverage fixes** (skip if working tree clean)

```bash
git add -u backend/agents/blogging
git commit -m "$(cat <<'EOF'
Fix lint and coverage after JSON-retry migration for critic and copy editor.

EOF
)"
```

---

## Spec coverage self-check

| Spec requirement | Task |
|---|---|
| Both call sites use `call_json_with_retry()` | Task 1 Step 3, Task 2 Step 3 |
| Plan-critic local retry constant / suffix removed | Task 1 Step 3 |
| Copy-editor `EventLoopException` unwrap preserved + tested | Task 2 Steps 1–4 |
| Existing tests pass | Task 1 Step 4, Task 2 Step 4, Task 3 Step 3 |
| Plan-critic re-raises transient errors | Task 1 Steps 1–4 |
| Lint clean; 90% coverage | Task 3 |
| No helper / other-agent changes | Global Constraints |

## Placeholder / consistency scan

- No TBD/TODO placeholders.
- Helper kwargs match `json_retry.call_json_with_retry` signature (`unwrap_exception`, `fresh_agent_per_attempt`, `on_exhausted`, `on_unexpected_error`).
- Fallback messages for copy editor match pre-migration strings so existing assertions on `"manually"` keep working.
