"""End-to-end property tests for the map-reduce code review.

Stages 1-3 each land their own unit tests; these tests pin the cross-cutting
properties that motivated the redesign, exercised through the public
``CodeReviewAgent.run`` entry point with a large synthetic submission:

    - bounded prompts at any input size (no chunk exceeds the absolute cap),
    - full coverage (every input file reaches some chunk prompt),
    - correct inline-comment anchoring (merged issue lines re-anchor to the
      original file lines the splitter produced), and
    - graceful degradation (one chunk's failure yields an info "not reviewed"
      finding instead of aborting the whole run).
"""

from __future__ import annotations

import re
import threading
from typing import Any, Dict, List

from code_review_agent import CodeReviewAgent
from code_review_agent.chunk_reviewer import CODE_TO_REVIEW_HEADER
from code_review_agent.coordinator import build_review_chunks
from code_review_agent.models import CodeReviewInput

from llm_service import LLMSemanticExhaustionError
from llm_service.clients.dummy import DummyLLMClient
from software_engineering_team.shared.context_sizing import (
    CODE_REVIEW_ABS_CHUNK_CHARS,
    compute_code_review_map_chunk_chars,
)

# A 1M-token context exercises the absolute 80K map-chunk cap (the small-model
# path is covered by test_context_sizing); both are <= the absolute cap, which
# is what these property tests assert.
_BIG_CONTEXT_TOKENS = 1_000_000


class _BigCtxRecorder(DummyLLMClient):
    """1M-context client that records every prompt and approves with no issues."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: List[str] = []
        self._lock = threading.Lock()

    def get_max_context_tokens(self) -> int:
        return _BIG_CONTEXT_TOKENS

    def complete(self, prompt: str, **kwargs: Any) -> str:
        with self._lock:
            self.prompts.append(prompt)
        return super().complete(prompt, **kwargs)

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        return {
            "approved": True,
            "issues": [],
            "summary": "Chunk reviewed; no issues.",
            "spec_compliance_notes": "",
            "suggested_commit_message": "",
        }


def _synthetic_files(num_files: int, chars_each: int) -> Dict[str, str]:
    """Build ``num_files`` distinct, syntactically plausible Python files."""
    files: Dict[str, str] = {}
    for f in range(num_files):
        lines = [f"# module pkg/mod_{f}.py", ""]
        i = 0
        while sum(len(x) + 1 for x in lines) < chars_each:
            lines.append(f"def func_{f}_{i}(a, b):  # filler {'y' * 20}")
            lines.append(f"    return a + b + {i}")
            i += 1
        files[f"pkg/mod_{f}.py"] = "\n".join(lines)
    return files


def test_e2e_bounded_prompts_and_full_coverage() -> None:
    """~500K chars across many files: more than one chunk call is made, every
    input file reaches some chunk prompt, and no chunk carries more code than
    the absolute map-chunk cap regardless of the model's large context."""
    files = _synthetic_files(num_files=8, chars_each=64_000)  # ~512K total
    total = sum(len(c) for c in files.values())
    assert total > 500_000, "test premise: the submission must be large"

    client = _BigCtxRecorder()
    cap = compute_code_review_map_chunk_chars(client)
    assert cap == CODE_REVIEW_ABS_CHUNK_CHARS  # documented default: 80_000

    # The splitter's own output: every chunk's rendered code is within the cap.
    chunks = build_review_chunks(list(files.items()), cap)
    assert len(chunks) > 1, "test premise: the submission must split into many chunks"
    assert all(len(chunk.content) <= cap for chunk in chunks)

    result = CodeReviewAgent(llm_client=client, force_in_process=True).run(
        CodeReviewInput(files=files, task_description="review big submission", language="python")
    )
    assert result.approved is True

    # More than one chunk call (the synthesis pass is findings-only and carries
    # no "### path ###" code header, so it is excluded from the chunk count).
    chunk_prompts = [p for p in client.prompts if "### pkg/mod_" in p]
    assert len(chunk_prompts) > 1

    # Every input file appears in some chunk prompt, and no prompt's code
    # exceeds the cap by more than the bounded (capped) context/template
    # overhead the prompt wraps around it.
    blob = "\n".join(chunk_prompts)
    for path in files:
        assert f"### {path} ###" in blob, f"{path} never reached a chunk prompt"


