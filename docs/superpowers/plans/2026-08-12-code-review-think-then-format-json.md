# Code Review Think-Then-Format JSON Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every `code_review_agent` LLM path whose outcome is structured JSON becomes two requests: a max-thinking text review (tools allowed), then a thinking-off JSON formatting call (no tools).

**Architecture:** Add a code-review-local `via_reasoning.py` twin of `llm_service`'s via-reasoning helpers (with `reasoning_think` override support). Split review prompts so JSON contracts move to the formatting call. Migrate chunk review (`complete_validated`), then Strands Agent paths (`submission_pass_runner`, false-positive filter, synthesis).

**Tech Stack:** Python 3.10+, Pydantic v2, `llm_service` (`complete` / `complete_validated` / `LLMClientModel`), Strands `Agent`, pytest

**Spec:** `docs/superpowers/specs/2026-08-12-code-review-think-then-format-json-design.md`

## Global Constraints

- Follow the approved design spec exactly (local wrapper only — do **not** modify `llm_service.complete_json_via_reasoning` / `complete_validated_via_reasoning`)
- Design-by-Contract docstrings (`Preconditions:` / `Postconditions:` / `Invariants:` where relevant) on every new public function/method/module
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only)
- Ruff line-length 120; Python 3.10 target
- Coverage ≥ 90% on new/changed code
- No env kill-switch in v1
- Thinking-off retry keeps the two-call split with `reasoning_think=False`

## File map

| File | Role |
|---|---|
| `code_review_agent/via_reasoning.py` | **Create** — delimiter wrap, untrusted guard, `complete_validated_via_reasoning_local`, `run_agent_via_reasoning` |
| `code_review_agent/profiles.py` | Split `build_review_system_prompt` into reasoning vs formatting pieces; keep a composed full prompt only if tests still need it |
| `code_review_agent/chunk_reviewer.py` | Use local validated via-reasoning; drop `FINAL_OUTPUT_CONTRACT_NOTE` from reasoning user prompt |
| `code_review_agent/model_resolution.py` | Add `response_format` to resolve helpers (or document that `via_reasoning` clones) so Agent call 1 is text-mode |
| `code_review_agent/submission_pass_runner.py` | `_call_agent` → `run_agent_via_reasoning` |
| `code_review_agent/false_positive_filter.py` | `_verify_group` → `run_agent_via_reasoning`; split FPF prompt JSON tail |
| `code_review_agent/synthesis.py` | Both synthesize helpers → `run_agent_via_reasoning` |
| `code_review_agent/prompts.py` | Split JSON output sections out of reasoning system prompts for FPF / architecture / side-effect / synthesis |
| Tests under `software_engineering_team/tests/` | New `test_via_reasoning.py`; update chunk / FPF / synthesis / profile / submission-pass tests |

---

### Task 1: Local `via_reasoning` helpers + unit tests

**Files:**
- Create: `backend/agents/software_engineering_team/code_review_agent/via_reasoning.py`
- Create: `backend/agents/software_engineering_team/tests/test_via_reasoning.py`

**Interfaces:**
- Consumes: `llm_service.complete_validated`, `LLMClient.complete`, `LLMClient.complete_json` (indirectly), Strands `Agent`, `LLMClientModel.clone` when available
- Produces:
  - `wrap_with_analysis_delimiters(prose: str) -> str`
  - `formatting_system_prompt_with_untrusted_guard(formatting_system_prompt: str | None) -> str`
  - `complete_validated_via_reasoning_local(client, *, schema: type[T], reasoning_prompt: str, reasoning_system_prompt: str | None, objective: str, formatting_instructions: str, formatting_system_prompt: str | None = None, reasoning_think: bool | str | None = True, reasoning_temperature: float = 0.3, temperature: float = 0.0, correction_attempts: int = 1, **kwargs) -> T`
  - `run_agent_via_reasoning(*, model, reasoning_prompt: str, reasoning_system_prompt: str, formatting_instructions: str, parse: Callable[[str], T], tools: list | None = None, reasoning_think: bool | str | None = True, formatting_system_prompt: str | None = None, agent_key: str = "code_review") -> T`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_via_reasoning.py` modeled on `llm_service/tests/test_structured_via_reasoning.py`’s `_RecordingClient` pattern, but allow `think` on the reasoning call:

```python
"""Tests for code_review_agent.via_reasoning two-call split."""

