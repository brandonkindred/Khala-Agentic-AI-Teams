# Migrate ghost_writer JSON-retry call sites Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `_find_gaps_via_llm` and `_evaluate_sufficiency` ad hoc JSON-retry loops with `call_json_with_retry()`, preserving each site’s fallback shape and leaving `_compile_narrative` unchanged.

**Architecture:** Direct helper calls at each site (Approach 1). Factory + `on_exhausted` / `on_unexpected_error` return site-specific fallbacks. Post-parse shaping (list→`StoryGap`, non-dict→default) stays outside the helper. Match shared-helper semantics: no local sleep/retry on generic exceptions; transient LLM errors re-raise.

**Tech Stack:** Python 3.10, `agents.blogging.shared.json_retry.call_json_with_retry`, strands `Agent`, pytest.

## Global Constraints

- Do not modify `call_json_with_retry` itself.
- Do not change `_compile_narrative` or `_generate_friendly_seeds`.
- Do not reference GitHub issue numbers in code, comments, docs, or commit messages.
- Design-by-Contract docstring sections required on any new/changed public functions (existing private methods keep current docstring style unless rewritten).
- `make lint` clean; ≥90% line coverage on touched files.
- Work only in the existing feature worktree / branch for this migration (do not rename mid-flight).

**Spec:** `docs/superpowers/specs/2026-07-23-migrate-ghost-writer-json-retry-design.md`

---

## File map

| File | Role |
|---|---|
| `backend/agents/blogging/ghost_writer_agent/agent.py` | Migrate two call sites; keep `extract_json_from_response` import (still used by `_generate_friendly_seeds`); drop `LLMJsonParseError` if unused |
| `backend/agents/blogging/tests/test_ghost_writer_and_more.py` | Align exception tests with helper semantics; keep happy/parse/fallback coverage |

No new modules.

---

### Task 1: Red — update exception tests for helper semantics

**Files:**
- Modify: `backend/agents/blogging/tests/test_ghost_writer_and_more.py`
- Test: same file

**Interfaces:**
- Consumes: existing `_patch_agent`, `_gap`, `_content_plan`, `GhostWriterElicitationAgent`
- Produces: failing tests that encode immediate-fallback-on-unexpected-error behavior

- [ ] **Step 1: Rewrite the gaps exception test to expect immediate `[]`**

Keep a raise-then-success stub (same as today’s recover test) but assert `[]` — that is what makes this test RED on the old local-retry loop.

Replace `test_ghost_find_gaps_via_llm_exception_then_recover` with:

```python
def test_ghost_find_gaps_via_llm_exception_falls_back_empty(monkeypatch) -> None:
    """Generic invoke errors fall back immediately (shared helper, no local retry)."""
    import agents.blogging.ghost_writer_agent.agent as gw_agent
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    state = {"i": 0}

    class _Stub:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            i = state["i"]
            state["i"] += 1
            if i == 0:
                raise RuntimeError("transient")
            return json.dumps(
                [
                    {
                        "section_title": "Intro",
                        "section_context": "Hook",
                        "seed_question": "Got a moment?",
                    }
                ]
            )

    monkeypatch.setattr(gw_agent, "Agent", _Stub)
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    # Old loop would recover on attempt 2; helper falls back on first unexpected error.
    assert agent._find_gaps_via_llm(_content_plan()) == []
```

- [ ] **Step 2: Drop the sleep patch from the sufficiency exception test**

Replace `test_ghost_evaluate_sufficiency_exception_then_default` with:

```python
def test_ghost_evaluate_sufficiency_exception_then_default(monkeypatch) -> None:
    import agents.blogging.ghost_writer_agent.agent as gw_agent
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    class _Boom:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise RuntimeError("LLM exploded")

    monkeypatch.setattr(gw_agent, "Agent", _Boom)
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._evaluate_sufficiency(_gap(), [])
    assert out["sufficient"] is False
```

- [ ] **Step 3: Run the two tests — gaps test must FAIL on current code**

Run from `backend/`:

```bash
.venv/bin/python -m pytest \
  agents/blogging/tests/test_ghost_writer_and_more.py::test_ghost_find_gaps_via_llm_exception_falls_back_empty \
  agents/blogging/tests/test_ghost_writer_and_more.py::test_ghost_evaluate_sufficiency_exception_then_default \
  -v
```

