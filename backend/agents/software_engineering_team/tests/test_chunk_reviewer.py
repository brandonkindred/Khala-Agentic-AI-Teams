"""Tests for Code Review Chunk Reviewer.

``ChunkReviewAgent`` runs a two-call via-reasoning split through
``run_agent_via_reasoning`` (a Strands ``Agent`` for the reasoning pass with
thinking, then ``complete_json`` for schema-validated formatting with
thinking off). Uses ``DummyLLMClient`` subclasses instead of ``MagicMock`` so
the injected client behaves like a real ``LLMClient``; both passes land on
``complete_json`` now (the reasoning pass reaches it via the Strands Agent's
``chat()`` delegation), so doubles route by call order rather than by
overriding ``complete``/``complete_json`` separately.

Prompt-*assembly* tests (what content chunk review builds, and whether the
shared spec/architecture/existing-code prefix is marked as a cache
breakpoint) monkeypatch ``run_agent_via_reasoning`` directly and inspect its
captured kwargs — this checks exactly what chunk review constructs without
depending on Strands/DummyLLMClient plumbing. End-to-end tests (the two-call
split, think overrides, error propagation, response field pass-through) let
the real ``run_agent_via_reasoning`` execute against a ``DummyLLMClient``
double.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Union

import pytest
from code_review_agent.chunk_reviewer import (
    CHUNK_REVIEW_NOTE,
    REVIEW_GUARDRAILS_NOTE,
    ChunkReviewAgent,
    _build_shared_review_prefix,
)
from code_review_agent.models import ChunkReviewInput, ChunkReviewLLMResponse, ChunkReviewOutput
from code_review_agent.profiles import build_review_reasoning_system_prompt

from llm_service import CacheBreakpoint, LLMJsonParseError, LLMSchemaValidationError
from llm_service.clients.dummy import DummyLLMClient


class _TwoCallStub(DummyLLMClient):
    """DummyLLMClient returning canned prose then a ChunkReviewLLMResponse-shaped dict.

    Both the reasoning pass (reached via the Strands Agent's ``chat()``
    delegation) and the formatting pass (a direct ``complete_json`` call from
    ``run_agent_via_reasoning``) land on ``complete_json`` now, routed by call
    order — mirrors ``test_code_review_synthesis.py``'s ``_SynthesisTwoCallStub``.
    """

    def __init__(self, canned: Dict[str, Any], prose: str = "prose review") -> None:
        super().__init__()
        self._canned = canned
        self._prose = prose
        self.complete_json_calls: list[Dict[str, Any]] = []

    def complete_json(
        self,
        prompt: str,
        *,
        objective: str = "dummy",
        think: Optional[Union[bool, str]] = None,
        **kwargs: Any,
    ) -> Any:
        self.complete_json_calls.append(
            {"prompt": prompt, "objective": objective, "think": think, **kwargs}
        )
        if len(self.complete_json_calls) == 1:
            return self._prose
        return self._canned


def _chunk_input(**overrides: Any) -> ChunkReviewInput:
    base = {
        "code_chunk": "### app/main.py ###\ndef foo(): pass",
        "file_path_or_label": "app/main.py",
        "task_description": "Add endpoint",
        "task_requirements": "",
        "acceptance_criteria": [],
        "spec_excerpt": "",
        "architecture_overview": "",
        "existing_codebase_excerpt": None,
    }
    base.update(overrides)
    return ChunkReviewInput(**base)  # type: ignore[arg-type]


def _capture_run_agent_via_reasoning(monkeypatch: pytest.MonkeyPatch) -> list[Dict[str, Any]]:
    """Monkeypatch ``run_agent_via_reasoning`` to record each call's kwargs.

    Preconditions:
        ``monkeypatch`` is the active pytest fixture.

    Postconditions:
        Returns a list that accumulates one kwargs dict per
        ``run_agent_via_reasoning`` call, in call order. Each call returns a
        canned, schema-valid ``ChunkReviewLLMResponse`` without touching any
        real LLM/Strands machinery — for tests that only care what
        ``_run_chunk_review`` constructs and passes, not how it executes.
    """
    calls: list[Dict[str, Any]] = []

    def _fake(**kwargs: Any) -> ChunkReviewLLMResponse:
        calls.append(kwargs)
        return ChunkReviewLLMResponse(
            approved=True, issues=[], summary="ok", spec_compliance_notes=""
        )

    monkeypatch.setattr("code_review_agent.chunk_reviewer.run_agent_via_reasoning", _fake)
    return calls


class _NonJsonClient(DummyLLMClient):
    """Reasoning pass succeeds; formatting pass cannot produce parseable JSON --
    matching the real ``LLMClient`` contract."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def complete_json(self, prompt: str, **kwargs: Any) -> Any:
        self.calls += 1
        if self.calls == 1:
            return "prose review"
        raise LLMJsonParseError(
            "I could not produce the requested JSON object.",
            response_preview="I could not produce the requested JSON object.",
        )