from __future__ import annotations

from typing import Any, Optional

import pytest
from pydantic import BaseModel

from llm_service.interface import LLMClient, LLMPermanentError
from software_engineering_team.code_review_agent.via_reasoning import (
    complete_validated_via_reasoning_local,
    formatting_system_prompt_with_untrusted_guard,
    wrap_with_analysis_delimiters,
)


class _Out(BaseModel):
    approved: bool
    summary: str


class _RecordingClient(LLMClient):
    def __init__(
        self,
        json_response: Optional[dict[str, Any]] = None,
        *,
        prose: str = "REVIEW PROSE",
        complete_error: Optional[Exception] = None,
    ) -> None:
        self._json_response = json_response if json_response is not None else {
            "approved": True,
            "summary": "ok",
        }
        self._prose = prose
        self._complete_error = complete_error
        self.reasoning_calls: list[dict[str, Any]] = []
        self.format_calls: list[dict[str, Any]] = []
        self.order: list[str] = []

    def complete(
        self,
        prompt: str,
        *,
        objective: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: "bool | str | None" = None,
    ) -> str:
        self.order.append("complete")
        self.reasoning_calls.append(
            {
                "prompt": prompt,
                "objective": objective,
                "system_prompt": system_prompt,
                "temperature": temperature,
                "think": think,
                "tools": tools,
            }
        )
        if self._complete_error is not None:
            raise self._complete_error
        return self._prose

    def complete_json(
        self,
        prompt: str,
        *,
        objective: str,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: "bool | str | None" = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.order.append("complete_json")
        self.format_calls.append(
            {
                "prompt": prompt,
                "objective": objective,
                "system_prompt": system_prompt,
                "temperature": temperature,
                "think": think,
                "tools": tools,
                "kwargs": kwargs,
            }
        )
        return self._json_response


def test_wrap_delimiters_include_prose_and_random_boundary() -> None:
    wrapped = wrap_with_analysis_delimiters("hello findings")
    assert "hello findings" in wrapped
    assert "ANALYSIS" in wrapped
    assert "END ANALYSIS" in wrapped


def test_untrusted_guard_appended() -> None:
    out = formatting_system_prompt_with_untrusted_guard("Format JSON.")
    assert out.startswith("Format JSON.")
    assert "untrusted data" in out.lower()


def test_validated_via_reasoning_sequences_reason_then_format() -> None:
    client = _RecordingClient()
    result = complete_validated_via_reasoning_local(
        client,
        schema=_Out,
        reasoning_prompt="Review this code",
        reasoning_system_prompt="You are a reviewer. Answer in prose.",
        formatting_instructions='Return {"approved": bool, "summary": str}',
        objective="review code chunk",
    )
    assert result.approved is True
    assert client.order == ["complete", "complete_json"]
    assert client.reasoning_calls[0]["think"] is True
    assert client.format_calls[0]["think"] is False
    assert "REVIEW PROSE" in client.format_calls[0]["prompt"]
    assert "Return {" in client.format_calls[0]["prompt"]


def test_validated_via_reasoning_honors_reasoning_think_false() -> None:
    client = _RecordingClient()
    complete_validated_via_reasoning_local(
        client,
        schema=_Out,
        reasoning_prompt="Review this code",
        reasoning_system_prompt="Prose only.",
        formatting_instructions="JSON shape here",
        objective="review code chunk",
        reasoning_think=False,
    )
    assert client.reasoning_calls[0]["think"] is False
    assert client.format_calls[0]["think"] is False


def test_validated_via_reasoning_step_one_failure_skips_format() -> None:
    client = _RecordingClient(complete_error=LLMPermanentError("boom"))
    with pytest.raises(LLMPermanentError, match="boom"):
        complete_validated_via_reasoning_local(
            client,
            schema=_Out,
            reasoning_prompt="Review this code",
            reasoning_system_prompt="Prose only.",
            formatting_instructions="JSON shape here",
            objective="review code chunk",
        )
    assert client.order == ["complete"]
    assert client.format_calls == []
```

Also add a focused `run_agent_via_reasoning` test that monkeypatches `Agent` to record `tools` / model config and asserts call 2 has `tools=[]` (or no tools kwarg). Keep that test in the same file once the function exists; if preferred, write it in Step 1 as well and expect import failure until Step 3.

- [ ] **Step 2: Run tests to verify they fail**

Run from `backend/`:

```bash
cd backend && python -m pytest agents/software_engineering_team/tests/test_via_reasoning.py -v
```

Expected: FAIL with `ModuleNotFoundError` / import error for `via_reasoning`.

- [ ] **Step 3: Implement `via_reasoning.py`**

Implement the module. Duplicate the small delimiter/guard helpers from `llm_service.structured` (do not import private `_wrap_*`). Core of `complete_validated_via_reasoning_local`:

```python
prose = client.complete(
    reasoning_prompt,
    objective=f"{objective} (reasoning)",
    system_prompt=reasoning_system_prompt,
    temperature=reasoning_temperature,
    think=True if reasoning_think is None else reasoning_think,
)
format_prompt = (
    f"{_DEFAULT_FORMAT_INSTRUCTIONS}\n\n{formatting_instructions}\n\n"
    f"{wrap_with_analysis_delimiters(prose)}"
)
return complete_validated(
    client,
    format_prompt,
    schema=schema,
    objective=f"{objective} (format)",
    system_prompt=formatting_system_prompt_with_untrusted_guard(formatting_system_prompt),
    temperature=temperature,
    correction_attempts=correction_attempts,
    think=False,
    **kwargs,
)
```

For `run_agent_via_reasoning`:

1. Resolve a **text-mode** model for call 1:
   - If `model` is `LLMClientModel` with `.clone`, use `model.clone(response_format="text", think=...)` when clone accepts those kwargs; else `get_strands_model(agent_key, response_format="text", think=..., client=underlying)`.
   - If `model` is a bare `LLMClient`, wrap with `get_strands_model(agent_key, client=model, response_format="text", think=...)`.
   - If injected test Model has no clone, pass through (tests that need format control should inject clonable doubles).
2. `Agent(model=text_model, system_prompt=reasoning_system_prompt, tools=tools or [])`; `prose = str(agent(reasoning_prompt)).strip()`.
3. Format call: if an `LLMClient` is available (`isinstance(model, LLMClient)` or `getattr(model, "client", None)` / `_client`), call `complete_json` with `think=False` and `parse(json.dumps(dict))` **or** pass the JSON string into `parse`. Prefer: `data = client.complete_json(...); return parse(json.dumps(data))` only when `parse` expects a string — match each call site’s existing `parse(raw: str)` signature by giving `parse` the raw JSON text from a no-tools Agent on a JSON-mode model **or** `json.dumps(complete_json(...))`.
4. Simplest consistent approach for Agent sites: second `Agent` with JSON-mode model, `tools=[]`, system prompt = formatting instructions + untrusted guard, user prompt = wrapped prose; `return parse(str(agent2(prompt)).strip())`.

Document DbC on every public function.

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd backend && python -m pytest agents/software_engineering_team/tests/test_via_reasoning.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/agents/software_engineering_team/code_review_agent/via_reasoning.py \
  backend/agents/software_engineering_team/tests/test_via_reasoning.py
git commit -m "$(cat <<'EOF'
Add code-review local think-then-format via_reasoning helpers.

Review work runs as text with configurable thinking; JSON transcription is a second thinking-off call.
EOF
)"
```

---

### Task 2: Split chunk-review prompts

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/profiles.py`
- Modify: `backend/agents/software_engineering_team/tests/test_review_profiles.py` (and any test asserting full `CODE_REVIEW_PROMPT` contains JSON + criteria together)

**Interfaces:**
- Consumes: existing `_SHARED_OUTPUT_SECTION`, `JSON_OUTPUT_INSTRUCTION`, profile specs
- Produces:
  - `REVIEW_PROSE_INSTRUCTION` constant (structured prose, no JSON)
  - `build_review_reasoning_system_prompt(profile) -> str` — role, standards, criteria, prose instruction; **no** `_SHARED_OUTPUT_SECTION` / `JSON_OUTPUT_INSTRUCTION`
  - `build_review_formatting_instructions(profile) -> str` — `_SHARED_OUTPUT_SECTION` + `JSON_OUTPUT_INSTRUCTION` (profile-agnostic JSON contract; `profile` accepted for API symmetry)
  - `build_review_system_prompt(profile)` — keep as `reasoning + formatting` concatenation **only** if something still needs the legacy full string; otherwise update all callers and delete the combined form. Prefer: keep `build_review_system_prompt` as the concatenation for backward-compatible imports, but chunk reviewer must call the split builders.

- [ ] **Step 1: Write/update failing profile tests**

Assert:

```python
def test_reasoning_prompt_omits_json_output_contract():
    text = build_review_reasoning_system_prompt(ReviewProfile.CODE_REVIEW)
    assert "Return a single JSON object" not in text
    assert "Answer in structured prose" in text  # or whatever REVIEW_PROSE_INSTRUCTION says
    assert "approved" not in text or "\"approved\"" not in text  # tighten to real markers


def test_formatting_instructions_include_json_contract():
    text = build_review_formatting_instructions(ReviewProfile.CODE_REVIEW)
    assert "Return a single JSON object" in text
    assert '"approved"' in text
```

Update any test that required the old monolithic prompt to assert via the split builders or the composed `build_review_system_prompt` if retained.

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && python -m pytest agents/software_engineering_team/tests/test_review_profiles.py -v -k "reasoning or formatting or CODE_REVIEW"
```

Expected: FAIL on missing symbols / assertions.

- [ ] **Step 3: Implement prompt split in `profiles.py`**

```python
REVIEW_PROSE_INSTRUCTION = (
    "\n\n**Output format:**\n"
    "Answer in structured prose (not JSON). For each issue you would report, "
    "state severity, category, file_path, line (when applicable), title, "
    "description, suggestion, and pre_existing. Then state whether the change "
    "should be approved, give a brief summary, and list any spec-compliance gaps.\n"
)

def build_review_reasoning_system_prompt(profile: ReviewProfile | str) -> str:
    spec = REVIEW_PROFILES[ReviewProfile(profile)]
    return (
        spec.role_line
        + "\n\n"
        + REVIEW_STANDARDS
        + _SHARED_ROLE_AND_SETTLED
        + REVIEW_PRIORITY_FRAMEWORK
        + spec.criteria_block
        + REVIEW_PROSE_INSTRUCTION
    )

def build_review_formatting_instructions(profile: ReviewProfile | str) -> str:
    _ = ReviewProfile(profile)  # validate
    return _SHARED_OUTPUT_SECTION + JSON_OUTPUT_INSTRUCTION

def build_review_system_prompt(profile: ReviewProfile | str) -> str:
    return (
        build_review_reasoning_system_prompt(profile)
        + build_review_formatting_instructions(profile)
    )
```

Note: composed `build_review_system_prompt` will contain both prose and JSON instructions — that is fine for legacy; chunk reviewer must not use it for the reasoning call.

- [ ] **Step 4: Run tests**

```bash
cd backend && python -m pytest agents/software_engineering_team/tests/test_review_profiles.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/agents/software_engineering_team/code_review_agent/profiles.py \
  backend/agents/software_engineering_team/tests/test_review_profiles.py
git commit -m "$(cat <<'EOF'
Split code-review system prompts into reasoning prose and JSON formatting.

Chunk review can think in text without a JSON response-format contract on the review call.
EOF
)"
```

---

### Task 3: Migrate `ChunkReviewAgent` to via-reasoning

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/chunk_reviewer.py`
- Modify: `backend/agents/software_engineering_team/tests/test_chunk_reviewer.py`
- Modify: `backend/agents/software_engineering_team/tests/test_chunk_review_llm_schema.py` (if it stubs `complete_json` only)

**Interfaces:**
- Consumes: `complete_validated_via_reasoning_local`, `build_review_reasoning_system_prompt`, `build_review_formatting_instructions`
- Produces: same `ChunkReviewOutput` shape; `think` on `run` / `_run_chunk_review` maps to `reasoning_think`

- [ ] **Step 1: Update chunk reviewer tests for two-call contract**

Extend `_StubClient` / recording clients so they override **both** `complete` and `complete_json`:

```python
class _TwoCallStub(DummyLLMClient):
    def __init__(self, canned: dict[str, Any], prose: str = "prose review") -> None:
        super().__init__()
        self._canned = canned
        self.complete_calls: list[dict[str, Any]] = []
        self.complete_json_calls: list[dict[str, Any]] = []

    def complete(self, prompt: str, *, objective: str, think=None, system_prompt=None, **kwargs):
        self.complete_calls.append(
            {"prompt": prompt, "objective": objective, "think": think, "system_prompt": system_prompt}
        )
        return "prose review"

    def complete_json(self, prompt: str, *, objective: str, think=None, **kwargs):
        self.complete_json_calls.append(
            {"prompt": prompt, "objective": objective, "think": think}
        )
        return self._canned
```

Assert:
- `run()` issues `complete` then `complete_json`
- reasoning `system_prompt` has no `"Return a single JSON object"`
- reasoning user `prompt` does **not** contain `FINAL_OUTPUT_CONTRACT_NOTE` / `"Respond with ONLY the single JSON object"`
- format call has `think is False`
- default path: reasoning `think is True` (or `None` only if wrapper maps None→True — prefer wrapper always passes explicit `True` by default)
- `run(..., think=False)` sets reasoning `think is False`

Update `_NonJsonClient` so `complete` returns prose and `complete_json` raises `LLMJsonParseError` (format failure still surfaces).

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && python -m pytest agents/software_engineering_team/tests/test_chunk_reviewer.py -v
```

Expected: FAIL (still single `complete_validated` path / missing `complete` calls).

- [ ] **Step 3: Implement chunk reviewer migration**

In `_run_chunk_review`:
- Remove `FINAL_OUTPUT_CONTRACT_NOTE` from `context_parts`.
- Replace `complete_validated(...)` with:

```python
from software_engineering_team.code_review_agent.via_reasoning import (
    complete_validated_via_reasoning_local,
)
from software_engineering_team.code_review_agent.profiles import (
    build_review_formatting_instructions,
    build_review_reasoning_system_prompt,
)

response = complete_validated_via_reasoning_local(
    llm,
    schema=ChunkReviewLLMResponse,
    reasoning_prompt=prompt,
    reasoning_system_prompt=build_review_reasoning_system_prompt(input_data.profile),
    formatting_instructions=build_review_formatting_instructions(input_data.profile),
    objective="review code chunk",
    reasoning_think=True if think is None else think,
    temperature=0.0,
)
```

Update module docstring invariants: two LLM requests per chunk; call 1 text+think; call 2 JSON think off.

- [ ] **Step 4: Run tests**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_chunk_reviewer.py \
  agents/software_engineering_team/tests/test_chunk_review_llm_schema.py \
  agents/software_engineering_team/tests/test_code_review_coordinator.py -v --tb=short
```

Expected: PASS (fix any coordinator stubs that only mock `complete_json` so they also implement `complete`).

- [ ] **Step 5: Commit**

```bash
git add backend/agents/software_engineering_team/code_review_agent/chunk_reviewer.py \
  backend/agents/software_engineering_team/tests/test_chunk_reviewer.py \
  backend/agents/software_engineering_team/tests/test_chunk_review_llm_schema.py \
  backend/agents/software_engineering_team/tests/test_code_review_coordinator.py
git commit -m "$(cat <<'EOF'
Migrate chunk review to think-then-format JSON split.

Review runs with max thinking in text mode; schema-validated JSON is a second thinking-off call.
EOF
)"
```

---

### Task 4: Text-mode resolution for Agent paths

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/model_resolution.py`
- Modify: `backend/agents/software_engineering_team/tests/test_model_resolution.py`