Expected:
- `test_ghost_find_gaps_via_llm_exception_falls_back_empty` **FAIL** with `AssertionError` (got 1 gap, expected `[]`)
- `test_ghost_evaluate_sufficiency_exception_then_default` **PASS** (same observable fallback)

- [ ] **Step 4: Commit the red tests**

```bash
git add backend/agents/blogging/tests/test_ghost_writer_and_more.py
git commit -m "$(cat <<'EOF'
Failing tests: ghost_writer unexpected errors use immediate fallback.

EOF
)"
```

---

### Task 2: Green — migrate `_evaluate_sufficiency`

**Files:**
- Modify: `backend/agents/blogging/ghost_writer_agent/agent.py` (imports + `_evaluate_sufficiency`)
- Test: `backend/agents/blogging/tests/test_ghost_writer_and_more.py`

**Interfaces:**
- Consumes: `call_json_with_retry(agent_factory, prompt, *, max_attempts=2, strict_json_suffix=..., on_exhausted=..., on_unexpected_error=..., logger=...) -> Dict[str, Any]`
- Produces: `_evaluate_sufficiency(...) -> Dict[str, Any]` with same public behavior except helper error policy

- [ ] **Step 1: Add the shared-helper import**

Near the top of `agent.py`, add:

```python
from agents.blogging.shared.json_retry import call_json_with_retry
```

Keep for now:

```python
from llm_service import LLMJsonParseError, extract_json_from_response
```

(`extract_json_from_response` is still required by `_generate_friendly_seeds`; `LLMJsonParseError` drops in Task 3 after gaps migrate.)

- [ ] **Step 2: Replace the `_evaluate_sufficiency` retry loop**

Replace the body from `agent = Agent(...)` through the final `return default` with:

```python
        def _agent_factory():
            return Agent(model=self._model, system_prompt=system)

        def _fallback(_exc: Exception) -> Dict[str, Any]:
            return default

        data = call_json_with_retry(
            _agent_factory,
            prompt,
            max_attempts=2,
            strict_json_suffix=_JSON_RETRY_SUFFIX,
            on_exhausted=_fallback,
            on_unexpected_error=_fallback,
            logger=logger,
        )
        if isinstance(data, dict):
            return data
        return default
```

Full method after change:

```python
    def _evaluate_sufficiency(
        self,
        gap: StoryGap,
        conversation: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        """Use the LLM evaluator to assess whether the conversation has enough material."""
        system = (
            _EVALUATE_SUFFICIENCY_SYSTEM
            + f"\n\nSection: {gap.section_title}\nContext: {gap.section_context}"
        )

        conv_text = ""
        for msg in conversation:
            role = "Ghost writer" if msg["role"] == "agent" else "Author"
            conv_text += f"{role}: {msg['content']}\n"

        prompt = (
            conv_text
            + "\nEvaluate the conversation above. Respond with the JSON object only, no markdown fences."
        )

        default = {
            "sufficient": False,
            "no_experience": False,
            "story_context": None,
            "missing": None,
        }

        def _agent_factory():
            return Agent(model=self._model, system_prompt=system)

        def _fallback(_exc: Exception) -> Dict[str, Any]:
            return default

        data = call_json_with_retry(
            _agent_factory,
            prompt,
            max_attempts=2,
            strict_json_suffix=_JSON_RETRY_SUFFIX,
            on_exhausted=_fallback,
            on_unexpected_error=_fallback,
            logger=logger,
        )
        if isinstance(data, dict):
            return data
        return default
```

- [ ] **Step 3: Run sufficiency-focused tests**

```bash
.venv/bin/python -m pytest agents/blogging/tests/test_ghost_writer_and_more.py -k "evaluate_sufficiency" -v
```

Expected: all matching tests **PASS**.

- [ ] **Step 4: Commit**

```bash
git add backend/agents/blogging/ghost_writer_agent/agent.py
git commit -m "$(cat <<'EOF'
Migrate ghost_writer sufficiency evaluator onto call_json_with_retry.

EOF
)"
```

---

### Task 3: Green — migrate `_find_gaps_via_llm`

**Files:**
- Modify: `backend/agents/blogging/ghost_writer_agent/agent.py` (`_find_gaps_via_llm` + import cleanup)
- Test: `backend/agents/blogging/tests/test_ghost_writer_and_more.py`

