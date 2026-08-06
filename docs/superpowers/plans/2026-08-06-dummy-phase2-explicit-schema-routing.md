# Dummy Phase 2 Explicit Schema Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove Branding Phase 2 text-anchor fallback from `DummyLLMClient` so Phase 2 stubs route only via `structured_output_model` / Strands tool name.

**Architecture:** Keep `_branding_phase2_structured_output_stub` and the existing model/tool-name fast paths. Delete `_branding_phase2_text_routed_stub` and its `complete_json` call site. Retarget tests that previously relied on system-prompt substrings to pass an explicit Pydantic model class.

**Tech Stack:** Python 3.10+, pytest, Pydantic, `DummyLLMClient` in `llm_service`, branding Phase 2 `*Output` models.

**Worktree:** the feature worktree (`.worktrees/` checkout for the current feature branch).

**Spec:** `docs/superpowers/specs/2026-08-06-dummy-phase2-explicit-schema-routing-design.md`

## Global Constraints

- Scope is Phase 2 Narrative & Messaging stubs only — do not change Phase 1/3/4/5 text routing.
- No production agent factory changes in `branding_team/agents.py`.
- No new exception types for missing model identity.
- DbC docstrings required on any new/changed public helpers; never mention GitHub issue numbers in code/comments/docs (PR body only).
- Tests must cover modified behavior; related package tests must pass.
- Python path: use `/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest` from the worktree `backend/` directory.

## File Structure

| File | Responsibility |
|---|---|
| `backend/agents/llm_service/clients/dummy.py` | Delete Phase 2 text-anchor helper + call site; refresh comments that describe the fallback |
| `backend/agents/llm_service/tests/test_dummy_client.py` | Regression for no-model Phase 2 prompts; retarget Phase 2 cumulative/mutability/voice tests to `structured_output_model=` |
| `backend/agents/branding_team/tests/test_dummy_stub_alignment.py` | Pass `structured_output_model=output_model` so Phase 2 stubs still align without text routing |

No new source files.

---

### Task 1: Failing regression — no model means no Phase 2 stub

**Files:**
- Modify: `backend/agents/llm_service/tests/test_dummy_client.py` (add test near the existing `test_structured_output_model_routes_by_class_despite_misleading_prompt` block, after line ~105)
- Test: `backend/agents/llm_service/tests/test_dummy_client.py`

**Interfaces:**
- Consumes: `DummyLLMClient.complete_json(prompt, *, system_prompt=None, structured_output_model=None, ...)`
- Produces: `test_phase2_system_prompt_without_model_does_not_route_by_text_anchors` — documents the postcondition that Phase 2–looking prompts without an explicit model must not return Phase 2 keys

- [ ] **Step 1: Write the failing test**

Add this test immediately after `test_structured_output_model_routes_by_class_despite_misleading_prompt`:

```python
def test_phase2_system_prompt_without_model_does_not_route_by_text_anchors() -> None:
    """Phase 2 stubs must not be selected from system-prompt substrings alone.

    A MessageMapper-shaped system prompt (messaging_framework +
    audience_message_maps) previously returned a MessagingFrameworkOutput
    payload via text-anchor fallback. Without structured_output_model, that
    path must not fire — incidental field-name mentions must not choose a
    schema.
    """
    c = DummyLLMClient()
    j = c.complete_json(
        "go",
        system_prompt=(
            "messaging_framework and audience_message_maps for the messaging specialist"
        ),
        temperature=0.0,
    )
    assert "messaging_framework" not in j
    assert "audience_message_maps" not in j
    assert "brand_story" not in j
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd $WORKTREE/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/llm_service/tests/test_dummy_client.py::test_phase2_system_prompt_without_model_does_not_route_by_text_anchors -v
```

Expected: FAIL — assertion on `"messaging_framework" not in j` fails because the text-anchor fallback still returns the messaging stub.

- [ ] **Step 3: Commit the failing test**

```bash
cd $WORKTREE
git add backend/agents/llm_service/tests/test_dummy_client.py
git commit -m "$(cat <<'EOF'
Add failing regression for Phase 2 text-anchor routing.

EOF
)"
```

---

### Task 2: Delete Phase 2 text-anchor fallback

**Files:**
- Modify: `backend/agents/llm_service/clients/dummy.py`
  - Delete `_branding_phase2_text_routed_stub` (approx lines 1199–1241)
  - Delete the `elif (phase2_stub := _branding_phase2_text_routed_stub(...))` branch (approx lines 2244–2250)
  - Update comments in `complete_json` that still describe the fallback (approx lines 1611–1616 and any nearby docstring mentions of text-anchor fallback for Phase 2)

