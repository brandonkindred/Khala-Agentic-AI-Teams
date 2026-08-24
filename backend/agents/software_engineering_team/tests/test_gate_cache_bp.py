"""Tests for cache-breakpoint marking on review-gate system prompts.

Asserts that:
- ``run_structured_persona`` correctly composes system_prompt_content into the
  Agent's system_prompt (for trusted metadata like spec/architecture).
- Untrusted file context (code under review) stays in the user message for
  QA and Security gates — never elevated to system-level instructions.
- QA/Security's trusted, non-code shared context (task description,
  architecture overview) IS elevated to a ``CacheBreakpoint``-marked
  ``system_prompt_content`` segment.
- Code Review's ``_build_shared_review_prefix`` produces stable breakpoint text
  from trusted spec/architecture metadata.
- The same breakpoint text is produced on repeated calls (stability).
"""

from __future__ import annotations

from typing import Any, List
from unittest.mock import patch

import pytest

from llm_service.cache_breakpoint import CacheBreakpoint
from software_engineering_team.shared.persona_agent_base import run_structured_persona
from software_engineering_team.shared.system_prompt_assembly import (
    build_system_prompt_with_content,
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
    result = build_system_prompt_with_content("persona text", None)
    assert result == "persona text"


def test_build_system_prompt_with_content_returns_str_when_empty_list() -> None:
    result = build_system_prompt_with_content("persona text", [])
    assert result == "persona text"


def test_build_system_prompt_with_content_returns_list_with_cache_breakpoint() -> None:
    bp = CacheBreakpoint("cached prefix")
    result = build_system_prompt_with_content("persona", [bp])
    assert isinstance(result, list)
    assert result[0] == {"text": "persona"}
    assert result[1] is bp


def test_build_system_prompt_with_content_normalizes_bare_strings() -> None:
    result = build_system_prompt_with_content("persona", ["extra context"])
    assert isinstance(result, list)
    assert result[0] == {"text": "persona"}
    assert result[1] == {"text": "extra context"}


def test_run_structured_persona_passes_cache_breakpoint_to_agent() -> None:
    """When system_prompt_content contains a CacheBreakpoint, the agent
    receives a list-form system_prompt with the breakpoint intact."""
    bp = CacheBreakpoint("spec excerpt")

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
    assert agent.system_prompt[1].text == "spec excerpt"


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


def test_run_structured_persona_fallback_on_agent_construction_error() -> None:
    """When agent_factory raises, fallback_factory is invoked (agent
    construction is inside the try/except)."""

    def _failing_factory(*, model: Any, system_prompt: Any) -> Any:
        raise RuntimeError("construction failed")

    sentinel = _DummyOutput()
    result = run_structured_persona(
        model=object(),
        system_prompt="persona",
        user_prompt="review",
        output_model=_DummyOutput,
        fallback_factory=lambda exc: sentinel,
        agent_factory=_failing_factory,
    )
    assert result is sentinel


# ---------------------------------------------------------------------------
# QA gate: file context stays in user prompt (not system)
# ---------------------------------------------------------------------------


def test_qa_gate_keeps_file_context_in_user_prompt() -> None:
    """QAExpertAgent.run() keeps the file-context prefix (code under review)
    in the user prompt, not system_prompt_content — untrusted code must not
    be elevated to system-level. The trusted task_description, however, IS
    elevated to a CacheBreakpoint-marked system_prompt_content segment."""
    from qa_agent import QAExpertAgent, QAInput

    from llm_service import CacheBreakpoint

    captured_kwargs: List[dict] = []

    def _spy(**kwargs: Any) -> Any:
        captured_kwargs.append(kwargs)
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

    with patch("qa_agent.agent.run_structured_persona", side_effect=_spy):
        agent = QAExpertAgent(None)
        agent._model = object()
        agent.run(input_data)

    assert len(captured_kwargs) == 1
    # system_prompt_content must carry the trusted task_description as a
    # CacheBreakpoint, but never the code under review.
    spc = captured_kwargs[0].get("system_prompt_content")
    assert spc is not None
    assert len(spc) == 1
    assert isinstance(spc[0], CacheBreakpoint)
    assert "greet" in spc[0].text
    assert "def hello(): pass" not in spc[0].text
    # The code under review must appear in the user_prompt, not task_description.
    user_prompt = captured_kwargs[0]["user_prompt"]
    assert "def hello(): pass" in user_prompt
    assert "**Language:** python" in user_prompt
    assert "greet" not in user_prompt


def test_qa_gate_no_shared_prefix_when_no_trusted_metadata() -> None:
    """When neither task_description nor architecture is set,
    system_prompt_content stays None — no empty CacheBreakpoint is created."""
    from qa_agent import QAExpertAgent, QAInput

    captured_kwargs: List[dict] = []

    def _spy(**kwargs: Any) -> Any:
        captured_kwargs.append(kwargs)
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

    input_data = QAInput(code="x = 1", language="python")

    with patch("qa_agent.agent.run_structured_persona", side_effect=_spy):
        agent = QAExpertAgent(None)
        agent._model = object()
        agent.run(input_data)

    assert len(captured_kwargs) == 1
    assert captured_kwargs[0].get("system_prompt_content") is None


def test_qa_gate_no_file_context_for_acceptance_evidence_mode() -> None:
    """In acceptance_evidence mode there is no code under review, so neither
    user_prompt nor system_prompt_content carries file context."""
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

    with patch("qa_agent.agent.run_structured_persona", side_effect=_spy):
        agent = QAExpertAgent(None)
        agent._model = object()
        agent.run(input_data)

    assert len(captured_kwargs) == 1
    spc = captured_kwargs[0].get("system_prompt_content")
    assert spc is None


# ---------------------------------------------------------------------------
# Security gate: file context stays in user prompt
# ---------------------------------------------------------------------------


def test_security_gate_keeps_file_context_in_user_prompt() -> None:
    """CybersecurityExpertAgent._build_user_prompt includes the file-context
    prefix (code under review) in the user message, not the system prompt.
    The trusted task_description is elevated out of the user prompt to the
    CacheBreakpoint segment (see test_security_gate_forwards_shared_prefix_
    as_cache_breakpoint below), so it no longer appears here."""
    from security_agent.agent import CybersecurityExpertAgent, _build_security_file_context_prefix
    from security_agent.models import SecurityInput

    input_data = SecurityInput(
        code="import os\nos.system('ls')",
        language="python",
        task_description="review command runner",
    )

    user_prompt = CybersecurityExpertAgent._build_user_prompt(input_data)

    # Code under review must be in the user prompt
    assert "import os" in user_prompt
    assert "os.system('ls')" in user_prompt
    assert "**Language:** python" in user_prompt
    # The trusted task description no longer appears in the user prompt.
    assert "review command runner" not in user_prompt

    # The file-context prefix helper returns the expected parts
    prefix_parts = _build_security_file_context_prefix(input_data)
    assert any("os.system('ls')" in part for part in prefix_parts)


def test_security_gate_forwards_shared_prefix_as_cache_breakpoint() -> None:
    """CybersecurityExpertAgent.run() forwards task_description as a
    CacheBreakpoint-marked system_prompt_content segment to
    run_single_shot_review, while the code under review stays in the
    user prompt."""
    from security_agent.agent import CybersecurityExpertAgent
    from security_agent.models import SecurityInput, SecurityLLMResponse

    from llm_service import CacheBreakpoint

    captured_kwargs: List[dict] = []

    def _spy(*args: Any, **kwargs: Any) -> Any:
        captured_kwargs.append(kwargs)
        return SecurityLLMResponse(vulnerabilities=[], summary="ok", remediations=[])

    input_data = SecurityInput(
        code="import os\nos.system('ls')",
        language="python",
        task_description="review command runner",
    )

    with patch("security_agent.agent.run_single_shot_review", side_effect=_spy):
        agent = CybersecurityExpertAgent(None)
        agent.run(input_data)

    assert len(captured_kwargs) == 1
    spc = captured_kwargs[0].get("system_prompt_content")
    assert spc is not None
    assert len(spc) == 1
    assert isinstance(spc[0], CacheBreakpoint)
    assert "review command runner" in spc[0].text
    assert "os.system" not in spc[0].text
    assert "os.system" in captured_kwargs[0]["prompt"]


def test_security_gate_no_shared_prefix_when_no_trusted_metadata() -> None:
    """When neither task_description nor architecture is set,
    system_prompt_content stays None."""
    from security_agent.agent import CybersecurityExpertAgent
    from security_agent.models import SecurityInput, SecurityLLMResponse

    captured_kwargs: List[dict] = []

    def _spy(*args: Any, **kwargs: Any) -> Any:
        captured_kwargs.append(kwargs)
        return SecurityLLMResponse(vulnerabilities=[], summary="ok", remediations=[])

    input_data = SecurityInput(code="x = 1", language="python")

    with patch("security_agent.agent.run_single_shot_review", side_effect=_spy):
        agent = CybersecurityExpertAgent(None)
        agent.run(input_data)

    assert len(captured_kwargs) == 1
    assert captured_kwargs[0].get("system_prompt_content") is None


# ---------------------------------------------------------------------------
# Code Review gate: _build_shared_review_prefix for trusted metadata
# ---------------------------------------------------------------------------


def test_build_shared_review_prefix_produces_non_empty_breakpoint_text() -> None:
    """_build_shared_review_prefix returns non-empty text parts from trusted
    metadata (spec/architecture/existing-code) that can be wrapped in a
    CacheBreakpoint for system_prompt_content."""
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
    _build_shared_review_prefix returns an empty list."""
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
