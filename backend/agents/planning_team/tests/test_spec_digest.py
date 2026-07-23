"""Unit tests for planning_team.spec_digest (section-aware map-reduce)."""

import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))

from planning_team import spec_digest  # noqa: E402
from planning_team.spec_digest import (  # noqa: E402
    compute_section_chars,
    map_reduce,
    split_sections,
)
from planning_team.tests.conftest import multi_heading_doc  # noqa: E402

# --- compute_section_chars -------------------------------------------------


def test_compute_section_chars_exact():
    llm = MagicMock()
    llm.get_max_context_tokens.return_value = 100_000
    # (100000 - 6000 - 4096) * 3.5
    assert compute_section_chars(llm) == int((100_000 - 6000 - 4096) * 3.5)


def test_compute_section_chars_floor_for_tiny_model():
    llm = MagicMock()
    llm.get_max_context_tokens.return_value = 1000  # below reserves -> floor
    assert compute_section_chars(llm) == spec_digest._MIN_SECTION_CHARS


def test_compute_section_chars_default_when_no_method():
    obj = object()  # no get_max_context_tokens attribute
    expected = int((spec_digest._DEFAULT_CONTEXT_TOKENS - 6000 - 4096) * 3.5)
    assert compute_section_chars(obj) == expected


def test_compute_section_chars_default_when_method_raises():
    class Raises:
        def get_max_context_tokens(self):
            raise RuntimeError("misconfigured client")

    expected = int((spec_digest._DEFAULT_CONTEXT_TOKENS - 6000 - 4096) * 3.5)
    assert compute_section_chars(Raises()) == expected


# --- split_sections --------------------------------------------------------


def test_split_sections_empty():
    assert split_sections("", 100) == []


def test_split_sections_rejects_non_positive_max_chars():
    import pytest

    with pytest.raises(ValueError):
        split_sections("anything", 0)


def test_split_sections_fits():
    assert split_sections("short text", 100) == ["short text"]


def test_split_sections_at_headings():
    text = "# A\n" + "a" * 50 + "\n# B\n" + "b" * 50 + "\n# C\n" + "c" * 50
    sections = split_sections(text, 60)
    assert len(sections) >= 2
    assert "".join(sections) == text  # nothing dropped
    assert sections[0].startswith("# A")


def test_split_sections_indented_headings():
    # CommonMark allows up to 3 leading spaces before a heading.
    text = "  # A\n" + "a" * 50 + "\n   # B\n" + "b" * 50
    sections = split_sections(text, 60)
    assert len(sections) >= 2
    assert "".join(sections) == text  # lossless
    assert sections[0].startswith("  # A")


def test_split_sections_leading_text_before_first_heading():
    text = "intro line\n\n# H1\n" + "a" * 80 + "\n# H2\n" + "b" * 80
    sections = split_sections(text, 60)
    assert "".join(sections) == text  # leading text preserved, nothing dropped
    assert sections[0].startswith("intro line")


def test_split_sections_oversized_block_after_buffered_block():
    # Small heading-block buffered, then an oversized blank-line-free heading-block.
    text = "# A\nshort\n# B\n" + "x" * 200
    sections = split_sections(text, 100)
    assert "".join(sections) == text
    assert any(len(s) > 100 for s in sections)  # oversized block kept whole


def test_split_sections_oversized_block_splits_on_blanklines():
    # No headings; one big block separated by blank lines.
    block = "para1 " * 20 + "\n\n" + "para2 " * 20
    sections = split_sections(block, 80)
    assert len(sections) >= 2
    assert "".join(sections) == block


def test_split_sections_crlf_blank_lines():
    # Windows CRLF blank lines must still split (no headings, so blank-line path).
    block = "para1 " * 20 + "\r\n\r\n" + "para2 " * 20
    sections = split_sections(block, 80)
    assert len(sections) >= 2
    assert "".join(sections) == block  # lossless even with CRLF


def test_split_sections_oversized_coherent_block_kept_whole():
    # No headings, no blank lines: a single coherent block over budget is kept whole
    # (map_reduce compacts it intelligently rather than slicing mid-content).
    text = "x" * 500
    sections = split_sections(text, 100)
    assert sections == [text]


def test_env_positive_int(monkeypatch):
    monkeypatch.setenv("PLANNING_TEST_INT", "120")
    assert spec_digest._env_positive_int("PLANNING_TEST_INT", 50) == 120
    monkeypatch.setenv("PLANNING_TEST_INT", "garbage")
    assert spec_digest._env_positive_int("PLANNING_TEST_INT", 50) == 50  # garbage -> default
    monkeypatch.setenv("PLANNING_TEST_INT", "0")
    assert spec_digest._env_positive_int("PLANNING_TEST_INT", 50) == 50  # non-positive -> default
    monkeypatch.delenv("PLANNING_TEST_INT", raising=False)
    assert spec_digest._env_positive_int("PLANNING_TEST_INT", 50) == 50  # unset -> default


