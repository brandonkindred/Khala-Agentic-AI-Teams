# Branding Phase 4/5 Stub-Routing Regression Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `test_dummy_client.py` so Phase 4/5 model-class stub routing has the same regression lock as Phase 2 (chat/stream tool-name routing plus focused `complete_json` cases).

**Architecture:** Replace the Phase-2-only name tuple with a Phase 2∪4∪5 `_MODEL_ROUTED_MODEL_NAMES` fixture; point chat/stream equality asserts at `_branding_structured_output_stub_by_model_name`; add three `complete_json` tests for cross-schema routing and `ChannelGuidelineOutput` channel extraction. Production code stays unchanged — the fix is already on `main`.

**Tech Stack:** pytest, `DummyLLMClient`, branding_team Pydantic models.

**Spec:** `docs/superpowers/specs/2026-08-07-branding-phase4-5-regression-tests-design.md`

## Global Constraints

- Touch only `backend/agents/llm_service/tests/test_dummy_client.py` for code (plus design/plan docs).
- Do not modify `dummy.py`, branding agents, or `test_dummy_structured_output_contract.py`.
- Assert against `_branding_structured_output_stub_by_model_name`, not the Phase-2-only helper.
- Never reference GitHub issue numbers in new test comments or commit messages.
- Work exclusively in `.worktrees/5559-branding-phase4-5-regression-tests` on branch `test/5559-branding-phase4-5-regression-tests`.
- Run tests with main-repo venv from worktree `backend/`:
  `/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest …`
- The production fix is already merged: new tests are expected to **PASS on first run** (characterization / regression lock). Do not temporarily break production code to force RED.

---

## File map

| File | Responsibility |
|---|---|
| `backend/agents/llm_service/tests/test_dummy_client.py` | Shared routed-name fixture, widened chat/stream tests, new complete_json tests |

No new modules.

---

### Task 1: Add complete_json cross-schema and channel-extraction tests

**Files:**
- Modify: `backend/agents/llm_service/tests/test_dummy_client.py` (near the existing `test_structured_output_model_routes_by_class_despite_misleading_prompt` block ~lines 80–105)

**Interfaces:**
- Consumes: `DummyLLMClient.complete_json`, `branding_team.models.OwnershipOutput`, `branding_team.models.ChannelGuidelineOutput`
- Produces: three new test functions listed below

- [ ] **Step 1: Add the three tests**

Insert after `test_structured_output_model_routes_by_class_despite_misleading_prompt` (before `test_phase2_system_prompt_without_model_does_not_route_by_text_anchors`):

```python
def test_phase45_structured_output_model_wins_over_channel_guide_prompt() -> None:
    """Model-class routing must ignore Phase 4 channel-guide prompt anchors.

    A system prompt that would text-route to ChannelGuidelineOutput must still
    yield the OwnershipOutput stub when that class is passed as
    structured_output_model.
    """
    from branding_team.models import OwnershipOutput

    c = DummyLLMClient()
    misleading_system_prompt = (
        "Define content_types and frequency_guidance for channel: 'website'."
    )
    j = c.complete_json(
        "go",
        system_prompt=misleading_system_prompt,
        temperature=0.0,
        structured_output_model=OwnershipOutput,
    )
    assert "ownership_model" in j
    assert "decision_authority" in j
    assert "channel" not in j
    assert "content_types" not in j
    OwnershipOutput.model_validate(j)


def test_channel_guideline_model_extracts_channel_from_system_prompt() -> None:
    """ChannelGuidelineOutput model routing fills channel from the prompt."""
    from branding_team.models import ChannelGuidelineOutput

    c = DummyLLMClient()
    j = c.complete_json(
        "go",
        system_prompt=(
            "You are a Website Channel Specialist. channel: 'website'\n"
            "Define content_types and frequency_guidance."
        ),
        temperature=0.0,
        structured_output_model=ChannelGuidelineOutput,
    )
    assert j["channel"] == "website"
    ChannelGuidelineOutput.model_validate(j)


def test_channel_guideline_model_defaults_channel_when_absent() -> None:
    """ChannelGuidelineOutput model routing defaults channel when prompt has none."""
    from branding_team.models import ChannelGuidelineOutput

    c = DummyLLMClient()
    j = c.complete_json(
        "go",
        system_prompt="Return channel guidelines without a quoted channel id.",
        temperature=0.0,
        structured_output_model=ChannelGuidelineOutput,
    )
    assert j["channel"] == "channel"
    ChannelGuidelineOutput.model_validate(j)
```