**Interfaces:**
- Consumes: `_branding_phase2_structured_output_stub(model_name: str) -> Optional[Dict[str, Any]]` (unchanged)
- Produces: `complete_json` no longer invokes any Phase 2 system-prompt substring matcher

- [ ] **Step 1: Delete `_branding_phase2_text_routed_stub`**

Remove the entire function from its `def` through the final `return None` (currently ~lines 1199–1241). Leave `_looks_like_structured_output_tool` and `class DummyLLMClient` adjacent with a single blank line between them.

- [ ] **Step 2: Remove the `complete_json` call site**

Replace the Phase 2 text-anchor block:

```python
        # Branding team — Phase 2 "Narrative & Messaging" Graph agents (built
        # with structured_output=, see agents.py). Text-anchor fallback lives
        # in _branding_phase2_text_routed_stub so this call site's branching
        # stays flat and under the mccabe complexity ceiling (mirrors
        # _branding_structured_stub for Phase 3/4/5 just below).
        elif (phase2_stub := _branding_phase2_text_routed_stub(system_lowered)) is not None:
            return phase2_stub
        # Branding Phase 3 / Phase 4 / Phase 5 stubs live in ``_branding_structured_stub``
```

with:

```python
        # Branding Phase 3 / Phase 4 / Phase 5 stubs live in ``_branding_structured_stub``
```

Phase 2 payloads are reached only via the earlier `structured_output_model` fast path (or `chat`/`stream` tool-name dispatch). Do not leave a dangling `elif` that referenced the deleted helper.

- [ ] **Step 3: Refresh `complete_json` comments / docstring**

In `complete_json`'s docstring and the long comment above the `structured_output_model` fast path, remove claims that `_branding_phase2_text_routed_stub` remains as a fallback. State clearly that Phase 2 requires `structured_output_model` (or Strands tool-name dispatch outside this method). Keep Phase 1/3/4/5 system-prompt anchoring notes unchanged.

Example replacement for the Phase 2 portion of that comment block:

```python
        # Branding Phase 1 / Phase 3 / Phase 4 / Phase 5 branches are the
        # exception: they anchor on ``system_prompt`` (via ``system_lowered``
        # later in this method) because every agent in those phases receives
        # the same serialized mission/phase context as its user message, so
        # only each agent's own system_prompt (its required output field names)
        # can distinguish which one is asking. Those anchors are multi-token
        # combinations unique to one agent's prompt. Phase 2 is routed only by
        # ``structured_output_model``'s class name (fast path below) or by
        # Strands StructuredOutputTool name in ``chat``/``stream`` — there is
        # no system-prompt substring fallback for Phase 2.
```

- [ ] **Step 4: Run the regression test — expect PASS**

```bash
cd $WORKTREE/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/llm_service/tests/test_dummy_client.py::test_phase2_system_prompt_without_model_does_not_route_by_text_anchors \
  agents/llm_service/tests/test_dummy_client.py::test_structured_output_model_routes_by_class_despite_misleading_prompt -v
```

Expected: both PASS.

- [ ] **Step 5: Confirm old text-routed Phase 2 tests now fail (optional sanity)**

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/llm_service/tests/test_dummy_client.py::test_branding_phase2_branches_return_cumulative_keys -v
```

Expected: FAIL (no `structured_output_model` → no Phase 2 keys). This failure is fixed in Task 3.

- [ ] **Step 6: Commit**

```bash
cd $WORKTREE
git add backend/agents/llm_service/clients/dummy.py
git commit -m "$(cat <<'EOF'
Remove Phase 2 dummy stub text-anchor fallback.

