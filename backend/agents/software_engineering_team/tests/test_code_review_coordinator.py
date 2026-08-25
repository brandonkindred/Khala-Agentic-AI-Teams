"""Tests for Code Review Coordinator.

Pure-function tests (the splitter and chunker) stay LLM-free. The
LLM-integration tests use ``DummyLLMClient`` subclasses because
``ChunkReviewAgent`` uses a two-call via-reasoning path (``complete`` then
``complete_json``) validated against ``ChunkReviewLLMResponse``.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from code_review_agent import mapping
from code_review_agent.chunk_reviewer import CHUNK_REVIEW_NOTE
from code_review_agent.chunking import _bisect_segment
from code_review_agent.coordinator import (
    MAX_CODE_REVIEW_ISSUES,
    MIN_SPLIT_SEGMENT_CHARS,
    _cap_issues,
    _compact_for_review,
    _is_content_failure,
    _issues_from_chunk_output,
    _map_parallelism,
    _reconcile_approval,
    _render_architecture_context,
    _run_tail_passes,
    _segment_range_label,
    _TailPassResult,
    _validate_line,
    build_review_chunks,
    cap_chunk_content,
    cap_review_chunk,
    run_coordinator,
    split_block_into_segments,
)
from code_review_agent.mapping import _bisect_halves_run_sequentially, _run_reviewer_call
from code_review_agent.models import (
    ChunkReviewOutput,
    CodeReviewInput,
    CodeReviewIssue,
    CodeReviewOutput,
    CodeReviewUnavailableError,
    FileSegment,
    ReviewChunk,
    _normalized_severity,
    is_no_op_suggestion,
)
from pydantic import ValidationError
from strands.models.model import Model
from tests.chunk_review_prompt_routing import (
    is_chunk_map_reasoning_prompt as _is_chunk_map_reasoning_prompt,
)
from tests.chunk_review_prompt_routing import (
    is_formatting_pass_prompt as _is_formatting_pass_prompt,
)

from llm_service import (
    LLMClient,
    LLMJsonParseError,
    LLMRateLimitError,
    LLMSchemaValidationError,
    LLMSemanticExhaustionError,
    LLMTruncatedError,
)
from llm_service.clients.dummy import DummyLLMClient
from software_engineering_team.shared.context_sizing import compute_code_review_map_chunk_chars

# Grace period for a buggy late progress notification to (wrongly) land before the
# test asserts none arrived after a map failure. Small by design; the preceding
# ``wait(timeout=10)`` already guarantees the worker finished, so this only guards
# against a notification queued just after that.
_LATE_NOTIFY_GRACE_PERIOD_S = 0.1

# Headroom under the map-chunk char budget so near-cap files cannot pack into
# one chunk (forces separate map units in multi-file recovery/parallelism tests).
_MAP_CHUNK_SEPARATION_HEADROOM = 2_000


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------


def _issue(severity: str, description: str, *, line: int = 1) -> CodeReviewIssue:
    """Build a minimal ``CodeReviewIssue`` for cap/reconcile unit tests."""
    return CodeReviewIssue(
        severity=severity,
        category="general",
        file_path="a.py",
        line=line,
        description=description,
        suggestion="fix it",
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ""),
        ("", ""),
        ("high", "high"),
        ("High", "high"),
        ("HIGH", "high"),
        (" critical ", "critical"),
        ("Medium", "medium"),
    ],
)
def test_normalized_severity_folds_case_and_whitespace(raw: str | None, expected: str) -> None:
    assert _normalized_severity(raw) == expected


def test_cap_issues_under_limit_preserves_order() -> None:
    issues = [_issue("low", f"n{i}", line=i) for i in range(1, 6)]
    capped = _cap_issues(issues)
    assert capped == issues
    assert capped is not issues  # shallow copy


def test_cap_issues_exactly_at_limit_preserves_order() -> None:
    issues = [_issue("medium", f"m{i}", line=i) for i in range(1, MAX_CODE_REVIEW_ISSUES + 1)]
    capped = _cap_issues(issues)
    assert len(capped) == MAX_CODE_REVIEW_ISSUES
    assert [i.description for i in capped] == [i.description for i in issues]


def test_cap_issues_over_limit_severity_first_stable_within_rank() -> None:
    # 5 critical, 5 high, then enough medium/low/info that total exceeds the cap.
    # Input order is deliberately low-first so a first-seen trim would keep nits.
    lows = [_issue("low", f"low-{i}", line=100 + i) for i in range(20)]
    mediums = [_issue("medium", f"med-{i}", line=200 + i) for i in range(20)]
    highs = [_issue("high", f"high-{i}", line=10 + i) for i in range(5)]
    criticals = [_issue("critical", f"crit-{i}", line=i) for i in range(5)]
    issues = [*lows, *mediums, *highs, *criticals]
    assert len(issues) > MAX_CODE_REVIEW_ISSUES

    capped = _cap_issues(issues)
    assert len(capped) == MAX_CODE_REVIEW_ISSUES
    severities = [i.severity for i in capped]
    assert severities[:5] == ["critical"] * 5
    assert severities[5:10] == ["high"] * 5
    assert all(s == "medium" for s in severities[10:])
    # Within severity, original relative order is preserved.
    assert [i.description for i in capped[:5]] == [f"crit-{i}" for i in range(5)]
    assert [i.description for i in capped[5:10]] == [f"high-{i}" for i in range(5)]
    assert [i.description for i in capped[10:]] == [f"med-{i}" for i in range(20)]


def test_cap_then_reconcile_medium_only_still_approves() -> None:
    issues = [_issue("medium", f"m{i}", line=i) for i in range(1, MAX_CODE_REVIEW_ISSUES + 6)]
    capped = _cap_issues(issues)
    assert len(capped) == MAX_CODE_REVIEW_ISSUES
    approved, out = _reconcile_approval(False, capped)
    assert approved is True
    assert len(out) == MAX_CODE_REVIEW_ISSUES


def test_cap_then_reconcile_keeps_critical_and_rejects() -> None:
    # Critical arrives last in the uncapped list (would be dropped by first-seen
    # trim) but severity-first ranking keeps it in the capped set.
    mediums = [_issue("medium", f"m{i}", line=i) for i in range(1, MAX_CODE_REVIEW_ISSUES + 1)]
    critical = _issue("critical", "must-keep", line=999)
    capped = _cap_issues([*mediums, critical])
    assert any(i.description == "must-keep" for i in capped)
    assert capped[0].severity == "critical"
    approved, out = _reconcile_approval(True, capped)
    assert approved is False
    assert any(i.severity == "critical" for i in out)


@pytest.mark.parametrize(
    "severity",
    ["High", "HIGH", " high ", "Critical", "CRITICAL", " critical "],
)
def test_reconcile_approval_treats_mixed_case_critical_high_as_blocking(
    severity: str,
) -> None:
    """Blocking membership must match ``_cap_issues`` fold, not raw equality."""
    approved, out = _reconcile_approval(True, [_issue(severity, "blocker")])
    assert approved is False
    assert len(out) == 1
    assert _normalized_severity(out[0].severity) in {"critical", "high"}


@pytest.mark.parametrize("severity", ["Medium", "LOW", "Info"])
def test_reconcile_approval_mixed_case_non_blocking_still_auto_approves(
    severity: str,
) -> None:
    """Non-blocking severities remain non-blocking after case fold."""
    approved, out = _reconcile_approval(False, [_issue(severity, "nit")])
    assert approved is True
    assert len(out) == 1


def test_reconcile_approval_override_log_names_non_critical_high(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The auto-approve override log must describe the overridden severities
    accurately: medium/low/info are 'non-critical/high', not 'minor/nit'.
    """
    with caplog.at_level("INFO"):
        approved, out = _reconcile_approval(False, [_issue("medium", "m"), _issue("low", "l")])
    assert approved is True
    assert len(out) == 2
    assert "2 non-critical/high issues, no critical/high" in caplog.text
    assert "minor/nit" not in caplog.text


# ---------------------------------------------------------------------------
# CodeReviewInput boundary validation
# ---------------------------------------------------------------------------


def test_input_without_files_raises() -> None:
    with pytest.raises(ValidationError):
        CodeReviewInput(task_description="t")


def test_input_with_empty_files_dict_raises() -> None:
    """files={} (e.g. a glob miss) is a caller bug, not an empty review."""
    with pytest.raises(ValidationError):
        CodeReviewInput(files={}, task_description="t")


# ---------------------------------------------------------------------------
# run_coordinator — LLM-integration tests
# ---------------------------------------------------------------------------


class _ScriptedClient(DummyLLMClient):
    """Returns a different canned response on each chunk's formatting-pass
    ``complete_json`` call.

    Used to simulate the coordinator dispatching to multiple chunks and
    each chunk getting its own LLM response. Thread-safe: map calls may run
    in parallel. Both the reasoning pass (reached via the Strands Agent's
    ``chat()`` delegation) and the formatting pass (a direct ``complete_json``
    call from ``run_agent_via_reasoning``) land on ``complete_json`` now, so
    only the formatting-pass call (identified by
    ``_is_formatting_pass_prompt``) advances the scripted response cursor
    -- the reasoning-pass call instead gets the inherited dummy default,
    whose prose is discarded once wrapped for formatting.
    """

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        super().__init__()
        self._responses = list(responses)
        self._idx = 0
        self._lock = threading.Lock()

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: str | None = None,
        tools: list | None = None,
        think: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not _is_formatting_pass_prompt(prompt):
            return super().complete_json(
                prompt,
                temperature=temperature,
                system_prompt=system_prompt,
                tools=tools,
                think=think,
                **kwargs,
            )
        with self._lock:
            if self._idx < len(self._responses):
                resp = self._responses[self._idx]
                self._idx += 1
                return resp
            # After the scripted responses are exhausted, fall back to the last
            # one so additional chunks don't crash the test.
            return self._responses[-1] if self._responses else {}


def test_run_coordinator_with_multi_file_code_merges_chunk_summaries() -> None:
    """Multiple file blocks → multiple chunks → merged CodeReviewOutput."""
    files = {
        "app/main.py": "x" * 20_000,
        "app/models.py": "y" * 20_000,
    }

    client = _ScriptedClient(
        [
            {"approved": True, "issues": [], "summary": "Chunk 1 OK", "spec_compliance_notes": ""},
            {"approved": True, "issues": [], "summary": "Chunk 2 OK", "spec_compliance_notes": ""},
            # Synthesis pass: empty summary → None → fall back to concatenation.
            {"summary": "", "spec_compliance_notes": "ignored"},
        ]
    )

    result = run_coordinator(
        client,
        CodeReviewInput(
            files=files,
            task_description="Add feature",
            language="python",
        ),
    )

    assert isinstance(result, CodeReviewOutput)
    assert result.approved is True
    assert result.issues == []
    # Coordinator concatenates chunk summaries with blank lines between.
    assert "Chunk 1" in result.summary
    assert "Chunk 2" in result.summary


class _CompactionCountingClient(DummyLLMClient):
    """Counts LLM compaction calls (``complete`` with the compactor prompt).

    Chunk review still flows through the inherited dummy behavior; only the
    compaction path (``compact_text`` → ``_compact_single`` → ``complete``) is
    tallied, identified by the fixed compactor-prompt marker.
    """

    _COMPACTOR_MARKER = "precise technical content compactor"

    def __init__(self) -> None:
        super().__init__()
        self.compaction_calls = 0
        self._lock = threading.Lock()

    def complete(self, prompt: str, **kwargs: Any) -> str:  # type: ignore[override]
        if self._COMPACTOR_MARKER in prompt:
            with self._lock:
                self.compaction_calls += 1
            return "COMPACTED"
        return super().complete(prompt, **kwargs)


def test_shared_context_compaction_is_memoized_across_runs() -> None:
    """The oversized spec/architecture/existing-codebase are compacted once and
    reused on the next coordinator run (the review→fix→re-review loop passes the
    same shared context each cycle)."""
    from shared.dev_models.models import SystemArchitecture

    over_budget = "specification detail line. " * 4000  # well over any budget
    arch = SystemArchitecture(
        overview="architecture overview line. " * 4000,
        architecture_document="# Arch",
        components=[],
        decisions=[],
        diagrams={},
    )

    def _make_input() -> CodeReviewInput:
        return CodeReviewInput(
            files={"app/main.py": "x" * 500},
            task_description="Add feature",
            language="python",
            spec_content=over_budget,
            architecture=arch,
            existing_codebase="prior codebase line. " * 4000,
        )

    client = _CompactionCountingClient()

    run_coordinator(client, _make_input())
    first_run_calls = client.compaction_calls
    assert first_run_calls > 0  # compaction actually fired on the cold run

    run_coordinator(client, _make_input())
    # Second run reuses the memoized compactions — no additional compaction calls.
    assert client.compaction_calls == first_run_calls


def test_shared_context_compaction_is_recorded_in_transcript(monkeypatch) -> None:
    """Oversized spec/architecture/existing-codebase compaction is an LLM call
    and must appear in the durable transcript, not only the later chunk review."""
    from llm_service import llm_attribution
    from shared.dev_models.models import SystemArchitecture

    over_budget = "specification detail line. " * 4000
    arch = SystemArchitecture(
        overview="architecture overview line. " * 4000,
        architecture_document="# Arch",
        components=[],
        decisions=[],
        diagrams={},
    )
    captured: list = []
    monkeypatch.setattr(
        "code_review_agent.coordinator.record_transcript_entry",
        lambda *args, **kwargs: captured.append(args),
    )
    client = _CompactionCountingClient()
    with llm_attribution(job_id="job-1"):
        run_coordinator(
            client,
            CodeReviewInput(
                files={"app/main.py": "x" * 500},
                task_description="Add feature",
                language="python",
                spec_content=over_budget,
                architecture=arch,
                existing_codebase="prior codebase line. " * 4000,
            ),
        )
    compaction = [args for args in captured if args and args[0] == "compaction"]
    assert len(compaction) == client.compaction_calls
    assert {args[1] for args in compaction} == {
        "specification",
        "architecture overview",
        "existing codebase",
    }


def test_render_architecture_context_folds_in_components_and_decisions() -> None:
    """The architecture excerpt built for the reviewer includes not just the
    overview prose but component responsibilities and architecture decisions
    (ADRs) -- the concrete signal an architecture-consistency check needs."""
    from shared.dev_models.models import ArchitectureComponent, SystemArchitecture

    arch = SystemArchitecture(
        overview="Layered service architecture.",
        components=[
            ArchitectureComponent(name="UserService", type="backend", description="Owns user CRUD")
        ],
        decisions=[{"title": "ADR-001", "decision": "Use Postgres for persistence"}],
    )
    rendered = _render_architecture_context(arch)
    assert "Layered service architecture." in rendered
    assert "UserService (backend): Owns user CRUD" in rendered
    assert "ADR-001: Use Postgres for persistence" in rendered


def test_render_architecture_context_handles_missing_and_malformed_fields() -> None:
    """A bare overview renders with no Components/Decisions sections; a malformed
    (non-dict) decision entry is skipped rather than raising.

    ``decisions`` entries that reach a real ``SystemArchitecture`` are always
    dicts (Pydantic validates ``List[Dict[str, Any]]`` at construction), so the
    non-dict case is exercised via a duck-typed stand-in -- the function only
    ever accesses ``.overview``/``.components``/``.decisions`` by attribute, so
    it works on anything shaped like a ``SystemArchitecture``.
    """
    from types import SimpleNamespace

    from shared.dev_models.models import SystemArchitecture

    bare = SystemArchitecture(overview="Just an overview.")
    rendered_bare = _render_architecture_context(bare)
    assert rendered_bare == "Just an overview."

    malformed = SimpleNamespace(overview="ov", components=[], decisions=["not-a-dict"])
    rendered_malformed = _render_architecture_context(malformed)
    assert rendered_malformed == "ov"


def test_chunk_prompt_includes_component_and_decision_text() -> None:
    """End-to-end: a submission reviewed with a component/decision-bearing
    architecture renders that content into the chunk reviewer's prompt."""
    from shared.dev_models.models import ArchitectureComponent, SystemArchitecture

    class _PromptCapturingClient(DummyLLMClient):
        """Records prompts; lock-guarded for parallel map/tail callers. Both
        the reasoning pass (reached via the Strands Agent's ``chat()``
        delegation) and the formatting pass now land on ``complete_json``, so
        recording happens there instead of a separate ``complete`` override.
        """

        def __init__(self) -> None:
            super().__init__()
            self.prompts: list[str] = []
            self._lock = threading.Lock()

        def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
            with self._lock:
                self.prompts.append(prompt)
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    arch = SystemArchitecture(
        overview="Layered service architecture.",
        components=[
            ArchitectureComponent(
                name="PaymentService", type="backend", description="Owns payment processing"
            )
        ],
        decisions=[{"title": "ADR-002", "decision": "All writes go through the repository layer"}],
    )
    client = _PromptCapturingClient()
    run_coordinator(
        client,
        CodeReviewInput(files={"app/main.py": "def f():\n    return 1\n"}, architecture=arch),
    )
    assert client.prompts, "expected at least one chunk-review call"
    assert any("PaymentService (backend): Owns payment processing" in p for p in client.prompts)
    assert any("ADR-002: All writes go through the repository layer" in p for p in client.prompts)


def test_run_coordinator_merges_issues_and_rejects_if_critical() -> None:
    """Coordinator merges issues across chunks; a single critical issue
    propagates to ``approved=False``."""
    files = {"app/main.py": "x" * 20_000}

    client = _ScriptedClient(
        [
            {
                "approved": False,
                "issues": [
                    {
                        "severity": "critical",
                        "category": "logic",
                        "file_path": "app/main.py",
                        "description": "SQL injection risk",
                        "suggestion": "Use parameterized queries",
                    }
                ],
                "summary": "Critical issue found.",
                "spec_compliance_notes": "",
            }
        ]
    )

    result = run_coordinator(
        client,
        CodeReviewInput(
            files=files,
            task_description="Add feature",
            language="python",
        ),
    )

    assert result.approved is False
    assert len(result.issues) == 1
    assert result.issues[0].severity == "critical"
    assert result.issues[0].file_path == "app/main.py"


def test_run_coordinator_keeps_same_description_on_different_lines() -> None:
    """Two findings sharing file_path + description but on different lines are
    distinct (line anchors inline comments), so dedup must keep both.

    Severity is "high" (not "medium"): ``ChunkReviewLLMResponse``'s consistency
    validator requires an ``approved=False`` verdict to carry at least one
    actionable critical/high issue -- unrelated to this test's actual subject
    (line-anchored dedup), so the fixture must satisfy it.
    """
    files = {"app/main.py": "\n".join(f"x{i} = {i}" for i in range(100))}

    client = _ScriptedClient(
        [
            {
                "approved": False,
                "issues": [
                    {
                        "severity": "high",
                        "category": "logic",
                        "file_path": "app/main.py",
                        "line": 10,
                        "description": "duplicate string literal",
                        "suggestion": "extract a constant",
                    },
                    {
                        "severity": "high",
                        "category": "logic",
                        "file_path": "app/main.py",
                        "line": 80,
                        "description": "duplicate string literal",
                        "suggestion": "extract a constant",
                    },
                ],
                "summary": "Two occurrences.",
                "spec_compliance_notes": "",
            }
        ]
    )

    result = run_coordinator(
        client,
        CodeReviewInput(files=files, task_description="Add feature", language="python"),
    )

    assert sorted(i.line for i in result.issues) == [10, 80]


def test_run_coordinator_drops_unanchored_twin_of_anchored_finding() -> None:
    """An unanchored (line=None) finding that duplicates an anchored one (same
    file_path + description) is dropped, so the issue is reported once (inline),
    not twice (once in the body, once inline)."""
    files = {"app/main.py": "\n".join(f"x{i} = {i}" for i in range(50))}

    client = _ScriptedClient(
        [
            {
                "approved": False,
                "issues": [
                    {
                        "severity": "high",
                        "category": "logic",
                        "file_path": "app/main.py",
                        "description": "missing null check",
                        "suggestion": "guard it",
                    },
                    {
                        "severity": "high",
                        "category": "logic",
                        "file_path": "app/main.py",
                        "line": 12,
                        "description": "missing null check",
                        "suggestion": "guard it",
                    },
                ],
                "summary": "One issue, reported twice.",
                "spec_compliance_notes": "",
            }
        ]
    )

    result = run_coordinator(
        client,
        CodeReviewInput(files=files, task_description="Add feature", language="python"),
    )

    assert len(result.issues) == 1
    assert result.issues[0].line == 12


