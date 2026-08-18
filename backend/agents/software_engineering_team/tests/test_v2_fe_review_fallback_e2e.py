"""Parity tests for frontend_code_v2_team's migrated fallback review path.

The fallback (``_run_llm_review``, used when no external ``code_review_agent``
tool is configured, or it fails) now calls the shared engine's real
``code_review_agent.coordinator.run_coordinator`` directly instead of the
retired ``shared/llm_review.py``'s hand-rolled chunk/prompt/parse loop (see
``shared.v2_review.run_coordinator_llm_review``). ``tests/test_v2_fe_review_phase.py``
already covers the call *contract* into ``run_coordinator`` by monkeypatching it
with a canned stub; every test here instead drives the real, unmocked
coordinator with a scripted ``DummyLLMClient``, so the whole
chunk -> map-phase LLM call -> merge -> translate chain is exercised for real.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from tests.chunk_review_prompt_routing import (
    is_chunk_map_reasoning_prompt as _is_chunk_map_reasoning_prompt,
)

from llm_service import LLMSemanticExhaustionError
from llm_service.clients.dummy import DummyLLMClient


@pytest.fixture(autouse=True)
def _clear_ambient_code_review_env(monkeypatch):
    """Guarantee determinism regardless of the ambient environment:

    - An inherited ``CODE_REVIEW_SPEC_COMPLIANCE_PASS=1`` would add an extra
      post-dedupe synthesis LLM call even under ``skip_tail_passes=True``
      (see ``CODE_REVIEW_SPEC_COMPLIANCE_PASS_ENV`` in ``coordinator.py``),
      breaking this file's exact ``call_count`` assertions.
    - An inherited ``CODE_REVIEW_MAX_BISECT_DEPTH=0`` would disable the
      per-file bisection that
      ``test_run_llm_review_real_coordinator_isolates_per_chunk_failure``
      relies on to isolate ``bad.tsx``'s failure from ``good.tsx`` (both
      files are small enough to share one map chunk); without bisection, the
      combined chunk simply fails outright and the coordinator raises
      ``CodeReviewUnavailableError`` instead of degrading gracefully.
    """
    monkeypatch.delenv("CODE_REVIEW_SPEC_COMPLIANCE_PASS", raising=False)
    monkeypatch.delenv("CODE_REVIEW_MAX_BISECT_DEPTH", raising=False)


def _task(**overrides):
    from shared.dev_models.models import Task, TaskType

    base = dict(
        id="t1",
        type=TaskType.FRONTEND,
        title="T",
        description="desc",
        requirements="reqs",
        assignee="frontend",
        acceptance_criteria=["AC"],
    )
    base.update(overrides)
    return Task(**base)


class _ScriptedClient(DummyLLMClient):
    """Returns a different canned response on each formatting-pass
    ``complete_json`` call.

    Copied from ``test_code_review_coordinator.py``'s ``_ScriptedClient``: a
    real ``DummyLLMClient`` (not a mock of the coordinator), so it drives the
    coordinator's actual map-reduce pipeline. Adds ``call_count`` so a test
    can assert how many real formatting-pass LLM calls the pipeline made.

    Both the reasoning pass and the formatting pass land on
    ``complete_json`` now (see ``_is_chunk_map_reasoning_prompt``); only the
    formatting-pass call advances the scripted response cursor and
    ``call_count`` -- the reasoning-pass call gets the inherited dummy
    default, whose prose is discarded once wrapped for formatting.
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
        if _is_chunk_map_reasoning_prompt(prompt):
            return super().complete_json(
                prompt,
                temperature=temperature,
                system_prompt=system_prompt,
                tools=tools,
                think=think,
                **kwargs,
            )
        with self._lock:
            self.call_count += 1
            if self._idx < len(self._responses):
                resp = self._responses[self._idx]
                self._idx += 1
                return resp
            return self._responses[-1] if self._responses else {}


