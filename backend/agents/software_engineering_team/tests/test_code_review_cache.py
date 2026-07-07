"""Tests for the coordinator's map-phase outcome cache.

The review→fix→re-review loop re-invokes ``run_coordinator`` after every batch
fix, but a fix only mutates the files that had issues. The cache reuses the
prior map-phase ``_ChunkOutcome`` for any chunk whose LLM input and context are
byte-identical, so only the touched chunks go back through the model. These
tests pin that behavior: hits skip the LLM, changed chunks (or a changed
profile / task context / model) miss, cached outcomes reproduce identical
findings, degraded outcomes are never cached, and the size-0 disable switch is a
pure passthrough.

The false-positive verification pass is disabled (``skip_false_positive_filter``)
so no post-map LLM calls muddy the count. Map-phase chunk reviews are the calls
the cache skips; they are the only calls carrying the coordinator's
``**Code to review:**`` marker, so ``map_calls`` counts exactly those and ignores
the reduce-phase synthesis pass (which fires whenever a run has >1 sub-review).

The process-global cache is cleared around every test by the autouse
``_reset_code_review_chunk_cache`` fixture in ``conftest.py``.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future
from typing import Any, Dict, List

import pytest
from code_review_agent import coordinator as coord
from code_review_agent import mapping
from code_review_agent.chunk_reviewer import CODE_TO_REVIEW_HEADER
from code_review_agent.coordinator import run_coordinator
from code_review_agent.models import (
    CodeReviewInput,
    CodeReviewUnavailableError,
    FileSegment,
    ReviewChunk,
    ReviewProfile,
)

from llm_service import LLMRateLimitError, LLMSemanticExhaustionError, LLMTruncatedError
from llm_service.clients.dummy import DummyLLMClient
from software_engineering_team.shared.context_sizing import (
    compute_code_review_sibling_surface_chars,
)

# The coordinator's chunk-review prompt is the only LLM call carrying this
# header (see ``chunk_reviewer._run_chunk_review``); the reduce-phase synthesis
# pass does not, so counting it isolates map-phase reviews. Sourced from the
# chunk-reviewer module so a prompt-template change can't silently break the
# count.
_MAP_MARKER = CODE_TO_REVIEW_HEADER


class _CountingClient(DummyLLMClient):
    """Returns a fixed canned response; counts total and map-phase calls.

    Thread-safe: map calls may run in parallel across chunks.
    """

    def __init__(self, response: Dict[str, Any]) -> None:
        super().__init__()
        self._response = response
        self._lock = threading.Lock()
        self.calls = 0
        self.map_calls = 0

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        with self._lock:
            self.calls += 1
            if _MAP_MARKER in prompt:
                self.map_calls += 1
        return dict(self._response)


class _SwitchingClient(DummyLLMClient):
    """Returns a different response on each call; counts map-phase calls.

    Lets a test prove a cache hit did *not* consult the model: a second run that
    hit the cache never advances past the first response.
    """

    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        super().__init__()
        self._responses = list(responses)
        self._idx = 0
        self._lock = threading.Lock()
        self.map_calls = 0

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        with self._lock:
            if _MAP_MARKER in prompt:
                self.map_calls += 1
            resp = self._responses[min(self._idx, len(self._responses) - 1)]
            self._idx += 1
            return dict(resp)


class _FailOnMarkerClient(DummyLLMClient):
    """Raises a content failure on chunks containing ``fail_marker`` while ``fail``.

    ``fail`` starts True and can be flipped to heal the client; the same instance
    is reused across runs so the model fingerprint (and thus the cache key for a
    clean sibling chunk) stays stable.
    """

    def __init__(self, fail_marker: str) -> None:
        super().__init__()
        self._fail_marker = fail_marker
        self._lock = threading.Lock()
        self.map_calls = 0
        self.fail = True

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        with self._lock:
            if _MAP_MARKER in prompt:
                self.map_calls += 1
        if self.fail and self._fail_marker in prompt:
            raise LLMSemanticExhaustionError("no verdict")
        return dict(_APPROVED)


_APPROVED = {"approved": True, "issues": [], "summary": "OK"}


def _one_file_input(content: str = "def f():\n    return 1\n", **overrides: Any) -> CodeReviewInput:
    kwargs: Dict[str, Any] = {
        "files": {"app/a.py": content},
        "task_description": "Add feature",
        "language": "python",
        "skip_false_positive_filter": True,
    }
    kwargs.update(overrides)
    return CodeReviewInput(**kwargs)


def _two_file_input(a: str, b: str, **overrides: Any) -> CodeReviewInput:
    # ~12k each so the two files land in separate chunks: two blocks over the
    # per-chunk budget cannot be grouped, and neither is large enough to split.
    kwargs: Dict[str, Any] = {
        "files": {"app/a.py": a, "app/b.py": b},
        "task_description": "Add feature",
        "language": "python",
        "skip_false_positive_filter": True,
    }
    kwargs.update(overrides)
    return CodeReviewInput(**kwargs)


def test_identical_rerun_hits_cache_and_skips_map_llm() -> None:
    """A byte-identical second run issues zero new map-phase LLM calls."""
    client = _CountingClient(_APPROVED)
    data = _one_file_input()

    first = run_coordinator(client, data)
    assert client.map_calls == 1  # one chunk, one map call

    second = run_coordinator(client, data)
    assert client.map_calls == 1  # no new map call — served from cache

    assert first.approved is second.approved is True
    assert [i.model_dump() for i in first.issues] == [i.model_dump() for i in second.issues]


def test_only_changed_chunk_is_re_reviewed() -> None:
    """Mutating one file re-reviews only its chunk; the other stays cached."""
    client = _CountingClient(_APPROVED)
    a = "x" * 12_000
    b = "y" * 12_000

    run_coordinator(client, _two_file_input(a, b))
    assert client.map_calls == 2  # two chunks, two map calls

    # Change only file b's content; file a's chunk is byte-identical.
    run_coordinator(client, _two_file_input(a, b + "z"))
    assert client.map_calls == 3  # exactly one new map call (the changed chunk)


def test_changed_profile_invalidates_cache() -> None:
    """Identical code but a different review profile forces a miss."""
    client = _CountingClient(_APPROVED)

    run_coordinator(client, _one_file_input(profile=ReviewProfile.CODE_REVIEW))
    assert client.map_calls == 1

    run_coordinator(client, _one_file_input(profile=ReviewProfile.SPEC_CONFORMANCE))
    assert client.map_calls == 2


def test_changed_task_context_invalidates_cache() -> None:
    """Identical code but a different task description forces a miss."""
    client = _CountingClient(_APPROVED)

    run_coordinator(client, _one_file_input(task_description="Task one"))
    assert client.map_calls == 1

    run_coordinator(client, _one_file_input(task_description="Task two"))
    assert client.map_calls == 2


def test_changed_model_invalidates_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """A changed resolved-model fingerprint forces a miss for identical code."""
    client = _CountingClient(_APPROVED)
    data = _one_file_input()

    monkeypatch.setattr(coord, "_review_model_fingerprint", lambda _llm: "model-A")
    run_coordinator(client, data)
    assert client.map_calls == 1

    monkeypatch.setattr(coord, "_review_model_fingerprint", lambda _llm: "model-B")
    run_coordinator(client, data)
    assert client.map_calls == 2


def test_cache_hit_reproduces_findings_without_consulting_model() -> None:
    """A hit reuses the stored findings even if the model would now differ."""
    high_issue = {
        "approved": False,
        "issues": [
            {
                "severity": "high",
                "category": "logic",
                "file_path": "app/a.py",
                "line": 1,
                "description": "Off-by-one",
                "suggestion": "Fix the bound",
            }
        ],
        "summary": "Needs work",
    }
    # Second (and later) responses differ — a hit must never surface them.
    client = _SwitchingClient([high_issue, _APPROVED])
    data = _one_file_input()

    first = run_coordinator(client, data)
    assert client.map_calls == 1
    assert first.approved is False
    assert len(first.issues) == 1

    second = run_coordinator(client, data)
    assert client.map_calls == 1  # not consulted again
    assert second.approved is first.approved
    assert [i.model_dump() for i in second.issues] == [i.model_dump() for i in first.issues]


def test_degraded_outcome_is_not_cached() -> None:
    """A degraded chunk is retried for real next cycle; a clean sibling is cached."""
    a = "A" * 12_000  # reviews cleanly
    b = "BBBB" + "B" * 12_000  # its chunk fails while the client is unhealthy

    # Same instance across both runs so the model fingerprint stays stable and
    # chunk a's cache key is unchanged.
    client = _FailOnMarkerClient(fail_marker="BBBB")
    degraded = run_coordinator(client, _two_file_input(a, b))
    calls_after_degraded = client.map_calls
    assert degraded.approved is False  # the not-reviewed finding blocks the merge
    assert any("could not be reviewed" in i.description for i in degraded.issues)

    # Heal the client and re-run identical input: chunk a is a cache hit (no new
    # call); chunk b was degraded so nothing was cached for it → exactly one new
    # call, which now succeeds. Asserting the *delta* (not an absolute count)
    # keeps the test robust to changes in the recovery retry/bisection logic.
    client.fail = False
    result = run_coordinator(client, _two_file_input(a, b))
    assert client.map_calls == calls_after_degraded + 1  # only the degraded chunk b
    assert result.approved is True


class _FailFullThenBisectClient(DummyLLMClient):
    """Fails the full chunk (both markers present) but approves each bisected half.

    Simulates a recoverable content failure on the full-chunk input that only
    succeeds once _review_chunk_with_recovery line-splits it: after the split, no
    single half carries both markers, so each half is approved. Uses
    ``LLMTruncatedError`` (finish_reason=length) — the content failure that still
    line-splits a single file; semantic exhaustion deliberately does not.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.map_calls = 0

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        with self._lock:
            if _MAP_MARKER in prompt:
                self.map_calls += 1
        if "S_MARK_START" in prompt and "E_MARK_END" in prompt:
            raise LLMTruncatedError("full chunk too big", finish_reason="length")
        return dict(_APPROVED)


