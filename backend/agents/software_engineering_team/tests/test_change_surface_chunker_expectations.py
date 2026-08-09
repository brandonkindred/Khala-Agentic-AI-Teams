"""Cross-module tests: change-surface ``pre_numbered`` ``### path ###`` blobs
against chunker/coordinator expectations.

The change-surface builder (``change_surface.py``) and the chunker/coordinator
(``chunking.py`` / ``coordinator.py``) are two sides of the same
``pre_numbered=True`` contract: the builder promises to emit ``### path ###``
blocks whose bodies carry ``N: `` original-line prefixes, and the chunker
promises to parse, segment, and chunk that exact shape without re-splitting on
construct boundaries or corrupting the embedded line numbers. The builder's
own unit tests (``test_change_surface_*.py``) lock its output in isolation;
these tests assert the *consumer* side actually accepts that output, offline,
with no live LLM. Multi-file surfaces are covered by a sibling issue and are
out of scope here -- this file is single-file/single-format focused.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from code_review_agent.chunking import (
    _blocks_from_input,
    build_review_chunks,
    split_block_into_segments,
)
from code_review_agent.coordinator import run_coordinator
from code_review_agent.models import CodeReviewInput

from llm_service.clients.dummy import DummyLLMClient
from software_engineering_team.code_review_agent.change_surface import (
    build_change_surface_from_patches,
)
from software_engineering_team.shared.chunking import parse_code_into_file_blocks

# Same fixture shape as ``test_change_surface_from_patches.py``: touching the
# body line of ``outer`` expands (via AST) to the enclosing function.
_PY_CONTENT = "def outer():\n    return 1\n\nx = 1\n"
_PY_PATCH = "@@ -1,2 +1,2 @@\n def outer():\n-    return 0\n+    return 1\n"


class _ScriptedClient(DummyLLMClient):
    """Returns a canned JSON response on each ``complete_json`` call."""

    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        super().__init__()
        self._responses = list(responses)
        self._idx = 0

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        resp = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1
        return resp


def _single_file_surface():
    return build_change_surface_from_patches(
        {"mod.py": _PY_PATCH},
        new_contents={"mod.py": _PY_CONTENT},
    )


def test_change_surface_code_round_trips_through_chunker_header_parser() -> None:
    """``surface.code`` parses back into exactly ``surface.blocks`` via the
    chunker's own ``### path ###`` header parser (the function
    ``_blocks_from_input`` delegates to for the legacy ``code=`` channel)."""
    surface = _single_file_surface()
    parsed = parse_code_into_file_blocks(surface.code)
    assert parsed == list(surface.blocks.items())


def test_blocks_from_input_accepts_surface_code_verbatim() -> None:
    surface = _single_file_surface()
    input_data = CodeReviewInput(code=surface.code, pre_numbered=True, language="python")
    blocks, skipped = _blocks_from_input(input_data)
    assert blocks == list(surface.blocks.items())
    assert skipped == []


def test_pre_numbered_surface_segments_split_on_line_boundaries_only() -> None:
    """A pre-numbered body large enough to force a split must be sliced on
    plain line boundaries, not construct boundaries (the ``N: `` prefixes
    defeat boundary detection, per ``split_block_into_segments``'s contract),
    and must fully reconstruct when concatenated back together."""
    body = "\n".join(f"{n}: line_{n}()" for n in range(1, 51))
    segments = split_block_into_segments("big.py", body, max_chars=80, pre_numbered=True)
    assert len(segments) > 1
    assert all(seg.pre_numbered for seg in segments)
    assert all(seg.path == "big.py" for seg in segments)
    assert "".join(seg.content for seg in segments) == body


def test_build_review_chunks_accepts_pre_numbered_surface_block() -> None:
    surface = _single_file_surface()
    path, body = next(iter(surface.blocks.items()))
    chunks = build_review_chunks([(path, body)], max_chars=4096, pre_numbered=True)
    segments = [seg for chunk in chunks for seg in chunk.segments]
    assert len(segments) == 1
    assert segments[0].path == path
    assert segments[0].content == body
    assert segments[0].pre_numbered is True


def test_change_surface_through_coordinator_preserves_original_line_citations() -> None:
    """Full round trip -- builder -> chunker header parse -> segment/chunk ->
    map phase -> coordinator normalization -- must preserve the original
    pre-numbered line the surface embedded. No live LLM: a scripted dummy
    client cites a line pulled straight out of the surface body."""
    surface = _single_file_surface()
    path, body = next(iter(surface.blocks.items()))
    m = re.search(r"^(\d+): ", body, re.M)
    assert m is not None, "test premise: surface body must carry a pre-numbered line"
    cited_line = int(m.group(1))

    client = _ScriptedClient(
        [
            {
                "approved": False,
                "issues": [
                    {
                        "severity": "high",
                        "category": "logic",
                        "file_path": path,
                        "line": cited_line,
                        "description": "off-by-one",
                        "suggestion": "fix it",
                    }
                ],
                "summary": "found one",
                "spec_compliance_notes": "",
            }
        ]
    )

    result = run_coordinator(
        client,
        CodeReviewInput(code=surface.code, pre_numbered=True, language="python"),
    )

    assert len(result.issues) == 1
    assert result.issues[0].line == cited_line
    assert result.issues[0].file_path == path
