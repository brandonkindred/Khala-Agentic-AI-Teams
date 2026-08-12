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

The shared chunk-outcome cache (``shared.cache`` — Redis when configured,
otherwise an in-process store) is cleared around every test by the autouse
``_reset_code_review_chunk_cache`` fixture in ``conftest.py``, which resets
factory state so tests do not observe cross-test cache hits.

Text-field truncation/length-limit behavior is out of scope here; it is
covered by the ``build_findings_digest`` tests in ``test_code_review_synthesis.py``.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List

import pytest
from code_review_agent import coordinator as coord
from code_review_agent import mapping
from code_review_agent.chunk_reviewer import CODE_TO_REVIEW_HEADER
from code_review_agent.coordinator import run_coordinator
from code_review_agent.models import (
    ChunkReviewOutput,
    CodeReviewInput,
    CodeReviewOutput,
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

    After the think-then-format split, the map marker lives on the reasoning
    ``complete`` prompt; ``complete_json`` is the format pass only.
    """

    def __init__(self, response: Dict[str, Any]) -> None:
        super().__init__()
        self._response = response
        self._lock = threading.Lock()
        self.calls = 0
        self.map_calls = 0

    def complete(self, prompt: str, **kwargs: Any) -> str:
        with self._lock:
            self.calls += 1
            if _MAP_MARKER in prompt:
                self.map_calls += 1
        return "Structured prose review summary."

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        with self._lock:
            self.calls += 1
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

    def complete(self, prompt: str, **kwargs: Any) -> str:
        with self._lock:
            if _MAP_MARKER in prompt:
                self.map_calls += 1
        return "Structured prose review summary."

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        with self._lock:
            resp = self._responses[min(self._idx, len(self._responses) - 1)]
            self._idx += 1
            return dict(resp)


class _FailOnMarkerClient(DummyLLMClient):
    """Raises a semantic exhaustion error on chunks containing ``fail_marker`` while ``fail``.

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

    def complete(self, prompt: str, **kwargs: Any) -> str:
        with self._lock:
            if _MAP_MARKER in prompt:
                self.map_calls += 1
        if self.fail and self._fail_marker in prompt:
            raise LLMSemanticExhaustionError("no verdict")
        return "Structured prose review summary."

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        return dict(_APPROVED)


_APPROVED = {"approved": True, "issues": [], "summary": "OK", "spec_compliance_notes": ""}


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


class _EnumLike:
    """Minimal stand-in for an enum member: exposes ``.value``, nothing else."""

    def __init__(self, value: str) -> None:
        self.value = value


def test_context_fingerprint_normalizes_non_profile_enum_values() -> None:
    """Every ``base_input`` field is ``.value``-normalized, not only ``profile``."""
    plain = {"task_description": "shared", "language": "python", "profile": "code_review"}
    with_enum = {**plain, "language": _EnumLike("python")}

    # An enum-like value whose .value matches the plain string hashes identically.
    assert mapping._context_fingerprint(plain, "model-A") == mapping._context_fingerprint(
        with_enum, "model-A"
    )

    # A different underlying .value still invalidates the digest.
    different_enum = {**plain, "language": _EnumLike("typescript")}
    assert mapping._context_fingerprint(plain, "model-A") != mapping._context_fingerprint(
        different_enum, "model-A"
    )


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
        "spec_compliance_notes": "",
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
    # Default graceful degradation: the clean sibling drives an approved verdict,
    # chunk b is recorded as a non-blocking not-reviewed range (never posted).
    assert degraded.approved is True
    assert not any(mapping.NOT_REVIEWED_FINDING_MARKER in i.description for i in degraded.issues)
    assert degraded.not_reviewed_ranges  # b's range is recorded for observability

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

    def complete(self, prompt: str, **kwargs: Any) -> str:
        with self._lock:
            if _MAP_MARKER in prompt:
                self.map_calls += 1
        if "S_MARK_START" in prompt and "E_MARK_END" in prompt:
            raise LLMTruncatedError("full chunk too big", finish_reason="length")
        return "Structured prose review summary."

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
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


def test_chunk_outcome_cache_size_default_override_and_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default is 512; env override applies; negative values clamp to 0."""
    monkeypatch.delenv("CODE_REVIEW_CHUNK_OUTCOME_CACHE_SIZE", raising=False)
    assert mapping._chunk_outcome_cache_size() == mapping.DEFAULT_CHUNK_OUTCOME_CACHE_SIZE

    monkeypatch.setenv("CODE_REVIEW_CHUNK_OUTCOME_CACHE_SIZE", "10")
    assert mapping._chunk_outcome_cache_size() == 10

    monkeypatch.setenv("CODE_REVIEW_CHUNK_OUTCOME_CACHE_SIZE", "-5")
    assert mapping._chunk_outcome_cache_size() == 0


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

    def complete(self, prompt: str, **kwargs: Any) -> str:
        with self._lock:
            if _MAP_MARKER in prompt:
                self.map_prompts.append(prompt)
        return "Structured prose review summary."

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
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


def test_symbol_surface_is_not_capped_at_sixty() -> None:
    """A file with more than the old 60-symbol cap has every symbol surfaced —
    there is no hardcoded per-file truncation anymore."""
    content = "\n".join(f"def fn_{i}():\n    pass" for i in range(75))
    result = coord._symbol_surface(content)
    assert len(result) == 75
    assert "fn_0" in result and "fn_74" in result


def test_symbol_surface_finds_symbols_in_pre_numbered_content() -> None:
    """Pre-numbered content (``FileSegment.pre_numbered`` / a diff-first
    ``files=`` submission's ``N: `` prefixes) must not blind symbol extraction
    -- the anchored patterns match column zero, so a raw ``"12: def foo():"``
    line would otherwise never match ``def`` at all, silently emptying the
    sibling surface for every pre-numbered submission."""
    content = "1: def foo():\n2:     pass\n3: \n4: class Bar:\n5:     pass\n"
    assert coord._symbol_surface(content) == ["Bar", "foo"]


def test_symbol_surface_pre_numbered_still_excludes_indented_defs() -> None:
    """De-numbering must restore each line's real column position, not just
    strip the prefix and shift everything to column zero -- an indented
    method stays non-top-level even once its ``N: `` prefix is removed."""
    content = (
        "10: class C:\n"
        "11:     def method(self):\n"  # indented → not top-level
        "12:         pass\n"
        "13: def top():\n"  # column-zero → top-level
        "14:     pass\n"
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


def test_sibling_surface_is_bounded_to_prompt_budget() -> None:
    """A large sibling surface cannot exceed the characters reserved in the prompt."""
    from code_review_agent.models import FileSegment, ReviewChunk

    surface = {f"app/f{i}.py": [f"sym{j}" for j in range(40)] for i in range(200)}
    chunk = ReviewChunk(segments=[FileSegment(path="app/self.py", content="x")])
    out = coord._sibling_surface(chunk, surface)
    assert len(out) == compute_code_review_sibling_surface_chars()
    assert "app/f0.py:" in out


def test_sibling_surface_honors_env_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from code_review_agent.models import FileSegment, ReviewChunk

    surface = {f"app/f{i}.py": [f"sym{j}" for j in range(40)] for i in range(200)}
    chunk = ReviewChunk(segments=[FileSegment(path="app/self.py", content="x")])

    monkeypatch.setenv("CODE_REVIEW_SIBLING_SURFACE_CHARS", "50")
    out = coord._sibling_surface(chunk, surface)
    assert len(out) == 50

    monkeypatch.setenv("CODE_REVIEW_SIBLING_SURFACE_CHARS", "0")
    assert coord._sibling_surface(chunk, surface) == ""


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

    def complete(self, prompt: str, **kwargs: Any) -> str:
        with self._lock:
            if _MAP_MARKER in prompt:
                self.map_prompts.append(prompt)
        if "def a_func" in prompt and "def b_func" in prompt:
            raise LLMSemanticExhaustionError("combined chunk too big")
        return "Structured prose review summary."

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
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
# Leader/waiter coordination is exercised with real threads against the shared
# cache backend (Memory by default): the leader holds the review open until the
# waiter has joined, proving only one LLM call runs and both callers share the
# outcome (or the same exception).


class _RaisingReviewer:
    """Reviewer whose ``run`` raises, to drive the leader failure path."""

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.calls = 0

    def run(self, chunk_input: Any) -> Any:
        self.calls += 1
        raise self._error


def _single_chunk() -> ReviewChunk:
    return ReviewChunk(
        segments=[FileSegment(path="app/a.py", content="def f():\n    return 1\n", total_lines=2)]
    )


def test_leader_review_failure_releases_slot_and_reraises() -> None:
    """A leader whose review fails re-raises, caches nothing, and frees the slot."""
    from shared.cache import get_shared_cache

    reviewer = _RaisingReviewer(LLMRateLimitError("rate limited"))
    chunk = _single_chunk()
    base_input: Dict[str, Any] = {"task_description": "t", "language": "python"}
    context_fp = "fp"
    key = mapping._chunk_cache_key(chunk, context_fp, "")

    # An infra failure surfaces as CodeReviewUnavailableError from the reviewer.
    with pytest.raises(CodeReviewUnavailableError):
        mapping._cached_review_chunk(reviewer, chunk, base_input, context_fp)

    assert reviewer.calls == 1
    assert get_shared_cache(mapping._chunk_cache_namespace()).get(key) is None
    with pytest.raises(CodeReviewUnavailableError):
        mapping._cached_review_chunk(reviewer, chunk, base_input, context_fp)
    assert reviewer.calls == 2


def test_waiter_reuses_resolved_inflight_result() -> None:
    """Concurrent callers of the same key share one review via single-flight."""
    from code_review_agent.models import ChunkReviewOutput

    chunk = _single_chunk()
    base_input: Dict[str, Any] = {"task_description": "t", "language": "python"}
    context_fp = "fp"
    started = threading.Event()
    release = threading.Event()
    results: List[mapping._ChunkOutcome] = []
    errors: List[BaseException] = []

    class _SlowReviewer:
        def __init__(self) -> None:
            self.calls = 0
            self._lock = threading.Lock()

        def run(self, chunk_input: Any) -> Any:
            with self._lock:
                self.calls += 1
            started.set()
            assert release.wait(timeout=2), "leader was not released"
            return ChunkReviewOutput(approved=True, issues=[], summary="ok")

    reviewer = _SlowReviewer()

    def leader() -> None:
        try:
            results.append(mapping._cached_review_chunk(reviewer, chunk, base_input, context_fp))
        except BaseException as exc:  # noqa: BLE001 - collect for assertion
            errors.append(exc)

    def waiter() -> None:
        assert started.wait(timeout=2), "leader never started"
        try:
            results.append(mapping._cached_review_chunk(reviewer, chunk, base_input, context_fp))
        except BaseException as exc:  # noqa: BLE001 - collect for assertion
            errors.append(exc)

    t_leader = threading.Thread(target=leader)
    t_waiter = threading.Thread(target=waiter)
    t_leader.start()
    assert started.wait(timeout=2), "leader never entered review"
    t_waiter.start()
    release.set()
    t_leader.join(timeout=5)
    t_waiter.join(timeout=5)

    assert not errors
    assert reviewer.calls == 1
    assert len(results) == 2
    assert results[0].approved_flags == [True]
    assert results[1].approved_flags == [True]
    assert results[0] is not results[1]


def test_waiter_reraises_resolved_inflight_exception() -> None:
    """Concurrent waiters re-raise the leader's exception without re-reviewing."""
    chunk = _single_chunk()
    base_input: Dict[str, Any] = {"task_description": "t", "language": "python"}
    context_fp = "fp"
    started = threading.Event()
    release = threading.Event()
    errors: List[BaseException] = []

    class _SlowFailingReviewer:
        def __init__(self) -> None:
            self.calls = 0
            self._lock = threading.Lock()

        def run(self, chunk_input: Any) -> Any:
            with self._lock:
                self.calls += 1
            started.set()
            assert release.wait(timeout=2), "leader was not released"
            raise LLMRateLimitError("rate limited")

    reviewer = _SlowFailingReviewer()

    def leader() -> None:
        try:
            mapping._cached_review_chunk(reviewer, chunk, base_input, context_fp)
        except BaseException as exc:  # noqa: BLE001 - collect for assertion
            errors.append(exc)

    def waiter() -> None:
        assert started.wait(timeout=2), "leader never started"
        try:
            mapping._cached_review_chunk(reviewer, chunk, base_input, context_fp)
        except BaseException as exc:  # noqa: BLE001 - collect for assertion
            errors.append(exc)

    t_leader = threading.Thread(target=leader)
    t_waiter = threading.Thread(target=waiter)
    t_leader.start()
    assert started.wait(timeout=2), "leader never entered review"
    t_waiter.start()
    release.set()
    t_leader.join(timeout=5)
    t_waiter.join(timeout=5)

    assert reviewer.calls == 1
    assert len(errors) == 2
    assert all(isinstance(e, CodeReviewUnavailableError) for e in errors)


def _simple_outcome() -> mapping._ChunkOutcome:
    return mapping._ChunkOutcome(approved_flags=[True], summaries=["ok"])


def test_clear_chunk_outcome_cache_empties_registries() -> None:
    """A clear empties the shared chunk-outcome namespace."""
    from shared.cache import get_shared_cache

    chunk = _single_chunk()
    context_fp = "fp"
    key = mapping._chunk_cache_key(chunk, context_fp, "")
    cache = get_shared_cache(mapping._chunk_cache_namespace())
    cache.set(key, mapping._chunk_outcome_to_bytes(_simple_outcome()), max_entries=8)
    assert cache.get(key) is not None

    mapping.clear_chunk_outcome_cache()

    assert cache.get(key) is None


class _BlockingReviewer:
    """Reviewer stand-in whose ``run`` signals entry, blocks, then approves.

    Drives a real leader through ``_cached_review_chunk`` so a test can pause
    it mid-review (LLM call in flight) and clear the cache from the main
    thread, rather than faking the leader's cache write directly.
    """

    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self._entered = entered
        self._release = release
        self.calls = 0

    def run(self, chunk_input: Any) -> ChunkReviewOutput:
        self.calls += 1
        self._entered.set()
        assert self._release.wait(timeout=5), "test deadlocked waiting for release"
        return ChunkReviewOutput(approved=True, issues=[], summary="ok")


def test_clear_mid_flight_does_not_prevent_leader_from_caching() -> None:
    """Clearing while a leader is in flight does not stop it from later caching.

    Pins the corrected postcondition on ``clear_chunk_outcome_cache``: the
    guaranteed-miss promise holds only when no review of that chunk is already
    in flight. A leader in flight when the clear runs holds no lock across its
    LLM call, so it can still publish its outcome afterward. Exercised through
    the real ``_cached_review_chunk`` leader path (a worker thread blocked
    inside its "LLM call" via ``_BlockingReviewer``, released only after the
    clear returns) rather than a direct cache write, so a future change to the
    caching logic itself — e.g. a generation check meant to suppress a stale
    post-clear write — would be caught by this test.
    """
    from shared.cache import get_shared_cache

    chunk = _single_chunk()
    base_input: Dict[str, Any] = {"task_description": "t", "language": "python"}
    context_fp = "fp"
    key = mapping._chunk_cache_key(chunk, context_fp, "")
    cache = get_shared_cache(mapping._chunk_cache_namespace())

    entered = threading.Event()
    release = threading.Event()
    reviewer = _BlockingReviewer(entered, release)
    outcomes: List[mapping._ChunkOutcome] = []

    def _run_leader() -> None:
        outcomes.append(mapping._cached_review_chunk(reviewer, chunk, base_input, context_fp))

    leader_thread = threading.Thread(target=_run_leader)
    leader_thread.start()
    try:
        # The leader registers its in-flight slot before calling reviewer.run,
        # so this confirms the clear below races a genuinely in-flight review.
        assert entered.wait(timeout=5), "leader never reached its blocking review call"
        assert cache.get(key) is None  # still computing; nothing durable yet

        mapping.clear_chunk_outcome_cache()
        assert cache.get(key) is None
    finally:
        # Always unblock so a failed assertion cannot leave the worker hung.
        release.set()
        leader_thread.join(timeout=5)
        assert not leader_thread.is_alive(), "leader thread did not finish"

    assert reviewer.calls == 1
    assert cache.get(key) is not None  # the "guaranteed miss" did not hold
    assert outcomes[0].approved_flags == [True]


def test_corrupt_chunk_cache_entry_is_evicted_and_recomputed() -> None:
    """A corrupt durable entry is deleted; the chunk is recomputed and re-cached."""
    from shared.cache import get_shared_cache

    chunk = _single_chunk()
    base_input: Dict[str, Any] = {"task_description": "t", "language": "python"}
    context_fp = "fp"
    key = mapping._chunk_cache_key(chunk, context_fp, "")
    cache = get_shared_cache(mapping._chunk_cache_namespace())
    cache.set(key, b"not-valid-chunk-outcome-json", max_entries=8)

    class _ApproveOnce:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, chunk_input: Any) -> ChunkReviewOutput:
            self.calls += 1
            return ChunkReviewOutput(approved=True, issues=[], summary="rebuilt")

    approving = _ApproveOnce()
    outcome = mapping._cached_review_chunk(approving, chunk, base_input, context_fp)
    assert approving.calls == 1
    assert outcome.approved_flags == [True]
    assert outcome.summaries == ["rebuilt"]
    raw = cache.get(key)
    assert raw is not None
    assert raw != b"not-valid-chunk-outcome-json"
    # Valid round-trip; a subsequent call is a hit (no extra review).
    assert mapping._chunk_outcome_from_bytes(raw).summaries == ["rebuilt"]
    again = mapping._cached_review_chunk(approving, chunk, base_input, context_fp)
    assert approving.calls == 1
    assert again.summaries == ["rebuilt"]


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
            "category": "logic",
            "file_path": "app/a.py",
            "description": "Missing input validation",
            "suggestion": "Validate inputs",
        }
    ],
    "summary": "Rejected",
    "spec_compliance_notes": "",
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