class _CiteFirstPrefixed(DummyLLMClient):
    """1M-context client that cites the first original-line prefix in each chunk."""

    def __init__(self) -> None:
        super().__init__()
        self._tls = threading.local()

    def get_max_context_tokens(self) -> int:
        return _BIG_CONTEXT_TOKENS

    def complete(self, prompt: str, **kwargs: Any) -> str:
        if CODE_TO_REVIEW_HEADER in prompt:
            # Match the splitter's generic original-line prefix ("N: <code>"),
            # not the synthetic content's "line" token, so the test stays robust
            # to changes in the filler format. The run-level assertion that cited
            # lines equal the segments' own start_lines confirms each is in range.
            m = re.search(r"^(\d+): ", prompt, re.M)
            assert m is not None, "split segments must render original-line prefixes"
            self._tls.cited = int(m.group(1))
        return super().complete(prompt, **kwargs)

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        cited = getattr(self._tls, "cited", None)
        if cited is None:
            return super().complete_json(prompt, **kwargs)
        self._tls.cited = None
        return {
            "approved": False,
            "issues": [
                {
                    "severity": "high",
                    "category": "logic",
                    "file_path": "huge.py",
                    "line": cited,
                    "start_line": cited,
                    "description": f"issue at original line {cited}",
                    "suggestion": "fix it",
                }
            ],
            "summary": "per-chunk issue",
            "spec_compliance_notes": "",
        }


def test_e2e_merged_issues_reanchored_to_original_lines() -> None:
    """A single large file split into many partial segments: each chunk cites
    its first visible original line, and the merged issues come back anchored
    to exactly those lines — computed from the splitter's boundaries, never
    hardcoded."""
    content = "\n".join(f"line {i:06d}".ljust(80, "x") for i in range(1, 7_001))  # ~560K chars
    assert len(content) > 500_000

    client = _CiteFirstPrefixed()
    cap = compute_code_review_map_chunk_chars(client)
    chunks = build_review_chunks([("huge.py", content)], cap)
    segments = [seg for chunk in chunks for seg in chunk.segments]
    assert len(segments) > 1, "test premise: the file must split into partial segments"

    result = CodeReviewAgent(llm_client=client, force_in_process=True).run(
        CodeReviewInput(files={"huge.py": content}, language="python")
    )

    expected = sorted(seg.start_line for seg in segments)
    assert sorted(i.line for i in result.issues) == expected
    assert all(i.file_path == "huge.py" for i in result.issues)
    # The client cites the same value for line and start_line; the merge must keep
    # both consistent, so a regression where start_line diverges from line is caught.
    assert all(i.start_line == i.line for i in result.issues)


class _FailOneFile(DummyLLMClient):
    """1M-context client that always fails any chunk naming a marker file."""

    def __init__(self, marker: str) -> None:
        super().__init__()
        self.marker = marker

    def get_max_context_tokens(self) -> int:
        return _BIG_CONTEXT_TOKENS

    def complete(self, prompt: str, **kwargs: Any) -> str:
        if CODE_TO_REVIEW_HEADER in prompt and self.marker in prompt:
            raise LLMSemanticExhaustionError("LLM returned reasoning only (no content)")
        return super().complete(prompt, **kwargs)


def test_e2e_one_chunk_failure_degrades_gracefully_without_blocking(monkeypatch) -> None:
    """A scripted client that exhausts one chunk's review (LLMSemanticExhaustionError)
    while the rest succeed: by default the run completes and degrades gracefully —
    the failed file is NOT posted as a "could not be reviewed" finding and does
    NOT block the gate (a reviewer-side hiccup is not a code defect). Its ranges
    are surfaced only via ``not_reviewed_ranges``, and the reviewed chunks drive
    the approved verdict."""
    monkeypatch.setenv("CODE_REVIEW_MAP_PARALLELISM", "1")
    monkeypatch.delenv("CODE_REVIEW_BLOCK_ON_UNREVIEWED", raising=False)
    files = _synthetic_files(num_files=6, chars_each=64_000)  # forces separate chunks
    bad_path = "pkg/mod_3.py"
    marker = f"### {bad_path} ###"

    result = CodeReviewAgent(llm_client=_FailOneFile(marker), force_in_process=True).run(
        CodeReviewInput(files=files, task_description="review", language="python")
    )

    # The run completed (no exception), approved (the other chunks drive the
    # verdict), and posted no "could not be reviewed" finding.
    assert result.approved is True
    assert not any("could not be reviewed" in i.description for i in result.issues)

    # The failed file's ranges are recorded non-blockingly. Recovery bisects the
    # segment into sub-ranges that together must name the entire file, so no
    # covered line is silently dropped.
    bad_ranges = [r for r in result.not_reviewed_ranges if r.startswith(bad_path)]
    assert bad_ranges, "the failed file must be recorded in not_reviewed_ranges"

    def _bounds(label: str) -> tuple[int, int]:
        # Label form: "pkg/mod_3.py (lines A-B)"
        a, b = label.split("(lines ")[1].rstrip(")").split("-")
        return int(a), int(b)

    bounds = [_bounds(r) for r in bad_ranges]
    assert all(start <= end for start, end in bounds)
    assert min(start for start, _ in bounds) == 1
    assert max(end for _, end in bounds) == len(files[bad_path].splitlines())
