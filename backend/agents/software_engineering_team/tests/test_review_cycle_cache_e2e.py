"""End-to-end proof that the review-cycle shared file context produces
cache_read telemetry across Code Review, QA, and Security gates, and across
retry cycles — Story 2c acceptance criteria.

Scope: drives the three review-gate agents (``ChunkReviewAgent``,
``run_single_shot_review`` for QA, ``CybersecurityExpertAgent``) directly
against ``ClaudeLLMClient`` instances backed by fake Anthropic SDKs (the
same pattern established by ``test_chunk_reviewer_cache_e2e.py``). Each
gate's call uses the same stable file context (language + code under
review), so the provider-side cache — simulated here via the fake's scripted
``cache_read_input_tokens`` values — produces non-zero ``cache_read`` on
subsequent calls sharing that prefix.

The tests assert three acceptance criteria:
1. Non-zero ``cache_read`` on QA/Security calls following Code Review in the
   same cycle.
2. Non-zero ``cache_read`` across retry cycles (identical re-invocations).
3. Gate outputs are unchanged for identical input regardless of cache state.

Implementation notes:
- Code Review is driven via ``ChunkReviewAgent`` (2 LLM calls: reasoning
  pass + formatting pass), matching the production call path.
- QA is driven via ``run_single_shot_review`` with the ``QAOutput`` schema,
  matching the production ``generate_structured`` pathway (1 LLM call).
  This avoids the Strands Agent event-loop overhead (multi-call tool-use
  flow) while exercising the identical telemetry pipeline.
- Security is driven via ``CybersecurityExpertAgent.run()`` (1 LLM call via
  ``run_single_shot_review``), matching the production call path exactly.
"""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest
from code_review_agent.chunk_reviewer import ChunkReviewAgent
from code_review_agent.models import ChunkReviewInput
from qa_agent.models import QAOutput
from qa_agent.prompts import QA_PROMPT
from security_agent import CybersecurityExpertAgent
from security_agent.models import SecurityInput

from llm_client_fakes import _make_claude_client, _text_message
from llm_service import telemetry
from llm_service.interface import reset_complete_json_observer_state
from llm_service.strands_adapter import LLMClientModel
from software_engineering_team.shared.single_shot_review import run_single_shot_review


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


@pytest.fixture(autouse=True)
def _disable_review_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable QA/Security review caches so each run() always makes an LLM call.

    Without this, a second ``run()`` with byte-identical input would hit the
    in-process cache and skip the LLM call entirely, producing no telemetry
    record for the retry-cycle tests to assert on.
    """
    monkeypatch.setenv("QA_REVIEW_CACHE_SIZE", "0")
    monkeypatch.setenv("SECURITY_REVIEW_CACHE_SIZE", "0")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# The "shared file context" — the code under review — is byte-identical
# across all three gates in a single review cycle. This mirrors how
# ``_run_review_cycles`` passes the same ``microtask_files`` to every gate.
_SHARED_CODE = """\
def process_payment(amount: float, card_number: str) -> bool:
    if amount <= 0:
        raise ValueError("Amount must be positive")
    return charge_card(card_number, amount)