- [ ] **Step 2: Run the three new tests (expect PASS — fix already on main)**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/5559-branding-phase4-5-regression-tests/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/llm_service/tests/test_dummy_client.py::test_phase45_structured_output_model_wins_over_channel_guide_prompt \
  agents/llm_service/tests/test_dummy_client.py::test_channel_guideline_model_extracts_channel_from_system_prompt \
  agents/llm_service/tests/test_dummy_client.py::test_channel_guideline_model_defaults_channel_when_absent \
  -v
```

Expected: 3 passed.

- [ ] **Step 3: Commit**

```bash
git add backend/agents/llm_service/tests/test_dummy_client.py
git commit -m "$(cat <<'EOF'
Add complete_json regression tests for Phase 4/5 model-class routing.

EOF
)"
```

---

### Task 2: Widen chat/stream parametrization to Phase 2∪4∪5

**Files:**
- Modify: `backend/agents/llm_service/tests/test_dummy_client.py`
  - imports (~lines 20–32)
  - `_PHASE2_ROUTED_MODEL_NAMES` block and the two parametrized tests (~747–862)

**Interfaces:**
- Consumes: `_branding_structured_output_stub_by_model_name(model_name: str, system_lowered: str = "") -> Optional[Dict[str, Any]]`
- Produces: `_MODEL_ROUTED_MODEL_NAMES: tuple[str, ...]` used by chat/stream tests

- [ ] **Step 1: Update imports**

In the `from llm_service.clients.dummy import (` block, replace
`_branding_phase2_structured_output_stub` with
`_branding_structured_output_stub_by_model_name`.

- [ ] **Step 2: Replace the name tuple and update chat/stream tests**

Replace `_PHASE2_ROUTED_MODEL_NAMES` and the two parametrized tests' bodies/docstrings as follows:

```python
_MODEL_ROUTED_MODEL_NAMES: tuple[str, ...] = (
    # Phase 2 — Narrative & Messaging
    "BrandStoryOutput",
    "BrandArchetypesOutput",
    "TaglineOutput",
    "MessagingFrameworkOutput",
    "PersonaProfilesOutput",
    "WritingGuidelinesOutput",
    # Phase 4 — Experience & Channel Activation
    "BrandExperiencePrinciplesOutput",
    "ChannelGuidelineOutput",
    "BrandArchitectureOutput",
    "BrandInActionOutput",
    # Phase 5 — Governance & Evolution
    "OwnershipOutput",
    "ApprovalWorkflowsOutput",
    "AssetWikiOutput",
    "TrainingOnboardingOutput",
    "BrandHealthKPIsOutput",
    "EvolutionFrameworkOutput",
    "BrandGuidelinesOutput",
)


@pytest.mark.parametrize("model_name", _MODEL_ROUTED_MODEL_NAMES)
def test_chat_routes_structured_output_tool_by_name_despite_misleading_prompt(
    model_name: str,
) -> None:
    """chat() must route by the tool's name, not by scanning the user prompt,
    for every model-routed branding class. Asserts exact equality against
    _branding_structured_output_stub_by_model_name's output rather than a
    hand-picked subset of keys.
    """
    c = DummyLLMClient()
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Please respond with the requested output."},
    ]
    result = c.chat(messages, tools=_openai_structured_output_tools(model_name))
    args = result["__tool_calls__"][0]["function"]["arguments"]
    assert args == _branding_structured_output_stub_by_model_name(model_name)


