"""More tests for assorted SE shared utility modules.

Covers ``json_utils`` (text completion + JSON recovery + merge helpers),
and ``deduplication``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# deduplication
# ---------------------------------------------------------------------------


def test_dedupe_strings_empty() -> None:
    """dedupe_strings returns an empty list for empty input."""
    from software_engineering_team.shared.deduplication import dedupe_strings

    assert dedupe_strings([]) == []


def test_dedupe_strings_removes_near_duplicates() -> None:
    """dedupe_strings drops near-duplicate strings (case-insensitive) while keeping distinct entries."""
    from software_engineering_team.shared.deduplication import dedupe_strings

    items = [
        "Use PostgreSQL for storage",
        "Use Postgresql for storage",  # near-duplicate
        "Use Redis for caching",
    ]
    out = dedupe_strings(items, similarity_threshold=0.85)
    assert "Use Redis for caching" in out
    assert len(out) == 2


def test_dedupe_strings_skips_non_strings() -> None:
    """dedupe_strings drops non-string items rather than crashing on them."""
    from software_engineering_team.shared.deduplication import dedupe_strings

    items = ["valid", 42, "another"]  # type: ignore[list-item]
    out = dedupe_strings(items)
    assert 42 not in out
    assert len(out) == 2


def test_dedupe_by_key_basic() -> None:
    """dedupe_by_key drops items whose key is a near-duplicate of an earlier item's key."""
    from software_engineering_team.shared.deduplication import dedupe_by_key

    items = [
        SimpleNamespace(name="API errors", priority="high"),
        SimpleNamespace(name="API error", priority="low"),  # near-dup name
        SimpleNamespace(name="DB latency", priority="medium"),
    ]
    out = dedupe_by_key(items, key_fn=lambda x: x.name)
    assert len(out) == 2


def test_dedupe_by_key_non_string_key_kept() -> None:
    """dedupe_by_key passes non-string keys through unchanged (no near-dup check applies)."""
    from software_engineering_team.shared.deduplication import dedupe_by_key

    items = [SimpleNamespace(name=None), SimpleNamespace(name=None)]
    out = dedupe_by_key(items, key_fn=lambda x: x.name)
    # Non-string keys pass through unchanged
    assert len(out) == 2


def test_dedupe_by_key_empty() -> None:
    """dedupe_by_key returns an empty list for empty input."""
    from software_engineering_team.shared.deduplication import dedupe_by_key

    assert dedupe_by_key([], key_fn=lambda x: x) == []


# ---------------------------------------------------------------------------
# json_utils
# ---------------------------------------------------------------------------


def test_default_decompose_by_h2_sections() -> None:
    """default_decompose_by_sections splits content on H2 (##) headers."""
    from software_engineering_team.shared.json_utils import default_decompose_by_sections

    content = "## a\nA\n## b\nB"
    out = default_decompose_by_sections(content)
    assert len(out) == 2


def test_default_decompose_by_h1_sections() -> None:
    """default_decompose_by_sections splits content on H1 (#) headers."""
    from software_engineering_team.shared.json_utils import default_decompose_by_sections

    content = "# a\nA\n# b\nB"
    out = default_decompose_by_sections(content)
    assert len(out) == 2


def test_default_decompose_chunks_by_size() -> None:
    """default_decompose_by_sections falls back to fixed-size chunking when no section headers are present."""
    from software_engineering_team.shared.json_utils import default_decompose_by_sections

    out = default_decompose_by_sections("x" * 50, chunk_size=10)
    assert len(out) == 5


def test_default_merge_results_empty() -> None:
    """default_merge_results returns an empty dict for no partial results."""
    from software_engineering_team.shared.json_utils import default_merge_results

    assert default_merge_results([]) == {}


def test_default_merge_results_lists_dicts_scalars() -> None:
    """default_merge_results merges lists (semantically deduped), nested dicts, and last-wins scalars across partial results."""
    from software_engineering_team.shared.json_utils import default_merge_results

    a = {
        "list": ["a", "b"],
        "obj": {"x": [1], "y": "y1"},
        "scalar": "",
    }
    b = {
        "list": ["b", "c"],  # 'b' is a duplicate string → semantic dedup
        "obj": {"x": [2], "z": "z1"},
        "scalar": "filled",
    }
    merged = default_merge_results([a, b])
    assert "a" in merged["list"]
    assert "c" in merged["list"]
    assert merged["obj"]["x"] == [1, 2]
    assert merged["obj"]["y"] == "y1"
    assert merged["obj"]["z"] == "z1"
    assert merged["scalar"] == "filled"


def test_attempt_fix_output_continuation_no_llm_attrs() -> None:
    """attempt_fix_output_continuation returns the raw text unchanged when the LLM lacks the base_url/model attrs the continuator needs."""
    from software_engineering_team.shared.json_utils import attempt_fix_output_continuation

    out = attempt_fix_output_continuation(
        llm=MagicMock(spec=[]),  # no base_url/model
        prompt="p",
        raw_text="partial",
        agent_name="A",
    )
    assert out == "partial"


def test_attempt_fix_output_continuation_with_attrs(monkeypatch) -> None:
    """attempt_fix_output_continuation delegates to ResponseContinuator and returns its completed content when the LLM exposes base_url/model."""
    from software_engineering_team.shared import json_utils

    class _LLM:
        base_url = "http://x"
        model = "m"
        timeout = 60

    class _FakeContinuator:
        def __init__(self, **kwargs):
            pass

        def attempt_continuation(self, **kwargs):
            return SimpleNamespace(content="full content")

    monkeypatch.setattr(
        "software_engineering_team.shared.continuation.ResponseContinuator",
        _FakeContinuator,
    )
    out = json_utils.attempt_fix_output_continuation(
        llm=_LLM(),
        prompt="p",
        raw_text="partial",
        agent_name="A",
    )
    assert out == "full content"


