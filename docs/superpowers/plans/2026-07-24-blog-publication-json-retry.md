# blog_publication_agent JSON-retry migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate `reject()` and `run_revision_loop()` in `blog_publication_agent` onto `call_json_with_retry()` so JSON-parse failures retry once and soft-fall back instead of raising uncaught.

**Architecture:** Both LLM JSON call sites call the shared helper directly (`max_attempts=2`, soft instruction on attempt 0, strict suffix on retry). `reject()` soft-falls back to `ready_to_revise=True` with empty questions; `run_revision_loop()` soft-falls back to `{"feedback_items": []}` so the existing empty-items synthesis path builds a deterministic `must_fix` from raw rejection text. Transient LLM transport errors re-raise via the helper default.

**Tech Stack:** Python 3.10+, pytest, strands `Agent`, `agents.blogging.shared.json_retry.call_json_with_retry`, DummyLLMClient for unrelated paths.

**Spec:** `docs/superpowers/specs/2026-07-24-blog-publication-json-retry-design.md`

## Global Constraints

- Both call sites must use `call_json_with_retry()`; no direct `extract_json_from_response` remains in this agent.
- Soft fallback on parse exhaustion / unexpected error; transient `LLMRateLimitError` / `LLMTemporaryError` re-raise.
- Existing `blog_publication_agent` tests must keep passing; retarget the `extract_json_from_response` monkeypatch.
- `make lint` clean; 90% line coverage floor holds for touched files.
- Work in the worktree at `.worktrees/issue-2085-migrate-blog-publication-json-retry` on branch `refactor/2085-migrate-blog-publication-json-retry`.
- Never reference GitHub issue numbers in code, comments, or commit messages (PR body only).

## File map

| File | Role |
|---|---|
| `backend/agents/blogging/blog_publication_agent/agent.py` | Production: replace both bare parse sites with `call_json_with_retry` + soft fallbacks |
| `backend/agents/blogging/tests/test_blog_publication_agent.py` | Tests: new parse-exhaustion coverage for both sites; retarget existing monkeypatch |
| `backend/agents/blogging/shared/json_retry.py` | Unchanged (consume only) |

---

### Task 1: `reject()` parse-exhaustion fallback via `call_json_with_retry`

**Files:**
- Modify: `backend/agents/blogging/blog_publication_agent/agent.py`
- Test: `backend/agents/blogging/tests/test_blog_publication_agent.py`

**Interfaces:**
- Consumes: `call_json_with_retry(agent_factory, prompt, *, max_attempts=2, strict_json_suffix=..., on_exhausted=..., on_unexpected_error=..., logger=...) -> dict`
- Produces: `reject()` returns `RejectionResponse` with `ready_to_revise=True` and `questions=[]` when JSON parse is exhausted or an unexpected non-transient error occurs

- [ ] **Step 1: Write the failing test for `reject()` parse exhaustion**

Append to `backend/agents/blogging/tests/test_blog_publication_agent.py`:

```python
def test_reject_json_parse_exhaustion_falls_back_ready_to_revise(
    agent, temp_blog_root, monkeypatch
) -> None:
    """Unparseable follow-up JSON retries then soft-falls back to ready_to_revise."""
    import json

    from agents.blogging.blog_publication_agent import agent as pub_mod

    result = agent.submit_draft(
        SubmitDraftInput(
            draft="# Rejected Post\n\nNeeds work.",
            title="Rejected Post",
        )
    )

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return "not json at all"

    monkeypatch.setattr(pub_mod, "Agent", _Agent)

    rejection = agent.reject(result.submission_id, "The intro is too short.")
    assert rejection.ready_to_revise is True
    assert rejection.questions == []
    assert rejection.submission_id == result.submission_id

    meta_path = temp_blog_root / "pending" / f"{result.submission_id}_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert "The intro is too short." in meta["rejection_feedback"]
```

- [ ] **Step 2: Run test to verify it fails**

Run from worktree `backend/`:

```bash
MAIN=/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams
WT=$MAIN/.worktrees/issue-2085-migrate-blog-publication-json-retry
cd "$WT/backend" && PYTHONPATH="$WT/backend:$WT/backend/agents" \
  "$MAIN/backend/.venv/bin/python" -m pytest \
  agents/blogging/tests/test_blog_publication_agent.py::test_reject_json_parse_exhaustion_falls_back_ready_to_revise -v
```

Expected: FAIL with an uncaught `LLMJsonParseError` (or similar parse failure) — current code does not soft-fall back.

- [ ] **Step 3: Migrate `reject()` onto `call_json_with_retry`**

In `backend/agents/blogging/blog_publication_agent/agent.py`:

1. Keep `from llm_service import extract_json_from_response` until Task 2 (convert site still uses it). Add:

```python
from agents.blogging.shared.json_retry import call_json_with_retry
```

2. Add module-level constants after `logger = logging.getLogger(__name__)`:

```python
_SOFT_JSON_INSTRUCTION = "\n\nRespond with valid JSON only, no markdown fences."

_REJECT_STRICT_JSON_SUFFIX = (
    "\n\nRespond with a single JSON object only (no markdown, no code fence). "
    'Keys: "ready_to_revise" (boolean), "questions" (array of strings), '
    '"feedback_summary" (string).'
)

_CONVERT_STRICT_JSON_SUFFIX = (
    "\n\nRespond with a single JSON object only (no markdown, no code fence). "
    'Keys: "feedback_items" (array of objects with category, severity, location?, '
    "issue, suggestion?)."
)
```

3. Replace the LLM block inside `reject()` (the `Agent(...)` / `agent(prompt)` / `extract_json_from_response` section) with:

```python
        prompt = REJECTION_FOLLOW_UP_PROMPT.format(
            feedback_collected=feedback_collected or "(none yet)",
            latest_feedback=latest_feedback,
        )

        def _agent_factory():
            return Agent(
                model=self._model,
                system_prompt="You help analyze rejection feedback for blog posts.",
            )

        def _reject_fallback(_exc: Exception) -> dict:
            return {
                "ready_to_revise": True,
                "questions": [],
                "feedback_summary": "\n".join(f"- {f}" for f in meta.rejection_feedback),
            }

        data = call_json_with_retry(
            _agent_factory,
            prompt + _SOFT_JSON_INSTRUCTION,
            max_attempts=2,
            strict_json_suffix=_REJECT_STRICT_JSON_SUFFIX,
            on_exhausted=_reject_fallback,
            on_unexpected_error=_reject_fallback,
            logger=logger,
        )
```

Keep the existing post-parse coercion (`ready_to_revise`, `questions`, `feedback_summary`) and `RejectionResponse` construction unchanged. Leave `run_revision_loop()` on `extract_json_from_response` for this task.

- [ ] **Step 4: Run the new test and the full publication suite**

```bash
cd "$WT/backend" && PYTHONPATH="$WT/backend:$WT/backend/agents" \
  "$MAIN/backend/.venv/bin/python" -m pytest \
  agents/blogging/tests/test_blog_publication_agent.py -v
```

Expected: all tests PASS, including `test_reject_json_parse_exhaustion_falls_back_ready_to_revise`.

- [ ] **Step 5: Commit**

```bash
cd "$WT"
git add backend/agents/blogging/blog_publication_agent/agent.py \
  backend/agents/blogging/tests/test_blog_publication_agent.py
git commit -m "$(cat <<'EOF'
Migrate reject() onto shared JSON-retry helper with soft fallback.

EOF
)"
```

---

### Task 2: `run_revision_loop()` convert-site migration + monkeypatch retarget

**Files:**
- Modify: `backend/agents/blogging/blog_publication_agent/agent.py`
- Modify: `backend/agents/blogging/tests/test_blog_publication_agent.py`

**Interfaces:**
- Consumes: same `call_json_with_retry` contract; `_CONVERT_STRICT_JSON_SUFFIX` from Task 1; existing empty-`feedback_items` synthesis block
- Produces: convert-step exhaustion returns `{"feedback_items": []}` so synthesis builds a `must_fix` and the loop still revises

- [ ] **Step 1: Write the failing test for convert-step parse exhaustion**

Append to `backend/agents/blogging/tests/test_blog_publication_agent.py`:

```python
def test_revision_loop_convert_json_parse_exhaustion_synthesizes_must_fix(
    agent, temp_blog_root, monkeypatch
) -> None:
    """Unparseable convert JSON falls back to empty items; raw rejection drives revise."""
    from agents.blogging.blog_copy_editor_agent import BlogCopyEditorAgent, CopyEditorOutput
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_publication_agent import agent as pub_mod
    from agents.blogging.blog_writer_agent import BlogWriterAgent, WriterOutput

    result = agent.submit_draft(
        SubmitDraftInput(
            draft="# Rejected Post\n\nNeeds work.",
            title="Rejected Post",
            audience="developers",
        )
    )
    agent.reject(result.submission_id, "The intro is too short.", force_ready_to_revise=True)

    class _BadConvertAgent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return "not json at all"

    monkeypatch.setattr(pub_mod, "Agent", _BadConvertAgent)

    calls = {"editor": 0, "revise": 0}

    def _fake_editor_run(self, copy_editor_input, **_kw):
        calls["editor"] += 1
        if calls["editor"] == 1:
            return CopyEditorOutput(
                approved=False,
                summary="needs a longer intro",
                feedback_items=[
                    FeedbackItem(
                        category="structure",
                        severity="must_fix",
                        location="intro",
                        issue="Intro is too short",
                        suggestion="Add context",
                    )
                ],
            )
        return CopyEditorOutput(approved=True, summary="looks good", feedback_items=[])

    def _fake_revise(self, revise_input):
        calls["revise"] += 1
        # Prove the synthesised human must_fix reached revise on iteration 0.
        assert any("intro is too short" in item.issue.lower() for item in revise_input.feedback_items)
        return WriterOutput(draft=revise_input.draft + "\n\nRevised.")

    monkeypatch.setattr(BlogCopyEditorAgent, "run", _fake_editor_run)
    monkeypatch.setattr(BlogWriterAgent, "revise", _fake_revise)

    draft_agent = BlogWriterAgent(
        llm_client=DummyLLMClient(),
        writing_style_guide_content="clear",
        brand_spec_content="brand",
    )
    copy_editor_agent = BlogCopyEditorAgent(llm_client=DummyLLMClient())

    revision = agent.run_revision_loop(
        result.submission_id,
        draft_agent=draft_agent,
        copy_editor_agent=copy_editor_agent,
        audience="developers",
    )

    assert calls["revise"] >= 1
    assert revision.iterations_completed >= 1
    assert "Revised." in revision.revised_draft
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd "$WT/backend" && PYTHONPATH="$WT/backend:$WT/backend/agents" \
  "$MAIN/backend/.venv/bin/python" -m pytest \
  agents/blogging/tests/test_blog_publication_agent.py::test_revision_loop_convert_json_parse_exhaustion_synthesizes_must_fix -v
```

