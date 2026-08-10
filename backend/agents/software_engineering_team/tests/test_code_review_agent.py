"""Tests for CodeReviewAgent (Strands-migrated).

``run`` always delegates to the map-reduce coordinator. Covers small and
large inputs through that path, plus the cases where a verdict contradicting
its own issues list (minor-only reject, zero-issue reject, approved-with-
critical-issue) now fails ``ChunkReviewLLMResponse`` schema validation
instead of being silently repaired by the coordinator's old
``_reconcile_approval`` safety net -- and the new-field propagation for
single-chunk reviews.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest
from code_review_agent import CodeReviewAgent
from code_review_agent.models import CodeReviewInput, CodeReviewOutput, CodeReviewUnavailableError

from llm_service.clients.dummy import DummyLLMClient


def _input(files: Optional[Dict[str, str]] = None, **overrides: Any) -> CodeReviewInput:
    if files is None:
        files = {"app/main.py": "def foo(): pass"}
    base = {
        "files": files,
        "task_description": "Add foo() helper",
        "language": "python",
    }
    base.update(overrides)
    return CodeReviewInput(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Single-call path
# ---------------------------------------------------------------------------


def test_small_code_returns_code_review_output() -> None:
    agent = CodeReviewAgent(llm_client=DummyLLMClient(), force_in_process=True)
    result = agent.run(_input())
    assert isinstance(result, CodeReviewOutput)
    # Dummy stub returns no issues + approved=True via the "senior code reviewer" branch.
    assert result.approved is True
    assert result.issues == []


def test_small_code_with_all_optional_fields_does_not_crash() -> None:
    """spec_content, task_requirements, acceptance_criteria, architecture,
    existing_codebase all plumbed through the builder."""
    from shared.dev_models.models import SystemArchitecture

    arch = SystemArchitecture(
        overview="Tiny service",
        architecture_document="# Arch",
        components=[],
        decisions=[],
        diagrams={},
    )
    agent = CodeReviewAgent(llm_client=DummyLLMClient(), force_in_process=True)
    result = agent.run(
        _input(
            task_requirements="Must support unicode",
            acceptance_criteria=["foo() exists", "foo() is public"],
            spec_content="Project spec: implement foo()",
            architecture=arch,
            existing_codebase="# prior state",
        )
    )
    assert isinstance(result, CodeReviewOutput)
    assert result.approved is True


# ---------------------------------------------------------------------------
# Coordinator (large code) path
# ---------------------------------------------------------------------------


def test_large_code_routes_through_coordinator() -> None:
    """Code larger than one map chunk is reviewed untruncated across multiple
    chunks. End-to-end with DummyLLMClient, the coordinator reviews each chunk
    and merges their output into one CodeReviewOutput."""
    files = {"app/main.py": "a" * 25_000, "app/util.py": "b" * 25_000}

    agent = CodeReviewAgent(llm_client=DummyLLMClient(), force_in_process=True)
    result = agent.run(_input(files=files))
    assert isinstance(result, CodeReviewOutput)
    assert result.approved is True
    # Dummy returns "Code review passed (dummy)." per chunk; coordinator
    # concatenates chunk summaries.
    assert "dummy" in result.summary.lower()


# ---------------------------------------------------------------------------
# _reconcile_approval safety net
# ---------------------------------------------------------------------------


class _StubClient(DummyLLMClient):
    """DummyLLMClient subclass that returns a canned response for every complete_json."""

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


def test_reconcile_low_only_reject_now_fails_schema_validation() -> None:
    """LLM flags approved=False with only low/info issues used to be overridden
    to True by the coordinator's ``_reconcile_approval`` safety net. That net
    only ever sees verdicts that already satisfy ``ChunkReviewLLMResponse``'s
    own consistency validator, which now requires an ``approved=False`` reply
    to carry an actionable critical/high issue -- a low-only rejection no
    longer reaches the coordinator at all, it fails schema validation and
    retries once. ``_input()`` is a single chunk, so the identical retry
    failure trips the coordinator's total-failure guard instead of ever
    producing a verdict to reconcile."""

    agent = CodeReviewAgent(
        llm_client=_StubClient(
            {
                "approved": False,
                "issues": [
                    {
                        "severity": "low",
                        "category": "naming",
                        "file_path": "app/main.py",
                        "description": "var name could be clearer",
                        "suggestion": "rename x to count",
                    },
                ],
                "summary": "One nit",
                "spec_compliance_notes": "",
            }
        ),
        force_in_process=True,
    )
    with pytest.raises(CodeReviewUnavailableError):
        agent.run(_input())


def test_reconcile_zero_issue_reject_with_summary_now_fails_schema_validation() -> None:
    """LLM returns approved=False with 0 issues but a non-empty summary used to
    be repaired by ``mapping._outcome_from_output`` into a synthesized
    high-severity issue built from the summary. ``ChunkReviewLLMResponse``'s
    consistency validator now rejects that shape at the schema layer instead
    (an ``approved=False`` verdict must already carry an actionable
    critical/high issue), so the reply fails validation and retries once.
    ``_input()`` is a single chunk, so the identical retry failure trips the
    coordinator's total-failure guard rather than fabricating a verdict for
    code that was never actually reviewed."""

    agent = CodeReviewAgent(
        llm_client=_StubClient(
            {
                "approved": False,
                "issues": [],
                "summary": "Code lacks error handling around DB calls",
                "spec_compliance_notes": "",
            }
        ),
        force_in_process=True,
    )
    with pytest.raises(CodeReviewUnavailableError):
        agent.run(_input())


def test_reconcile_zero_issue_zero_summary_reject_now_fails_schema_validation() -> None:
    """LLM returns approved=False with 0 issues AND 0 summary used to be
    auto-approved by the coordinator's ``_reconcile_approval`` safety net
    (preventing unresolvable loops). That net only ever sees verdicts that
    already satisfy ``ChunkReviewLLMResponse``'s own consistency validator,
    which now requires an ``approved=False`` reply to carry an actionable
    critical/high issue regardless of the summary -- this reply fails schema
    validation and retries once. ``_input()`` is a single chunk, so the
    identical retry failure trips the coordinator's total-failure guard
    instead of ever reaching the auto-approve safety net."""

    agent = CodeReviewAgent(
        llm_client=_StubClient(
            {"approved": False, "issues": [], "summary": "", "spec_compliance_notes": ""}
        ),
        force_in_process=True,
    )
    with pytest.raises(CodeReviewUnavailableError):
        agent.run(_input())


def test_multiple_run_calls_on_same_instance_succeed() -> None:
    """Regression: a single ``CodeReviewAgent`` instance must handle many
    ``run()`` calls in sequence. Each call builds a fresh review prompt and
    invokes ``complete_validated`` directly against the injected
    ``LLMClient``, so no persistent state from a previous review (message
    history, cached model, etc.) can leak into the next one.
    """
    agent = CodeReviewAgent(llm_client=DummyLLMClient(), force_in_process=True)
    for i in range(4):
        result = agent.run(_input(files={f"app/m{i}.py": f"def f{i}(): pass"}))
        assert isinstance(result, CodeReviewOutput)
        assert result.approved is True, f"run {i} failed: {result.summary}"


def test_small_code_routes_through_coordinator_chunk_path() -> None:
    """``run`` always delegates to the coordinator: even tiny inputs are
    reviewed through the chunk-review prompt, not a separate single-call path."""

    class _Recorder(DummyLLMClient):
        def __init__(self) -> None:
            super().__init__()
            self.prompts: list[str] = []

        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            self.prompts.append(prompt)
            return super().complete_json(prompt, **kwargs)

    client = _Recorder()
    agent = CodeReviewAgent(llm_client=client, force_in_process=True)
    result = agent.run(_input())
    assert result.approved is True
    # 1 chunk-review call + 1 side-effect/blast-radius pass call (additive,
    # runs once per submission regardless of chunk count).
    assert len(client.prompts) == 2
    # CHUNK_REVIEW_NOTE marker proves the coordinator's map path was used.
    assert "one chunk of the full codebase" in client.prompts[0]


def test_single_chunk_propagates_notes_through_agent() -> None:
    agent = CodeReviewAgent(
        llm_client=_StubClient(
            {
                "approved": True,
                "issues": [],
                "summary": "Looks good.",
                "spec_compliance_notes": "Meets the acceptance criteria.",
            }
        ),
        force_in_process=True,
    )
    result = agent.run(_input())
    assert result.approved is True
    assert result.spec_compliance_notes == "Meets the acceptance criteria."


def test_agent_accepts_files_dict_input() -> None:
    agent = CodeReviewAgent(llm_client=DummyLLMClient(), force_in_process=True)
    result = agent.run(
        CodeReviewInput(
            files={"app/main.py": "def foo(): pass"},
            task_description="Add foo() helper",
            language="python",
        )
    )
    assert isinstance(result, CodeReviewOutput)
    assert result.approved is True


def test_repo_root_defaults_none_and_round_trips_through_json() -> None:
    """``repo_root`` defaults to None, is JSON-native (so it survives the Temporal
    boundary), and does not disturb the code/files validator."""
    default = CodeReviewInput(files={"a.py": "x = 1\n"})
    assert default.repo_root is None

    with_root = CodeReviewInput(files={"a.py": "x = 1\n"}, repo_root="/tmp/checkout")
    dumped = with_root.model_dump(mode="json")
    assert dumped["repo_root"] == "/tmp/checkout"
    restored = CodeReviewInput.model_validate(dumped)
    assert restored.repo_root == "/tmp/checkout"
    assert restored.files == {"a.py": "x = 1\n"}


def test_repo_root_does_not_satisfy_code_or_files_requirement() -> None:
    """``repo_root`` is not a code source: an input with only ``repo_root`` and no
    files/code still fails the ``_require_code_or_files`` validator."""

    with pytest.raises(ValueError):
        CodeReviewInput(repo_root="/tmp/checkout")


def test_run_raises_unavailable_when_review_cannot_complete() -> None:
    """Contract: a review that cannot be completed raises — callers must treat
    it as a failed run, never as feedback for the coding agent."""

    from llm_service import LLMRateLimitError

    class _AlwaysRateLimited(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            raise LLMRateLimitError("429")

    agent = CodeReviewAgent(llm_client=_AlwaysRateLimited(), force_in_process=True)
    with pytest.raises(CodeReviewUnavailableError):
        agent.run(_input())


def test_reconcile_approved_true_with_critical_issue_now_fails_schema_validation() -> None:
    """LLM returns a critical issue with approved=True (the mirror image of
    the zero-issue-reject conflict above) used to be overridden to False by
    the coordinator's ``_reconcile_approval`` safety net. That net only ever
    sees verdicts that already satisfy ``ChunkReviewLLMResponse``'s own
    consistency validator, which now requires ``approved=True`` to carry NO
    actionable critical/high issue -- this contradictory reply fails schema
    validation and retries once. ``_input()`` is a single chunk, so the
    identical retry failure trips the coordinator's total-failure guard
    instead of ever reaching the reconcile safety net."""

    agent = CodeReviewAgent(
        llm_client=_StubClient(
            {
                "approved": True,  # deliberately wrong
                "issues": [
                    {
                        "severity": "critical",
                        "category": "logic",
                        "file_path": "app/main.py",
                        "description": "SQL injection in user query",
                        "suggestion": "Use parameterized queries",
                    },
                ],
                "summary": "LGTM",
                "spec_compliance_notes": "",
            }
        ),
        force_in_process=True,
    )
    with pytest.raises(CodeReviewUnavailableError):
        agent.run(_input())


# ---------------------------------------------------------------------------
# Progress callback reporting
# ---------------------------------------------------------------------------


def _recording_callback(calls: list) -> Any:
    """Build a ReviewProgressCallback that appends (step, detail, fraction) tuples."""

    def _cb(step: str, detail: str, fraction: float) -> None:
        calls.append((step, detail, fraction))

    return _cb


def test_run_reports_progress_steps_in_order() -> None:
    """Every review routes through the coordinator, which emits preparing →
    reviewing (per chunk) → finalizing → done with non-decreasing fractions
    ending at 1.0."""
    calls: list = []
    agent = CodeReviewAgent(llm_client=DummyLLMClient(), force_in_process=True)
    result = agent.run(_input(), progress_callback=_recording_callback(calls))
    assert result.approved is True
    steps = [c[0] for c in calls]
    assert steps[0] == "preparing"
    assert "reviewing" in steps
    assert "finalizing" in steps
    assert steps[-1] == "done"
    fractions = [c[2] for c in calls]
    assert fractions == sorted(fractions), "fractions must be non-decreasing"
    assert fractions[-1] == 1.0
    assert "approved=True" in calls[-1][1]


def test_no_callback_behaves_identically() -> None:
    """progress_callback=None (the default) must not change the review result."""
    agent = CodeReviewAgent(llm_client=DummyLLMClient(), force_in_process=True)
    with_cb_calls: list = []
    result_no_cb = agent.run(_input())
    result_with_cb = agent.run(_input(), progress_callback=_recording_callback(with_cb_calls))
    assert result_no_cb == result_with_cb
    assert with_cb_calls, "callback must have been invoked when provided"


def test_large_code_forwards_callback_to_coordinator() -> None:
    """Oversized code routes to the coordinator, which must keep reporting —
    including per-chunk 'chunk i/N' details."""
    files = {"app/main.py": "a" * 25_000, "app/util.py": "b" * 25_000}

    calls: list = []
    agent = CodeReviewAgent(llm_client=DummyLLMClient(), force_in_process=True)
    result = agent.run(_input(files=files), progress_callback=_recording_callback(calls))
    assert isinstance(result, CodeReviewOutput)
    details = [c[1] for c in calls]
    assert any("chunk 1/" in d for d in details), f"expected per-chunk reports, got {details}"
    assert calls[-1][0] == "done"
    assert calls[-1][2] == 1.0


def test_notify_review_progress_clamps_out_of_range_fraction(caplog):
    """An out-of-range fraction is a reporter bug: logged and clamped, never raised
    and never forwarded raw — progress reporting must not abort a review."""
    import logging

    from code_review_agent.models import notify_review_progress

    received = []
    with caplog.at_level(logging.WARNING):
        notify_review_progress(lambda s, d, f: received.append((s, d, f)), "reviewing", "x", 1.8)
        notify_review_progress(lambda s, d, f: received.append((s, d, f)), "reviewing", "y", -0.2)

    assert received == [("reviewing", "x", 1.0), ("reviewing", "y", 0.0)]
    assert sum("out of range" in r.message for r in caplog.records) == 2


def test_raising_callback_is_swallowed_and_never_changes_the_review(caplog):
    """A raising progress callback (e.g. a status-store outage behind the legacy
    v2 detail_callback bridge) is an observability bug: it must be logged and
    swallowed at the invocation boundary, never abort the review — the call
    sites' broad except would otherwise divert a healthy reviewer to the
    lower-fidelity LLM fallback."""
    import logging

    def _boom(step: str, detail: str, fraction: float) -> None:
        raise RuntimeError("store down")

    agent = CodeReviewAgent(llm_client=DummyLLMClient(), force_in_process=True)
    baseline = agent.run(_input())
    with caplog.at_level(logging.WARNING):
        result = agent.run(_input(), progress_callback=_boom)

    assert result == baseline
    assert any("callback failed (ignored" in r.message for r in caplog.records)