def test_bisected_recovery_outcome_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """A chunk that only succeeds via bisection is re-attempted next cycle, not cached."""
    # Isolate the chunk cache: the submission-level short-circuit would otherwise
    # serve the (approved) second run before chunking, masking chunk-cache behavior.
    monkeypatch.setenv("CODE_REVIEW_SUBMISSION_CACHE_SIZE", "0")
    # Lower the bisect floor so a modest single chunk (well under the map budget,
    # so it isn't pre-split) is still large enough to bisect during recovery.
    monkeypatch.setenv("CODE_REVIEW_MIN_SPLIT_SEGMENT_CHARS", "2000")
    body = "y = 1\n" * 1_600  # ~9.6k chars across thousands of lines (>= 2 x 2000)
    content = "S_MARK_START\n" + body + "E_MARK_END\n"
    data = _one_file_input(content=content)

    client = _FailFullThenBisectClient()
    first = run_coordinator(client, data)
    before = client.map_calls
    assert before >= 3  # full chunk (fails) + the >=2 bisected halves (succeed)
    assert first.approved is True  # halves approved; no not-reviewed finding

    # Identical re-run: the bisected aggregate must NOT have been cached, so the
    # full chunk is re-attempted (fails, bisects) again — the same work repeats
    # rather than a 0-call cache hit. (A cache hit would leave map_calls == before.)
    second = run_coordinator(client, data)
    assert client.map_calls == 2 * before
    assert second.approved is True