# --- map_reduce ------------------------------------------------------------


def _identity_reduce(parts):
    return {"parts": list(parts)}


def test_map_reduce_empty_returns_fallback():
    llm = MagicMock()
    llm.get_max_context_tokens.return_value = 16384
    fb = {"fallback": True}
    out = map_reduce(
        "   ",
        llm,
        content_description="x",
        map_fn=lambda *a: {"k": 1},
        reduce_fn=_identity_reduce,
        fallback=fb,
    )
    assert out is fb


def test_map_reduce_single_section_one_map_call():
    llm = MagicMock()
    llm.get_max_context_tokens.return_value = 16384
    calls = []

    def _map(section, _llm, idx, total):
        calls.append((section, idx, total))
        return {"v": section}

    out = map_reduce(
        "small",
        llm,
        content_description="x",
        map_fn=_map,
        reduce_fn=_identity_reduce,
        fallback={},
    )
    assert len(calls) == 1
    assert calls[0][2] == 1  # total == 1
    assert out == {"parts": [{"v": "small"}]}


def test_map_reduce_warns_on_many_sections(caplog):
    """A very large input that splits into many sections logs a warning (no cap, no drop)."""
    llm = MagicMock()
    llm.get_max_context_tokens.return_value = 1000  # floor 8000 chars
    # 51 heading-blocks of ~5k each -> 51 sections (each block alone exceeds half the
    # 8000 budget, so packing keeps them separate), above the _MANY_SECTIONS_WARN=50 line.
    text = multi_heading_doc(51, 5000)
    with caplog.at_level("WARNING"):
        out = map_reduce(
            text,
            llm,
            content_description="spec",
            map_fn=lambda *a: {"ok": 1},
            reduce_fn=_identity_reduce,
            fallback={},
        )
    assert any("sections" in r.message for r in caplog.records)
    assert len(out["parts"]) > 50  # every section still processed, nothing dropped


def test_map_reduce_multiple_sections_all_reduced():
    llm = MagicMock()
    llm.get_max_context_tokens.return_value = 1000  # tiny -> floor 8000 chars
    text = multi_heading_doc(4, 5000)  # 4 blocks ~5000 each -> several <=8000 sections
    seen = []

    def _map(section, _llm, idx, total):
        seen.append(idx)
        return {"len": len(section)}

    out = map_reduce(
        text,
        llm,
        content_description="x",
        map_fn=_map,
        reduce_fn=_identity_reduce,
        fallback={},
    )
    assert len(seen) >= 2
    assert len(out["parts"]) == len(seen)


def test_map_reduce_skips_none_results():
    llm = MagicMock()
    llm.get_max_context_tokens.return_value = 1000
    text = multi_heading_doc(4, 5000)

    def _map(section, _llm, idx, total):
        return None if idx == 0 else {"ok": idx}

    out = map_reduce(
        text,
        llm,
        content_description="x",
        map_fn=_map,
        reduce_fn=_identity_reduce,
        fallback={"fb": 1},
    )
    # First section skipped (None), later ones kept.
    assert all(p.get("ok") != 0 for p in out["parts"])
    assert len(out["parts"]) >= 1


def test_map_reduce_all_fail_returns_fallback():
    llm = MagicMock()
    llm.get_max_context_tokens.return_value = 16384

    def _boom(*_a):
        raise RuntimeError("map boom")

    fb = {"fb": True}
    out = map_reduce(
        "data",
        llm,
        content_description="x",
        map_fn=_boom,
        reduce_fn=_identity_reduce,
        fallback=fb,
    )
    assert out is fb


def test_map_reduce_oversized_section_is_compacted(monkeypatch):
    """A section exceeding the per-section budget is compacted before mapping."""
    llm = MagicMock()
    llm.get_max_context_tokens.return_value = 1000  # floor 8000 chars

    compact_calls = []

    def fake_compact(text, *, max_chars, llm, content_description):
        compact_calls.append(content_description)
        return "COMPACTED"

    monkeypatch.setattr(spec_digest, "compact_text", fake_compact)

    # Single blank-line-free, heading-free block > 8000 chars -> one oversized section.
    text = "q" * 9000

    mapped = []

    def _map(section, _llm, idx, total):
        mapped.append(section)
        return {"s": section}

    map_reduce(
        text,
        llm,
        content_description="spec",
        map_fn=_map,
        reduce_fn=_identity_reduce,
        fallback={},
    )
    assert compact_calls, "compact_text should have been called for oversized section"
    assert mapped == ["COMPACTED"]


