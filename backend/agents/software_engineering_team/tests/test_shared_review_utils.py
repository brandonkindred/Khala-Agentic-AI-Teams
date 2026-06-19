"""Tests for software_engineering_team.shared.review_utils.

These exercise the shared documentation self-review orchestration directly (the
two V2 team wrappers delegate to it). The team-specific prompt, parser, result
type, and LLM invocation are injected, so the helper is tested in isolation from
any one team's models — mirroring ``test_shared_llm_review.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Tuple

from software_engineering_team.shared.review_utils import (
    MAX_DOC_REVIEW_CHUNK_CHARS,
    doc_review_code_chunks,
    run_documentation_self_review,
)

# A prompt template carrying every format field the helper fills in. The doc and
# code bodies are interpolated verbatim, so prompts recorded by the fake invoke
# are exactly what each pass would show the LLM.
_PROMPT = (
    "iter={iteration}/{max_iterations} task={task_description}\n"
    "DOCS:\n{documentation}\nCODE:\n{code}"
)


@dataclass
class _Result:
    """Stand-in for each team's ``DocumentationSelfReviewResult``."""

    documentation: Dict[str, str]
    iterations: int
    final_quality_score: float
    improvements_made: List[str]
    summary: str


def _parse(raw: str) -> Dict[str, Any]:
    """Parse a canned JSON response into the dict the helper expects."""
    return json.loads(raw)


def _response(score: float, *, files: Dict[str, str] | None = None) -> str:
    return json.dumps(
        {
            "quality_score": score,
            "improvements": ["tweak"],
            "files": files or {},
        }
    )


def _recording_invoke(text: str) -> Tuple[Callable[[str], str], List[str]]:
    """Return an ``invoke_model`` that records prompts and returns ``text``."""
    prompts: List[str] = []

    def invoke(prompt: str) -> str:
        prompts.append(prompt)
        return text

    return invoke, prompts


def _make_big_code_file(idx: int, approx_chars: int = 30_000) -> str:
    """Build a ~approx_chars file of many small functions, with a tail sentinel."""
    lines: List[str] = []
    size = 0
    n = 0
    while size < approx_chars:
        line = f"def f{idx}_{n}(a, b):\n    return a + b + {n}"
        lines.append(line)
        size += len(line) + 1
        n += 1
    lines.append(f"# SENTINEL_END_{idx}")
    return "\n".join(lines)


class TestDocReviewCodeChunking:
    """The doc self-review must render its code context without truncation."""

    _NUM_FILES = 6

    def _big_code_files(self) -> Dict[str, str]:
        return {f"src/mod_{i}.py": _make_big_code_file(i) for i in range(self._NUM_FILES)}

    def test_chunks_cover_all_files_without_clipping(self):
        code_files = self._big_code_files()
        chunks = doc_review_code_chunks(code_files)
        # Large input is genuinely split into multiple bounded chunks.
        assert len(chunks) > 1
        joined = "\n".join(chunks)
        # No file silently dropped: every path appears across the chunks.
        for path in code_files:
            assert path in joined
        # No file clipped mid-content: every file's tail sentinel survives.
        for i in range(self._NUM_FILES):
            assert f"SENTINEL_END_{i}" in joined
        # Every chunk stays within the per-call budget.
        for chunk in chunks:
            assert len(chunk) <= MAX_DOC_REVIEW_CHUNK_CHARS

    def test_empty_code_yields_single_placeholder_pass(self):
        assert doc_review_code_chunks({}) == ["(No code context)"]
        # Blank-only content is treated as no code context.
        assert doc_review_code_chunks({"a.py": "   \n"}) == ["(No code context)"]


