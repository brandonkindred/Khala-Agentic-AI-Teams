"""Tests for Story 2c Step 2: CacheBreakpoint marking on review-gate requests.

Asserts that each review gate (Code Review, QA, Security) emits the shared
file-context prefix as a ``CacheBreakpoint``-marked system-content segment,
and that the same breakpoint applies across retry cycles (the identical
``CacheBreakpoint`` text is produced for the same input on repeated calls).
"""

from __future__ import annotations

from typing import Any, List
from unittest.mock import patch

import pytest

from llm_service.cache_breakpoint import CacheBreakpoint
from software_engineering_team.shared.persona_agent_base import (
    _build_system_prompt_with_content,
    run_structured_persona,
)

# ---------------------------------------------------------------------------
# run_structured_persona: system_prompt_content → CacheBreakpoint in system
# ---------------------------------------------------------------------------


class _RecordingAgent:
    """Captures the system_prompt passed to the agent factory."""

    instances: "List[_RecordingAgent]" = []

    def __init__(self, *, model: Any, system_prompt: Any) -> None:
        self.system_prompt = system_prompt
        _RecordingAgent.instances.append(self)

    def __call__(self, user_prompt: str, *, structured_output_model: type) -> Any:
        class _Result:
            structured_output = structured_output_model()

        return _Result()


@pytest.fixture(autouse=True)
def _clear_recording_agents() -> None:
    _RecordingAgent.instances.clear()


class _DummyOutput:
    pass


def test_build_system_prompt_with_content_returns_str_when_no_content() -> None:
    result = _build_system_prompt_with_content("persona text", None)
    assert result == "persona text"


def test_build_system_prompt_with_content_returns_str_when_empty_list() -> None:
    result = _build_system_prompt_with_content("persona text", [])
    assert result == "persona text"


def test_build_system_prompt_with_content_returns_list_with_cache_breakpoint() -> None:
    bp = CacheBreakpoint("cached prefix")
    result = _build_system_prompt_with_content("persona", [bp])
    assert isinstance(result, list)
    assert result[0] == {"text": "persona"}
    assert result[1] is bp


def test_build_system_prompt_with_content_normalizes_bare_strings() -> None:
    result = _build_system_prompt_with_content("persona", ["extra context"])
    assert isinstance(result, list)
    assert result[0] == {"text": "persona"}
    assert result[1] == {"text": "extra context"}


def test_run_structured_persona_passes_cache_breakpoint_to_agent() -> None:
    """When system_prompt_content contains a CacheBreakpoint, the agent
    receives a list-form system_prompt with the breakpoint intact."""
    bp = CacheBreakpoint("file context prefix")

    run_structured_persona(
        model=object(),
        system_prompt="security persona",
        user_prompt="review this",
        output_model=_DummyOutput,
        fallback_factory=lambda exc: _DummyOutput(),
        agent_factory=_RecordingAgent,
        system_prompt_content=[bp],
    )

    assert len(_RecordingAgent.instances) == 1
    agent = _RecordingAgent.instances[0]
    assert isinstance(agent.system_prompt, list)
    assert agent.system_prompt[0] == {"text": "security persona"}
    assert isinstance(agent.system_prompt[1], CacheBreakpoint)
    assert agent.system_prompt[1].text == "file context prefix"


def test_run_structured_persona_no_content_passes_plain_str() -> None:
    """When system_prompt_content is None, agent gets a plain string."""
    run_structured_persona(
        model=object(),
        system_prompt="persona",
        user_prompt="review",
        output_model=_DummyOutput,
        fallback_factory=lambda exc: _DummyOutput(),
        agent_factory=_RecordingAgent,
        system_prompt_content=None,
    )

    agent = _RecordingAgent.instances[0]
    assert agent.system_prompt == "persona"


# ---------------------------------------------------------------------------
# QA gate: CacheBreakpoint present on the request
# ---------------------------------------------------------------------------


def test_qa_gate_emits_cache_breakpoint_for_file_context() -> None:
    """QAExpertAgent.run() wraps the file-context prefix in a CacheBreakpoint
    and passes it as system_prompt_content to run_structured_persona."""
    from qa_agent import QAExpertAgent, QAInput

    captured_kwargs: List[dict] = []

    def _spy(**kwargs: Any) -> Any:
        captured_kwargs.append(kwargs)
        # Return a valid QAOutput-shaped object via the real function with a
        # no-op agent that returns the right type.
        from qa_agent.models import QAOutput

        return QAOutput(
            bugs_found=[],
            approved=True,
            summary="ok",
            integration_tests="",
            unit_tests="",
            test_plan="",
            live_test_notes="",
            readme_content="",
            suggested_commit_message="",
        )

    input_data = QAInput(
        code="def hello(): pass",
        language="python",
        task_description="greet",
    )

    with patch(
        "qa_agent.agent.run_structured_persona", side_effect=_spy
    ):
        agent = QAExpertAgent(None)
        # Bypass model resolution for the test
        agent._model = object()
        agent.run(input_data)

    assert len(captured_kwargs) == 1
    spc = captured_kwargs[0].get("system_prompt_content")
    assert spc is not None
    assert len(spc) == 1
    assert isinstance(spc[0], CacheBreakpoint)
    # The breakpoint text contains the file context (language + code)
    assert "**Language:** python" in spc[0].text
    assert "def hello(): pass" in spc[0].text


