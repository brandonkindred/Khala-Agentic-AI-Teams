# Migrate Remaining Blogging Tests to `make_writer_agent()` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `make_writer_agent()` the default construction path for `BlogWriterAgent` across the remaining blogging test files (issue #2098).

**Architecture:** Behavior-preserving mechanical migration. Replace direct `BlogWriterAgent(...)` call sites with `make_writer_agent(...)` using the existing factory kwargs. Keep one intentional direct construction (`llm_client=None` validation) with a one-line comment. Do not change the factory signature.

**Tech Stack:** Python 3.10+, pytest, existing `backend/agents/blogging/tests/conftest.py` helpers.

**Spec:** `docs/superpowers/specs/2026-07-24-migrate-make-writer-agent-design.md`

## Global Constraints

- Work only in `.worktrees/refactor-2098-make-writer-agent` on branch `refactor/2098-migrate-make-writer-agent`.
- Do not modify `make_writer_agent()` unless a call site proves an optional override is required (expected: no change).
- Out of scope: `test_blog_writer_agent.py`, stub-class replacement in `test_run_pipeline_minimal.py` / `test_agent_workflow.py`, `_api_test_utils.py` / `_content_plan_test_utils.py`.
- Do not mention tracker issue numbers in source comments or commit messages (PR body may use `Closes #2098`).
- Preserve exact style/brand strings and custom `llm_client` values at each site.
- For sites that previously used constructor defaults (empty guidelines), pass `writing_style_guide_content=""` and `brand_spec_content=""` explicitly — do not silently switch to factory defaults `"Style"` / `"Brand"`.
- Import style: `from .conftest import make_writer_agent` (match `test_writer_run.py`).
- Run pytest from `backend/agents` (CI cwd for the blogging matrix entry).

---

## File map

| File | Change |
|---|---|
| `backend/agents/blogging/tests/test_writer_plan_content.py` | Replace `_agent_with_guidelines` body with factory |
| `backend/agents/blogging/tests/test_planning_loop_parity.py` | Replace construction; drop unused `BlogWriterAgent` import if unused |
| `backend/agents/blogging/tests/test_blog_publication_agent.py` | Replace 2 constructions |
| `backend/agents/blogging/tests/test_blog_writer_agent_revise.py` | Replace 1 construction with custom LLM |
| `backend/agents/blogging/tests/test_blog_writer_agent_integration.py` | Replace 1 construction with live client |
| `backend/agents/blogging/tests/test_writer_and_v2_helpers.py` | Migrate 4 sites; keep `llm_client=None` direct + comment |
| `backend/agents/blogging/tests/conftest.py` | Unchanged (factory body is the allowed remaining `BlogWriterAgent(`) |

---

### Task 1: Migrate plan-content helper and planning-loop parity

**Files:**
- Modify: `backend/agents/blogging/tests/test_writer_plan_content.py`
- Modify: `backend/agents/blogging/tests/test_planning_loop_parity.py`
- Test: those two modules

**Interfaces:**
- Consumes: `make_writer_agent(*, llm_client=None, writing_style_guide_content="Style", brand_spec_content="Brand")`
- Produces: Both files construct writers only via the factory

- [ ] **Step 1: Replace `_agent_with_guidelines` in `test_writer_plan_content.py`**

Replace the helper (currently constructs `BlogWriterAgent` + `DummyLLMClient` directly) with:

```python
def _agent_with_guidelines():
    from .conftest import make_writer_agent

    return make_writer_agent()
```

Remove now-unused imports of `BlogWriterAgent` / `DummyLLMClient` from this helper path if they are no longer referenced elsewhere in the file (they currently exist only inside the helper — delete those imports with the old body).

- [ ] **Step 2: Replace construction in `test_planning_loop_parity.py`**

Change the writer construction block to:

```python
from .conftest import make_writer_agent

# ... inside test_planning_agent_and_writer_agent_produce_equivalent_plan:
    planning_result = BlogPlanningAgent(DummyLLMClient()).run(inp, length_policy=policy)
    writer_result = make_writer_agent(
        writing_style_guide_content="x",
        brand_spec_content="y",
    ).plan_content(inp, length_policy=policy)
```

Remove the unused `from agents.blogging.blog_writer_agent.agent import BlogWriterAgent` import. Keep `DummyLLMClient` (still used for `BlogPlanningAgent`). Prefer a top-of-file import of `make_writer_agent` for consistency with this file's existing top-level imports.

- [ ] **Step 3: Run the two modules**

```bash
cd backend/agents
pytest blogging/tests/test_writer_plan_content.py blogging/tests/test_planning_loop_parity.py -v --tb=short -m "not integration"
```

