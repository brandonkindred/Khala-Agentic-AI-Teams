"""End-to-end proof that the code-review map phase's shared-prefix cache
breakpoint (``chunk_reviewer._build_shared_review_prefix`` /
``_run_chunk_review``, see PR #6749) actually pays off across chunks of one
review run: a real second chunk reads a non-zero ``cache_read_tokens`` off
the shared prefix, and review findings are unchanged whether a call was
cache-served or not.

Scope: this module drives ``ChunkReviewAgent`` directly, twice, against one
shared ``LLMClientModel``-wrapped ``ClaudeLLMClient`` (a real client, fake
Anthropic SDK underneath — mirrors ``llm_service/tests/test_cache_breakpoint_e2e.py``).
It deliberately does not drive the coordinator's parallel ``mapping.py``
chunk fan-out: a provider cache hit depends only on the wire-level prefix
being byte-identical across independent HTTP calls, not on how those calls
are scheduled, and the coordinator already constructs exactly one
``ChunkReviewAgent(llm)`` per run and reuses it across every chunk
(``coordinator.py``'s map phase) — two direct ``.run()`` calls against one
injected model faithfully reproduce that sharing. Each chunk review makes
exactly two LLM calls (the reasoning pass, which carries the cache
breakpoint, then the formatting pass, which never does — see
``chunk_reviewer._run_chunk_review``'s docstring), so two chunks need four
scripted Anthropic replies, in that fixed order.
"""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest
from code_review_agent.chunk_reviewer import ChunkReviewAgent
from code_review_agent.models import ChunkReviewInput

from llm_client_fakes import _make_claude_client, _text_message
from llm_service import telemetry
from llm_service.interface import reset_complete_json_observer_state
from llm_service.strands_adapter import LLMClientModel


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """Isolate each test's telemetry and JSON-observer state.

    Precondition: none.
    Postcondition: the telemetry call log is empty before the test runs, and
    the ``complete_json`` observer's per-turn state is empty both before and
    after, so no test leaks call records into the next.
    """
    telemetry.clear_call_log()
    reset_complete_json_observer_state()
    yield
    reset_complete_json_observer_state()


def _shared_context() -> Dict[str, Any]:
    """Shared context fields used for every chunk of one coordinator run.

    ``spec_excerpt``, ``architecture_overview``, and
    ``existing_codebase_excerpt`` form the byte-identical shared prefix that
    ``_build_shared_review_prefix`` marks as a ``CacheBreakpoint``.
    ``task_description`` and ``spec_compliance_single_pass`` are also shared
    across chunks but are not part of the cache-marked prefix.
    """
    return {
        "task_description": "Add pagination to the users endpoint",
        "spec_excerpt": "Spec excerpt shared across every chunk of this run.",
        "spec_compliance_single_pass": False,
        "architecture_overview": "Architecture overview shared across every chunk.",
        "existing_codebase_excerpt": "Existing codebase excerpt shared across every chunk.",
    }


def _chunk_input(code_chunk: str, file_path: str) -> ChunkReviewInput:
    """Build one chunk's ``ChunkReviewInput``, sharing the run-wide context.

    ``code_chunk``/``file_path`` vary per chunk; every other field comes from
    :func:`_shared_context`, so two calls with different chunk args still
    carry the identical shared prefix a real coordinator run would produce.
    """
    return ChunkReviewInput(
        code_chunk=code_chunk, file_path_or_label=file_path, **_shared_context()
    )


def _format_reply(summary: str) -> str:
    """Return the formatting-pass JSON reply a canned ``_text_message`` carries.

    Always ``approved=True`` with no issues, so it trivially satisfies
    ``ChunkReviewLLMResponse``'s approval/issues consistency validator --
    these tests assert on cache telemetry and output equality, not on
    findings content.
    """
    return json.dumps(
        {"approved": True, "issues": [], "summary": summary, "spec_compliance_notes": ""}
    )


def test_second_chunk_reasoning_call_reads_nonzero_cache_after_first_chunk_writes_it() -> None:
    """The map phase's shared spec/architecture/existing-code prefix is
    byte-identical across chunks, so the second chunk's reasoning call reads
    back a non-zero ``cache_read_tokens`` on the exact same wire payload the
    first chunk wrote."""
    client, fake_messages = _make_claude_client(
        [
            _text_message(
                "Chunk A: no issues found.",
                cache_read_input_tokens=0,
                cache_creation_input_tokens=512,
            ),
            _text_message(_format_reply("Chunk A summary")),
            _text_message(
                "Chunk B: no issues found.",
                cache_read_input_tokens=512,
                cache_creation_input_tokens=0,
            ),
            _text_message(_format_reply("Chunk B summary")),
        ]
    )
    model = LLMClientModel(client, agent_key="code_review")
    agent = ChunkReviewAgent(model)

    agent.run(_chunk_input("### app/main.py ###\ndef list_users(): ...", "app/main.py"))
    agent.run(_chunk_input("### app/utils.py ###\ndef paginate(items): ...", "app/utils.py"))

    calls = telemetry.get_recent_calls()
    assert len(calls) == 4
    chunk_a_reasoning, _chunk_a_format, chunk_b_reasoning, _chunk_b_format = calls

    assert chunk_a_reasoning["cache_read_tokens"] == 0
    assert chunk_a_reasoning["cache_creation_tokens"] == 512
    # The acceptance-criterion assertion: non-zero cache_read on chunk 2+.
    assert chunk_b_reasoning["cache_read_tokens"] == 512
    assert chunk_b_reasoning["cache_creation_tokens"] == 0

    # Corroborate at the wire level: the two reasoning calls' cache-marked
    # system segment is byte-identical -- the real-world precondition for
    # Anthropic to have served that cache hit at all, not just a scripted
    # telemetry number.
    reasoning_calls = [fake_messages.captured_calls[0], fake_messages.captured_calls[2]]
    first_system, second_system = (call["system"] for call in reasoning_calls)
    assert first_system == second_system
    cache_marked = [
        block
        for block in first_system
        if isinstance(block, dict) and block.get("cache_control") == {"type": "ephemeral"}
    ]
    assert cache_marked, f"expected a cache_control block in system content, got {first_system}"


def test_chunk_review_findings_unchanged_when_second_call_is_cache_served() -> None:
    """Reviewing the identical chunk twice yields identical findings, whether
    or not the second call happened to be cache-served -- caching must never
    change what the reviewer reports."""
    client, _fake_messages = _make_claude_client(
        [
            _text_message(
                "No issues found.", cache_read_input_tokens=0, cache_creation_input_tokens=512
            ),
            _text_message(_format_reply("Looks good.")),
            _text_message(
                "No issues found.", cache_read_input_tokens=512, cache_creation_input_tokens=0
            ),
            _text_message(_format_reply("Looks good.")),
        ]
    )
    model = LLMClientModel(client, agent_key="code_review")
    agent = ChunkReviewAgent(model)
    chunk_input = _chunk_input("### app/main.py ###\ndef list_users(): ...", "app/main.py")

    first = agent.run(chunk_input)
    second = agent.run(chunk_input)

    assert first == second

    calls = telemetry.get_recent_calls()
    assert len(calls) == 4
    assert calls[0]["cache_read_tokens"] == 0  # first call: cache miss
    assert calls[2]["cache_read_tokens"] == 512  # second, identical call: cache-served
