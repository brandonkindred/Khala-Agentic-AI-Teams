# Reconcile JSON-Salvage Engines in LlmToolAgentBase Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `LlmToolAgentBase` with a class-attribute-parameterized JSON-parsing step that reproduces both of today's salvage engines (including Review `"text"` mode), preserving `{}` vs `None` failure sentinels, without migrating any consumers.

**Architecture:** Add five class attributes (`json_parse_strategy`, `review_parse_mode`, `parse_context`, `parse_on_fail_msg`, `_parse_review`) and an always-available `_parse_llm_json(self, raw)` method. Dispatch: `"extract"` → lazy `extract_json_object`; `"lenient"` + `"text"` → `_parse_review`; `"lenient"` + `"json"` → lazy `lenient_json_object`.

**Tech Stack:** Python 3.10+, pytest, ruff, existing `lenient_json_object` and `shared.llm_recovery.extract_json_object`.

**Spec:** `docs/superpowers/specs/2026-07-22-reconcile-json-salvage-engines-design.md`

**Worktree:** `.worktrees/issue-2038-reconcile-json-salvage` on branch `refactor/2038-reconcile-json-salvage-engines`

## Global Constraints

- Do not edit `ReviewToolAgent` / `BaseReviewToolAgent`, `PlanGeneratorToolAgent`, or `JsonGeneratorToolAgent`.
- Do not unify failure-return semantics (`{}` for lenient, `None` for extract).
- Keep the `code_review_agent` import-purity guarantee (subprocess regression test must still pass).
- Lazy-import engines inside the corresponding branch only — no module-level import of `tool_agent_base` or `shared.llm_recovery`.
- `review_parse_mode` is consulted only when `json_parse_strategy == "lenient"`.
- No GitHub issue numbers in code, comments, commit messages, or docs (PR body only).
- DbC: update module/class/`_parse_llm_json` docstrings with `Preconditions:` / `Postconditions:` / `Invariants:`.
- ≥90% line coverage on `llm_tool_agent_base.py`; `make lint` and the test file must pass from `backend/`.

## File map

| Path | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/shared/llm_tool_agent_base.py` | Class attrs + `_parse_llm_json` dispatch |
| `backend/agents/software_engineering_team/tests/test_llm_tool_agent_base.py` | Strategy success/failure + text mode + sentinel contract tests |

---

### Task 1: Failing tests for both salvage strategies

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_llm_tool_agent_base.py`

**Interfaces:**
- Consumes: `LlmToolAgentBase` (existing); expects future class attrs `json_parse_strategy`, `review_parse_mode`, `parse_context`, `parse_on_fail_msg`, `_parse_review` and method `_parse_llm_json(self, raw: str)`
- Produces: failing tests that lock dispatch and failure-sentinel contracts

- [ ] **Step 1: Append the new tests**

Add this section to the end of
`backend/agents/software_engineering_team/tests/test_llm_tool_agent_base.py`
(keep all existing tests unchanged):

```python
# ---------------------------------------------------------------------------
# parameterized JSON parsing (lenient vs extract; text mode)
# ---------------------------------------------------------------------------


def test_lenient_json_success_parses_object():
    class ReviewJsonLike(LlmToolAgentBase):
        json_parse_strategy = "lenient"
        review_parse_mode = "json"
        parse_context = "unit-test"
        parse_on_fail_msg = "reporting empty."

    agent = ReviewJsonLike()
    assert agent._parse_llm_json('{"ok": true, "n": 1}') == {"ok": True, "n": 1}


def test_lenient_json_failure_returns_empty_dict_not_none():
    """Real engine sentinel: unparseable input must yield {} (not None)."""

    class ReviewJsonLike(LlmToolAgentBase):
        json_parse_strategy = "lenient"
        review_parse_mode = "json"
        parse_context = "unit-test"
        parse_on_fail_msg = "reporting empty."

    agent = ReviewJsonLike()
    result = agent._parse_llm_json("no json object here at all")
    assert result == {}
    assert result is not None


def test_lenient_text_mode_calls_parse_review_hook(monkeypatch):
    calls = []
    engine_calls = []

    def fake_parse_review(raw: str):
        calls.append(raw)
        return {"issues": [{"description": "from-hook"}]}

    def boom_lenient(*args, **kwargs):
        engine_calls.append(("lenient", args, kwargs))
        raise AssertionError("lenient_json_object must not be called in text mode")

    def boom_extract(*args, **kwargs):
        engine_calls.append(("extract", args, kwargs))
        raise AssertionError("extract_json_object must not be called in text mode")

    monkeypatch.setattr(
        "software_engineering_team.shared.tool_agent_base.lenient_json_object",
        boom_lenient,
    )
    monkeypatch.setattr(
        "shared.llm_recovery.extract_json_object",
        boom_extract,
        raising=False,
    )

    class ReviewTextLike(LlmToolAgentBase):
        json_parse_strategy = "lenient"
        review_parse_mode = "text"
        _parse_review = staticmethod(fake_parse_review)

    agent = ReviewTextLike()
    result = agent._parse_llm_json("TEMPLATE OUTPUT")

    assert result == {"issues": [{"description": "from-hook"}]}
    assert calls == ["TEMPLATE OUTPUT"]
    assert engine_calls == []


def test_extract_success_returns_dict(monkeypatch):
    def fake_extract(raw: str):
        assert raw == '{"a": 1}'
        return {"a": 1}

    monkeypatch.setattr("shared.llm_recovery.extract_json_object", fake_extract)

    class PlanJsonLike(LlmToolAgentBase):
        json_parse_strategy = "extract"

    agent = PlanJsonLike()
    assert agent._parse_llm_json('{"a": 1}') == {"a": 1}


def test_extract_failure_returns_none_not_empty_dict():
    """Real engine sentinel: unparseable input must yield None (not {})."""

    class PlanJsonLike(LlmToolAgentBase):
        json_parse_strategy = "extract"

    agent = PlanJsonLike()
    result = agent._parse_llm_json("no json object here at all")
    assert result is None
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run from `backend/`:

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_llm_tool_agent_base.py \
  -k "lenient_json or lenient_text or extract_" -v
```

