# Migrate blog_writer_agent JSON fallbacks onto call_json_with_retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route the three revise-path JSON fallbacks in `blog_writer_agent` through a private `_fallback_draft_via_json` helper that calls shared `call_json_with_retry()`, while keeping the primary `---DRAFT---` text+marker path unchanged.

**Architecture:** Add `_fallback_draft_via_json(prompt) -> Optional[str]` on `BlogWriterAgent`. It builds a JSON-mode strands `Agent` factory, calls `call_json_with_retry` with `max_attempts=2`, soft JSON on the base prompt, a strict suffix requiring `{"draft": "..."}`, and `on_exhausted` / `on_unexpected_error` returning `{}`. Each of the three revise sites replaces its `_call_agent_json` try/except with this helper. Draft `run`, revision-plan JSON, and guideline analysis stay on `_call_agent_json`.

**Tech Stack:** Python 3.10+, `agents.blogging.shared.json_retry.call_json_with_retry`, strands `Agent`, pytest, existing blogging writer test helpers.

## Global Constraints

- Work only in the worktree at `.worktrees/issue-2084-migrate-blog-writer-json-retry` on branch `refactor/2084-migrate-blog-writer-json-retry`.
- Do not modify `backend/agents/blogging/shared/json_retry.py`.
- Do not migrate ghost-writer, publication, plan-critic, or copy-editor.
- Do not replace the text+marker primary path with pure JSON.
- Do not migrate `_call_agent_json` usages in `run`, `_generate_revision_plan`, or `analyze_user_feedback_for_guideline_updates`.
- Never reference GitHub issue numbers in code, comments, commit messages, or docs (PR body only later).
- Every new/changed function keeps DbC docstring sections (`Preconditions:` / `Postconditions:`).
- `make lint` clean; 90% line coverage floor holds for touched files.
- Prefer the main-repo venv for pytest: `/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python`.

## File map

| File | Role |
|---|---|
| `backend/agents/blogging/blog_writer_agent/agent.py` | Add `_fallback_draft_via_json`; wire three revise fallback sites |
| `backend/agents/blogging/tests/test_writer_run.py` | Update `_revise_single_item` fallback tests; add helper unit tests |
| `backend/agents/blogging/tests/test_writer_interactive.py` | Update batch-revise and user-feedback fallback tests |
| `docs/superpowers/specs/2026-07-23-migrate-blog-writer-json-retry-design.md` | Spec (already committed; do not change unless behavior diverges) |

---

### Task 1: `_fallback_draft_via_json` — failing tests, then implement

**Files:**
- Modify: `backend/agents/blogging/tests/test_writer_run.py`
- Modify: `backend/agents/blogging/blog_writer_agent/agent.py`
- Test: `backend/agents/blogging/tests/test_writer_run.py`

**Interfaces:**
- Consumes: `call_json_with_retry(agent_factory, prompt, *, max_attempts=2, strict_json_suffix=..., on_exhausted=..., on_unexpected_error=..., logger=...) -> dict`
- Produces: `BlogWriterAgent._fallback_draft_via_json(self, prompt: str) -> Optional[str]`

- [ ] **Step 1: Write failing unit tests for the new helper**

Append to `backend/agents/blogging/tests/test_writer_run.py` (after the existing `_revise_single_item` tests is fine):