**Interfaces:**
- Consumes: `get_strands_model(..., response_format=, think=)`
- Produces: `resolve_code_review_model(llm, think=None, response_format: str = "json")` and `resolve_code_review_verify_model(..., response_format: str = "json")` forwarding `response_format`

- [ ] **Step 1: Write failing tests**

```python
def test_resolve_code_review_model_forwards_response_format_text(monkeypatch):
    seen = {}

    def fake_get(key, **kwargs):
        seen.update(kwargs)
        seen["key"] = key
        return object()

    monkeypatch.setattr(model_resolution, "get_strands_model", fake_get)
    model_resolution.resolve_code_review_model(object(), response_format="text", think=True)
    # When llm is not a Strands Model, production path is taken — pass a plain object
    # that is NOT isinstance(_StrandsModel). Use a simple namespace or MagicMock that
    # fails the isinstance check.
```

Use a plain `object()` only if `isinstance(object(), _StrandsModel)` is False (it is). Assert `seen["response_format"] == "text"` and `seen["think"] is True`.

- [ ] **Step 2: Run test — expect FAIL** (unexpected kwarg).

- [ ] **Step 3: Add `response_format: str = "json"` parameter to both resolve helpers and pass through to `get_strands_model`. For injected `_StrandsModel` instances: if `response_format !=` current and `.clone` exists, `return llm.clone(response_format=response_format, think=think)` when possible; else return `llm` unchanged (document limitation for opaque test doubles).

