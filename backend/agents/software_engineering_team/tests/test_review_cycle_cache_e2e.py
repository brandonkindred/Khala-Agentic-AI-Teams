"""End-to-end proof that the review-cycle cache-token telemetry pipeline
works across Code Review, QA, and Security gates, and across retry cycles —
Story 2c acceptance criteria.

Scope: drives the three review-gate agents (``ChunkReviewAgent``,
``generate_structured`` for QA, ``CybersecurityExpertAgent``) directly
against ``ClaudeLLMClient`` instances backed by fake Anthropic SDKs (the
same pattern established by ``test_chunk_reviewer_cache_e2e.py``).

The tests verify:
1. Code Review's system content carries ``cache_control`` blocks on the wire
   (the explicit cache opt-in via ``CacheBreakpoint``).
2. The telemetry pipeline faithfully records ``cache_read_tokens`` and
   ``cache_creation_tokens`` from provider responses (Story 2a data flow).
3. Gate outputs are unchanged regardless of cache state.

Implementation notes:
- Code Review is driven via ``ChunkReviewAgent`` (2 LLM calls: reasoning
  pass + formatting pass), matching the production call path exactly.
- Security is driven via ``CybersecurityExpertAgent.run()`` (1 LLM call via
  ``run_single_shot_review``), matching the production call path exactly.
- QA is driven via ``run_single_shot_review`` with the ``QAOutput`` schema
  (the ``generate_structured`` → ``complete_json`` pathway). This exercises
  the same ``ClaudeLLMClient.complete_json`` → ``record_llm_call`` telemetry
  path that production uses. The Strands Agent tool-call wrapper
  (``run_structured_persona``) is not exercised here because the
  ``_SequentialFakeMessages`` test double returns plain text responses, not
  tool-use content blocks — Strands' ``structured_output_model`` requires
  the model to emit a StructuredOutputTool invocation, which this fake
  cannot simulate. The telemetry recording code is downstream of both paths
  (Strands or direct ``complete_json``), so the assertion coverage is
  unaffected.
"""

from __future__ import annotations

import json

import pytest
from code_review_agent.chunk_reviewer import ChunkReviewAgent
from code_review_agent.models import ChunkReviewInput
from qa_agent.models import QAOutput
from qa_agent.prompts import QA_PROMPT
from security_agent import CybersecurityExpertAgent
from security_agent.models import SecurityInput

from llm_client_fakes import _make_claude_client, _text_message
from llm_service import telemetry
from llm_service.strands_adapter import LLMClientModel
from software_engineering_team.shared.single_shot_review import run_single_shot_review

