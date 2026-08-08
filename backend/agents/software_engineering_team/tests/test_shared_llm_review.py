"""Tests for software_engineering_team.shared.llm_review.run_llm_review.

These exercise the shared LLM-review fallback directly (the two V2 team wrappers
delegate to it). The team-specific prompt/parser/issue type are injected, so the
helper is tested in isolation from any one team's models.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from software_engineering_team.shared.llm_review import run_llm_review, run_team_llm_review


def _task(**overrides):
    from shared.dev_models.models import Task, TaskType

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
_PROMPT_WITH_CONTEXT = "{architecture_context}|{spec_content}|{requirements}|{code}"


def _parse_one_issue(_raw: str):
    return {"issues": [{"description": "bad code", "severity": "high", "file_path": "x.py"}]}


def test_run_llm_review_parses_issues():
    """A small input is reviewed in a single call and parsed issues become
    issue_factory instances."""
    prompts: list[str] = []

    def invoke(prompt: str) -> str:
        prompts.append(prompt)
        return "raw"

    out = run_llm_review(
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
    assert len(out.issues) == 1
    assert out.issues[0].description == "bad code"
    assert out.issues[0].file_path == "x.py"
    assert out.raw_issue_count == 1  # nothing grounded away, so raw == kept


def test_run_llm_review_skips_blank_files():
    """Blank files contribute nothing and trigger no LLM call."""
    calls = {"n": 0}

    def invoke(prompt: str) -> str:
        calls["n"] += 1
        return "raw"

    out = run_llm_review(
        task=_task(),
        files={"empty.py": "   \n\t"},
        prompt_template=_PROMPT,
        parse_template=_parse_one_issue,
        issue_factory=_Issue,
        invoke_model=invoke,
        max_chars=60_000,
        warn_threshold=20,
    )

    assert out.issues == []
    assert out.raw_issue_count == 0
    assert calls["n"] == 0


def test_run_llm_review_chunks_large_file_and_skips_failing_chunk(caplog):
    """A too-large file is split into function-aware chunks (tail not dropped);
    a chunk whose invoke raises is skipped while the others' issues survive, and
    crossing warn_threshold emits a WARNING."""
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

    with caplog.at_level(logging.WARNING, logger="software_engineering_team.shared.llm_review"):
        out = run_llm_review(
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
    assert len(out.issues) == calls["n"] - 1
    assert out.raw_issue_count == len(out.issues)  # nothing grounded away here
    # The many-chunks warning fired because chunk count exceeded warn_threshold=0.
    assert any(
        rec.levelno == logging.WARNING and "large review" in rec.getMessage()
        for rec in caplog.records
    )


def test_run_llm_review_forwards_architecture_context_and_spec_content():
    """``architecture_context``/``spec_content`` reach the prompt when the
    template references them; omitting them defaults to '(none provided)' so
    the fallback reviewer is never silently missing the caller's context."""
    prompts: list[str] = []

    def invoke(prompt: str) -> str:
        prompts.append(prompt)
        return "raw"

    run_llm_review(
        task=_task(),
        files={"x.py": "code"},
        prompt_template=_PROMPT_WITH_CONTEXT,
        parse_template=lambda _raw: {"issues": []},
        issue_factory=_Issue,
        invoke_model=invoke,
        max_chars=60_000,
        warn_threshold=20,
        architecture_context="Layered service architecture.",
        spec_content="All endpoints require auth.",
    )
    assert "Layered service architecture." in prompts[0]
    assert "All endpoints require auth." in prompts[0]

    prompts.clear()
    run_llm_review(
        task=_task(),
        files={"x.py": "code"},
        prompt_template=_PROMPT_WITH_CONTEXT,
        parse_template=lambda _raw: {"issues": []},
        issue_factory=_Issue,
        invoke_model=invoke,
        max_chars=60_000,
        warn_threshold=20,
    )
    assert "(none provided)" in prompts[0]


def test_run_llm_review_preserves_header_on_oversized_single_line():
    """A single source line longer than the cap (a minified bundle) is hard-split,
    and every prompt keeps the ### path ### header so a finding in any tail piece
    stays attributable to its file."""
    prompts: list[str] = []

    def invoke(prompt: str) -> str:
        prompts.append(prompt)
        return "raw"

    line = "DATA = '" + ("a" * 65_000) + "'"  # one unsplittable line over the cap
    assert "\n" not in line and len(line) > 60_000

    run_llm_review(
        task=_task(),
        files={"bundle.py": line},
        prompt_template=_PROMPT,
        parse_template=_parse_one_issue,
        issue_factory=_Issue,
        invoke_model=invoke,
        max_chars=60_000,
        warn_threshold=20,
    )

    assert len(prompts) > 1  # the oversized line was hard-split across prompts
    assert not any(line in prompt for prompt in prompts)  # no single prompt holds it whole
    assert all("### bundle.py ###" in prompt for prompt in prompts)  # header on every piece


