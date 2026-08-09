"""Tests for shared.review_progress.call_code_review_agent (progress → phase detail bridge)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock

from software_engineering_team.shared.review_progress import call_code_review_agent


class _ReportingAgent:
    """Fake review agent that supports progress reporting."""

    def run(self, inp: Any, progress_callback: Any = None) -> Any:
        if progress_callback is not None:
            progress_callback("reviewing", "chunk 2/5: src/app.py", 0.4)
            progress_callback("done", "", 1.0)
        return MagicMock(issues=[])


class _LegacyAgent:
    """Fake review agent WITHOUT the progress_callback parameter (external/injected)."""

    def run(self, inp: Any) -> Any:
        return MagicMock(issues=[])


def test_bridge_formats_progress_as_phase_detail_strings() -> None:
    details: List[str] = []
    result = call_code_review_agent(_ReportingAgent(), {"code": "x"}, details.append)
    assert result is not None
    assert details[0] == "Code review 40%: chunk 2/5: src/app.py"
    # Empty detail falls back to the step name.
    assert details[1] == "Code review 100%: done"


def test_bridge_skips_kwarg_for_legacy_agent_signature() -> None:
    """An agent whose run() lacks progress_callback must still work — no TypeError,
    which the call sites' except Exception would silently turn into an LLM fallback."""
    details: List[str] = []
    result = call_code_review_agent(_LegacyAgent(), {"code": "x"}, details.append)
    assert result is not None
    assert details == []


def test_bridge_no_callback_passes_nothing() -> None:
    captured: dict = {}

    class _Capturing:
        def run(self, inp: Any, progress_callback: Any = None) -> Any:
            captured["progress_callback"] = progress_callback
            return MagicMock(issues=[])

    call_code_review_agent(_Capturing(), {"code": "x"}, None)
    assert captured["progress_callback"] is None


def test_bridge_handles_unsignaturable_run() -> None:
    """A run not introspectable by inspect.signature (C builtins raise ValueError)
    must degrade to calling without the kwarg, not raise."""

    class _BuiltinRunAgent:
        # inspect.signature(max) raises ValueError ("no signature found"); the bridge
        # must fall back to calling run without the progress kwarg.
        run = max

    details: List[str] = []
    result = call_code_review_agent(_BuiltinRunAgent(), {"code": "x"}, details.append)
    assert result == "code"  # max() over the single-key dict
    assert details == []


def test_run_code_review_phase_threads_detail_callback(monkeypatch, tmp_path: Path) -> None:
    """End-to-end through run_code_review_phase: the agent's progress reports reach
    detail_callback as formatted strings."""
    from shared.dev_models.models import Task, TaskType
    from software_engineering_team.backend_code_v2_team.phases.review import run_code_review_phase

    task = Task(
        id="t1",
        type=TaskType.BACKEND,
        title="T",
        description="desc",
        requirements="reqs",
        assignee="backend",
        acceptance_criteria=["AC"],
    )
    from software_engineering_team.backend_code_v2_team.models import Microtask

    microtask = Microtask(id="m1", title="M", description="md")

    details: List[str] = []
    result = run_code_review_phase(
        llm=MagicMock(),
        task=task,
        microtask=microtask,
        repo_path=tmp_path,
        files={"x.py": "code"},
        code_review_agent=_ReportingAgent(),
        detail_callback=details.append,
    )
    assert result is not None
    assert any(d.startswith("Code review 40%:") for d in details), details


def test_run_code_review_phase_forwards_architecture_and_spec_content(tmp_path: Path) -> None:
    """``run_code_review_phase``'s ``architecture``/``spec_content`` reach the
    ``CodeReviewInput`` built for the external code-review agent."""
    from shared.dev_models.models import (
        ReviewContext,
        SystemArchitecture,
        Task,
        TaskType,
    )
    from software_engineering_team.backend_code_v2_team.models import Microtask
    from software_engineering_team.backend_code_v2_team.phases.review import run_code_review_phase

    task = Task(
        id="t1",
        type=TaskType.BACKEND,
        title="T",
        description="desc",
        requirements="reqs",
        assignee="backend",
        acceptance_criteria=["AC"],
    )
    microtask = Microtask(id="m1", title="M", description="md")
    architecture = SystemArchitecture(overview="layered architecture")

    captured: dict = {}

    class _CapturingAgent:
        def run(self, inp: Any) -> Any:
            captured["architecture"] = inp.architecture
            captured["spec_content"] = inp.spec_content
            return MagicMock(issues=[])

    run_code_review_phase(
        llm=MagicMock(),
        task=task,
        microtask=microtask,
        repo_path=tmp_path,
        files={"x.py": "code"},
        code_review_agent=_CapturingAgent(),
        review_context=ReviewContext(
            architecture=architecture, spec_content="the full project spec"
        ),
    )
    assert captured["architecture"] is architecture
    assert captured["spec_content"] == "the full project spec"


def test_bridge_passes_kwarg_to_var_keyword_run() -> None:
    """A forward-compatible wrapper (`def run(self, inp, **kwargs)`) forwards the
    kwarg, so the bridge must pass it — silently dropping progress for such an
    agent was a false negative of the old name-only signature check."""
    received: dict = {}

    class KwargsAgent:
        def run(self, inp, **kwargs):
            received.update(kwargs)
            if "progress_callback" in kwargs and kwargs["progress_callback"]:
                kwargs["progress_callback"]("reviewing", "chunk 1/2", 0.5)
            return "result"

    details: list = []
    out = call_code_review_agent(KwargsAgent(), object(), details.append)

    assert out == "result"
    assert "progress_callback" in received
    assert details == ["Code review 50%: chunk 1/2"]


def test_bridge_percent_uses_rounding() -> None:
    """The bridge's percent matches the UI's toFixed(0) rounding (0.366 → 37%), not
    int() truncation (36%) — the two rendered numbers must never disagree."""
    received: dict = {}

    class Agent:
        def run(self, inp, progress_callback=None):
            received["cb"] = progress_callback
            progress_callback("reviewing", "chunk 2/5", 0.366)
            return "r"

    details: list = []
    call_code_review_agent(Agent(), object(), details.append)
    assert details == ["Code review 37%: chunk 2/5"]