```python
def test_fallback_draft_via_json_success(monkeypatch) -> None:
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _agent()
    captured: dict = {}

    def fake_retry(factory, prompt, **kwargs):
        captured["max_attempts"] = kwargs.get("max_attempts")
        captured["prompt"] = prompt
        captured["strict"] = kwargs.get("strict_json_suffix", "")
        assert callable(factory)
        assert callable(kwargs.get("on_exhausted"))
        assert callable(kwargs.get("on_unexpected_error"))
        return {"draft": "  # From JSON  \n"}

    monkeypatch.setattr(
        "agents.blogging.blog_writer_agent.agent.call_json_with_retry",
        fake_retry,
    )
    out = a._fallback_draft_via_json("revise this draft")
    assert out == "# From JSON"
    assert captured["max_attempts"] == 2
    assert "Respond with valid JSON only" in captured["prompt"]
    assert "draft" in captured["strict"].lower()


def test_fallback_draft_via_json_empty_draft_returns_none(monkeypatch) -> None:
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _agent()
    monkeypatch.setattr(
        "agents.blogging.blog_writer_agent.agent.call_json_with_retry",
        lambda *a, **k: {"draft": "   "},
    )
    assert a._fallback_draft_via_json("prompt") is None


def test_fallback_draft_via_json_missing_draft_returns_none(monkeypatch) -> None:
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _agent()
    monkeypatch.setattr(
        "agents.blogging.blog_writer_agent.agent.call_json_with_retry",
        lambda *a, **k: {},
    )
    assert a._fallback_draft_via_json("prompt") is None


def test_fallback_draft_via_json_exhausted_hook_returns_none(monkeypatch) -> None:
    """on_exhausted returning {} must yield None (keep original draft at call sites)."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from llm_service import LLMJsonParseError

    a = _agent()

    def fake_retry(factory, prompt, **kwargs):
        return kwargs["on_exhausted"](LLMJsonParseError("bad json"))

    monkeypatch.setattr(
        "agents.blogging.blog_writer_agent.agent.call_json_with_retry",
        fake_retry,
    )
    assert a._fallback_draft_via_json("prompt") is None


def test_fallback_draft_via_json_unexpected_hook_returns_none(monkeypatch) -> None:
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _agent()

    def fake_retry(factory, prompt, **kwargs):
        return kwargs["on_unexpected_error"](RuntimeError("boom"))

    monkeypatch.setattr(
        "agents.blogging.blog_writer_agent.agent.call_json_with_retry",
        fake_retry,
    )
    assert a._fallback_draft_via_json("prompt") is None


def test_fallback_draft_via_json_transient_reraises(monkeypatch) -> None:
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from llm_service import LLMRateLimitError

    a = _agent()

    def fake_retry(factory, prompt, **kwargs):
        raise LLMRateLimitError("rate limited")

    monkeypatch.setattr(
        "agents.blogging.blog_writer_agent.agent.call_json_with_retry",
        fake_retry,
    )
    import pytest

    with pytest.raises(LLMRateLimitError):
        a._fallback_draft_via_json("prompt")
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run from the worktree `backend/` directory:

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/issue-2084-migrate-blog-writer-json-retry/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/blogging/tests/test_writer_run.py::test_fallback_draft_via_json_success \
  agents/blogging/tests/test_writer_run.py::test_fallback_draft_via_json_empty_draft_returns_none \
  agents/blogging/tests/test_writer_run.py::test_fallback_draft_via_json_missing_draft_returns_none \
  agents/blogging/tests/test_writer_run.py::test_fallback_draft_via_json_exhausted_hook_returns_none \
  agents/blogging/tests/test_writer_run.py::test_fallback_draft_via_json_unexpected_hook_returns_none \
  agents/blogging/tests/test_writer_run.py::test_fallback_draft_via_json_transient_reraises \
  -v
```

Expected: FAIL with `AttributeError: ... has no attribute '_fallback_draft_via_json'` (or import error for `call_json_with_retry` once the method exists but import is missing).

- [ ] **Step 3: Implement `_fallback_draft_via_json`**

In `backend/agents/blogging/blog_writer_agent/agent.py`:

1. Add import near the other blogging shared imports:

```python
from agents.blogging.shared.json_retry import call_json_with_retry
```

2. Insert this method on `BlogWriterAgent` immediately after `_call_agent_json` (before `_assert_guidelines_present`):

```python
    def _fallback_draft_via_json(self, prompt: str) -> Optional[str]:
        """Parse a revised draft via shared JSON retry when the text path fails.

        Preconditions:
            - ``prompt`` is a non-empty string (same prompt used for the text path).
        Postconditions:
            - Returns a non-empty stripped draft string on success.
            - Returns ``None`` when JSON cannot yield a usable draft (caller keeps
              the prior draft).
            - Transient LLM transport errors (``LLMRateLimitError`` /
              ``LLMTemporaryError``) propagate unwrapped from ``call_json_with_retry``.
        """
        assert isinstance(prompt, str) and prompt.strip(), "prompt must be a non-empty string"

        soft_json_instruction = "\n\nRespond with valid JSON only, no markdown fences."
        strict_json_suffix = (
            "\n\nRespond with a single JSON object only (no markdown, no code fence). "
            'Keys: "draft" (string — the full revised blog post in Markdown).'
        )

        def _agent_factory():
            return Agent(model=self._model, system_prompt=WRITING_SYSTEM_PROMPT)

        def _empty_fallback(_exc: Exception) -> dict:
            return {}

        data = call_json_with_retry(
            _agent_factory,
            prompt + soft_json_instruction,
            max_attempts=2,
            strict_json_suffix=strict_json_suffix,
            on_exhausted=_empty_fallback,
            on_unexpected_error=_empty_fallback,
            logger=logger,
        )
        raw_draft = data.get("draft") if isinstance(data, dict) else None
        if isinstance(raw_draft, str) and raw_draft.strip():
            return raw_draft.strip()
        return None
```