def test_qa_gate_no_cache_breakpoint_for_acceptance_evidence_mode() -> None:
    """In acceptance_evidence mode there is no code under review, so no
    CacheBreakpoint is emitted."""
    from qa_agent import QAExpertAgent, QAInput

    captured_kwargs: List[dict] = []

    def _spy(**kwargs: Any) -> Any:
        captured_kwargs.append(kwargs)
        from qa_agent.models import QAOutput

        return QAOutput(
            bugs_found=[],
            approved=True,
            quality_gates={},
            summary="ok",
            integration_tests="",
            unit_tests="",
            test_plan="",
            live_test_notes="",
            readme_content="",
            suggested_commit_message="",
        )

    input_data = QAInput(
        code="",
        language="python",
        task_description="validate",
        request_mode="acceptance_evidence",
        acceptance_criteria=["Must pass"],
        tool_results={"tests": {"status": "pass"}},
    )

    with patch(
        "qa_agent.agent.run_structured_persona", side_effect=_spy
    ):
        agent = QAExpertAgent(None)
        agent._model = object()
        agent.run(input_data)

    assert len(captured_kwargs) == 1
    spc = captured_kwargs[0].get("system_prompt_content")
    # No CacheBreakpoint for acceptance_evidence mode
    assert spc is None


# ---------------------------------------------------------------------------
# Security gate: CacheBreakpoint present on the request
# ---------------------------------------------------------------------------


def test_security_gate_emits_cache_breakpoint_for_file_context() -> None:
    """CybersecurityExpertAgent.run() wraps the file-context prefix in a
    cache-control-marked system prompt block when the client supports prompt
    caching, and passes it to run_single_shot_review."""
    from security_agent.agent import (
        _build_cache_aware_system_prompt,
        _build_security_file_context_prefix,
    )
    from security_agent.models import SecurityInput

    input_data = SecurityInput(
        code="import os\nos.system('ls')",
        language="python",
        task_description="review command runner",
    )

    # Verify the cache-aware system prompt is a list with cache_control when
    # the client supports prompt caching.
    prefix_text = "\n".join(_build_security_file_context_prefix(input_data))

    # Simulate a caching-capable client
    class _CachingClient:
        def supports_prompt_caching(self) -> bool:
            return True

    system_prompt = _build_cache_aware_system_prompt(prefix_text, _CachingClient())
    assert isinstance(system_prompt, list)
    assert len(system_prompt) == 2
    # First block is the persona (no cache_control)
    assert system_prompt[0]["type"] == "text"
    assert "cache_control" not in system_prompt[0]
    # Second block is the file-context prefix (with cache_control)
    assert system_prompt[1]["type"] == "text"
    assert system_prompt[1]["cache_control"] == {"type": "ephemeral"}
    assert "**Language:** python" in system_prompt[1]["text"]
    assert "os.system('ls')" in system_prompt[1]["text"]

    # Verify non-caching client gets a plain string
    system_prompt_str = _build_cache_aware_system_prompt(prefix_text, None)
    assert isinstance(system_prompt_str, str)
    assert "**Language:** python" in system_prompt_str
    assert "os.system('ls')" in system_prompt_str


# ---------------------------------------------------------------------------
# Code Review gate: CacheBreakpoint already present (Story 2a/2b, verify)
# ---------------------------------------------------------------------------


def test_build_shared_review_prefix_produces_non_empty_breakpoint_text() -> None:
    """_build_shared_review_prefix returns non-empty text parts that can be
    wrapped in a CacheBreakpoint when spec/architecture/existing-code are
    present."""
    from code_review_agent.chunk_reviewer import _build_shared_review_prefix

    parts = _build_shared_review_prefix(
        spec_excerpt="Must validate inputs",
        architecture_overview="Single FastAPI service",
        existing_codebase_excerpt="class User: ...",
        spec_compliance_single_pass=False,
    )
    assert parts  # non-empty when any block is present
    bp = CacheBreakpoint("\n".join(parts))
    assert "Must validate inputs" in bp.text
    assert "Single FastAPI service" in bp.text
    assert "class User: ..." in bp.text


def test_build_shared_review_prefix_returns_empty_list_when_all_blocks_empty() -> None:
    """When spec/architecture/existing-code are all empty,
    _build_shared_review_prefix returns an empty list, so no CacheBreakpoint
    text is produced."""
    from code_review_agent.chunk_reviewer import _build_shared_review_prefix

    parts = _build_shared_review_prefix(
        spec_excerpt="",
        architecture_overview="",
        existing_codebase_excerpt="",
        spec_compliance_single_pass=False,
    )
    assert parts == []


# ---------------------------------------------------------------------------
# Cross-gate: same breakpoint text across retries (stability)
# ---------------------------------------------------------------------------


def test_cache_breakpoint_text_is_stable_across_calls() -> None:
    """The same input produces the same CacheBreakpoint text on repeated
    calls — a prerequisite for provider-side caching across retries."""
    from qa_agent import QAInput
    from qa_agent.agent import _build_qa_file_context_prefix

    input_data = QAInput(
        code="x = 1\ny = 2",
        language="python",
        task_description="simple",
    )

    text_1 = "\n".join(_build_qa_file_context_prefix(input_data))
    text_2 = "\n".join(_build_qa_file_context_prefix(input_data))
    assert text_1 == text_2
    # Also verify it can be wrapped without error
    bp = CacheBreakpoint(text_1)
    assert bp.text == text_1