def test_spec_compliance_pass_toggle_invalidates_submission_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flipping ``CODE_REVIEW_SPEC_COMPLIANCE_PASS`` busts the submission-level cache.

    Regression test for a HIGH-severity gap: an approved submission cached with the
    flag off must never be served to an identical resubmission once the flag is on --
    doing so would silently skip the new post-dedupe ``synthesize_spec_compliance``
    pass the flag adds. The second canned response would reject; only a genuine
    re-review (never a stale flag-off cache hit) would surface it.
    """
    monkeypatch.delenv("CODE_REVIEW_SPEC_COMPLIANCE_PASS", raising=False)
    client = _SwitchingClient([_APPROVED, _REJECTED])
    data = _one_file_input()  # profile defaults to CODE_REVIEW

    first = run_coordinator(client, data)
    assert first.approved is True

    monkeypatch.setenv("CODE_REVIEW_SPEC_COMPLIANCE_PASS", "true")
    second = run_coordinator(client, data)
    assert second.approved is False  # real re-review, not a stale flag-off cache hit


def test_spec_compliance_flag_passed_to_fingerprint_is_profile_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_coordinator`` computes the ``CODE_REVIEW_SPEC_COMPLIANCE_PASS`` decision
    exactly once and passes the *already profile-gated* result into
    ``_submission_fingerprint`` -- the fingerprint helper itself never reads the raw
    env var.

    Regression test: a profile-blind ``env_bool`` read inside the fingerprint helper
    would fingerprint a non-``CODE_REVIEW`` submission as flag-sensitive whenever the
    env var happens to be set, even though the resolved decision (and thus the real
    review) never depends on it for that profile -- causing needless cache misses.
    Spying on the call site (rather than inferring it from cache hit/miss side
    effects) proves the exact boolean the coordinator resolved and threaded through.
    """
    monkeypatch.setenv("CODE_REVIEW_SPEC_COMPLIANCE_PASS", "true")
    original = coord._submission_fingerprint
    calls: list = []

    def _spy(input_data, model_fingerprint, spec_compliance_single_pass):
        calls.append(spec_compliance_single_pass)
        return original(input_data, model_fingerprint, spec_compliance_single_pass)

    monkeypatch.setattr(coord, "_submission_fingerprint", _spy)

    client = _CountingClient(_APPROVED)
    run_coordinator(client, _one_file_input(profile=ReviewProfile.SPEC_CONFORMANCE))

    assert calls == [False], (
        "the flag is CODE_REVIEW-only; a non-CODE_REVIEW profile must fingerprint "
        "as spec-compliance-pass=False regardless of the env var"
    )

    calls.clear()
    run_coordinator(client, _one_file_input(profile=ReviewProfile.CODE_REVIEW))
    assert calls == [True], "the CODE_REVIEW profile must fingerprint the env var as-is"


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