Expected: FAIL with uncaught `LLMJsonParseError` from the convert step.

- [ ] **Step 3: Migrate `run_revision_loop()` convert site and drop `extract_json_from_response`**

Replace the convert LLM block in `run_revision_loop()`:

```python
        def _agent_factory():
            return Agent(
                model=self._model,
                system_prompt="You convert rejection feedback into structured editor feedback.",
            )

        def _convert_fallback(_exc: Exception) -> dict:
            return {"feedback_items": []}

        data = call_json_with_retry(
            _agent_factory,
            CONVERT_FEEDBACK_TO_EDITOR_PROMPT.format(feedback=human_feedback_text)
            + _SOFT_JSON_INSTRUCTION,
            max_attempts=2,
            strict_json_suffix=_CONVERT_STRICT_JSON_SUFFIX,
            on_exhausted=_convert_fallback,
            on_unexpected_error=_convert_fallback,
            logger=logger,
        )
```

Remove `from llm_service import extract_json_from_response` entirely (no remaining uses).

- [ ] **Step 4: Retarget the existing monkeypatch**

In `test_revision_loop_stops_after_editor_approval`, replace:

```python
    monkeypatch.setattr(
        "agents.blogging.blog_publication_agent.agent.extract_json_from_response",
        lambda _text: {"feedback_items": []},
    )
```

with:

```python
    monkeypatch.setattr(
        "agents.blogging.blog_publication_agent.agent.call_json_with_retry",
        lambda *_a, **_kw: {"feedback_items": []},
    )
```

- [ ] **Step 5: Run the full publication suite**

```bash
cd "$WT/backend" && PYTHONPATH="$WT/backend:$WT/backend/agents" \
  "$MAIN/backend/.venv/bin/python" -m pytest \
  agents/blogging/tests/test_blog_publication_agent.py -v
```

Expected: all tests PASS (including both new parse-exhaustion tests and the retargeted approval-stop test).

- [ ] **Step 6: Lint + coverage check**

```bash
cd "$WT/backend" && make lint
cd "$WT/backend" && PYTHONPATH="$WT/backend:$WT/backend/agents" \
  "$MAIN/backend/.venv/bin/python" -m pytest \
  agents/blogging/tests/test_blog_publication_agent.py \
  --cov=agents.blogging.blog_publication_agent.agent \
  --cov-report=term-missing -q
```

Expected: lint clean; line coverage for `blog_publication_agent/agent.py` ≥ 90%.

- [ ] **Step 7: Commit**

```bash
cd "$WT"
git add backend/agents/blogging/blog_publication_agent/agent.py \
  backend/agents/blogging/tests/test_blog_publication_agent.py
git commit -m "$(cat <<'EOF'
Migrate revision-loop JSON convert onto shared retry helper.

EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| Both sites use `call_json_with_retry` | Task 1 (`reject`), Task 2 (`run_revision_loop`) |
| Soft fallback `ready_to_revise=True` on reject exhaustion | Task 1 |
| Soft fallback `{"feedback_items": []}` on convert exhaustion | Task 2 |
| Transient errors re-raise (helper default, no local catch) | Task 1 + 2 (no catch added) |
| New tests for both previously-unhandled paths | Task 1 Step 1, Task 2 Step 1 |
| Retarget existing `extract_json_from_response` monkeypatch | Task 2 Step 4 |
| Drop direct `extract_json_from_response` import | Task 2 Step 3 |
| Existing tests pass; lint; 90% coverage | Task 2 Steps 5–6 |
| Out of scope: early-break-on-approval bug | Not in plan |
| Out of scope: helper implementation / other agents | Not in plan |
