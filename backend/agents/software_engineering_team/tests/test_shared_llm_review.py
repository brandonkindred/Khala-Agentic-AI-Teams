"""Tests for software_engineering_team.shared.llm_review.run_llm_review.

These exercise the shared LLM-review fallback directly (the two V2 team wrappers
delegate to it). The team-specific prompt/parser/issue type are injected, so the
helper is tested in isolation from any one team's models.
"""

from __future__ import annotations

from dataclasses import dataclass

from software_engineering_team.shared.llm_review import run_llm_review


def _task(**overrides):
    from software_engineering_team.shared.models import Task, TaskType

    base = dict(
        id="t1",
        type=TaskType.BACKEND,
        title="T",
        description="desc",
        requirements="reqs",
        assignee="backend",
        acceptance_criteria=["AC"],
    )
    base.update(overrides)
    return Task(**base)


@dataclass
class _Issue:
    source: str = ""
    severity: str = "medium"
    description: str = ""
    file_path: str = ""
    recommendation: str = ""


_PROMPT = "reqs={requirements} ac={acceptance_criteria}\n{code}"


def _parse_one_issue(_raw: str):
    return {"issues": [{"description": "bad code", "severity": "high", "file_path": "x.py"}]}


def test_run_llm_review_parses_issues():
    """A small input is reviewed in a single call and parsed issues become
    issue_factory instances."""
    prompts: list[str] = []

    def invoke(prompt: str) -> str:
        prompts.append(prompt)
        return "raw"

    issues = run_llm_review(
        task=_task(),
        files={"x.py": "code"},
        prompt_template=_PROMPT,
        parse_template=_parse_one_issue,
        issue_factory=_Issue,
        invoke_model=invoke,
        max_chars=60_000,
        warn_threshold=20,
    )

    assert len(prompts) == 1  # single call for a small input
    assert len(issues) == 1
    assert issues[0].description == "bad code"
    assert issues[0].file_path == "x.py"


def test_run_llm_review_skips_blank_files():
    """Blank files contribute nothing and trigger no LLM call."""
    calls = {"n": 0}

    def invoke(prompt: str) -> str:
        calls["n"] += 1
        return "raw"

    issues = run_llm_review(
        task=_task(),
        files={"empty.py": "   \n\t"},
        prompt_template=_PROMPT,
        parse_template=_parse_one_issue,
        issue_factory=_Issue,
        invoke_model=invoke,
        max_chars=60_000,
        warn_threshold=20,
    )

    assert issues == []
    assert calls["n"] == 0


def test_run_llm_review_chunks_large_file_and_skips_failing_chunk():
    """A too-large file is split into function-aware chunks (tail not dropped);
    a chunk whose invoke raises is skipped while the others' issues survive."""
    prompts: list[str] = []
    calls = {"n": 0}

    def invoke(prompt: str) -> str:
        calls["n"] += 1
        prompts.append(prompt)
        if calls["n"] == 1:
            raise RuntimeError("model unavailable")
        return "raw"

    big = "\n".join(f"def fn_{i:04d}():\n    return {i}" for i in range(2500))
    assert len(big) > 60_000  # forces more than one chunk

    issues = run_llm_review(
        task=_task(),
        files={"big.py": big},
        prompt_template=_PROMPT,
        parse_template=_parse_one_issue,
        issue_factory=_Issue,
        invoke_model=invoke,
        max_chars=60_000,
        warn_threshold=0,  # exercise the many-chunks warning path
    )

    assert calls["n"] > 1  # every chunk attempted
    joined = "\n".join(prompts)
    assert "fn_0000" in joined  # head reviewed
    assert "fn_2499" in joined  # tail reviewed, not truncated
    # First chunk raised; remaining chunks each yield one parsed issue.
    assert len(issues) == calls["n"] - 1