class _PerFileScriptedClient(DummyLLMClient):
    """Returns a response keyed by which file marker appears in the prompt.

    Unlike ``_ScriptedClient`` (which returns responses strictly by call
    order), this binds each canned response to the chunk it actually belongs
    to via the chunk's ``### path ###`` header. Real map-phase calls may run
    concurrently, so call order is not guaranteed to match file order; an
    order-based client would still pass an attribution assertion even if the
    coordinator mixed up which finding came from which file's content, since
    each canned response's ``file_path`` is a hard-coded value independent of
    the prompt it was served to. Selecting by marker closes that gap.

    Markers are matched on the reasoning-pass ``complete_json`` prompt
    (identified via ``_is_chunk_map_reasoning_prompt``); the formatting-pass
    ``complete_json`` wrap does not carry ``### path ###`` headers directly,
    but DOES carry the reasoning pass's own prose verbatim (wrapped in
    ``wrap_with_analysis_delimiters``'s "--- ANALYSIS" markers) -- real
    map-phase calls run the reasoning pass in a Strands ``Agent`` dispatched
    via ``asyncio.to_thread`` (a pooled worker thread) while the formatting
    pass runs synchronously back on the calling thread, so a
    ``threading.local`` cannot bridge state between the two calls for one
    chunk. Instead the reasoning-pass response echoes its own marker back
    (``{"summary": marker}``); ``chat()`` JSON-serializes that into the raw
    prose, which the formatting prompt then wraps verbatim, so the same
    marker is directly re-matchable there with no shared mutable state.
    """

    def __init__(self, responses_by_marker: dict[str, dict[str, Any]]) -> None:
        super().__init__()
        self._responses_by_marker = dict(responses_by_marker)
        self.call_count = 0
        self._lock = threading.Lock()

    def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        if not _is_chunk_map_reasoning_prompt(prompt):
            for marker, response in self._responses_by_marker.items():
                if marker in prompt:
                    return response
            return super().complete_json(prompt, **kwargs)
        with self._lock:
            self.call_count += 1
        for marker in self._responses_by_marker:
            if marker in prompt:
                return {"summary": f"Structured prose review summary. {marker}"}
        raise AssertionError(f"no scripted response matches prompt: {prompt[:200]!r}")


class _PromptCapturingClient(DummyLLMClient):
    """Records every chunk-review reasoning prompt; always returns a clean pass.

    Both the reasoning pass and the formatting pass land on ``complete_json``
    now (see ``_is_chunk_map_reasoning_prompt``), so recording happens there,
    gated to the reasoning-pass call only. The shared review-context prefix
    (spec excerpt / architecture overview / existing-codebase excerpt --
    see ``chunk_reviewer._build_shared_review_prefix``) is attached to the
    reasoning ``Agent``'s SYSTEM content, not the user-turn prompt, and
    ``DummyLLMClient.chat()`` forwards it to ``complete_json`` as the
    ``system_prompt`` kwarg rather than folding it into ``prompt`` -- so both
    are recorded together to keep this a faithful "what did the reasoning
    call actually see" capture.
    """

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []
        self._lock = threading.Lock()

    def complete_json(
        self, prompt: str, *, system_prompt: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        if _is_chunk_map_reasoning_prompt(prompt):
            with self._lock:
                self.prompts.append(f"{prompt}\n{system_prompt or ''}")
        return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}