def test_code_review_agent_uses_coordinator_when_code_exceeds_limit() -> None:
    """End-to-end: ``CodeReviewAgent.run`` with code larger than the
    single-call limit dispatches to the coordinator and returns a
    merged CodeReviewOutput. The map-call count proves the coordinator split the
    oversized code into more than one chunk (rather than a single-call path)."""
    from code_review_agent.agent import CodeReviewAgent
    from code_review_agent.chunk_reviewer import CODE_TO_REVIEW_HEADER

    class _MapCounter(DummyLLMClient):
        """Counts per-chunk map-phase reviews (prompts carrying the code
        header). The reasoning pass now lands on ``complete_json`` (via the
        Strands Agent's ``chat()`` delegation), so this counts there instead
        of a separate ``complete`` override -- the formatting pass's prompt
        wraps the reasoning prose and never carries the raw code header."""

        def __init__(self) -> None:
            super().__init__()
            self.map_calls = 0

        def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
            if CODE_TO_REVIEW_HEADER in prompt:
                self.map_calls += 1
            return super().complete_json(prompt, **kwargs)

    # Multi-line so the splitter can break it at line boundaries into >1 chunk
    # (a single 25k-char line would stay one un-splittable chunk).
    files = {"app/main.py": "".join(f"x{i} = {i}\n" for i in range(4000))}

    client = _MapCounter()
    agent = CodeReviewAgent(llm_client=client, force_in_process=True)
    result = agent.run(
        CodeReviewInput(
            files=files,
            task_description="Test",
            language="python",
        )
    )

    assert isinstance(result, CodeReviewOutput)
    assert result.approved is True
    # Oversized code took the coordinator's map-reduce path: >1 chunk reviewed.
    assert client.map_calls > 1


# ---------------------------------------------------------------------------
# split_block_into_segments
# ---------------------------------------------------------------------------


def _numbered_file(n_lines: int, width: int = 40) -> str:
    """A deterministic multi-line file where every line is identifiable."""
    return "\n".join(f"line {i:05d} ".ljust(width, "x") for i in range(1, n_lines + 1))


def _failme_content_in_bisect_window(budget: int) -> str:
    """Build FAILME lines sized into [2 * MIN_SPLIT_SEGMENT_CHARS, budget).

    Preconditions:
        - budget > 2 * MIN_SPLIT_SEGMENT_CHARS (window must admit at least one line stride).
    Postconditions:
        - Returned content length L satisfies 2 * MIN_SPLIT_SEGMENT_CHARS <= L < budget.
        - Every line is 40 chars and contains the FAILME marker.
    Raises:
        ValueError: if budget is too tight for any whole number of lines to land in
            [2 * MIN_SPLIT_SEGMENT_CHARS, budget) — this helper can only produce content
            lengths that are multiples of the line stride (line_body_width + 1 chars),
            so some tight budgets admit no valid line count at all.
    """
    line_body_width = 40
    stride = line_body_width + 1  # joined length of n lines is stride*n - 1
    n_lines = max(1, math.ceil(2 * MIN_SPLIT_SEGMENT_CHARS / stride))
    content = "\n".join(
        f"FAILME {i:05d}".ljust(line_body_width, "x") for i in range(1, n_lines + 1)
    )
    if len(content) >= budget and n_lines > 1:
        n_lines -= 1
        content = "\n".join(
            f"FAILME {i:05d}".ljust(line_body_width, "x") for i in range(1, n_lines + 1)
        )
    if not (2 * MIN_SPLIT_SEGMENT_CHARS <= len(content) < budget):
        raise ValueError(
            f"budget={budget} admits no line count landing content in "
            f"[{2 * MIN_SPLIT_SEGMENT_CHARS}, budget) at this helper's {stride}-char "
            "line granularity"
        )
    return content


def test_failme_content_in_bisect_window_raises_for_unsatisfiable_tight_budget() -> None:
    """A budget within one line-stride of 2*MIN_SPLIT_SEGMENT_CHARS admits no valid
    line count (this helper's line lengths are quantized to 41-char strides), so the
    helper must raise a clear ValueError instead of silently violating its documented
    postcondition (regression for the bisect-window helper)."""
    budget = 2 * MIN_SPLIT_SEGMENT_CHARS + 1
    with pytest.raises(ValueError, match="admits no line count"):
        _failme_content_in_bisect_window(budget)


def test_failme_content_in_bisect_window_satisfies_postcondition_at_min_feasible_budget() -> None:
    """The smallest budget admitting a valid line count (one stride past the lower
    bound) must still satisfy the documented postcondition exactly."""
    budget = 2 * MIN_SPLIT_SEGMENT_CHARS + 41 + 1
    content = _failme_content_in_bisect_window(budget)
    assert 2 * MIN_SPLIT_SEGMENT_CHARS <= len(content) < budget


def test_split_within_budget_returns_single_whole_segment() -> None:
    content = _numbered_file(10)
    segments = split_block_into_segments("a.py", content, max_chars=10_000)
    assert len(segments) == 1
    seg = segments[0]
    assert seg.content == content
    assert seg.start_line == 1
    assert seg.total_lines == 10
    assert seg.is_partial is False
    assert seg.prompt_content == content  # whole files render verbatim, no prefixes


def test_split_reassembles_content_exactly_and_tracks_lines() -> None:
    content = _numbered_file(300)  # ~12.3K chars
    segments = split_block_into_segments("a.py", content, max_chars=4_000)
    assert len(segments) > 1
    assert "".join(s.content for s in segments) == content
    assert all(len(s.content) <= 4_000 for s in segments)
    # line bookkeeping is contiguous: each segment starts right after the previous
    assert segments[0].start_line == 1
    for prev, cur in zip(segments, segments[1:]):
        assert cur.start_line == prev.end_line + 1
    assert segments[-1].end_line == 300
    assert all(s.total_lines == 300 for s in segments)
    # the line at each boundary really is the one the bookkeeping claims
    for seg in segments[1:]:
        assert seg.content.splitlines()[0].startswith(f"line {seg.start_line:05d}")
    # partial segments render with absolute original line numbers prefixed,
    # and the rendered size stays within the budget the splitter was given
    for seg in segments:
        first_rendered = seg.prompt_content.splitlines()[0]
        prefix = re.match(r"^[ ]*(\d+)[:|] ", first_rendered)
        assert prefix is not None and int(prefix.group(1)) == seg.start_line
        assert len(seg.prompt_content) <= 4_000


def test_split_never_breaks_a_line_even_when_oversized() -> None:
    content = "x" * 25_000  # one giant line
    segments = split_block_into_segments("big.js", content, max_chars=8_000)
    assert len(segments) == 1
    assert segments[0].content == content


# ---------------------------------------------------------------------------
# Function-aware splitting: cuts land between whole constructs, never mid-body
# ---------------------------------------------------------------------------


def _python_functions(n_funcs: int, body_lines: int = 8) -> str:
    """A .py file of equal-sized top-level functions, blank-line separated."""
    parts: list[str] = []
    for f in range(1, n_funcs + 1):
        parts.append(f"def func_{f:03d}():")
        for b in range(body_lines):
            parts.append(f"    value_{f:03d}_{b} = compute({b})  " + "y" * 20)
        parts.append("")
    return "\n".join(parts)


def _ts_functions(n_funcs: int, body_lines: int = 8) -> str:
    """A .ts file of equal-sized top-level functions with column-0 braces."""
    parts: list[str] = []
    for f in range(1, n_funcs + 1):
        parts.append(f"function fn_{f:03d}() {{")
        for b in range(body_lines):
            parts.append(f"  const value_{f:03d}_{b} = compute({b});  " + "z" * 20)
        parts.append("}")
        parts.append("")
    return "\n".join(parts)


def test_split_breaks_at_function_boundary_python() -> None:
    """An oversized .py file splits at top-level def boundaries, never mid-function."""
    content = _python_functions(12)  # ~12 functions, each well under the budget
    segments = split_block_into_segments("svc.py", content, max_chars=1_500)
    assert len(segments) > 1
    # Every segment begins at a top-level ``def`` — no function is severed.
    for seg in segments:
        assert seg.content.splitlines()[0].startswith("def func_")
    # No function start is lost or duplicated, and content reassembles exactly.
    assert sum(s.content.count("def func_") for s in segments) == 12
    assert "".join(s.content for s in segments) == content
    assert all(len(s.prompt_content) <= 1_500 for s in segments)


def test_split_breaks_at_function_boundary_typescript() -> None:
    """An oversized .ts file splits at top-level function boundaries via the heuristic."""
    content = _ts_functions(12)
    segments = split_block_into_segments("widget.ts", content, max_chars=1_500)
    assert len(segments) > 1
    for seg in segments:
        assert seg.content.splitlines()[0].startswith("function fn_")
    assert sum(s.content.count("function fn_") for s in segments) == 12
    assert "".join(s.content for s in segments) == content
    assert all(len(s.prompt_content) <= 1_500 for s in segments)


def test_split_falls_back_to_line_boundary_when_no_constructs() -> None:
    """A single oversized function with no interior boundary degrades to line splitting."""
    # One giant function larger than the budget: its only construct boundary is
    # line 1, which the splitter never cuts before, so it degrades to splitting
    # on line boundaries within the function body.
    body = "\n".join(f"    step_{i:04d} = run({i})  " + "y" * 20 for i in range(300))
    content = "def one_big_function():\n" + body
    segments = split_block_into_segments("mono.py", content, max_chars=2_000)
    assert len(segments) > 1
    assert "".join(s.content for s in segments) == content
    assert all(len(s.prompt_content) <= 2_000 for s in segments)


def test_split_function_aware_keeps_contiguous_line_bookkeeping() -> None:
    """Function-aware cuts keep start/end line bookkeeping contiguous and exact."""
    content = _python_functions(10)
    segments = split_block_into_segments("svc.py", content, max_chars=1_200)
    assert len(segments) > 1
    assert "".join(s.content for s in segments) == content
    assert segments[0].start_line == 1
    for prev, cur in zip(segments, segments[1:]):
        assert cur.start_line == prev.end_line + 1
    total_lines = len(content.splitlines())
    assert segments[-1].end_line == total_lines
    assert all(s.total_lines == total_lines for s in segments)


# ---------------------------------------------------------------------------
# pre_numbered: explicit producer flag (never sniffed from content)
# ---------------------------------------------------------------------------


def test_split_flags_pre_numbered_segments_without_double_prefixing() -> None:
    hunk = "\n".join(f"{4000 + i}: code_{i}()".ljust(40, " ") for i in range(300))
    segments = split_block_into_segments("pr.py", hunk, max_chars=4_000, pre_numbered=True)
    assert len(segments) > 1
    assert all(s.pre_numbered for s in segments)
    # Pre-numbered content already carries its prefixes: rendered verbatim.
    assert all(s.prompt_content == s.content for s in segments)


def test_int_keyed_mapping_content_is_not_treated_as_pre_numbered() -> None:
    """A dict-literal file whose lines look like ``N: value`` must NOT be
    treated as pre-numbered unless the producer declared it: its split
    segments get real original-line prefixes in the prompt."""
    content = "STATUS = {\n" + "\n".join(f"    {i}: 'v{i}'," for i in range(1, 300)) + "\n}"
    segments = split_block_into_segments("status.py", content, max_chars=2_000)
    assert len(segments) > 1
    assert all(not s.pre_numbered for s in segments)
    for seg in segments:
        prefix = re.match(r"^[ ]*(\d+)[:|] ", seg.prompt_content.splitlines()[0])
        assert prefix is not None and int(prefix.group(1)) == seg.start_line


def test_segment_range_label_uses_embedded_numbers_for_pre_numbered() -> None:
    """Unreviewed-range reporting must cite the embedded original lines for
    pre-numbered hunks, not the positional 1-based indices."""
    hunk = "\n".join(f"{4000 + i}: code_{i}()" for i in range(51))
    seg = split_block_into_segments("src/feature.py", hunk, 100_000, pre_numbered=True)[0]
    assert _segment_range_label(seg) == "src/feature.py (original lines 4000-4050)"
    plain = FileSegment(path="a.py", content="x = 1\ny = 2", start_line=5, total_lines=20)
    assert _segment_range_label(plain) == "a.py (lines 5-6 of 20)"


# ---------------------------------------------------------------------------
# FileSegment / ReviewChunk construction invariants
# ---------------------------------------------------------------------------


def test_file_segment_valid_constructs() -> None:
    seg = FileSegment(path="a.py", content="x = 1\ny = 2", start_line=5, total_lines=20)
    assert seg.end_line == 6


def test_partial_segment_prompt_content_aligns_source_across_digit_widths() -> None:
    """Partial-file prefixes must not shift hanging indents at the 9→10 boundary."""
    content = "    foo(\n        'bar',\n    )\n"
    seg = FileSegment(path="a.py", content=content, start_line=9, total_lines=20)
    match = re.compile(r"^([ ]*\d+(?:: |\| ))(.*)$")
    gutters, sources = zip(
        *(
            (m.group(1), m.group(2))
            for ln in seg.prompt_content.splitlines()
            if (m := match.match(ln))
        )
    )
    assert list(sources) == ["    foo(", "        'bar',", "    )"]
    assert len({len(g) for g in gutters}) == 1
    assert sources[1].index("'") - sources[0].index("f") == 4


def test_file_segment_rejects_zero_start_line() -> None:
    with pytest.raises(ValidationError, match="start_line must be 1-based"):
        FileSegment(path="a.py", content="x = 1", start_line=0, total_lines=1)


def test_file_segment_rejects_total_lines_too_small() -> None:
    with pytest.raises(ValidationError, match="total_lines must be at least line_count"):
        FileSegment(path="a.py", content="x = 1\ny = 2", total_lines=1)


def test_file_segment_rejects_extending_past_eof() -> None:
    with pytest.raises(ValidationError, match="segment extends past end of file"):
        FileSegment(path="a.py", content="x = 1\ny = 2", start_line=5, total_lines=5)


def test_review_chunk_rejects_duplicate_paths() -> None:
    with pytest.raises(ValidationError, match="unique paths"):
        ReviewChunk(
            segments=[
                FileSegment(path="a.py", content="x = 1", total_lines=1),
                FileSegment(path="a.py", content="y = 2", total_lines=1),
            ]
        )


def test_review_chunk_rejects_duplicate_empty_paths() -> None:
    with pytest.raises(ValidationError, match="unique paths"):
        ReviewChunk(
            segments=[
                FileSegment(path="", content="x = 1", total_lines=1),
                FileSegment(path="", content="y = 2", total_lines=1),
            ]
        )


def test_review_chunk_allows_distinct_paths_including_empty() -> None:
    chunk = ReviewChunk(
        segments=[
            FileSegment(path="", content="x = 1", total_lines=1),
            FileSegment(path="a.py", content="y = 2", total_lines=1),
        ]
    )
    assert len(chunk.segments) == 2


@pytest.mark.parametrize(
    "content",
    [
        "line1\nline2\nline3",  # no trailing newline
        "line1\nline2\nline3\n",  # trailing newline terminates the last line
        "line1\nline2\nline3\n\n",  # blank line before EOF
    ],
    ids=["no-trailing-newline", "trailing-newline", "blank-line-before-eof"],
)
def test_file_segment_line_count_matches_total_lines_convention(content: str) -> None:
    """A whole-file segment's line_count must equal the total_lines that
    chunking.split_block_into_segments computes for identical content (the
    same `len(content.splitlines()) or 1` formula), so end_line always lands
    on total_lines and is_partial is False -- regardless of a trailing
    newline or a blank final line."""
    total_lines = len(content.splitlines()) or 1
    seg = FileSegment(path="a.py", content=content, start_line=1, total_lines=total_lines)
    assert seg.line_count == total_lines
    assert seg.end_line == total_lines
    assert seg.is_partial is False


def test_bisect_segment_first_half_end_line_reflects_its_own_content(monkeypatch) -> None:
    """Both halves of a bisected segment must report their own true range, not
    the original segment's — end_line/is_partial are computed from
    start_line + content on FileSegment, so model_copy(update={"content": ...})
    alone (no explicit end_line update) already keeps them correct."""
    monkeypatch.setenv("CODE_REVIEW_MIN_SPLIT_SEGMENT_CHARS", "1000")  # env floor is 1000
    lines = [f"line{i}\n" for i in range(1, 301)]  # 300 lines, ~2.3KB, clears the 2x-floor gate
    seg = FileSegment(path="a.py", content="".join(lines), start_line=1, total_lines=300)

    halves = _bisect_segment(seg)
    assert halves is not None
    first, second = halves

    assert first.content + second.content == seg.content
    assert first.start_line == seg.start_line
    assert first.end_line == seg.start_line + first.line_count - 1
    assert first.end_line < seg.end_line
    assert second.start_line == first.end_line + 1
    assert second.end_line == seg.end_line
    assert first.is_partial is True
    assert second.is_partial is True
    # Concrete cross-check against the known input (not the property under
    # test): the split boundary derived from first.line_count must match
    # where the original `lines` were actually cut.
    assert first.line_count + second.line_count == seg.line_count == 300
    assert first.content == "".join(lines[: first.line_count])
    assert second.content == "".join(lines[first.line_count :])


# ---------------------------------------------------------------------------
# build_review_chunks — no-file-dropped property
# ---------------------------------------------------------------------------


def _coverage_by_path(chunks: list) -> dict:
    by_path: dict = {}
    for chunk in chunks:
        for seg in chunk.segments:
            by_path.setdefault(seg.path, []).append(seg)
    return by_path


def test_build_review_chunks_no_file_dropped_property() -> None:
    """Mixed input — small files, one 3×-cap file, one headerless block — must be
    covered exactly once, with every rendered chunk within the cap."""
    cap = 10_000
    big = _numbered_file(750)  # ~3× the cap
    blocks = [
        ("small_a.py", "def a(): pass"),
        ("big.py", big),
        ("small_b.py", "def b(): pass"),
        ("", "headerless = True"),
    ]
    chunks = build_review_chunks(blocks, max_chars=cap)

    covered = _coverage_by_path(chunks)
    assert set(covered) == {"small_a.py", "big.py", "small_b.py", ""}
    for path, content in blocks:
        segs = covered[path]
        # exactly-once coverage: concatenation reproduces the block, contiguously
        assert "".join(s.content for s in segs) == content
        assert segs[0].start_line == 1
        for prev, cur in zip(segs, segs[1:]):
            assert cur.start_line == prev.end_line + 1

    for chunk in chunks:
        assert len(chunk.content) <= cap
        paths = [s.path for s in chunk.segments]
        assert len(paths) == len(set(paths)), "a chunk may not hold two segments of one path"


def test_build_review_chunks_oversized_single_line_sits_alone() -> None:
    blocks = [("a.py", "x" * 30_000), ("b.py", "def b(): pass")]
    chunks = build_review_chunks(blocks, max_chars=10_000)
    oversized = [c for c in chunks if any(s.path == "a.py" for s in c.segments)]
    assert len(oversized) == 1
    assert [s.path for s in oversized[0].segments] == ["a.py"]


def test_cap_chunk_content_passes_through_within_budget() -> None:
    content = "x" * 9_999
    assert cap_chunk_content(content, 10_000) == [content]


def test_cap_chunk_content_splits_oversized_into_bounded_pieces() -> None:
    content = "y" * 25_001  # an unsplittable single line over the cap
    pieces = cap_chunk_content(content, 10_000)
    assert len(pieces) == 3
    assert "".join(pieces) == content  # nothing dropped or duplicated
    assert all(len(p) <= 10_000 for p in pieces)


def test_cap_chunk_content_rejects_nonpositive_cap() -> None:
    with pytest.raises(AssertionError):
        cap_chunk_content("abc", 0)


def test_cap_review_chunk_passes_through_within_budget() -> None:
    chunk = ReviewChunk(segments=[FileSegment(path="a.py", content="x = 1", total_lines=1)])
    assert cap_review_chunk(chunk, 10_000) == [chunk.content]


def test_cap_review_chunk_preserves_header_on_every_piece() -> None:
    """An over-budget single-segment chunk (a line longer than the cap) is split
    into bounded pieces that each carry the ### path ### header so a finding in
    any tail piece stays attributable."""
    line = "y" * 25_000  # one unsplittable line over the cap
    chunk = ReviewChunk(segments=[FileSegment(path="bundle.js", content=line, total_lines=1)])
    pieces = cap_review_chunk(chunk, 10_000)
    assert len(pieces) > 1
    assert all(len(p) <= 10_000 for p in pieces)
    assert all(p.startswith("### bundle.js ###\n") for p in pieces)
    # Stripping the header from each piece reproduces the original line.
    header = "### bundle.js ###\n"
    assert "".join(p[len(header) :] for p in pieces) == line


def test_cap_review_chunk_headerless_segment_falls_back_to_raw_split() -> None:
    """A headerless (path == '') over-budget chunk has no header to preserve, so
    it splits like cap_chunk_content."""
    line = "z" * 25_000
    chunk = ReviewChunk(segments=[FileSegment(path="", content=line, total_lines=1)])
    pieces = cap_review_chunk(chunk, 10_000)
    assert len(pieces) > 1
    assert all(len(p) <= 10_000 for p in pieces)
    assert "".join(pieces) == line  # raw split, no header injected


def test_cap_review_chunk_drops_header_when_header_alone_exceeds_cap() -> None:
    """When the ### path ### header itself is >= max_chars, attaching it to
    every piece would blow the budget it's meant to enforce. In that case the
    header must be dropped (raw split of the whole rendered content, header
    included at most once) rather than every piece exceeding max_chars."""
    long_path = "pkg/" + ("d" * 50) + "/module.py"
    header = f"### {long_path} ###\n"
    line = "y" * 25_000
    chunk = ReviewChunk(segments=[FileSegment(path=long_path, content=line, total_lines=1)])
    max_chars = len(header)  # header alone already meets the cap
    pieces = cap_review_chunk(chunk, max_chars)
    assert all(len(p) <= max_chars for p in pieces)
    # Raw split of chunk.content (header + line, header appearing once) --
    # not the per-piece-header behavior, which would repeat the header in
    # every piece and push each piece's length past max_chars.
    assert "".join(pieces) == chunk.content
    assert chunk.content.count(header) == 1


