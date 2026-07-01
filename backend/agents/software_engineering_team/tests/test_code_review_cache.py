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
from typing import Any, Dict, List

import pytest
from code_review_agent import coordinator as coord
from code_review_agent.chunk_reviewer import CODE_TO_REVIEW_HEADER
from code_review_agent.coordinator import run_coordinator
from code_review_agent.models import CodeReviewInput, ReviewProfile

from llm_service import LLMSemanticExhaustionError
from llm_service.clients.dummy import DummyLLMClient

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


def test_cache_disabled_via_env_is_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """Size 0 disables the cache: every run re-invokes the model, as before."""
    monkeypatch.setenv("CODE_REVIEW_CHUNK_OUTCOME_CACHE_SIZE", "0")
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

    monkeypatch.setattr(coord, "resolve_code_review_model", lambda _llm: _Model())
    assert coord._review_model_fingerprint(object()) == "claude-x"


def test_model_fingerprint_falls_back_to_config_then_typename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no id attributes it reads ``config['model']``, else the type name."""

    class _ConfigModel:
        config = {"model": "cfg-model"}

    monkeypatch.setattr(coord, "resolve_code_review_model", lambda _llm: _ConfigModel())
    assert coord._review_model_fingerprint(object()) == "cfg-model"

    class _Bare:
        pass

    monkeypatch.setattr(coord, "resolve_code_review_model", lambda _llm: _Bare())
    assert coord._review_model_fingerprint(object()) == "_Bare"


def test_model_fingerprint_falls_back_when_resolution_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolution failure never raises; it falls back to the client type name."""

    def _boom(_llm: Any) -> Any:
        raise RuntimeError("no model")

    monkeypatch.setattr(coord, "resolve_code_review_model", _boom)
    assert coord._review_model_fingerprint(_CountingClient(_APPROVED)) == "_CountingClient"


def test_lru_evicts_oldest_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past capacity, the oldest entry is evicted and re-reviewed on return."""
    monkeypatch.setenv("CODE_REVIEW_CHUNK_OUTCOME_CACHE_SIZE", "1")
    client = _CountingClient(_APPROVED)

    a = _one_file_input(task_description="A")  # distinct context → distinct key
    b = _one_file_input(task_description="B")

    run_coordinator(client, a)  # map_calls=1, caches A
    run_coordinator(client, b)  # map_calls=2, caches B, evicts A (capacity 1)
    run_coordinator(client, b)  # map_calls=2, B still cached (hit)
    assert client.map_calls == 2
    run_coordinator(client, a)  # map_calls=3, A was evicted → miss
    assert client.map_calls == 3