Expected: all tests PASS.

- [ ] **Step 4: Commit**

```bash
git add backend/agents/blogging/tests/test_writer_plan_content.py \
        backend/agents/blogging/tests/test_planning_loop_parity.py
git commit -m "$(cat <<'EOF'
Migrate plan-content and planning-loop tests onto make_writer_agent().

EOF
)"
```

---

### Task 2: Migrate publication, revise, and integration constructions

**Files:**
- Modify: `backend/agents/blogging/tests/test_blog_publication_agent.py`
- Modify: `backend/agents/blogging/tests/test_blog_writer_agent_revise.py`
- Modify: `backend/agents/blogging/tests/test_blog_writer_agent_integration.py`
- Test: those three modules

**Interfaces:**
- Consumes: same `make_writer_agent` signature; `llm_client=` override for revise + integration
- Produces: No remaining direct `BlogWriterAgent(` in these three files

- [ ] **Step 1: Migrate `test_blog_publication_agent.py` (two sites)**

Add near other imports:

```python
from .conftest import make_writer_agent
```

Replace site ~84:

```python
    draft_agent = make_writer_agent(
        writing_style_guide_content="Use clear sentence flow and plain language.",
        brand_spec_content="Brand voice: practical and trustworthy.",
    )
```

Replace site ~163:

```python
    draft_agent = make_writer_agent(
        writing_style_guide_content="clear",
        brand_spec_content="brand",
    )
```

Keep `BlogWriterAgent` import if still required for `monkeypatch.setattr(BlogWriterAgent, "revise", ...)` in `test_revision_loop_stops_after_editor_approval`. Keep `DummyLLMClient` for publication/editor agents.

- [ ] **Step 2: Migrate `test_blog_writer_agent_revise.py`**

Add:

```python
from .conftest import make_writer_agent
```

Replace the construction in `test_revise_generates_plan_then_applies_all_feedback`:

```python
    agent = make_writer_agent(
        llm_client=llm,
        writing_style_guide_content="Use short paragraphs.",
        brand_spec_content="Brand voice: practical and direct.",
    )
```

Remove `BlogWriterAgent` from the import if unused afterward (`ReviseWriterInput` stays). Keep `DummyLLMClient` for `_ReviseTrackingLLM`.

- [ ] **Step 3: Migrate `test_blog_writer_agent_integration.py`**

Add:

```python
from .conftest import make_writer_agent
```

Replace the live-client construction:

```python
    agent = make_writer_agent(
        llm_client=client,
        writing_style_guide_content=(
            "Clear, conversational prose: full thoughts in natural-length sentences (~8th grade). "
            "No em dashes. Define terms on first use."
        ),
        brand_spec_content="Brand voice: practical, direct, and transparent.",
    )
```

Remove `BlogWriterAgent` from imports if unused (`WriterInput` / `WriterOutput` stay).

- [ ] **Step 4: Run the three modules (skip live integration if unmarked)**

```bash
cd backend/agents
pytest blogging/tests/test_blog_publication_agent.py \
       blogging/tests/test_blog_writer_agent_revise.py \
       blogging/tests/test_blog_writer_agent_integration.py \
       -v --tb=short -m "not integration"
```

Expected: unit tests PASS; the Ollama integration test SKIP (or PASS if a live provider is configured — either is fine).

- [ ] **Step 5: Commit**

```bash
git add backend/agents/blogging/tests/test_blog_publication_agent.py \
        backend/agents/blogging/tests/test_blog_writer_agent_revise.py \
        backend/agents/blogging/tests/test_blog_writer_agent_integration.py
git commit -m "$(cat <<'EOF'
Migrate publication, revise, and integration writer constructions to make_writer_agent().

EOF
)"
```

---

### Task 3: Migrate `test_writer_and_v2_helpers.py` (including intentional exception)

**Files:**
- Modify: `backend/agents/blogging/tests/test_writer_and_v2_helpers.py`
- Test: that module

**Interfaces:**
- Consumes: `make_writer_agent` with empty guidelines and custom style/brand
- Produces: Exactly one remaining direct `BlogWriterAgent(llm_client=None)` with an intentional-construction comment

- [ ] **Step 1: Add factory import**

Near the top of the writer-helper section (or module top), ensure:

```python
from .conftest import make_writer_agent
```

Keep `BlogWriterAgent` imported only for the `llm_client=None` validation test.

- [ ] **Step 2: Migrate `test_writer_agent_requires_guidelines`**

```python
def test_writer_agent_requires_guidelines() -> None:
    # Empty guidelines preserve constructor-default behavior (factory defaults are non-empty).
    agent = make_writer_agent(writing_style_guide_content="", brand_spec_content="")
    with pytest.raises(ValueError, match="brand"):
        agent._assert_guidelines_present()
```