def test_cap_review_chunk_rejects_nonpositive_cap() -> None:
    chunk = ReviewChunk(segments=[FileSegment(path="a.py", content="x = 1", total_lines=1)])
    with pytest.raises(AssertionError):
        cap_review_chunk(chunk, 0)


def test_review_chunk_paths_label_marks_partial_segments() -> None:
    big = _numbered_file(750)
    chunks = build_review_chunks([("big.py", big)], max_chars=10_000)
    assert len(chunks) > 1
    first = chunks[0]
    assert first.paths_label.startswith("big.py (lines 1-")
    assert "of 750)" in first.paths_label
    whole = ReviewChunk(segments=[FileSegment(path="a.py", content="x = 1", total_lines=1)])
    assert whole.paths_label == "a.py"


# ---------------------------------------------------------------------------
# files= input path
# ---------------------------------------------------------------------------


def test_blank_file_content_is_named_by_info_finding() -> None:
    """An empty/whitespace-only file is skipped from review but never silently:
    it gets a non-blocking info finding naming it."""
    result = run_coordinator(
        DummyLLMClient(),
        CodeReviewInput(
            files={"pkg/__init__.py": "", "pkg/api.py": "def api(): pass"},
            task_description="t",
            language="python",
        ),
    )
    assert result.approved is True
    info = [i for i in result.issues if i.severity == "info"]
    assert [i.file_path for i in info] == ["pkg/__init__.py"]
    assert "empty" in info[0].description


def test_all_files_blank_short_circuits_with_info_findings() -> None:
    result = run_coordinator(
        DummyLLMClient(),
        CodeReviewInput(files={"a.py": "   "}, task_description="t"),
    )
    assert result.approved is True
    assert result.summary == "No code to review."
    assert [i.file_path for i in result.issues] == ["a.py"]
    assert result.issues[0].severity == "info"


# ---------------------------------------------------------------------------
# Failure recovery: retry, bisect, fail loudly
# ---------------------------------------------------------------------------


def _bisecting_failure(msg: str = "no content") -> LLMTruncatedError:
    """A recoverable content failure that still LINE-splits and same-input-retries.

    Used to exercise the generic recovery machinery (line-bisection, same-input
    retry, degrade) independently of semantic exhaustion, which now takes a
    fast-path: a single-file semantically-exhausted chunk degrades immediately
    (no line-split, no retry). ``LLMTruncatedError`` (finish_reason=length) is the
    canonical still-recoverable-via-smaller-input failure.
    """
    return LLMTruncatedError(msg, partial_content="", finish_reason="length")


class _SelectiveRaiser(DummyLLMClient):
    """Raises for prompts containing a marker; otherwise delegates to Dummy.

    Records every prompt so tests can count map calls. Thread-safe for use
    with concurrent tail-pass execution. Both the reasoning pass (reached via
    the Strands Agent's ``chat()`` delegation) and the formatting pass (a
    direct ``complete_json`` call from ``run_agent_via_reasoning``) land on
    ``complete_json`` now -- the marker only ever appears in the reasoning
    prompt (the raw code chunk), so gating the raise on "not a formatting
    pass" (``--- ANALYSIS`` absent) keeps this from ever misfiring on a
    formatting-pass prompt.
    """

    def __init__(self, marker: str, exc: Exception | None = None) -> None:
        super().__init__()
        self.marker = marker
        self.exc = exc or _bisecting_failure("LLM output truncated")
        self.prompts: list[str] = []
        self._lock = threading.Lock()

    def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            self.prompts.append(prompt)
            if self.marker in prompt and not _is_formatting_pass_prompt(prompt):
                raise self.exc
        return super().complete_json(prompt, **kwargs)


class _FailNTimes(DummyLLMClient):
    """Fails the first ``n`` chunk-map reasoning passes, then succeeds.

    The reasoning pass now lands on ``complete_json`` (via the Strands
    Agent's ``chat()`` delegation), so this checks
    ``_is_chunk_map_reasoning_prompt`` directly on the ``complete_json``
    prompt instead of a separate ``complete`` override -- the formatting
    pass's prompt never carries the code-to-review header, so it is
    naturally excluded.
    """

    def __init__(self, n: int) -> None:
        super().__init__()
        self.remaining = n
        self.prompts: list[str] = []
        self._lock = threading.Lock()

    def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        if _is_chunk_map_reasoning_prompt(prompt):
            with self._lock:
                self.prompts.append(prompt)
                if self.remaining > 0:
                    self.remaining -= 1
                    raise _bisecting_failure("transient")
        return super().complete_json(prompt, **kwargs)


def _delegate_chat_via_complete_json(
    delegate: Any, messages: list, *, tools: list | None = None, **kwargs: Any
) -> Any:
    """Shared ``chat()``-to-``complete_json`` bridge for a wrapped-``DummyLLMClient``
    delegate (see ``_HalfTimingDummyDelegate``/``_MultiFileFirstCallFailsDelegate``,
    which both define an explicit ``chat`` override for the same reason and
    otherwise differ only in their ``complete_json`` per-half/per-path hooks).

    Preconditions:
        ``delegate`` exposes ``complete_json`` (its own override) and ``_inner``
        (a real ``DummyLLMClient``, used for the tooled-call passthrough).

    Postconditions:
        A tooled call (chunk review never passes tools) degrades unchanged to
        ``delegate._inner.chat(...)``. Otherwise extracts the system/user
        prompt from ``messages`` and routes to ``delegate.complete_json(...)``,
        JSON-serializing the result when ``response_format="text"`` was
        requested (matching the real ``DummyLLMClient.chat()`` contract).
    """
    if tools:
        return delegate._inner.chat(messages, tools=tools, **kwargs)
    system_prompt = None
    user_prompt = ""
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "system":
            system_prompt = m.get("content")
        elif m.get("role") == "user":
            user_prompt = m.get("content") or ""
    response_format = kwargs.pop("response_format", "json")
    data = delegate.complete_json(user_prompt, system_prompt=system_prompt, tools=None, **kwargs)
    if response_format == "text":
        return json.dumps(data) if isinstance(data, dict) else str(data)
    return data


class _HalfTimingDummyDelegate:
    """Inner delegate for a non-``DummyLLMClient`` stand-in (see
    ``_NonDummyLLMClient`` further below in this file): forces the combined
    a.py+b.py chunk review to fail (triggering bisection), returns a distinct
    low-severity issue per single-file chunk-review half (so a test can
    inspect merge order), and records each half's call interval — sleeping
    first if a delay was configured for that half — so a test can prove or
    control relative timing. Every other prompt (the tail passes) is
    delegated to a real ``DummyLLMClient``.
    """

    def __init__(self, delays: dict[str, float] | None = None) -> None:
        self._inner = DummyLLMClient()
        self.delays = delays or {}
        self._lock = threading.Lock()
        self.intervals: dict[str, tuple[float, float]] = {}
        self._pending_half: str | None = None

    def __getattr__(self, name: str) -> Any:
        # Forward anything not overridden below (get_max_context_tokens,
        # update_config, get_config, structured_output, stream, ...) to
        # the real DummyLLMClient — _NonDummyLLMClient calls those directly.
        return getattr(self._inner, name)

    def chat(self, messages: list, *, tools: list | None = None, **kwargs: Any) -> Any:
        """Explicit override so the reasoning pass's ``self.complete_json(...)``
        call (inside ``DummyLLMClient.chat()``) binds to THIS delegate's own
        override -- ``__getattr__`` forwarding would instead hand back the
        real inner ``DummyLLMClient``'s bound ``chat`` method, whose internal
        ``self.complete_json(...)`` call binds to the inner client and
        bypasses this delegate's per-half timing/failure hooks entirely.
        Chunk review never passes tools (``tools=[]``), so the real
        ``chat()``'s tool-call branches are never exercised here and are not
        reproduced; a tooled call still degrades to the inner client. See
        ``_delegate_chat_via_complete_json`` for the shared parsing/routing
        logic this shares with ``_MultiFileFirstCallFailsDelegate.chat``.
        """
        return _delegate_chat_via_complete_json(self, messages, tools=tools, **kwargs)

    def _half_review_response(self, key: str) -> dict[str, Any]:
        return {
            "approved": True,
            "issues": [
                {
                    "severity": "low",
                    "category": "general",
                    "file_path": f"{key}.py",
                    "line": 1,
                    "description": f"finding-{key}",
                    "suggestion": "n/a",
                }
            ],
            "summary": f"summary-{key}",
            "spec_compliance_notes": "",
        }

    def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        # Both the reasoning pass (reached via the Strands Agent's ``chat()``
        # delegation) and the formatting pass (a direct ``complete_json`` call
        # from ``run_agent_via_reasoning``) land here now. The formatting pass
        # is identified by ``_is_formatting_pass_prompt`` (it carries the
        # "--- ANALYSIS" wrapper), while any reasoning prompt never carries
        # that marker -- so the pass is identified by content, not by call
        # order (concurrent halves interleave calls).
        if _is_formatting_pass_prompt(prompt):
            with self._lock:
                key = self._pending_half
                self._pending_half = None
            if key is not None:
                return self._half_review_response(key)
            return self._inner.complete_json(prompt, **kwargs)
        is_chunk_review = _is_chunk_map_reasoning_prompt(prompt)
        has_a, has_b = "### a.py ###" in prompt, "### b.py ###" in prompt
        if is_chunk_review and has_a and has_b:
            raise _bisecting_failure("no content")  # force bisection
        if is_chunk_review and (has_a or has_b):
            key = "a" if has_a else "b"
            start = time.monotonic()
            time.sleep(self.delays.get(key, 0.0))
            end = time.monotonic()
            with self._lock:
                self.intervals[key] = (start, end)
                self._pending_half = key
        return self._inner.complete_json(prompt, **kwargs)


class _MultiFileFirstCallFailsDelegate:
    """Inner delegate for a non-``DummyLLMClient`` stand-in (see
    ``_NonDummyLLMClient``): the FIRST chunk-review call naming a given
    top-level file path fails (forcing that chunk to bisect); every later
    call naming that path succeeds but blocks on ``release`` while tracking
    how many chunk-review calls are simultaneously in flight, so a test can
    observe the true PEAK concurrency across several top-level chunks that
    bisect at the same time -- not just within one chunk's own bisection.
    Every other prompt (the tail passes) is delegated to a real
    ``DummyLLMClient``.
    """

    def __init__(self, paths: list[str], release: threading.Event) -> None:
        self._inner = DummyLLMClient()
        self._paths = paths
        self._release = release
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        self.current = 0
        self.peak = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def chat(self, messages: list, *, tools: list | None = None, **kwargs: Any) -> Any:
        """See ``_HalfTimingDummyDelegate.chat`` for why this explicit
        override is required: ``__getattr__`` forwarding would otherwise hand
        back the inner ``DummyLLMClient``'s bound ``chat``, whose internal
        ``self.complete_json(...)`` call bypasses this delegate's per-path
        bisect/concurrency hooks for the reasoning pass. See
        ``_delegate_chat_via_complete_json`` for the shared parsing/routing
        logic this shares with ``_HalfTimingDummyDelegate.chat``."""
        return _delegate_chat_via_complete_json(self, messages, tools=tools, **kwargs)

    def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        # The reasoning pass now lands here too (via the Strands Agent's
        # ``chat()`` delegation). A path marker only ever appears in a
        # reasoning-pass prompt (the raw code chunk) -- the formatting pass
        # wraps the reasoning prose instead -- so this needs no additional
        # pass-discrimination gate.
        is_chunk_review = _is_chunk_map_reasoning_prompt(prompt)
        path = next((p for p in self._paths if f"### {p} ###" in prompt), None)
        if is_chunk_review and path is not None:
            with self._lock:
                first = path not in self._seen
                self._seen.add(path)
            if first:
                raise _bisecting_failure("no content")  # force this chunk to bisect
            with self._lock:
                self.current += 1
                self.peak = max(self.peak, self.current)
            self._release.wait(timeout=5)
            with self._lock:
                self.current -= 1
        return self._inner.complete_json(prompt, **kwargs)


class _TimedDummyHalfClient(DummyLLMClient):
    """``DummyLLMClient`` subclass with the same combined-fails/per-half-timing
    behavior as ``_HalfTimingDummyDelegate``, but reached as a bare
    ``DummyLLMClient`` instance (not wrapped) — used to prove the two halves
    still run strictly sequentially for a scripted double, exactly as before
    this fan-out existed.
    """

    def __init__(self, delay: float = 0.0) -> None:
        super().__init__()
        self.delay = delay
        self._lock = threading.Lock()
        self.intervals: dict[str, tuple[float, float]] = {}
        self._pending_half: str | None = None

    def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        # Both passes land on ``complete_json`` now; the formatting pass is
        # identified by ``_is_formatting_pass_prompt``.
        if _is_formatting_pass_prompt(prompt):
            self._pending_half = None
            return super().complete_json(prompt, **kwargs)
        is_chunk_review = _is_chunk_map_reasoning_prompt(prompt)
        has_a, has_b = "### a.py ###" in prompt, "### b.py ###" in prompt
        if is_chunk_review and has_a and has_b:
            raise _bisecting_failure("no content")
        if is_chunk_review and (has_a or has_b):
            key = "a" if has_a else "b"
            start = time.monotonic()
            time.sleep(self.delay)
            end = time.monotonic()
            with self._lock:
                self.intervals[key] = (start, end)
                self._pending_half = key
        return super().complete_json(prompt, **kwargs)