- [ ] **Step 4: Run the new tests to verify they pass**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/issue-2084-migrate-blog-writer-json-retry/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/blogging/tests/test_writer_run.py::test_fallback_draft_via_json_success \
  agents/blogging/tests/test_writer_run.py::test_fallback_draft_via_json_empty_draft_returns_none \
  agents/blogging/tests/test_writer_run.py::test_fallback_draft_via_json_missing_draft_returns_none \
  agents/blogging/tests/test_writer_run.py::test_fallback_draft_via_json_exhausted_hook_returns_none \
  agents/blogging/tests/test_writer_run.py::test_fallback_draft_via_json_unexpected_hook_returns_none \
  agents/blogging/tests/test_writer_run.py::test_fallback_draft_via_json_transient_reraises \
  -q
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/issue-2084-migrate-blog-writer-json-retry
git add backend/agents/blogging/blog_writer_agent/agent.py \
  backend/agents/blogging/tests/test_writer_run.py
git commit -m "$(cat <<'EOF'
Add blog writer JSON draft fallback via call_json_with_retry.

EOF
)"
```

---

### Task 2: Wire three revise call sites and update fallback tests

**Files:**
- Modify: `backend/agents/blogging/blog_writer_agent/agent.py` (three fallback blocks)
- Modify: `backend/agents/blogging/tests/test_writer_run.py` (`_revise_single_item` fallback tests)
- Modify: `backend/agents/blogging/tests/test_writer_interactive.py` (batch revise + user-feedback fallback tests)
- Test: those two test files

**Interfaces:**
- Consumes: `BlogWriterAgent._fallback_draft_via_json(self, prompt: str) -> Optional[str]` from Task 1
- Produces: `_revise_single_item`, `revise`, and `revise_from_user_feedback` use the helper instead of `_call_agent_json` for draft recovery

- [ ] **Step 1: Update failing/outdated tests first (patch `_fallback_draft_via_json`)**

In `backend/agents/blogging/tests/test_writer_run.py`:

Replace `test_writer_revise_single_item_fallback_path` body so it patches `_fallback_draft_via_json` instead of `_call_agent_json`:

```python
def test_writer_revise_single_item_fallback_path(monkeypatch) -> None:
    """All text attempts fail; JSON fallback helper succeeds."""
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    a = _agent()
    import agents.blogging.blog_writer_agent.agent as wa_mod

    monkeypatch.setattr(wa_mod.time, "sleep", lambda *_: None)

    def boom(self, p, system_prompt=""):
        raise RuntimeError("transient")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    monkeypatch.setattr(
        BlogWriterAgent,
        "_fallback_draft_via_json",
        lambda self, p: "# Recovered",
    )
    item = FeedbackItem(category="x", severity="minor", issue="i")
    plan = make_content_plan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    ri = ReviseWriterInput(
        draft="# Orig", feedback_items=[item], feedback_summary="s", content_plan=plan
    )
    out = a._revise_single_item(
        draft="# Orig",
        item=item,
        item_index=1,
        total_items=1,
        style_guide_text="style",
        revise_input=ri,
    )
    assert "Recovered" in out