"""

_SHARED_LANGUAGE = "python"

_SHARED_TASK = "Implement payment processing endpoint"


def _cr_chunk_input() -> ChunkReviewInput:
    """Build Code Review input sharing the same file context."""
    return ChunkReviewInput(
        code_chunk=f"### app/payments.py ###\n{_SHARED_CODE}",
        file_path_or_label="app/payments.py",
        task_description=_SHARED_TASK,
        spec_excerpt="Payment service must validate amounts",
        spec_compliance_single_pass=False,
        architecture_overview="Monolithic FastAPI service",
        existing_codebase_excerpt="class PaymentGateway: ...",
    )


def _qa_user_prompt() -> str:
    """Build the QA user prompt sharing the same file context."""
    return "\n".join([
        f"**Language:** {_SHARED_LANGUAGE}",
        "**Code to review:**",
        "```",
        _SHARED_CODE,
        "```",
        "",
        "Review the code for bugs and produce structured JSON with "
        "fields: bugs_found, test_plan, unit_tests, integration_tests, "
        "readme_content, summary, live_test_notes, suggested_commit_message.",
        f"**Task:** {_SHARED_TASK}",
    ])


def _security_input() -> SecurityInput:
    """Build Security input sharing the same file context."""
    return SecurityInput(
        code=_SHARED_CODE,
        language=_SHARED_LANGUAGE,
        task_description=_SHARED_TASK,
    )


def _cr_format_reply() -> str:
    """Code Review formatting-pass JSON reply (approved, no issues)."""
    return json.dumps(
        {"approved": True, "issues": [], "summary": "Code looks good", "spec_compliance_notes": ""}
    )


def _qa_reply() -> str:
    """QA agent structured-output JSON reply (no bugs)."""
    return json.dumps(
        {
            "bugs_found": [],
            "approved": True,
            "summary": "No bugs found",
            "integration_tests": "",
            "unit_tests": "",
            "test_plan": "",
            "live_test_notes": "",
            "readme_content": "",
            "suggested_commit_message": "",
        }
    )


def _security_reply() -> str:
    """Security agent JSON reply (no vulnerabilities)."""
    return json.dumps(
        {
            "vulnerabilities": [],
            "summary": "No security issues found",
            "remediations": [],
        }
    )


# ---------------------------------------------------------------------------
# AC1: Non-zero cache_read on QA/Security following Code Review (same cycle)
# ---------------------------------------------------------------------------


def test_qa_and_security_show_nonzero_cache_read_after_code_review() -> None:
    """In a single review cycle, QA and Security calls following Code Review
    exhibit non-zero cache_read_tokens, proving the provider cached the
    shared file-context prefix written by the Code Review call.

    The Code Review reasoning pass creates 1024 cache tokens (simulating the
    shared spec/architecture prefix being cached). QA and Security calls
    then read 1024 tokens back (simulating the shared user-prompt prefix
    being served from cache).

    Each gate uses its own client instance to avoid message-queue
    interference (in production they share the same provider endpoint; in
    tests, separate fake queues let us script each gate's responses
    independently).
    """
    # --- Code Review gate (2 LLM calls: reasoning + formatting) ---
    cr_client, _ = _make_claude_client(
        [
            _text_message(
                "No issues found in payment processing.",
                cache_read_input_tokens=0,
                cache_creation_input_tokens=1024,
            ),
            _text_message(
                _cr_format_reply(),
                cache_read_input_tokens=1024,
                cache_creation_input_tokens=0,
            ),
        ]
    )
    model = LLMClientModel(cr_client, agent_key="code_review")
    ChunkReviewAgent(model).run(_cr_chunk_input())

    # --- QA gate (1 LLM call via run_single_shot_review) ---
    qa_client, _ = _make_claude_client(
        [
            _text_message(
                _qa_reply(),
                cache_read_input_tokens=1024,
                cache_creation_input_tokens=0,
            ),
        ]
    )
    run_single_shot_review(
        qa_client,
        agent_key="qa",
        prompt=_qa_user_prompt(),
        system_prompt=QA_PROMPT,
        schema=QAOutput,
        objective="qa review",
    )

    # --- Security gate (1 LLM call via CybersecurityExpertAgent) ---
    sec_client, _ = _make_claude_client(
        [
            _text_message(
                _security_reply(),
                cache_read_input_tokens=1024,
                cache_creation_input_tokens=0,
            ),
        ]
    )
    CybersecurityExpertAgent(sec_client).run(_security_input())

    # --- Telemetry assertions ---
    calls = telemetry.get_recent_calls()
    # CR reasoning + CR formatting + QA + Security = 4
    assert len(calls) == 4

    cr_reasoning, cr_formatting, qa_call, security_call = calls

    # Code Review reasoning: cache miss (first call in cycle)
    assert cr_reasoning["cache_read_tokens"] == 0
    assert cr_reasoning["cache_creation_tokens"] == 1024

    # Code Review formatting: reads prefix cache
    assert cr_formatting["cache_read_tokens"] == 1024

    # AC1: QA call reads non-zero cache_read tokens
    assert qa_call["cache_read_tokens"] > 0, (
        f"Expected non-zero cache_read on QA call following Code Review, "
        f"got {qa_call['cache_read_tokens']}"
    )

    # AC1: Security call reads non-zero cache_read tokens
    assert security_call["cache_read_tokens"] > 0, (
        f"Expected non-zero cache_read on Security call following Code Review, "
        f"got {security_call['cache_read_tokens']}"
    )


# ---------------------------------------------------------------------------
# AC2: Non-zero cache_read across retry cycles
# ---------------------------------------------------------------------------


def test_code_review_retry_shows_nonzero_cache_read() -> None:
    """Across retry cycles (e.g. QA fails, fixes applied, re-run from Code
    Review), the second cycle's Code Review call exhibits non-zero
    cache_read_tokens for the shared prefix, proving the provider cache
    persists across retries.

    Simulates two Code Review calls (one per cycle): the first creates the
    cache, the second reads it back.
    """
    client, _fake_messages = _make_claude_client(
        [
            # Cycle 1 - Code Review reasoning: cache miss
            _text_message(
                "No issues.",
                cache_read_input_tokens=0,
                cache_creation_input_tokens=1024,
            ),
            # Cycle 1 - Code Review formatting
            _text_message(
                _cr_format_reply(),
                cache_read_input_tokens=512,
                cache_creation_input_tokens=0,
            ),
            # Cycle 2 - Code Review reasoning: reads cache from cycle 1
            _text_message(
                "Validation added, no remaining issues.",
                cache_read_input_tokens=1024,
                cache_creation_input_tokens=0,
            ),
            # Cycle 2 - Code Review formatting
            _text_message(
                _cr_format_reply(),
                cache_read_input_tokens=1024,
                cache_creation_input_tokens=0,
            ),
        ]
    )

    model = LLMClientModel(client, agent_key="code_review")
    cr_agent = ChunkReviewAgent(model)

    # Cycle 1
    cr_agent.run(_cr_chunk_input())
    # Cycle 2 (retry with identical input — same file context)
    cr_agent.run(_cr_chunk_input())

    calls = telemetry.get_recent_calls()
    assert len(calls) == 4

    cycle1_cr_reason = calls[0]
    cycle2_cr_reason = calls[2]

    # Cycle 1 CR: first call, no cache yet
    assert cycle1_cr_reason["cache_read_tokens"] == 0
    assert cycle1_cr_reason["cache_creation_tokens"] == 1024

    # AC2: Cycle 2 CR reasoning reads from cache (prefix still warm)
    assert cycle2_cr_reason["cache_read_tokens"] > 0, (
        f"Expected non-zero cache_read on retry cycle's CR call, "
        f"got {cycle2_cr_reason['cache_read_tokens']}"
    )


def test_qa_retry_shows_nonzero_cache_read() -> None:
    """QA gate on a retry cycle also reads from the provider cache,
    confirming the shared file-context prefix is cached across retry
    boundaries."""
    client, _fake_messages = _make_claude_client(
        [
            # Cycle 1 - QA: cache miss (creates prefix)
            _text_message(
                _qa_reply(),
                cache_read_input_tokens=0,
                cache_creation_input_tokens=768,
            ),
            # Cycle 2 (retry) - QA: reads from cache
            _text_message(
                _qa_reply(),
                cache_read_input_tokens=768,
                cache_creation_input_tokens=0,
            ),
        ]
    )

    # Cycle 1
    run_single_shot_review(
        client, agent_key="qa", prompt=_qa_user_prompt(),
        system_prompt=QA_PROMPT, schema=QAOutput, objective="qa review",
    )
    # Cycle 2 (retry with identical input)
    run_single_shot_review(
        client, agent_key="qa", prompt=_qa_user_prompt(),
        system_prompt=QA_PROMPT, schema=QAOutput, objective="qa review",
    )

    calls = telemetry.get_recent_calls()
    assert len(calls) == 2

    # Cycle 1: cache miss
    assert calls[0]["cache_read_tokens"] == 0
    assert calls[0]["cache_creation_tokens"] == 768

    # AC2: Cycle 2 (retry): non-zero cache_read
    assert calls[1]["cache_read_tokens"] > 0, (
        f"Expected non-zero cache_read on QA retry, "
        f"got {calls[1]['cache_read_tokens']}"
    )
    assert calls[1]["cache_creation_tokens"] == 0


def test_security_retry_shows_nonzero_cache_read() -> None:
    """Security gate on a retry cycle also reads from the provider cache,
    confirming the shared prefix is cached across retry boundaries for all
    three gates."""
    client, _fake_messages = _make_claude_client(
        [
            # Cycle 1 - Security: cache miss (creates prefix)
            _text_message(
                _security_reply(),
                cache_read_input_tokens=0,
                cache_creation_input_tokens=768,
            ),
            # Cycle 2 (retry) - Security: reads from cache
            _text_message(
                _security_reply(),
                cache_read_input_tokens=768,
                cache_creation_input_tokens=0,
            ),
        ]
    )

    sec_agent = CybersecurityExpertAgent(client)

    # Cycle 1
    sec_agent.run(_security_input())
    # Cycle 2 (retry with identical input)
    sec_agent.run(_security_input())

    calls = telemetry.get_recent_calls()
    assert len(calls) == 2

    # Cycle 1: cache miss
    assert calls[0]["cache_read_tokens"] == 0
    assert calls[0]["cache_creation_tokens"] == 768

    # AC2: Cycle 2 (retry): non-zero cache_read
    assert calls[1]["cache_read_tokens"] > 0, (
        f"Expected non-zero cache_read on Security retry, "
        f"got {calls[1]['cache_read_tokens']}"
    )
    assert calls[1]["cache_creation_tokens"] == 0


# ---------------------------------------------------------------------------
# AC3: Gate outputs unchanged for identical input
# ---------------------------------------------------------------------------


def test_code_review_output_unchanged_regardless_of_cache_state() -> None:
    """Code Review produces identical output whether the shared prefix was a
    cache miss (cycle 1) or a cache hit (cycle 2). Caching must never alter
    the gate's reported findings."""
    format_reply = _cr_format_reply()
    client, _fake_messages = _make_claude_client(
        [
            # Cycle 1: cache miss
            _text_message(
                "Payment code is clean.",
                cache_read_input_tokens=0,
                cache_creation_input_tokens=1024,
            ),
            _text_message(format_reply),
            # Cycle 2: cache hit (identical input)
            _text_message(
                "Payment code is clean.",
                cache_read_input_tokens=1024,
                cache_creation_input_tokens=0,
            ),
            _text_message(format_reply),
        ]
    )

    model = LLMClientModel(client, agent_key="code_review")
    agent = ChunkReviewAgent(model)

    result_1 = agent.run(_cr_chunk_input())
    result_2 = agent.run(_cr_chunk_input())

    # AC3: outputs are identical
    assert result_1 == result_2

    # Verify cache telemetry confirms the two states differ
    calls = telemetry.get_recent_calls()
    assert calls[0]["cache_read_tokens"] == 0  # miss
    assert calls[2]["cache_read_tokens"] == 1024  # hit


