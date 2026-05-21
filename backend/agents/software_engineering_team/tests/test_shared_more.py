"""More tests for assorted SE shared utility modules.

Covers ``json_utils`` (text completion + JSON recovery + merge helpers),
``planning_cache`` (hash key + set/get/miss), and ``deduplication``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# deduplication
# ---------------------------------------------------------------------------


def test_dedupe_strings_empty() -> None:
    from software_engineering_team.shared.deduplication import dedupe_strings

    assert dedupe_strings([]) == []


def test_dedupe_strings_removes_near_duplicates() -> None:
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
    from software_engineering_team.shared.deduplication import dedupe_strings

    items = ["valid", 42, "another"]  # type: ignore[list-item]
    out = dedupe_strings(items)
    assert 42 not in out
    assert len(out) == 2


def test_dedupe_by_key_basic() -> None:
    from software_engineering_team.shared.deduplication import dedupe_by_key

    items = [
        SimpleNamespace(name="API errors", priority="high"),
        SimpleNamespace(name="API error", priority="low"),  # near-dup name
        SimpleNamespace(name="DB latency", priority="medium"),
    ]
    out = dedupe_by_key(items, key_fn=lambda x: x.name)
    assert len(out) == 2


def test_dedupe_by_key_non_string_key_kept() -> None:
    from software_engineering_team.shared.deduplication import dedupe_by_key

    items = [SimpleNamespace(name=None), SimpleNamespace(name=None)]
    out = dedupe_by_key(items, key_fn=lambda x: x.name)
    # Non-string keys pass through unchanged
    assert len(out) == 2


def test_dedupe_by_key_empty() -> None:
    from software_engineering_team.shared.deduplication import dedupe_by_key

    assert dedupe_by_key([], key_fn=lambda x: x) == []


# ---------------------------------------------------------------------------
# planning_cache
# ---------------------------------------------------------------------------


def test_compute_planning_cache_key_stable() -> None:
    from software_engineering_team.shared.planning_cache import compute_planning_cache_key

    k1 = compute_planning_cache_key("spec", "arch")
    k2 = compute_planning_cache_key("spec", "arch")
    assert k1 == k2
    assert len(k1) == 24


def test_compute_planning_cache_key_changes_with_inputs() -> None:
    from software_engineering_team.shared.planning_cache import compute_planning_cache_key

    base = compute_planning_cache_key("spec", "arch")
    different_spec = compute_planning_cache_key("spec2", "arch")
    different_arch = compute_planning_cache_key("spec", "arch2")
    assert base != different_spec
    assert base != different_arch


def test_compute_planning_cache_key_includes_project_overview() -> None:
    from software_engineering_team.shared.planning_cache import compute_planning_cache_key

    a = compute_planning_cache_key("spec", "arch")
    b = compute_planning_cache_key(
        "spec",
        "arch",
        project_overview={"primary_goal": "g", "delivery_strategy": "s"},
    )
    assert a != b


def test_compute_planning_cache_key_sprint_id_changes_key() -> None:
    from software_engineering_team.shared.planning_cache import compute_planning_cache_key

    a = compute_planning_cache_key("spec", "arch")
    b = compute_planning_cache_key("spec", "arch", sprint_id="S1")
    assert a != b


def test_get_cached_plan_miss_when_missing(tmp_path: Path) -> None:
    from software_engineering_team.shared.planning_cache import get_cached_plan

    assert get_cached_plan(tmp_path, "abc") is None


def test_set_and_get_cached_plan_roundtrip(tmp_path: Path) -> None:
    from software_engineering_team.shared.planning_cache import (
        get_cached_plan,
        set_cached_plan,
    )

    assignment = SimpleNamespace(model_dump=lambda: {"tasks": [{"id": "t1"}]})
    set_cached_plan(tmp_path, "cache_key", assignment, [{"req": "x"}], summary="ok")
    cached = get_cached_plan(tmp_path, "cache_key")
    assert cached is not None
    assert cached["summary"] == "ok"
    assert cached["assignment"]["tasks"]


def test_set_cached_plan_accepts_dict(tmp_path: Path) -> None:
    from software_engineering_team.shared.planning_cache import (
        get_cached_plan,
        set_cached_plan,
    )

    set_cached_plan(tmp_path, "k", {"tasks": []}, [], summary="")
    cached = get_cached_plan(tmp_path, "k")
    assert cached is not None


def test_get_cached_plan_corrupt_returns_none(tmp_path: Path) -> None:
    from software_engineering_team.shared.planning_cache import (
        _cache_dir,
        get_cached_plan,
    )

    cache_path = _cache_dir(tmp_path) / "corrupt.json"
    cache_path.write_text("not json{", encoding="utf-8")
    assert get_cached_plan(tmp_path, "corrupt") is None


def test_get_cached_plan_key_mismatch_returns_none(tmp_path: Path) -> None:
    from software_engineering_team.shared.planning_cache import (
        _cache_dir,
        get_cached_plan,
    )

    cache_path = _cache_dir(tmp_path) / "k.json"
    cache_path.write_text('{"cache_key": "different"}', encoding="utf-8")
    assert get_cached_plan(tmp_path, "k") is None


def test_set_cached_plan_handles_unserializable(tmp_path: Path) -> None:
    from software_engineering_team.shared.planning_cache import (
        get_cached_plan,
        set_cached_plan,
    )

    # Pass an assignment whose model_dump raises
    class _Bad:
        def model_dump(self):
            raise RuntimeError("oops")

    set_cached_plan(tmp_path, "k", _Bad(), [], summary="")
    # No file written → still a cache miss
    assert get_cached_plan(tmp_path, "k") is None


# ---------------------------------------------------------------------------
# json_utils
# ---------------------------------------------------------------------------


def test_default_decompose_by_h2_sections() -> None:
    from software_engineering_team.shared.json_utils import default_decompose_by_sections

    content = "## a\nA\n## b\nB"
    out = default_decompose_by_sections(content)
    assert len(out) == 2


def test_default_decompose_by_h1_sections() -> None:
    from software_engineering_team.shared.json_utils import default_decompose_by_sections

    content = "# a\nA\n# b\nB"
    out = default_decompose_by_sections(content)
    assert len(out) == 2


def test_default_decompose_chunks_by_size() -> None:
    from software_engineering_team.shared.json_utils import default_decompose_by_sections

    out = default_decompose_by_sections("x" * 50, chunk_size=10)
    assert len(out) == 5


def test_default_merge_results_empty() -> None:
    from software_engineering_team.shared.json_utils import default_merge_results

    assert default_merge_results([]) == {}


def test_default_merge_results_lists_dicts_scalars() -> None:
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
    from software_engineering_team.shared.json_utils import attempt_fix_output_continuation

    out = attempt_fix_output_continuation(
        llm=MagicMock(spec=[]),  # no base_url/model
        prompt="p",
        raw_text="partial",
        agent_name="A",
    )
    assert out == "partial"


def test_attempt_fix_output_continuation_with_attrs(monkeypatch) -> None:
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


def test_complete_with_continuation_delegates(monkeypatch) -> None:
    from software_engineering_team.shared import json_utils

    called = {}

    def fake_text(llm, prompt, *, agent_name, max_continuation_cycles):
        called["agent"] = agent_name
        return "text!"

    monkeypatch.setattr(json_utils, "complete_text_with_continuation", fake_text)
    out = json_utils.complete_with_continuation(MagicMock(), "prompt", agent_name="X")
    assert out == "text!"
    assert called["agent"] == "X"


def test_parse_json_with_recovery_no_chunks_path(monkeypatch) -> None:
    from software_engineering_team.shared import json_utils

    monkeypatch.setattr(
        "software_engineering_team.shared.llm.complete_json_with_continuation",
        lambda llm, prompt, task_id: {"got": prompt},
    )
    out = json_utils.parse_json_with_recovery(MagicMock(), "p", agent_name="A")
    assert out == {"got": "p"}


def test_parse_json_with_recovery_returns_none_on_exception(monkeypatch) -> None:
    from software_engineering_team.shared import json_utils

    def boom(*a, **kw):
        raise RuntimeError("network")

    monkeypatch.setattr(
        "software_engineering_team.shared.llm.complete_json_with_continuation", boom
    )
    out = json_utils.parse_json_with_recovery(MagicMock(), "p", agent_name="A")
    assert out is None


def test_parse_json_with_recovery_chunked(monkeypatch) -> None:
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
    from software_engineering_team.shared import json_utils

    def boom(*a, **kw):
        raise RuntimeError("chunk failed")

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