def test_cache_disabled_via_env_is_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """Size 0 disables the cache: every run re-invokes the model, as before."""
    monkeypatch.setenv("CODE_REVIEW_CHUNK_OUTCOME_CACHE_SIZE", "0")
    # Also disable the submission-level short-circuit so this isolates the chunk
    # cache's passthrough rather than the coarser identical-submission skip.
    monkeypatch.setenv("CODE_REVIEW_SUBMISSION_CACHE_SIZE", "0")
    client = _CountingClient(_APPROVED)
    data = _one_file_input()

    run_coordinator(client, data)
    assert client.map_calls == 1
    run_coordinator(client, data)
    assert client.map_calls == 2  # no caching — second run calls the model again


def test_model_fingerprint_prefers_model_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fingerprint reads a resolved model's id attribute when present."""

    class _Model:
        model_id = "claude-x"

    monkeypatch.setattr(mapping, "resolve_code_review_model", lambda _llm: _Model())
    assert mapping._review_model_fingerprint(object()) == "claude-x"


def test_model_fingerprint_falls_back_to_config_then_typename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no id attributes it reads ``config['model']``, else the type name."""

    class _ConfigModel:
        config = {"model": "cfg-model"}

    monkeypatch.setattr(mapping, "resolve_code_review_model", lambda _llm: _ConfigModel())
    assert mapping._review_model_fingerprint(object()) == "cfg-model"

    class _Bare:
        pass

    monkeypatch.setattr(mapping, "resolve_code_review_model", lambda _llm: _Bare())
    assert mapping._review_model_fingerprint(object()) == "_Bare"


def test_model_fingerprint_falls_back_when_resolution_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolution failure never raises; it falls back to the client type name."""

    def _boom(_llm: Any) -> Any:
        raise RuntimeError("no model")

    monkeypatch.setattr(mapping, "resolve_code_review_model", _boom)
    assert mapping._review_model_fingerprint(_CountingClient(_APPROVED)) == "_CountingClient"