Expected: FAIL — `LlmToolAgentBase` has no `_parse_llm_json` (and/or missing class attrs).

- [ ] **Step 3: Commit the failing tests**

```bash
git add backend/agents/software_engineering_team/tests/test_llm_tool_agent_base.py
git commit -m "$(cat <<'EOF'
Add failing tests for parameterized JSON-salvage on LlmToolAgentBase.

Lock lenient vs extract dispatch and {} vs None failure sentinels, including
Review text-mode hook routing.
EOF
)"
```

---

### Task 2: Implement `_parse_llm_json` and class attrs

**Files:**
- Modify: `backend/agents/software_engineering_team/shared/llm_tool_agent_base.py`

**Interfaces:**
- Consumes: class attrs from Task 1 tests; `lenient_json_object` and `extract_json_object` via lazy import
- Produces: `LlmToolAgentBase._parse_llm_json(self, raw: str) -> Optional[dict[str, Any]]`

- [ ] **Step 1: Update imports and module docstring**

At the top of `llm_tool_agent_base.py`, extend typing imports and mention JSON
parsing in the module docstring (still note that fallback logic remains out of
scope; remove the line that says JSON parsing remains out of scope):

```python
from typing import Any, Callable, Dict, Optional
```

Update the opening module docstring so it lists the opt-in model-resolution step,
the opt-in LLM invocation step, **and** the parameterized JSON-parsing step.
Keep the `code_review_agent` purity postcondition.

- [ ] **Step 2: Add class attributes**

On `LlmToolAgentBase`, after the existing invocation attrs
(`use_run_strands_agent`), add:

```python
    json_parse_strategy: str = "lenient"  # "lenient" | "extract"
    review_parse_mode: str = "json"  # "json" | "text"; only for lenient
    parse_context: str = ""
    parse_on_fail_msg: str = "reporting empty result."
    _parse_review: Optional[Callable[[str], Dict[str, Any]]] = None
```

Also add `import logging` at module level (stdlib only — does not break purity).

- [ ] **Step 3: Implement `_parse_llm_json`**

Append this method to `LlmToolAgentBase` (after `_invoke_llm`):

