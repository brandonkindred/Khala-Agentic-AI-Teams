"""Parity tests for backend_code_v2_team's migrated fallback review path.

The fallback (``_run_llm_review``, used when no external ``code_review_agent``
tool is configured, or it fails) now calls the shared engine's real
``code_review_agent.coordinator.run_coordinator`` directly instead of the
retired ``shared/llm_review.py``'s hand-rolled chunk/prompt/parse loop (see
``shared.v2_review.run_coordinator_llm_review``). ``tests/test_v2_review_phase.py``
already covers the call *contract* into ``run_coordinator`` by monkeypatching it
with a canned stub; every test here instead drives the real, unmocked
coordinator with a scripted ``DummyLLMClient``, so the whole
chunk -> map-phase LLM call -> merge -> translate chain is exercised for real.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from llm_service import LLMSemanticExhaustionError
from llm_service.clients.dummy import DummyLLMClient


@pytest.fixture(autouse=True)
def _clear_spec_compliance_pass_env(monkeypatch):
    """Guarantee determinism regardless of the ambient environment: an inherited
    ``CODE_REVIEW_SPEC_COMPLIANCE_PASS=1`` would add an extra post-dedupe
    synthesis LLM call even under ``skip_tail_passes=True`` (see
    ``CODE_REVIEW_SPEC_COMPLIANCE_PASS_ENV`` in ``coordinator.py``), breaking
    this file's exact ``call_count`` assertions."""
    monkeypatch.delenv("CODE_REVIEW_SPEC_COMPLIANCE_PASS", raising=False)


def _task(**overrides):
    from shared.dev_models.models import Task, TaskType

    base = dict(
        id="t1",
        type=TaskType.BACKEND,
        title="T",
        description="desc",
        requirements="reqs",
        assignee="backend",
        acceptance_criteria=["AC"],
    )
    base.update(overrides)
    return Task(**base)


class _ScriptedClient(DummyLLMClient):
    """Returns a different canned response on each ``complete_json`` call.

    Copied from ``test_code_review_coordinator.py``'s ``_ScriptedClient``: a
    real ``DummyLLMClient`` (not a mock of the coordinator), so it drives the
    coordinator's actual map-reduce pipeline. Adds ``call_count`` so a test
    can assert how many real LLM calls the pipeline made.
    """

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        super().__init__()
        self._responses = list(responses)
        self._idx = 0
        self.call_count = 0
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
        with self._lock:
            self.call_count += 1
            if self._idx < len(self._responses):
                resp = self._responses[self._idx]
                self._idx += 1
                return resp
            return self._responses[-1] if self._responses else {}