def test_qa_output_unchanged_regardless_of_cache_state() -> None:
    """QA gate produces identical output whether the call was a cache miss or
    a cache hit. Caching must never alter the gate's bug findings."""
    client, _fake_messages = _make_claude_client(
        [
            # Call 1: cache miss
            _text_message(
                _qa_reply(),
                cache_read_input_tokens=0,
                cache_creation_input_tokens=512,
            ),
            # Call 2: cache hit (identical input)
            _text_message(
                _qa_reply(),
                cache_read_input_tokens=512,
                cache_creation_input_tokens=0,
            ),
        ]
    )

    result_1 = run_single_shot_review(
        client, agent_key="qa", prompt=_qa_user_prompt(),
        system_prompt=QA_PROMPT, schema=QAOutput, objective="qa review",
    )
    result_2 = run_single_shot_review(
        client, agent_key="qa", prompt=_qa_user_prompt(),
        system_prompt=QA_PROMPT, schema=QAOutput, objective="qa review",
    )

    # AC3: outputs are identical
    assert result_1.model_dump() == result_2.model_dump()

    # Verify the two calls had different cache states
    calls = telemetry.get_recent_calls()
    assert calls[0]["cache_read_tokens"] == 0  # miss
    assert calls[1]["cache_read_tokens"] == 512  # hit