def test_failing_multi_segment_chunk_bisects_and_recovers() -> None:
    """A chunk whose combined review fails is bisected per segment; both halves
    succeed individually, so the review completes normally."""

    class _FailOnCombined(DummyLLMClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
            if _is_chunk_map_reasoning_prompt(prompt):
                self.calls += 1
                if "### a.py ###" in prompt and "### b.py ###" in prompt:
                    raise LLMSemanticExhaustionError("no content")
            return super().complete_json(prompt, **kwargs)

    client = _FailOnCombined()
    result = run_coordinator(
        client,
        CodeReviewInput(
            files={"a.py": "def a(): pass", "b.py": "def b(): pass"},
            task_description="t",
            language="python",
            skip_tail_passes=True,
        ),
    )
    # Map-phase reasoning attempts only (format passes are not counted): combined
    # fail + two single-file recoveries.
    assert client.calls == 3
    assert result.approved is True
    assert all(i.severity != "info" for i in result.issues)


def test_transient_failure_recovers_via_same_input_retry() -> None:
    """A chunk too small to bisect gets one same-input retry, so a one-off
    transient error never costs the review."""
    client = _FailNTimes(1)
    result = run_coordinator(
        client,
        CodeReviewInput(
            files={"only.py": "def only(): pass"},
            task_description="t",
            language="python",
            skip_tail_passes=True,
        ),
    )
    assert result.approved is True
    # Map-phase reasoning attempts only: initial failure + successful retry.
    assert len(client.prompts) == 2


def test_transient_failure_in_bisected_child_recovers() -> None:
    """A one-off transient failure in a bisected child that is too small to
    split further gets its own same-input retry — it must not abort a review
    that bisection was already recovering."""

    class _FailCombinedAndChildOnce(DummyLLMClient):
        def __init__(self) -> None:
            super().__init__()
            self.a_failures = 0
            self.calls = 0

        def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
            if _is_chunk_map_reasoning_prompt(prompt):
                self.calls += 1
                if "### a.py ###" in prompt and "### b.py ###" in prompt:
                    raise _bisecting_failure("no content")  # force bisection
                if (
                    "### a.py ###" in prompt
                    and "### b.py ###" not in prompt
                    and self.a_failures == 0
                ):
                    self.a_failures += 1
                    raise _bisecting_failure("transient child hiccup")
            return super().complete_json(prompt, **kwargs)

    client = _FailCombinedAndChildOnce()
    result = run_coordinator(
        client,
        CodeReviewInput(
            files={"a.py": "def a(): pass", "b.py": "def b(): pass"},
            task_description="t",
            language="python",
            skip_tail_passes=True,
        ),
    )
    assert result.approved is True
    # Map-phase reasoning attempts only: combined fail + a fail + a retry success
    # + b success.
    assert client.calls == 4


def test_bisect_halves_run_sequentially_detects_dummy_and_wrapped_dummy() -> None:
    """The bisect-halves dummy guard (shared ``is_dummy_llm_client_wrapped``
    detection): True for a bare ``DummyLLMClient`` and for a wrapper exposing a
    ``.client`` attribute pointing at one; False for anything else."""
    assert _bisect_halves_run_sequentially(DummyLLMClient()) is True
    assert _bisect_halves_run_sequentially(MagicMock()) is False

    class _Wrapper:
        def __init__(self, client: Any) -> None:
            self.client = client

    assert _bisect_halves_run_sequentially(_Wrapper(DummyLLMClient())) is True
    assert _bisect_halves_run_sequentially(_Wrapper(MagicMock())) is False


def test_run_reviewer_call_acquires_and_always_releases_the_run_limiter() -> None:
    """``_run_reviewer_call`` is the single choke point every actual chunk
    review passes through: it must acquire the given semaphore before calling
    ``reviewer.run`` and release it afterward on both success and failure, so
    a permit is never leaked and the next caller (a sibling top-level chunk or
    a bisection half) can always proceed once this call finishes."""

    class _Stub:
        def run(self, chunk_input: Any, **kwargs: Any) -> Any:
            return ("ok", kwargs)

    limiter = threading.Semaphore(1)
    assert _run_reviewer_call(_Stub(), object(), limiter) == ("ok", {})
    # Released after success: the sole permit is available again.
    assert limiter.acquire(blocking=False) is True
    limiter.release()

    class _Raises:
        def run(self, chunk_input: Any, **kwargs: Any) -> Any:
            raise ValueError("boom")

    with pytest.raises(ValueError):
        _run_reviewer_call(_Raises(), object(), limiter)
    # Released after a failure too -- never leaked on the exception path.
    assert limiter.acquire(blocking=False) is True
    limiter.release()


def test_run_reviewer_call_with_no_limiter_is_a_passthrough() -> None:
    """``run_limiter=None`` (a direct caller, a test double, or the Temporal
    per-activity call path -- none of which share a limiter object) skips the
    semaphore entirely and just forwards to ``reviewer.run``."""

    class _Stub:
        def run(self, chunk_input: Any, **kwargs: Any) -> Any:
            return kwargs

    assert _run_reviewer_call(_Stub(), object(), None, think=False) == {"think": False}


def test_bisected_halves_reviewed_concurrently_for_non_dummy_client() -> None:
    """When the LLM is not a scripted ``DummyLLMClient`` double, the two
    bisected halves are reviewed concurrently instead of strictly
    sequentially: each half's call sleeps briefly, and their recorded
    intervals must overlap — sequential calls could never produce that."""
    delegate = _HalfTimingDummyDelegate(delays={"a": 0.05, "b": 0.05})
    stand_in = _NonDummyLLMClient(delegate)
    result = run_coordinator(
        stand_in,
        CodeReviewInput(
            files={"a.py": "def a(): pass", "b.py": "def b(): pass"},
            task_description="t",
            language="python",
        ),
    )
    assert result.approved is True
    a_start, a_end = delegate.intervals["a"]
    b_start, b_end = delegate.intervals["b"]
    assert a_start < b_end and b_start < a_end


def test_bisected_halves_stay_sequential_for_dummy_llm_client() -> None:
    """Scripted ``DummyLLMClient`` doubles use a shared non-thread-safe
    response index, so the two bisected halves must still run one at a
    time for them: each half's call sleeps briefly, and their recorded
    intervals must NOT overlap."""
    client = _TimedDummyHalfClient(delay=0.05)
    result = run_coordinator(
        client,
        CodeReviewInput(
            files={"a.py": "def a(): pass", "b.py": "def b(): pass"},
            task_description="t",
            language="python",
        ),
    )
    assert result.approved is True
    a_start, a_end = client.intervals["a"]
    b_start, b_end = client.intervals["b"]
    assert a_end <= b_start or b_end <= a_start


def test_bisection_absorb_preserves_halves_order_regardless_of_completion_order() -> None:
    """halves[1] (b.py) finishes first — its call has no delay while halves[0]
    (a.py) sleeps — but the merged outcome must still list a.py's finding
    before b.py's: ``.absorb()`` merge order is fixed by input order, not by
    which concurrent branch actually completes first."""
    delegate = _HalfTimingDummyDelegate(delays={"a": 0.08, "b": 0.0})
    stand_in = _NonDummyLLMClient(delegate)
    result = run_coordinator(
        stand_in,
        CodeReviewInput(
            files={"a.py": "def a(): pass", "b.py": "def b(): pass"},
            task_description="t",
            language="python",
        ),
    )
    a_start, a_end = delegate.intervals["a"]
    b_start, b_end = delegate.intervals["b"]
    assert b_end <= a_end  # sanity check: b genuinely finished first
    findings = [i.description for i in result.issues if i.description.startswith("finding-")]
    assert findings == ["finding-a", "finding-b"]


def test_semantic_exhaustion_single_file_degrades_without_bisect_or_retry() -> None:
    """A single-file chunk that semantically exhausts degrades straight to a
    blocking not-reviewed finding — no line-split, no same-input retry (both would
    only re-run the model's already-spent thinking ladder for the same doomed
    result). Exactly one map call, unlike a line-splitting content failure."""
    # Size the file above the bisect floor but within one map chunk: a
    # line-splitting failure WOULD bisect it (see the LLMTruncatedError analogue
    # in test_large_failing_file_bisects_then_raises_with_ranges), but semantic
    # exhaustion must not.
    budget = compute_code_review_map_chunk_chars(DummyLLMClient())
    content = _failme_content_in_bisect_window(budget)
    # retry_thinking_level set => the client actually spent its downgrade ladder,
    # so re-sampling is futile and the fast-path degrades without retry.
    client = _SelectiveRaiser(
        "FAILME",
        exc=LLMSemanticExhaustionError(
            "LLM returned reasoning only (no content)", retry_thinking_level=False
        ),
    )
    with pytest.raises(CodeReviewUnavailableError) as excinfo:
        run_coordinator(
            client,
            CodeReviewInput(files={"big.py": content}, task_description="t", language="python"),
        )
    assert len(client.prompts) == 1  # no line-split, no retry
    assert any("big.py" in r for r in excinfo.value.unreviewed)


def test_length_empty_semantic_exhaustion_still_line_splits() -> None:
    """A ``finish_reason="length"`` empty turn is token-budget-bound, not a reasoning
    loop: a smaller chunk can leave room for content, so it must still line-split like
    a truncation — unlike a reasoning-only (finish_reason=stop) exhaustion, which does
    not. This is the same large single-file setup as the reasoning-loop test above,
    but the length variant bisects (>=2 calls) instead of degrading on the first."""
    budget = compute_code_review_map_chunk_chars(DummyLLMClient())
    content = _failme_content_in_bisect_window(budget)
    client = _SelectiveRaiser(
        "FAILME",
        exc=LLMSemanticExhaustionError(
            "no content at the token cap", finish_reason="length", retry_thinking_level=False
        ),
    )
    with pytest.raises(CodeReviewUnavailableError) as excinfo:
        run_coordinator(
            client,
            CodeReviewInput(files={"big.py": content}, task_description="t", language="python"),
        )
    assert len(client.prompts) >= 2  # line-split like a truncation, not degraded on attempt 1
    assert any("big.py" in r for r in excinfo.value.unreviewed)


def test_semantic_exhaustion_multi_file_still_separates_files(monkeypatch) -> None:
    """A multi-file chunk that semantically exhausts on the combined review still
    splits by FILE, so a clean sibling is reviewed while only the culprit degrades
    — file separation is worthwhile even though line-splitting is not. On the
    default (non-blocking) path the culprit's range is surfaced via
    ``not_reviewed_ranges`` and does not block; the clean sibling still reviews."""
    monkeypatch.delenv("CODE_REVIEW_BLOCK_ON_UNREVIEWED", raising=False)

    class _FailWhenBadPresent(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
            if _is_chunk_map_reasoning_prompt(prompt) and "### bad.py ###" in prompt:
                raise LLMSemanticExhaustionError("no content", retry_thinking_level=False)
            return super().complete_json(prompt, **kwargs)

    result = run_coordinator(
        _FailWhenBadPresent(),
        CodeReviewInput(
            files={"bad.py": "def bad(): pass", "good.py": "def good(): pass"},
            task_description="t",
            language="python",
        ),
    )
    # File separation happened: only bad.py degraded (its range is recorded
    # non-blockingly), while good.py was reviewed and is absent from the ranges.
    assert any("bad.py" in r for r in result.not_reviewed_ranges)
    assert not any("good.py" in r for r in result.not_reviewed_ranges)
    # Non-blocking by default: no posted "could not be reviewed" finding, and the
    # reviewed sibling's clean verdict is not rejected by the degraded culprit.
    assert not any("could not be reviewed" in i.description for i in result.issues)
    assert result.approved is True


def test_semantic_exhaustion_without_ladder_still_gets_same_input_retry() -> None:
    """A semantic exhaustion where the client ran NO downgrade ladder
    (retry_thinking_level is None — e.g. thinking was already off) is a stochastic
    empty, not a doomed reasoning loop: the coordinator still gives it one
    same-input retry, recovering a single-file chunk a fast-path degrade would have
    blocked."""

    class _FailOnceNoLadder(DummyLLMClient):
        def __init__(self) -> None:
            super().__init__()
            self._map_reasoning_attempts = 0

        def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
            if _is_chunk_map_reasoning_prompt(prompt):
                self._map_reasoning_attempts += 1
                if self._map_reasoning_attempts == 1:
                    raise LLMSemanticExhaustionError("reasoning only")
            return super().complete_json(prompt, **kwargs)

    client = _FailOnceNoLadder()
    result = run_coordinator(
        client,
        CodeReviewInput(
            files={"only.py": "def only(): pass"},
            task_description="t",
            language="python",
            skip_tail_passes=True,
        ),
    )
    assert result.approved is True
    # Map-phase reasoning attempts only: initial no-ladder exhaustion + retry.
    assert client._map_reasoning_attempts == 2


def test_context_chained_child_failure_is_not_misclassified_as_semantic() -> None:
    """A child truncation raised while recovering a semantically-exhausted multi-file
    chunk must keep its own line-bisect/retry recovery — recovery runs outside the
    parent's ``except`` block, so the parent's exhaustion is never context-chained
    onto the child (which would wrongly fast-path-degrade the truncation)."""

    class _CombinedExhaustsChildTruncatesOnce(DummyLLMClient):
        def __init__(self) -> None:
            super().__init__()
            self.bad_calls = 0

        def complete(self, prompt: str, **kwargs: Any) -> str:
            if not _is_chunk_map_reasoning_prompt(prompt):
                return super().complete(prompt, **kwargs)
            if "### a.py ###" in prompt and "### b.py ###" in prompt:
                raise LLMSemanticExhaustionError("no content", retry_thinking_level=False)
            if "### a.py ###" in prompt and "### b.py ###" not in prompt:
                self.bad_calls += 1
                if self.bad_calls == 1:
                    raise LLMTruncatedError("truncated", finish_reason="length")
            return super().complete(prompt, **kwargs)

        def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
            return super().complete_json(prompt, **kwargs)

    client = _CombinedExhaustsChildTruncatesOnce()
    result = run_coordinator(
        client,
        CodeReviewInput(
            files={"a.py": "def a(): pass", "b.py": "def b(): pass"},
            task_description="t",
            language="python",
        ),
    )
    # a.py recovered via its own retry (not degraded), so the review approves with
    # no "not reviewed" findings — the context-chain misclassification is gone.
    assert result.approved is True
    assert not [i for i in result.issues if "could not be reviewed" in i.description]


def test_persistent_small_chunk_failure_raises_unavailable() -> None:
    """A small chunk that fails its initial call and its retry has no verdict:
    the run must raise, never render approved/rejected on unreviewed code."""
    client = _SelectiveRaiser("def only")
    with pytest.raises(CodeReviewUnavailableError) as excinfo:
        run_coordinator(
            client,
            CodeReviewInput(
                files={"only.py": "def only(): pass"},
                task_description="t",
                language="python",
            ),
        )
    assert len(client.prompts) == 2  # initial + one same-input retry
    assert any("only.py" in r for r in excinfo.value.unreviewed)


def test_partial_terminal_failure_degrades_gracefully_without_blocking(monkeypatch) -> None:
    """One chunk keeps failing while another succeeds: by default the run
    completes and degrades gracefully — the unreviewable chunk is NOT posted as a
    "could not be reviewed" finding and does NOT block (a reviewer-side hiccup is
    not a code defect); its range is surfaced only via ``not_reviewed_ranges``,
    and the sibling chunk drives the approved verdict."""
    monkeypatch.setenv("CODE_REVIEW_MAP_PARALLELISM", "1")
    # Default graceful behavior (opt-out explicitly off, in case the env leaks).
    monkeypatch.delenv("CODE_REVIEW_BLOCK_ON_UNREVIEWED", raising=False)
    llm_probe = DummyLLMClient()
    cap = compute_code_review_map_chunk_chars(llm_probe)

    filler_size = cap - _MAP_CHUNK_SEPARATION_HEADROOM  # forces the two files into separate chunks

    files = {
        "bad.py": "FAILME = True\n" + ("x = 1\n" * 50),
        "good.py": "ok = 1\n".ljust(filler_size, "#"),
    }
    assert len(files["bad.py"]) < 2 * MIN_SPLIT_SEGMENT_CHARS

    client = _SelectiveRaiser("FAILME")
    result = run_coordinator(
        client,
        CodeReviewInput(files=files, task_description="t", language="python"),
    )

    # The run completed without raising and approved (good.py drives the verdict);
    # bad.py is recorded only as a non-blocking not-reviewed range, never posted.
    assert result.approved is True
    assert not any("could not be reviewed" in i.description for i in result.issues)
    assert any("bad.py" in r for r in result.not_reviewed_ranges)
    assert not any("good.py" in r for r in result.not_reviewed_ranges)


def test_fail_closed_pre_numbered_chunk_uses_embedded_line_numbers(monkeypatch) -> None:
    """For a pre-numbered (PR-diff) chunk, a not-reviewed finding must carry the
    real embedded line numbers — not the positional segment indices — so the
    finding anchors to the correct diff lines downstream. Anchoring only matters
    when the finding is actually posted, i.e. under the fail-closed opt-out
    (``CODE_REVIEW_BLOCK_ON_UNREVIEWED``), which this test exercises."""
    monkeypatch.setenv("CODE_REVIEW_MAP_PARALLELISM", "1")
    monkeypatch.setenv("CODE_REVIEW_BLOCK_ON_UNREVIEWED", "true")
    # Embedded original lines 4000-4004 carry the marker; positional indices
    # for this segment would be 1-5, which must NOT leak into the finding.
    bad = "\n".join(f"{4000 + i}: FAILME_{i}()" for i in range(5))
    files = {"bad.py": bad, "good.py": "100: ok()\n101: also_ok()"}

    client = _SelectiveRaiser("FAILME")
    result = run_coordinator(
        client,
        CodeReviewInput(files=files, pre_numbered=True, task_description="t", language="python"),
    )

    not_reviewed = [
        i
        for i in result.issues
        if "could not be reviewed" in i.description and i.file_path == "bad.py"
    ]
    assert not_reviewed, "the failed pre-numbered chunk must surface a not-reviewed finding"
    assert not_reviewed[0].start_line == 4000
    assert not_reviewed[0].line == 4004


def test_degraded_finding_does_not_leak_raw_exception_text_default(monkeypatch) -> None:
    """On the default (graceful) path, a chunk that fails with a parse error that
    embeds raw model output must not leak that text anywhere observable: not in
    the merged summary and not in the non-blocking ``not_reviewed_ranges``."""
    monkeypatch.setenv("CODE_REVIEW_MAP_PARALLELISM", "1")
    monkeypatch.delenv("CODE_REVIEW_BLOCK_ON_UNREVIEWED", raising=False)
    secret = "leaked_password = 'hunter2'"
    leaky = LLMJsonParseError(
        f"Could not parse structured JSON. Response preview: '{secret}'...",
        response_preview=secret,
    )
    files = {
        "bad.py": "FAILME = True\n" + ("x = 1\n" * 50),
        "good.py": "ok = 1",
    }
    assert len(files["bad.py"]) < 2 * MIN_SPLIT_SEGMENT_CHARS

    result = run_coordinator(
        _SelectiveRaiser("FAILME", exc=leaky),
        CodeReviewInput(files=files, task_description="t", language="python"),
    )

    # bad.py is recorded as a not-reviewed range (non-blocking), never posted.
    assert any("bad.py" in r for r in result.not_reviewed_ranges)
    assert not any("could not be reviewed" in i.description for i in result.issues)
    # Neither the summary nor the range labels carry the raw model output.
    assert secret not in result.summary
    assert secret not in " ".join(result.not_reviewed_ranges)


def test_degraded_finding_does_not_leak_raw_exception_text_when_blocking(monkeypatch) -> None:
    """Under the fail-closed opt-out, the posted not-reviewed finding names only
    the failure class — never ``str(exc)`` — so parse/schema errors that embed
    raw model output (response previews) can never reach a PR comment."""
    monkeypatch.setenv("CODE_REVIEW_MAP_PARALLELISM", "1")
    monkeypatch.setenv("CODE_REVIEW_BLOCK_ON_UNREVIEWED", "true")
    secret = "leaked_password = 'hunter2'"
    leaky = LLMJsonParseError(
        f"Could not parse structured JSON. Response preview: '{secret}'...",
        response_preview=secret,
    )
    files = {
        "bad.py": "FAILME = True\n" + ("x = 1\n" * 50),
        "good.py": "ok = 1",
    }
    assert len(files["bad.py"]) < 2 * MIN_SPLIT_SEGMENT_CHARS

    result = run_coordinator(
        _SelectiveRaiser("FAILME", exc=leaky),
        CodeReviewInput(files=files, task_description="t", language="python"),
    )

    not_reviewed = [i for i in result.issues if "could not be reviewed" in i.description]
    assert not_reviewed, "the failed chunk must surface a not-reviewed finding when blocking"
    finding = not_reviewed[0]
    # The failure class is named (useful diagnostic); the raw message is not.
    assert "LLMJsonParseError" in finding.description
    assert secret not in finding.description
    assert "Response preview" not in finding.description
    # The merged summary is likewise sanitized.
    assert secret not in result.summary


def test_unexpected_chunk_exception_fails_closed() -> None:
    """A non-LLM exception (a bug in the reviewer code, e.g. KeyError) is NOT a
    known content failure: it must propagate unchanged — never be retried,
    bisected, or masked as a not-reviewed finding — so the defect surfaces."""
    client = _SelectiveRaiser("def only", exc=KeyError("reviewer bug"))
    with pytest.raises(KeyError):
        run_coordinator(
            client,
            CodeReviewInput(
                files={"only.py": "def only(): pass"},
                task_description="t",
                language="python",
            ),
        )
    # Exactly one call: no retry/bisect for an unexpected error.
    assert len(client.prompts) == 1


def test_is_content_failure_classifies_model_output_errors_only() -> None:
    """Known model-output failures (``LLMJsonParseError`` from the client,
    a raw ``json.JSONDecodeError`` in the exception chain,
    ``LLMSemanticExhaustionError``, ``LLMTruncatedError``, and
    ``LLMSchemaValidationError``) are recoverable content failures;
    reviewer-code bugs are not."""
    assert _is_content_failure(LLMJsonParseError("bad")) is True
    assert _is_content_failure(LLMSemanticExhaustionError("empty")) is True
    assert _is_content_failure(json.JSONDecodeError("Expecting value", "not json", 0)) is True
    # A token-limit truncation is recoverable: a smaller chunk yields a smaller review.
    assert _is_content_failure(LLMTruncatedError("truncated", finish_reason="length")) is True
    # A schema-invalid (but parseable) reply is recoverable the same way a
    # parse failure is -- the chunk reviewer's complete_validated call raises
    # this after exhausting its corrective retry.
    assert _is_content_failure(LLMSchemaValidationError("schema invalid")) is True
    # A JSONDecodeError wrapped by strands must still be recognised via the chain.
    wrapped = RuntimeError("agent failed")
    wrapped.__cause__ = json.JSONDecodeError("Expecting value", "x", 0)
    assert _is_content_failure(wrapped) is True
    # Reviewer-code bugs fail closed.
    assert _is_content_failure(KeyError("bug")) is False
    assert _is_content_failure(TypeError("bug")) is False


def test_chain_has_empty_types_returns_false_without_raising() -> None:
    """Empty ``types`` must return False (never raise); non-empty still matches the chain."""
    from code_review_agent.mapping import _chain_has

    assert _chain_has(ValueError("x"), ()) is False
    assert _chain_has(ValueError("x"), (ValueError,)) is True
    wrapped = RuntimeError("outer")
    wrapped.__cause__ = TypeError("inner")
    assert _chain_has(wrapped, (TypeError,)) is True


def test_raw_json_decode_failure_degrades_not_fails_closed(monkeypatch) -> None:
    """A raw ``json.JSONDecodeError`` in the exception chain (e.g. wrapped by
    the injected client or a lower layer, rather than the client's own
    ``LLMJsonParseError``) is still recoverable model output, so it must take
    the degrade path (run completes, graceful) — not fail closed like a
    reviewer-code bug. By default the range is non-blocking."""
    monkeypatch.setenv("CODE_REVIEW_MAP_PARALLELISM", "1")
    monkeypatch.delenv("CODE_REVIEW_BLOCK_ON_UNREVIEWED", raising=False)
    llm_probe = DummyLLMClient()
    cap = compute_code_review_map_chunk_chars(llm_probe)

    filler_size = cap - _MAP_CHUNK_SEPARATION_HEADROOM

    files = {
        "bad.py": "FAILME = True\n" + ("x = 1\n" * 50),
        "good.py": "ok = 1\n".ljust(filler_size, "#"),
    }
    bad_json = json.JSONDecodeError("Expecting value", "not json", 0)
    result = run_coordinator(
        _SelectiveRaiser("FAILME", exc=bad_json),
        CodeReviewInput(files=files, task_description="t", language="python"),
    )
    # Completed (no exception); bad.py degrades to a non-blocking not-reviewed range.
    assert result.approved is True
    assert not any("could not be reviewed" in i.description for i in result.issues)
    assert any("bad.py" in r for r in result.not_reviewed_ranges)


def test_truncated_chunk_review_degrades_not_fails_closed(monkeypatch) -> None:
    """A chunk whose review response hits the output-token limit
    (``LLMTruncatedError``, finish_reason=length) is recoverable model output:
    it must take the degrade path (bisect/retry, then graceful degradation, run
    completes) rather than aborting the whole review job with an unexpected
    exception. Regression test for a real @khala review run that failed with
    'code review failed: Response truncated due to token limit
    (finish_reason=length)'."""
    monkeypatch.setenv("CODE_REVIEW_MAP_PARALLELISM", "1")
    monkeypatch.delenv("CODE_REVIEW_BLOCK_ON_UNREVIEWED", raising=False)
    llm_probe = DummyLLMClient()
    cap = compute_code_review_map_chunk_chars(llm_probe)

    filler_size = cap - _MAP_CHUNK_SEPARATION_HEADROOM

    files = {
        "bad.py": "FAILME = True\n" + ("x = 1\n" * 50),
        "good.py": "ok = 1\n".ljust(filler_size, "#"),
    }
    truncated = LLMTruncatedError(
        "Response truncated due to token limit (finish_reason=length)",
        partial_content='{"issues": [',
        finish_reason="length",
    )
    result = run_coordinator(
        _SelectiveRaiser("FAILME", exc=truncated),
        CodeReviewInput(files=files, task_description="t", language="python"),
    )
    # Completed (no exception); bad.py degrades to a non-blocking not-reviewed range.
    assert result.approved is True
    assert not any("could not be reviewed" in i.description for i in result.issues)
    assert any("bad.py" in r for r in result.not_reviewed_ranges)


def test_not_reviewed_ranges_populated_and_not_in_issues(monkeypatch) -> None:
    """A degraded chunk populates ``not_reviewed_ranges`` (observability) while
    contributing nothing to ``issues`` on the default (graceful) path."""
    monkeypatch.setenv("CODE_REVIEW_MAP_PARALLELISM", "1")
    monkeypatch.delenv("CODE_REVIEW_BLOCK_ON_UNREVIEWED", raising=False)
    llm_probe = DummyLLMClient()
    cap = compute_code_review_map_chunk_chars(llm_probe)

    filler_size = cap - _MAP_CHUNK_SEPARATION_HEADROOM

    files = {
        "bad.py": "FAILME = True\n" + ("x = 1\n" * 50),
        "good.py": "ok = 1\n".ljust(filler_size, "#"),
    }
    result = run_coordinator(
        _SelectiveRaiser("FAILME"),
        CodeReviewInput(files=files, task_description="t", language="python"),
    )
    assert result.not_reviewed_ranges == ["bad.py (lines 1-51)"]
    assert result.issues == []


def test_block_on_unreviewed_env_restores_fail_closed(monkeypatch) -> None:
    """With ``CODE_REVIEW_BLOCK_ON_UNREVIEWED`` set, a partial degrade restores the
    legacy fail-closed behavior: the unreviewable chunk becomes a blocking ``high``
    finding in ``issues`` and rejects the merged review."""
    monkeypatch.setenv("CODE_REVIEW_MAP_PARALLELISM", "1")
    monkeypatch.setenv("CODE_REVIEW_BLOCK_ON_UNREVIEWED", "true")
    llm_probe = DummyLLMClient()
    cap = compute_code_review_map_chunk_chars(llm_probe)

    filler_size = cap - _MAP_CHUNK_SEPARATION_HEADROOM

    files = {
        "bad.py": "FAILME = True\n" + ("x = 1\n" * 50),
        "good.py": "ok = 1\n".ljust(filler_size, "#"),
    }
    result = run_coordinator(
        _SelectiveRaiser("FAILME"),
        CodeReviewInput(files=files, task_description="t", language="python"),
    )
    assert result.approved is False
    not_reviewed = [i for i in result.issues if "could not be reviewed" in i.description]
    assert len(not_reviewed) == 1
    assert not_reviewed[0].severity == "high"
    assert not_reviewed[0].file_path == "bad.py"
    # The range is still surfaced for observability even under the opt-out.
    assert any("bad.py" in r for r in result.not_reviewed_ranges)


def test_total_failure_still_raises_even_with_graceful_default(monkeypatch) -> None:
    """When NO chunk can be reviewed, the run produced no verdict at all — the
    total-failure guard must still raise ``CodeReviewUnavailableError`` even though
    partial degradation is now non-blocking by default."""
    monkeypatch.setenv("CODE_REVIEW_MAP_PARALLELISM", "1")
    monkeypatch.delenv("CODE_REVIEW_BLOCK_ON_UNREVIEWED", raising=False)
    llm_probe = DummyLLMClient()
    cap = compute_code_review_map_chunk_chars(llm_probe)

    filler_size = cap - _MAP_CHUNK_SEPARATION_HEADROOM

    # Two separate chunks, BOTH carrying the failure marker → nothing reviewed.
    files = {
        "bad1.py": "FAILME = True\n" + ("x = 1\n" * 20),
        "bad2.py": "FAILME = True\n".ljust(filler_size, "#"),
    }
    client = _SelectiveRaiser("FAILME")
    with pytest.raises(CodeReviewUnavailableError) as excinfo:
        run_coordinator(
            client,
            CodeReviewInput(files=files, task_description="t", language="python"),
        )
    unreviewed = " ".join(excinfo.value.unreviewed)
    assert "bad1.py" in unreviewed and "bad2.py" in unreviewed


def test_total_failure_unreviewed_excludes_genuine_issue_descriptions(monkeypatch) -> None:
    """``CodeReviewUnavailableError.unreviewed`` must name only not-reviewed
    ranges, never genuine reviewer findings -- even if a ``_ChunkOutcome``
    somehow carried both (violating its own invariant that a failed chunk
    never contributes to ``issues``). Regression guard for the coordinator
    once concatenating ``outcome.issues`` into ``unreviewed``."""
    import code_review_agent.coordinator as coord
    from code_review_agent import mapping

    genuine = CodeReviewIssue(
        severity="high",
        category="logic",
        file_path="reviewed.py",
        description="a genuine finding a chunk actually reviewed",
        suggestion="fix it",
    )
    not_reviewed = CodeReviewIssue(
        severity="high",
        category="general",
        file_path="unreviewed.py",
        description="reviewed.py (lines 1-2) could not be reviewed automatically",
        suggestion="",
    )
    # approved_flags is deliberately empty (no chunk succeeded) while issues is
    # non-empty, to prove the coordinator's own construction of ``unreviewed``
    # never leaks genuine findings, independent of whether mapping's own
    # invariant always holds elsewhere.
    forced_outcome = mapping._ChunkOutcome(
        issues=[genuine], not_reviewed_issues=[not_reviewed], approved_flags=[]
    )
    monkeypatch.setattr(coord, "_map_chunks", lambda *args, **kwargs: [forced_outcome])

    with pytest.raises(CodeReviewUnavailableError) as excinfo:
        run_coordinator(
            DummyLLMClient(),
            CodeReviewInput(files={"a.py": "x = 1\n"}, task_description="t", language="python"),
        )
    assert excinfo.value.unreviewed == [not_reviewed.description]
    assert genuine.description not in excinfo.value.unreviewed


# ---------------------------------------------------------------------------
# Last-resort thinking-off retry (make semantic exhaustion rare)
# ---------------------------------------------------------------------------


class _ThinkAwareReviewer:
    """A stand-in ``ChunkReviewAgent`` whose ``run`` reacts to the ``think`` kwarg.

    Records every ``think`` value passed. Raises ``fail_exc`` unless
    ``recover_on_think_off`` and ``think is False``, in which case it returns a
    clean approved output. ``.llm`` is a plain object so
    ``thinking_override_supported`` can be monkeypatched to gate the retry on.
    """

    def __init__(self, fail_exc: Exception, recover_on_think_off: bool = True) -> None:
        self.llm = object()
        self.fail_exc = fail_exc
        self.recover_on_think_off = recover_on_think_off
        self.think_calls: list[Any] = []

    def run(self, chunk_input: Any, think: Any = None) -> ChunkReviewOutput:
        self.think_calls.append(think)
        if self.recover_on_think_off and think is False:
            return ChunkReviewOutput(approved=True, issues=[], summary="ok (thinking off)")
        raise self.fail_exc


def _tiny_chunk() -> ReviewChunk:
    return ReviewChunk(
        segments=[FileSegment(path="b.py", content="x = 1\n", start_line=1, total_lines=1)]
    )


def test_thinking_off_retry_recovers_semantic_exhaustion(monkeypatch) -> None:
    """A terminal chunk that keeps exhausting under default thinking is recovered
    by the last-resort thinking-off retry, producing a real review (no
    not-reviewed range). The retry is normally skipped for injected strands
    models, so force the production-path gate on to exercise it."""
    monkeypatch.setenv("CODE_REVIEW_THINKING_OFF_RETRY", "true")
    monkeypatch.setattr(mapping, "thinking_override_supported", lambda llm: True)

    reviewer = _ThinkAwareReviewer(LLMSemanticExhaustionError("reasoning only"))
    outcome = mapping._review_chunk_with_recovery(
        reviewer, _tiny_chunk(), {"language": "python", "task_description": "t"}
    )

    assert outcome.approved_flags == [True]
    assert not outcome.not_reviewed_issues
    assert outcome.degraded_recovery is True  # reduced-fidelity → excluded from cache
    # initial + one same-input retry (both default thinking), then thinking-off.
    assert reviewer.think_calls == [None, None, False]


def test_thinking_off_retry_that_also_fails_degrades(monkeypatch) -> None:
    """When the thinking-off retry ALSO returns a content failure, the chunk
    degrades to a not-reviewed outcome rather than raising."""
    monkeypatch.setenv("CODE_REVIEW_THINKING_OFF_RETRY", "true")
    monkeypatch.setattr(mapping, "thinking_override_supported", lambda llm: True)
    reviewer = _ThinkAwareReviewer(
        LLMSemanticExhaustionError("still nothing"), recover_on_think_off=False
    )
    outcome = mapping._review_chunk_with_recovery(
        reviewer, _tiny_chunk(), {"language": "python", "task_description": "t"}
    )
    assert outcome.approved_flags == []
    assert outcome.not_reviewed_issues  # degraded
    assert reviewer.think_calls == [None, None, False]


def test_thinking_off_retry_infra_failure_raises_unavailable(monkeypatch) -> None:
    """An infrastructure failure DURING the thinking-off retry surfaces as
    ``CodeReviewUnavailableError`` (not a silent degrade)."""
    from code_review_agent import mapping

    monkeypatch.setattr(mapping, "thinking_override_supported", lambda llm: True)

    class _InfraOnThinkOff(_ThinkAwareReviewer):
        def run(self, chunk_input: Any, think: Any = None) -> ChunkReviewOutput:
            self.think_calls.append(think)
            if think is False:
                raise LLMRateLimitError("429 during thinking-off retry")
            raise LLMSemanticExhaustionError("reasoning only")

    reviewer = _InfraOnThinkOff(LLMSemanticExhaustionError("x"))
    with pytest.raises(CodeReviewUnavailableError):
        mapping._review_chunk_with_recovery(
            reviewer, _tiny_chunk(), {"language": "python", "task_description": "t"}
        )


def test_thinking_off_retry_non_infra_error_degrades_best_effort(monkeypatch) -> None:
    """A non-infra error during the best-effort thinking-off retry does not fail
    the whole run: the chunk degrades on its original content failure (a genuine
    reviewer-code bug would already have failed closed on the first attempt,
    before this last-resort retry)."""
    from code_review_agent import mapping

    monkeypatch.setattr(mapping, "thinking_override_supported", lambda llm: True)

    class _BugOnThinkOff(_ThinkAwareReviewer):
        def run(self, chunk_input: Any, think: Any = None) -> ChunkReviewOutput:
            self.think_calls.append(think)
            if think is False:
                raise KeyError("unexpected during thinking-off retry")
            raise LLMSemanticExhaustionError("reasoning only")

    reviewer = _BugOnThinkOff(LLMSemanticExhaustionError("x"))
    outcome = mapping._review_chunk_with_recovery(
        reviewer, _tiny_chunk(), {"language": "python", "task_description": "t"}
    )
    assert outcome.not_reviewed_issues  # degraded rather than raising
    assert reviewer.think_calls == [None, None, False]


def test_thinking_off_retry_disabled_by_env(monkeypatch) -> None:
    """With ``CODE_REVIEW_THINKING_OFF_RETRY`` off, a terminal exhaustion degrades
    without attempting a thinking-off retry (no ``think=False`` call)."""
    from code_review_agent import mapping

    monkeypatch.setenv("CODE_REVIEW_THINKING_OFF_RETRY", "false")
    monkeypatch.setattr(mapping, "thinking_override_supported", lambda llm: True)
    reviewer = _ThinkAwareReviewer(LLMSemanticExhaustionError("reasoning only"))
    outcome = mapping._review_chunk_with_recovery(
        reviewer, _tiny_chunk(), {"language": "python", "task_description": "t"}
    )
    assert outcome.not_reviewed_issues  # degraded
    assert False not in reviewer.think_calls  # never attempted thinking-off


def test_thinking_off_retry_only_for_exhaustion_and_truncation(monkeypatch) -> None:
    """A JSON parse failure is a content failure but not one a thinking-off pass
    fixes, so the retry does not fire for it (it degrades directly)."""
    from code_review_agent import mapping

    monkeypatch.setattr(mapping, "thinking_override_supported", lambda llm: True)
    reviewer = _ThinkAwareReviewer(LLMJsonParseError("bad json", response_preview="x"))
    outcome = mapping._review_chunk_with_recovery(
        reviewer, _tiny_chunk(), {"language": "python", "task_description": "t"}
    )
    assert outcome.not_reviewed_issues  # degraded
    assert False not in reviewer.think_calls  # parse errors don't trigger thinking-off


def test_resolve_code_review_model_think_override(monkeypatch) -> None:
    """``resolve_code_review_model`` forwards ``think`` to ``get_strands_model`` on
    the production path, omits it when None, and returns an injected strands model
    unchanged (its thinking level can't be re-resolved)."""
    from code_review_agent import model_resolution
    from code_review_agent.model_resolution import (
        resolve_code_review_model,
        thinking_override_supported,
    )

    captured: dict[str, Any] = {}

    def _fake_get(agent_key: str, **kw: Any) -> str:
        captured["agent_key"] = agent_key
        captured["think"] = kw.get("think", "OMITTED")
        return "MODEL"

    monkeypatch.setattr(model_resolution, "get_strands_model", _fake_get)

    assert resolve_code_review_model(object(), think=False) == "MODEL"
    assert captured == {"agent_key": "code_review", "think": False}
    captured.clear()
    assert resolve_code_review_model(object()) == "MODEL"
    assert captured == {"agent_key": "code_review", "think": "OMITTED"}  # think not passed

    dummy = DummyLLMClient()  # a strands Model — returned unchanged, think ignored
    assert resolve_code_review_model(dummy, think=False) is dummy
    assert thinking_override_supported(dummy) is False
    assert thinking_override_supported(object()) is True


def test_resolve_code_review_model_think_off_uses_real_factory(monkeypatch) -> None:
    """Regression: the production thinking-off path must not raise. It calls the
    real ``llm_service.get_strands_model(..., think=False)`` — an earlier version
    of that export did not accept ``think``, so this raised ``TypeError`` that the
    retry swallowed, silently disabling the last-resort retry in production. Uses
    ``LLM_PROVIDER=dummy`` so a real ``LLMClientModel`` is built without a
    configured provider, and does NOT monkeypatch ``get_strands_model`` so the
    real signature is exercised."""
    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    from code_review_agent.model_resolution import resolve_code_review_model

    # ``object()`` is not a strands Model, so this takes the production
    # ``get_strands_model`` path (the one that was broken).
    model = resolve_code_review_model(object(), think=False)
    assert model.get_config().get("think") is False
    # The default (think=None) path stays on the provider default.
    assert resolve_code_review_model(object()).get_config().get("think") is None


def test_compact_for_review_rejects_negative_max_chars() -> None:
    """The documented non-negative budget is enforced, not left to slice quirks."""
    from llm_service.clients.dummy import DummyLLMClient

    with pytest.raises(ValueError, match="non-negative"):
        _compact_for_review("text", -1, DummyLLMClient(), "spec")


def test_compact_for_review_uses_provider_start_timestamps(monkeypatch) -> None:
    """Continuation callbacks share one outer complete(); each transcript
    entry must keep that turn's provider-recorded start, not the callback time."""
    from llm_service.clients.dummy import DummyLLMClient
    from llm_service.interface import record_complete_json_turn, reset_complete_json_observer_state

    reset_complete_json_observer_state()
    captured: list = []
    monkeypatch.setattr(
        "code_review_agent.coordinator.record_transcript_entry",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )

    class _ContinuationClient(DummyLLMClient):
        def complete(self, prompt: str, **kwargs: Any) -> str:
            record_complete_json_turn("first prompt", "PARTIAL", started_monotonic=10.0)
            record_complete_json_turn("continuation messages", " REST", started_monotonic=20.0)
            return "PARTIAL REST"

    _compact_for_review("x" * 200, 50, _ContinuationClient(), "spec")
    assert [kwargs["started_monotonic"] for _, kwargs in captured] == [10.0, 20.0]
    assert [args[3] for args, _ in captured] == ["PARTIAL", " REST"]


def test_not_reviewed_range_label_edge_cases() -> None:
    """The observability label handles a missing path and a missing line range."""
    from code_review_agent.coordinator import _not_reviewed_range_label
    from code_review_agent.models import CodeReviewIssue

    assert (
        _not_reviewed_range_label(
            CodeReviewIssue(file_path="a.py", start_line=3, line=9, description="")
        )
        == "a.py (lines 3-9)"
    )
    # No line range → just the path.
    assert _not_reviewed_range_label(CodeReviewIssue(file_path="a.py", description="")) == "a.py"
    # Headerless finding with no path.
    assert _not_reviewed_range_label(CodeReviewIssue(file_path="", description="")) == "(unknown)"


def test_all_empty_files_completes_with_info_findings_not_raise() -> None:
    """A submission of only empty files creates no chunks, so it must complete
    via the no-code early return (info findings, approved) — the total-failure
    guard must not fire when there was simply nothing to review."""
    result = run_coordinator(
        DummyLLMClient(),
        CodeReviewInput(
            files={"a.py": "", "b.py": "   \n\t"}, task_description="t", language="python"
        ),
    )
    assert result.approved is True
    assert {i.file_path for i in result.issues} == {"a.py", "b.py"}
    assert all(i.severity == "info" for i in result.issues)


def test_infra_failure_fails_fast_without_retry_or_bisect() -> None:
    """Rate-limit/unreachable/auth failures can't be fixed by smaller chunks:
    exactly one map call, then CodeReviewUnavailableError."""
    client = _SelectiveRaiser("def", exc=LLMRateLimitError("429"))
    with pytest.raises(CodeReviewUnavailableError):
        run_coordinator(
            client,
            CodeReviewInput(
                files={"only.py": "def only(): pass"},
                task_description="t",
                language="python",
            ),
        )
    # Exactly one map call: infra failures fail fast with no retry/bisect.
    assert len(client.prompts) == 1


def test_infra_failure_is_detected_through_exception_chain() -> None:
    """Strands may wrap the client error; classification must walk the chain."""
    wrapped = RuntimeError("agent invocation failed")
    wrapped.__cause__ = LLMRateLimitError("429")
    client = _SelectiveRaiser("def", exc=wrapped)
    with pytest.raises(CodeReviewUnavailableError):
        run_coordinator(
            client,
            CodeReviewInput(
                files={"only.py": "def only(): pass"},
                task_description="t",
                language="python",
            ),
        )
    # Exactly one map call: chain-walked infra failure also fails fast.
    assert len(client.prompts) == 1


def test_large_failing_file_bisects_then_raises_with_ranges() -> None:
    """A single big segment that keeps failing bisects by lines until the
    floor, then the run raises naming an unreviewed range."""
    # One chunk (below the map cap) but above the bisect floor, and every half
    # still carries the failure marker. Size to the middle of that window from
    # the live budget so the test stays valid if the map budget shifts (e.g. the
    # sibling-surface reservation).
    budget = compute_code_review_map_chunk_chars(DummyLLMClient())
    content = _failme_content_in_bisect_window(budget)
    client = _SelectiveRaiser("FAILME")
    with pytest.raises(CodeReviewUnavailableError) as excinfo:
        run_coordinator(
            client,
            CodeReviewInput(files={"big.py": content}, task_description="t", language="python"),
        )
    assert len(client.prompts) >= 2  # at least one bisect retry happened
    assert any("big.py" in r for r in excinfo.value.unreviewed)


def test_failing_single_giant_line_retries_once_then_raises() -> None:
    """A single line above the bisect floor cannot split (line boundaries are
    never broken); it gets the same-input retry, then the run raises."""
    client = _SelectiveRaiser("FAILME")
    with pytest.raises(CodeReviewUnavailableError):
        run_coordinator(
            client,
            CodeReviewInput(
                files={"min.js": "FAILME;" + ("x" * 20_000)},
                task_description="t",
                language="typescript",
            ),
        )
    assert len(client.prompts) == 2  # initial + same-input retry; no bisect possible


def test_parallel_map_failure_cancels_and_raises() -> None:
    """With multiple chunks reviewed concurrently, a terminal failure must
    surface as CodeReviewUnavailableError (pending work is cancelled, no
    partial verdict is rendered)."""
    llm_probe = DummyLLMClient()
    cap = compute_code_review_map_chunk_chars(llm_probe)
    files = {
        "a.py": "FAILME = 1\n".ljust(cap - _MAP_CHUNK_SEPARATION_HEADROOM, "#"),
        "b.py": "FAILME = 2\n".ljust(cap - _MAP_CHUNK_SEPARATION_HEADROOM, "#"),
        "c.py": "FAILME = 3\n".ljust(cap - _MAP_CHUNK_SEPARATION_HEADROOM, "#"),
    }
    client = _SelectiveRaiser("FAILME", exc=LLMRateLimitError("429"))
    with pytest.raises(CodeReviewUnavailableError):
        run_coordinator(
            client,
            CodeReviewInput(files=files, task_description="t", language="python"),
        )


@pytest.mark.parametrize("fail_first", [True, False])
def test_parallel_map_failure_does_not_wait_for_inflight_reviews(fail_first: bool) -> None:
    """A fast infra failure must propagate immediately regardless of where the
    failing chunk sits in submission order: completions are observed as they
    happen (never joined in submission order), pending chunks are cancelled,
    and in-flight reviews are left to finish in the background — so the
    failure is never blocked behind another chunk's model timeout."""
    release = threading.Event()

    class _OneFailsOneBlocks(DummyLLMClient):
        def __init__(self) -> None:
            super().__init__()
            self.slow_finished = False

        def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
            if "FAILME" in prompt:
                raise LLMRateLimitError("429")
            if _is_chunk_map_reasoning_prompt(prompt):
                release.wait(timeout=10)
                self.slow_finished = True
            return super().complete_json(prompt, **kwargs)

    llm_probe = DummyLLMClient()
    cap = compute_code_review_map_chunk_chars(llm_probe)
    contents = {
        "fast_fail.py": "FAILME = 1\n".ljust(cap - _MAP_CHUNK_SEPARATION_HEADROOM, "#"),
        "slow.py": "ok = 1\n".ljust(cap - _MAP_CHUNK_SEPARATION_HEADROOM, "#"),
    }
    order = ["fast_fail.py", "slow.py"] if fail_first else ["slow.py", "fast_fail.py"]
    files = {name: contents[name] for name in order}
    client = _OneFailsOneBlocks()
    try:
        with pytest.raises(CodeReviewUnavailableError):
            run_coordinator(
                client,
                CodeReviewInput(files=files, task_description="t", language="python"),
            )
        # The failure surfaced while the other chunk's review was still
        # in flight — the coordinator did not block waiting for it.
        assert client.slow_finished is False
    finally:
        release.set()


def test_sequential_map_failure_does_not_start_later_chunk(monkeypatch) -> None:
    """Under CODE_REVIEW_MAP_PARALLELISM=1 (the documented sequential mode), a
    first-chunk infrastructure failure aborts immediately and the later chunk's
    review is never started — no extra LLM call fires past fail-fast (a 1-worker
    pool could otherwise dequeue the next chunk before cancellation)."""
    monkeypatch.setenv("CODE_REVIEW_MAP_PARALLELISM", "1")

    class _FailFirstRecordLater(DummyLLMClient):
        def __init__(self) -> None:
            super().__init__()
            self.saw_second = False

        def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
            if "FAILME" in prompt:
                raise LLMRateLimitError("429")
            if _is_chunk_map_reasoning_prompt(prompt) and "SECONDCHUNK" in prompt:
                self.saw_second = True
            return super().complete_json(prompt, **kwargs)

    llm_probe = DummyLLMClient()
    cap = compute_code_review_map_chunk_chars(llm_probe)
    # One ~full chunk per file, in insertion order, so the failing chunk is first.
    files = {
        "a_fail.py": "FAILME = 1\n".ljust(cap - _MAP_CHUNK_SEPARATION_HEADROOM, "#"),
        "b_second.py": "SECONDCHUNK = 1\n".ljust(cap - _MAP_CHUNK_SEPARATION_HEADROOM, "#"),
    }
    client = _FailFirstRecordLater()
    with pytest.raises(CodeReviewUnavailableError):
        run_coordinator(
            client,
            CodeReviewInput(files=files, task_description="t", language="python"),
        )
    assert client.saw_second is False, "later chunk must not be reviewed after fail-fast"


def test_map_parallelism_clamped_by_llm_max_concurrency(monkeypatch) -> None:
    """The configured CODE_REVIEW_MAP_PARALLELISM ceiling is clamped by
    LLM_MAX_CONCURRENCY, so a wide ceiling never yields a width above the
    process-global gate."""
    monkeypatch.setenv("CODE_REVIEW_MAP_PARALLELISM", "20")
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "2")
    assert _map_parallelism() == 2

    monkeypatch.setenv("CODE_REVIEW_MAP_PARALLELISM", "3")
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "10")
    assert _map_parallelism() == 3

    monkeypatch.delenv("CODE_REVIEW_MAP_PARALLELISM", raising=False)
    monkeypatch.delenv("LLM_MAX_CONCURRENCY", raising=False)
    assert (
        _map_parallelism() == 8
    )  # default ceiling (16) clamped by default LLM_MAX_CONCURRENCY (8)