def test_complete_text_with_continuation(monkeypatch) -> None:
    """complete_text_with_continuation runs the agent and strips the completed text output."""
    from software_engineering_team.shared import json_utils

    class _FakeAgent:
        def __init__(self, **kwargs):
            pass

        def __call__(self, prompt):
            return "  hello world  "

    import strands

    monkeypatch.setattr(strands, "Agent", _FakeAgent)
    out = json_utils.complete_text_with_continuation(llm=MagicMock(), prompt="hi")
    assert out == "hello world"


def test_parse_json_with_recovery_no_chunks_path(monkeypatch) -> None:
    """parse_json_with_recovery returns the single-call result when no decomposition is supplied."""
    from software_engineering_team.shared import json_utils

    monkeypatch.setattr(
        "software_engineering_team.shared.llm.complete_json_with_continuation",
        lambda llm, prompt, task_id: {"got": prompt},
    )
    out = json_utils.parse_json_with_recovery(MagicMock(), "p", agent_name="A")
    assert out == {"got": "p"}


def test_parse_json_with_recovery_returns_none_on_exception(monkeypatch) -> None:
    """parse_json_with_recovery returns None when completion raises LLMJsonParseError."""
    from llm_service import LLMJsonParseError
    from software_engineering_team.shared import json_utils

    def boom(*a, **kw):
        raise LLMJsonParseError("bad json")

    monkeypatch.setattr(
        "software_engineering_team.shared.llm.complete_json_with_continuation", boom
    )
    out = json_utils.parse_json_with_recovery(MagicMock(), "p", agent_name="A")
    assert out is None


def test_parse_json_with_recovery_propagates_non_recovery_exception(monkeypatch) -> None:
    """Non-recovery exceptions from completion must propagate, not become None."""
    from software_engineering_team.shared import json_utils

    def boom(*a, **kw):
        raise ValueError("programming bug")

    monkeypatch.setattr(
        "software_engineering_team.shared.llm.complete_json_with_continuation", boom
    )
    try:
        json_utils.parse_json_with_recovery(MagicMock(), "p", agent_name="A")
    except ValueError as e:
        assert str(e) == "programming bug"
    else:
        raise AssertionError("expected ValueError to propagate")


def test_parse_json_with_recovery_returns_none_on_transient_llm_error(monkeypatch) -> None:
    """parse_json_with_recovery returns None when completion raises a transient LLM error."""
    from llm_service import LLMTemporaryError
    from software_engineering_team.shared import json_utils

    def boom(*a, **kw):
        raise LLMTemporaryError("provider exhausted")

    monkeypatch.setattr(
        "software_engineering_team.shared.llm.complete_json_with_continuation", boom
    )
    out = json_utils.parse_json_with_recovery(MagicMock(), "p", agent_name="A")
    assert out is None


def test_parse_json_with_recovery_chunked(monkeypatch) -> None:
    """parse_json_with_recovery decomposes, completes each chunk, and merges the per-chunk results."""
    from software_engineering_team.shared import json_utils

    calls = {"n": 0}

    def fake_complete(llm, prompt, task_id):
        calls["n"] += 1
        return {"items": [prompt]}

    monkeypatch.setattr(
        "software_engineering_team.shared.llm.complete_json_with_continuation",
        fake_complete,
    )
    out = json_utils.parse_json_with_recovery(
        MagicMock(),
        "main",
        agent_name="A",
        decompose_fn=lambda c: ["chunk1", "chunk2"],
        merge_fn=lambda results: {"items": sum((r.get("items", []) for r in results), [])},
        original_content="big content",
        chunk_prompt_template="chunk={chunk_content}",
        on_chunk_progress=lambda i, n: None,
    )
    assert out == {"items": ["chunk=chunk1", "chunk=chunk2"]}
    assert calls["n"] == 2


def test_parse_json_with_recovery_chunked_empty(monkeypatch) -> None:
    """parse_json_with_recovery skips chunking and returns the single-call result when decomposition yields no chunks."""
    from software_engineering_team.shared import json_utils

    monkeypatch.setattr(
        "software_engineering_team.shared.llm.complete_json_with_continuation",
        lambda llm, prompt, task_id: {"got": "x"},
    )
    out = json_utils.parse_json_with_recovery(
        MagicMock(),
        "p",
        agent_name="A",
        decompose_fn=lambda c: [],
        merge_fn=lambda results: {"merged": True},
        original_content="x",
        chunk_prompt_template="x={chunk_content}",
    )
    assert out == {"got": "x"}


def test_parse_json_with_recovery_chunked_failure(monkeypatch) -> None:
    """parse_json_with_recovery returns None when a chunk completion raises LLMJsonParseError."""
    from llm_service import LLMJsonParseError
    from software_engineering_team.shared import json_utils

    def boom(*a, **kw):
        raise LLMJsonParseError("chunk failed")

    monkeypatch.setattr(
        "software_engineering_team.shared.llm.complete_json_with_continuation", boom
    )
    out = json_utils.parse_json_with_recovery(
        MagicMock(),
        "main",
        agent_name="A",
        decompose_fn=lambda c: ["c1"],
        merge_fn=lambda r: {"merged": True},
        original_content="x",
        chunk_prompt_template="x={chunk_content}",
    )
    assert out is None