EOF
)"
```

---

### Task 3: Retarget `test_dummy_client` Phase 2 callers to explicit models

**Files:**
- Modify: `backend/agents/llm_service/tests/test_dummy_client.py`
  - `_BRANDING_PHASE2_SYSTEM_PROMPTS` / `test_branding_phase2_branches_return_cumulative_keys`
  - `test_branding_phase2_branch_results_do_not_share_mutable_state`
  - `test_voice_principles_branch_nests_editorial_quality_bar_in_writing_guidelines`
- Test: same file

**Interfaces:**
- Consumes: Phase 2 model classes from `branding_team.models`:
  `BrandStoryOutput`, `BrandArchetypesOutput`, `TaglineOutput`,
  `MessagingFrameworkOutput`, `PersonaProfilesOutput`, `WritingGuidelinesOutput`
- Produces: Phase 2 unit tests that pin cumulative stubs via `structured_output_model=`

- [ ] **Step 1: Rewrite the Phase 2 parametrized table**

Replace `_BRANDING_PHASE2_SYSTEM_PROMPTS` and its test with model-driven cases. System prompts are no longer the routing key; keep them only if useful as noise, or drop them.

```python
_BRANDING_PHASE2_MODEL_CASES = [
    (
        "BrandStoryOutput",
        {"brand_story", "hero_narrative", "boilerplate_variants"},
    ),
    (
        "BrandArchetypesOutput",
        {"brand_story", "hero_narrative", "boilerplate_variants", "brand_archetypes"},
    ),
    (
        "TaglineOutput",
        {
            "brand_story",
            "hero_narrative",
            "boilerplate_variants",
            "brand_archetypes",
            "tagline",
            "tagline_rationale",
            "elevator_pitches",
        },
    ),
    (
        "MessagingFrameworkOutput",
        {
            "brand_story",
            "hero_narrative",
            "boilerplate_variants",
            "brand_archetypes",
            "tagline",
            "tagline_rationale",
            "elevator_pitches",
            "messaging_framework",
            "audience_message_maps",
        },
    ),
    (
        "PersonaProfilesOutput",
        {
            "brand_story",
            "hero_narrative",
            "boilerplate_variants",
            "brand_archetypes",
            "tagline",
            "tagline_rationale",
            "elevator_pitches",
            "messaging_framework",
            "audience_message_maps",
            "persona_profiles",
        },
    ),
    (
        "WritingGuidelinesOutput",
        {
            "brand_story",
            "hero_narrative",
            "boilerplate_variants",
            "brand_archetypes",
            "tagline",
            "tagline_rationale",
            "elevator_pitches",
            "messaging_framework",
            "audience_message_maps",
            "persona_profiles",
            "writing_guidelines",
        },
    ),
]


@pytest.mark.parametrize("model_name,expected_keys", _BRANDING_PHASE2_MODEL_CASES)
def test_branding_phase2_branches_return_cumulative_keys(
    model_name: str, expected_keys: set[str]
) -> None:
    """Each Phase 2 branding specialist stub must carry forward exactly the
    keys its predecessors introduced, plus its own — pinned by explicit
    ``structured_output_model`` class name, not system-prompt substrings.
    """
    import branding_team.models as branding_models

    output_model = getattr(branding_models, model_name)
    c = DummyLLMClient()
    j = c.complete_json(
        "dummy prompt",
        temperature=0.0,
        structured_output_model=output_model,
    )
    assert set(j.keys()) == expected_keys
```

- [ ] **Step 2: Update the mutability test**

```python
def test_branding_phase2_branch_results_do_not_share_mutable_state() -> None:
    """Each ``complete_json`` call must hand back independent objects so
    mutating one response's nested lists/dicts can't leak into another call's
    response."""
    from branding_team.models import BrandStoryOutput

    c = DummyLLMClient()
    first = c.complete_json(
        "dummy prompt", temperature=0.0, structured_output_model=BrandStoryOutput
    )
    second = c.complete_json(
        "dummy prompt", temperature=0.0, structured_output_model=BrandStoryOutput
    )
    first["boilerplate_variants"].append("mutated")
    assert "mutated" not in second["boilerplate_variants"]
```

- [ ] **Step 3: Update the voice-principles nesting test**

```python
def test_voice_principles_branch_nests_editorial_quality_bar_in_writing_guidelines() -> None:
    """WritingGuidelinesOutput stub must nest ``editorial_quality_bar`` inside
    ``writing_guidelines``, not as a sibling top-level key.
    """
    from branding_team.models import WritingGuidelinesOutput

    c = DummyLLMClient()
    j = c.complete_json(
        "go",
        temperature=0.0,
        structured_output_model=WritingGuidelinesOutput,
    )
    assert isinstance(j["writing_guidelines"], dict)
    guidelines = j["writing_guidelines"]
    for field in ("voice_principles", "style_dos", "style_donts", "editorial_quality_bar"):
        assert len(guidelines[field]) == 3
    assert "editorial_quality_bar" not in j