- [ ] **Step 4: Run `test_model_resolution.py` — expect PASS.**

- [ ] **Step 5: Commit**

```bash
git commit -m "$(cat <<'EOF'
Allow code-review model resolution to request text response_format.

Agent review calls can opt into text mode so thinking is not forced off by JSON format.
EOF
)"
```

---

### Task 5: Migrate `submission_pass_runner._call_agent`

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/submission_pass_runner.py`
- Modify: tests that cover architecture / side-effect / merged passes (e.g. `test_architecture_consistency_pass.py`, `test_submission_pass_runner.py` if present)

**Interfaces:**
- Consumes: `run_agent_via_reasoning`
- Produces: `_call_agent` still returns `parse(...)` result; internally two Agent/LLM calls

- [ ] **Step 1: Add/adjust a unit test** that stubs `run_agent_via_reasoning` or records two Agent constructions via monkeypatch on `submission_pass_runner.Agent`, asserting first has tools and second does not.

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Change `_call_agent` to:**

```python
def _call_agent(model, system_prompt, tools, prompt, parse):
    from software_engineering_team.code_review_agent.via_reasoning import (
        run_agent_via_reasoning,
    )
    # Split: callers currently pass a single system_prompt that includes JSON.
    # Until Task 6/7 finish prompt splits for each pass, temporarily derive:
    #   reasoning_system_prompt = system_prompt with JSON tail stripped OR
    #   require callers to pass split prompts.