class _PromptCapturingClient(DummyLLMClient):
    """Records every map-phase prompt; returns a fixed approval."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.map_prompts: List[str] = []

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        with self._lock:
            if _MAP_MARKER in prompt:
                self.map_prompts.append(prompt)
        return dict(_APPROVED)


def _defined_file(symbol: str, body: str = "1") -> str:
    """A ~12k Python file defining ``symbol`` (its own chunk; has a surface)."""
    return f"def {symbol}():\n    return {body}\n" + ("# pad " + "x" * 12_000)


# ---------------------------------------------------------------------------
# Cross-file "sibling surface" mitigation
# ---------------------------------------------------------------------------


def test_symbol_surface_extracts_py_and_ts_symbols() -> None:
    """Python def/class and TS export bindings (incl. export lists) are found."""
    py = "def foo():\n    pass\n\nclass Bar:\n    pass\n"
    assert coord._symbol_surface(py) == ["Bar", "foo"]

    # Trailing comma / blank entry in the export list is skipped, not crashed on.
    ts = "export function alpha() {}\nexport const beta = 1\nexport { gamma, delta as epsilon, }\n"
    assert coord._symbol_surface(ts) == ["alpha", "beta", "epsilon", "gamma"]


def test_symbol_surface_excludes_indented_python_defs() -> None:
    """Only column-zero def/class count; indented methods/nested defs are not
    top-level symbols and must not be advertised as siblings."""
    content = (
        "class C:\n"
        "    def method(self):\n"  # indented → not top-level
        "        def nested():\n"  # nested → not top-level
        "            pass\n"
        "def top():\n"  # column-zero → top-level
        "    pass\n"
    )
    assert coord._symbol_surface(content) == ["C", "top"]


def test_half_sibling_surface_falls_back_without_map() -> None:
    """With no surface map (a direct caller), a bisected half keeps the parent's surface."""
    from code_review_agent.models import FileSegment, ReviewChunk

    half = ReviewChunk(segments=[FileSegment(path="app/a.py", content="def a(): pass")])
    assert mapping._half_sibling_surface(half, None, "parent surface") == "parent surface"
    # With the map available it recomputes for the half instead.
    surface = {"app/a.py": ["a"], "app/b.py": ["foo"]}
    assert mapping._half_sibling_surface(half, surface, "parent surface") == "app/b.py: foo"