def test_map_phase_peak_concurrency_bounded_by_llm_max_concurrency(monkeypatch) -> None:
    """End-to-end: with a high CODE_REVIEW_MAP_PARALLELISM ceiling but a low
    LLM_MAX_CONCURRENCY, the map phase never runs more concurrent chunk
    reviews than the global gate allows, even with more chunks available
    than that gate."""
    monkeypatch.setenv("CODE_REVIEW_MAP_PARALLELISM", "20")
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "2")

    lock = threading.Lock()
    state = {"current": 0, "peak": 0}
    release = threading.Event()

    class _ConcurrencyProbe(DummyLLMClient):
        def complete(self, prompt: str, **kwargs: Any) -> str:
            if _is_chunk_map_reasoning_prompt(prompt):
                with lock:
                    state["current"] += 1
                    state["peak"] = max(state["peak"], state["current"])
                release.wait(timeout=5)
                with lock:
                    state["current"] -= 1
            return super().complete(prompt, **kwargs)

        def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
            return super().complete_json(prompt, **kwargs)

    def _release_soon() -> None:
        time.sleep(0.2)
        release.set()

    llm_probe = DummyLLMClient()
    cap = compute_code_review_map_chunk_chars(llm_probe)

    files = {
        f"f{i}.py": f"x = {i}\n".ljust(cap - _MAP_CHUNK_SEPARATION_HEADROOM, "#") for i in range(5)
    }

    client = _ConcurrencyProbe()
    threading.Thread(target=_release_soon, daemon=True).start()
    try:
        run_coordinator(
            client, CodeReviewInput(files=files, task_description="t", language="python")
        )
    finally:
        release.set()
    assert state["peak"] <= 2