def test_chunk_review_records_reasoning_and_formatting_transcript_entries(
    monkeypatch,
) -> None:
    """The transcript records the reasoning pass with the reasoning system
    prompt and the formatting call with the formatting guard.

    Mirrors ``test_synthesize_records_reasoning_conversation_in_transcript``'s
    contract: the reasoning-pass entry (or entries) reflect whatever
    ``transcript.record_reasoning_transcript_turns`` observed -- individual
    inner HTTP continuation turns when the Strands adapter recorded them on
    this context, otherwise one entry summarizing the reasoning ``Agent``'s
    conversation. This call path runs the reasoning pass inside the Strands
    ``Agent``'s own async event loop, whose task gets its own copied
    ``contextvars`` context, so turns a client records via
    ``record_complete_json_turn`` during that call are not guaranteed
    visible back on the synchronous caller's context afterward -- the
    fallback path exists precisely for this, so this test does not assume
    which path is taken."""
    from code_review_agent.via_reasoning import formatting_system_prompt_with_untrusted_guard

    from llm_service import llm_attribution

    canned = {
        "approved": True,
        "issues": [],
        "summary": "ok",
        "spec_compliance_notes": "",
    }

    class _ReasoningClient(DummyLLMClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def complete_json(self, prompt: str, **kwargs: Any) -> Any:
            self.calls += 1
            if self.calls == 1:
                return "prose review"
            return canned

    captured: list = []
    monkeypatch.setattr(
        "code_review_agent.chunk_reviewer.record_transcript_entry",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )

    agent = ChunkReviewAgent(llm=_ReasoningClient())
    with llm_attribution(job_id="job-1"):
        agent.run(_chunk_input())

    format_system = formatting_system_prompt_with_untrusted_guard(None)
    reasoning_system = build_review_reasoning_system_prompt("code_review")
    assert len(captured) >= 2
    *reasoning_entries, formatting_entry = captured
    assert reasoning_entries
    for entry in reasoning_entries:
        assert entry[1]["system_prompt"] == reasoning_system
    assert formatting_entry[1]["system_prompt"] == format_system
    # record_transcript_entry(stage, target, prompt, response, *, ...) -- response is
    # positional-only (everything after it is keyword-only), so args[3] is exactly
    # ``response``; there is no keyword fallback to assert against instead.
    assert "ok" in formatting_entry[0][3]


def test_chunk_review_raises_llm_json_parse_error_on_non_json_model_output() -> None:
    """When the injected client's formatting ``complete_json`` cannot produce
    parseable JSON, ``LLMJsonParseError`` propagates unchanged out of
    ``_run_chunk_review``.

    This guards the coupling in ``mapping._CONTENT_FAILURE_TYPES``, which lists
    ``LLMJsonParseError`` precisely because this call path can raise it: if the
    reviewer stopped calling ``run_agent_via_reasoning``/``llm.complete_json``
    and raised something else instead, that classification would silently stop
    matching -- this test fails loudly instead.
    """
    agent = ChunkReviewAgent(llm=_NonJsonClient())
    with pytest.raises(LLMJsonParseError):
        agent.run(_chunk_input())


class _NonObjectJsonClient(DummyLLMClient):
    """Reasoning pass succeeds; formatting pass returns well-formed but
    non-object JSON."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def complete_json(self, prompt: str, **kwargs: Any) -> Any:
        self.calls += 1
        if self.calls == 1:
            return "prose review"
        return ["not", "an", "object"]


def test_chunk_review_raises_llm_schema_validation_error_on_non_object_json_response() -> None:
    """A validly-parsed but non-object JSON response (e.g. a bare list) must
    raise ``LLMSchemaValidationError`` -- not crash with an unclassified
    error. ``mapping.py``'s ``_CONTENT_FAILURE_TYPES`` already classifies
    ``LLMSchemaValidationError`` as a recoverable content failure alongside
    ``LLMJsonParseError``, so this still degrades gracefully like any other
    malformed response.
    """
    agent = ChunkReviewAgent(llm=_NonObjectJsonClient())
    with pytest.raises(LLMSchemaValidationError):
        agent.run(_chunk_input())


def test_chunk_review_agent_run_returns_chunk_review_output() -> None:
    """``ChunkReviewAgent.run`` returns a ``ChunkReviewOutput`` — approved with no
    issues — when backed by the default ``DummyLLMClient``."""
    agent = ChunkReviewAgent(llm=DummyLLMClient())
    result = agent.run(_chunk_input())
    assert isinstance(result, ChunkReviewOutput)
    assert result.approved is True
    assert result.issues == []


def test_chunk_review_agent_carries_file_path_from_issue() -> None:
    """When the LLM sets a file_path on an issue, it flows through to the
    output unchanged."""
    agent = ChunkReviewAgent(
        llm=_TwoCallStub(
            {
                "approved": False,
                "issues": [
                    {
                        "severity": "critical",
                        "category": "documentation",
                        "file_path": "app/models.py",
                        "description": "Missing docstring on User",
                        "suggestion": "Add a docstring describing fields",
                    },
                ],
                "summary": "One issue found.",
                "spec_compliance_notes": "",
            }
        )
    )
    result = agent.run(_chunk_input(file_path_or_label="app/models.py"))
    assert isinstance(result, ChunkReviewOutput)
    assert result.approved is False
    assert len(result.issues) == 1
    # ``ChunkReviewOutput.issues`` remains ``List[Dict[str, Any]]`` for
    # backward compat — callers can still index by key.
    assert result.issues[0]["file_path"] == "app/models.py"
    assert result.issues[0]["description"] == "Missing docstring on User"
    assert "One issue" in result.summary


def test_segment_note_is_prepended_to_prompt(monkeypatch) -> None:
    """A segment note is rendered under a ``**Segment notes:**`` header and appears
    before the code-to-review section in the chunk prompt."""
    calls = _capture_run_agent_via_reasoning(monkeypatch)
    note = "app/main.py is shown only from original line 501 to 1000 (of 2400 total)."
    ChunkReviewAgent(llm=DummyLLMClient()).run(_chunk_input(segment_note=note))
    prompt = calls[0]["reasoning_prompt"]
    assert "**Segment notes:**" in prompt
    assert note in prompt
    assert prompt.index(note) < prompt.index("Code to review")


def test_no_segment_note_means_no_segment_section(monkeypatch) -> None:
    """With no segment note, the prompt omits the ``**Segment notes:**`` section."""
    calls = _capture_run_agent_via_reasoning(monkeypatch)
    ChunkReviewAgent(llm=DummyLLMClient()).run(_chunk_input())
    assert "**Segment notes:**" not in calls[0]["reasoning_prompt"]


def test_review_guardrails_note_is_in_every_prompt(monkeypatch) -> None:
    """The anti-false-positive guardrails (no phantom truncation, don't flag
    existing files as missing, defer cross-caller checks to the side-effect
    pass, relative imports are conventional) are injected into the per-chunk
    user prompt (not the byte-locked system prompt).
    """
    calls = _capture_run_agent_via_reasoning(monkeypatch)
    ChunkReviewAgent(llm=DummyLLMClient()).run(_chunk_input())
    prompt = calls[0]["reasoning_prompt"]
    assert "**Review guardrails" in prompt
    # Full sentences, not bare substrings, so a stray unrelated occurrence of
    # "COMPLETE" or "does not exist" elsewhere in the prompt can't false-pass.
    assert "Surface-first: the code shown for this chunk is COMPLETE" in prompt
    assert "Do NOT claim that a file, module, or symbol referenced here 'does not exist'" in prompt
    assert "SOLELY because it is off-chunk" in prompt
    assert (
        "Defer that cross-caller check to the dedicated side-effect / blast-radius pass" in prompt
    )
    assert "from .models import" in prompt  # relative imports are conventional


def test_shared_file_context_prefix_precedes_role_instructions(monkeypatch) -> None:
    """The shared microtask file context (the "Files in this chunk" label and
    the code under review) is a stable prefix ahead of the per-chunk
    role-specific instructions (the chunk note, review guardrails, and task
    description) -- pure reorder/isolation, no cache marking yet."""
    calls = _capture_run_agent_via_reasoning(monkeypatch)
    ChunkReviewAgent(llm=DummyLLMClient()).run(_chunk_input())
    prompt = calls[0]["reasoning_prompt"]
    code_pos = prompt.index("Code to review")
    assert code_pos < prompt.index(CHUNK_REVIEW_NOTE.strip())
    assert code_pos < prompt.index(REVIEW_GUARDRAILS_NOTE.strip())
    assert code_pos < prompt.index("Task description")


def test_user_decisions_rendered_as_settled_in_prompt(monkeypatch) -> None:
    """A resolved user decision is surfaced to the reviewer as settled so it is never flagged
    as an open/unanswered question."""
    calls = _capture_run_agent_via_reasoning(monkeypatch)
    ChunkReviewAgent(llm=DummyLLMClient()).run(
        _chunk_input(user_decisions=["Which auth? → OAuth2 (Google)"])
    )
    prompt = calls[0]["reasoning_prompt"]
    assert "User decisions already made" in prompt
    assert "Which auth? → OAuth2 (Google)" in prompt


def test_no_user_decisions_means_no_decisions_section(monkeypatch) -> None:
    """With no user decisions, the prompt omits the settled-decisions section."""
    calls = _capture_run_agent_via_reasoning(monkeypatch)
    ChunkReviewAgent(llm=DummyLLMClient()).run(_chunk_input())
    assert "User decisions already made" not in calls[0]["reasoning_prompt"]


def test_code_chunk_is_never_compacted(monkeypatch) -> None:
    """The coordinator bounds the chunk; the reviewer must send it verbatim.
    A chunk above the map budget is logged but not truncated, so a sentinel at
    the very end must survive into the prompt."""
    from software_engineering_team.shared.context_sizing import (
        compute_code_review_map_chunk_chars,
    )

    budget = compute_code_review_map_chunk_chars(DummyLLMClient())
    sentinel = "UNIQUE_TAIL_SENTINEL_42"
    chunk = ("x = 1\n" * ((budget // 6) + 200)) + sentinel
    assert len(chunk) > budget

    calls = _capture_run_agent_via_reasoning(monkeypatch)
    ChunkReviewAgent(llm=DummyLLMClient()).run(_chunk_input(code_chunk=chunk))
    assert sentinel in calls[0]["reasoning_prompt"]


def test_new_output_fields_are_parsed_through() -> None:
    """``spec_compliance_notes`` from the model reply is passed through onto the
    ``ChunkReviewOutput``."""
    agent = ChunkReviewAgent(
        llm=_TwoCallStub(
            {
                "approved": True,
                "issues": [],
                "summary": "ok",
                "spec_compliance_notes": "Chunk meets the spec.",
            }
        )
    )
    result = agent.run(_chunk_input())
    assert result.spec_compliance_notes == "Chunk meets the spec."


def test_missing_new_output_fields_raise_schema_validation_error() -> None:
    """``ChunkReviewLLMResponse`` requires all four top-level fields (no
    defaults): a reply missing ``spec_compliance_notes`` fails validation,
    raising ``LLMSchemaValidationError`` -- replacing the old hand-rolled
    parser's silent ``.get(..., "")`` default. No local corrective retry:
    the coordinator's chunk-level recovery is the retry layer now."""
    agent = ChunkReviewAgent(llm=_TwoCallStub({"approved": True, "issues": [], "summary": "ok"}))
    with pytest.raises(LLMSchemaValidationError):
        agent.run(_chunk_input())


def test_reject_without_actionable_issue_raises_schema_validation_error() -> None:
    """``ChunkReviewLLMResponse``'s consistency validator rejects ``approved=False``
    with no issues at all -- there is no actionable critical/high finding to
    justify the rejection, so the reply is malformed and fails schema
    validation rather than being silently accepted."""
    agent = ChunkReviewAgent(
        llm=_TwoCallStub(
            {"approved": False, "issues": [], "summary": "No issues.", "spec_compliance_notes": ""}
        )
    )
    with pytest.raises(LLMSchemaValidationError):
        agent.run(_chunk_input())


def test_reject_with_only_low_severity_issue_raises_schema_validation_error() -> None:
    """``approved=False`` still fails validation when every issue is below the
    critical/high threshold -- an info/low finding alone does not justify a
    rejection per the review prompt's own contract."""
    agent = ChunkReviewAgent(
        llm=_TwoCallStub(
            {
                "approved": False,
                "issues": [
                    {
                        "severity": "low",
                        "category": "naming",
                        "description": "Minor style nit.",
                        "suggestion": "Consider renaming.",
                    },
                ],
                "summary": "Minor issue only.",
                "spec_compliance_notes": "",
            }
        )
    )
    with pytest.raises(LLMSchemaValidationError):
        agent.run(_chunk_input())


def test_chunk_review_agent_passes_blank_file_path_through_unchanged() -> None:
    """A blank/omitted issue file_path stays blank — never filled with the
    chunk label, which would defeat the coordinator's per-path offset lookup
    (the coordinator resolves blank paths itself). ``ChunkReviewIssueLLM``
    defaults an omitted ``file_path`` to ``""`` and always emits the key on
    ``model_dump()``, so it is present-but-blank rather than absent."""
    agent = ChunkReviewAgent(
        llm=_TwoCallStub(
            {
                "approved": False,
                "issues": [
                    {
                        "severity": "high",
                        "category": "naming",
                        "description": "Use snake_case",
                        "suggestion": "Rename to get_user",
                    },
                ],
                "summary": "Fix naming.",
                "spec_compliance_notes": "",
            }
        )
    )
    result = agent.run(_chunk_input(file_path_or_label="app/main.py"))
    assert len(result.issues) == 1
    assert result.issues[0]["file_path"] == ""
    assert result.issues[0]["severity"] == "high"


def test_declared_language_reaches_prompt_without_heuristic(monkeypatch) -> None:
    """The caller's language is used verbatim; the extension-based fallback only
    applies when no language was declared."""
    calls = _capture_run_agent_via_reasoning(monkeypatch)
    ChunkReviewAgent(llm=DummyLLMClient()).run(
        _chunk_input(code_chunk="TIMEOUT = 30", language="python")
    )
    assert "**Language:** python" in calls[0]["reasoning_prompt"]

    # No language declared: falls back to the ".py" extension of the default
    # file_path_or_label ("app/main.py"), not a "typescript" guess.
    ChunkReviewAgent(llm=DummyLLMClient()).run(_chunk_input(code_chunk="TIMEOUT = 30"))
    assert "**Language:** python" in calls[1]["reasoning_prompt"]


def test_undeclared_language_falls_back_to_typescript_for_non_python_path(monkeypatch) -> None:
    """A chunk with no declared language and a non-Python file extension still
    falls back to "typescript" — the extension-based guess only recognizes
    .py/.pyi as Python."""
    calls = _capture_run_agent_via_reasoning(monkeypatch)
    ChunkReviewAgent(llm=DummyLLMClient()).run(
        _chunk_input(file_path_or_label="app/app.component.ts", language="")
    )
    assert "**Language:** typescript" in calls[0]["reasoning_prompt"]


def test_reasoning_prompt_omits_final_output_contract_note(monkeypatch) -> None:
    """The via-reasoning path sends prose on call 1; the JSON contract note must
    not appear in the reasoning user prompt (formatting owns JSON on call 2)."""
    calls = _capture_run_agent_via_reasoning(monkeypatch)
    ChunkReviewAgent(llm=DummyLLMClient()).run(_chunk_input())
    assert "Respond with ONLY the single JSON object" not in calls[0]["reasoning_prompt"]


def test_chunk_review_uses_two_call_via_reasoning_path() -> None:
    """``run()`` issues the reasoning call then the formatting call with the
    expected think flags and split system prompts."""
    canned = {
        "approved": True,
        "issues": [],
        "summary": "ok",
        "spec_compliance_notes": "",
    }
    client = _TwoCallStub(canned)
    ChunkReviewAgent(llm=client).run(_chunk_input())
    assert len(client.complete_json_calls) == 2
    reasoning_call, format_call = client.complete_json_calls
    reasoning_system = reasoning_call.get("system_prompt") or ""
    assert "Return a single JSON object" not in reasoning_system
    assert "Respond with ONLY the single JSON object" not in reasoning_call["prompt"]
    assert reasoning_call["think"] is True
    assert format_call["think"] is False
    assert build_review_reasoning_system_prompt("code_review") in reasoning_system


class _ThinkRecorderClient(DummyLLMClient):
    """Records ``think`` on both the reasoning and formatting calls, which
    alternate strictly (reasoning, formatting, reasoning, formatting, ...)
    across successive ``ChunkReviewAgent.run`` calls."""

    def __init__(self) -> None:
        super().__init__()
        self.reasoning_think_values: list = []
        self.format_think_values: list = []
        self.calls = 0

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        self.calls += 1
        if self.calls % 2 == 1:
            self.reasoning_think_values.append(kwargs.get("think"))
        else:
            self.format_think_values.append(kwargs.get("think"))
        return super().complete_json(prompt, **kwargs)


def test_run_forwards_think_override() -> None:
    """``think=False`` disables thinking on the reasoning pass; the default maps
    to ``True``. The formatting pass always uses ``think=False``."""
    client = _ThinkRecorderClient()
    agent = ChunkReviewAgent(llm=client)
    agent.run(_chunk_input(), think=False)
    assert client.reasoning_think_values[-1] is False
    assert client.format_think_values[-1] is False
    agent.run(_chunk_input())
    assert client.reasoning_think_values[-1] is True
    assert client.format_think_values[-1] is False


def test_shared_context_is_passed_through_in_full(monkeypatch) -> None:
    """An oversized spec excerpt is forwarded verbatim into the cache-breakpoint
    system content — no hard cap, no extra LLM compaction calls — so an
    upstream compaction failure cannot silently drop context from the chunk
    review."""
    from software_engineering_team.shared.context_sizing import (
        compute_code_review_spec_excerpt_chars,
    )

    max_spec = compute_code_review_spec_excerpt_chars(DummyLLMClient())
    oversized_spec = ("S" * max_spec) + "TAIL_BEYOND_BUDGET"
    calls = _capture_run_agent_via_reasoning(monkeypatch)
    ChunkReviewAgent(llm=DummyLLMClient()).run(_chunk_input(spec_excerpt=oversized_spec))
    [breakpoint_] = calls[0]["system_prompt_content"]
    assert isinstance(breakpoint_, CacheBreakpoint)
    assert "TAIL_BEYOND_BUDGET" in breakpoint_.text
    assert "S" * 100 in breakpoint_.text
    assert "TAIL_BEYOND_BUDGET" not in calls[0]["reasoning_prompt"]


def test_architecture_overview_is_passed_through_in_full(monkeypatch) -> None:
    """An oversized architecture overview is forwarded verbatim into the
    cache-breakpoint system content — no hard cap re-applied here — so a tail
    past the old budget still reaches the model."""
    from software_engineering_team.shared.context_sizing import (
        compute_code_review_arch_overview_chars,
    )

    max_arch = compute_code_review_arch_overview_chars(DummyLLMClient())
    oversized_arch = ("A" * max_arch) + "TAIL_BEYOND_BUDGET"
    calls = _capture_run_agent_via_reasoning(monkeypatch)
    ChunkReviewAgent(llm=DummyLLMClient()).run(_chunk_input(architecture_overview=oversized_arch))
    [breakpoint_] = calls[0]["system_prompt_content"]
    assert "TAIL_BEYOND_BUDGET" in breakpoint_.text
    assert "A" * 100 in breakpoint_.text
    assert "TAIL_BEYOND_BUDGET" not in calls[0]["reasoning_prompt"]


def test_existing_codebase_excerpt_is_passed_through_in_full(monkeypatch) -> None:
    """An oversized existing-codebase excerpt is forwarded verbatim into the
    cache-breakpoint system content — no hard cap re-applied here — so a tail
    past the old budget still reaches the model."""
    from software_engineering_team.shared.context_sizing import (
        compute_code_review_existing_codebase_chars,
    )

    max_existing = compute_code_review_existing_codebase_chars(DummyLLMClient())
    oversized_existing = ("E" * max_existing) + "TAIL_BEYOND_BUDGET"
    calls = _capture_run_agent_via_reasoning(monkeypatch)
    ChunkReviewAgent(llm=DummyLLMClient()).run(
        _chunk_input(existing_codebase_excerpt=oversized_existing)
    )
    [breakpoint_] = calls[0]["system_prompt_content"]
    assert "TAIL_BEYOND_BUDGET" in breakpoint_.text
    assert "E" * 100 in breakpoint_.text
    assert "TAIL_BEYOND_BUDGET" not in calls[0]["reasoning_prompt"]


def test_spec_compliance_single_pass_omits_acceptance_criteria_and_spec_excerpt(
    monkeypatch,
) -> None:
    """When ``spec_compliance_single_pass`` is True (``CODE_REVIEW_SPEC_COMPLIANCE_PASS``
    is on), the per-chunk prompt omits the acceptance-criteria block and the
    cache-breakpoint system content omits the spec excerpt -- that content is
    now handled once, post-dedupe, by ``synthesize_spec_compliance`` -- but
    ``architecture_overview`` is unaffected, per ADR-010's contract boundary."""
    calls = _capture_run_agent_via_reasoning(monkeypatch)
    ChunkReviewAgent(llm=DummyLLMClient()).run(
        _chunk_input(
            spec_compliance_single_pass=True,
            acceptance_criteria=["Must validate input"],
            spec_excerpt="SPEC_MARKER_TEXT",
            architecture_overview="ARCH_MARKER_TEXT",
        )
    )
    prompt = calls[0]["reasoning_prompt"]
    assert "Must validate input" not in prompt
    assert "**Acceptance criteria" not in prompt
    assert "SPEC_MARKER_TEXT" not in prompt
    [breakpoint_] = calls[0]["system_prompt_content"]
    assert "**Project specification" not in breakpoint_.text
    assert "SPEC_MARKER_TEXT" not in breakpoint_.text
    assert "ARCH_MARKER_TEXT" in breakpoint_.text


def test_spec_compliance_single_pass_default_false_keeps_legacy_rendering(monkeypatch) -> None:
    """Default (``spec_compliance_single_pass`` unset/False) still renders the
    acceptance-criteria block in the prompt and the spec excerpt in the
    cache-breakpoint system content, preserving the legacy flag-off rendering
    -- the counterpart to the flag-on test above, which asserts the same
    markers are absent."""
    calls = _capture_run_agent_via_reasoning(monkeypatch)
    ChunkReviewAgent(llm=DummyLLMClient()).run(
        _chunk_input(
            acceptance_criteria=["Must validate input"],
            spec_excerpt="SPEC_MARKER_TEXT",
        )
    )
    assert "Must validate input" in calls[0]["reasoning_prompt"]
    [breakpoint_] = calls[0]["system_prompt_content"]
    assert "SPEC_MARKER_TEXT" in breakpoint_.text


def test_run_chunk_review_marks_shared_prefix_as_cache_breakpoint(monkeypatch) -> None:
    """The spec/architecture/existing-code prefix is emitted as a single
    ``CacheBreakpoint`` in ``system_prompt_content``, with per-chunk content
    (the file label, in this case) present in the user-turn prompt instead --
    the issue's explicit "breakpoint present on the map-phase request" AC."""
    calls = _capture_run_agent_via_reasoning(monkeypatch)
    ChunkReviewAgent(llm=DummyLLMClient()).run(
        _chunk_input(
            spec_excerpt="SPEC_TEXT",
            architecture_overview="ARCH_TEXT",
            existing_codebase_excerpt="EXISTING_TEXT",
            file_path_or_label="app/FILELABEL_MARKER.py",
        )
    )
    expected_text = "\n".join(
        _build_shared_review_prefix("SPEC_TEXT", "ARCH_TEXT", "EXISTING_TEXT", False)
    )
    assert calls[0]["system_prompt_content"] == [CacheBreakpoint(expected_text)]
    assert "FILELABEL_MARKER" in calls[0]["reasoning_prompt"]


def test_run_chunk_review_omits_system_prompt_content_when_no_shared_context(monkeypatch) -> None:
    """With no spec/architecture/existing-code content, no cache-breakpoint
    system content is attached — behavior unchanged from before this
    mechanism existed."""
    calls = _capture_run_agent_via_reasoning(monkeypatch)
    ChunkReviewAgent(llm=DummyLLMClient()).run(_chunk_input())
    assert calls[0]["system_prompt_content"] is None


def test_run_chunk_review_shared_prefix_excluded_from_user_prompt(monkeypatch) -> None:
    """The shared spec/architecture/existing-code text never appears in the
    user-turn ``reasoning_prompt`` — it moved entirely to the cache-breakpoint
    system content."""
    calls = _capture_run_agent_via_reasoning(monkeypatch)
    ChunkReviewAgent(llm=DummyLLMClient()).run(
        _chunk_input(
            spec_excerpt="SPEC_MARKER_XYZ",
            architecture_overview="ARCH_MARKER_XYZ",
            existing_codebase_excerpt="EXISTING_MARKER_XYZ",
        )
    )
    prompt = calls[0]["reasoning_prompt"]
    assert "SPEC_MARKER_XYZ" not in prompt
    assert "ARCH_MARKER_XYZ" not in prompt
    assert "EXISTING_MARKER_XYZ" not in prompt


def test_build_shared_review_prefix_orders_spec_arch_existing() -> None:
    """With all three blocks present, they render in spec → architecture →
    existing-codebase order, each under its established header/delimiters."""
    parts = _build_shared_review_prefix("SPEC_TEXT", "ARCH_TEXT", "EXISTING_TEXT", False)
    prompt = "\n".join(parts)
    assert "**Project specification (excerpt):**" in prompt
    assert "**Architecture:**" in prompt
    assert "**Existing codebase (excerpt):**" in prompt
    assert prompt.count("---") == 4  # two "---" delimiter pairs: spec, existing
    assert prompt.index("SPEC_TEXT") < prompt.index("ARCH_TEXT") < prompt.index("EXISTING_TEXT")


def test_build_shared_review_prefix_omits_spec_when_single_pass_true() -> None:
    """``spec_compliance_single_pass=True`` suppresses the spec-excerpt block
    even when ``spec_excerpt`` is set, but architecture/existing still render."""
    parts = _build_shared_review_prefix("SPEC_TEXT", "ARCH_TEXT", "EXISTING_TEXT", True)
    prompt = "\n".join(parts)
    assert "**Project specification" not in prompt
    assert "SPEC_TEXT" not in prompt
    assert "ARCH_TEXT" in prompt
    assert "EXISTING_TEXT" in prompt


def test_build_shared_review_prefix_returns_empty_list_when_all_absent() -> None:
    """With no spec/architecture/existing content, the prefix is empty."""
    assert _build_shared_review_prefix("", "", "", False) == []


def test_build_shared_review_prefix_omits_absent_blocks_individually() -> None:
    """Only the blocks with content render; absent blocks contribute nothing,
    even when the other two are present."""
    parts = _build_shared_review_prefix("", "ARCH_ONLY_TEXT", "", False)
    prompt = "\n".join(parts)
    assert "**Architecture:**" in prompt
    assert "ARCH_ONLY_TEXT" in prompt
    assert "**Project specification" not in prompt
    assert "**Existing codebase" not in prompt