- [ ] **Step 3: Keep intentional direct construction for None LLM**

```python
def test_writer_agent_assertion_on_none_llm() -> None:
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    # Direct construction intentional: make_writer_agent() replaces None with DummyLLMClient.
    with pytest.raises(AssertionError):
        BlogWriterAgent(llm_client=None)
```

- [ ] **Step 4: Migrate `test_writer_agent_style_prompt_merge`**

```python
def test_writer_agent_style_prompt_merge() -> None:
    a = make_writer_agent(
        writing_style_guide_content="Style A",
        brand_spec_content="Brand B",
    )
    assert "BRAND SPEC" in a._style_prompt
    assert "Brand B" in a._style_prompt
    assert "WRITING STYLE GUIDE" in a._style_prompt
```

- [ ] **Step 5: Migrate deterministic self-check tests (preserve empty guidelines)**

```python
def test_writer_agent_deterministic_self_check() -> None:
    a = make_writer_agent(writing_style_guide_content="", brand_spec_content="")
    bad = (
        "In today's fast-paced world—as we navigate change. "
        "Studies show this works.\n\n"
        "Word. Word. Word.\n"
    )
    out = a._deterministic_self_check(bad)
    joined = "\n".join(out)
    assert "Em/en dash" in joined
    assert "Banned phrase" in joined
    assert "Vague citation" in joined or "Reader address" in joined


def test_writer_agent_deterministic_self_check_clean_draft() -> None:
    a = make_writer_agent(writing_style_guide_content="", brand_spec_content="")
    clean = (
        "# Header\n\n"
        "You are reading something. You will learn. You will see. "
        "We covered tracing, costs, and evaluation in real workloads.\n\n"
        "These are pragmatic and useful for shipping high-quality software in your team's stack.\n"
    )
    out = a._deterministic_self_check(clean)
    assert isinstance(out, list)
```

Remove unused `DummyLLMClient` imports from these migrated tests if no longer needed in the writer-agent section (v2 helpers below may still need other imports — only remove what becomes unused).

- [ ] **Step 6: Run the module**

```bash
cd backend/agents
pytest blogging/tests/test_writer_and_v2_helpers.py -v --tb=short -m "not integration"
```

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/agents/blogging/tests/test_writer_and_v2_helpers.py
git commit -m "$(cat <<'EOF'
Migrate writer helper tests onto make_writer_agent(), keep None-LLM validation direct.

EOF
)"
```

---

### Task 4: Acceptance grep + full blogging suite coverage

**Files:**
- Verify only (no production edits expected)
- Test: `blogging/tests/`

**Interfaces:**
- Consumes: all prior task migrations
- Produces: Acceptance criteria satisfied

- [ ] **Step 1: Enumerate remaining `BlogWriterAgent(` sites**

```bash
cd backend/agents
rg -n "BlogWriterAgent\(" blogging/tests/
```

Expected remaining hits only:
1. `blogging/tests/conftest.py` — inside `make_writer_agent`
2. `blogging/tests/test_writer_and_v2_helpers.py` — `BlogWriterAgent(llm_client=None)` with the intentional comment
3. Optionally constructions already on `make_writer_agent` inside `test_blog_writer_agent.py` should **not** appear as `BlogWriterAgent(` (that file is already migrated)

If any other direct construction remains, migrate it the same way (or add an intentional comment) before continuing.

- [ ] **Step 2: Run full blogging unit suite with coverage gate**

```bash
cd backend/agents
pytest blogging/tests/ -v --tb=short -n 4 -m "not integration" \
  --cov=blogging --cov-report=term-missing --cov-fail-under=90
```

Expected: exit code 0; coverage ≥ 90%.

- [ ] **Step 3: Final commit only if Step 1–2 required extra fixes; otherwise stop**

If Step 1–2 required additional edits, commit them:

```bash
git add backend/agents/blogging/tests/
git commit -m "$(cat <<'EOF'
Finish make_writer_agent migration leftovers and confirm blogging coverage gate.

EOF
)"
```

If no further edits, do not create an empty commit.

---

## Spec coverage self-check

| Spec requirement | Task |
|---|---|
| Migrate remaining direct constructions to `make_writer_agent()` | Tasks 1–3 |
| One-line comment for intentional direct construction | Task 3 Step 3 |
| Do not extend factory unless needed | Global Constraints + no conftest edit |
| Full blogging suite passes with coverage ≥ baseline (90%) | Task 4 |
| Out-of-scope files untouched | Global Constraints |
| Empty-guidelines sites preserve empty strings | Task 3 Steps 2/5 |