def test_small_diff_does_not_over_provision_workers(monkeypatch) -> None:
    """With a high ceiling and high LLM_MAX_CONCURRENCY, a diff with fewer
    chunks than either limit still uses no more workers than it has chunks."""
    monkeypatch.setenv("CODE_REVIEW_MAP_PARALLELISM", "16")
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "16")

    lock = threading.Lock()
    state = {"current": 0, "peak": 0}

    class _ConcurrencyProbe(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
            if _is_chunk_map_reasoning_prompt(prompt):
                with lock:
                    state["current"] += 1
                    state["peak"] = max(state["peak"], state["current"])
                time.sleep(0.05)
                with lock:
                    state["current"] -= 1
            return super().complete_json(prompt, **kwargs)

    llm_probe = DummyLLMClient()
    cap = compute_code_review_map_chunk_chars(llm_probe)

    files = {
        f"f{i}.py": f"x = {i}\n".ljust(cap - _MAP_CHUNK_SEPARATION_HEADROOM, "#") for i in range(3)
    }

    client = _ConcurrencyProbe()
    run_coordinator(client, CodeReviewInput(files=files, task_description="t", language="python"))
    assert 2 <= state["peak"] <= 3


def test_run_wide_limiter_caps_concurrent_bisection_across_top_level_chunks(monkeypatch) -> None:
    """Two top-level chunks that bisect AT THE SAME TIME must not push the
    total number of concurrent ``reviewer.run()`` calls above
    CODE_REVIEW_MAP_PARALLELISM: each top-level worker's bisection halves
    share one review-run-scoped limiter with the outer fan-out and with every
    other chunk's bisection, rather than each spinning up its own independent
    2-worker budget on top of whatever else is in flight. With 2 top-level
    chunks (workers=2) each bisecting into 2 concurrent halves, an unbounded
    (pre-fix) run could reach 4 concurrent chunk-review calls even though the
    ceiling here is 2.
    """
    monkeypatch.setenv("CODE_REVIEW_MAP_PARALLELISM", "2")
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "10")  # not the binding constraint here

    budget = compute_code_review_map_chunk_chars(DummyLLMClient())
    # Each file sized into the bisect window but comfortably more than half the
    # map-chunk budget, so the two files never pack into one top-level chunk
    # (build_review_chunks flushes a new chunk once the running total would
    # exceed the cap) -- two independent, simultaneously-bisecting chunks.
    content_a = _failme_content_in_bisect_window(budget)
    content_b = _failme_content_in_bisect_window(budget)

    release = threading.Event()
    delegate = _MultiFileFirstCallFailsDelegate(["big_a.py", "big_b.py"], release)
    stand_in = _NonDummyLLMClient(delegate)

    def _release_soon() -> None:
        time.sleep(0.3)
        release.set()

    threading.Thread(target=_release_soon, daemon=True).start()
    try:
        result = run_coordinator(
            stand_in,
            CodeReviewInput(
                files={"big_a.py": content_a, "big_b.py": content_b},
                task_description="t",
                language="python",
            ),
        )
    finally:
        release.set()

    assert result.approved is True
    assert delegate.peak <= 2, "run-wide limiter must cap concurrent bisection calls at the ceiling"
    assert delegate.peak >= 1  # sanity: the concurrent path (not the sequential fallback) ran


def test_unnamed_path_reviews_as_single_unnamed_block() -> None:
    client = _ScriptedClient(
        [{"approved": True, "issues": [], "summary": "fine", "spec_compliance_notes": ""}]
    )
    result = run_coordinator(
        client, CodeReviewInput(files={"": "x = compute()\ny = x + 1"}, task_description="t")
    )
    assert result.approved is True
    assert result.summary == "fine"


# ---------------------------------------------------------------------------
# Issue normalization: paths, sanitization, anchoring
# ---------------------------------------------------------------------------


def test_normalize_issue_path_blank_and_suffix_cases() -> None:
    """_normalize_issue_path maps blank inputs and (lines X-Y of Z) labels
    back to the underlying file path."""
    from code_review_agent.coordinator import _normalize_issue_path

    seg = FileSegment(path="a.py", content="x = 1", start_line=501, total_lines=900)
    chunk = ReviewChunk(segments=[seg])
    assert _normalize_issue_path("", chunk) == "a.py"
    assert _normalize_issue_path("a.py (lines 501-505 of 900)", chunk) == "a.py"
    two = ReviewChunk(segments=[seg, FileSegment(path="b.py", content="y = 2", total_lines=1)])
    assert _normalize_issue_path("", two) == ""


def test_issues_from_chunk_output_skips_non_dict_entries() -> None:
    """_issues_from_chunk_output defensively ignores non-dict LLM items."""
    from code_review_agent.coordinator import _issues_from_chunk_output

    seg = FileSegment(path="a.py", content="x = 1", start_line=501, total_lines=900)
    chunk = ReviewChunk(segments=[seg])
    assert _issues_from_chunk_output(chunk, ["not-a-dict"]) == []


def test_blank_path_in_multi_segment_chunk_stays_blank() -> None:
    """A blank path in a multi-segment chunk must never be replaced with a
    fabricated multi-file label (which would defeat offset lookup and feed
    garbage paths downstream)."""
    tail = FileSegment(path="big.py", content="x = 1\ny = 2", start_line=801, total_lines=802)
    other = FileSegment(path="small.py", content="z = 3", total_lines=1)
    chunk = ReviewChunk(segments=[tail, other])
    issues = _issues_from_chunk_output(
        chunk, [{"description": "problem", "line": 1, "severity": "high"}]
    )
    assert len(issues) == 1
    assert issues[0].file_path == ""
    assert issues[0].line == 1  # unknown segment → anchored as-is, never shifted


def test_malformed_severity_and_category_are_sanitized_not_crashing() -> None:
    """LLM output is untrusted boundary input: null/numeric severity must be
    coerced, never allowed to raise out of the coordinator."""
    seg = FileSegment(path="a.py", content="x = 1\ny = 2\nz = 3", total_lines=3)
    chunk = ReviewChunk(segments=[seg])
    issues = _issues_from_chunk_output(
        chunk,
        [
            {"description": "bad sev", "severity": None, "category": None, "line": 2},
            {"description": "numeric sev", "severity": 3, "line": 1},
            {"description": "unknown sev", "severity": "blocker", "line": 3},
            {"severity": "high"},  # no description → skipped
        ],
    )
    assert [i.severity for i in issues] == ["high", "high", "high"]
    assert issues[0].category == "general"


def test_unrecognized_category_is_clamped_to_general() -> None:
    """An off-contract category string (not in the documented set) is clamped
    to 'general' rather than passed through verbatim -- mirrors the existing
    severity clamp so CodeReviewIssue.category never drifts from its contract."""
    seg = FileSegment(path="a.py", content="x = 1", total_lines=1)
    chunk = ReviewChunk(segments=[seg])
    issues = _issues_from_chunk_output(
        chunk,
        [{"description": "d", "category": "made-up-category", "severity": "high"}],
    )
    assert issues[0].category == "general"


def test_side_effects_category_survives_chunk_output_validation() -> None:
    """Regression test: the "side-effects" category (advertised to the chunk
    reviewer by profiles.py's "Caller Side Effects" criterion, item 3, /
    output contract) must be accepted by the same validator as every other
    documented category -- it
    was previously missing from _VALID_CATEGORIES, silently clamping every
    chunk-level side-effects finding to "general" and losing its
    classification for rendering/grouping/dedup."""
    seg = FileSegment(path="a.py", content="x = 1", total_lines=1)
    chunk = ReviewChunk(segments=[seg])
    issues = _issues_from_chunk_output(
        chunk,
        [{"description": "d", "category": "side-effects", "severity": "high"}],
    )
    assert issues[0].category == "side-effects"


def test_documentation_category_survives_chunk_output_validation() -> None:
    """The "documentation" category (advertised to the chunk reviewer by
    profiles.py's "Contracts" criterion, item 2, / output contract, and used
    for a docstring-vs-implementation mismatch) must be accepted by the same
    validator as every other documented category rather than clamped to
    "general" -- mirrors the side-effects regression above."""
    seg = FileSegment(path="a.py", content="x = 1", total_lines=1)
    chunk = ReviewChunk(segments=[seg])
    issues = _issues_from_chunk_output(
        chunk,
        [{"description": "d", "category": "documentation", "severity": "medium"}],
    )
    assert issues[0].category == "documentation"


def test_pre_existing_tag_is_carried_through_and_defaults_true() -> None:
    """The optional ``pre_existing`` tag (used by the PR-review path to route a
    finding to an issue proposal instead of a PR comment) survives conversion,
    tolerates string encodings, and defaults True when absent (uncertain
    findings are treated as out-of-scope rather than guessed into scope)."""
    seg = FileSegment(path="a.py", content="x = 1\ny = 2\nz = 3", total_lines=3)
    chunk = ReviewChunk(segments=[seg])
    issues = _issues_from_chunk_output(
        chunk,
        [
            {"description": "tagged bool", "line": 1, "pre_existing": True},
            {"description": "tagged str", "line": 2, "pre_existing": "true"},
            {"description": "tagged false str", "line": 3, "pre_existing": "false"},
            {"description": "untagged", "line": 1},
        ],
    )
    assert [i.pre_existing for i in issues] == [True, True, False, True]


def test_omission_tag_is_carried_through_and_defaults_false() -> None:
    """The optional ``omission`` tag (the positive signal for "this change
    should have added or modified file X but didn't", distinct from
    ``pre_existing``) survives conversion, tolerates string encodings, and
    defaults False when absent -- mirrors
    ``test_pre_existing_tag_is_carried_through_and_defaults_true``."""
    seg = FileSegment(path="a.py", content="x = 1\ny = 2\nz = 3", total_lines=3)
    chunk = ReviewChunk(segments=[seg])
    issues = _issues_from_chunk_output(
        chunk,
        [
            {"description": "tagged bool", "line": 1, "omission": True},
            {"description": "tagged str", "line": 2, "omission": "true"},
            {"description": "tagged false str", "line": 3, "omission": "false"},
            {"description": "untagged", "line": 1},
        ],
    )
    assert [i.omission for i in issues] == [True, True, False, False]


def test_omission_and_pre_existing_both_true_is_rejected_on_construction() -> None:
    """``CodeReviewIssue`` rejects the self-contradictory combination
    directly, not just via LLM-schema validation: an omission is by
    definition in-scope for this change (see ``omission``'s Field
    description), so any caller constructing both True is a bug."""
    with pytest.raises(ValidationError):
        CodeReviewIssue(description="d", omission=True, pre_existing=True)


def test_issues_from_chunk_output_reconciles_contradictory_raw_tags() -> None:
    """A raw LLM dict tagging both ``omission`` and ``pre_existing`` true
    (a self-contradictory reply that ``CodeReviewIssue`` would otherwise
    reject via ``_omission_implies_in_scope``) is reconciled at this
    boundary rather than raised: ``omission`` wins, so the constructed
    issue is in-scope. This keeps ``_issues_from_chunk_output``'s documented
    "never raises on malformed output" contract intact."""
    seg = FileSegment(path="a.py", content="x = 1", total_lines=1)
    chunk = ReviewChunk(segments=[seg])
    issues = _issues_from_chunk_output(
        chunk,
        [{"description": "contradictory tags", "line": 1, "omission": True, "pre_existing": True}],
    )
    assert len(issues) == 1
    assert issues[0].omission is True
    assert issues[0].pre_existing is False


_NO_OP_SUGGESTIONS = [
    "No changes needed.",
    "no changes needed",
    "No change required",
    "no change is required",
    "No code changes needed.",
    "No action needed.",
    "no action is required",
    "No fix needed",
    "no fixes required",
    "Nothing to change.",
    "nothing to fix",
    "Nothing to do",
]


@pytest.mark.parametrize(
    "suggestion",
    _NO_OP_SUGGESTIONS,
)
def test_is_no_op_suggestion_matches_known_phrasings(suggestion: str) -> None:
    """Known no-op phrasings are classified as no-ops."""
    assert is_no_op_suggestion(suggestion) is True


def test_is_no_op_suggestion_spares_blank_and_substantive_text() -> None:
    """Blank and substantive strings are not treated as no-ops."""
    assert is_no_op_suggestion("") is False
    assert is_no_op_suggestion(None) is False
    assert is_no_op_suggestion("   ") is False
    # Contains the word "change" but is not, as a whole string, a no-op verdict.
    assert (
        is_no_op_suggestion("Change the timeout to 30s and no changes are needed elsewhere.")
        is False
    )
    assert is_no_op_suggestion("Add a null check before dereferencing `user`.") is False


@pytest.mark.parametrize(
    "suggestion",
    _NO_OP_SUGGESTIONS,
)
def test_issue_with_no_op_suggestion_is_dropped(suggestion: str) -> None:
    """A finding whose suggested fix says, in full, that nothing needs to
    change is the reviewer's own admission there is no issue -- it must never
    be reported."""
    seg = FileSegment(path="a.py", content="x = 1\ny = 2", total_lines=2)
    chunk = ReviewChunk(segments=[seg])
    issues = _issues_from_chunk_output(
        chunk,
        [
            {
                "description": "informational observation",
                "severity": "info",
                "file_path": "a.py",
                "line": 1,
                "suggestion": suggestion,
            }
        ],
    )
    assert issues == []


def test_no_op_suggestion_filter_does_not_drop_real_issues() -> None:
    """A substantive suggestion survives even when it contains a word ('change')
    the no-op phrasing also uses -- only a whole-string no-op match is dropped."""
    seg = FileSegment(path="a.py", content="x = 1\ny = 2", total_lines=2)
    chunk = ReviewChunk(segments=[seg])
    issues = _issues_from_chunk_output(
        chunk,
        [
            {
                "description": "off-by-one in the loop bound",
                "severity": "high",
                "file_path": "a.py",
                "line": 1,
                "suggestion": "Change `range(n)` to `range(n + 1)` to include the last element.",
            },
            {
                "description": "no suggestion given at all",
                "severity": "low",
                "file_path": "a.py",
                "line": 2,
                # A blank suggestion is not the same as an explicit "no change
                # needed" -- it must still be reported.
                "suggestion": "",
            },
        ],
    )
    assert len(issues) == 2
    assert issues[0].suggestion.startswith("Change `range(n)`")
    assert issues[1].suggestion == ""


def test_coerce_bool_recognizes_truthy_tokens_only() -> None:
    from code_review_agent.chunking import _coerce_bool

    assert _coerce_bool(True) is True
    assert _coerce_bool("true") is True
    assert _coerce_bool("YES") is True
    assert _coerce_bool("1") is True
    # Falsy / unrecognized string tokens (note: bare bool("false") would be True).
    assert _coerce_bool("false") is False
    assert _coerce_bool("no") is False
    assert _coerce_bool("") is False
    assert _coerce_bool(None) is False
    assert _coerce_bool(0) is False
    # A bare number is never treated as true, even a truthy one — only a real
    # bool or a recognized truthy string counts (mirrors tech_lead_agent's
    # stricter convention for the same LLM-flag-drift problem).
    assert _coerce_bool(1) is False


def test_coerce_scope_tags_reconciles_omission_and_pre_existing() -> None:
    """_coerce_scope_tags is the single shared reconciliation helper for the
    pre_existing/omission pair, used by chunking._issues_from_chunk_output,
    architecture_consistency_pass._coerce_finding, and
    side_effect_impact_pass._coerce_finding: each coerces via _coerce_bool,
    omission wins when both raw tags are true, and pre_existing defaults True
    when absent (uncertain findings treated as out-of-scope)."""
    from code_review_agent.chunking import _coerce_scope_tags

    # Absent pre_existing defaults True (uncertain ⇒ out-of-scope).
    assert _coerce_scope_tags({}) == (True, False)
    assert _coerce_scope_tags({"pre_existing": True}) == (True, False)
    assert _coerce_scope_tags({"pre_existing": False}) == (False, False)
    assert _coerce_scope_tags({"omission": True}) == (False, True)
    # omission wins over a contradictory pre_existing tag.
    assert _coerce_scope_tags({"pre_existing": True, "omission": True}) == (False, True)
    # String encodings tolerated the same way _coerce_bool tolerates them.
    assert _coerce_scope_tags({"pre_existing": "true", "omission": "yes"}) == (False, True)
    # Explicitly false pre_existing is honored.
    assert _coerce_scope_tags({"pre_existing": "false"}) == (False, False)


def test_validate_line_absolute_numbering_has_no_overlap_ambiguity() -> None:
    """Partial segments are rendered with original line-number prefixes, so a
    citation is absolute by construction: a segment whose absolute range
    overlaps [1, line_count] (e.g. 100 lines starting at line 50) anchors
    line 75 to line 75 — never shifted to 124 — and out-of-range citations
    are dropped rather than mis-anchored."""
    overlapping = FileSegment(
        path="big.py",
        content="\n".join(f"l{i}" for i in range(100)),  # 100 lines, covers 50-149
        start_line=50,
        total_lines=900,
    )
    assert _validate_line(75, overlapping) == 75  # absolute, inside [50, 149]
    assert _validate_line(50, overlapping) == 50
    assert _validate_line(149, overlapping) == 149
    assert _validate_line(5, overlapping) is None  # below the shown range → dropped
    assert _validate_line(200, overlapping) is None  # above the shown range → dropped
    assert _validate_line(None, overlapping) is None
    assert _validate_line(7, None) == 7  # unknown segment → as-is
    pre = FileSegment(path="pr.py", content="4000: a\n4001: b", pre_numbered=True, total_lines=2)
    assert _validate_line(4001, pre) == 4001  # pre-numbered owns its numbering