def test_non_approved_cached_submission_treated_as_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-approved entry under the submission key is never served as a hit."""
    from shared.cache import get_shared_cache

    client = _CountingClient(_APPROVED)
    data = _one_file_input()
    key = "poisoned-submission-key"
    reject = CodeReviewOutput.model_validate(_REJECTED)
    get_shared_cache(coord._submission_cache_namespace()).set(
        key, reject.model_dump_json().encode("utf-8"), max_entries=8
    )
    monkeypatch.setattr(coord, "_submission_fingerprint", lambda *_a, **_k: key)

    spy = _chunking_spy(monkeypatch)
    out = run_coordinator(client, data)
    assert spy["n"] == 1  # reviewed for real (poisoned entry treated as miss)
    assert out.approved is True


def test_cached_submission_with_unreviewed_ranges_treated_as_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Approved-but-partial entries must not short-circuit a full re-review."""
    from shared.cache import get_shared_cache

    client = _CountingClient(_APPROVED)
    data = _one_file_input()
    key = "partial-submission-key"
    partial = CodeReviewOutput(
        approved=True,
        issues=[],
        summary="OK but incomplete",
        not_reviewed_ranges=["src/a.py:1-10"],
    )
    get_shared_cache(coord._submission_cache_namespace()).set(
        key, partial.model_dump_json().encode("utf-8"), max_entries=8
    )
    monkeypatch.setattr(coord, "_submission_fingerprint", lambda *_a, **_k: key)

    spy = _chunking_spy(monkeypatch)
    out = run_coordinator(client, data)
    assert spy["n"] == 1
    assert out.approved is True
    assert out.not_reviewed_ranges == []


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


