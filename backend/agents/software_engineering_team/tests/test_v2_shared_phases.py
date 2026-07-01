"""
Unit tests for the shared, profile-parameterized code-v2 phase implementations
(``software_engineering_team.shared.phases.*``), the prompt builders
(``shared.prompts``), and :class:`StackProfile`.

These exercise the parameterization branches directly — the stack-profile
conventions lookup, the ``{language_conventions}`` slot gating, the planning
context/parse helpers, the non-gated execution loop, and the setup scaffolding
helpers — independently of the two team wrappers that also drive them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from software_engineering_team.backend_code_v2_team import models as be_models
from software_engineering_team.shared.models import SystemArchitecture, Task, TaskStatus, TaskType
from software_engineering_team.shared.phases import execution as sh_exec
from software_engineering_team.shared.phases import planning as sh_plan
from software_engineering_team.shared.phases import setup as sh_setup
from software_engineering_team.shared.prompts import (
    build_execution_prompt,
    build_planning_prompt,
    build_problem_solving_single_issue_prompt,
)
from software_engineering_team.shared.stack_profile import StackProfile

# --- helpers ---------------------------------------------------------------


class _StubAgent:
    def __init__(self, resp: str) -> None:
        self._resp = resp

    def __call__(self, _prompt: str) -> str:
        return self._resp


def _agent_factory(resp: str):
    """Return an ``agent_factory(model=...) -> callable(prompt) -> str`` stub."""

    def factory(*, model=None):  # noqa: ARG001 - model is resolved but unused by the stub
        return _StubAgent(resp)

    return factory


def _task() -> Task:
    return Task(
        id="t1",
        type=TaskType.BACKEND,
        assignee="backend-code-v2",
        status=TaskStatus.PENDING,
        description="build a thing",
        requirements="req",
    )


_BACKEND_PROFILE = StackProfile(
    name="backend",
    default_language="python",
    planning_language_label="Language",
    planning_progress_label="language",
    conventions_by_language={"java": "JAVA", "_default": "PY"},
    execution_has_language_conventions=True,
    problem_solving_has_language_conventions=True,
    detect_language=lambda _p, _t: "python",
)

_FRONTEND_PROFILE = StackProfile(
    name="frontend",
    default_language="typescript",
    planning_language_label="Language/stack",
    planning_progress_label="stack",
    conventions_by_language={"_default": "TS"},
    execution_has_language_conventions=False,
    problem_solving_has_language_conventions=False,
    detect_language=lambda _p, _t: "typescript",
)


# --- StackProfile ----------------------------------------------------------


def test_conventions_for_specific_and_default():
    assert _BACKEND_PROFILE.conventions_for("java") == "JAVA"
    assert _BACKEND_PROFILE.conventions_for("python") == "PY"
    assert _BACKEND_PROFILE.conventions_for("cobol") == "PY"  # falls back to _default
    assert _FRONTEND_PROFILE.conventions_for("angular") == "TS"


# --- prompt builders -------------------------------------------------------


def test_build_planning_prompt_substitutes_and_preserves_brace_value():
    out = build_planning_prompt(
        team_kind="frontend",
        tool_agent_domains="- general — x",
        language_input_line="Target stack (angular)",
        language_output="{detected_language}",
        planning_rules="Rules:\n- do X",
    )
    assert "frontend development team" in out
    assert "- general — x" in out
    assert "Target stack (angular)" in out
    # The literal brace-bearing value survives str.replace substitution.
    assert "{detected_language}" in out


def test_build_execution_prompt_slot_gating():
    be = build_execution_prompt(
        engineer_intro="You are BE.",
        coding_standards="\nCS\n",
        has_language_conventions=True,
        file_noun="code files",
        path_rules="- rule",
    )
    fe = build_execution_prompt(
        engineer_intro="You are FE.",
        coding_standards="\nCS\n",
        has_language_conventions=False,
        file_noun="component/service files",
        path_rules="- rule",
    )
    assert "{language_conventions}" in be
    assert "{language_conventions}" not in fe
    # Downstream .format() slots preserved in both.
    for p in (be, fe):
        assert "{microtask_description}" in p
        assert "{architecture_context}" in p


def test_build_problem_solving_single_issue_slot_gating():
    be = build_problem_solving_single_issue_prompt(
        coding_standards="\nCS\n",
        has_language_conventions=True,
        file_output_block="## FILE x ##\n",
    )
    fe = build_problem_solving_single_issue_prompt(
        coding_standards="\nCS\n",
        has_language_conventions=False,
        file_output_block="## FILE x ##\n",
    )
    assert "{language_conventions}" in be
    assert "{language_conventions}" not in fe
    for p in (be, fe):
        assert "{source}" in p and "{current_code}" in p


# --- planning helpers ------------------------------------------------------


def test_build_context_includes_architecture_and_code():
    arch = SystemArchitecture(overview="ARCH-OVERVIEW")
    out = sh_plan.build_context(
        _task(),
        arch,
        "print('code')",
        "python",
        planning_prompt="PLAN",
        language_label="Language",
    )
    assert out.startswith("PLAN")
    assert "**Language:** python" in out
    assert "ARCH-OVERVIEW" in out
    assert "print('code')" in out


def test_build_context_skips_placeholder_code():
    out = sh_plan.build_context(
        _task(),
        None,
        "# No code files found",
        "python",
        planning_prompt="PLAN",
        language_label="Language/stack",
    )
    assert "**Language/stack:** python" in out
    assert "Existing codebase" not in out


def test_parse_planning_output_fallback_and_skip():
    raw = {
        "microtasks": [
            {"id": "m1", "tool_agent": "not-a-real-agent"},  # ValueError -> GENERAL
            {"title": "no id"},  # skipped
            {"id": "m2", "tool_agent": "general"},
        ],
        "language": "python",
        "summary": "s",
    }
    result = sh_plan.parse_planning_output(raw, "python", models=be_models)
    assert [m.id for m in result.microtasks] == ["m1", "m2"]
    assert result.microtasks[0].tool_agent == be_models.ToolAgentKind.GENERAL


# --- execution -------------------------------------------------------------


def test_run_general_microtask_impl_gates_conventions():
    seen_prompt = {}

    def capturing_factory(*, model=None):  # noqa: ARG001
        def agent(prompt: str) -> str:
            seen_prompt["prompt"] = prompt
            return "resp"

        return agent

    files = sh_exec._run_general_microtask_impl(
        llm=object(),
        microtask=SimpleNamespace(description="do it", title="t"),
        task=_task(),
        language="python",
        existing_code="",
        architecture=None,
        execution_prompt="conv={language_conventions} desc={microtask_description}",
        parse_files_and_summary=lambda _r: {"files": {"a.py": "x"}},
        profile=_BACKEND_PROFILE,
        agent_factory=capturing_factory,
        resolve_model=lambda _llm: None,
    )
    assert files == {"a.py": "x"}
    # backend profile injected the conventions into the prompt.
    assert "conv=PY" in seen_prompt["prompt"]
    assert "desc=do it" in seen_prompt["prompt"]


def test_run_general_microtask_impl_omits_conventions_for_frontend():
    files = sh_exec._run_general_microtask_impl(
        llm=object(),
        microtask=SimpleNamespace(description="do it", title="t"),
        task=_task(),
        language="typescript",
        existing_code="x" * 10,
        architecture=SystemArchitecture(overview="A"),
        # No {language_conventions} slot — frontend profile must not pass it.
        execution_prompt="desc={microtask_description} arch={architecture_context}",
        parse_files_and_summary=lambda _r: {"files": {}},
        profile=_FRONTEND_PROFILE,
        agent_factory=_agent_factory("resp"),
        resolve_model=lambda _llm: None,
    )
    assert files == {}


def test_dedup_issues_removes_repeats():
    seen: set = set()
    a = SimpleNamespace(file_path="f.py", description="bug")
    b = SimpleNamespace(file_path="f.py", description="bug")
    c = SimpleNamespace(file_path="g.py", description="bug")
    first = sh_exec._dedup_issues([a, b, c], seen)
    assert len(first) == 2  # b is a dup of a
    # seen persists across calls
    assert sh_exec._dedup_issues([c], seen) == []


def test_run_execution_impl_filters_and_handles_failure():
    def raising_runner(_inp):
        raise RuntimeError("boom")

    planning = be_models.PlanningResult(
        microtasks=[
            be_models.Microtask(
                id="mt-1", tool_agent=be_models.ToolAgentKind.GENERAL, description="one"
            ),
            be_models.Microtask(
                id="mt-2", tool_agent=be_models.ToolAgentKind.DATA_ENGINEERING, description="two"
            ),
        ],
        language="python",
    )
    result = sh_exec.run_execution_impl(
        llm=object(),
        task=_task(),
        planning_result=planning,
        repo_path=Path("/tmp"),
        architecture=None,
        existing_code="",
        tool_runners={be_models.ToolAgentKind.DATA_ENGINEERING: raising_runner},
        progress_callback=None,
        only_microtask_ids=["mt-2"],  # only the raising one runs
        models=be_models,
        run_general_microtask=lambda **_kw: {},
    )
    # Only mt-2 ran and it failed; mt-1 was filtered out.
    ran = [m for m in result.microtasks]
    assert len(ran) == 1 and ran[0].id == "mt-2"
    assert ran[0].status == be_models.MicrotaskStatus.FAILED


# --- setup helpers ---------------------------------------------------------


def test_ensure_readme_prepends_title(tmp_path: Path):
    readme = tmp_path / "README.md"
    readme.write_text("existing body without heading\n", encoding="utf-8")
    sh_setup._ensure_readme_with_title(tmp_path, "My Project")
    content = readme.read_text(encoding="utf-8")
    assert content.startswith("# My Project\n\n")
    assert "existing body without heading" in content


def test_commit_scaffolding_empty_is_noop():
    calls = []
    sh_setup._commit_scaffolding(
        Path("/tmp"), set(), commit_paths=lambda *a, **k: calls.append(a) or (True, "")
    )
    assert calls == []  # nothing committed for an empty scaffolding set


def test_commit_scaffolding_logs_when_not_committed(tmp_path: Path, caplog):
    with caplog.at_level("WARNING"):
        sh_setup._commit_scaffolding(
            tmp_path, {"a.txt"}, commit_paths=lambda *a, **k: (False, "rejected by hook")
        )
    assert "not committed" in caplog.text.lower()


def test_commit_scaffolding_swallows_exception(tmp_path: Path, caplog):
    def _raise(*_a, **_k):
        raise RuntimeError("git down")

    with caplog.at_level("WARNING"):
        sh_setup._commit_scaffolding(tmp_path, {"a.txt"}, commit_paths=_raise)
    assert "could not commit setup scaffolding" in caplog.text.lower()


def test_configure_quality_tooling_impl_commits_scaffolding(tmp_path: Path):
    committed = {}

    def ensure_lint(path: Path, written: set) -> bool:
        (path / "cfg").write_text("x", encoding="utf-8")
        written.add("cfg")
        return True

    def ensure_test(_path: Path, _written: set) -> bool:
        return True

    def commit_paths(_path, paths, _msg):
        committed["paths"] = list(paths)
        return True, "ok"

    lint_ok, test_ok = sh_setup.configure_quality_tooling_impl(
        tmp_path,
        ensure_linting=ensure_lint,
        ensure_testing=ensure_test,
        commit_paths=commit_paths,
    )
    assert lint_ok and test_ok
    assert committed["paths"] == ["cfg"]


def test_run_setup_impl_initializes_new_repo(tmp_path: Path):
    repo = tmp_path / "proj"

    def _cqt(_path: Path):
        return True, True

    result = sh_setup.run_setup_impl(
        repo_path=repo, task_title="Demo", configure_quality_tooling=_cqt
    )
    assert result.repo_initialized is True
    assert result.linting_configured and result.testing_configured
    # A real git repo was created on the development branch.
    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True
    )
    assert branch.stdout.strip() == "development"


def test_run_setup_impl_on_existing_repo(tmp_path: Path):
    from software_engineering_team.tests.test_helpers import init_repo_with_existing_development

    init_repo_with_existing_development(tmp_path)
    calls = {"n": 0}

    def _cqt(_path: Path):
        calls["n"] += 1
        return True, True

    result = sh_setup.run_setup_impl(
        repo_path=tmp_path, task_title="Demo", configure_quality_tooling=_cqt
    )
    # Existing-repo branch: no fresh init, quality tooling still configured.
    assert result.repo_initialized is False
    assert calls["n"] == 1
    assert result.linting_configured and result.testing_configured


def test_ensure_readme_logs_when_commit_raises(tmp_path: Path, monkeypatch, caplog):
    from software_engineering_team.shared import git_utils

    def _raise(*_a, **_k):
        raise RuntimeError("no git")

    monkeypatch.setattr(git_utils, "write_files_and_commit", _raise)
    with caplog.at_level("WARNING"):
        sh_setup._ensure_readme_with_title(tmp_path, "Title")
    assert "could not commit readme" in caplog.text.lower()
    assert (tmp_path / "README.md").read_text(encoding="utf-8").startswith("# Title")


def test_write_microtask_files_strips_leading_slash(tmp_path: Path):
    sh_exec._write_microtask_files(tmp_path, {"/pkg/mod.py": "content", "top.txt": "t"})
    assert (tmp_path / "pkg" / "mod.py").read_text(encoding="utf-8") == "content"
    assert (tmp_path / "top.txt").read_text(encoding="utf-8") == "t"


def test_run_execution_impl_runner_success_and_general_fallback(tmp_path: Path):
    def good_runner(_inp):
        return SimpleNamespace(files={"gen.py": "x"}, summary="done")

    planning = be_models.PlanningResult(
        microtasks=[
            be_models.Microtask(
                id="mt-runner",
                tool_agent=be_models.ToolAgentKind.DATA_ENGINEERING,
                description="via runner",
            ),
            be_models.Microtask(
                id="mt-fallback",
                tool_agent=be_models.ToolAgentKind.GENERAL,
                description="via llm",
            ),
        ],
        language="python",
    )
    result = sh_exec.run_execution_impl(
        llm=object(),
        task=_task(),
        planning_result=planning,
        repo_path=tmp_path,
        architecture=None,
        existing_code="ctx",
        tool_runners={be_models.ToolAgentKind.DATA_ENGINEERING: good_runner},
        progress_callback=None,
        only_microtask_ids=None,
        models=be_models,
        run_general_microtask=lambda **_kw: {"fb.py": "y"},
    )
    assert result.files == {"gen.py": "x", "fb.py": "y"}
    assert all(m.status == be_models.MicrotaskStatus.COMPLETED for m in result.microtasks)


def test_run_planning_impl_appends_tool_agent_recommendations(monkeypatch):
    template = (
        "## MICROTASKS ##\n---\nid: mt-1\ntitle: t\ndescription: d\ntool_agent: general\n"
        "depends_on:\n---\n## END MICROTASKS ##\n## LANGUAGE ##\npython\n## END LANGUAGE ##\n"
        "## SUMMARY ##\nbase\n## END SUMMARY ##\n"
    )
    monkeypatch.setattr(sh_plan, "Agent", lambda *a, **kw: _StubAgent(template))
    monkeypatch.setattr(sh_plan, "resolve_text_mode_strands_model", lambda _llm: object())

    class _PlanAgent:
        def plan(self, _inp):
            return SimpleNamespace(recommendations=["use an index"])

    class _RaisingAgent:
        def plan(self, _inp):
            raise RuntimeError("plan failed")

    result = sh_plan.run_planning_impl(
        llm=object(),
        task=_task(),
        repo_path=Path("/tmp"),
        architecture=None,
        existing_code="",
        tool_agents={
            be_models.ToolAgentKind.DATA_ENGINEERING: _PlanAgent(),
            be_models.ToolAgentKind.AUTH: _RaisingAgent(),
        },
        profile=_BACKEND_PROFILE,
        planning_prompt="PLAN",
        parse_planning_template=lambda _raw: {
            "microtasks": [{"id": "mt-1", "tool_agent": "general"}],
            "language": "python",
            "summary": "base",
        },
        models=be_models,
    )
    # The planning tool agent's recommendation was appended to the summary; the
    # raising agent was caught and skipped.
    assert "use an index" in result.summary
    assert result.microtasks[0].id == "mt-1"