class _PromptCapturingClient(DummyLLMClient):
    """Records every chunk-review prompt; always returns a clean pass."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []
        self._lock = threading.Lock()

    def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            self.prompts.append(prompt)
        return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}


class _FailBadKeepGood(DummyLLMClient):
    """Fails any chunk touching ``bad.py``; returns a genuine issue for ``good.py``.

    Mirrors ``test_code_review_coordinator.py``'s
    ``test_semantic_exhaustion_multi_file_still_separates_files`` fixture
    (``_FailWhenBadPresent``), scripted with a real finding for the surviving
    file so the translated output is non-empty.
    """

    def __init__(self) -> None:
        super().__init__()
        self.call_count = 0
        self._lock = threading.Lock()

    def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            self.call_count += 1
        if "### bad.py ###" in prompt:
            raise LLMSemanticExhaustionError("no content", retry_thinking_level=False)
        if "### good.py ###" in prompt:
            return {
                "approved": False,
                "issues": [
                    {
                        "severity": "high",
                        "category": "logic",
                        "file_path": "good.py",
                        "description": "real issue",
                        "suggestion": "fix it",
                    }
                ],
                "summary": "found one",
                "spec_compliance_notes": "",
            }
        return super().complete_json(prompt, **kwargs)


class _AlwaysFail(DummyLLMClient):
    """Fails every chunk-review call, unconditionally."""

    def __init__(self) -> None:
        super().__init__()
        self.call_count = 0

    def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        self.call_count += 1
        raise LLMSemanticExhaustionError("no content", retry_thinking_level=False)


def test_run_llm_review_real_coordinator_single_chunk_translates_issue():
    """One small file, one real map-phase LLM call: the coordinator's
    ``CodeReviewIssue`` is translated into a ``ReviewIssue`` end-to-end
    (``suggestion`` -> ``recommendation``), with ``raw_issue_count`` staying
    ``None`` -- no monkeypatched ``run_coordinator`` involved."""
    from software_engineering_team.backend_code_v2_team.phases.review import _run_llm_review

    client = _ScriptedClient(
        [
            {
                "approved": False,
                "issues": [
                    {
                        "severity": "high",
                        "category": "logic",
                        "file_path": "x.py",
                        "description": "bad code",
                        "suggestion": "fix it",
                    }
                ],
                "summary": "found one",
                "spec_compliance_notes": "",
            }
        ]
    )

    out = _run_llm_review(llm=client, task=_task(), files={"x.py": "def f():\n    return 1\n"})

    assert len(out.issues) == 1
    issue = out.issues[0]
    assert issue.source == "code_review"
    assert issue.severity == "high"
    assert issue.description == "bad code"
    assert issue.file_path == "x.py"
    assert issue.recommendation == "fix it"
    assert out.raw_issue_count is None
    assert client.call_count == 1


def test_run_llm_review_real_coordinator_multi_chunk_attributes_issues_to_source_files():
    """Two files too large to share one map-phase chunk each get their own
    real LLM call, and the two returned issues are attributed to the correct
    source file -- proving real chunking, not a single stubbed call."""
    from software_engineering_team.backend_code_v2_team.phases.review import _run_llm_review

    file_a = "x" * 20_000
    file_b = "y" * 20_000
    client = _ScriptedClient(
        [
            {
                "approved": True,
                "issues": [
                    {
                        "severity": "medium",
                        "category": "logic",
                        "file_path": "a.py",
                        "description": "issue in a",
                        "suggestion": "fix a",
                    }
                ],
                "summary": "chunk a",
                "spec_compliance_notes": "",
            },
            {
                "approved": True,
                "issues": [
                    {
                        "severity": "medium",
                        "category": "logic",
                        "file_path": "b.py",
                        "description": "issue in b",
                        "suggestion": "fix b",
                    }
                ],
                "summary": "chunk b",
                "spec_compliance_notes": "",
            },
        ]
    )

    out = _run_llm_review(llm=client, task=_task(), files={"a.py": file_a, "b.py": file_b})

    assert {i.file_path for i in out.issues} == {"a.py", "b.py"}
    by_path = {i.file_path: i for i in out.issues}
    assert by_path["a.py"].description == "issue in a"
    assert by_path["b.py"].description == "issue in b"
    # 2 separate map-phase calls (one per oversized file) prove real chunking.
    assert client.call_count >= 2


def test_run_llm_review_real_coordinator_isolates_per_chunk_failure(monkeypatch):
    """One file's chunk review fails outright; its sibling still gets reviewed
    and its genuine issue still comes through -- the pipeline degrades that one
    file rather than aborting the whole review.

    This is the NEW shape, not a repeat of the retired ``shared/llm_review.py``'s
    "skip the failing chunk" behavior: the real coordinator records the failed
    file in ``CodeReviewOutput.not_reviewed_ranges``, but
    ``run_coordinator_llm_review`` only ever reads ``result.issues`` (see its
    docstring) -- so that range is silently absorbed at this boundary and never
    becomes a synthetic "could not be reviewed" issue here. What's testable at
    this integration point is exactly what's observable: no exception, and the
    surviving file's genuine finding.
    """
    monkeypatch.delenv("CODE_REVIEW_BLOCK_ON_UNREVIEWED", raising=False)
    from software_engineering_team.backend_code_v2_team.phases.review import _run_llm_review

    client = _FailBadKeepGood()

    out = _run_llm_review(
        llm=client,
        task=_task(),
        files={"bad.py": "def bad(): pass", "good.py": "def good(): pass"},
    )

    assert [i.file_path for i in out.issues] == ["good.py"]
    assert not any("could not be reviewed" in i.description for i in out.issues)
    assert not any("not reviewed" in i.description for i in out.issues)


def test_run_llm_review_real_coordinator_forwards_review_context_to_prompt():
    """``review_context`` (architecture + spec_content) reaches the actual
    chunk-review LLM prompt through the real coordinator -- the existing
    mocked test only proves the field lands on ``CodeReviewInput``, not that a
    real prompt renders it."""
    from shared.dev_models.models import ReviewContext, SystemArchitecture
    from software_engineering_team.backend_code_v2_team.phases.review import _run_llm_review

    client = _PromptCapturingClient()
    architecture = SystemArchitecture(overview="Layered service architecture.")
    review_context = ReviewContext(
        architecture=architecture, spec_content="All endpoints require auth."
    )

    _run_llm_review(
        llm=client,
        task=_task(),
        files={"x.py": "def f():\n    return 1\n"},
        review_context=review_context,
    )

    assert client.prompts, "expected at least one real chunk-review call"
    assert any("Layered service architecture." in p for p in client.prompts)
    assert any("All endpoints require auth." in p for p in client.prompts)


def test_run_llm_review_real_coordinator_oversized_single_line_file_is_reviewed():
    """A single unsplittable source line longer than the map-chunk budget is
    still reviewed, not silently dropped or crashed on.

    Unlike the retired ``shared/llm_review.py`` (which hard-split an oversized
    single line into bounded pieces via ``cap_review_chunk`` before calling the
    LLM), the real coordinator's chunk builder produces one oversized,
    unsplittable chunk for this case and sends it to the LLM as a single call
    -- ``cap_review_chunk``/``cap_chunk_content`` are not invoked from the
    map phase today (confirmed: they're only used by the QA/security fallback
    in ``shared/agent_review.py``). Parity here means "still reviewed, still
    correctly attributed" -- not "still hard-split".
    """
    from software_engineering_team.backend_code_v2_team.phases.review import _run_llm_review

    line = "DATA = '" + ("a" * 100_000) + "'"
    assert "\n" not in line  # unsplittable at a line boundary

    client = _ScriptedClient(
        [
            {
                "approved": True,
                "issues": [
                    {
                        "severity": "medium",
                        "category": "logic",
                        "file_path": "bundle.py",
                        "description": "minified data literal",
                        "suggestion": "extract constant",
                    }
                ],
                "summary": "found",
                "spec_compliance_notes": "",
            }
        ]
    )

    out = _run_llm_review(llm=client, task=_task(), files={"bundle.py": line})

    assert len(out.issues) == 1
    assert out.issues[0].file_path == "bundle.py"
    assert client.call_count == 1


def test_run_llm_review_real_coordinator_empty_files_raises_value_error():
    """``files={}`` raises ``ValueError`` -- an intentional divergence from the
    retired ``shared/llm_review.py``, which used to silently return an empty
    clean pass (``LlmReviewOutput([], 0)``) with no LLM call at all.
    ``run_coordinator_llm_review`` deliberately does not special-case this: it
    constructs ``CodeReviewInput`` directly, and that type's own fail-closed
    validation raises so a caller bug (e.g. a glob miss) never silently
    becomes an approved empty review (see its docstring's Preconditions)."""
    from software_engineering_team.backend_code_v2_team.phases.review import _run_llm_review

    client = _ScriptedClient([])

    with pytest.raises(ValueError):
        _run_llm_review(llm=client, task=_task(), files={})


def test_run_llm_review_real_coordinator_propagates_unavailable_when_all_chunks_fail():
    """A total coordinator failure (no chunk reviewable) propagates
    ``CodeReviewUnavailableError`` through the real pipeline -- the
    real-coordinator counterpart to the existing mocked
    ``test_run_llm_review_propagates_coordinator_unavailable``."""
    from software_engineering_team.backend_code_v2_team.phases.review import _run_llm_review
    from software_engineering_team.code_review_agent.models import CodeReviewUnavailableError

    client = _AlwaysFail()

    with pytest.raises(CodeReviewUnavailableError):
        _run_llm_review(llm=client, task=_task(), files={"x.py": "def f():\n    return 1\n"})