def test_run_llm_review_drops_ungrounded_insurance_hallucination(caplog):
    """Meal-planning task + fabricated Insurance Provider finding is dropped
    before return (regression for the LLM-fallback hallucination loop)."""

    def parse_insurance(_raw: str):
        return {
            "issues": [
                {
                    "description": "index.html does not support Insurance Provider ZephyrCare",
                    "severity": "high",
                    "file_path": "index.html",
                    "recommendation": "Add ZephyrCare to the provider dropdown",
                }
            ]
        }

    with caplog.at_level(logging.WARNING, logger="software_engineering_team.shared.llm_review"):
        out = run_llm_review(
            task=_task(
                requirements="Build a meal planning UI for weekly menus",
                description="Meal planner",
                acceptance_criteria=["user can plan meals for the week"],
            ),
            files={"index.html": "<html><body>Meal Planner</body></html>"},
            prompt_template=_PROMPT,
            parse_template=parse_insurance,
            issue_factory=_Issue,
            invoke_model=lambda _p: "raw",
            max_chars=60_000,
            warn_threshold=20,
            enable_llm_review_grounding=True,
        )
    assert out.issues == []
    # The raw count is captured before grounding, so a caller can tell "the LLM
    # fabricated and grounding caught it" apart from "the LLM found nothing".
    assert out.raw_issue_count >= 1
    assert len(out.issues) < out.raw_issue_count
    assert any(
        rec.levelno == logging.WARNING and "ZephyrCare" in rec.getMessage()
        for rec in caplog.records
    ), "Dropped issue should be logged at WARNING with full payload"


def test_run_llm_review_grounding_kill_switch_keeps_ungrounded():
    """enable_llm_review_grounding=False preserves today's behavior (no drop)."""

    def parse_insurance(_raw: str):
        return {
            "issues": [
                {
                    "description": "index.html does not support Insurance Provider ZephyrCare",
                    "severity": "high",
                    "file_path": "index.html",
                }
            ]
        }

    out = run_llm_review(
        task=_task(
            requirements="Build a meal planning UI for weekly menus",
            acceptance_criteria=["user can plan meals for the week"],
        ),
        files={"index.html": "<html></html>"},
        prompt_template=_PROMPT,
        parse_template=parse_insurance,
        issue_factory=_Issue,
        invoke_model=lambda _p: "raw",
        max_chars=60_000,
        warn_threshold=20,
        enable_llm_review_grounding=False,
    )
    assert len(out.issues) == 1
    assert "Insurance Provider" in out.issues[0].description
    assert out.raw_issue_count == 1  # kill switch still reports the raw count


# ---------------------------------------------------------------------------
# run_team_llm_review: the review_context bounding step both V2 teams share
# ---------------------------------------------------------------------------


def test_run_team_llm_review_forwards_architecture_and_spec_content():
    """``review_context`` is rendered and bounded into ``architecture_context``/
    ``spec_content`` before delegating to ``run_llm_review``."""
    from llm_service.clients.dummy import DummyLLMClient
    from shared.dev_models.models import ReviewContext, SystemArchitecture

    prompts: list[str] = []

    def invoke(prompt: str) -> str:
        prompts.append(prompt)
        return "raw"

    architecture = SystemArchitecture(overview="Layered service architecture.")
    review_context = ReviewContext(
        architecture=architecture, spec_content="All endpoints require auth."
    )

    run_team_llm_review(
        llm=DummyLLMClient(),
        task=_task(),
        files={"x.py": "code"},
        prompt_template=_PROMPT_WITH_CONTEXT,
        parse_template=_parse_one_issue,
        issue_factory=_Issue,
        invoke_model=invoke,
        max_chars=60_000,
        warn_threshold=20,
        review_context=review_context,
    )
    assert "Layered service architecture." in prompts[0]
    assert "All endpoints require auth." in prompts[0]


def test_run_team_llm_review_without_review_context_never_touches_llm():
    """``review_context=None`` (the default) is the common case -- no code_review_agent
    context is available yet -- and must never call into ``llm`` at all, so a bare
    test double without ``get_max_context_tokens`` still works."""

    def invoke(prompt: str) -> str:
        return "raw"

    out = run_team_llm_review(
        llm=object(),  # no get_max_context_tokens -- would raise AttributeError if touched
        task=_task(),
        files={"x.py": "code"},
        prompt_template=_PROMPT_WITH_CONTEXT,
        parse_template=_parse_one_issue,
        issue_factory=_Issue,
        invoke_model=invoke,
        max_chars=60_000,
        warn_threshold=20,
    )
    assert len(out.issues) == 1