# ---------------------------------------------------------------------------
# Reconcile safety-net port (coordinator level)
# ---------------------------------------------------------------------------


def test_zero_issue_reject_with_summary_now_fails_schema_validation() -> None:
    """A chunk rejecting with zero issues but a meaningful summary used to be
    silently repaired by ``mapping._outcome_from_output`` (synthesizing a
    "high" issue from the summary text). ``ChunkReviewLLMResponse``'s
    consistency validator now intentionally rejects that exact shape at the
    schema layer instead (an ``approved=False`` verdict must already carry an
    actionable critical/high issue) -- per ``models.py``'s own documented
    rationale, this reply is no longer "silently absorbed by the
    coordinator's safety net" but fails validation and retries once. With
    this being the submission's only chunk, the retry fails identically and
    the coordinator's total-failure guard raises ``CodeReviewUnavailableError``
    rather than fabricating a verdict for code that was never actually
    reviewed."""
    client = _ScriptedClient(
        [
            {
                "approved": False,
                "issues": [],
                "summary": "Missing input validation throughout.",
                "spec_compliance_notes": "",
            }
        ]
    )
    with pytest.raises(CodeReviewUnavailableError):
        run_coordinator(
            client,
            CodeReviewInput(files={"a.py": "x = 1"}, task_description="t", language="python"),
        )


def test_rejecting_chunk_with_only_a_summary_degrades_instead_of_blocking() -> None:
    """A chunk that rejects with zero issues but a meaningful summary now fails
    ``ChunkReviewLLMResponse`` validation (see
    ``test_zero_issue_reject_with_summary_now_fails_schema_validation``) instead
    of being synthesized into a "high" issue. With sibling chunks that did
    produce a verdict, the coordinator degrades the failing chunk to a
    non-blocking "not reviewed" range rather than aborting the whole
    submission -- so the overall review still completes, an unrelated empty
    file still contributes its info finding, and no phantom high issue is
    fabricated from the untrusted summary text."""
    llm_probe = DummyLLMClient()
    cap = compute_code_review_map_chunk_chars(llm_probe)

    class _RejectBWithSummaryOnly(DummyLLMClient):
        """Both the reasoning pass (reached via the Strands Agent's ``chat()``
        delegation) and the formatting pass land on ``complete_json`` now.
        The reasoning-pass call returns the marker as raw prose (identified
        by the absence of the "--- ANALYSIS" formatting wrapper); the
        formatting-pass call then finds that marker in its own
        ``wrap_with_analysis_delimiters``-wrapped prompt, mirroring how the
        marker used to flow from ``complete`` into ``complete_json``.
        """

        _B_MARKER = "B_CHUNK_REJECT_SUMMARY_ONLY"

        def complete_json(self, prompt: str, **kwargs: Any) -> Any:
            if not _is_formatting_pass_prompt(prompt):
                if (
                    _is_chunk_map_reasoning_prompt(prompt)
                    and "### b.py ###" in prompt
                    and "### a.py ###" not in prompt
                ):
                    return self._B_MARKER
                return super().complete_json(prompt, **kwargs)
            if self._B_MARKER in prompt:
                return {
                    "approved": False,
                    "issues": [],
                    "summary": "Missing error handling around DB calls in b.py.",
                    "spec_compliance_notes": "",
                }
            return {
                "approved": True,
                "issues": [],
                "summary": "a.py looks fine",
                "spec_compliance_notes": "",
            }

    files = {
        "a.py": "a = 1\n".ljust(cap - _MAP_CHUNK_SEPARATION_HEADROOM, "#"),
        "b.py": "b = 2\n".ljust(cap - _MAP_CHUNK_SEPARATION_HEADROOM, "#"),
        "empty.py": "",  # contributes the non-blocking info finding
    }
    result = run_coordinator(
        _RejectBWithSummaryOnly(),
        CodeReviewInput(files=files, task_description="t", language="python"),
    )

    # b.py degrades non-blockingly (CODE_REVIEW_BLOCK_ON_UNREVIEWED is off by
    # default) rather than being synthesized into a rejecting "high" issue.
    assert "b.py (lines 1-2)" in result.not_reviewed_ranges
    assert not any("Missing error handling around DB calls" in i.description for i in result.issues)
    info = [i for i in result.issues if i.severity == "info"]
    assert [i.file_path for i in info] == ["empty.py"]


def test_silent_rejection_never_borrows_an_approving_chunks_summary() -> None:
    """A chunk that rejects with no issues AND no summary has no actionable
    feedback for ``ChunkReviewLLMResponse``'s consistency validator either
    (same as ``test_zero_issue_reject_with_summary_now_fails_schema_validation``,
    the empty summary here doesn't change that), so it fails schema
    validation and degrades to a non-blocking ``not_reviewed_ranges`` entry
    rather than the old ``_reconcile_approval`` auto-approve safety net. The
    approving sibling chunk's summary must never leak into a phantom 'Code
    review rejected: Looks good' issue (verdicts and summaries stay paired
    per chunk)."""
    llm_probe = DummyLLMClient()
    cap = compute_code_review_map_chunk_chars(llm_probe)

    class _SilentRejectB(DummyLLMClient):
        """See ``_RejectBWithSummaryOnly`` above for why the marker now
        flows through a single ``complete_json`` override instead of a
        separate ``complete``/``complete_json`` pair."""

        _B_MARKER = "B_CHUNK_SILENT_REJECT"

        def complete_json(self, prompt: str, **kwargs: Any) -> Any:
            if not _is_formatting_pass_prompt(prompt):
                if (
                    _is_chunk_map_reasoning_prompt(prompt)
                    and "### b.py ###" in prompt
                    and "### a.py ###" not in prompt
                ):
                    return self._B_MARKER
                return super().complete_json(prompt, **kwargs)
            if self._B_MARKER in prompt:
                return {
                    "approved": False,
                    "issues": [],
                    "summary": "",
                    "spec_compliance_notes": "",
                }
            return {
                "approved": True,
                "issues": [],
                "summary": "Looks good",
                "spec_compliance_notes": "",
            }

    files = {
        "a.py": "a = 1\n".ljust(cap - _MAP_CHUNK_SEPARATION_HEADROOM, "#"),
        "b.py": "b = 2\n".ljust(cap - _MAP_CHUNK_SEPARATION_HEADROOM, "#"),
    }
    result = run_coordinator(
        _SilentRejectB(),
        CodeReviewInput(files=files, task_description="t", language="python"),
    )

    assert "b.py (lines 1-2)" in result.not_reviewed_ranges
    assert result.approved is True
    assert result.issues == []
    assert "Looks good" in result.summary  # the approving summary survives as summary text


def test_no_stale_progress_reports_after_map_failure() -> None:
    """A worker still in flight when the map phase fails must never report
    progress afterwards — a stale 'reviewing' report would overwrite the
    caller's recorded failure state.

    ``parallel_map``'s fast-fail abort flag only *narrows* (its own docstring's
    word), never eliminates, the window in which a not-yet-started task is
    skipped instead of run — a task must already be past its own abort check
    (i.e. actually "in flight") for cancellation to be a no-op. The fast-fail
    branch below therefore waits on ``slow_started`` before raising, so the
    slow chunk's worker is deterministically already blocked in ``release.wait``
    (past that check) before the abort flag can ever be set, instead of
    racing raw thread-scheduling timing to get there first.
    """
    release = threading.Event()
    slow_started = threading.Event()
    calls: list = []

    class _OneFailsOneBlocks(DummyLLMClient):
        def __init__(self) -> None:
            super().__init__()
            self.slow_finished = threading.Event()

        def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
            if _is_formatting_pass_prompt(prompt) or not _is_chunk_map_reasoning_prompt(prompt):
                return super().complete_json(prompt, **kwargs)
            if "FAILME" in prompt:
                assert slow_started.wait(timeout=10), "slow chunk must start first"
                raise LLMRateLimitError("429")
            slow_started.set()
            release.wait(timeout=10)
            result = super().complete_json(prompt, **kwargs)
            self.slow_finished.set()
            return result

    llm_probe = DummyLLMClient()
    cap = compute_code_review_map_chunk_chars(llm_probe)
    files = {
        "fast_fail.py": "FAILME = 1\n".ljust(cap - _MAP_CHUNK_SEPARATION_HEADROOM, "#"),
        "slow.py": "ok = 1\n".ljust(cap - _MAP_CHUNK_SEPARATION_HEADROOM, "#"),
    }
    client = _OneFailsOneBlocks()
    try:
        with pytest.raises(CodeReviewUnavailableError):
            run_coordinator(
                client,
                CodeReviewInput(files=files, task_description="t", language="python"),
                progress_callback=lambda s, d, f: calls.append((s, d, f)),
            )
        reports_at_failure = list(calls)
    finally:
        release.set()
    # Let the abandoned worker finish, then confirm it reported nothing more
    # (the brief grace period lets a buggy late notify land before asserting).
    assert client.slow_finished.wait(timeout=10), "abandoned worker must still complete"
    time.sleep(_LATE_NOTIFY_GRACE_PERIOD_S)
    assert calls == reports_at_failure
    assert not any("slow.py" in d for _s, d, _f in calls)


def test_coordinator_single_chunk_propagates_notes() -> None:
    """Default-off path: a single chunk's ``spec_compliance_notes`` propagate directly.

    ``CODE_REVIEW_SPEC_COMPLIANCE_PASS`` is not enabled here, so the coordinator
    keeps the single-chunk fast path (see ``_merge_narrative``) and the lone
    chunk's notes reach ``CodeReviewOutput.spec_compliance_notes`` unchanged,
    with no synthesis LLM call in between.
    """
    client = _ScriptedClient(
        [
            {
                "approved": True,
                "issues": [],
                "summary": "All good.",
                "spec_compliance_notes": "Meets all acceptance criteria.",
            }
        ]
    )
    result = run_coordinator(
        client,
        CodeReviewInput(files={"a.py": "def a(): pass"}, task_description="t", language="python"),
    )
    assert result.spec_compliance_notes == "Meets all acceptance criteria."


def test_coordinator_multi_chunk_synthesizes_notes() -> None:
    """Spec notes must survive multi-chunk reviews; the reduce phase runs one
    findings-only synthesis pass that produces a single coherent value (not a
    raw join)."""
    llm_probe = DummyLLMClient()
    cap = compute_code_review_map_chunk_chars(llm_probe)
    files = {
        "a.py": "a = 1\n".ljust(max(0, cap - 1_000), "#"),
        "b.py": "b = 2\n".ljust(max(0, cap - 1_000), "#"),
    }
    # The scripted client answers both chunk reviews and the synthesis pass with
    # the same canned payload, so the synthesized notes are "chunk notes" (one
    # coherent value), never the old "chunk notes\n\nchunk notes" concatenation.
    client = _ScriptedClient(
        [
            {
                "approved": True,
                "issues": [],
                "summary": "ok",
                "spec_compliance_notes": "chunk notes",
            }
        ]
    )
    result = run_coordinator(
        client, CodeReviewInput(files=files, task_description="t", language="python")
    )
    assert result.approved is True
    assert result.spec_compliance_notes == "chunk notes"