```

Replace `test_writer_revise_single_item_total_failure_returns_original` so fallback returns `None`:

```python
def test_writer_revise_single_item_total_failure_returns_original(monkeypatch) -> None:
    """All retries + fallback fail → original draft returned."""
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    a = _agent()
    import agents.blogging.blog_writer_agent.agent as wa_mod

    monkeypatch.setattr(wa_mod.time, "sleep", lambda *_: None)

    def boom(self, p, system_prompt=""):
        raise RuntimeError("nope")

    monkeypatch.setattr(BlogWriterAgent, "_call_text", boom)
    monkeypatch.setattr(BlogWriterAgent, "_fallback_draft_via_json", lambda self, p: None)
    item = FeedbackItem(category="x", severity="minor", issue="i")
    plan = make_content_plan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    ri = ReviseWriterInput(
        draft="# Orig", feedback_items=[item], feedback_summary="s", content_plan=plan
    )
    out = a._revise_single_item(
        draft="# Orig\nBody.",
        item=item,
        item_index=1,
        total_items=1,
        style_guide_text="style",
        revise_input=ri,
    )
    assert "Orig" in out
```

In `backend/agents/blogging/tests/test_writer_interactive.py`:

Update `test_revise_from_user_feedback_no_marker_then_json_fallback`:

```python
def test_revise_from_user_feedback_no_marker_then_json_fallback(monkeypatch) -> None:
    """LLM returns no ---DRAFT--- marker but JSON fallback works."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = _make_agent()

    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, prompt, system_prompt="": "no marker here",
    )
    monkeypatch.setattr(
        BlogWriterAgent,
        "_fallback_draft_via_json",
        lambda self, p: "# Fallback",
    )
    out = a.revise_from_user_feedback(
        draft="# Original", user_feedback="x", content_plan_text="cp"
    )
    assert "# Fallback" in out.draft
```

Update `test_revise_falls_back_to_original_when_llm_fails`:

```python
def test_revise_falls_back_to_original_when_llm_fails(monkeypatch, tmp_path) -> None:
    """If all retries fail and json fallback fails, return original draft."""
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput, RevisionPlan
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    a = _make_agent()

    monkeypatch.setattr(
        BlogWriterAgent,
        "_generate_revision_plan",
        lambda self, draft, items, ri: RevisionPlan(summary="planned", changes=[], risks=[]),
    )

    def fail(self, *a, **kw):
        raise RuntimeError("transient")

    import agents.blogging.blog_writer_agent.agent as wa_mod

    monkeypatch.setattr(wa_mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(BlogWriterAgent, "_call_text", fail)
    monkeypatch.setattr(BlogWriterAgent, "_fallback_draft_via_json", lambda self, p: None)

    plan = make_content_plan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    out = a.revise(
        ReviseWriterInput(
            draft="# Original\nBody",
            feedback_items=[FeedbackItem(category="x", severity="minor", issue="y")],
            feedback_summary="s",
            content_plan=plan,
        ),
    )
    assert "Original" in out.draft
```

Also add a positive batch-revise JSON fallback coverage test in the same file:

```python
def test_revise_batch_uses_json_fallback_when_text_fails(monkeypatch) -> None:
    """Batch revise uses _fallback_draft_via_json when text path yields no draft."""
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput, RevisionPlan
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    a = _make_agent()
    import agents.blogging.blog_writer_agent.agent as wa_mod

    monkeypatch.setattr(wa_mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        BlogWriterAgent,
        "_generate_revision_plan",
        lambda self, draft, items, ri: RevisionPlan(summary="planned", changes=[], risks=[]),
    )
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, *a, **kw: (_ for _ in ()).throw(RuntimeError("transient")),
    )
    monkeypatch.setattr(
        BlogWriterAgent,
        "_fallback_draft_via_json",
        lambda self, p: "# Batch Recovered",
    )
    plan = make_content_plan(
        overarching_topic="x",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    out = a.revise(
        ReviseWriterInput(
            draft="# Original\nBody",
            feedback_items=[FeedbackItem(category="x", severity="minor", issue="y")],
            feedback_summary="s",
            content_plan=plan,
        ),
    )
    assert "Batch Recovered" in out.draft
```

- [ ] **Step 2: Run those tests to confirm they fail against current call sites**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/issue-2084-migrate-blog-writer-json-retry/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/blogging/tests/test_writer_run.py::test_writer_revise_single_item_fallback_path \
  agents/blogging/tests/test_writer_run.py::test_writer_revise_single_item_total_failure_returns_original \
  agents/blogging/tests/test_writer_interactive.py::test_revise_from_user_feedback_no_marker_then_json_fallback \
  agents/blogging/tests/test_writer_interactive.py::test_revise_falls_back_to_original_when_llm_fails \
  agents/blogging/tests/test_writer_interactive.py::test_revise_batch_uses_json_fallback_when_text_fails \
  -v
```

Expected: FAIL — call sites still invoke `_call_agent_json`, so patched `_fallback_draft_via_json` is never used (fallback success tests keep original / miss recovered text; new batch test fails similarly).

- [ ] **Step 3: Replace the three `_call_agent_json` fallback blocks**

In `backend/agents/blogging/blog_writer_agent/agent.py`:

**`_revise_single_item`** — replace the trailing fallback try/except with:

```python
        fallback = self._fallback_draft_via_json(prompt)
        if fallback:
            return fallback
        logger.warning(
            "Revise item %s/%s: could not produce revision; keeping draft as-is.",
            item_index,
            total_items,
        )
        return draft
```

**`revise` batch path** — replace the `if current_draft == draft:` `_call_agent_json` try/except with:

```python
        if current_draft == draft:
            fallback = self._fallback_draft_via_json(prompt)
            if fallback:
                current_draft = fallback
```

**`revise_from_user_feedback`** — replace the `if current_draft == draft:` `_call_agent_json` try/except with:

```python
        if current_draft == draft:
            fallback = self._fallback_draft_via_json(prompt)
            if fallback:
                current_draft = fallback
```

Do not change the text-path `for attempt in range(...)` loops.

- [ ] **Step 4: Run writer revise/interactive tests**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/issue-2084-migrate-blog-writer-json-retry/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/blogging/tests/test_writer_run.py \
  agents/blogging/tests/test_writer_interactive.py \
  agents/blogging/tests/test_writer_self_review_and_revise.py \
  agents/blogging/tests/test_blog_writer_agent.py \
  agents/blogging/tests/test_blog_writer_agent_revise.py \
  -q
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/issue-2084-migrate-blog-writer-json-retry
git add backend/agents/blogging/blog_writer_agent/agent.py \
  backend/agents/blogging/tests/test_writer_run.py \
  backend/agents/blogging/tests/test_writer_interactive.py
git commit -m "$(cat <<'EOF'
Migrate blog writer revise JSON fallbacks onto shared helper.

EOF
)"
```

---

### Task 3: Lint and coverage gate

**Files:**
- Verify only (fix lint/coverage issues in the files from Tasks 1–2 if needed)

**Interfaces:**
- Consumes: completed migration from Tasks 1–2
- Produces: clean lint + coverage evidence for the PR

- [ ] **Step 1: Run lint from backend/**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/issue-2084-migrate-blog-writer-json-retry/backend
make lint
```

Expected: clean (fix any ruff issues in touched files if not).

- [ ] **Step 2: Run coverage on touched writer modules**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/issue-2084-migrate-blog-writer-json-retry/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/blogging/tests/test_writer_run.py \
  agents/blogging/tests/test_writer_interactive.py \
  agents/blogging/tests/test_writer_self_review_and_revise.py \
  agents/blogging/tests/test_blog_writer_agent.py \
  agents/blogging/tests/test_blog_writer_agent_revise.py \
  agents/blogging/tests/test_writer_and_v2_helpers.py \
  --cov=agents.blogging.blog_writer_agent.agent \
  --cov-report=term-missing \
  --cov-fail-under=90 \
  -q
```

Expected: coverage ≥ 90% for `blog_writer_agent.agent`; all tests PASS.

- [ ] **Step 3: Commit any lint/coverage fixes if needed**

Only if Step 1 or 2 required code/test changes:

```bash
git add -u
git commit -m "$(cat <<'EOF'
Fix lint and coverage after blog writer JSON-fallback migration.

EOF
)"
```

If no fixes were needed, skip this commit.

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| Private `_fallback_draft_via_json` using `call_json_with_retry` | Task 1 |
| `max_attempts=2`, soft + strict JSON, empty fallbacks | Task 1 |
| Wire `_revise_single_item` | Task 2 |
| Wire batch revise in `revise` | Task 2 |
| Wire `revise_from_user_feedback` | Task 2 |
| Preserve text+marker primary path | Task 2 (loops untouched) |
| Tests for fallback success / failure per site | Task 1 + Task 2 |
| Leave other `_call_agent_json` callers alone | Global constraints + Task 2 scope |
| `make lint` + 90% coverage | Task 3 |
| No helper module changes | Global constraints |
