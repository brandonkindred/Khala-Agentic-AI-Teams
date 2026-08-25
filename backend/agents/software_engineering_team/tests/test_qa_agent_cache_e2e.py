"""End-to-end proof that QA's CacheBreakpoint-marked shared review prefix
(``qa_agent.agent._build_qa_shared_review_prefix`` -- ``task_description`` +
``architecture.overview``, see PR #7191) actually pays off across two
``QAExpertAgent.run()`` calls sharing one microtask: a real second call reads
a non-zero ``cache_read_tokens`` off the identical system-level prefix, and
QA findings are unchanged whether or not a call was cache-served.

Mirrors ``test_chunk_reviewer_cache_e2e.py``'s proof pattern (real
``ClaudeLLMClient``, fake Anthropic SDK underneath), adapted for
``QAExpertAgent.run()``'s single LLM call per invocation (``run_structured_persona``
makes one ``structured_output_model`` call -- no separate reasoning/formatting
pass like ``ChunkReviewAgent``, so two ``run()`` calls need exactly two
scripted replies, not four).

Unlike ``chunk_reviewer``'s formatting pass or the V2 tool-agent family's JSON
parse mode (both of which get away with a plain-text JSON reply),
``run_structured_persona``'s ``agent(user_prompt, structured_output_model=QAOutput)``
call drives Strands' *forced tool-call* structured-output mechanism (see
``test_review_cycle_cache_e2e.py``'s docstring, which documents this same gap
for the plain-text ``_text_message`` fake): the model must reply with a
``tool_use`` content block invoking a tool named after the output model class
(``"QAOutput"``, see ``strands.tools.structured_output.structured_output_utils
.convert_pydantic_to_tool_spec``), not a text block containing JSON.
``llm_client_fakes._tool_use_message`` scripts that tool_use block directly
so the real ``ClaudeLLMClient`` -> ``LLMClientModel`` -> Strands ``Agent``
chain completes successfully, closing the gap left open there for QA
specifically.

QA also carries its own whole-input ``ReviewResultCache`` (keyed on the full
``QAInput`` + resolved model, unrelated to Anthropic wire-level prompt
caching): a byte-identical second ``QAInput`` would otherwise skip the LLM
call entirely and never reach the wire. Every test here disables that cache
via ``QA_REVIEW_CACHE_SIZE=0`` so both calls are genuine.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
from qa_agent.agent import QAExpertAgent
from qa_agent.models import QAInput

from llm_client_fakes import _make_claude_client, _tool_use_message
from llm_service import telemetry
from llm_service.strands_adapter import LLMClientModel
from shared.dev_models.models import SystemArchitecture

pytestmark = [pytest.mark.usefixtures("_reset_llm_telemetry_state")]

_SHARED_ARCHITECTURE = SystemArchitecture(
    overview="Architecture overview shared across every call of this microtask.",
    components=[],
    architecture_document="",
)


def _qa_input(code: str) -> QAInput:
    """Build one call's ``QAInput``, sharing the run-wide cache-marked prefix.

    ``task_description``/``architecture`` are byte-identical across calls --
    exactly the fields ``_build_qa_shared_review_prefix`` wraps in a
    ``CacheBreakpoint``. ``code`` varies so the unrelated whole-input
    ``ReviewResultCache`` doesn't collide on its own key.
    """
    return QAInput(
        code=code,
        language="python",
        task_description="Add pagination to the users endpoint",
        architecture=_SHARED_ARCHITECTURE,
    )


def _qa_output_payload(summary: str) -> Dict[str, Any]:
    """Build a ``QAOutput``-shaped dict for a scripted tool-call ``input``.

    QA-specific (the required/defaulted field set is ``QAOutput``'s, not a
    generic fake concern), so it stays local rather than living in the
    shared ``llm_client_fakes`` module alongside ``_tool_use_message``.
    """
    return {
        "bugs_found": [],
        "approved": True,
        "summary": summary,
        "integration_tests": "",
        "unit_tests": "",
        "test_plan": "",
        "live_test_notes": "",
        "readme_content": "",
        "suggested_commit_message": "",
    }


def test_second_qa_call_reads_nonzero_cache_after_first_call_writes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QA's shared task/architecture prefix is byte-identical across calls of
    one microtask, so the second call reads back a non-zero
    ``cache_read_tokens`` on the exact same wire payload the first call
    wrote."""
    monkeypatch.setenv("QA_REVIEW_CACHE_SIZE", "0")
    client, fake_messages = _make_claude_client(
        [
            _tool_use_message(
                "QAOutput",
                _qa_output_payload("Call A: no bugs."),
                cache_read_input_tokens=0,
                cache_creation_input_tokens=512,
            ),
            _tool_use_message(
                "QAOutput",
                _qa_output_payload("Call B: no bugs."),
                cache_read_input_tokens=512,
                cache_creation_input_tokens=0,
            ),
        ]
    )
    model = LLMClientModel(client, agent_key="qa")
    agent = QAExpertAgent(model)

    agent.run(_qa_input("def list_users(): ..."))
    agent.run(_qa_input("def paginate(items): ..."))

    calls = telemetry.get_recent_calls()
    assert len(calls) == 2
    first_call, second_call = calls

    assert first_call["cache_read_tokens"] == 0
    assert first_call["cache_creation_tokens"] == 512
    # The acceptance-criterion assertion: non-zero cache_read on the 2nd+ call.
    assert second_call["cache_read_tokens"] == 512
    assert second_call["cache_creation_tokens"] == 0

    # Corroborate at the wire level: the two calls' cache-marked system
    # segment is byte-identical -- the real-world precondition for Anthropic
    # to have served that cache hit at all, not just a scripted telemetry
    # number.
    first_system, second_system = (call["system"] for call in fake_messages.captured_calls)
    assert first_system == second_system
    cache_marked = [
        block
        for block in first_system
        if isinstance(block, dict) and block.get("cache_control") == {"type": "ephemeral"}
    ]
    assert cache_marked, f"expected a cache_control block in system content, got {first_system}"


def test_qa_findings_unchanged_when_second_call_is_cache_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reviewing the identical microtask twice yields identical QA findings,
    whether or not the second call happened to be cache-served -- caching
    must never change what QA reports."""
    monkeypatch.setenv("QA_REVIEW_CACHE_SIZE", "0")
    client, _fake_messages = _make_claude_client(
        [
            _tool_use_message(
                "QAOutput",
                _qa_output_payload("No bugs found."),
                cache_read_input_tokens=0,
                cache_creation_input_tokens=512,
            ),
            _tool_use_message(
                "QAOutput",
                _qa_output_payload("No bugs found."),
                cache_read_input_tokens=512,
                cache_creation_input_tokens=0,
            ),
        ]
    )
    model = LLMClientModel(client, agent_key="qa")
    agent = QAExpertAgent(model)
    qa_input = _qa_input("def list_users(): ...")

    first = agent.run(qa_input)
    second = agent.run(qa_input)

    assert first == second

    calls = telemetry.get_recent_calls()
    assert len(calls) == 2
    assert calls[0]["cache_read_tokens"] == 0  # first call: cache miss
    assert calls[1]["cache_read_tokens"] == 512  # second, identical call: cache-served
