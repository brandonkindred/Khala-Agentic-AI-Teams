# Unify Model Resolution in LlmToolAgentBase Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `LlmToolAgentBase` with opt-in, class-attribute-parameterized model resolution that reproduces both of today's tool-agent resolver call shapes, without migrating any consumers.

**Architecture:** Add four class attributes (`resolve_models`, `response_format`, `uses_json_model`, `get_strands_model_fn`). When `resolve_models` is true, `__init__` lazy-imports `llm_service.strands_model.resolve_strands_model` and sets `_model` (and optionally `_model_json`) using kwargs that match the Review or Plan/Json recipes exactly.

**Tech Stack:** Python 3.10+, pytest, ruff, existing `llm_service.strands_model.resolve_strands_model`.

**Spec:** `docs/superpowers/specs/2026-07-22-unify-model-resolution-llm-tool-agent-base-design.md`

**Worktree:** `.worktrees/issue-2033-unify-model-resolution` on branch `refactor/unify-model-resolution-llm-tool-agent-base`

## Global Constraints

- Do not edit `ReviewToolAgent`, `PlanGeneratorToolAgent`, or `JsonGeneratorToolAgent`.
- Do not change `resolve_strands_model` itself.
- Keep the `code_review_agent` import-purity guarantee (subprocess regression test must still pass).
- Lazy-import the resolver inside the resolve step — no module-level `llm_service.strands_model` / `get_strands_model` import required for the default skeleton path.
- Forward `get_strands_model_fn` only when the class attr is not `None`.
- When `uses_json_model` is true, the second resolve always uses `response_format="json"` (independent of primary `response_format`).
- No GitHub issue numbers in code, comments, commit messages, or docs (PR body only).
- DbC: update class/module docstrings — `Preconditions:` / `Postconditions:` / `Invariants:` must reflect optional `_model` / `_model_json`.
- ≥90% line coverage on `llm_tool_agent_base.py`; `make lint` and the test file must pass from `backend/`.

## File map

| Path | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/shared/llm_tool_agent_base.py` | Class attrs + opt-in resolve step in `__init__` |
| `backend/agents/software_engineering_team/tests/test_llm_tool_agent_base.py` | Opt-out + Review path + Review+JSON + Plan/Json path tests |

---

### Task 1: Failing tests for both resolver recipes

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_llm_tool_agent_base.py`

**Interfaces:**
- Consumes: `LlmToolAgentBase` (existing); expects future class attrs `resolve_models`, `response_format`, `uses_json_model`, `get_strands_model_fn` and instance attrs `_model` / `_model_json` when opted in
- Produces: four new failing tests that lock the resolution contract

- [ ] **Step 1: Append the new tests**

Add this section to the end of
`backend/agents/software_engineering_team/tests/test_llm_tool_agent_base.py`
(keep all existing tests unchanged):

```python
# ---------------------------------------------------------------------------
# opt-in model resolution (parameterized recipes)
# ---------------------------------------------------------------------------


def _patch_resolve(monkeypatch, fake):
    """Patch the lazy-imported resolver before constructing an opted-in agent."""
    monkeypatch.setattr("llm_service.strands_model.resolve_strands_model", fake)


def test_resolve_models_false_does_not_set_model(monkeypatch):
    calls = []

    def fake_resolve(llm, **kwargs):
        calls.append((llm, kwargs))
        return object()

    _patch_resolve(monkeypatch, fake_resolve)

    agent = LlmToolAgentBase(llm=object())

    assert not hasattr(agent, "_model")
    assert not hasattr(agent, "_model_json")
    assert calls == []


def test_review_like_resolves_text_model_without_get_strands_model_fn(monkeypatch):
    calls = []
    text_model = object()

    def fake_resolve(llm, **kwargs):
        calls.append((llm, dict(kwargs)))
        return text_model

    _patch_resolve(monkeypatch, fake_resolve)

    class ReviewLike(LlmToolAgentBase):
        resolve_models = True

    llm = object()
    agent = ReviewLike(llm=llm)

    assert agent._model is text_model
    assert not hasattr(agent, "_model_json")
    assert len(calls) == 1
    assert calls[0][0] is llm
    assert calls[0][1] == {"response_format": "text"}
    assert "get_strands_model_fn" not in calls[0][1]


def test_review_like_uses_json_model_resolves_second_json_model(monkeypatch):
    calls = []
    text_model = object()
    json_model = object()

    def fake_resolve(llm, **kwargs):
        calls.append((llm, dict(kwargs)))
        return text_model if kwargs.get("response_format") == "text" else json_model

    _patch_resolve(monkeypatch, fake_resolve)

    class ReviewJsonLike(LlmToolAgentBase):
        resolve_models = True
        uses_json_model = True

    llm = object()
    agent = ReviewJsonLike(llm=llm)

    assert agent._model is text_model
    assert agent._model_json is json_model
    assert len(calls) == 2
    assert calls[0][1] == {"response_format": "text"}
    assert calls[1][1] == {"response_format": "json"}
    assert "get_strands_model_fn" not in calls[0][1]
    assert "get_strands_model_fn" not in calls[1][1]


def test_plan_json_like_resolves_with_get_strands_model_fn(monkeypatch):
    calls = []
    json_model = object()
    sentinel_fn = object()

    def fake_resolve(llm, **kwargs):
        calls.append((llm, dict(kwargs)))
        return json_model

    _patch_resolve(monkeypatch, fake_resolve)

    class PlanJsonLike(LlmToolAgentBase):
        resolve_models = True
        response_format = "json"
        get_strands_model_fn = sentinel_fn

    llm = object()
    agent = PlanJsonLike(llm=llm)

    assert agent._model is json_model
    assert not hasattr(agent, "_model_json")
    assert len(calls) == 1
    assert calls[0][0] is llm
    assert calls[0][1] == {
        "response_format": "json",
        "get_strands_model_fn": sentinel_fn,
    }
```