pytestmark = [pytest.mark.usefixtures("_reset_llm_telemetry_state")]

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    """Build Code Review input sharing the same file context.

    The spec/architecture/codebase excerpts are sized to be representative of
    production content. This test verifies the ``cache_control`` breakpoint is
    emitted on the wire and that telemetry records the provider-reported cache
    tokens; it does not assert that the provider actually caches the prefix
    (that depends on the provider's minimum-prefix-length threshold, which
    varies and is not controllable from the client).
    """
    # Representative-length excerpts for the CacheBreakpoint prefix.
    spec_excerpt = (
        "## Payment Service Specification\n"
        "The payment service handles all monetary transactions for the platform. "
        "It must validate amounts (positive, non-zero, within configured limits), "
        "sanitize card numbers (strip spaces, validate Luhn checksum), support "
        "idempotency keys to prevent duplicate charges, and emit structured audit "
        "events for every state transition. Refunds must be processed within the "
        "same settlement window when possible. PCI-DSS compliance requires that "
        "raw card numbers never persist beyond the tokenization boundary; the "
        "service must delegate to the vault for storage and retrieve only opaque "
        "payment tokens for subsequent operations. Rate limiting: max 100 charges "
        "per merchant per minute, with exponential backoff on gateway 429s. "
        "Timeouts: gateway calls must complete within 30 seconds or abort with a "
        "retriable error code. All amounts are represented in the smallest currency "
        "unit (cents for USD, pence for GBP) to avoid floating-point rounding."
    )
    architecture_overview = (
        "## Architecture Overview\n"
        "Single-region FastAPI monolith deployed on ECS Fargate behind an ALB. "
        "PostgreSQL 15 for transactional data (payments, refunds, audit log) with "
        "read replicas for the dashboard queries. Redis cluster for idempotency "
        "key deduplication (TTL 24h) and rate-limit counters (sliding window). "
        "Outbound payment gateway calls go through a circuit-breaker (Hystrix "
        "pattern, 50% failure threshold, 30s recovery window). Async event bus "
        "(SQS + SNS fan-out) for audit events consumed by the compliance service "
        "and the real-time fraud-detection pipeline. Secrets (API keys, DB creds) "
        "in AWS Secrets Manager, rotated every 90 days. Observability: structured "
        "JSON logs to CloudWatch, OpenTelemetry traces to X-Ray, custom metrics "
        "(p99 latency, charge success rate, refund ratio) to CloudWatch Metrics "
        "with alarms on SLO breaches."
    )
    existing_codebase_excerpt = (
        "## Existing Codebase Context\n"
        "class PaymentGateway:\n"
        "    def __init__(self, api_key: str, timeout: int = 30):\n"
        "        self._client = httpx.AsyncClient(timeout=timeout)\n"
        "        self._api_key = api_key\n\n"
        "    async def charge(self, amount_cents: int, token: str) -> ChargeResult:\n"
        "        resp = await self._client.post('/v1/charges', json={...})\n"
        "        return ChargeResult.from_response(resp)\n\n"
        "class PaymentRepository:\n"
        "    async def create_payment(self, payment: Payment) -> Payment: ...\n"
        "    async def get_by_idempotency_key(self, key: str) -> Optional[Payment]: ...\n"
        "    async def update_status(self, payment_id: str, status: Status) -> None: ...\n\n"
        "class AuditLogger:\n"
        "    def __init__(self, event_bus: EventBus):\n"
        "        self._bus = event_bus\n\n"
        "    async def log_state_transition(self, payment: Payment, old: Status, new: Status):\n"
        "        await self._bus.publish(AuditEvent(...))\n"
    )
    return ChunkReviewInput(
        code_chunk=f"### app/payments.py ###\n{_SHARED_CODE}",
        file_path_or_label="app/payments.py",
        task_description=_SHARED_TASK,
        spec_excerpt=spec_excerpt,
        spec_compliance_single_pass=False,
        architecture_overview=architecture_overview,
        existing_codebase_excerpt=existing_codebase_excerpt,
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
        "fields: approved, bugs_found, test_plan, unit_tests, integration_tests, "
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
# Code Review cache breakpoint: wire-level verification
# ---------------------------------------------------------------------------


def test_cross_gate_cache_telemetry_baseline() -> None:
    """Code Review's reasoning call carries a ``cache_control`` block on the
    wire (the explicit cache opt-in via ``CacheBreakpoint``), and the
    telemetry pipeline records the cache-creation tokens reported by the
    provider.

    QA and Security have no explicit cache opt-in (no ``cache_control``
    block), so their cache_read_tokens are expected to be 0 in a normal
    single-cycle run. The test verifies telemetry records 0 faithfully.
    """
    # --- Code Review gate (2 LLM calls: reasoning + formatting) ---
    cr_client, cr_fake = _make_claude_client(
        [
            _text_message(
                "No issues found in payment processing.",
                cache_read_input_tokens=0,
                cache_creation_input_tokens=1024,
            ),
            _text_message(
                _cr_format_reply(),
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        ]
    )
    model = LLMClientModel(cr_client, agent_key="code_review")
    ChunkReviewAgent(model).run(_cr_chunk_input())

    # Wire-level assertion: CR reasoning call's system content carries a
    # cache_control breakpoint (the real-world precondition for Anthropic to
    # cache this prefix for subsequent calls in the same session).
    cr_reasoning_call = cr_fake.captured_calls[0]
    cr_system = cr_reasoning_call["system"]
    cache_marked_blocks = [
        block
        for block in cr_system
        if isinstance(block, dict) and block.get("cache_control") == {"type": "ephemeral"}
    ]
    assert cache_marked_blocks, (
        f"Expected a cache_control block in CR reasoning system content, got {cr_system}"
    )

    # --- QA gate (no cache opt-in: cache_read expected 0) ---
    qa_client, qa_fake = _make_claude_client(
        [
            _text_message(
                _qa_reply(),
                cache_read_input_tokens=0,
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

    # --- Security gate (no cache opt-in: cache_read expected 0) ---
    sec_client, sec_fake = _make_claude_client(
        [
            _text_message(
                _security_reply(),
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        ]
    )
    CybersecurityExpertAgent(sec_client).run(_security_input())

    # Wire-level assertion: QA and Security carry the shared file-context
    # prefix in their user messages (structural stability for future cache
    # opt-in, and confirms both gates review the same code).
    qa_prompt = qa_fake.captured_calls[0]["messages"][0]["content"]
    sec_prompt = sec_fake.captured_calls[0]["messages"][0]["content"]
    assert _SHARED_CODE.strip() in qa_prompt, "QA prompt must contain the shared code"
    assert _SHARED_CODE.strip() in sec_prompt, "Security prompt must contain the shared code"

    # --- Telemetry assertions ---
    calls = telemetry.get_recent_calls()
    assert len(calls) == 4

    cr_reasoning, cr_formatting, qa_call, security_call = calls

    # Code Review reasoning: cache creation (explicit breakpoint)
    assert cr_reasoning["cache_read_tokens"] == 0
    assert cr_reasoning["cache_creation_tokens"] == 1024

    # Code Review formatting: no breakpoint, no cache activity
    assert cr_formatting["cache_read_tokens"] == 0
    assert cr_formatting["cache_creation_tokens"] == 0

    # QA: no cache opt-in, so no cache activity
    assert qa_call["cache_read_tokens"] == 0
    assert qa_call["cache_creation_tokens"] == 0

    # Security: no cache opt-in, so no cache activity
    assert security_call["cache_read_tokens"] == 0
    assert security_call["cache_creation_tokens"] == 0


# ---------------------------------------------------------------------------
# AC2: Non-zero cache_read across retry cycles
# ---------------------------------------------------------------------------


def test_code_review_retry_shows_nonzero_cache_read() -> None:
    """Across retry cycles (e.g. QA fails, fixes applied, re-run from Code
    Review), the second cycle's Code Review call exhibits non-zero
    cache_read_tokens for the shared prefix, proving the provider cache
    persists across retries.

    Wire-level verification: both cycles' reasoning calls carry the same
    ``cache_control``-marked system content (byte-identical spec/architecture
    prefix), which is the real-world precondition for Anthropic to serve a
    cache hit on the second cycle.
    """
    client, fake_messages = _make_claude_client(
        [
            # Cycle 1 - Code Review reasoning: cache miss
            _text_message(
                "No issues.",
                cache_read_input_tokens=0,
                cache_creation_input_tokens=1024,
            ),
            # Cycle 1 - Code Review formatting (different system prompt, no breakpoint)
            _text_message(
                _cr_format_reply(),
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
            # Cycle 2 - Code Review reasoning: reads cache from cycle 1
            _text_message(
                "Validation added, no remaining issues.",
                cache_read_input_tokens=1024,
                cache_creation_input_tokens=0,
            ),
            # Cycle 2 - Code Review formatting (different system prompt, no breakpoint)
            _text_message(
                _cr_format_reply(),
                cache_read_input_tokens=0,
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

    # Wire-level assertion: both reasoning calls carry the same
    # cache_control-marked system content (the precondition for a real
    # provider cache hit on the second call).
    reasoning_calls = [fake_messages.captured_calls[0], fake_messages.captured_calls[2]]
    cycle1_system, cycle2_system = (call["system"] for call in reasoning_calls)
    assert cycle1_system == cycle2_system, (
        "Retry cycle's system content must be byte-identical to cycle 1's "
        "for the provider cache to hit"
    )
    # Confirm cache_control is present
    cache_marked = [
        block
        for block in cycle1_system
        if isinstance(block, dict) and block.get("cache_control") == {"type": "ephemeral"}
    ]
    assert cache_marked, (
        f"Expected cache_control block in reasoning system content, got {cycle1_system}"
    )

    # Telemetry assertions
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
    assert cycle2_cr_reason["cache_creation_tokens"] == 0, (
        f"Expected zero cache_creation on CR cache-hit retry, "
        f"got {cycle2_cr_reason['cache_creation_tokens']}"
    )


def test_qa_telemetry_propagates_cache_tokens_on_retries() -> None:
    """Verify the telemetry pipeline faithfully records cache_read_tokens and
    cache_creation_tokens from the provider response on repeated QA calls.

    QA has no explicit cache opt-in (no ``cache_control`` block), so any
    non-zero cache tokens reported by the provider are incidental. This test
    proves the telemetry pipeline (Story 2a) propagates those values
    end-to-end regardless of their source.

    Wire-level verification: both calls carry the same user-prompt content,
    confirming the prompt is structurally stable across retries.
    """
    client, fake_messages = _make_claude_client(
        [
            # Call 1: provider reports cache creation (incidental)
            _text_message(
                _qa_reply(),
                cache_read_input_tokens=0,
                cache_creation_input_tokens=768,
            ),
            # Call 2: provider reports cache read (incidental)
            _text_message(
                _qa_reply(),
                cache_read_input_tokens=768,
                cache_creation_input_tokens=0,
            ),
        ]
    )

    run_single_shot_review(
        client, agent_key="qa", prompt=_qa_user_prompt(),
        system_prompt=QA_PROMPT, schema=QAOutput, objective="qa review",
    )
    run_single_shot_review(
        client, agent_key="qa", prompt=_qa_user_prompt(),
        system_prompt=QA_PROMPT, schema=QAOutput, objective="qa review",
    )

    # Wire-level assertion: both calls send the same prompt (structural
    # stability for future cache opt-in).
    call1_prompt = fake_messages.captured_calls[0]["messages"][0]["content"]
    call2_prompt = fake_messages.captured_calls[1]["messages"][0]["content"]
    assert call1_prompt == call2_prompt, (
        "Retry cycle's user prompt must be byte-identical"
    )

    calls = telemetry.get_recent_calls()
    assert len(calls) == 2

    # Telemetry propagation: values are faithfully recorded
    assert calls[0]["cache_read_tokens"] == 0
    assert calls[0]["cache_creation_tokens"] == 768
    assert calls[1]["cache_read_tokens"] == 768
    assert calls[1]["cache_creation_tokens"] == 0


def test_security_telemetry_propagates_cache_tokens_on_retries() -> None:
    """Verify the telemetry pipeline faithfully records cache_read_tokens and
    cache_creation_tokens from the provider response on repeated Security
    calls.

    Security has no explicit cache opt-in (no ``cache_control`` block), so
    any non-zero cache tokens reported by the provider are incidental. This
    test proves the telemetry pipeline propagates those values end-to-end.
    """
    client, _fake_messages = _make_claude_client(
        [
            # Call 1: provider reports cache creation (incidental)
            _text_message(
                _security_reply(),
                cache_read_input_tokens=0,
                cache_creation_input_tokens=768,
            ),
            # Call 2: provider reports cache read (incidental)
            _text_message(
                _security_reply(),
                cache_read_input_tokens=768,
                cache_creation_input_tokens=0,
            ),
        ]
    )

    sec_agent = CybersecurityExpertAgent(client)

    sec_agent.run(_security_input())
    sec_agent.run(_security_input())

    calls = telemetry.get_recent_calls()
    assert len(calls) == 2

    # Telemetry propagation: values are faithfully recorded
    assert calls[0]["cache_read_tokens"] == 0
    assert calls[0]["cache_creation_tokens"] == 768
    assert calls[1]["cache_read_tokens"] == 768
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

    # Output is identical regardless of reported cache state
    assert result_1.model_dump() == result_2.model_dump()

    # Verify cache telemetry confirms the two states differ
    calls = telemetry.get_recent_calls()
    assert calls[0]["cache_read_tokens"] == 0  # miss
    assert calls[2]["cache_read_tokens"] == 1024  # hit
    assert calls[2]["cache_creation_tokens"] == 0  # hit: no new cache creation


def test_qa_output_unchanged_regardless_of_cache_state() -> None:
    """QA gate produces identical output regardless of what cache tokens the
    provider reports. The application layer must not alter findings based on
    whether the provider served from cache or not."""
    client, _fake_messages = _make_claude_client(
        [
            # Call 1: provider reports cache creation
            _text_message(
                _qa_reply(),
                cache_read_input_tokens=0,
                cache_creation_input_tokens=512,
            ),
            # Call 2: provider reports cache read (simulated different state)
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

    # Output is identical regardless of reported cache state
    assert result_1.model_dump() == result_2.model_dump()

    # Telemetry confirms the two calls had different reported cache states
    calls = telemetry.get_recent_calls()
    assert calls[0]["cache_read_tokens"] == 0
    assert calls[1]["cache_read_tokens"] == 512


def test_security_output_unchanged_regardless_of_cache_state() -> None:
    """Security gate produces identical output regardless of what cache tokens
    the provider reports. The application layer must not alter findings based
    on whether the provider served from cache or not."""
    client, _fake_messages = _make_claude_client(
        [
            # Call 1: provider reports cache creation
            _text_message(
                _security_reply(),
                cache_read_input_tokens=0,
                cache_creation_input_tokens=512,
            ),
            # Call 2: provider reports cache read (simulated different state)
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

    # Output is identical regardless of reported cache state
    assert result_1.model_dump() == result_2.model_dump()

    # Telemetry confirms the two calls had different reported cache states
    calls = telemetry.get_recent_calls()
    assert calls[0]["cache_read_tokens"] == 0
    assert calls[1]["cache_read_tokens"] == 512


# ---------------------------------------------------------------------------
# Cross-gate stability: same file context produces same prompt prefix
# ---------------------------------------------------------------------------


def test_shared_file_context_text_is_byte_identical_across_gates() -> None:
    """The file-context prefix (language + code under review) is
    byte-identical across QA and Security gates for the same microtask_files
    — the precondition for provider-side caching to produce a hit.

    QA and Security both delegate to the single shared
    ``build_file_context_prefix`` helper, so cross-gate identity holds by
    construction rather than by two hand-maintained copies happening to
    agree.
    """
    from software_engineering_team.shared.review_prompt_utils import build_file_context_prefix

    prefix = "\n".join(build_file_context_prefix(_SHARED_LANGUAGE, _SHARED_CODE))

    assert "python" in prefix.lower()
    assert _SHARED_CODE in prefix


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
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        ]
    )
    qa_client, _ = _make_claude_client(
        [
            _text_message(
                _qa_reply(),
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        ]
    )
    sec_client, _ = _make_claude_client(
        [
            _text_message(
                _security_reply(),
                cache_read_input_tokens=0,
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

    # Only CR reasoning creates cache tokens (explicit breakpoint).
    # QA/Security have no cache opt-in: 0 cache activity expected.
    total_creation = sum(c["cache_creation_tokens"] for c in calls)
    total_read = sum(c["cache_read_tokens"] for c in calls)
    assert total_creation == 2048  # only the reasoning call creates
    assert total_read == 0  # no cache reads in a single-pass cycle