```

**Preferred (matches spec):** change `run_submission_pass` / pass modules to pass `reasoning_system_prompt` + `formatting_instructions` instead of one `system_prompt`. Update `_call_agent` signature:

```python
def _call_agent(
    model,
    reasoning_system_prompt: str,
    formatting_instructions: str,
    tools: list,
    prompt: str,
    parse: Callable[[str], T],
) -> T:
    return run_agent_via_reasoning(
        model=model,
        reasoning_prompt=prompt,
        reasoning_system_prompt=reasoning_system_prompt,
        formatting_instructions=formatting_instructions,
        parse=parse,
        tools=tools,
        reasoning_think=True,
    )
```

Update `architecture_consistency_pass.py`, `side_effect_impact_pass.py`, `merged_architecture_side_effect_pass.py` to pass split prompt constants from `prompts.py` (do the prompt constant split in this task or Task 6 — if deferred, Task 5 may include the `prompts.py` splits for those three passes).

- [ ] **Step 4: Run submission-pass related tests — expect PASS.**

- [ ] **Step 5: Commit** with message explaining submission passes now think-then-format.

---

### Task 6: Migrate false-positive filter + split FPF prompt

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py`
- Modify: `backend/agents/software_engineering_team/code_review_agent/prompts.py` (FPF prompt constants)
- Modify: `backend/agents/software_engineering_team/tests/test_false_positive_filter.py`