def test_sibling_surface_is_capped_to_the_shared_limit() -> None:
    """A large sibling surface is truncated to the shared cap, so the cache key
    hashes exactly the (capped) bytes the prompt will carry."""
    from code_review_agent.models import FileSegment, ReviewChunk

    surface = {f"app/f{i}.py": [f"sym{j}" for j in range(40)] for i in range(200)}
    chunk = ReviewChunk(segments=[FileSegment(path="app/self.py", content="x")])
    out = coord._sibling_surface(chunk, surface)
    assert len(out) <= compute_code_review_sibling_surface_chars()


def test_sibling_surface_cap_is_env_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """CODE_REVIEW_SIBLING_SURFACE_CHARS tunes the cap without code changes."""
    from code_review_agent.models import FileSegment, ReviewChunk

    surface = {f"app/f{i}.py": [f"sym{j}" for j in range(40)] for i in range(200)}
    chunk = ReviewChunk(segments=[FileSegment(path="app/self.py", content="x")])

    monkeypatch.setenv("CODE_REVIEW_SIBLING_SURFACE_CHARS", "50")
    assert len(coord._sibling_surface(chunk, surface)) <= 50

    monkeypatch.setenv("CODE_REVIEW_SIBLING_SURFACE_CHARS", "0")
    assert coord._sibling_surface(chunk, surface) == ""  # 0 drops the block


def test_surface_by_path_skips_headerless_and_symbolless() -> None:
    """Only named blocks with a non-empty surface appear in the map."""
    blocks = [
        ("app/a.py", "def a():\n    pass\n"),
        ("app/blank.py", "x = 1  # no top-level def/class/export\n"),
        ("", "def orphan():\n    pass\n"),  # headerless → skipped
    ]
    surface = coord._surface_by_path(blocks)
    assert surface == {"app/a.py": ["a"]}


def test_sibling_surface_excludes_own_chunk_paths() -> None:
    """A chunk sees only the *other* files' surfaces, path-sorted."""
    from code_review_agent.models import FileSegment, ReviewChunk

    chunk = ReviewChunk(segments=[FileSegment(path="app/a.py", content="def a(): pass")])
    surface = {"app/a.py": ["a"], "app/b.py": ["foo"], "app/c.py": ["bar"]}
    assert coord._sibling_surface(chunk, surface) == "app/b.py: foo\napp/c.py: bar"


def test_sibling_surface_appears_in_chunk_prompt() -> None:
    """File A's reviewer prompt lists the symbols its sibling B defines."""
    client = _PromptCapturingClient()
    data = _two_file_input(_defined_file("a_func"), _defined_file("foo"))

    run_coordinator(client, data)

    # Select A's own prompt by its code (B's prompt also mentions "a_func" in its
    # sibling-surface block, so match the definition, not the bare name).
    a_prompt = next(p for p in client.map_prompts if "def a_func" in p)
    assert "app/b.py: foo" in a_prompt
    assert "Other files changed in this submission" in a_prompt