- [ ] **Step 2: Run the new tests and confirm they fail**

Run from `backend/` (use the main-repo venv if the worktree has none):

```bash
cd backend
../.venv/bin/python -m pytest agents/software_engineering_team/tests/test_llm_tool_agent_base.py -v --no-cov
```

If the worktree is at `.worktrees/issue-2033-unify-model-resolution`, prefer:

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/issue-2033-unify-model-resolution/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_llm_tool_agent_base.py -v --no-cov
```

Expected: the four new tests FAIL (e.g. `AttributeError: type object 'ReviewLike' has no attribute 'resolve_models'` is fine, or failure asserting `_model` / call kwargs). The four pre-existing tests must still PASS.

- [ ] **Step 3: Commit the failing tests**

```bash
git add backend/agents/software_engineering_team/tests/test_llm_tool_agent_base.py
git commit -m "$(cat <<'EOF'
Add failing tests for parameterized LlmToolAgentBase model resolution.

EOF
)"
```

---

### Task 2: Implement opt-in model resolution

**Files:**
- Modify: `backend/agents/software_engineering_team/shared/llm_tool_agent_base.py`

**Interfaces:**
- Consumes: `llm_service.strands_model.resolve_strands_model(llm, *, response_format=..., get_strands_model_fn=...)`
- Produces: `LlmToolAgentBase` with class attrs and opt-in `_model` / `_model_json` population

- [ ] **Step 1: Replace the module with the resolved implementation**

Overwrite `backend/agents/software_engineering_team/shared/llm_tool_agent_base.py` with:

```python
"""Dependency-light base shared by the LLM tool-agent classes.

Holds the ``_agent_factory`` monkeypatch resolver and an opt-in, class-attribute
parameterized model-resolution step. Deliberately imports nothing from
``code_review_agent`` so it can be depended on from any team without pulling in
the code-review engine. LLM invocation, JSON parsing, and fallback logic remain
out of scope here.

Preconditions:
    None beyond standard Python import semantics.

Postconditions:
    Importing this module never triggers an import of ``code_review_agent``
    (verified by ``tests/test_llm_tool_agent_base.py``).

Invariants:
    ``LlmToolAgentBase`` always stores ``self.llm``. When ``resolve_models`` is
    true it also stores ``self._model``, and ``self._model_json`` when
    ``uses_json_model`` is true.
"""

from __future__ import annotations

import importlib
from typing import Any, Callable, Optional