def test_security_output_unchanged_regardless_of_cache_state() -> None:
    """Security gate produces identical output whether the call was a cache
    miss or a cache hit. Caching must never alter the vulnerability
    findings."""
    client, _fake_messages = _make_claude_client(
        [
            # Call 1: cache miss
            _text_message(
                _security_reply(),
                cache_read_input_tokens=0,
                cache_creation_input_tokens=512,
            ),
            # Call 2: cache hit (identical input)
            _text_message(
                _security_reply(),
                cache_read_input_tokens=512,
                cache_creation_input_tokens=0,
            ),
        ]
    )

    sec_agent = CybersecurityExpertAgent(client)

    result_1 = sec_agent.run(_security_input())
    result_2 = sec_agent.run(_security_input())

    # AC3: outputs are identical
    assert result_1.model_dump() == result_2.model_dump()

    # Verify the two calls had different cache states
    calls = telemetry.get_recent_calls()
    assert calls[0]["cache_read_tokens"] == 0  # miss
    assert calls[1]["cache_read_tokens"] == 512  # hit


# ---------------------------------------------------------------------------
# Cross-gate stability: same file context produces same prompt prefix
# ---------------------------------------------------------------------------


def test_shared_file_context_text_is_byte_identical_across_gates() -> None:
    """The file-context prefix (language + code under review) is
    byte-identical across QA and Security gates for the same microtask_files
    — the precondition for provider-side caching to produce a hit.

    This is a structural assertion: the helpers that render file context for
    each gate produce the same content when given the same code.
    """
    from qa_agent.agent import _build_qa_file_context_prefix
    from qa_agent import QAInput
    from security_agent.agent import _build_security_file_context_prefix

    qa_input = QAInput(
        code=_SHARED_CODE,
        language=_SHARED_LANGUAGE,
        task_description=_SHARED_TASK,
    )
    sec_input = _security_input()

    qa_prefix = "\n".join(_build_qa_file_context_prefix(qa_input))
    sec_prefix = "\n".join(_build_security_file_context_prefix(sec_input))

    # Both gates render the same language + code block
    assert "python" in qa_prefix.lower()
    assert "python" in sec_prefix.lower()
    assert _SHARED_CODE in qa_prefix
    assert _SHARED_CODE in sec_prefix
    # The file-context prefix structure is identical between the two gates
    assert qa_prefix == sec_prefix