class _FailFullCaptureHalvesClient(DummyLLMClient):
    """Fails the combined multi-file chunk (both defs present); captures & approves
    each bisected half (only one def present)."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.map_prompts: List[str] = []

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        with self._lock:
            if _MAP_MARKER in prompt:
                self.map_prompts.append(prompt)
        if "def a_func" in prompt and "def b_func" in prompt:
            raise LLMSemanticExhaustionError("combined chunk too big")
        return dict(_APPROVED)


def test_bisected_multifile_chunk_recomputes_sibling_surface() -> None:
    """When a two-file chunk bisects, each half is given the *other* file's surface.

    The combined chunk excluded both files from its sibling surface; after the
    split each half must recompute so the half reviewing app/a.py now sees
    app/b.py's exported symbols (the cross-file break vector).
    """
    # Small files so both land in a single chunk (a multi-segment chunk bisects
    # by segment regardless of size).
    a = "def a_func():\n    return b_func()\n"
    b = "def b_func():\n    return 1\n"
    client = _FailFullCaptureHalvesClient()

    run_coordinator(client, _two_file_input(a, b))

    # The half reviewing a.py carries 'def a_func' but not 'def b_func'; with the
    # recompute it now lists b.py's surface (before the fix it inherited the
    # combined chunk's empty sibling surface).
    a_half = next(p for p in client.map_prompts if "def a_func" in p and "def b_func" not in p)
    assert "app/b.py: b_func" in a_half


def test_sibling_rename_invalidates_dependent_chunk() -> None:
    """Renaming a symbol in B re-reviews the unchanged dependent chunk A."""
    client = _CountingClient(_APPROVED)
    a = _defined_file("a_func")

    run_coordinator(client, _two_file_input(a, _defined_file("foo")))
    assert client.map_calls == 2  # A and B

    # B renames foo -> bar: B's content changes (miss) AND A's sibling surface
    # changes (foo -> bar), so A is re-reviewed too.
    run_coordinator(client, _two_file_input(a, _defined_file("bar")))
    assert client.map_calls == 4


def test_sibling_body_only_edit_keeps_dependent_cached() -> None:
    """A body-only change to B leaves B's surface — and A's cache — intact."""
    client = _CountingClient(_APPROVED)
    a = _defined_file("a_func")

    run_coordinator(client, _two_file_input(a, _defined_file("foo", body="1")))
    assert client.map_calls == 2

    # B's body changes but its surface (def foo) does not: B misses, A stays cached.
    run_coordinator(client, _two_file_input(a, _defined_file("foo", body="2")))
    assert client.map_calls == 3


# ---------------------------------------------------------------------------
# Single-flight de-duplication of concurrent identical chunks
# ---------------------------------------------------------------------------
#
# The waiter/leader branches are covered deterministically without real thread
# races: pre-seeding a resolved ``Future`` in the in-flight registry exercises the
# waiter path (``future.result()``), and a reviewer that raises drives the leader
# failure path. The leader create-resolve-release path is covered by every solo
# ``run_coordinator`` test above. Mutual exclusion of two real leaders is
# guaranteed by the cache lock, so it needs no timing-dependent test.


class _NeverRuns:
    """Reviewer stand-in that must never be invoked — the waiter path must not review."""

    def __init__(self) -> None:
        self.calls = 0

    def run(self, chunk_input: Any) -> Any:
        self.calls += 1
        raise AssertionError("reviewer.run must not be called on the waiter path")


class _RaisingReviewer:
    """Reviewer whose ``run`` raises, to drive the leader failure path."""

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.calls = 0

    def run(self, chunk_input: Any) -> Any:
        self.calls += 1
        raise self._error


def _single_chunk() -> ReviewChunk:
    return ReviewChunk(segments=[FileSegment(path="app/a.py", content="def f():\n    return 1\n")])


def _simple_outcome() -> mapping._ChunkOutcome:
    return mapping._ChunkOutcome(approved_flags=[True], summaries=["ok"])


def test_leader_review_failure_releases_slot_and_reraises() -> None:
    """A leader whose review fails re-raises, caches nothing, and frees the slot."""
    reviewer = _RaisingReviewer(LLMRateLimitError("rate limited"))
    chunk = _single_chunk()
    base_input: Dict[str, Any] = {"task_description": "t", "language": "python"}
    context_fp = "fp"
    key = mapping._chunk_cache_key(chunk, context_fp, "")

    # An infra failure surfaces as CodeReviewUnavailableError from the reviewer.
    with pytest.raises(CodeReviewUnavailableError):
        mapping._cached_review_chunk(reviewer, chunk, base_input, context_fp)

    assert reviewer.calls == 1
    assert mapping._CHUNK_INFLIGHT == {}  # slot released on the failure path
    assert key not in mapping._CHUNK_OUTCOME_CACHE  # failure is never cached


def test_waiter_reuses_resolved_inflight_result() -> None:
    """A waiter reads a resolved in-flight future's outcome without reviewing."""
    chunk = _single_chunk()
    base_input: Dict[str, Any] = {"task_description": "t", "language": "python"}
    context_fp = "fp"
    key = mapping._chunk_cache_key(chunk, context_fp, "")
    published = _simple_outcome()
    fut: Future = Future()
    fut.set_result(published)
    mapping._CHUNK_INFLIGHT[key] = fut  # simulate a leader already in flight

    reviewer = _NeverRuns()
    got = mapping._cached_review_chunk(reviewer, chunk, base_input, context_fp)

    assert reviewer.calls == 0  # the waiter never fires its own review
    assert got.approved_flags == published.approved_flags
    assert got is not published  # each caller owns an independent clone