def test_single_chunk_keeps_notes_after_bisection() -> None:
    """A logically-single-chunk review that recovers via bisection keeps its
    spec notes (now via the findings-only synthesis pass over the two halves)."""

    class _FailCombinedWithNotes(DummyLLMClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def complete(self, prompt: str, **kwargs: Any) -> str:
            if _is_chunk_map_reasoning_prompt(prompt):
                self.calls += 1
                if "### a.py ###" in prompt and "### b.py ###" in prompt:
                    raise LLMSemanticExhaustionError("no content")
            return super().complete(prompt, **kwargs)

        def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            return {
                "approved": True,
                "issues": [],
                "summary": "ok",
                "spec_compliance_notes": "half notes",
            }

    result = run_coordinator(
        _FailCombinedWithNotes(),
        CodeReviewInput(
            files={"a.py": "def a(): pass", "b.py": "def b(): pass"},
            task_description="t",
            language="python",
        ),
    )
    assert result.spec_compliance_notes == "half notes"


# ---------------------------------------------------------------------------
# Language threading
# ---------------------------------------------------------------------------


def test_language_is_threaded_into_every_chunk_prompt() -> None:
    """The caller-declared language must reach the chunk prompt — never be
    re-guessed from the first 500 chars of the chunk."""

    class _Recorder(DummyLLMClient):
        def __init__(self) -> None:
            super().__init__()
            self.prompts: list[str] = []

        def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
            self.prompts.append(prompt)
            return super().complete_json(prompt, **kwargs)

    client = _Recorder()
    # No "def " anywhere: the old heuristic would have guessed typescript.
    run_coordinator(
        client,
        CodeReviewInput(
            files={"config.py": "TIMEOUT = 30\nRETRIES = 2"},
            task_description="t",
            language="python",
        ),
    )
    chunk_prompts = [p for p in client.prompts if CHUNK_REVIEW_NOTE in p]
    assert chunk_prompts
    assert all("**Language:** python" in p for p in chunk_prompts)


def test_user_decisions_thread_through_coordinator_to_chunk_prompt() -> None:
    """A CodeReviewInput.user_decisions reaches the per-chunk review prompt as settled context."""

    class _Recorder(DummyLLMClient):
        def __init__(self) -> None:
            super().__init__()
            self.prompts: list[str] = []

        def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
            self.prompts.append(prompt)
            return super().complete_json(prompt, **kwargs)

    client = _Recorder()
    run_coordinator(
        client,
        CodeReviewInput(
            files={"config.py": "TIMEOUT = 30"},
            task_description="t",
            language="python",
            user_decisions=["Which timeout? → 30s"],
        ),
    )
    chunk_prompts = [p for p in client.prompts if CHUNK_REVIEW_NOTE in p]
    assert chunk_prompts
    assert all("Which timeout? → 30s" in p for p in chunk_prompts)


# ---------------------------------------------------------------------------
# End-to-end property: large synthetic input through CodeReviewAgent.run
# ---------------------------------------------------------------------------


class _RecordingClient(DummyLLMClient):
    """Delegates to Dummy but records map-phase reasoning prompts.

    Thread-safe: map calls may append concurrently under parallelism.
    """

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []
        self._lock = threading.Lock()

    def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            self.prompts.append(prompt)
        return super().complete_json(prompt, **kwargs)


def test_large_synthetic_input_is_fully_covered_with_bounded_prompts() -> None:
    from code_review_agent.agent import CodeReviewAgent

    client = _RecordingClient()
    cap = compute_code_review_map_chunk_chars(client)
    files = {f"app/mod_{i}.py": _numbered_file(2_500) for i in range(5)}  # ~500K chars total

    agent = CodeReviewAgent(llm_client=client, force_in_process=True)
    result = agent.run(CodeReviewInput(files=files, task_description="t", language="python"))

    assert isinstance(result, CodeReviewOutput)
    assert len(client.prompts) > 1
    # Every file appears in at least one map prompt.
    for path in files:
        assert any(f"### {path} ###" in p for p in client.prompts)
    # Map prompts are bounded: chunk cap plus the fixed instruction overhead.
    # (Tail-pass prompts — false-positive filter / merged architecture+side-effect —
    # intentionally use their own budgets and are not subject to this map-call bound.)
    chunk_prompts = [p for p in client.prompts if CHUNK_REVIEW_NOTE in p]
    assert chunk_prompts
    assert all(len(p) <= cap + 2_000 for p in chunk_prompts)


# ---------------------------------------------------------------------------
# Progress callback reporting
# ---------------------------------------------------------------------------


def test_coordinator_reports_per_chunk_progress() -> None:
    """With 2 chunks the coordinator reports one 'chunk i/2 reviewed' per
    completion (fractions inside (0.10, 0.90], non-decreasing even with
    parallel workers), then finalizing and done at 1.0."""
    files = {
        "app/main.py": "a" * 25_000,
        "app/util.py": "b" * 25_000,
    }

    calls: list = []

    def _cb(step: str, detail: str, fraction: float) -> None:
        calls.append((step, detail, fraction))

    result = run_coordinator(
        DummyLLMClient(),
        CodeReviewInput(files=files, task_description="Add feature", language="python"),
        progress_callback=_cb,
    )
    assert isinstance(result, CodeReviewOutput)

    steps = [c[0] for c in calls]
    assert steps[0] == "preparing"
    assert "finalizing" in steps
    assert steps[-1] == "done"

    reviewing = [c for c in calls if c[0] == "reviewing"]
    assert any("chunk 1/2 reviewed" in c[1] for c in reviewing)
    assert any("chunk 2/2 reviewed" in c[1] for c in reviewing)
    assert all(0.10 < c[2] <= 0.90 for c in reviewing), reviewing

    fractions = [c[2] for c in calls]
    assert fractions == sorted(fractions), "fractions must be non-decreasing"
    assert fractions[-1] == 1.0
    assert "approved=" in calls[-1][1]


def test_empty_input_still_reports_done() -> None:
    """The done/1.0 postcondition holds on the empty-input short-circuit too."""
    calls: list = []
    result = run_coordinator(
        DummyLLMClient(),
        CodeReviewInput(files={"a.py": ""}, task_description="t"),
        progress_callback=lambda s, d, f: calls.append((s, d, f)),
    )
    assert result.approved is True
    assert calls[-1][0] == "done"
    assert calls[-1][2] == 1.0


# ---------------------------------------------------------------------------
# repo-reader threading
# ---------------------------------------------------------------------------


def test_coordinator_does_not_run_class_cohesion_pass() -> None:
    """The coordinator no longer runs a per-class cohesion review.

    A submission containing a Python class is reviewed only through the
    size-based map phase — no per-class LLM call (whose prompt would carry the
    cohesion "Stated purpose" marker) is ever issued, and no advisory
    structure/medium cohesion finding appears in the output.
    """

    class _CohesionSpy(DummyLLMClient):
        def __init__(self) -> None:
            super().__init__()
            self.saw_cohesion_prompt = False

        def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
            if "Stated purpose" in prompt:
                self.saw_cohesion_prompt = True
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    spy = _CohesionSpy()
    src = (
        "class Report:\n"
        '    """Builds a report."""\n'
        "    def build(self):\n"
        "        return 1\n"
        "    def send_email(self, to):\n"
        "        return to\n"
    )
    result = run_coordinator(
        spy,
        CodeReviewInput(
            files={"report.py": src},
            task_description="t",
            language="python",
            skip_false_positive_filter=True,
        ),
    )
    assert spy.saw_cohesion_prompt is False
    assert result.approved is True
    assert result.issues == []


def test_coordinator_threads_repo_reader_to_filter(monkeypatch) -> None:
    """The ``repo_reader`` argument is forwarded verbatim to the false-positive filter."""
    import code_review_agent.coordinator as coord

    captured: dict[str, Any] = {}

    def _spy(llm, input_data, issues, repo_reader=None, index=None):
        captured["reader"] = repo_reader
        return issues

    monkeypatch.setattr(coord, "filter_false_positives", _spy)
    reader = object()
    run_coordinator(
        DummyLLMClient(),
        CodeReviewInput(files={"a.py": "x = 1\n"}, task_description="t"),
        repo_reader=reader,
    )
    assert captured["reader"] is reader


def test_coordinator_runs_with_submission_cache_disabled(monkeypatch) -> None:
    """``run_coordinator`` must not crash when the submission cache is disabled
    (``CODE_REVIEW_SUBMISSION_CACHE_SIZE`` resolves to 0). Guards the explicit
    ``cached = None`` initialization: without it, any future refactor that reads
    ``cached`` outside the cache-enabled branch would raise ``UnboundLocalError``
    for this codepath."""
    import code_review_agent.coordinator as coord

    monkeypatch.setattr(coord, "_submission_cache_size", lambda: 0)
    result = run_coordinator(
        DummyLLMClient(),
        CodeReviewInput(files={"a.py": "x = 1\n"}, task_description="t"),
    )
    assert result.approved is True


def test_coordinator_builds_codebase_index_once_and_shares_it(monkeypatch) -> None:
    """The submission is parsed into a ``CodebaseIndex`` exactly once per
    ``run_coordinator`` call, and the same instance is forwarded to both the
    false-positive filter and the merged architecture/side-effect pass (rather
    than each independently rebuilding it from the same input)."""
    import code_review_agent.coordinator as coord
    from code_review_agent.false_positive_filter import CodebaseIndex

    build_calls: list = []
    original_from_input = CodebaseIndex.from_input

    def _counting_from_input(*args, **kwargs):
        result = original_from_input(*args, **kwargs)
        build_calls.append(result)
        return result

    monkeypatch.setattr(
        CodebaseIndex,
        "from_input",
        classmethod(lambda cls, *a, **kw: _counting_from_input(*a, **kw)),
    )

    received_indexes: list = []

    def _filter_spy(llm, input_data, issues, repo_reader=None, index=None):
        received_indexes.append(index)
        return issues

    def _merged_spy(llm, input_data, repo_reader=None, index=None):
        received_indexes.append(index)
        return [], []

    monkeypatch.setattr(coord, "filter_false_positives", _filter_spy)
    monkeypatch.setattr(coord, "find_architecture_and_side_effect_issues", _merged_spy)

    run_coordinator(
        DummyLLMClient(),
        CodeReviewInput(files={"a.py": "x = 1\n"}, task_description="t"),
    )

    assert len(build_calls) == 1, "CodebaseIndex.from_input should be called exactly once"
    assert received_indexes == [build_calls[0], build_calls[0]]


def test_single_chunk_summary_reflects_architecture_findings(monkeypatch) -> None:
    """A single-chunk review whose only new findings come from the
    architecture-consistency pass must not silently drop them from the
    narrative: with exactly one map summary the coordinator would otherwise
    return that chunk's summary verbatim, which said nothing about a finding
    the map phase never saw."""
    import code_review_agent.coordinator as coord
    from code_review_agent.models import CodeReviewIssue

    arch_issue = CodeReviewIssue(
        severity="high",
        category="architecture",
        file_path="a.py",
        description="Duplicates the existing `Widget` service.",
    )
    monkeypatch.setattr(
        coord,
        "find_architecture_and_side_effect_issues",
        lambda *a, **kw: ([arch_issue], []),
    )

    synth_calls: list = []
    original_synthesize = coord.synthesize_review_findings

    def _spy(*args, **kwargs):
        synth_calls.append(True)
        return original_synthesize(*args, **kwargs)

    monkeypatch.setattr(coord, "synthesize_review_findings", _spy)

    result = run_coordinator(
        DummyLLMClient(),
        CodeReviewInput(files={"a.py": "x = 1\n"}, task_description="t"),
    )

    assert synth_calls, "synthesis must run so the narrative reflects the architecture finding"
    assert any(i.description == arch_issue.description for i in result.issues)


def test_single_chunk_summary_reflects_side_effect_findings(monkeypatch) -> None:
    """Regression test: the same gap as
    ``test_single_chunk_summary_reflects_architecture_findings``, but for the
    merged architecture/side-effect pass -- ``_merge_narrative`` was only ever told
    about ``architecture_findings``, so a single-chunk review whose only new
    findings come from the side-effect pass silently dropped them from the
    narrative (returning the map phase's chunk summary verbatim, which never
    mentions a finding it never saw)."""
    import code_review_agent.coordinator as coord
    from code_review_agent.models import CodeReviewIssue

    side_effect_issue = CodeReviewIssue(
        severity="high",
        category="side-effects",
        file_path="a.py",
        description="bar() no longer raises ValueError; app/caller.py still catches it.",
    )
    monkeypatch.setattr(
        coord,
        "find_architecture_and_side_effect_issues",
        lambda *a, **kw: ([], [side_effect_issue]),
    )

    synth_calls: list = []
    original_synthesize = coord.synthesize_review_findings

    def _spy(*args, **kwargs):
        synth_calls.append(True)
        return original_synthesize(*args, **kwargs)

    monkeypatch.setattr(coord, "synthesize_review_findings", _spy)

    result = run_coordinator(
        DummyLLMClient(),
        CodeReviewInput(files={"a.py": "x = 1\n"}, task_description="t"),
    )

    assert synth_calls, "synthesis must run so the narrative reflects the side-effect finding"
    assert any(i.description == side_effect_issue.description for i in result.issues)


def test_spec_compliance_single_pass_off_by_default_skips_dedicated_pass(monkeypatch) -> None:
    """Default (``CODE_REVIEW_SPEC_COMPLIANCE_PASS`` unset): ``synthesize_spec_compliance``
    is never invoked -- the flag-off path costs zero extra calls, matching today's
    behavior exactly (see ``test_chunk_reviewer.py`` for the per-chunk-prompt side of
    this same guarantee).

    Hermetic against external environment state: explicitly clears the env var
    instead of relying on it happening to be unset in CI or a developer's shell.
    """
    import code_review_agent.coordinator as coord

    monkeypatch.delenv("CODE_REVIEW_SPEC_COMPLIANCE_PASS", raising=False)
    calls: list = []
    monkeypatch.setattr(
        coord,
        "synthesize_spec_compliance",
        lambda *a, **kw: calls.append(True) or "should not run",
    )

    run_coordinator(
        DummyLLMClient(),
        CodeReviewInput(files={"a.py": "x = 1\n"}, task_description="t"),
    )

    assert not calls, "synthesize_spec_compliance must not run when the flag is off"


def test_spec_compliance_single_pass_routes_note_into_synthesis(monkeypatch) -> None:
    """``CODE_REVIEW_SPEC_COMPLIANCE_PASS=true`` on an explicit ``CODE_REVIEW`` profile,
    multi-chunk submission: ``synthesize_spec_compliance`` runs exactly once, after
    deduplication, over the final merged issue list, and its note replaces the
    (now-empty) per-chunk spec notes fed into ``synthesize_review_findings`` for
    EVERY chunk -- not just the first one. Two chunks each report the identical
    (file_path, line, description) finding, so the merged list ``synthesize_spec_compliance``
    receives is the single deduped copy, never the raw two-copy per-chunk list; this
    also proves the old single-chunk fast path is bypassed regardless of chunk count,
    so the dedicated pass's finding is never silently dropped (ADR-010 contract
    boundary point 4)."""
    import code_review_agent.coordinator as coord
    from code_review_agent.profiles import ReviewProfile

    monkeypatch.setenv("CODE_REVIEW_SPEC_COMPLIANCE_PASS", "true")

    spec_calls: list = []

    def _spec_spy(*args, **kwargs):
        spec_calls.append(kwargs)
        return "SPEC_GAP_MARKER: missing rate limiting."

    monkeypatch.setattr(coord, "synthesize_spec_compliance", _spec_spy)

    synth_calls: list = []
    original_synthesize = coord.synthesize_review_findings

    def _spy(*args, **kwargs):
        synth_calls.append(kwargs.get("chunk_spec_notes"))
        return original_synthesize(*args, **kwargs)

    monkeypatch.setattr(coord, "synthesize_review_findings", _spy)

    duplicate_issue = {
        "severity": "high",
        "category": "logic",
        "file_path": "app/main.py",
        "line": 10,
        "description": "duplicate string literal",
        "suggestion": "extract a constant",
    }
    client = _ScriptedClient(
        [
            {
                "approved": False,
                "issues": [duplicate_issue],
                "summary": "Chunk 1 finding.",
                "spec_compliance_notes": "",
            },
            {
                "approved": False,
                "issues": [duplicate_issue],
                "summary": "Chunk 2 finding.",
                "spec_compliance_notes": "",
            },
        ]
    )

    files = {
        "app/main.py": "x" * 20_000,
        "app/models.py": "y" * 20_000,
    }
    result = run_coordinator(
        client,
        CodeReviewInput(
            files=files,
            task_description="t",
            profile=ReviewProfile.CODE_REVIEW,
            skip_tail_passes=True,
        ),
    )

    assert synth_calls, (
        "synthesis must run (fast path bypassed) so the single-pass note isn't dropped"
    )
    assert synth_calls[-1] == ["SPEC_GAP_MARKER: missing rate limiting."], (
        "chunk_spec_notes must be the single dedicated note for every chunk, not a "
        "per-chunk list -- otherwise a later chunk's per-chunk note could survive"
    )

    assert len(spec_calls) == 1, "synthesize_spec_compliance must be called exactly once"
    assert spec_calls[0]["issues"] == result.issues, (
        "synthesize_spec_compliance must run over the final merged/deduped issue list"
    )
    # Both chunks reported the identical (file_path, line, description) finding;
    # a genuine post-dedupe call sees exactly one copy, not two.
    assert len(spec_calls[0]["issues"]) == 1, (
        "the duplicate finding from both chunks must already be deduped before "
        "synthesize_spec_compliance runs"
    )


def test_spec_compliance_single_pass_restricted_to_code_review_profile(monkeypatch) -> None:
    """``CODE_REVIEW_SPEC_COMPLIANCE_PASS=true`` on a non-``CODE_REVIEW`` profile must
    not call ``synthesize_spec_compliance`` -- and (per ``test_chunk_reviewer.py``'s
    gating tests, computed off the same profile-aware boolean) must not omit the
    per-chunk acceptance-criteria/spec-excerpt blocks either. Either alone (chunk
    omission without a replacement pass) would silently drop spec-compliance checking
    for that submission entirely."""
    import code_review_agent.coordinator as coord
    from code_review_agent.profiles import ReviewProfile

    monkeypatch.setenv("CODE_REVIEW_SPEC_COMPLIANCE_PASS", "true")
    calls: list = []
    monkeypatch.setattr(
        coord,
        "synthesize_spec_compliance",
        lambda *a, **kw: calls.append(True) or "should not run",
    )

    run_coordinator(
        DummyLLMClient(),
        CodeReviewInput(
            files={"a.py": "x = 1\n"},
            task_description="t",
            acceptance_criteria=["Must validate input"],
            profile=ReviewProfile.ACCEPTANCE,
        ),
    )

    assert not calls, "synthesize_spec_compliance must not run outside the CODE_REVIEW profile"


def test_spec_compliance_single_pass_failure_falls_back_to_per_chunk_notes(monkeypatch) -> None:
    """When the dedicated ``synthesize_spec_compliance`` pass raises, the coordinator
    must not abort: ``single_pass_spec_notes`` stays ``None`` so ``_merge_narrative``
    falls back to per-chunk-sourced behavior (documented on the call site).
    """
    import code_review_agent.coordinator as coord
    from code_review_agent.profiles import ReviewProfile

    monkeypatch.setenv("CODE_REVIEW_SPEC_COMPLIANCE_PASS", "true")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("spec-compliance single pass exploded")

    monkeypatch.setattr(coord, "synthesize_spec_compliance", _boom)

    merge_kwargs: list = []
    original_merge = coord._merge_narrative

    def _merge_spy(*args, **kwargs):
        merge_kwargs.append(kwargs)
        return original_merge(*args, **kwargs)

    monkeypatch.setattr(coord, "_merge_narrative", _merge_spy)

    result = run_coordinator(
        DummyLLMClient(),
        CodeReviewInput(
            files={"a.py": "x = 1\n"},
            task_description="t",
            profile=ReviewProfile.CODE_REVIEW,
            skip_tail_passes=True,
        ),
    )

    assert isinstance(result, CodeReviewOutput)
    assert merge_kwargs, "_merge_narrative must still run after a failed single pass"
    assert merge_kwargs[-1].get("single_pass_spec_notes") is None, (
        "failed synthesize_spec_compliance must leave single_pass_spec_notes as None "
        "so _merge_narrative falls back to per-chunk notes"
    )


def test_run_spec_compliance_single_pass_flag_off_skips_call(monkeypatch) -> None:
    """``_run_spec_compliance_single_pass`` in isolation: ``spec_compliance_single_pass=False``
    returns ``None`` without ever calling ``synthesize_spec_compliance``."""
    import code_review_agent.coordinator as coord

    spec_calls: list = []
    monkeypatch.setattr(
        coord,
        "synthesize_spec_compliance",
        lambda *args, **kwargs: spec_calls.append((args, kwargs)),
    )

    result = coord._run_spec_compliance_single_pass(
        llm=DummyLLMClient(),
        input_data=CodeReviewInput(files={"a.py": "x = 1\n"}, task_description="t"),
        deduped=[],
        spec_compliance_single_pass=False,
    )

    assert result is None
    assert spec_calls == []


def test_run_spec_compliance_single_pass_flag_on_calls_synthesis(monkeypatch) -> None:
    """``spec_compliance_single_pass=True`` calls ``synthesize_spec_compliance`` exactly
    once with the ``deduped`` issues passed straight through, and returns its result."""
    import code_review_agent.coordinator as coord

    issue = CodeReviewIssue(
        severity="high",
        category="logic",
        file_path="a.py",
        line=1,
        description="finding",
    )

    spec_calls: list = []

    def _spec_spy(*args, **kwargs):
        spec_calls.append((args, kwargs))
        return "SPEC_GAP_MARKER: missing validation."

    monkeypatch.setattr(coord, "synthesize_spec_compliance", _spec_spy)

    dummy_llm = DummyLLMClient()
    input_data = CodeReviewInput(files={"a.py": "x = 1\n"}, task_description="t")
    result = coord._run_spec_compliance_single_pass(
        llm=dummy_llm,
        input_data=input_data,
        deduped=[issue],
        spec_compliance_single_pass=True,
    )

    assert result == "SPEC_GAP_MARKER: missing validation."
    assert len(spec_calls) == 1
    call_args, call_kwargs = spec_calls[0]
    assert call_args == (dummy_llm,), "llm must be forwarded to synthesize_spec_compliance"
    assert call_kwargs["input_data"] is input_data
    assert call_kwargs["issues"] == [issue]


def test_run_spec_compliance_single_pass_failure_returns_none(monkeypatch) -> None:
    """A raising ``synthesize_spec_compliance`` is caught and logged, not propagated;
    the helper returns ``None`` so the caller falls back to per-chunk notes."""
    import code_review_agent.coordinator as coord

    def _boom(*_args, **_kwargs):
        raise RuntimeError("spec-compliance single pass exploded")

    monkeypatch.setattr(coord, "synthesize_spec_compliance", _boom)

    result = coord._run_spec_compliance_single_pass(
        llm=DummyLLMClient(),
        input_data=CodeReviewInput(files={"a.py": "x = 1\n"}, task_description="t"),
        deduped=[],
        spec_compliance_single_pass=True,
    )

    assert result is None


def test_skip_tail_passes_short_circuits_with_no_llm_calls() -> None:
    """``input_data.skip_tail_passes=True`` returns the genuine issues unchanged
    with ``has_additive_findings`` False and no tail-pass LLM calls at all --
    the raising stand-in below proves neither pass touched the LLM."""

    class _RaisingLLM:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(
                f"tail passes must not touch the LLM when skipped (called {name!r})"
            )

    from code_review_agent.false_positive_filter import CodebaseIndex

    input_data = CodeReviewInput(files={"a.py": "x = 1\n"}, skip_tail_passes=True)
    genuine_issues = [
        CodeReviewIssue(
            severity="high",
            category="logic",
            file_path="a.py",
            description="bug",
            suggestion="fix",
        )
    ]
    index = CodebaseIndex.from_input(input_data, repo_reader=None)

    result = _run_tail_passes(
        llm=_RaisingLLM(),
        input_data=input_data,
        genuine_issues=genuine_issues,
        repo_reader=None,
        shared_index=index,
    )

    assert result == _TailPassResult(issues=genuine_issues, has_additive_findings=False)


class _NonDummyLLMClient(LLMClient, Model):
    """A strands ``Model`` + ``LLMClient`` that is not a ``DummyLLMClient``
    instance, forwarding every call to an inner scripted ``DummyLLMClient`` by
    composition (never inheritance). ``code_review_agent.model_resolution
    .resolve_code_review_model`` only honors an injected client verbatim when
    it already implements the strands ``Model`` interface (otherwise it
    silently substitutes the default production model), so this must
    implement ``Model`` too, not just ``LLMClient``. Used to exercise the
    production-client code path while chunk-review responses stay the
    deterministic canned ones from the inner scripted client."""

    def __init__(self, inner: DummyLLMClient) -> None:
        self._inner = inner

    def update_config(self, **model_config: Any) -> None:
        self._inner.update_config(**model_config)

    def get_config(self) -> dict[str, Any]:
        return self._inner.get_config()

    def structured_output(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.structured_output(*args, **kwargs)

    async def stream(self, *args: Any, **kwargs: Any):
        async for event in self._inner.stream(*args, **kwargs):
            yield event

    def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        return self._inner.complete_json(prompt, **kwargs)

    def complete(self, prompt: str, **kwargs: Any) -> str:
        return self._inner.complete(prompt, **kwargs)

    def chat(self, messages: list, **kwargs: Any) -> Any:
        return self._inner.chat(messages, **kwargs)

    def get_max_context_tokens(self) -> int:
        return self._inner.get_max_context_tokens()


def _noop_filter(llm, input_data, issues, repo_reader=None, index=None):
    """Shared stub: return findings unchanged (false-positive filter no-op)."""
    return list(issues)


def _noop_merged(llm, input_data, repo_reader=None, index=None):
    """Shared stub: merged architecture/side-effect pass finds nothing."""
    return [], []


def test_merged_pass_runs_before_false_positive_filter(monkeypatch) -> None:
    """The tail passes run sequentially in dependency order: the merged
    architecture/side-effect pass FIRST, then the false-positive filter -- so
    the additive findings are in the set the filter verifies. (The old code ran
    the two concurrently and appended the merged findings AFTER filtering, so
    they bypassed verification entirely.)"""
    import code_review_agent.coordinator as coord

    order: list[str] = []

    def _merged(llm, input_data, repo_reader=None, index=None):
        order.append("merged")
        return [], []

    def _filter(llm, input_data, issues, repo_reader=None, index=None):
        order.append("filter")
        return list(issues)

    monkeypatch.setattr(coord, "find_architecture_and_side_effect_issues", _merged)
    monkeypatch.setattr(coord, "filter_false_positives", _filter)

    result = run_coordinator(
        DummyLLMClient(),
        CodeReviewInput(files={"a.py": "x = 1\n"}, task_description="t"),
    )

    assert isinstance(result, CodeReviewOutput)
    assert order == ["merged", "filter"]


def test_additive_findings_are_false_positive_filtered(monkeypatch) -> None:
    """Regression for the FP-bypass bug: an architecture/side-effect finding now
    passes THROUGH the false-positive filter. A filter stub that confirms the
    architecture finding is a false positive (dropping it) must remove it from
    the output -- previously the merged findings were appended after the filter
    and such a false positive would be posted as a PR comment."""
    import code_review_agent.coordinator as coord
    from code_review_agent.models import CodeReviewIssue

    arch_fp = CodeReviewIssue(
        severity="high",
        category="architecture",
        file_path="a.py",
        description="Spurious: duplicates a service that does not actually exist.",
    )

    def _merged(llm, input_data, repo_reader=None, index=None):
        return [arch_fp], []

    def _drop_architecture(llm, input_data, issues, repo_reader=None, index=None):
        # Simulate the verifier confirming the architecture finding is a false
        # positive and dropping it, keeping every other finding.
        return [i for i in issues if i.category != "architecture"]

    monkeypatch.setattr(coord, "find_architecture_and_side_effect_issues", _merged)
    monkeypatch.setattr(coord, "filter_false_positives", _drop_architecture)

    result = run_coordinator(
        DummyLLMClient(),
        CodeReviewInput(files={"a.py": "x = 1\n"}, task_description="t"),
    )

    assert isinstance(result, CodeReviewOutput)
    assert all(i.category != "architecture" for i in result.issues), (
        "an additive finding confirmed as a false positive must be dropped by the filter"
    )


def test_run_coordinator_additive_findings_appear_for_dummy_and_nondummy(monkeypatch) -> None:
    """A production (non-``DummyLLMClient``) client and a bare ``DummyLLMClient``
    must produce a byte-identical merged ``CodeReviewOutput`` for the same input:
    the tail passes run the same way for both, and the additive
    architecture/side-effect findings survive combination + the (no-op here)
    false-positive filter and appear in the final output."""
    import code_review_agent.coordinator as coord
    from code_review_agent.models import CodeReviewIssue

    arch_issue = CodeReviewIssue(
        severity="medium",
        category="architecture",
        file_path="a.py",
        description="Duplicates the existing `Widget` service.",
    )
    side_effect_issue = CodeReviewIssue(
        severity="medium",
        category="side-effects",
        file_path="a.py",
        description="bar() no longer raises ValueError; app/caller.py still catches it.",
    )

    def _merged(llm, input_data, repo_reader=None, index=None):
        return [arch_issue], [side_effect_issue]

    monkeypatch.setattr(coord, "filter_false_positives", _noop_filter)
    monkeypatch.setattr(coord, "find_architecture_and_side_effect_issues", _merged)

    script = [
        {
            "approved": False,
            "issues": [
                {
                    "severity": "high",
                    "category": "logic",
                    "file_path": "a.py",
                    "line": 1,
                    "description": "Off-by-one error",
                    "suggestion": "Use range(n) not range(n + 1)",
                }
            ],
            "summary": "One issue found.",
            "spec_compliance_notes": "",
        }
    ]
    input_data = CodeReviewInput(files={"a.py": "x = 1\n"}, task_description="t")

    # Two independent scripted clients (fresh response cursors) so the
    # map-phase chunk review returns the exact same canned findings on both
    # runs -- only the tail-pass fan-out (Dummy-wrapped vs. not) differs.
    concurrent_result = run_coordinator(_NonDummyLLMClient(_ScriptedClient(script)), input_data)
    sequential_result = run_coordinator(_ScriptedClient(script), input_data)

    assert concurrent_result == sequential_result
    descriptions = {i.description for i in concurrent_result.issues}
    assert "Off-by-one error" in descriptions
    assert arch_issue.description in descriptions
    assert side_effect_issue.description in descriptions