class TestDocumentationSelfReviewLoop:
    _NUM_FILES = 6

    def _big_code_files(self) -> Dict[str, str]:
        return {f"src/mod_{i}.py": _make_big_code_file(i) for i in range(self._NUM_FILES)}

    def test_large_input_shows_every_chunk_to_llm(self):
        code_files = self._big_code_files()
        n_chunks = len(doc_review_code_chunks(code_files))
        invoke, prompts = _recording_invoke(_response(0.95, files={"docs/readme.md": "Refined"}))
        result = run_documentation_self_review(
            documentation={"docs/readme.md": "old docs"},
            code_files=code_files,
            prompt_template=_PROMPT,
            parse_template=_parse,
            result_factory=_Result,
            invoke_model=invoke,
            task_description="task",
            min_iterations=1,
            max_iterations=1,
        )
        # One LLM call per code chunk; all code shown across the single pass.
        assert len(prompts) == n_chunks
        all_prompts = "\n".join(prompts)
        for i in range(self._NUM_FILES):
            assert f"SENTINEL_END_{i}" in all_prompts
        assert "docs/readme.md" in result.documentation
        assert result.iterations == 1

    def test_small_input_single_call_per_iteration(self):
        invoke, prompts = _recording_invoke(_response(0.95, files={"docs/readme.md": "Refined"}))
        result = run_documentation_self_review(
            documentation={"docs/readme.md": "old"},
            code_files={"src/a.py": "a = 1"},
            prompt_template=_PROMPT,
            parse_template=_parse,
            result_factory=_Result,
            invoke_model=invoke,
            task_description="task",
            min_iterations=1,
            max_iterations=2,
            quality_threshold=0.9,
        )
        # Small code = one chunk = one call; score 0.95 >= 0.9 stops at min_iterations.
        assert len(prompts) == 1
        assert result.iterations == 1
        assert result.final_quality_score == 0.95

    def test_large_documentation_passed_in_full(self):
        """A large doc must reach the LLM uncut (guards against tail truncation)."""
        big_doc = ("Documentation paragraph. " * 600) + "\nDOC_TAIL_SENTINEL"
        assert len(big_doc) > 12_000
        invoke, prompts = _recording_invoke(_response(0.95))
        run_documentation_self_review(
            documentation={"docs/readme.md": big_doc},
            code_files={"src/a.py": "a = 1"},
            prompt_template=_PROMPT,
            parse_template=_parse,
            result_factory=_Result,
            invoke_model=invoke,
            task_description="task",
            min_iterations=1,
            max_iterations=1,
        )
        # One small code chunk = one call, and the doc tail appears in the prompt uncut.
        assert len(prompts) == 1
        assert "DOC_TAIL_SENTINEL" in prompts[0]

    def test_llm_failure_is_resilient_and_reports_progress(self):
        def invoke(_prompt: str) -> str:
            raise RuntimeError("boom")

        seen: List[str] = []
        result = run_documentation_self_review(
            documentation={"docs/readme.md": "old"},
            code_files={"src/a.py": "a = 1"},
            prompt_template=_PROMPT,
            parse_template=_parse,
            result_factory=_Result,
            invoke_model=invoke,
            task_description="task",
            min_iterations=1,
            max_iterations=1,
            detail_callback=seen.append,
        )
        # Every chunk's call fails, but the pass never raises and returns the docs
        # unchanged with the default score.
        assert result.documentation == {"docs/readme.md": "old"}
        assert result.iterations == 1
        assert result.final_quality_score == 0.5
        # Per-iteration and final progress callbacks both fired.
        assert any("iteration 1/1" in m for m in seen)
        assert any("complete" in m for m in seen)

    def test_iteration_score_is_min_across_chunks(self):
        code_files = self._big_code_files()
        n_chunks = len(doc_review_code_chunks(code_files))
        assert n_chunks >= 2
        # First chunk scores high, the rest low → iteration score is the minimum.
        responses = [_response(0.95)] + [_response(0.80)] * (n_chunks - 1)
        idx = {"i": 0}

        def invoke(_prompt: str) -> str:
            resp = responses[idx["i"]] if idx["i"] < len(responses) else responses[-1]
            idx["i"] += 1
            return resp

        result = run_documentation_self_review(
            documentation={"docs/readme.md": "old"},
            code_files=code_files,
            prompt_template=_PROMPT,
            parse_template=_parse,
            result_factory=_Result,
            invoke_model=invoke,
            task_description="task",
            min_iterations=1,
            max_iterations=1,
        )
        assert result.final_quality_score == 0.80

    def test_chunk_failure_suppresses_early_stop(self):
        code_files = self._big_code_files()
        n_chunks = len(doc_review_code_chunks(code_files))
        assert n_chunks >= 2
        # First chunk of iteration 1 fails; every other chunk scores 0.95. Without
        # the failure gate the high score would stop after iteration 1, leaving the
        # failed chunk's code unreviewed. With it, iteration 2 re-reviews all chunks.
        calls = {"n": 0}

        def invoke(_prompt: str) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            return _response(0.95)

        result = run_documentation_self_review(
            documentation={"docs/readme.md": "old"},
            code_files=code_files,
            prompt_template=_PROMPT,
            parse_template=_parse,
            result_factory=_Result,
            invoke_model=invoke,
            task_description="task",
            min_iterations=1,
            max_iterations=2,
            quality_threshold=0.9,
        )
        assert result.iterations == 2
        assert result.final_quality_score == 0.95

    def test_all_chunks_fail_keeps_prior_score_then_recovers(self):
        """When every chunk fails an iteration the score is unchanged; a later
        clean iteration sets it. Covers the ``iteration_score is None`` branch."""
        calls = {"n": 0}

        def invoke(_prompt: str) -> str:
            calls["n"] += 1
            if calls["n"] == 1:  # only chunk of iteration 1 fails
                raise RuntimeError("transient")
            return _response(0.95)

        result = run_documentation_self_review(
            documentation={"docs/readme.md": "old"},
            code_files={"src/a.py": "a = 1"},
            prompt_template=_PROMPT,
            parse_template=_parse,
            result_factory=_Result,
            invoke_model=invoke,
            task_description="task",
            min_iterations=1,
            max_iterations=2,
            quality_threshold=0.9,
        )
        # Iteration 1 (all chunks failed) kept the default score; iteration 2 set 0.95.
        assert result.iterations == 2
        assert result.final_quality_score == 0.95


class TestDocReviewManyChunksWarning:
    def test_warns_when_chunk_count_exceeds_threshold(self, monkeypatch, caplog):
        import software_engineering_team.shared.review_utils as review_utils

        monkeypatch.setattr(review_utils, "MANY_CHUNKS_WARN_THRESHOLD", 0)
        code_files = {f"src/mod_{i}.py": _make_big_code_file(i) for i in range(2)}
        with caplog.at_level("WARNING"):
            chunks = review_utils.doc_review_code_chunks(code_files)
        assert len(chunks) > 0
        assert any("code chunk(s)" in r.message for r in caplog.records)