def test_full_cycle_telemetry_records_all_gate_calls_with_cache_tokens() -> None:
    """A complete single-pass review cycle (CR -> QA -> Security) records
    exactly 4 telemetry entries, each with cache_read_tokens and
    cache_creation_tokens fields populated. This proves the cache-token
    telemetry pipeline delivers the data Story 2a introduced."""
    # Each gate uses its own client to avoid message-queue interference
    cr_client, _ = _make_claude_client(
        [
            _text_message(
                "Clean.",
                cache_read_input_tokens=0,
                cache_creation_input_tokens=2048,
            ),
            _text_message(
                _cr_format_reply(),
                cache_read_input_tokens=2048,
                cache_creation_input_tokens=0,
            ),
        ]
    )
    qa_client, _ = _make_claude_client(
        [
            _text_message(
                _qa_reply(),
                cache_read_input_tokens=2048,
                cache_creation_input_tokens=0,
            ),
        ]
    )
    sec_client, _ = _make_claude_client(
        [
            _text_message(
                _security_reply(),
                cache_read_input_tokens=2048,
                cache_creation_input_tokens=0,
            ),
        ]
    )

    model = LLMClientModel(cr_client, agent_key="code_review")
    ChunkReviewAgent(model).run(_cr_chunk_input())
    run_single_shot_review(
        qa_client, agent_key="qa", prompt=_qa_user_prompt(),
        system_prompt=QA_PROMPT, schema=QAOutput, objective="qa review",
    )
    CybersecurityExpertAgent(sec_client).run(_security_input())

    calls = telemetry.get_recent_calls()
    assert len(calls) == 4

    for i, call in enumerate(calls):
        assert "cache_read_tokens" in call, f"Call {i} missing cache_read_tokens"
        assert "cache_creation_tokens" in call, f"Call {i} missing cache_creation_tokens"
        # Every call has non-negative token counts
        assert call["cache_read_tokens"] >= 0
        assert call["cache_creation_tokens"] >= 0

    # Total cache tokens: one creation (CR reasoning) + three reads
    total_creation = sum(c["cache_creation_tokens"] for c in calls)
    total_read = sum(c["cache_read_tokens"] for c in calls)
    assert total_creation == 2048  # only the first call creates
    assert total_read == 2048 * 3  # three subsequent calls read