def test_waiter_reraises_resolved_inflight_exception() -> None:
    """A waiter re-raises the leader's stored exception without reviewing."""
    chunk = _single_chunk()
    base_input: Dict[str, Any] = {"task_description": "t", "language": "python"}
    context_fp = "fp"
    key = mapping._chunk_cache_key(chunk, context_fp, "")
    fut: Future = Future()
    fut.set_exception(CodeReviewUnavailableError("leader failed"))
    mapping._CHUNK_INFLIGHT[key] = fut

    reviewer = _NeverRuns()
    with pytest.raises(CodeReviewUnavailableError):
        mapping._cached_review_chunk(reviewer, chunk, base_input, context_fp)
    assert reviewer.calls == 0


def test_lru_evicts_oldest_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past capacity, the oldest entry is evicted and re-reviewed on return."""
    monkeypatch.setenv("CODE_REVIEW_CHUNK_OUTCOME_CACHE_SIZE", "1")
    # Disable the submission-level short-circuit so identical reruns exercise the
    # chunk cache's LRU eviction rather than being served whole from above.
    monkeypatch.setenv("CODE_REVIEW_SUBMISSION_CACHE_SIZE", "0")
    client = _CountingClient(_APPROVED)

    a = _one_file_input(task_description="A")  # distinct context → distinct key
    b = _one_file_input(task_description="B")

    run_coordinator(client, a)  # map_calls=1, caches A
    run_coordinator(client, b)  # map_calls=2, caches B, evicts A (capacity 1)
    run_coordinator(client, b)  # map_calls=2, B still cached (hit)
    assert client.map_calls == 2
    run_coordinator(client, a)  # map_calls=3, A was evicted → miss
    assert client.map_calls == 3


# ---------------------------------------------------------------------------
# Submission-level short-circuit + changed-files scoping
# ---------------------------------------------------------------------------

# A rejecting response (blocking ``high``) so the submission is *not* approved
# and therefore never stored in the submission-level cache.
_REJECTED = {
    "approved": False,
    "issues": [
        {
            "severity": "high",
            "category": "correctness",
            "file_path": "app/a.py",
            "description": "Missing input validation",
            "suggestion": "Validate inputs",
        }
    ],
    "summary": "Rejected",
}


def _chunking_spy(monkeypatch: pytest.MonkeyPatch) -> Dict[str, int]:
    """Count calls to ``build_review_chunks`` inside the coordinator.

    A short-circuited run returns before chunking, so the spy staying at 0 proves
    the whole review pipeline (chunk → map → false-positive → merge) was skipped —
    a stronger signal than a bare LLM-call count, which the chunk cache alone can
    already hold flat.
    """
    calls = {"n": 0}
    original = coord.build_review_chunks

    def _spy(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(coord, "build_review_chunks", _spy)
    return calls


def test_identical_approved_submission_short_circuits_entire_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A byte-identical, previously-approved submission does zero review work."""
    client = _CountingClient(_APPROVED)
    data = _one_file_input()

    first = run_coordinator(client, data)  # cold: reviews and caches the approval
    assert first.approved is True

    spy = _chunking_spy(monkeypatch)
    calls_before = client.calls
    second = run_coordinator(client, data)

    assert spy["n"] == 0  # never chunked → no map/false-positive/merge → no LLM calls
    assert client.calls == calls_before  # no new model calls of any kind
    assert second.approved is True
    assert [i.model_dump() for i in second.issues] == [i.model_dump() for i in first.issues]