**Interfaces:**
- Consumes: same `call_json_with_retry` as Task 2; fallbacks return `[]` (list) — helper return annotation is `Dict` but runtime list from `json.loads` is accepted and checked after
- Produces: `_find_gaps_via_llm(...) -> List[StoryGap]`

- [ ] **Step 1: Replace the `_find_gaps_via_llm` retry loop**

Replace the method body with:

```python
    def _find_gaps_via_llm(self, content_plan: ContentPlan) -> List[StoryGap]:
        """Fallback: use LLM to identify story gaps when plan lacks story_opportunity fields."""
        outline_text = self._plan_to_text(content_plan)
        prompt = f"Content plan:\n\n{outline_text}\n\nIdentify story gaps."

        def _agent_factory():
            return Agent(model=self._model, system_prompt=_FIND_GAPS_SYSTEM)

        def _fallback(_exc: Exception) -> list:
            return []

        data = call_json_with_retry(
            _agent_factory,
            prompt,
            max_attempts=2,
            strict_json_suffix=_JSON_RETRY_SUFFIX,
            on_exhausted=_fallback,
            on_unexpected_error=_fallback,
            logger=logger,
        )
        if not isinstance(data, list):
            logger.warning("Ghost writer: no JSON array in gap-finding response")
            return []
        gaps = []
        for item in data[:3]:
            ctx = item.get("section_context", "")
            seed = (item.get("seed_question") or "").strip()
            if not seed:
                seed = f"I'd love to hear about a time you dealt with {ctx.lower().rstrip('.')}. What comes to mind?"
            gaps.append(
                StoryGap(
                    section_title=item.get("section_title", ""),
                    section_context=ctx,
                    seed_question=seed,
                )
            )
        logger.info("Ghost writer: found %s story gap(s) via LLM", len(gaps))
        return gaps
```

- [ ] **Step 2: Drop unused `LLMJsonParseError` import**

Change:

```python
from llm_service import LLMJsonParseError, extract_json_from_response
```

to:

```python
from llm_service import extract_json_from_response
```

Keep `import time` — `_compile_narrative` still uses `time.sleep`.

- [ ] **Step 3: Run gaps + full ghost-writer test file**

```bash
.venv/bin/python -m pytest agents/blogging/tests/test_ghost_writer_and_more.py -v
```

Expected: all tests **PASS**, including `test_ghost_find_gaps_via_llm_exception_falls_back_empty`.

- [ ] **Step 4: Commit**

```bash
git add backend/agents/blogging/ghost_writer_agent/agent.py
git commit -m "$(cat <<'EOF'
Migrate ghost_writer gap finder onto call_json_with_retry.

EOF
)"
```

---

### Task 4: Lint and coverage gate

**Files:**
- Verify only (no expected code changes unless lint/coverage forces a fix)

- [ ] **Step 1: Lint**

From `backend/`:

```bash
make lint
```

Expected: ruff check + format clean (exit 0). If format changes files, include them in the fix commit below.

- [ ] **Step 2: Coverage on touched modules**

```bash
.venv/bin/python -m pytest agents/blogging/tests/test_ghost_writer_and_more.py agents/blogging/tests/test_json_retry.py \
  --cov=agents.blogging.ghost_writer_agent.agent \
  --cov=agents.blogging.shared.json_retry \
  --cov-report=term-missing \
  --cov-fail-under=90
```

Expected: exit 0; ghost_writer agent line coverage ≥90% for exercised paths (pragma-marked interview loop remains excluded).

- [ ] **Step 3: Commit only if lint/format produced diffs; otherwise skip**

```bash
git add -u backend/agents/blogging
git commit -m "$(cat <<'EOF'
Fix lint after ghost_writer JSON-retry migration.

EOF
)"
```

If `git status` is clean, do not create an empty commit.

---

## Spec coverage checklist

| Spec item | Task |
|---|---|
| Migrate `_evaluate_sufficiency` | Task 2 |
| Migrate `_find_gaps_via_llm` | Task 3 |
| Leave `_compile_narrative` alone | Tasks 2–3 (no edits) |
| Fallbacks `default` / `[]` preserved | Tasks 2–3 |
| Immediate fallback on unexpected errors | Task 1 + 2–3 |
| Update exception tests | Task 1 |
| Keep happy/parse/exhausted tests | Tasks 2–3 (unchanged assertions) |
| No helper changes | Global constraint |
| Lint + 90% coverage | Task 4 |
