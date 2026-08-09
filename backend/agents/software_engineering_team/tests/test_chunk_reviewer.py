"""Tests for Code Review Chunk Reviewer.

``ChunkReviewAgent`` calls the injected ``LLMClient``'s ``complete_json``
directly (via ``llm_service.complete_validated``, validated against
``ChunkReviewLLMResponse``) — no strands ``Agent``/``Model`` is built for
this call path. Uses ``DummyLLMClient`` subclasses instead of ``MagicMock``
so the injected client behaves like a real ``LLMClient``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest
from code_review_agent.chunk_reviewer import ChunkReviewAgent
from code_review_agent.models import ChunkReviewInput, ChunkReviewOutput

from llm_service import LLMJsonParseError, LLMSchemaValidationError
from llm_service.clients.dummy import DummyLLMClient


class _StubClient(DummyLLMClient):
    """DummyLLMClient subclass returning a canned ChunkReviewLLMResponse-shaped dict."""

    def __init__(self, canned: Dict[str, Any]) -> None:
        super().__init__()
        self._canned = canned

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
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


class _NonJsonClient(DummyLLMClient):
    """DummyLLMClient whose ``complete_json`` cannot produce parseable JSON, so
    it raises ``LLMJsonParseError`` itself -- matching the real ``LLMClient``
    contract (``complete_json`` returns ``Dict[str, Any]``, never a bare
    string; a real client that cannot parse its own reply raises rather than
    returning unparsed text, e.g. ``OllamaLLMClient._extract_json``)."""

    def complete_json(self, prompt: str, **kwargs: Any) -> Any:
        raise LLMJsonParseError(
            "I could not produce the requested JSON object.",
            response_preview="I could not produce the requested JSON object.",
        )


def test_chunk_review_raises_llm_json_parse_error_on_non_json_model_output() -> None:
    """When the injected client's ``complete_json`` cannot produce parseable
    JSON on any of ``complete_validated``'s attempts, ``LLMJsonParseError``
    propagates unchanged out of ``_run_chunk_review``.

    This guards the coupling in ``mapping._CONTENT_FAILURE_TYPES``, which lists
    ``LLMJsonParseError`` precisely because this call path can raise it: if the
    reviewer stopped calling ``complete_validated``/``llm.complete_json`` and
    raised something else instead, that classification would silently stop
    matching -- this test fails loudly instead.
    """
    agent = ChunkReviewAgent(llm=_NonJsonClient())
    with pytest.raises(LLMJsonParseError):
        agent.run(_chunk_input())


class _NonObjectJsonClient(DummyLLMClient):
    """DummyLLMClient whose ``complete_json`` returns well-formed but
    non-object JSON (a bare list) -- ``ChunkReviewLLMResponse.model_validate``
    rejects a non-mapping value, so this must surface as
    ``LLMSchemaValidationError``, not an unclassified ``AttributeError``."""

    def complete_json(self, prompt: str, **kwargs: Any) -> Any:
        return ["not", "an", "object"]


def test_chunk_review_raises_llm_schema_validation_error_on_non_object_json_response() -> None:
    """A validly-parsed but non-object JSON response (e.g. a bare list) must
    raise ``LLMSchemaValidationError`` once ``complete_validated`` exhausts its
    corrective retry (the fake client returns the same value every time) --
    not crash with an unclassified error. ``mapping.py``'s
    ``_CONTENT_FAILURE_TYPES`` already classifies ``LLMSchemaValidationError``
    as a recoverable content failure alongside ``LLMJsonParseError``, so this
    still degrades gracefully like any other malformed response.
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
        llm=_StubClient(
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


class _RecorderClient(DummyLLMClient):
    """Delegates to Dummy but records every prompt."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list = []

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        self.prompts.append(prompt)
        return super().complete_json(prompt, **kwargs)


def test_segment_note_is_prepended_to_prompt() -> None:
    """A segment note is rendered under a ``**Segment notes:**`` header and appears
    before the code-to-review section in the chunk prompt."""
    client = _RecorderClient()
    agent = ChunkReviewAgent(llm=client)
    note = "app/main.py is shown only from original line 501 to 1000 (of 2400 total)."
    agent.run(_chunk_input(segment_note=note))
    assert len(client.prompts) == 1
    prompt = client.prompts[0]
    assert "**Segment notes:**" in prompt
    assert note in prompt
    assert prompt.index(note) < prompt.index("Code to review")


def test_no_segment_note_means_no_segment_section() -> None:
    """With no segment note, the prompt omits the ``**Segment notes:**`` section."""
    client = _RecorderClient()
    agent = ChunkReviewAgent(llm=client)
    agent.run(_chunk_input())
    assert "**Segment notes:**" not in client.prompts[0]


def test_review_guardrails_note_is_in_every_prompt() -> None:
    """The anti-false-positive guardrails (no phantom truncation, don't flag
    existing files as missing, defer cross-caller checks to the side-effect
    pass, relative imports are conventional) are injected into the per-chunk
    user prompt (not the byte-locked system prompt).

    Precondition: a ChunkReviewAgent is instantiated and run once, so exactly one
    prompt is recorded for inspection.
    """
    client = _RecorderClient()
    agent = ChunkReviewAgent(llm=client)
    agent.run(_chunk_input())
    prompt = client.prompts[0]
    assert "**Review guardrails" in prompt
    # Full sentences, not bare substrings, so a stray unrelated occurrence of
    # "COMPLETE" or "does not exist" elsewhere in the prompt can't false-pass.
    assert "Surface-first completeness: the code shown below is COMPLETE" in prompt
    assert "Do NOT claim that a file, module, or symbol referenced here 'does not exist'" in prompt
    assert "SOLELY because it is off-chunk" in prompt
    assert (
        "That cross-caller check is the job of the dedicated side-effect / blast-radius pass"
        in prompt
    )
    assert "from .models import" in prompt  # relative imports are conventional


def test_user_decisions_rendered_as_settled_in_prompt() -> None:
    """A resolved user decision is surfaced to the reviewer as settled so it is never flagged
    as an open/unanswered question."""
    client = _RecorderClient()
    agent = ChunkReviewAgent(llm=client)
    agent.run(_chunk_input(user_decisions=["Which auth? → OAuth2 (Google)"]))
    prompt = client.prompts[0]
    assert "User decisions already made" in prompt
    assert "Which auth? → OAuth2 (Google)" in prompt


def test_no_user_decisions_means_no_decisions_section() -> None:
    """With no user decisions, the prompt omits the settled-decisions section."""
    client = _RecorderClient()
    agent = ChunkReviewAgent(llm=client)
    agent.run(_chunk_input())
    assert "User decisions already made" not in client.prompts[0]


def test_code_chunk_is_never_compacted() -> None:
    """The coordinator bounds the chunk; the reviewer must send it verbatim.
    A chunk above the map budget is logged but not truncated, so a sentinel at
    the very end must survive into the prompt."""
    from software_engineering_team.shared.context_sizing import (
        compute_code_review_map_chunk_chars,
    )

    client = _RecorderClient()
    budget = compute_code_review_map_chunk_chars(client)
    sentinel = "UNIQUE_TAIL_SENTINEL_42"
    chunk = ("x = 1\n" * ((budget // 6) + 200)) + sentinel
    assert len(chunk) > budget

    agent = ChunkReviewAgent(llm=client)
    agent.run(_chunk_input(code_chunk=chunk))
    assert sentinel in client.prompts[0]


def test_new_output_fields_are_parsed_through() -> None:
    """``spec_compliance_notes`` from the model reply is passed through onto the
    ``ChunkReviewOutput``."""
    agent = ChunkReviewAgent(
        llm=_StubClient(
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
    defaults): a reply missing ``spec_compliance_notes`` fails validation.
    ``complete_validated`` retries once with a corrective prompt, but
    ``_StubClient`` always returns the same canned (still-incomplete) payload
    regardless of prompt, so the retry fails identically and
    ``LLMSchemaValidationError`` is raised -- replacing the old hand-rolled
    parser's silent ``.get(..., "")`` default."""
    agent = ChunkReviewAgent(llm=_StubClient({"approved": True, "issues": [], "summary": "ok"}))
    with pytest.raises(LLMSchemaValidationError):
        agent.run(_chunk_input())


def test_reject_without_actionable_issue_raises_schema_validation_error() -> None:
    """``ChunkReviewLLMResponse``'s consistency validator rejects ``approved=False``
    with no issues at all -- there is no actionable critical/high finding to
    justify the rejection, so the reply is malformed and fails schema
    validation rather than being silently accepted."""
    agent = ChunkReviewAgent(
        llm=_StubClient(
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
        llm=_StubClient(
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
        llm=_StubClient(
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


def test_declared_language_reaches_prompt_without_heuristic() -> None:
    """The caller's language is used verbatim; the extension-based fallback only
    applies when no language was declared."""
    client = _RecorderClient()
    agent = ChunkReviewAgent(llm=client)
    agent.run(_chunk_input(code_chunk="TIMEOUT = 30", language="python"))
    assert "**Language:** python" in client.prompts[0]

    # No language declared: falls back to the ".py" extension of the default
    # file_path_or_label ("app/main.py"), not a "typescript" guess.
    fallback_client = _RecorderClient()
    ChunkReviewAgent(llm=fallback_client).run(_chunk_input(code_chunk="TIMEOUT = 30"))
    assert "**Language:** python" in fallback_client.prompts[0]


def test_undeclared_language_falls_back_to_typescript_for_non_python_path() -> None:
    """A chunk with no declared language and a non-Python file extension still
    falls back to "typescript" — the extension-based guess only recognizes
    .py/.pyi as Python."""
    client = _RecorderClient()
    ChunkReviewAgent(llm=client).run(
        _chunk_input(file_path_or_label="app/app.component.ts", language="")
    )
    assert "**Language:** typescript" in client.prompts[0]


def test_final_output_contract_note_follows_the_code_block() -> None:
    """The output-contract nudge (emit only the JSON, no reasoning) is appended as
    the last thing the model reads — after the code block — so a thinking model is
    steered toward a final answer instead of reasoning-only output."""
    from code_review_agent.chunk_reviewer import FINAL_OUTPUT_CONTRACT_NOTE

    client = _RecorderClient()
    ChunkReviewAgent(llm=client).run(_chunk_input())
    prompt = client.prompts[0]
    assert FINAL_OUTPUT_CONTRACT_NOTE.strip() in prompt
    # It comes after the code-to-review section (last thing the model sees).
    assert prompt.index("Code to review") < prompt.index("Respond with ONLY")


class _ThinkRecorderClient(DummyLLMClient):
    """Delegates to Dummy but records the ``think`` kwarg of every call."""

    def __init__(self) -> None:
        super().__init__()
        self.think_values: list = []

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        self.think_values.append(kwargs.get("think"))
        return super().complete_json(prompt, **kwargs)


def test_run_forwards_think_override() -> None:
    """``ChunkReviewAgent.run(think=...)`` threads the override directly to the
    injected client's ``complete_json`` call (via ``complete_validated``); the
    default is ``None`` (the client's own platform-default thinking level, per
    ``LLMClient.complete_json``'s contract)."""
    client = _ThinkRecorderClient()
    agent = ChunkReviewAgent(llm=client)
    agent.run(_chunk_input(), think=False)
    assert client.think_values[-1] is False
    agent.run(_chunk_input())
    assert client.think_values[-1] is None


def test_shared_context_is_passed_through_in_full() -> None:
    """Spec/arch/existing excerpts are forwarded verbatim — no hard cap, no
    extra LLM compaction calls — so an upstream compaction failure cannot
    silently drop context from the chunk prompt."""
    from software_engineering_team.shared.context_sizing import (
        compute_code_review_spec_excerpt_chars,
    )

    client = _RecorderClient()
    max_spec = compute_code_review_spec_excerpt_chars(client)
    oversized_spec = ("S" * max_spec) + "TAIL_BEYOND_BUDGET"
    agent = ChunkReviewAgent(llm=client)
    agent.run(_chunk_input(spec_excerpt=oversized_spec))
    assert len(client.prompts) == 1  # exactly one LLM call: the review itself
    assert "TAIL_BEYOND_BUDGET" in client.prompts[0]
    assert "S" * 100 in client.prompts[0]


def test_architecture_overview_is_passed_through_in_full() -> None:
    """An oversized architecture overview is forwarded verbatim — no hard cap
    re-applied here — so a tail past the old budget still reaches the prompt."""
    from software_engineering_team.shared.context_sizing import (
        compute_code_review_arch_overview_chars,
    )

    client = _RecorderClient()
    max_arch = compute_code_review_arch_overview_chars(client)
    oversized_arch = ("A" * max_arch) + "TAIL_BEYOND_BUDGET"
    agent = ChunkReviewAgent(llm=client)
    agent.run(_chunk_input(architecture_overview=oversized_arch))
    assert len(client.prompts) == 1
    assert "TAIL_BEYOND_BUDGET" in client.prompts[0]
    assert "A" * 100 in client.prompts[0]


def test_existing_codebase_excerpt_is_passed_through_in_full() -> None:
    """An oversized existing-codebase excerpt is forwarded verbatim — no hard
    cap re-applied here — so a tail past the old budget still reaches the
    prompt."""
    from software_engineering_team.shared.context_sizing import (
        compute_code_review_existing_codebase_chars,
    )

    client = _RecorderClient()
    max_existing = compute_code_review_existing_codebase_chars(client)
    oversized_existing = ("E" * max_existing) + "TAIL_BEYOND_BUDGET"
    agent = ChunkReviewAgent(llm=client)
    agent.run(_chunk_input(existing_codebase_excerpt=oversized_existing))
    assert len(client.prompts) == 1
    assert "TAIL_BEYOND_BUDGET" in client.prompts[0]
    assert "E" * 100 in client.prompts[0]


def test_spec_compliance_single_pass_omits_acceptance_criteria_and_spec_excerpt() -> None:
    """When ``spec_compliance_single_pass`` is True (``CODE_REVIEW_SPEC_COMPLIANCE_PASS``
    is on), the per-chunk prompt omits the acceptance-criteria/spec-excerpt blocks --
    that content is now handled once, post-dedupe, by ``synthesize_spec_compliance`` --
    but ``architecture_overview`` is unaffected, per ADR-010's contract boundary."""
    client = _RecorderClient()
    agent = ChunkReviewAgent(llm=client)
    agent.run(
        _chunk_input(
            spec_compliance_single_pass=True,
            acceptance_criteria=["Must validate input"],
            spec_excerpt="SPEC_MARKER_TEXT",
            architecture_overview="ARCH_MARKER_TEXT",
        )
    )
    assert len(client.prompts) == 1
    prompt = client.prompts[0]
    assert "Must validate input" not in prompt
    assert "SPEC_MARKER_TEXT" not in prompt
    assert "**Acceptance criteria" not in prompt
    assert "**Project specification" not in prompt
    assert "ARCH_MARKER_TEXT" in prompt


def test_spec_compliance_single_pass_default_false_keeps_legacy_rendering() -> None:
    """Default (``spec_compliance_single_pass`` unset/False) still renders the
    acceptance-criteria/spec-excerpt blocks in the prompt, preserving the legacy
    flag-off rendering -- the counterpart to the flag-on test above, which asserts
    the same markers are absent."""
    client = _RecorderClient()
    agent = ChunkReviewAgent(llm=client)
    agent.run(
        _chunk_input(
            acceptance_criteria=["Must validate input"],
            spec_excerpt="SPEC_MARKER_TEXT",
        )
    )
    assert len(client.prompts) == 1
    prompt = client.prompts[0]
    assert "Must validate input" in prompt
    assert "SPEC_MARKER_TEXT" in prompt