class _FailBadKeepGood(DummyLLMClient):
    """Fails any chunk touching ``bad.tsx``; returns a genuine issue for ``good.tsx``.

    Mirrors ``test_code_review_coordinator.py``'s
    ``test_semantic_exhaustion_multi_file_still_separates_files`` fixture
    (``_FailWhenBadPresent``), scripted with a real finding for the surviving
    file so the translated output is non-empty.

    The reasoning pass (a Strands ``Agent`` dispatched via
    ``asyncio.to_thread``) and the formatting pass (a synchronous call back
    on the calling thread) do not share a thread, so a ``threading.local``
    handoff between them (as a prior version of this double used) silently
    breaks. Instead the reasoning pass's ``good.tsx`` response echoes a
    marker back; ``chat()`` JSON-serializes it into the raw prose, which the
    formatting prompt wraps verbatim, so the marker is directly
    re-matchable in the formatting-pass prompt with no shared mutable state.
    """

    _GOOD_FILE_MARKER = "GOOD_FILE_REVIEWED_MARKER"

    def __init__(self) -> None:
        super().__init__()
        self.call_count = 0
        self._lock = threading.Lock()

    def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        if not _is_chunk_map_reasoning_prompt(prompt):
            if self._GOOD_FILE_MARKER in prompt:
                return {
                    "approved": False,
                    "issues": [
                        {
                            "severity": "high",
                            "category": "logic",
                            "file_path": "good.tsx",
                            "description": "real issue",
                            "suggestion": "fix it",
                        }
                    ],
                    "summary": "found one",
                    "spec_compliance_notes": "",
                }
            return super().complete_json(prompt, **kwargs)
        with self._lock:
            self.call_count += 1
        if "### bad.tsx ###" in prompt:
            raise LLMSemanticExhaustionError("no content", retry_thinking_level=False)
        if "### good.tsx ###" in prompt:
            return {"summary": self._GOOD_FILE_MARKER}
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
    from software_engineering_team.frontend_code_v2_team.phases.review import _run_llm_review

    client = _ScriptedClient(
        [
            {
                "approved": False,
                "issues": [
                    {
                        "severity": "high",
                        "category": "logic",
                        "file_path": "x.tsx",
                        "description": "bad code",
                        "suggestion": "fix it",
                    }
                ],
                "summary": "found one",
                "spec_compliance_notes": "",
            }
        ]
    )

    out = _run_llm_review(llm=client, task=_task(), files={"x.tsx": "const f = () => 1;\n"})

    assert len(out.issues) == 1
    issue = out.issues[0]
    assert issue.source == "code_review"
    assert issue.severity == "high"
    assert issue.description == "bad code"
    assert issue.file_path == "x.tsx"
    assert issue.recommendation == "fix it"
    assert out.raw_issue_count is None
    assert client.call_count == 1


def test_run_llm_review_real_coordinator_multi_chunk_attributes_issues_to_source_files():
    """Two files too large to share one map-phase chunk each get their own
    real LLM call, and the two returned issues are attributed to the correct
    source file -- proving real chunking, not a single stubbed call.

    The scripted client selects its response by the ``### path ###`` marker
    in the prompt it actually receives (not by call order, which real
    concurrent map-phase calls do not guarantee), so the assertions below
    genuinely verify per-file attribution rather than merely echoing
    hard-coded ``file_path`` values back from whichever response landed on
    whichever call index."""
    from software_engineering_team.frontend_code_v2_team.phases.review import _run_llm_review

    file_a = "x" * 20_000
    file_b = "y" * 20_000
    client = _PerFileScriptedClient(
        {
            "### a.tsx ###": {
                "approved": True,
                "issues": [
                    {
                        "severity": "medium",
                        "category": "logic",
                        "file_path": "a.tsx",
                        "description": "issue in a",
                        "suggestion": "fix a",
                    }
                ],
                "summary": "chunk a",
                "spec_compliance_notes": "",
            },
            "### b.tsx ###": {
                "approved": True,
                "issues": [
                    {
                        "severity": "medium",
                        "category": "logic",
                        "file_path": "b.tsx",
                        "description": "issue in b",
                        "suggestion": "fix b",
                    }
                ],
                "summary": "chunk b",
                "spec_compliance_notes": "",
            },
        }
    )

    out = _run_llm_review(llm=client, task=_task(), files={"a.tsx": file_a, "b.tsx": file_b})

    assert {i.file_path for i in out.issues} == {"a.tsx", "b.tsx"}
    by_path = {i.file_path: i for i in out.issues}
    assert by_path["a.tsx"].description == "issue in a"
    assert by_path["b.tsx"].description == "issue in b"
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
    from software_engineering_team.frontend_code_v2_team.phases.review import _run_llm_review

    client = _FailBadKeepGood()

    out = _run_llm_review(
        llm=client,
        task=_task(),
        files={"bad.tsx": "const bad = () => {};\n", "good.tsx": "const good = () => {};\n"},
    )

    assert [i.file_path for i in out.issues] == ["good.tsx"]
    assert not any("could not be reviewed" in i.description for i in out.issues)
    assert not any("not reviewed" in i.description for i in out.issues)