class LlmToolAgentBase:
    """Bare constructor, shared ``_agent_factory``, and opt-in model resolution.

    Subclasses opt into resolution by setting ``resolve_models = True`` and
    (when needed) overriding ``response_format``, ``uses_json_model``, and/or
    ``get_strands_model_fn``.

    Recipes:
        Review-like — ``resolve_models = True`` (defaults give text mode; set
        ``uses_json_model = True`` for a second JSON-mode model).
        Plan/Json-like — ``resolve_models = True``, ``response_format = "json"``,
        ``get_strands_model_fn = <callable>``.

    Preconditions:
        ``llm``, if provided, is whatever ``resolve_strands_model`` accepts
        (Strands ``Model``, ``LLMClient``, or ``None``).

    Postconditions:
        ``self.llm`` holds the constructor argument. If ``resolve_models`` is
        true, ``self._model`` is set; if ``uses_json_model`` is also true,
        ``self._model_json`` is set.

    Invariants:
        Resolution runs only when ``resolve_models`` is true. The
        ``get_strands_model_fn`` kwarg is forwarded only when the class attr
        is not ``None``.
    """

    resolve_models: bool = False
    response_format: str = "text"
    uses_json_model: bool = False
    get_strands_model_fn: Optional[Callable[..., Any]] = None

    def __init__(self, llm=None) -> None:
        self.llm = llm
        if not self.resolve_models:
            return

        from llm_service.strands_model import resolve_strands_model

        resolve_kwargs: dict[str, Any] = {"response_format": self.response_format}
        if self.get_strands_model_fn is not None:
            resolve_kwargs["get_strands_model_fn"] = self.get_strands_model_fn

        self._model = resolve_strands_model(llm, **resolve_kwargs)

        if self.uses_json_model:
            json_kwargs: dict[str, Any] = {"response_format": "json"}
            if self.get_strands_model_fn is not None:
                json_kwargs["get_strands_model_fn"] = self.get_strands_model_fn
            self._model_json = resolve_strands_model(llm, **json_kwargs)

    def _agent_factory(self):
        """Resolve ``Agent`` from the concrete subclass's defining module.

        This is what lets ``monkeypatch.setattr(<agent_module>, "Agent", ...)``
        intercept LLM calls made from this shared base.

        Preconditions:
            ``type(self).__module__`` names a module that defines an ``Agent``
            symbol (patched in tests, or the real Strands ``Agent`` in
            production).

        Postconditions:
            Returns the ``Agent`` symbol from that module.
        """
        mod = importlib.import_module(type(self).__module__)
        return getattr(mod, "Agent")
```

- [ ] **Step 2: Run the full test file and confirm all tests pass**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/issue-2033-unify-model-resolution/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_llm_tool_agent_base.py -v --no-cov
```

Expected: 8 passed (4 existing + 4 new).

- [ ] **Step 3: Commit the implementation**

```bash
git add backend/agents/software_engineering_team/shared/llm_tool_agent_base.py
git commit -m "$(cat <<'EOF'
Add opt-in parameterized model resolution to LlmToolAgentBase.

EOF
)"
```

---

### Task 3: Lint, coverage, and acceptance gate

**Files:**
- Verify only (no intentional further edits unless lint/coverage forces a fix)

**Interfaces:**
- Consumes: Task 1–2 deliverables
- Produces: green lint + coverage evidence for the touched module

- [ ] **Step 1: Run ruff on the touched files**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/issue-2033-unify-model-resolution/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m ruff check \
  agents/software_engineering_team/shared/llm_tool_agent_base.py \
  agents/software_engineering_team/tests/test_llm_tool_agent_base.py
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m ruff format --check \
  agents/software_engineering_team/shared/llm_tool_agent_base.py \
  agents/software_engineering_team/tests/test_llm_tool_agent_base.py
```

Expected: no issues. If format check fails, run `ruff format` on those paths and amend only if this commit has not been pushed and was created by you in this session; otherwise make a new commit.

- [ ] **Step 2: Run coverage on the touched module**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/issue-2033-unify-model-resolution/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_llm_tool_agent_base.py \
  --cov=software_engineering_team.shared.llm_tool_agent_base \
  --cov-report=term-missing \
  --cov-fail-under=90
```

Expected: coverage ≥90% for `llm_tool_agent_base.py`, all tests pass.

- [ ] **Step 3: Confirm consumers were not modified**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/issue-2033-unify-model-resolution
git diff origin/main -- \
  backend/agents/software_engineering_team/shared/tool_agent_base.py \
  backend/agents/software_engineering_team/frontend_code_v2_team/tool_agents/_plan_base.py \
  backend/agents/software_engineering_team/ai_agent_development_team/tool_agents/_base.py
```

Expected: empty diff (no consumer changes).

- [ ] **Step 4: Commit any lint/format fixes if needed; otherwise skip**

Only if Step 1 required edits:

```bash
git add backend/agents/software_engineering_team/shared/llm_tool_agent_base.py \
  backend/agents/software_engineering_team/tests/test_llm_tool_agent_base.py
git commit -m "$(cat <<'EOF'
Fix lint/format on LlmToolAgentBase model-resolution changes.

EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| Class-attr parameterization (`resolve_models`, `response_format`, `uses_json_model`, `get_strands_model_fn`) | Task 2 |
| Opt-in only (`resolve_models=False` keeps skeleton behavior) | Task 1 + 2 |
| Review-like text resolve without `get_strands_model_fn` | Task 1 + 2 |
| Optional `_model_json` with `"json"` | Task 1 + 2 |
| Plan/Json-like resolve with `get_strands_model_fn` | Task 1 + 2 |
| Lazy-import from `llm_service.strands_model` | Task 2 |
| No consumer migration | Task 3 Step 3 |
| Import purity preserved | Task 1 keeps existing purity test; Task 2 keeps lazy import |
| ≥90% coverage + lint | Task 3 |
