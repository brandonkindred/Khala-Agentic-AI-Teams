"""
Unit tests for the frontend-code-v2 team: models, phases, tool agents, orchestrator.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))

from llm_service.clients.dummy import DummyLLMClient  # noqa: E402


class _TextStubClient(DummyLLMClient):
    """Returns a canned text response through the Strands ``stream()`` path."""

    def __init__(self, text: str = "") -> None:
        super().__init__()
        self._text = text

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: bool = False,
        **kwargs: Any,
    ) -> Any:
        return self._text


from frontend_code_v2_team.models import (  # noqa: E402
    FrontendCodeV2WorkflowResult,
    Microtask,
    MicrotaskStatus,
    Phase,
    PlanningResult,
    SetupResult,
    ToolAgentInput,
    ToolAgentKind,
    ToolAgentOutput,
)


class TestModels:
    def test_microtask_defaults(self):
        mt = Microtask(id="mt-1")
        assert mt.status == MicrotaskStatus.PENDING
        assert mt.tool_agent == ToolAgentKind.GENERAL
        assert mt.depends_on == []
        assert mt.output_files == {}

    def test_planning_result_defaults(self):
        pr = PlanningResult()
        assert pr.language == "typescript"
        assert pr.microtasks == []

    def test_workflow_result_defaults(self):
        wr = FrontendCodeV2WorkflowResult()
        assert not wr.success
        assert wr.current_phase == Phase.SETUP
        assert wr.iterations_used == 0
        assert wr.setup_result is None

    def test_phase_enum_includes_setup(self):
        assert Phase.SETUP.value == "setup"
        assert Phase.SETUP in Phase

    def test_tool_agent_kind_frontend_specific(self):
        assert ToolAgentKind.STATE_MANAGEMENT.value == "state_management"
        assert ToolAgentKind.UI_DESIGN.value == "ui_design"
        assert ToolAgentKind.GIT_BRANCH_MANAGEMENT in ToolAgentKind
        assert ToolAgentKind.BUILD_SPECIALIST in ToolAgentKind

    def test_setup_result_model(self):
        sr = SetupResult(repo_initialized=True, readme_created=True, branch_created=True)
        assert sr.repo_initialized

    def test_tool_agent_io(self):
        mt = Microtask(id="mt-test", description="test")
        inp = ToolAgentInput(microtask=mt, repo_path="/tmp/repo", language="angular")
        assert inp.language == "angular"
        out = ToolAgentOutput(files={"app.component.ts": "content"}, summary="done")
        assert out.success


class TestSetupPhase:
    def test_run_setup_on_existing_repo(self, tmp_path):
        from frontend_code_v2_team.phases.setup import run_setup

        (tmp_path / ".git").mkdir()
        result = run_setup(repo_path=tmp_path, task_title="My App")
        assert isinstance(result, SetupResult)
        assert result.summary is not None

    def test_run_setup_creates_repo_when_missing(self, tmp_path):
        from frontend_code_v2_team.phases.setup import run_setup

        assert not (tmp_path / ".git").exists()
        result = run_setup(repo_path=tmp_path, task_title="New App")
        assert result.repo_initialized or (tmp_path / ".git").exists()
        assert result.summary


class TestPlanningPhase:
    def test_language_detection_angular(self, tmp_path):
        from frontend_code_v2_team.phases.planning import _detect_language

        from software_engineering_team.shared.models import Task, TaskStatus, TaskType

        (tmp_path / "angular.json").write_text("{}")
        task = Task(
            id="t1",
            type=TaskType.FRONTEND,
            assignee="frontend-code-v2",
            status=TaskStatus.PENDING,
            description="build ui",
        )
        assert _detect_language(tmp_path, task) == "angular"

    def test_language_detection_from_description(self, tmp_path):
        from frontend_code_v2_team.phases.planning import _detect_language

        from software_engineering_team.shared.models import Task, TaskStatus, TaskType

        task = Task(
            id="t1",
            type=TaskType.FRONTEND,
            assignee="frontend-code-v2",
            status=TaskStatus.PENDING,
            description="Use React and TypeScript",
        )
        assert _detect_language(tmp_path, task) == "react"

    def test_parse_planning_output(self):
        from frontend_code_v2_team.phases.planning import _parse_planning_output

        raw = {
            "microtasks": [
                {
                    "id": "mt-1",
                    "title": "Add component",
                    "tool_agent": "ui_design",
                    "description": "create component",
                },
                {
                    "id": "mt-2",
                    "title": "Add tests",
                    "tool_agent": "testing_qa",
                    "description": "unit tests",
                    "depends_on": ["mt-1"],
                },
            ],
            "language": "angular",
            "summary": "Plan created",
        }
        result = _parse_planning_output(raw, "typescript")
        assert len(result.microtasks) == 2
        assert result.microtasks[0].tool_agent == ToolAgentKind.UI_DESIGN
        assert result.microtasks[1].depends_on == ["mt-1"]
        assert result.language == "angular"

    def test_run_planning_fallback(self, tmp_path):
        from frontend_code_v2_team.phases.planning import run_planning

        from software_engineering_team.shared.models import Task, TaskStatus, TaskType

        mock_llm = _TextStubClient(
            "## MICROTASKS ##\n## END MICROTASKS ##\n"
            "## LANGUAGE ##\ntypescript\n## END LANGUAGE ##\n"
            "## SUMMARY ##\nempty\n## END SUMMARY ##"
        )
        task = Task(
            id="t1",
            type=TaskType.FRONTEND,
            assignee="frontend-code-v2",
            status=TaskStatus.PENDING,
            description="build something",
        )
        result = run_planning(llm=mock_llm, task=task, repo_path=tmp_path)
        assert len(result.microtasks) == 1
        assert result.microtasks[0].id == "mt-implement-task"


class TestToolAgents:
    def test_build_tool_agents_includes_all_kinds(self):
        from frontend_code_v2_team.orchestrator import _build_tool_agents

        agents = _build_tool_agents(MagicMock())
        assert ToolAgentKind.GIT_BRANCH_MANAGEMENT in agents
        assert ToolAgentKind.BUILD_SPECIALIST in agents
        assert ToolAgentKind.UI_DESIGN in agents
        assert hasattr(agents[ToolAgentKind.GIT_BRANCH_MANAGEMENT], "create_feature_branch")
        assert hasattr(agents[ToolAgentKind.GIT_BRANCH_MANAGEMENT], "commit_current_changes")
        assert hasattr(agents[ToolAgentKind.GIT_BRANCH_MANAGEMENT], "deliver")

    def test_git_agent_create_feature_branch(self, tmp_path):
        import subprocess

        from frontend_code_v2_team.tool_agents.git_branch_management import (
            GitBranchManagementToolAgent,
        )

        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "t@t.com"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "config", "commit.gpgsign", "false"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )
        (tmp_path / "f").write_text("x")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "branch", "-m", "development"], cwd=tmp_path, capture_output=True, check=True
        )
        agent = GitBranchManagementToolAgent()
        ok, name = agent.create_feature_branch(tmp_path, "task-1", "Login page")
        assert ok is True
        assert name

    def test_git_agent_commit_current_changes(self, tmp_path):
        from frontend_code_v2_team.tool_agents.git_branch_management import (
            GitBranchManagementToolAgent,
        )

        (tmp_path / ".git").mkdir()
        agent = GitBranchManagementToolAgent()
        ok, msg = agent.commit_current_changes(tmp_path, "wip: test")
        assert isinstance(ok, bool)
        assert isinstance(msg, str)

    def test_build_specialist_stub(self):
        from frontend_code_v2_team.tool_agents.build_specialist import BuildSpecialistAdapterAgent

        agent = BuildSpecialistAdapterAgent()
        out = agent.execute(ToolAgentInput(microtask=Microtask(id="mt-1"), repo_path="/tmp"))
        assert out.summary
        assert hasattr(agent, "plan")
        assert hasattr(agent, "review")
        assert hasattr(agent, "problem_solve")
        assert hasattr(agent, "deliver")


class TestFrontendDevelopmentAgent:
    def test_build_tool_runners(self):
        from frontend_code_v2_team.models import ToolAgentKind
        from frontend_code_v2_team.orchestrator import FrontendDevelopmentAgent
        from frontend_code_v2_team.tool_agents.git_branch_management import (
            GitBranchManagementToolAgent,
        )
        from frontend_code_v2_team.tool_agents.state_management import StateManagementToolAgent

        agent = FrontendDevelopmentAgent(MagicMock())
        tool_agents = {
            ToolAgentKind.STATE_MANAGEMENT: StateManagementToolAgent(),
            ToolAgentKind.GIT_BRANCH_MANAGEMENT: GitBranchManagementToolAgent(),
        }
        runners = agent._build_tool_runners(tool_agents)
        assert ToolAgentKind.STATE_MANAGEMENT in runners
        assert ToolAgentKind.GIT_BRANCH_MANAGEMENT in runners


# ---------------------------------------------------------------------------
# Documentation self-review: function-aware code chunking (large-input path)
# ---------------------------------------------------------------------------

_DOC_REVIEW_RESPONSE = (
    "## QUALITY_SCORE ##\n0.95\n## END QUALITY_SCORE ##\n"
    "## IMPROVEMENTS ##\n- Clarified usage\n## END IMPROVEMENTS ##\n"
    "## FILE docs/readme.md ##\nRefined content\n"
    "## SUMMARY ##\nRefinements made\n## END SUMMARY ##"
)


class _RecordingDocClient(DummyLLMClient):
    """Records every user prompt and returns a canned doc-review response.

    ``DummyLLMClient.stream`` forwards the rendered user prompt to
    ``complete_json`` (one call per Agent invocation, no tools), so the recorded
    prompts are exactly what each documentation self-review pass showed the LLM.
    """

    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text
        self.prompts: list[str] = []

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: bool = False,
        **kwargs: Any,
    ) -> Any:
        self.prompts.append(prompt)
        return self._text


def _make_big_code_file(idx: int, approx_chars: int = 30_000) -> str:
    """Build a ~approx_chars file of many small functions, with a tail sentinel."""
    lines: list[str] = []
    size = 0
    n = 0
    while size < approx_chars:
        line = f"function f{idx}_{n}(a, b) {{ return a + b + {n}; }}"
        lines.append(line)
        size += len(line) + 1
        n += 1
    lines.append(f"// SENTINEL_END_{idx}")
    return "\n".join(lines)


class TestDocumentationSelfReviewChunking:
    """Issue: the doc self-review used to triple-truncate its code context."""

    _NUM_FILES = 6

    def _big_code_files(self) -> dict:
        return {f"src/mod_{i}.ts": _make_big_code_file(i) for i in range(self._NUM_FILES)}

    def test_chunks_cover_all_files_without_clipping(self):
        from frontend_code_v2_team.phases.review import (
            MAX_DOC_REVIEW_CHUNK_CHARS,
            _doc_review_code_chunks,
        )

        code_files = self._big_code_files()
        chunks = _doc_review_code_chunks(code_files)
        # Large input is genuinely split into multiple bounded chunks.
        assert len(chunks) > 1
        joined = "\n".join(chunks)
        # No file silently dropped: every path appears across the chunks.
        for path in code_files:
            assert path in joined
        # No file clipped mid-content: every file's tail sentinel survives.
        for i in range(self._NUM_FILES):
            assert f"SENTINEL_END_{i}" in joined
        # Every chunk stays within the per-call budget (short lines never make a
        # single segment exceed it).
        for chunk in chunks:
            assert len(chunk) <= MAX_DOC_REVIEW_CHUNK_CHARS

    def test_empty_code_yields_single_placeholder_pass(self):
        from frontend_code_v2_team.phases.review import _doc_review_code_chunks

        assert _doc_review_code_chunks({}) == ["(No code context)"]
        # Blank-only content is treated as no code context.
        assert _doc_review_code_chunks({"a.ts": "   \n"}) == ["(No code context)"]

    def test_large_input_shows_every_chunk_to_llm(self):
        from frontend_code_v2_team.phases.review import (
            _doc_review_code_chunks,
            run_documentation_self_review,
        )

        code_files = self._big_code_files()
        n_chunks = len(_doc_review_code_chunks(code_files))
        client = _RecordingDocClient(_DOC_REVIEW_RESPONSE)
        result = run_documentation_self_review(
            llm=client,
            documentation={"docs/readme.md": "old docs"},
            code_files=code_files,
            task_description="task",
            min_iterations=1,
            max_iterations=1,
        )
        # One LLM call per code chunk; all code shown across the single pass.
        assert len(client.prompts) == n_chunks
        all_prompts = "\n".join(client.prompts)
        for i in range(self._NUM_FILES):
            assert f"SENTINEL_END_{i}" in all_prompts
        assert "docs/readme.md" in result.documentation
        assert result.iterations == 1

    def test_small_input_single_call_per_iteration(self):
        from frontend_code_v2_team.phases.review import run_documentation_self_review

        client = _RecordingDocClient(_DOC_REVIEW_RESPONSE)
        result = run_documentation_self_review(
            llm=client,
            documentation={"docs/readme.md": "old"},
            code_files={"src/a.ts": "export const a = 1;"},
            task_description="task",
            min_iterations=1,
            max_iterations=2,
            quality_threshold=0.9,
        )
        # Small code = one chunk = one call; score 0.95 >= 0.9 stops at min_iterations.
        assert len(client.prompts) == 1
        assert result.iterations == 1
        assert result.final_quality_score == 0.95


class _RaisingDocClient(DummyLLMClient):
    """Raises on every LLM call to exercise the per-chunk failure path."""

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: bool = False,
        **kwargs: Any,
    ) -> Any:
        raise RuntimeError("boom")


class TestDocumentationSelfReviewResilience:
    def test_llm_failure_is_resilient_and_reports_progress(self):
        from frontend_code_v2_team.phases.review import run_documentation_self_review

        seen: list[str] = []
        result = run_documentation_self_review(
            llm=_RaisingDocClient(),
            documentation={"docs/readme.md": "old"},
            code_files={"src/a.ts": "export const a = 1;"},
            task_description="task",
            min_iterations=1,
            max_iterations=1,
            detail_callback=seen.append,
        )
        # Every chunk's call fails, but the pass never raises and returns the
        # docs unchanged with the default score.
        assert result.documentation == {"docs/readme.md": "old"}
        assert result.iterations == 1
        assert result.final_quality_score == 0.5
        # Per-iteration and final progress callbacks both fired.
        assert any("iteration 1/1" in m for m in seen)
        assert any("complete" in m for m in seen)


class TestDocReviewManyChunksWarning:
    def test_warns_when_chunk_count_exceeds_threshold(self, monkeypatch, caplog):
        import frontend_code_v2_team.phases.review as review_mod

        monkeypatch.setattr(review_mod, "MANY_CHUNKS_WARN_THRESHOLD", 0)
        code_files = {f"src/mod_{i}.ts": _make_big_code_file(i) for i in range(2)}
        with caplog.at_level("WARNING"):
            chunks = review_mod._doc_review_code_chunks(code_files)
        assert len(chunks) > 0
        assert any("code chunk(s)" in r.message for r in caplog.records)


def _doc_response(score: float) -> str:
    return (
        f"## QUALITY_SCORE ##\n{score}\n## END QUALITY_SCORE ##\n"
        "## IMPROVEMENTS ##\n- tweak\n## END IMPROVEMENTS ##\n"
        "## FILE docs/readme.md ##\nRefined\n"
        "## SUMMARY ##\ndone\n## END SUMMARY ##"
    )


class _ScriptedDocClient(DummyLLMClient):
    """Returns a different canned response on each ``complete_json`` call."""

    def __init__(self, responses: list) -> None:
        super().__init__()
        self._responses = list(responses)
        self._idx = 0

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: bool = False,
        **kwargs: Any,
    ) -> Any:
        resp = (
            self._responses[self._idx] if self._idx < len(self._responses) else self._responses[-1]
        )
        self._idx += 1
        return resp


class TestDocReviewMinScoreAcrossChunks:
    def test_iteration_score_is_min_across_chunks(self):
        from frontend_code_v2_team.phases.review import (
            _doc_review_code_chunks,
            run_documentation_self_review,
        )

        code_files = {f"src/mod_{i}.ts": _make_big_code_file(i) for i in range(6)}
        n_chunks = len(_doc_review_code_chunks(code_files))
        assert n_chunks >= 2
        # First chunk scores high, the rest low → iteration score is the minimum.
        responses = [_doc_response(0.95)] + [_doc_response(0.80)] * (n_chunks - 1)
        client = _ScriptedDocClient(responses)
        result = run_documentation_self_review(
            llm=client,
            documentation={"docs/readme.md": "old"},
            code_files=code_files,
            task_description="task",
            min_iterations=1,
            max_iterations=1,
        )
        assert result.final_quality_score == 0.80