**Interfaces:**
- Consumes: `run_agent_via_reasoning`, `resolve_code_review_verify_model(..., response_format="text")` for call 1 (wrapper may do this internally)
- Produces: same verdict dict behavior

- [ ] **Step 1: Tests** — recording Agent factory: first call has tools, second has no tools; JSON parse still drives keep/drop. Fix any stubs that only return JSON from a single Agent call so call 1 returns prose and call 2 returns JSON.

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Split `FALSE_POSITIVE_VERIFY_PROMPT` into reasoning + formatting instructions in `prompts.py`. Wire `_verify_group` through `run_agent_via_reasoning`. Keep `_agent_read_the_cited_file` inspection on the **reasoning** Agent instance (call 1) — the wrapper must return or expose that agent, **or** `_verify_group` must keep constructing call 1 locally for the read check.

**Important design detail for implementers:** today’s `_agent_read_the_cited_file(agent, ...)` inspects the tool-using Agent after the call. `run_agent_via_reasoning` must either:

1. Accept an optional `on_reasoning_agent: Callable[[Agent], None]` callback, or
2. Return `(parsed, reasoning_agent)`, or
3. Keep FPF’s call-1 construction inline and only use the wrapper for the format half.

**Choose (1)** — add `on_reasoning_agent: Callable[[Agent], None] | None = None` to `run_agent_via_reasoning` and invoke it after Agent construction / before or after the prompt run so FPF can capture the agent for `_agent_read_the_cited_file`. Add a unit test in `test_via_reasoning.py` for the callback. If Task 1’s wrapper lacked this, extend it in this task with tests first (TDD).