def test_submission_cache_size_clamps_negative_and_garbage_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_submission_cache_size()`` never returns ``None``/negative/non-int
    regardless of a hostile ``CODE_REVIEW_SUBMISSION_CACHE_SIZE`` value -- the
    eviction loop in ``run_coordinator`` relies on this without re-checking
    (see its postcondition)."""
    monkeypatch.setenv("CODE_REVIEW_SUBMISSION_CACHE_SIZE", "-5")
    assert coord._submission_cache_size() == 0

    monkeypatch.setenv("CODE_REVIEW_SUBMISSION_CACHE_SIZE", "not-a-number")
    assert coord._submission_cache_size() == coord.DEFAULT_SUBMISSION_CACHE_SIZE

    monkeypatch.delenv("CODE_REVIEW_SUBMISSION_CACHE_SIZE", raising=False)
    assert coord._submission_cache_size() == coord.DEFAULT_SUBMISSION_CACHE_SIZE


def test_submission_cache_bypassed_when_repo_reader_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verdict backed by ``repo_reader`` reads beyond ``CodeReviewInput``, so
    it must never be served from (or written to) the input-only submission
    cache -- otherwise a stale approval could mask a since-added architecture/
    redundancy finding or a since-resolved false positive from a rest-of-repo
    change the cache key cannot see."""
    client = _CountingClient(_APPROVED)
    data = _one_file_input()
    reader = object()  # a RepoReader stand-in; never actually read from here

    run_coordinator(client, data, repo_reader=reader)  # would cache without the guard

    spy = _chunking_spy(monkeypatch)
    run_coordinator(client, data, repo_reader=reader)
    assert spy["n"] == 1  # no short-circuit: reviewed again despite identical bytes

    # A repo_reader-backed run must not poison the cache for reader-free reruns either.
    run_coordinator(client, data)
    assert spy["n"] == 2


def test_consumer_cache_namespaces_include_build_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production namespace helpers must suffix stems when KHALA_BUILD_ID is set."""
    monkeypatch.delenv("KHALA_CACHE_BUILD_ID", raising=False)
    monkeypatch.setenv("KHALA_BUILD_ID", "sha-deadbeef")
    assert mapping._chunk_cache_namespace() == "cr:chunk:v2:sha-deadbeef"
    assert coord._submission_cache_namespace() == "cr:sub:v1:sha-deadbeef"
    # get_shared_cache must address the suffixed namespace (not the bare stem).
    from shared.cache import get_shared_cache, reset_shared_cache_state

    reset_shared_cache_state()
    cache = get_shared_cache(mapping._chunk_cache_namespace())
    cache.set("k", b"v", max_entries=4)
    assert get_shared_cache("cr:chunk:v2").get("k") is None
    assert get_shared_cache(mapping._chunk_cache_namespace()).get("k") == b"v"
