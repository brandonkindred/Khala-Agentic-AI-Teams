"""Unit tests for planning_v3_team.spec_digest (section-aware map-reduce)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))

from planning_v3_team import spec_digest  # noqa: E402
from planning_v3_team.spec_digest import (  # noqa: E402
    compute_section_chars,
    map_reduce,
    parse_json_response,
    split_sections,
)

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


# --- split_sections --------------------------------------------------------


def test_split_sections_empty():
    assert split_sections("", 100) == []


def test_split_sections_fits():
    assert split_sections("short text", 100) == ["short text"]


def test_split_sections_at_headings():
    text = "# A\n" + "a" * 50 + "\n# B\n" + "b" * 50 + "\n# C\n" + "c" * 50
    sections = split_sections(text, 60)
    assert len(sections) >= 2
    assert "".join(sections) == text  # nothing dropped
    assert sections[0].startswith("# A")


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


def test_split_sections_oversized_coherent_block_kept_whole():
    # No headings, no blank lines: a single coherent block over budget is kept whole
    # (map_reduce compacts it intelligently rather than slicing mid-content).
    text = "x" * 500
    sections = split_sections(text, 100)
    assert sections == [text]


# --- parse_json_response ---------------------------------------------------


def test_parse_json_plain():
    assert parse_json_response('{"a": 1}') == {"a": 1}


def test_parse_json_fenced():
    assert parse_json_response('```json\n{"a": 2}\n```') == {"a": 2}


def test_parse_json_empty_and_invalid():
    assert parse_json_response("") is None
    assert parse_json_response(None) is None
    assert parse_json_response("not json") is None


def test_parse_json_non_object_returns_none():
    # Valid JSON that is not an object (array / scalar) must be rejected so reducers
    # can rely on a dict-or-None contract.
    assert parse_json_response('["a", "b"]') is None
    assert parse_json_response('"a string"') is None
    assert parse_json_response("42") is None


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


def _multi_heading_doc(n: int, body_chars: int) -> str:
    """Build a markdown doc with ``n`` heading-blocks of ~``body_chars`` each."""
    return "".join(f"# Heading {i}\n" + ("b" * body_chars) + "\n" for i in range(n))


def test_map_reduce_multiple_sections_all_reduced():
    llm = MagicMock()
    llm.get_max_context_tokens.return_value = 1000  # tiny -> floor 8000 chars
    text = _multi_heading_doc(4, 5000)  # 4 blocks ~5000 each -> several <=8000 sections
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
    text = _multi_heading_doc(4, 5000)

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


def test_map_reduce_no_compact_when_client_lacks_surface():
    """An llm without .complete is never asked to compact; section mapped uncompacted."""

    class CtxOnly:
        def get_max_context_tokens(self):
            return 1000  # floor 8000

        # no .complete -> _can_compact False

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
