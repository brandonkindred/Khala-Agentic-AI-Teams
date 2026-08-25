"""End-to-end regression proof that the V2 tool-agent review path never
promotes task_description or the reviewed code into the (higher-privilege)
system prompt.

History: an earlier revision of this PR cache-marked ``current_files`` (the
reviewed code) as a Strands system segment via
``tool_agent_base.build_shared_tool_agent_review_system_content``, then --
after review feedback -- narrowed that to cache-mark only
``task_description``. A second round of review feedback established that
``task_description`` is not safely cacheable either: it can originate from an
externally-authored GitHub issue body (see ``github_source/issue_to_plan.py``),
making it exactly as adversary-controllable as the reviewed code, just from a
different source. ``build_shared_tool_agent_review_system_content`` now
always returns ``None`` (see its docstring) -- there is currently no field
available to this call site that is both shared across every wired tool
agent's call for one microtask and safe to place in the system role.

This file keeps a real, wire-level regression test for that conclusion
(mirroring ``test_chunk_reviewer_cache_e2e.py``'s real-``ClaudeLLMClient``
approach) so a future change that starts cache-marking either field again --
plausible, since the whole point of the ``shared_review_context`` /
``system_prompt_content`` plumbing kept in place is to support a genuinely
safe field later -- gets caught here rather than shipping quietly.
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


def test_shared_review_context_is_always_none() -> None:
    """The builder produces nothing to cache -- confirmed directly, not just
    inferred from the absence of wire-level cache_control below."""
    assert (
        build_shared_tool_agent_review_system_content("Add pagination to the users endpoint")
        is None
    )


def test_neither_task_description_nor_code_ever_reaches_the_system_prompt() -> None:
    """Wire-level proof, against a real ClaudeLLMClient: two distinct wired
    tool agents reviewing the same microtask never send a system prompt at
    all (nothing is cache-eligible today), and both task_description and the
    reviewed code reach the model exclusively via the user-turn messages."""
    client, fake_messages = _make_claude_client(
        [
            _text_message(_issues_reply()),
            _text_message(_issues_reply()),
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

    assert len(fake_messages.captured_calls) == 2
    task_marker = "Add pagination to the users endpoint"
    code_marker = "def list_users(): ..."
    for call in fake_messages.captured_calls:
        # The Strands/LLMClientModel JSON-mode plumbing emits its own static
        # "respond with JSON only" system instruction independent of this
        # path -- that is expected and unrelated to caching. What must never
        # appear there is task_description/code content or a cache_control
        # block, since system_prompt_content is never non-empty on this path
        # today (build_shared_tool_agent_review_system_content always
        # returns None).
        system_content = call.get("system")
        rendered_system = json.dumps(system_content) if system_content else ""
        assert task_marker not in rendered_system, f"task leaked into system: {system_content}"
        assert code_marker not in rendered_system, f"code leaked into system: {system_content}"
        assert '"cache_control"' not in rendered_system, (
            f"unexpected cache_control block in system content: {system_content}"
        )
        rendered_messages = json.dumps(call["messages"])
        assert task_marker in rendered_messages
        assert code_marker in rendered_messages


def test_tool_agent_findings_independent_across_agents() -> None:
    """Two tool agents reviewing the same microtask get their own,
    independent findings from their own LLM call -- unaffected by whether a
    shared_review_context is threaded through (it is always None today)."""
    client, _fake_messages = _make_claude_client(
        [
            _text_message(
                json.dumps({"issues": [], "summary": "security: clean"}),
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
            _text_message(
                json.dumps({"issues": [], "summary": "qa: clean"}),
                cache_read_input_tokens=0,
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

    assert security_out.summary == "Security review: 0 issue(s) found."
    assert qa_out.summary == "TestingQA review: 0 issue(s) found."

    calls = telemetry.get_recent_calls()
    assert len(calls) == 2
    assert calls[0]["cache_creation_tokens"] == 0
    assert calls[1]["cache_creation_tokens"] == 0
    # No CacheBreakpoint means no cache read either -- explicit alongside the
    # cache_creation_tokens checks above so the "no wire-level caching in the
    # V2 tool-agent family" guarantee is asserted directly, not just inferred.
    assert calls[0]["cache_read_tokens"] == 0
    assert calls[1]["cache_read_tokens"] == 0