def test_run_llm_review_real_coordinator_forwards_review_context_to_prompt():
    """``review_context`` (architecture + spec_content) reaches the actual
    chunk-review LLM prompt through the real coordinator -- the existing
    mocked test only proves the field lands on ``CodeReviewInput``, not that a
    real prompt renders it."""
    from shared.dev_models.models import ReviewContext, SystemArchitecture
    from software_engineering_team.frontend_code_v2_team.phases.review import _run_llm_review

    client = _PromptCapturingClient()
    architecture = SystemArchitecture(overview="Layered service architecture.")
    review_context = ReviewContext(
        architecture=architecture, spec_content="All endpoints require auth."
    )

    _run_llm_review(
        llm=client,
        task=_task(),
        files={"x.tsx": "const f = () => 1;\n"},
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
    from software_engineering_team.frontend_code_v2_team.phases.review import _run_llm_review

    line = "const DATA = '" + ("a" * 100_000) + "';"
    assert "\n" not in line  # unsplittable at a line boundary

    client = _ScriptedClient(
        [
            {
                "approved": True,
                "issues": [
                    {
                        "severity": "medium",
                        "category": "logic",
                        "file_path": "bundle.tsx",
                        "description": "minified data literal",
                        "suggestion": "extract constant",
                    }
                ],
                "summary": "found",
                "spec_compliance_notes": "",
            }
        ]
    )

    out = _run_llm_review(llm=client, task=_task(), files={"bundle.tsx": line})

    assert len(out.issues) == 1
    assert out.issues[0].file_path == "bundle.tsx"
    assert client.call_count == 1


def test_run_llm_review_real_coordinator_empty_files_raises_value_error():
    """``files={}`` raises ``ValueError`` -- an intentional divergence from the
    retired ``shared/llm_review.py``, which used to silently return an empty
    clean pass (``LlmReviewOutput([], 0)``) with no LLM call at all.
    ``run_coordinator_llm_review`` deliberately does not special-case this: it
    constructs ``CodeReviewInput`` directly, and that type's own fail-closed
    validation raises so a caller bug (e.g. a glob miss) never silently
    becomes an approved empty review (see its docstring's Preconditions)."""
    from software_engineering_team.frontend_code_v2_team.phases.review import _run_llm_review

    client = _ScriptedClient([])

    with pytest.raises(ValueError):
        _run_llm_review(llm=client, task=_task(), files={})


def test_run_llm_review_real_coordinator_propagates_unavailable_when_all_chunks_fail():
    """A total coordinator failure (no chunk reviewable) propagates
    ``CodeReviewUnavailableError`` through the real pipeline -- the
    real-coordinator counterpart to the existing mocked
    ``test_fe_run_llm_review_propagates_coordinator_unavailable``."""
    from software_engineering_team.code_review_agent.models import CodeReviewUnavailableError
    from software_engineering_team.frontend_code_v2_team.phases.review import _run_llm_review

    client = _AlwaysFail()

    with pytest.raises(CodeReviewUnavailableError):
        _run_llm_review(llm=client, task=_task(), files={"x.tsx": "const f = () => 1;\n"})


def test_run_llm_review_real_coordinator_forwards_accessibility_note_to_prompt():
    """Frontend's ``_ACCESSIBILITY_VERIFY_NOTE`` (restoring the retired
    ``REVIEW_PROMPT``'s explicit accessibility-verification guidance) reaches
    the real chunk-review LLM prompt via ``extra_task_requirements`` -- the
    existing mocked test only proves it lands on
    ``CodeReviewInput.task_requirements``, not that a real prompt renders it."""
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import _run_llm_review

    client = _PromptCapturingClient()

    _run_llm_review(llm=client, task=_task(), files={"x.tsx": "const f = () => 1;\n"})

    assert client.prompts, "expected at least one real chunk-review call"
    assert any(review_mod._ACCESSIBILITY_VERIFY_NOTE in p for p in client.prompts)