@pytest.mark.asyncio
@pytest.mark.parametrize("model_name", _MODEL_ROUTED_MODEL_NAMES)
async def test_stream_routes_structured_output_tool_by_name_despite_misleading_prompt(
    model_name: str,
) -> None:
    """stream() must route by tool_specs' name, not by scanning the user text,
    for every model-routed branding class — mirrors the chat() test above,
    including asserting exact equality rather than a hand-picked key subset.
    """
    c = DummyLLMClient()
    messages = _as_stream_messages(
        [{"role": "user", "content": [{"text": "You are a helpful assistant."}]}]
    )
    tool_specs = cast(
        Any,
        [
            {
                "name": model_name,
                "description": "IMPORTANT: This StructuredOutputTool should only be invoked...",
                "inputSchema": {"json": {"type": "object", "properties": {}}},
            }
        ],
    )
    chunks: list[str] = []
    async for event in c.stream(messages, tool_specs=tool_specs):
        delta = (event.get("contentBlockDelta") or {}).get("delta") or {}
        tool_input = (delta.get("toolUse") or {}).get("input")
        if tool_input:
            chunks.append(tool_input)
    data = json.loads(chunks[0])
    assert data == _branding_structured_output_stub_by_model_name(model_name)
```

Also update the section comment above `_openai_structured_output_tools` (~720–722) so it no longer says the fragility is Phase-2-only / “issue #…” — describe model-name routing for branding structured-output tools without issue numbers.

Ensure no remaining references to `_PHASE2_ROUTED_MODEL_NAMES` or `_branding_phase2_structured_output_stub` remain in this file (`rg` to confirm).

Leave `test_chat_unrecognized_tool_name_falls_back_to_text_scan` and
`test_stream_unrecognized_tool_name_falls_back_to_text_scan` bodies unchanged.

- [ ] **Step 3: Run the widened parametrized tests**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/5559-branding-phase4-5-regression-tests/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/llm_service/tests/test_dummy_client.py::test_chat_routes_structured_output_tool_by_name_despite_misleading_prompt \
  agents/llm_service/tests/test_dummy_client.py::test_stream_routes_structured_output_tool_by_name_despite_misleading_prompt \
  -v
```

Expected: 34 passed (17 model names × 2 tests).

- [ ] **Step 4: Commit**

```bash
git add backend/agents/llm_service/tests/test_dummy_client.py
git commit -m "$(cat <<'EOF'
Widen chat/stream routing tests across Phase 2/4/5 model names.

EOF
)"
```

---

### Task 3: Full suite verification

**Files:**
- Verify: `backend/agents/llm_service/tests/test_dummy_client.py`

**Interfaces:** none new

- [ ] **Step 1: Run the full dummy client suite**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/5559-branding-phase4-5-regression-tests/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/llm_service/tests/test_dummy_client.py \
  -q
```

Expected: all passed (prior count was 64; expect +3 new tests and +22 new parametrizations from widening 6→17 on two tests → roughly 64 − 12 + 34 + 3 = 89, or compute from pytest summary). Use the pytest summary line as ground truth; zero failures.

- [ ] **Step 2: Lint the touched test file**

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/ruff check agents/llm_service/tests/test_dummy_client.py
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/ruff format --check agents/llm_service/tests/test_dummy_client.py
```

Expected: clean. Format if needed and commit separately:

```bash
git add backend/agents/llm_service/tests/test_dummy_client.py
git commit -m "$(cat <<'EOF'
Format Phase 4/5 routing regression tests.

EOF
)"
```

- [ ] **Step 3: Final status**

```bash
git status -sb
git log --oneline origin/main..HEAD
```

Confirm only the intended commits are present.

---

## Plan self-review

1. **Spec coverage:** Shared fixture, widened chat/stream, complete_json cross-schema, channel present/absent, no production changes, no contract-suite edits — all mapped to Tasks 1–3.
2. **Placeholders:** none.
3. **Type consistency:** `_MODEL_ROUTED_MODEL_NAMES` and `_branding_structured_output_stub_by_model_name` used consistently; Phase-2-only helper removed from imports.