```python
    def _parse_llm_json(self, raw: str) -> Optional[Dict[str, Any]]:
        """Parse model output via the selected JSON-salvage strategy.

        When ``json_parse_strategy`` is ``"extract"``, delegates to
        ``shared.llm_recovery.extract_json_object`` (failure → ``None``).
        When ``"lenient"`` and ``review_parse_mode == "text"``, calls
        ``self._parse_review(raw)``. Otherwise uses
        ``tool_agent_base.lenient_json_object`` (failure → ``{}``).

        Preconditions:
            ``raw`` is a ``str``. ``json_parse_strategy`` is ``"lenient"`` or
            ``"extract"``. If strategy is ``"lenient"``, ``review_parse_mode``
            is ``"json"`` or ``"text"``. If mode is ``"text"``,
            ``_parse_review`` is not ``None``.

        Postconditions:
            Returns a ``dict`` for lenient/text paths (``{}`` on lenient JSON
            failure). Returns ``dict | None`` for extract (``None`` on failure).
            Does not import ``tool_agent_base`` or ``shared.llm_recovery`` until
            the corresponding branch runs.
        """
        strategy = type(self).json_parse_strategy
        assert strategy in ("lenient", "extract"), strategy

        if strategy == "extract":
            from shared.llm_recovery import extract_json_object

            return extract_json_object(raw)

        mode = type(self).review_parse_mode
        assert mode in ("json", "text"), mode

        if mode == "text":
            parse_review = type(self)._parse_review
            assert parse_review is not None, "_parse_review required for text mode"
            return parse_review(raw)

        from software_engineering_team.shared.tool_agent_base import lenient_json_object

        return lenient_json_object(
            raw,
            logger=logging.getLogger(type(self).__module__),
            context=self.parse_context,
            on_fail_msg=self.parse_on_fail_msg,
        )
```

Update the class docstring Recipes block to mention the three JSON-parse recipes
from the spec (Review JSON, Review text, Plan/Json extract).

Use `type(self).json_parse_strategy` / `type(self)._parse_review` (not
`self._parse_review` alone for the hook) so a class-level `staticmethod` is not
unexpectedly bound — same pattern as `get_strands_model_fn` in model resolution.

- [ ] **Step 4: Run the new tests to verify they pass**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_llm_tool_agent_base.py -v
```

Expected: all tests PASS (existing + new).

- [ ] **Step 5: Commit the implementation**

```bash
git add backend/agents/software_engineering_team/shared/llm_tool_agent_base.py
git commit -m "$(cat <<'EOF'
Add parameterized JSON-salvage step to LlmToolAgentBase.

Support lenient (with text-mode hook) and extract strategies, preserving
their distinct {} vs None failure sentinels without migrating consumers.
EOF
)"
```

---

### Task 3: Lint, coverage, and closeout verification

**Files:**
- Verify only (no intentional product changes unless lint/coverage forces a fix)

**Interfaces:**
- Consumes: Tasks 1–2 deliverables
- Produces: green lint + coverage ≥90% on `llm_tool_agent_base.py`

- [ ] **Step 1: Lint**

```bash
cd backend && make lint
```

Expected: ruff check + format clean for touched files. Fix any issues in place.

- [ ] **Step 2: Coverage on the touched module**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_llm_tool_agent_base.py \
  --cov=software_engineering_team.shared.llm_tool_agent_base \
  --cov-report=term-missing \
  --cov-fail-under=90
```

Expected: PASS with ≥90% line coverage. If a branch is missing (e.g. unknown
strategy assert), add a focused test rather than pragma.

Optional extra test if coverage requires the unknown-strategy / missing-hook
assert paths:

```python
def test_unknown_json_parse_strategy_raises():
    class Bad(LlmToolAgentBase):
        json_parse_strategy = "nope"

    with pytest.raises(AssertionError):
        Bad()._parse_llm_json("{}")


def test_text_mode_without_parse_review_raises():
    class Bad(LlmToolAgentBase):
        json_parse_strategy = "lenient"
        review_parse_mode = "text"

    with pytest.raises(AssertionError):
        Bad()._parse_llm_json("raw")
```

(Import `pytest` at top of the test module if adding these.)

- [ ] **Step 3: Confirm the three consumer bases were not modified**

```bash
git diff --name-only origin/main...HEAD
```

Expected: only
`shared/llm_tool_agent_base.py`,
`tests/test_llm_tool_agent_base.py`,
and the design/plan docs under `docs/superpowers/`. No
`tool_agent_base.py`, `_plan_base.py`, or `ai_agent_development_team/.../_base.py`.

- [ ] **Step 4: Commit any lint/coverage fixes** (skip if working tree clean)

```bash
git add -u
git commit -m "$(cat <<'EOF'
Tighten JSON-salvage tests and lint for LlmToolAgentBase.

Cover precondition asserts and keep coverage above the project floor.
EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| Parameterized `_parse_llm_json` with lenient + extract | Task 2 |
| Text mode via `review_parse_mode` + `_parse_review` | Tasks 1–2 |
| Failure sentinels `{}` vs `None` preserved | Task 1 (real engines) + Task 2 |
| No consumer migration | Task 3 name-only check |
| Import purity for `code_review_agent` | Existing purity test kept; lazy imports in Task 2 |
| Unit tests for success/failure/text | Task 1 |
| `make lint` + 90% coverage | Task 3 |