def test_short_circuit_bypasses_model() -> None:
    """The short-circuit reproduces the approval without consulting the model."""
    # Second canned response would reject; a real re-review would surface it.
    client = _SwitchingClient([_APPROVED, _REJECTED])
    data = _one_file_input()

    first = run_coordinator(client, data)
    assert first.approved is True

    second = run_coordinator(client, data)
    assert second.approved is True  # served from cache, never saw the reject response


def test_rejected_submission_is_not_short_circuited(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rejection is never stored, so an identical resubmission reviews again."""
    client = _CountingClient(_REJECTED)
    data = _one_file_input()

    first = run_coordinator(client, data)
    assert first.approved is False

    spy = _chunking_spy(monkeypatch)
    second = run_coordinator(client, data)
    assert spy["n"] == 1  # reviewed again (no submission short-circuit)
    assert second.approved is False


def test_short_circuit_returns_independent_clone() -> None:
    """Mutating a short-circuit result never corrupts the cached entry."""
    client = _CountingClient(_APPROVED)
    data = _one_file_input()

    run_coordinator(client, data)
    served = run_coordinator(client, data)  # short-circuited clone
    served.summary = "mutated by caller"  # caller mutates its own copy

    again = run_coordinator(client, data)
    assert again.summary != "mutated by caller"  # cache entry untouched


def test_submission_cache_disabled_reviews_every_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CODE_REVIEW_SUBMISSION_CACHE_SIZE=0`` disables the short-circuit."""
    monkeypatch.setenv("CODE_REVIEW_SUBMISSION_CACHE_SIZE", "0")
    client = _CountingClient(_APPROVED)
    data = _one_file_input()

    run_coordinator(client, data)
    spy = _chunking_spy(monkeypatch)
    run_coordinator(client, data)
    assert spy["n"] == 1  # no short-circuit; the review ran again


def test_clear_submission_cache_forces_review(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clearing the submission cache forces a cold re-review."""
    client = _CountingClient(_APPROVED)
    data = _one_file_input()

    run_coordinator(client, data)
    coord.clear_submission_outcome_cache()

    spy = _chunking_spy(monkeypatch)
    run_coordinator(client, data)
    assert spy["n"] == 1  # cache cleared → review ran again


def test_submission_cache_evicts_oldest_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past capacity, the oldest approved submission is evicted and re-reviewed."""
    monkeypatch.setenv("CODE_REVIEW_SUBMISSION_CACHE_SIZE", "1")
    client = _CountingClient(_APPROVED)
    a = _one_file_input(task_description="A")  # distinct submissions → distinct keys
    b = _one_file_input(task_description="B")

    run_coordinator(client, a)  # caches A
    run_coordinator(client, b)  # caches B, evicts A (capacity 1)

    spy = _chunking_spy(monkeypatch)
    run_coordinator(client, b)  # B still cached → short-circuit
    assert spy["n"] == 0
    run_coordinator(client, a)  # A was evicted → full review again
    assert spy["n"] == 1