def test_map_reduce_no_compact_when_client_lacks_complete():
    """An llm without .complete is never asked to compact; section mapped uncompacted."""

    class CtxOnly:
        def get_max_context_tokens(self):
            return 1000  # floor 8000

        # no .complete -> supports_compaction False

    text = "w" * 9000
    mapped = []

    def _map(section, _llm, idx, total):
        mapped.append(len(section))
        return {"ok": 1}

    map_reduce(
        text,
        CtxOnly(),
        content_description="spec",
        map_fn=_map,
        reduce_fn=_identity_reduce,
        fallback={},
    )
    # Section passed through untruncated (no compaction, full 9000 chars in one section).
    assert mapped == [9000]


def test_map_reduce_compacts_when_complete_present_without_context(monkeypatch):
    """Callable complete alone is enough to enter compact_text (ctx size is optional)."""

    class CompleteOnly:
        def complete(self, *a, **k):
            return "unused"

        # no get_max_context_tokens — compute_section_chars falls back to default

    compact_calls = []

    def fake_compact(text, *, max_chars, llm, content_description):
        compact_calls.append(content_description)
        return "COMPACTED"

    monkeypatch.setattr(spec_digest, "compact_text", fake_compact)

    # Force an oversized section: tiny returned section budget via monkeypatch.
    monkeypatch.setattr(spec_digest, "compute_section_chars", lambda _llm: 8000)

    text = "q" * 9000
    mapped = []

    def _map(section, _llm, idx, total):
        mapped.append(section)
        return {"s": section}

    map_reduce(
        text,
        CompleteOnly(),
        content_description="spec",
        map_fn=_map,
        reduce_fn=_identity_reduce,
        fallback={},
    )
    assert compact_calls, "compact_text should run when only complete is present"
    assert mapped == ["COMPACTED"]


def test_map_reduce_compact_failure_uses_uncompacted_section(monkeypatch):
    """A raising compact_text must not bubble out; the section is mapped uncompacted."""
    llm = MagicMock()
    llm.get_max_context_tokens.return_value = 1000  # floor 8000 chars

    def boom(*a, **k):
        raise RuntimeError("transient LLM error")

    monkeypatch.setattr(spec_digest, "compact_text", boom)

    text = "q" * 9000  # one oversized section
    mapped = []

    def _map(section, _llm, idx, total):
        mapped.append(len(section))
        return {"ok": 1}

    out = map_reduce(
        text,
        llm,
        content_description="spec",
        map_fn=_map,
        reduce_fn=_identity_reduce,
        fallback={"fb": True},
    )
    # compact_text raised -> fell back to the original chunk, still mapped (no exception).
    assert mapped == [9000]
    assert out == {"parts": [{"ok": 1}]}


# --- parallelism ------------------------------------------------------------


def test_map_parallelism_delegates_to_env_positive_int(monkeypatch):
    monkeypatch.setenv("PLANNING_MAP_PARALLELISM", "7")
    assert spec_digest._map_parallelism() == 7
    monkeypatch.setenv("PLANNING_MAP_PARALLELISM", "garbage")
    assert spec_digest._map_parallelism() == spec_digest._DEFAULT_MAP_PARALLELISM
    monkeypatch.delenv("PLANNING_MAP_PARALLELISM", raising=False)
    assert spec_digest._map_parallelism() == spec_digest._DEFAULT_MAP_PARALLELISM


def test_map_reduce_max_workers_threads_through_to_parallel_map(monkeypatch):
    llm = MagicMock()
    llm.get_max_context_tokens.return_value = 1000  # floor 8000 chars
    text = multi_heading_doc(4, 5000)  # multiple sections -> parallel path

    captured = {}

    def fake_parallel_map(items, fn, *, max_workers, skip_none):
        captured["max_workers"] = max_workers
        captured["skip_none"] = skip_none
        return [fn(item) for item in items]

    monkeypatch.setattr(spec_digest, "parallel_map", fake_parallel_map)

    out = map_reduce(
        text,
        llm,
        content_description="x",
        map_fn=lambda *a: {"ok": 1},
        reduce_fn=_identity_reduce,
        fallback={},
        max_workers=3,
    )
    assert captured["max_workers"] == 3
    assert captured["skip_none"] is False
    assert len(out["parts"]) >= 2


def test_map_reduce_runs_sections_across_multiple_threads():
    """With max_workers > 1 and enough sections, map_fn actually runs on >1 thread."""
    llm = MagicMock()
    llm.get_max_context_tokens.return_value = 1000  # floor 8000 chars
    text = multi_heading_doc(8, 5000)

    thread_names = []
    lock = threading.Lock()

    def _map(section, _llm, idx, total):
        with lock:
            thread_names.append(threading.current_thread().name)
        return {"idx": idx}

    map_reduce(
        text,
        llm,
        content_description="x",
        map_fn=_map,
        reduce_fn=_identity_reduce,
        fallback={},
        max_workers=4,
    )
    assert len(thread_names) == 8
    assert len(set(thread_names)) > 1
