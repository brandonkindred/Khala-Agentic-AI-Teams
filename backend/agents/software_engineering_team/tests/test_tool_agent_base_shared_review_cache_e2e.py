"""End-to-end proof that the V2 tool-agent review path's shared code/task
cache breakpoint (``tool_agent_base.build_shared_tool_agent_review_system_content``
/ ``BaseReviewToolAgent.review``) actually pays off across distinct wired tool
agents in one microtask: a second tool agent's call reads a non-zero
``cache_read_tokens`` off the shared segment the first agent's call wrote, and
review findings are unaffected by whether a call was cache-served.

Mirrors ``test_chunk_reviewer_cache_e2e.py``'s proof pattern (real
``ClaudeLLMClient`` against a scripted, fake Anthropic SDK), adapted to this
path's shape: each wired tool agent's ``review()`` makes exactly one LLM call
(no separate reasoning/formatting pass), so two agents sharing one microtask's
context need two scripted replies, in order.
"""

from __future__ import annotations

import json

import pytest
from strands import Agent  # noqa: F401  (resolved by BaseReviewToolAgent._agent_factory)

from llm_client_fakes import _make_claude_client, _text_message
from llm_service import telemetry
from llm_service.strands_adapter import LLMClientModel
from software_engineering_team.shared.tool_agent_base import (
    BaseReviewToolAgent,
    build_shared_tool_agent_review_system_content,
)
from software_engineering_team.shared.v2_models import ToolAgentPhaseInput

pytestmark = [pytest.mark.usefixtures("_reset_llm_telemetry_state")]


class _SecurityDemoAgent(BaseReviewToolAgent):
    name = "Security"
    issue_source = "security"
    review_prompt = (
        "You are a security reviewer. Report issues as JSON.\n\n"
        "**Task:** {task_description}\n\n**Code to review:**\n{code}"
    )
    review_parse_mode = "json"


class _QaDemoAgent(BaseReviewToolAgent):
    name = "TestingQA"
    issue_source = "testing_qa"
    review_prompt = (
        "You are a QA reviewer. Report issues as JSON.\n\n"
        "**Task:** {task_description}\n\n**Code to review:**\n{code}"
    )
    review_parse_mode = "json"


def _issues_reply(*, count: int = 0) -> str:
    return json.dumps({"issues": [], "summary": f"{count} issue(s)."})


def _phase_input(shared_ctx) -> ToolAgentPhaseInput:
    return ToolAgentPhaseInput(
        current_files={"app/main.py": "def list_users(): ..."},
        task_description="Add pagination to the users endpoint",
        shared_review_context=shared_ctx,
    )


def test_second_tool_agent_call_reads_nonzero_cache_after_first_writes_it() -> None:
    """Security's review() call writes the shared code/task cache breakpoint;
    QA's review() call for the same microtask reads it back as a cache hit."""
    client, fake_messages = _make_claude_client(
        [
            _text_message(
                _issues_reply(), cache_read_input_tokens=0, cache_creation_input_tokens=512
            ),
            _text_message(
                _issues_reply(), cache_read_input_tokens=512, cache_creation_input_tokens=0
            ),
        ]
    )
    model = LLMClientModel(client, agent_key="tool_agent_review")
    security_agent = _SecurityDemoAgent(model)
    qa_agent = _QaDemoAgent(model)

    shared_ctx = build_shared_tool_agent_review_system_content(
        "Add pagination to the users endpoint"
    )
    phase_inp = _phase_input(shared_ctx)

    security_agent.review(phase_inp)
    qa_agent.review(phase_inp)

    calls = telemetry.get_recent_calls()
    assert len(calls) == 2
    security_call, qa_call = calls

    assert security_call["cache_read_tokens"] == 0
    assert security_call["cache_creation_tokens"] == 512
    # The acceptance-criterion assertion: non-zero cache_read on the second+
    # wired tool agent's call within one microtask.
    assert qa_call["cache_read_tokens"] == 512
    assert qa_call["cache_creation_tokens"] == 0

    # Corroborate at the wire level: the two calls' cache-marked system
    # segment is byte-identical -- the real-world precondition for Anthropic
    # to have served that cache hit at all, not just a scripted telemetry
    # number. The two agents' own (uncached) role instructions differ, so
    # only the shared segment -- not the whole system block -- is compared.
    first_system = fake_messages.captured_calls[0]["system"]
    second_system = fake_messages.captured_calls[1]["system"]
    first_cache_marked = [
        block
        for block in first_system
        if isinstance(block, dict) and block.get("cache_control") == {"type": "ephemeral"}
    ]
    second_cache_marked = [
        block
        for block in second_system
        if isinstance(block, dict) and block.get("cache_control") == {"type": "ephemeral"}
    ]
    assert first_cache_marked, (
        f"expected a cache_control block in system content, got {first_system}"
    )
    assert first_cache_marked == second_cache_marked

    # Security invariant, proven at the wire level (not just asserted in
    # docstrings): the reviewed code is untrusted, repository-controlled
    # content and must never appear in the (higher-privilege) system prompt
    # -- only the internal task description is cache-eligible. It must still
    # reach the model, via the user-turn messages.
    code_marker = "def list_users(): ..."
    for system_block in (first_system, second_system):
        rendered_system = json.dumps(system_block)
        assert code_marker not in rendered_system, (
            f"reviewed code leaked into the system prompt: {system_block}"
        )
    for call in fake_messages.captured_calls:
        assert code_marker in json.dumps(call["messages"])


def test_tool_agent_findings_unchanged_regardless_of_cache_state() -> None:
    """Two tool agents reviewing the same shared context get their own,
    independent findings -- caching the shared segment must never leak one
    agent's parsed result into another's, or change what either reports."""
    client, _fake_messages = _make_claude_client(
        [
            _text_message(
                json.dumps({"issues": [], "summary": "security: clean"}),
                cache_read_input_tokens=0,
                cache_creation_input_tokens=512,
            ),
            _text_message(
                json.dumps({"issues": [], "summary": "qa: clean"}),
                cache_read_input_tokens=512,
                cache_creation_input_tokens=0,
            ),
        ]
    )
    model = LLMClientModel(client, agent_key="tool_agent_review")
    security_agent = _SecurityDemoAgent(model)
    qa_agent = _QaDemoAgent(model)

    shared_ctx = build_shared_tool_agent_review_system_content(
        "Add pagination to the users endpoint"
    )
    phase_inp = _phase_input(shared_ctx)

    security_out = security_agent.review(phase_inp)
    qa_out = qa_agent.review(phase_inp)

    assert "Security review: 0 issue(s) found." == security_out.summary
    assert "TestingQA review: 0 issue(s) found." == qa_out.summary

    calls = telemetry.get_recent_calls()
    assert calls[0]["cache_read_tokens"] == 0  # security: cache miss (first writer)
    assert calls[1]["cache_read_tokens"] == 512  # qa: cache-served