```

- [ ] **Step 4: Run Phase 2–related dummy client tests**

```bash
cd $WORKTREE/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/llm_service/tests/test_dummy_client.py -q
```

Expected: all PASS (63+ tests; count may rise by 1 from Task 1).

- [ ] **Step 5: Commit**

```bash
cd $WORKTREE
git add backend/agents/llm_service/tests/test_dummy_client.py
git commit -m "$(cat <<'EOF'
Retarget Phase 2 dummy client tests to explicit models.

EOF
)"
```

---

### Task 4: Update branding stub-alignment suite

**Files:**
- Modify: `backend/agents/branding_team/tests/test_dummy_stub_alignment.py` (the `complete_json` call in `test_dummy_stub_matches_agent_output_model`, ~line 82)
- Test: same file

**Interfaces:**
- Consumes: `output_model` already parametrized per factory
- Produces: alignment checks that exercise Phase 2 via `structured_output_model=` while Phase 1 still falls through to system-prompt routing when the class name is not in `_PHASE2_STRUCTURED_OUTPUT_MODEL_NAMES`

- [ ] **Step 1: Pass `structured_output_model` in the alignment test**

Change:

```python
    result = DummyLLMClient().complete_json(prompt, system_prompt=agent.system_prompt)
```

to:

```python
    result = DummyLLMClient().complete_json(
        prompt,
        system_prompt=agent.system_prompt,
        structured_output_model=output_model,
    )
```

Passing Phase 1 models is safe: unrecognized names fall through to existing Phase 1 text anchors (same behavior as `test_unrecognized_structured_output_model_falls_back_to_text_routing`).

- [ ] **Step 2: Run the alignment suite**

```bash
cd $WORKTREE/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/branding_team/tests/test_dummy_stub_alignment.py -v
```

Expected: all parametrized cases PASS.

- [ ] **Step 3: Commit**

```bash
cd $WORKTREE
git add backend/agents/branding_team/tests/test_dummy_stub_alignment.py
git commit -m "$(cat <<'EOF'
Pass structured_output_model in branding stub alignment tests.

EOF
)"
```

---

### Task 5: Package verification and closeout

**Files:**
- Verify only (no intentional source edits unless ruff complains about leftovers)
- Confirm `rg '_branding_phase2_text_routed_stub'` returns no matches under `backend/`

**Interfaces:**
- Consumes: Tasks 1–4 deliverables
- Produces: green `llm_service` dummy + branding stub alignment evidence for the PR

- [ ] **Step 1: Grep for deleted symbol**

```bash
cd $WORKTREE
rg '_branding_phase2_text_routed_stub' backend/
```

Expected: no matches.

- [ ] **Step 2: Run full related suites + ruff on touched files**

```bash
cd $WORKTREE/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/llm_service/tests/test_dummy_client.py \
  agents/branding_team/tests/test_dummy_stub_alignment.py \
  agents/branding_team/tests/test_dummy_structured_output_contract.py -q
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/ruff check \
  agents/llm_service/clients/dummy.py \
  agents/llm_service/tests/test_dummy_client.py \
  agents/branding_team/tests/test_dummy_stub_alignment.py
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/ruff format --check \
  agents/llm_service/clients/dummy.py \
  agents/llm_service/tests/test_dummy_client.py \
  agents/branding_team/tests/test_dummy_stub_alignment.py
```

Expected: all tests PASS; ruff check/format clean.

- [ ] **Step 3: Commit only if Step 2 required drive-by lint fixes**

Otherwise skip — worktree should already be commit-clean from Tasks 1–4.

---

## Spec coverage self-check

| Spec requirement | Task |
|---|---|
| Delete `_branding_phase2_text_routed_stub` + call site | Task 2 |
| Keep model/tool-name routing | Task 2 (untouched keepers) |
| No Phase 2 substring sniffing without model | Task 1 + Task 2 |
| Retarget `test_dummy_client` Phase 2 tests | Task 3 |
| Update `test_dummy_stub_alignment` | Task 4 |
| Related lint/tests pass | Task 5 |
| Phases 1/3/4/5 unchanged | Global constraint + Task 2 comment refresh only |
| No production agent factory changes | Global constraint |

## Placeholder / consistency self-check

- No TBD/TODO placeholders.
- Model class names match `_PHASE2_STRUCTURED_OUTPUT_MODEL_NAMES` in `dummy.py`.
- Pytest commands use the main-repo `.venv` against the worktree `backend/` path.