- [ ] **Step 4: Run FPF tests — PASS.**

- [ ] **Step 5: Commit.**

---

### Task 7: Migrate synthesis (+ remaining Agent JSON paths)

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/synthesis.py`
- Modify: `backend/agents/software_engineering_team/code_review_agent/prompts.py` (`REVIEW_SYNTHESIS_PROMPT`, `SPEC_COMPLIANCE_PASS_PROMPT`)
- Modify: synthesis tests under `software_engineering_team/tests/`

**Interfaces:**
- Consumes: `run_agent_via_reasoning` with `tools=[]`
- Produces: same `SynthesisResult | None` best-effort contract (never raises)

- [ ] **Step 1: Update synthesis tests** for two calls; call 1 prose, call 2 JSON; failure on either still returns `None`.

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Split synthesis prompts; replace `Agent(...)` + `json.loads` with `run_agent_via_reasoning`, parsing into `SynthesisResult` inside `parse` or after. Preserve broad `except Exception` returning `None`.

- [ ] **Step 4: Run synthesis tests — PASS.**

- [ ] **Step 5: Commit.**

---

### Task 8: Audit grep + coverage cleanup

**Files:**
- All of `code_review_agent/`
- Any remaining tests broken by the two-call contract

- [ ] **Step 1: Grep for leftover single-shot JSON review paths**

```bash
cd backend/agents/software_engineering_team/code_review_agent
rg -n "complete_validated\(|complete_json\(|Agent\(" -g'*.py'
rg -n "JSON_OUTPUT_INSTRUCTION|Return a single JSON object" -g'*.py'
```

For each hit: migrate, or document in the PR why it is not a JSON-outcome review path (there should be **no** remaining review/format paths that request JSON on the reasoning call).

- [ ] **Step 2: Run the broader code-review suite**

```bash
cd backend && python -m pytest agents/software_engineering_team/tests/ -k "code_review or chunk_review or false_positive or synthesis or architecture_consistency or side_effect or submission_pass or via_reasoning or review_profile" -v --tb=short
```

Fix failures. Ensure new `via_reasoning.py` meets ≥90% line coverage (`pytest --cov=software_engineering_team.code_review_agent.via_reasoning`).

- [ ] **Step 3: Commit any residual fixes.**

- [ ] **Step 4: Final success check against spec**
  - No reasoning call with `response_format="json"` for code-review JSON outcomes
  - Default reasoning uses max think (`think=True`)
  - Format calls `think=False`, no tools
  - Thinking-off retry still splits with `reasoning_think=False`
  - Public output shapes unchanged

---

## Plan self-review

| Spec requirement | Task |
|---|---|
| Local wrapper (not llm_service change) | Task 1 |
| `reasoning_think` override for thinking-off retry | Task 1 + Task 3 |
| Prompt split for chunk review | Task 2 + Task 3 |
| Tool-using passes: tools on call 1 only | Task 5 + Task 6 |
| FPF read-file grounding check still works | Task 6 (`on_reasoning_agent`) |
| Synthesis | Task 7 |
| All JSON paths / grep audit | Task 8 |
| Tests ≥90% | Tasks 1–8 |
| No env kill-switch | Honored (not implemented) |

No TBD/placeholder steps remain after choosing FPF callback option (1) explicitly in Task 6.
