"""Cross-module smoke tests: multi-file change-surface ``pre_numbered``
``### path ###`` blobs against chunker/coordinator expectations.

The sibling test file (``test_change_surface_chunker_expectations.py``, for
issue #5457) locks the single-file/single-format contract between the
change-surface builder (``change_surface.py``) and the chunker/coordinator
(``chunking.py`` / ``coordinator.py``) and explicitly scopes multi-file
surfaces out. This file closes that gap: a surface spanning *two* files must
still parse, chunk, and reduce correctly -- no file/segment mixing, no path
corruption, no dropped files -- fully offline (scripted dummy LLM client, no
live LLM/network dependency). SE integration is out of scope; this exercises
only the surface->chunker->coordinator contract.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from code_review_agent.chunking import _blocks_from_input, build_review_chunks
from code_review_agent.coordinator import run_coordinator
from code_review_agent.models import CodeReviewInput

from llm_service.clients.dummy import DummyLLMClient
from software_engineering_team.code_review_agent.change_surface import (
    build_change_surface_from_patches,
)

# Two distinct fixtures so cross-file mixing is actually detectable: each
# file's pre-numbered body carries different content and different line
# numbers than the other's.
_PY_CONTENT_A = "def outer():\n    return 1\n\nx = 1\n"
_PY_PATCH_A = "@@ -1,2 +1,2 @@\n def outer():\n-    return 0\n+    return 1\n"

_PY_CONTENT_B = "\n\n\ndef helper():\n    return 2\n\ny = 2\n"
_PY_PATCH_B = "@@ -4,2 +4,2 @@\n def helper():\n-    return 0\n+    return 2\n"


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


def _multi_file_surface():
    return build_change_surface_from_patches(
        {"mod_a.py": _PY_PATCH_A, "mod_b.py": _PY_PATCH_B},
        new_contents={"mod_a.py": _PY_CONTENT_A, "mod_b.py": _PY_CONTENT_B},
    )


def test_blocks_from_input_accepts_multi_file_surface_verbatim() -> None:
    surface = _multi_file_surface()
    input_data = CodeReviewInput(files=dict(surface.blocks), pre_numbered=True, language="python")
    blocks, skipped = _blocks_from_input(input_data)
    assert blocks == list(surface.blocks.items())
    assert skipped == []


def test_build_review_chunks_covers_both_files_without_cross_contamination() -> None:
    """Both files' bodies must be fully covered, each path appearing in
    exactly one segment (``build_review_chunks``'s own no-duplicate-path-per-
    chunk contract), with no interleaving or truncation across files."""
    surface = _multi_file_surface()
    blocks = list(surface.blocks.items())
    chunks = build_review_chunks(blocks, max_chars=4096, pre_numbered=True)
    segments = [seg for chunk in chunks for seg in chunk.segments]

    seg_paths = [seg.path for seg in segments]
    assert sorted(seg_paths) == ["mod_a.py", "mod_b.py"]
    assert len(seg_paths) == len(set(seg_paths))  # no path duplicated across segments

    body_by_path = dict(blocks)
    for seg in segments:
        assert seg.content == body_by_path[seg.path]
        assert seg.pre_numbered is True

    for chunk in chunks:
        chunk_paths = [seg.path for seg in chunk.segments]
        assert len(chunk_paths) == len(set(chunk_paths))


def test_multi_file_surface_through_coordinator_preserves_per_file_line_citations() -> None:
    """Full round trip -- builder -> chunker header parse -> segment/chunk ->
    map phase -> coordinator normalization -- must attribute each file's
    citation to that file only, never bleeding into the other's path/line."""
    surface = _multi_file_surface()
    body_a = surface.blocks["mod_a.py"]
    body_b = surface.blocks["mod_b.py"]

    m_a = re.search(r"^(\d+): ", body_a, re.M)
    m_b = re.search(r"^(\d+): ", body_b, re.M)
    assert m_a is not None and m_b is not None, "test premise: both bodies must be pre-numbered"
    cited_line_a = int(m_a.group(1))
    cited_line_b = int(m_b.group(1))

    client = _ScriptedClient(
        [
            {
                "approved": False,
                "issues": [
                    {
                        "severity": "high",
                        "category": "logic",
                        "file_path": "mod_a.py",
                        "line": cited_line_a,
                        "description": "off-by-one in mod_a",
                        "suggestion": "fix it",
                    },
                    {
                        "severity": "medium",
                        "category": "logic",
                        "file_path": "mod_b.py",
                        "line": cited_line_b,
                        "description": "off-by-one in mod_b",
                        "suggestion": "fix it too",
                    },
                ],
                "summary": "found two",
                "spec_compliance_notes": "",
            }
        ]
    )

    result = run_coordinator(
        client,
        CodeReviewInput(files=dict(surface.blocks), pre_numbered=True, language="python"),
    )

    issues_by_path = {issue.file_path: issue for issue in result.issues}
    assert set(issues_by_path) == {"mod_a.py", "mod_b.py"}
    assert issues_by_path["mod_a.py"].line == cited_line_a
    assert issues_by_path["mod_b.py"].line == cited_line_b
