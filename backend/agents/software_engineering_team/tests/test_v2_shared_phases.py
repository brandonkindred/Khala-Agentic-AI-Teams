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
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_service.strands_model import LlmRunner
from shared.dev_models.models import (
    ArchitectureComponent,
    SystemArchitecture,
    Task,
    TaskStatus,
    TaskType,
)
from shared.git.git_utils import write_files_and_commit
from software_engineering_team.backend_code_v2_team import models as be_models
from software_engineering_team.shared.phases import execution as sh_exec
from software_engineering_team.shared.phases import planning as sh_plan
from software_engineering_team.shared.phases import problem_solving as sh_ps
from software_engineering_team.shared.phases import review_cycle as sh_review_cycle
from software_engineering_team.shared.phases import rollback as sh_rollback
from software_engineering_team.shared.phases import setup as sh_setup
from software_engineering_team.shared.prompts import (
    build_execution_prompt,
    build_planning_prompt,
    build_problem_solving_single_issue_prompt,
)
from software_engineering_team.shared.repo_writer import (
    UnsafeRepoPathError,
    write_repo_text_files,
)
from software_engineering_team.shared.stack_profile import StackProfile
from software_engineering_team.tests.test_helpers import init_repo_with_existing_development

# --- helpers ---------------------------------------------------------------


def _runner(resp: str, *, on_prompt=None) -> LlmRunner:
    """Build an ``LlmRunner`` whose agent returns ``resp`` (recording the prompt)."""

    def factory(*, model=None):  # noqa: ARG001 - model is resolved but unused by the stub
        def agent(prompt: str) -> str:
            if on_prompt is not None:
                on_prompt(prompt)
            return resp

        return agent

    return LlmRunner(agent_factory=factory, resolve_model=lambda _llm: None)


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
    has_language_conventions=True,
    build_verify_label="backend_code_v2",
    detect_language=lambda _p, _t: "python",
    repo_extensions=frozenset({".py"}),
    repo_exclude_dirs=frozenset({".git"}),
    repo_max_chars=1000,
    detect_tooling=lambda _p: (True, True),
)

_FRONTEND_PROFILE = StackProfile(
    name="frontend",
    default_language="typescript",
    planning_language_label="Language/stack",
    planning_progress_label="stack",
    conventions_by_language={"_default": "TS"},
    has_language_conventions=False,
    build_verify_label="frontend_code_v2",
    detect_language=lambda _p, _t: "typescript",
    repo_extensions=frozenset({".ts"}),
    repo_exclude_dirs=frozenset({".git"}),
    repo_max_chars=1000,
    detect_tooling=lambda _p: (True, True),
)


# --- StackProfile ----------------------------------------------------------


def test_conventions_for_specific_and_default():
    """Conventions for specific and default."""
    assert _BACKEND_PROFILE.conventions_for("java") == "JAVA"
    assert _BACKEND_PROFILE.conventions_for("python") == "PY"
    assert _BACKEND_PROFILE.conventions_for("cobol") == "PY"  # falls back to _default
    assert _FRONTEND_PROFILE.conventions_for("angular") == "TS"


# --- prompt builders -------------------------------------------------------


def test_build_planning_prompt_substitutes_and_preserves_brace_value():
    """Build planning prompt substitutes and preserves brace value."""
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
    """Build execution prompt slot gating."""
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
    """Build problem solving single issue slot gating."""
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
    """Build context includes architecture and code."""
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
    """Build context skips placeholder code."""
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
    """Parse planning output fallback and skip."""
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


def test_parse_planning_output_dedupes_by_id():
    """A microtask id repeated in planner output is skipped; the first occurrence wins."""
    raw = {
        "microtasks": [
            {"id": "m1", "title": "first", "tool_agent": "general"},
            {"id": "m1", "title": "second", "tool_agent": "general"},
            {"id": "m2", "title": "third", "tool_agent": "general"},
        ],
        "language": "python",
        "summary": "s",
    }
    result = sh_plan.parse_planning_output(raw, "python", models=be_models)
    assert [m.id for m in result.microtasks] == ["m1", "m2"]
    assert result.microtasks[0].title == "first"


# --- execution -------------------------------------------------------------


def test_run_general_microtask_impl_gates_conventions():
    """Run general microtask impl gates conventions."""
    seen_prompt = {}

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
        runner=_runner("resp", on_prompt=lambda p: seen_prompt.update(prompt=p)),
    )
    assert files == {"a.py": "x"}
    # backend profile injected the conventions into the prompt.
    assert "conv=PY" in seen_prompt["prompt"]
    assert "desc=do it" in seen_prompt["prompt"]


def test_run_general_microtask_impl_drops_unparsable_python():
    """Codegen returning an incomplete .py file must not be handed back as output."""
    files = sh_exec._run_general_microtask_impl(
        llm=object(),
        microtask=SimpleNamespace(id="mt-1", description="do it", title="t"),
        task=_task(),
        language="python",
        existing_code="",
        architecture=None,
        execution_prompt="desc={microtask_description}",
        parse_files_and_summary=lambda _r: {
            "files": {"a.py": "def foo(:\n    pass\n", "b.py": "def ok():\n    return 1\n"}
        },
        profile=_BACKEND_PROFILE,
        runner=_runner("resp"),
    )
    assert files == {"b.py": "def ok():\n    return 1\n"}


def test_run_general_microtask_impl_omits_conventions_for_frontend():
    """Run general microtask impl omits conventions for frontend."""
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
        runner=_runner("resp"),
    )
    assert files == {}


def test_run_general_microtask_impl_includes_components_and_decisions_in_arch_context():
    """Regression test: the general coder's architecture_context slot must fold
    in components/decisions, not just .overview -- previously it used
    architecture.overview directly, so an explicit component boundary or ADR
    was invisible to the LLM actually writing the code."""
    seen_prompt = {}
    architecture = SystemArchitecture(
        overview="Layered service architecture.",
        components=[
            ArchitectureComponent(
                name="billing-service", type="backend", description="Owns all billing writes."
            )
        ],
        decisions=[
            {"title": "ADR-003", "decision": "All billing writes go through billing-service."}
        ],
    )

    files = sh_exec._run_general_microtask_impl(
        llm=object(),
        microtask=SimpleNamespace(description="do it", title="t"),
        task=_task(),
        language="python",
        existing_code="",
        architecture=architecture,
        execution_prompt="arch={architecture_context}",
        parse_files_and_summary=lambda _r: {"files": {}},
        profile=_BACKEND_PROFILE,
        runner=_runner("resp", on_prompt=lambda p: seen_prompt.update(prompt=p)),
    )
    assert files == {}
    assert "Layered service architecture." in seen_prompt["prompt"]
    assert "billing-service" in seen_prompt["prompt"]
    assert "ADR-003" in seen_prompt["prompt"]


def test_dedup_issues_removes_repeats():
    """Dedup issues removes repeats."""
    seen: set = set()
    a = SimpleNamespace(file_path="f.py", description="bug")
    b = SimpleNamespace(file_path="f.py", description="bug")
    c = SimpleNamespace(file_path="g.py", description="bug")
    first = sh_review_cycle._dedup_issues([a, b, c], seen)
    assert len(first) == 2  # b is a dup of a
    # seen persists across calls
    assert sh_review_cycle._dedup_issues([c], seen) == []


def test_dedup_issues_tolerates_shapeless_elements():
    """Elements missing file_path/description dedup on an empty-string key instead of raising."""
    seen: set = set()
    shapeless_a = object()
    shapeless_b = object()
    result = sh_review_cycle._dedup_issues([shapeless_a, shapeless_b], seen)
    assert result == [shapeless_a]  # shapeless_b dedups against the ("", "") key
    assert seen == {("", "")}


def test_run_execution_impl_filters_and_handles_failure():
    """Run execution impl filters and handles failure."""

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


def test_run_execution_impl_independent_microtasks_all_complete():
    """Independent microtasks both complete and contribute disjoint files."""
    a_entered = threading.Event()
    b_entered = threading.Event()

    def overlapping_coder(**kwargs):
        mid = kwargs["microtask"].id
        if mid == "mt-1":
            a_entered.set()
            assert b_entered.wait(timeout=2), "mt-2 never overlapped with mt-1"
        else:
            b_entered.set()
            assert a_entered.wait(timeout=2), "mt-1 never overlapped with mt-2"
        time.sleep(0.02)
        return {f"src/{mid}.py": "print(1)\n"}

    planning = be_models.PlanningResult(
        microtasks=[
            be_models.Microtask(
                id="mt-1", tool_agent=be_models.ToolAgentKind.GENERAL, description="one"
            ),
            be_models.Microtask(
                id="mt-2", tool_agent=be_models.ToolAgentKind.GENERAL, description="two"
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
        tool_runners={},
        progress_callback=None,
        only_microtask_ids=None,
        models=be_models,
        run_general_microtask=overlapping_coder,
    )
    assert {m.status for m in result.microtasks} == {be_models.MicrotaskStatus.COMPLETED}
    assert result.files == {"src/mt-1.py": "print(1)\n", "src/mt-2.py": "print(1)\n"}


def test_run_execution_impl_caps_parallel_map_workers(monkeypatch):
    """Independent waves do not size the pool at wave length."""
    seen: list[int] = []
    real = sh_exec.parallel_map

    def _wrapped(*args, **kwargs):
        seen.append(kwargs["max_workers"])
        return real(*args, **kwargs)

    monkeypatch.setattr(sh_exec, "parallel_map", _wrapped)
    monkeypatch.setenv("SE_EXECUTION_WAVE_CONCURRENCY", "2")

    planning = be_models.PlanningResult(
        microtasks=[
            be_models.Microtask(
                id=f"mt-{i}", tool_agent=be_models.ToolAgentKind.GENERAL, description="x"
            )
            for i in range(5)
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
        tool_runners={},
        progress_callback=None,
        only_microtask_ids=None,
        models=be_models,
        run_general_microtask=lambda **kw: {f"src/{kw['microtask'].id}.py": "print(1)\n"},
    )
    assert seen == [2]
    assert {m.status for m in result.microtasks} == {be_models.MicrotaskStatus.COMPLETED}


def test_run_execution_impl_sequential_chain_runs_in_dependency_order():
    """A fully sequential chain is scheduled into one-microtask waves and runs A then B then C."""
    order: list[str] = []

    def recording_coder(**kwargs):
        order.append(kwargs["microtask"].id)
        return {f"src/{kwargs['microtask'].id}.py": "ok\n"}

    planning = be_models.PlanningResult(
        microtasks=[
            be_models.Microtask(
                id="mt-c",
                tool_agent=be_models.ToolAgentKind.GENERAL,
                description="c",
                depends_on=["mt-b"],
            ),
            be_models.Microtask(
                id="mt-b",
                tool_agent=be_models.ToolAgentKind.GENERAL,
                description="b",
                depends_on=["mt-a"],
            ),
            be_models.Microtask(
                id="mt-a",
                tool_agent=be_models.ToolAgentKind.GENERAL,
                description="a",
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
        tool_runners={},
        progress_callback=None,
        only_microtask_ids=None,
        models=be_models,
        run_general_microtask=recording_coder,
    )
    assert order == ["mt-a", "mt-b", "mt-c"]
    assert all(m.status == be_models.MicrotaskStatus.COMPLETED for m in result.microtasks)
    assert result.files == {
        "src/mt-a.py": "ok\n",
        "src/mt-b.py": "ok\n",
        "src/mt-c.py": "ok\n",
    }


def test_run_execution_impl_logs_cleanly_when_tool_agent_is_none():
    """A microtask with no tool_agent runs to completion without an AttributeError."""
    mt = be_models.Microtask(
        id="mt-1", tool_agent=be_models.ToolAgentKind.GENERAL, description="one"
    )
    mt.tool_agent = None  # simulate an unset tool_agent, as generate_microtask_files tolerates

    planning = be_models.PlanningResult(microtasks=[mt], language="python")
    result = sh_exec.run_execution_impl(
        llm=object(),
        task=_task(),
        planning_result=planning,
        repo_path=Path("/tmp"),
        architecture=None,
        existing_code="",
        tool_runners={},
        progress_callback=None,
        only_microtask_ids=None,
        models=be_models,
        run_general_microtask=lambda **_kw: {},
    )
    assert result.microtasks[0].status == be_models.MicrotaskStatus.COMPLETED


# --- setup helpers ---------------------------------------------------------


def test_ensure_readme_prepends_title(tmp_path: Path):
    """Ensure readme prepends title."""
    readme = tmp_path / "README.md"
    readme.write_text("existing body without heading\n", encoding="utf-8")
    sh_setup._ensure_readme_with_title(tmp_path, "My Project")
    content = readme.read_text(encoding="utf-8")
    assert content.startswith("# My Project\n\n")
    assert "existing body without heading" in content


def test_commit_scaffolding_empty_is_noop():
    """Commit scaffolding empty is noop."""
    calls = []
    sh_setup._commit_scaffolding(
        Path("/tmp"), set(), commit_paths=lambda *a, **k: calls.append(a) or (True, "")
    )
    assert calls == []  # nothing committed for an empty scaffolding set


def test_commit_scaffolding_logs_when_not_committed(tmp_path: Path, caplog):
    """Commit scaffolding logs when not committed."""
    with caplog.at_level("WARNING"):
        sh_setup._commit_scaffolding(
            tmp_path, {"a.txt"}, commit_paths=lambda *a, **k: (False, "rejected by hook")
        )
    assert "not committed" in caplog.text.lower()


def test_commit_scaffolding_swallows_exception(tmp_path: Path, caplog):
    """Commit scaffolding swallows exception."""

    def _raise(*_a, **_k):
        raise RuntimeError("git down")

    with caplog.at_level("WARNING"):
        sh_setup._commit_scaffolding(tmp_path, {"a.txt"}, commit_paths=_raise)
    assert "could not commit setup scaffolding" in caplog.text.lower()


def test_configure_quality_tooling_impl_commits_scaffolding(tmp_path: Path):
    """Configure quality tooling impl commits scaffolding."""
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
    """Run setup impl initializes new repo."""
    repo = tmp_path / "proj"

    def _cqt(_path: Path):
        return True, True

    result = sh_setup.run_setup_impl(
        repo_path=repo, task_title="Demo", configure_quality_tooling=_cqt
    )
    assert result.repo_initialized is True
    assert result.linting_configured and result.testing_configured
    # A real git repo was created on the development branch. Use rev-parse
    # (not `branch --show-current`) for portability with older Git.
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, capture_output=True, text=True
    )
    assert branch.stdout.strip() == "development"


def test_run_setup_impl_on_existing_repo(tmp_path: Path):
    """Run setup impl on existing repo."""
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


def test_run_setup_impl_succeeds_in_worktree_with_development_checked_out_elsewhere(
    tmp_path: Path,
):
    """Reproduces the coding-team worktree handoff: a worker's linked git
    worktree already has its feature branch checked out (development stays
    attached in the shared checkout) when the real v2 team lead's setup phase
    runs there. ``run_setup_impl``'s ``ensure_development_branch(path)`` call
    must not fail trying to attach a branch that's exclusively held elsewhere.
    """
    from shared.git.git_utils import add_worktree, create_feature_branch

    repo = tmp_path / "shared-checkout"
    repo.mkdir()
    init_repo_with_existing_development(repo)
    subprocess.run(["git", "checkout", "development"], cwd=repo, capture_output=True, check=True)

    worktree = tmp_path / "worker-worktree"
    ok, msg = add_worktree(repo, worktree, ref="development")
    assert ok, msg
    ok, branch = create_feature_branch(worktree, "development", "t1-worker-task")
    assert ok, branch

    def _cqt(_path: Path):
        return True, True

    result = sh_setup.run_setup_impl(
        repo_path=worktree, task_title="Demo", configure_quality_tooling=_cqt
    )

    assert result.linting_configured and result.testing_configured
    assert "Setup failed" not in result.summary
    # The worktree's own feature-branch checkout is untouched; development
    # stays attached in the shared checkout, unaffected.
    wt_branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=worktree, capture_output=True, text=True
    )
    assert wt_branch.stdout.strip() == branch
    repo_branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True
    )
    assert repo_branch.stdout.strip() == "development"


def test_ensure_readme_logs_when_commit_raises(tmp_path: Path, monkeypatch, caplog):
    """Ensure readme logs when commit raises."""

    def _raise(*_a, **_k):
        raise RuntimeError("no git")

    # setup.py now imports write_files_and_commit at module top, so patch it there.
    monkeypatch.setattr(sh_setup, "write_files_and_commit", _raise)
    with caplog.at_level("WARNING"):
        sh_setup._ensure_readme_with_title(tmp_path, "Title")
    assert "could not commit readme" in caplog.text.lower()
    assert (tmp_path / "README.md").read_text(encoding="utf-8").startswith("# Title")


def test_write_microtask_files_strips_leading_slash(tmp_path: Path):
    """Write microtask files strips leading slash."""
    sh_review_cycle._write_microtask_files(tmp_path, {"/pkg/mod.py": "content", "top.txt": "t"})
    assert (tmp_path / "pkg" / "mod.py").read_text(encoding="utf-8") == "content"
    assert (tmp_path / "top.txt").read_text(encoding="utf-8") == "t"


def test_run_execution_impl_runner_success_and_general_fallback(tmp_path: Path):
    """Run execution impl runner success and general fallback."""

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


def test_run_planning_impl_appends_tool_agent_recommendations():
    """Run planning impl appends tool agent recommendations."""
    template = (
        "## MICROTASKS ##\n---\nid: mt-1\ntitle: t\ndescription: d\ntool_agent: general\n"
        "depends_on:\n---\n## END MICROTASKS ##\n## LANGUAGE ##\npython\n## END LANGUAGE ##\n"
        "## SUMMARY ##\nbase\n## END SUMMARY ##\n"
    )

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
        runner=_runner(template),
    )
    # The planning tool agent's recommendation was appended to the summary; the
    # raising agent was caught and skipped.
    assert "use an index" in result.summary
    assert result.microtasks[0].id == "mt-1"


# --- LlmRunner + shared writer + problem_solving ---------------------------


def test_llm_runner_run_stringifies_and_strips():
    """Llm runner run stringifies and strips."""
    captured = {}

    def factory(*, model=None):
        captured["model"] = model

        def agent(prompt: str):
            captured["prompt"] = prompt
            return "  spaced result \n"

        return agent

    runner = LlmRunner(agent_factory=factory, resolve_model=lambda llm: f"model:{llm}")
    out = runner.run("LLM", "the prompt")
    assert out == "spaced result"  # stringified + stripped
    assert captured == {"model": "model:LLM", "prompt": "the prompt"}


def test_write_repo_text_files_rejects_traversal(tmp_path: Path):
    """Write repo text files rejects traversal."""
    write_repo_text_files(tmp_path, {"/pkg/mod.py": "content", "top.txt": "t"})
    assert (tmp_path / "pkg" / "mod.py").read_text(encoding="utf-8") == "content"
    assert (tmp_path / "top.txt").read_text(encoding="utf-8") == "t"

    with pytest.raises(UnsafeRepoPathError, match="Path traversal"):
        write_repo_text_files(tmp_path, {"../escape.py": "x"})
    # A sibling-prefixed directory must not be mistaken for containment.
    assert not (tmp_path.parent / "escape.py").exists()


def test_write_repo_text_files_rejects_empty_path(tmp_path: Path):
    """Write repo text files rejects an empty relative path."""
    with pytest.raises(UnsafeRepoPathError, match="must not be empty"):
        write_repo_text_files(tmp_path, {"/": "x"})


def test_write_microtask_output_or_fail_success_and_rejection(tmp_path: Path):
    """Write microtask output helper: writes on a safe path, review-fails on unsafe.

    On rejection the recorded rollback reverts both the in-memory result and the
    worktree: a key this microtask created is removed/unlinked, and a key it
    overwrote is restored to its pre-microtask value in both places.
    """
    mt = SimpleNamespace(id="mt-1", status="in_progress", notes="")
    review_failed_ids: set = set()
    # ``shared.py`` pre-exists on disk and in all_files (an earlier product);
    # ``gen.py`` is created by this microtask.
    (tmp_path / "shared.py").write_text("orig", encoding="utf-8")
    all_files = {"kept.py": "k", "shared.py": "orig"}

    # Snapshot the pre-write baselines the way the loop does, then write.
    rollback = sh_exec._MicrotaskRollback()
    sh_rollback._record_prior_values(
        rollback, tmp_path, all_files, {"gen.py": "g2", "shared.py": "clob"}
    )
    ok = sh_review_cycle.write_microtask_output_or_fail(
        tmp_path,
        {"gen.py": "g2", "shared.py": "clob"},
        mt=mt,
        task_id="t1",
        review_failed_ids=review_failed_ids,
        all_files=all_files,
        rollback=rollback,
        review_failed_status="REVIEW_FAILED",
    )
    assert ok is True
    all_files.update({"gen.py": "g2", "shared.py": "clob"})
    assert (tmp_path / "gen.py").read_text(encoding="utf-8") == "g2"
    assert not review_failed_ids

    # Unsafe path → no exception, marks review-failed, rolls back this microtask's
    # contributions: ``gen.py`` (created) is removed/unlinked and ``shared.py`` (an
    # earlier file this one overwrote) is restored — in all_files and on disk.
    rejected = sh_review_cycle.write_microtask_output_or_fail(
        tmp_path,
        {"../evil.py": "x"},
        mt=mt,
        task_id="t1",
        review_failed_ids=review_failed_ids,
        all_files=all_files,
        rollback=rollback,
        review_failed_status="REVIEW_FAILED",
    )
    assert rejected is False
    assert mt.status == "REVIEW_FAILED"
    assert "mt-1" in review_failed_ids
    assert "gen.py" not in all_files  # created by this microtask → removed
    assert all_files["kept.py"] == "k"  # untouched key left alone
    assert all_files["shared.py"] == "orig"  # earlier file restored
    assert not (tmp_path.parent / "evil.py").exists()
    assert not (tmp_path / "gen.py").exists()  # created file unlinked
    assert (tmp_path / "shared.py").read_text(encoding="utf-8") == "orig"  # disk restored


def test_resolve_physical_path_and_snapshot_states(tmp_path: Path):
    """``_resolve_physical_path_in_repo`` collapses a symlink chain to the physical file
    and rejects out-of-repo escapes; ``_snapshot_disk_state`` reports file/dir/absent."""
    root = tmp_path.resolve()
    # a symlink chain to an in-repo file resolves to that physical file
    (tmp_path / "real.py").write_text("X", encoding="utf-8")
    (tmp_path / "mid").symlink_to("real.py")
    (tmp_path / "top").symlink_to("mid")
    assert sh_rollback._resolve_physical_path_in_repo(root, root / "top") == root / "real.py"
    # a symlink pointing out of the repo is rejected
    (tmp_path / "escape").symlink_to(tmp_path.parent)
    assert sh_rollback._resolve_physical_path_in_repo(root, root / "escape") is None

    assert sh_rollback._snapshot_disk_state(root / "real.py").file_bytes == b"X"
    (tmp_path / "adir").mkdir()
    directory = sh_rollback._snapshot_disk_state(root / "adir")
    assert directory.file_bytes is None and not directory.absent
    assert sh_rollback._snapshot_disk_state(root / "missing").absent


def test_file_lock_keys_collapse_lexical_aliases(tmp_path: Path):
    """``shared.py`` and ``./shared.py`` map to the same physical lock key."""
    root = tmp_path.resolve()
    (tmp_path / "shared.py").write_text("x", encoding="utf-8")
    keys = sh_rollback._file_lock_keys(root, ["shared.py", "./shared.py", "shared.py"])
    expected = sh_rollback._resolve_physical_path_in_repo(root, root / "shared.py")
    assert expected is not None
    assert keys == [str(expected)]


def test_snapshot_disk_state_degrades_on_read_error(tmp_path: Path, monkeypatch):
    """A read/stat OSError during snapshot degrades to a leave-alone entry, never raised,
    so rollback bookkeeping cannot abort the run."""
    f = tmp_path / "f.py"
    f.write_text("x", encoding="utf-8")

    def _boom(self, *a, **k):
        raise OSError("unreadable")

    monkeypatch.setattr(Path, "read_bytes", _boom)
    entry = sh_rollback._snapshot_disk_state(tmp_path.resolve() / "f.py")
    assert entry.file_bytes is None and not entry.absent  # leave-alone, no exception


def _issue(**kw):
    base = dict(
        source="review",
        severity="high",
        file_path="a.py",
        description="bug",
        recommendation="fix it",
    )
    base.update(kw)
    return be_models.ReviewIssue(**base)


def test_run_batch_coding_fixes_impl_skips_non_dict_issues_addressed():
    """A non-dict entry in issues_addressed must be skipped, not crash."""
    parsed = {
        "files": {"a.py": "fixed"},
        # Malformed: one bare string alongside a valid dict entry. addressed_count
        # (2) < len(actionable) (3), so the unresolved-computation block runs and
        # must skip the non-dict entry instead of calling ``.get`` on a str.
        "issues_addressed": ["oops-not-a-dict", {"issue_index": 2}],
        "summary": "did it",
    }
    result = sh_ps.run_batch_coding_fixes_impl(
        llm=object(),
        microtask=SimpleNamespace(id="mt-1"),
        issues=[_issue(), _issue(description="bug2"), _issue(description="bug3")],
        current_files={"a.py": "orig"},
        language="python",
        task_id="t1",
        phase_name="code_review",
        detail_callback=None,
        profile=_BACKEND_PROFILE,
        models=be_models,
        batch_fix_prompt="{language_conventions}{issue_count}{phase_name}{formatted_issues}{current_code}",
        parse_batch_fix_template=lambda _raw: parsed,
        runner=_runner("ignored — parse stub returns the parsed dict"),
    )
    # No crash on the bare string; issue 2 (index 1) addressed, issues 1 & 3 unresolved.
    assert result.files["a.py"] == "fixed"
    assert len(result.unresolved_issues) == 2


def test_run_batch_coding_fixes_impl_ignores_non_dict_files():
    """A non-dict ``files`` value from the parser must be ignored, not crash."""
    parsed = {
        "files": ["a.py", "not-a-mapping"],
        "issues_addressed": [],
        "summary": "did it",
    }
    result = sh_ps.run_batch_coding_fixes_impl(
        llm=object(),
        microtask=SimpleNamespace(id="mt-1"),
        issues=[_issue()],
        current_files={"a.py": "orig"},
        language="python",
        task_id="t1",
        phase_name="code_review",
        detail_callback=None,
        profile=_BACKEND_PROFILE,
        models=be_models,
        batch_fix_prompt="{language_conventions}{issue_count}{phase_name}{formatted_issues}{current_code}",
        parse_batch_fix_template=lambda _raw: parsed,
        runner=_runner("ignored — parse stub returns the parsed dict"),
    )
    # No crash on the list; files falls back to {} so the prior version is kept.
    assert result.files == {"a.py": "orig"}


def test_run_batch_coding_fixes_impl_tracks_unresolved_by_index_not_identity():
    """A rejected file's issue stays unresolved by list position, not ``id(issue)``.

    Two issues share identical field values (same ``file_path``/description) so
    they compare equal under Pydantic's value-based ``__eq__`` -- proving the
    tracking can't rely on object identity or value equality, only position.
    Issue 1 (a.py) and issue 3 (b.py) are claimed addressed; issue 2 (a.py) is
    left unaddressed by the first pass. The LLM's rewrite of a.py is rejected
    as invalid Python, so issue 1's a.py fix never actually landed -- issue 1
    must be added back as unresolved without duplicating issue 2 (already
    unresolved and also pointing at a.py).
    """
    parsed = {
        "files": {"a.py": "def broken(:\n", "b.py": "fixed"},
        "issues_addressed": [{"issue_index": 1}, {"issue_index": 3}],
        "summary": "did it",
    }
    issue1 = _issue(file_path="a.py")
    issue2 = _issue(file_path="a.py")
    issue3 = _issue(file_path="b.py")
    assert issue1 == issue2  # identical field values -- equality can't disambiguate them
    result = sh_ps.run_batch_coding_fixes_impl(
        llm=object(),
        microtask=SimpleNamespace(id="mt-1"),
        issues=[issue1, issue2, issue3],
        current_files={"a.py": "orig", "b.py": "orig"},
        language="python",
        task_id="t1",
        phase_name="code_review",
        detail_callback=None,
        profile=_BACKEND_PROFILE,
        models=be_models,
        batch_fix_prompt="{language_conventions}{issue_count}{phase_name}{formatted_issues}{current_code}",
        parse_batch_fix_template=lambda _raw: parsed,
        runner=_runner("ignored — parse stub returns the parsed dict"),
    )
    # a.py's invalid rewrite is discarded -- the prior version is kept.
    assert result.files["a.py"] == "orig"
    assert result.files["b.py"] == "fixed"
    # Both a.py issues (issue1 from the rejected-file pass, issue2 from the
    # not-addressed pass) surface exactly once each; b.py's issue3 resolved.
    # ``issue1 == issue2`` (identical field values), so identity (``is``),
    # not equality, is what must distinguish them here.
    assert len(result.unresolved_issues) == 2
    assert sum(1 for u in result.unresolved_issues if u is issue1) == 1
    assert sum(1 for u in result.unresolved_issues if u is issue2) == 1
    assert not any(u is issue3 for u in result.unresolved_issues)


def test_run_batch_coding_fixes_impl_detects_duplicate_addressed_indices():
    """A same-length ``issues_addressed`` with a duplicate index must not report resolved.

    ``actionable`` has 2 issues; ``issues_addressed`` also has 2 entries but both point
    at issue_index 1, so issue 2 was never actually addressed. Length alone must not be
    used as a shortcut to skip validation -- ``resolved`` must derive from the real set
    of valid addressed indices.
    """
    parsed = {
        "files": {"a.py": "fixed"},
        "issues_addressed": [{"issue_index": 1}, {"issue_index": 1}],
        "summary": "did it",
    }
    result = sh_ps.run_batch_coding_fixes_impl(
        llm=object(),
        microtask=SimpleNamespace(id="mt-1"),
        issues=[_issue(), _issue(description="bug2")],
        current_files={"a.py": "orig"},
        language="python",
        task_id="t1",
        phase_name="code_review",
        detail_callback=None,
        profile=_BACKEND_PROFILE,
        models=be_models,
        batch_fix_prompt="{language_conventions}{issue_count}{phase_name}{formatted_issues}{current_code}",
        parse_batch_fix_template=lambda _raw: parsed,
        runner=_runner("ignored — parse stub returns the parsed dict"),
    )
    assert result.resolved is False
    assert len(result.unresolved_issues) == 1


def test_fix_issues_one_at_a_time_impl_resolves_then_reports_unresolved():
    """First issue resolves via a file fix; second never resolves → unresolved."""
    responses = iter(
        [
            "## FILE a.py ##\nfixed\n## RESOLVED ##\ntrue\n## END RESOLVED ##\n",  # issue 1 fixed
            "## RESOLVED ##\nfalse\n",  # issue 2 attempt → no files, not resolved
        ]
    )
    parses = iter(
        [
            {"files": {"a.py": "fixed"}, "resolved": True, "summary": "s", "root_cause": "rc"},
            {"files": {}, "resolved": False},
        ]
    )

    runner = LlmRunner(
        agent_factory=lambda *, model=None: lambda _p: next(responses),
        resolve_model=lambda _llm: None,
    )
    merged, fixes, unresolved = sh_ps._fix_issues_one_at_a_time_impl(
        llm=object(),
        actionable=[_issue(), _issue(description="bug2")],
        current_files={"a.py": "orig"},
        lang_conv="PY",
        task_id="t1",
        single_issue_prompt="{source}{severity}{description}{file_path}{recommendation}{current_code}",
        parse_single=lambda _raw: next(parses),
        has_language_conventions=False,
        runner=runner,
    )
    assert merged["a.py"] == "fixed"
    assert len(fixes) == 1 and fixes[0]["fix"] == "s"
    assert len(unresolved) == 1


def test_fix_issues_one_at_a_time_impl_survives_parse_failure(caplog):
    """A parser exception on one attempt is logged and the loop retries, not aborts."""
    responses = iter(["raw-1", "raw-2"])

    def _parse_single(_raw):
        parsed = next(parse_results)
        if parsed is _RAISE:
            raise ValueError("malformed LLM output")
        return parsed

    _RAISE = object()
    parse_results = iter(
        [
            _RAISE,  # attempt 1: parser raises
            {"files": {"a.py": "fixed"}, "resolved": True, "summary": "s", "root_cause": "rc"},
        ]
    )

    runner = LlmRunner(
        agent_factory=lambda *, model=None: lambda _p: next(responses),
        resolve_model=lambda _llm: None,
    )
    with caplog.at_level("WARNING"):
        merged, fixes, unresolved = sh_ps._fix_issues_one_at_a_time_impl(
            llm=object(),
            actionable=[_issue()],
            current_files={"a.py": "orig"},
            lang_conv="PY",
            task_id="t1",
            single_issue_prompt="{source}{severity}{description}{file_path}{recommendation}{current_code}",
            parse_single=_parse_single,
            has_language_conventions=False,
            runner=runner,
        )
    # The parse failure on attempt 1 doesn't abort the phase -- attempt 2 still runs and resolves.
    assert merged["a.py"] == "fixed"
    assert len(fixes) == 1 and fixes[0]["fix"] == "s"
    assert not unresolved
    assert any("parsing/validation failed" in r.message for r in caplog.records)


# --- prompt byte-identity regression lock --------------------------------


# SHA-256 of each team's prompt constant, captured after the consolidation was
# verified to reproduce the pre-refactor templates byte-for-byte. Byte-identical
# prompt output is an acceptance criterion of that refactor, so this test locks
# the six constants against silent drift. If you INTENTIONALLY change a template
# or builder, regenerate a digest with:
#     python -c "import hashlib; from software_engineering_team.<team>_code_v2_team \
#         import prompts as p; print(hashlib.sha256(p.<NAME>.encode()).hexdigest())"
_EXPECTED_PROMPT_DIGESTS = {
    (
        "backend",
        "PLANNING_PROMPT",
    ): "a32179389eda2720e8a9b55e014f96f88c4364ce12b158d344dc62018d5a0476",
    (
        "backend",
        "EXECUTION_PROMPT",
    ): "09aee4531ca79f0c99088cdd14cf4c799fe8eb29aa50d64c7c5ff3ae2f54e4b1",
    (
        "backend",
        "PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT",
    ): "c9ace258417f3dd641023de0cd1e230da3b3aaebd1026822c03d7294b2dc95a2",
    (
        "frontend",
        "PLANNING_PROMPT",
    ): "aaca2421d786e2f4c612b14030528f4a63a0ba714b67bf936af900bf59ad0a60",
    (
        "frontend",
        "EXECUTION_PROMPT",
    ): "13daf0ab51f32e3c35079403ce8f5d5f9a19e65d9122c5acc02657daf1f844be",
    (
        "frontend",
        "PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT",
    ): "4e0ad641c389180f5ecdb90909896937ed00fccfea449a5b2929b095918d5477",
}


@pytest.mark.parametrize(("team", "name"), sorted(_EXPECTED_PROMPT_DIGESTS))
def test_prompt_constants_are_byte_stable(team: str, name: str):
    """Each team's built prompt constant must stay byte-for-byte stable.

    Guards the refactor's byte-identity acceptance criterion: a change to the
    shared builders or templates that alters any team prompt fails here.
    """
    import hashlib
    import importlib

    prompts = importlib.import_module(f"software_engineering_team.{team}_code_v2_team.prompts")
    actual = hashlib.sha256(getattr(prompts, name).encode("utf-8")).hexdigest()
    assert actual == _EXPECTED_PROMPT_DIGESTS[(team, name)], (
        f"{team} {name} changed; if intentional, update its digest in _EXPECTED_PROMPT_DIGESTS."
    )


# --- write_repo_text_files: root / dot / traversal rejection -------------


@pytest.mark.parametrize("bad_key", [".", "a/..", "sub/../..", "..", "/"])
def test_write_repo_text_files_rejects_root_and_traversal(tmp_path: Path, bad_key: str):
    """A key that resolves to (or escapes) the repo root raises UnsafeRepoPathError.

    Regression for the case where such a key slipped past the containment guard
    and reached ``write_text`` on the repo directory, raising a bare
    ``IsADirectoryError`` that the ``except UnsafeRepoPathError`` handlers missed.
    """
    with pytest.raises(UnsafeRepoPathError):
        write_repo_text_files(tmp_path, {bad_key: "x"})
    # Nothing was written and the repo root is still an empty directory.
    assert list(tmp_path.iterdir()) == []


def test_write_repo_text_files_writes_nested_and_strips_leading_slash(tmp_path: Path):
    """Valid nested and leading-slash keys are written under the repo root."""
    write_repo_text_files(tmp_path, {"/pkg/mod.py": "a", "top.txt": "b"})
    assert (tmp_path / "pkg" / "mod.py").read_text(encoding="utf-8") == "a"
    assert (tmp_path / "top.txt").read_text(encoding="utf-8") == "b"


def test_write_repo_text_files_is_atomic_on_reject(tmp_path: Path):
    """A batch with a later unsafe key writes none of its files (atomic reject).

    Regression: paths are validated before any write, so a valid key preceding
    an unsafe one is not left partially written in the working tree.
    """
    with pytest.raises(UnsafeRepoPathError):
        write_repo_text_files(tmp_path, {"good.py": "x", "../bad.py": "y"})
    assert list(tmp_path.iterdir()) == []


def test_write_files_and_commit_reports_unsafe_path_as_failure(tmp_path: Path):
    """write_files_and_commit returns (False, msg) on an unsafe path, not a raise.

    Its contract is ``(success, message)`` and callers unpack it; an unsafe key
    must route into the write-failure path rather than abort with an exception,
    and no file of the batch may be written.
    """
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    ok, msg = write_files_and_commit(tmp_path, {"good.py": "x", "../bad.py": "y"}, "msg")
    assert ok is False
    assert "unsafe" in msg.lower()
    assert not (tmp_path / "good.py").exists()


# --- problem-solving cleanup helpers / batch / runners ----------------------


def test_format_summary_for_log_collapses_and_bounds():
    long = "line1\nline2 " + ("x" * 200)
    out = sh_ps._format_summary_for_log(long, max_chars=40)
    assert "\n" not in out
    assert len(out) == 40
    assert out.endswith("…")
    assert "line1 line2" in out


def test_fill_named_placeholders_preserves_unrelated_braces():
    out = sh_ps._fill_named_placeholders(
        "conv={language_conventions} code={current_code}",
        language_conventions="PY",
        current_code="def f(x):\n    return {x}\n",
    )
    assert out == "conv=PY code=def f(x):\n    return {x}\n"
    assert "{x}" in out


def test_fill_named_placeholders_does_not_rescan_inserted_values():
    """A value containing another placeholder token must not be expanded later."""
    out = sh_ps._fill_named_placeholders(
        "desc={description}|code={current_code}",
        description="see {current_code} for context",
        current_code="BODY",
    )
    assert out == "desc=see {current_code} for context|code=BODY"


def test_attr_or_preserves_empty_string():
    issue = SimpleNamespace(source="", severity=None, description="d")
    assert sh_ps._attr_or(issue, "source", "review") == ""
    assert sh_ps._attr_or(issue, "severity", "medium") == "medium"
    assert sh_ps._attr_or(issue, "description", "No description") == "d"


def test_format_all_code_truncation_marker_respects_budget():
    files = {"a.py": "AAAA", "b.py": "BBBBBBBBBB"}
    out = sh_ps._format_all_code(files, max_chars=20)
    assert len(out) <= 20


def test_format_all_code_counts_join_separators_in_budget():
    """Many small files must not exceed max_chars via uncounted join separators."""
    files = {f"f{i}.py": "x" for i in range(30)}
    out = sh_ps._format_all_code(files, max_chars=50)
    assert len(out) <= 50


def test_format_issues_for_batch_normalizes_empty_severity():
    out = sh_ps._format_issues_for_batch([_issue(severity="")])
    assert "- **Severity:** medium" in out


def test_fix_issues_one_at_a_time_prompt_normalizes_empty_severity():
    captured: list[str] = []
    issue = _issue(severity="")
    runner = _runner("## RESOLVED ##\ntrue\n", on_prompt=captured.append)
    sh_ps._fix_issues_one_at_a_time_impl(
        llm=object(),
        actionable=[issue],
        current_files={"a.py": "x"},
        lang_conv="PY",
        task_id="t1",
        single_issue_prompt="{severity}|{description}|{current_code}",
        parse_single=lambda _raw: {"files": {}, "resolved": True},
        has_language_conventions=False,
        runner=runner,
    )
    assert captured[0].startswith("medium|")


def test_format_issues_for_batch_preserves_empty_source():
    out = sh_ps._format_issues_for_batch([_issue(source="")])
    assert "- **Source:** " in out
    # Empty string must not be replaced with the "review" default.
    assert "- **Source:** review" not in out


def test_run_batch_coding_fixes_impl_preserves_braces_in_code():
    captured: list[str] = []
    parsed = {
        "files": {"a.py": "def f(x):\n    return x\n"},
        "issues_addressed": [{"issue_index": 1}],
        "summary": "ok",
    }
    result = sh_ps.run_batch_coding_fixes_impl(
        llm=object(),
        microtask=SimpleNamespace(id="mt-1"),
        issues=[_issue()],
        current_files={"a.py": "def f(x):\n    return {x}\n"},
        language="python",
        task_id="t1",
        phase_name="code_review",
        detail_callback=None,
        profile=_BACKEND_PROFILE,
        models=be_models,
        batch_fix_prompt=(
            "{language_conventions}|{issue_count}|{phase_name}|{formatted_issues}|{current_code}"
        ),
        parse_batch_fix_template=lambda _raw: parsed,
        runner=_runner("x", on_prompt=captured.append),
    )
    assert result.resolved is True
    assert "{x}" in captured[0]


def test_run_batch_coding_fixes_impl_parse_failure_returns_unresolved():
    def _boom(_raw: str):
        raise ValueError("bad json")

    result = sh_ps.run_batch_coding_fixes_impl(
        llm=object(),
        microtask=SimpleNamespace(id="mt-1"),
        issues=[_issue()],
        current_files={"a.py": "orig"},
        language="python",
        task_id="t1",
        phase_name="code_review",
        detail_callback=None,
        profile=_BACKEND_PROFILE,
        models=be_models,
        batch_fix_prompt="{language_conventions}{issue_count}{phase_name}{formatted_issues}{current_code}",
        parse_batch_fix_template=_boom,
        runner=_runner("raw"),
    )
    assert result.resolved is False
    assert len(result.unresolved_issues) == 1
    assert "parse" in result.summary.lower()


def test_run_batch_coding_fixes_impl_rejects_non_list_issues_addressed():
    parsed = {
        "files": {"a.py": "fixed"},
        "issues_addressed": {"issue_index": 1},
        "summary": "did it",
    }
    result = sh_ps.run_batch_coding_fixes_impl(
        llm=object(),
        microtask=SimpleNamespace(id="mt-1"),
        issues=[_issue()],
        current_files={"a.py": "orig"},
        language="python",
        task_id="t1",
        phase_name="code_review",
        detail_callback=None,
        profile=_BACKEND_PROFILE,
        models=be_models,
        batch_fix_prompt="{language_conventions}{issue_count}{phase_name}{formatted_issues}{current_code}",
        parse_batch_fix_template=lambda _raw: parsed,
        runner=_runner("x"),
    )
    assert result.resolved is False
    assert len(result.unresolved_issues) == 1


def test_run_batch_coding_fixes_impl_addressed_count_uses_valid_indices():
    parsed = {
        "files": {"a.py": "fixed"},
        "issues_addressed": ["nope", {"issue_index": 1}, {"issue_index": 1}, {"issue_index": 99}],
        "summary": "did it",
    }
    details: list[str] = []
    result = sh_ps.run_batch_coding_fixes_impl(
        llm=object(),
        microtask=SimpleNamespace(id="mt-1"),
        issues=[_issue(), _issue(description="bug2")],
        current_files={"a.py": "orig"},
        language="python",
        task_id="t1",
        phase_name="code_review",
        detail_callback=details.append,
        profile=_BACKEND_PROFILE,
        models=be_models,
        batch_fix_prompt="{language_conventions}{issue_count}{phase_name}{formatted_issues}{current_code}",
        parse_batch_fix_template=lambda _raw: parsed,
        runner=_runner("x"),
    )
    assert result.resolved is False
    assert len(result.unresolved_issues) == 1
    assert any("1/2" in d for d in details)


def test_run_batch_coding_fixes_impl_coerces_non_str_summary():
    parsed = {
        "files": {"a.py": "def broken(:\n"},
        "issues_addressed": [{"issue_index": 1}],
        "summary": 12345,
    }
    result = sh_ps.run_batch_coding_fixes_impl(
        llm=object(),
        microtask=SimpleNamespace(id="mt-1"),
        issues=[_issue()],
        current_files={"a.py": "orig"},
        language="python",
        task_id="t1",
        phase_name="code_review",
        detail_callback=None,
        profile=_BACKEND_PROFILE,
        models=be_models,
        batch_fix_prompt="{language_conventions}{issue_count}{phase_name}{formatted_issues}{current_code}",
        parse_batch_fix_template=lambda _raw: parsed,
        runner=_runner("x"),
    )
    assert isinstance(result.summary, str)
    assert "12345" in result.summary
    assert "rejected" in result.summary.lower()


def test_run_batch_coding_fixes_impl_no_actionable_returns_fresh_dict():
    current = {"a.py": "orig"}
    result = sh_ps.run_batch_coding_fixes_impl(
        llm=object(),
        microtask=SimpleNamespace(id="mt-1"),
        issues=[_issue(severity="low")],
        current_files=current,
        language="python",
        task_id="t1",
        phase_name="code_review",
        detail_callback=None,
        profile=_BACKEND_PROFILE,
        models=be_models,
        batch_fix_prompt="{language_conventions}{issue_count}{phase_name}{formatted_issues}{current_code}",
        parse_batch_fix_template=lambda _raw: {},
        runner=_runner("x"),
    )
    assert result.resolved is True
    assert result.files == current
    assert result.files is not current


def test_run_batch_coding_fixes_impl_treats_empty_severity_as_actionable():
    """Empty severity must default to medium and enter the batch fix path."""
    captured: list[str] = []
    parsed = {
        "files": {"a.py": "x = 1\n"},
        "issues_addressed": [{"issue_index": 1}],
        "summary": "ok",
    }
    result = sh_ps.run_batch_coding_fixes_impl(
        llm=object(),
        microtask=SimpleNamespace(id="mt-1"),
        issues=[_issue(severity="")],
        current_files={"a.py": "orig"},
        language="python",
        task_id="t1",
        phase_name="code_review",
        detail_callback=captured.append,
        profile=_BACKEND_PROFILE,
        models=be_models,
        batch_fix_prompt="{language_conventions}{issue_count}{phase_name}{formatted_issues}{current_code}",
        parse_batch_fix_template=lambda _raw: parsed,
        runner=_runner("x"),
    )
    assert result.resolved is True
    assert any("Fixing all 1" in d for d in captured)


def test_tool_file_rewrite_counts_as_applied_even_with_recommendations():
    """Tool file updates count as applied; recommendations still do not."""

    class _Agent:
        def problem_solve(self, _inp):
            return SimpleNamespace(
                files={"a.py": "x = 1\n"},
                recommendations=["also consider Y"],
                summary="tool rewrite",
            )

    parses = iter(
        [
            {
                "files": {"a.py": "llm = 1\n"},
                "resolved": True,
                "summary": "llm fix",
                "root_cause": "",
            }
        ]
    )
    kind = be_models.ToolAgentKind.GENERAL
    result = sh_ps.run_problem_solving_impl(
        llm=object(),
        task=_task(),
        review_result=SimpleNamespace(issues=[_issue()]),
        current_files={"a.py": "orig"},
        language="python",
        repo_path="/tmp",
        tool_agents={kind: _Agent()},
        profile=_BACKEND_PROFILE,
        models=be_models,
        single_issue_prompt="{language_conventions}{source}{severity}{description}{file_path}{recommendation}{current_code}",
        parse_single=lambda _raw: next(parses),
        runner=_runner("## FILE ##"),
    )
    assert result.summary.startswith("Applied 2 fix(s);")
    assert sum(1 for e in result.fixes_applied if e.get("advisory")) == 1
    assert sum(1 for e in result.fixes_applied if str(e.get("fix", "")).startswith("updated ")) == 1


def test_applied_fix_count_ignores_freeform_recommendation_summary():
    """An LLM fix whose summary text is literally 'recommendation' still counts."""
    parses = iter(
        [
            {
                "files": {"a.py": "x = 1\n"},
                "resolved": True,
                "summary": "recommendation",
                "root_cause": "",
            }
        ]
    )
    result = sh_ps.run_problem_solving_impl(
        llm=object(),
        task=_task(),
        review_result=SimpleNamespace(issues=[_issue()]),
        current_files={"a.py": "orig"},
        language="python",
        repo_path="/tmp",
        tool_agents=None,
        profile=_BACKEND_PROFILE,
        models=be_models,
        single_issue_prompt="{language_conventions}{source}{severity}{description}{file_path}{recommendation}{current_code}",
        parse_single=lambda _raw: next(parses),
        runner=_runner("## FILE ##"),
    )
    assert result.summary.startswith("Applied 1 fix(s);")
    assert any(
        e.get("fix") == "recommendation" and not e.get("advisory") for e in result.fixes_applied
    )


def test_recommendation_only_tool_does_not_inflate_applied_fix_count():
    class _Agent:
        def problem_solve(self, _inp):
            return SimpleNamespace(files={}, recommendations=["do X"], summary="advise")

    parses = iter(
        [
            {
                "files": {"a.py": "x = 1\n"},
                "resolved": True,
                "summary": "llm fix",
                "root_cause": "",
            }
        ]
    )
    kind = be_models.ToolAgentKind.GENERAL
    result = sh_ps.run_problem_solving_impl(
        llm=object(),
        task=_task(),
        review_result=SimpleNamespace(issues=[_issue()]),
        current_files={"a.py": "orig"},
        language="python",
        repo_path="/tmp",
        tool_agents={kind: _Agent()},
        profile=_BACKEND_PROFILE,
        models=be_models,
        single_issue_prompt="{language_conventions}{source}{severity}{description}{file_path}{recommendation}{current_code}",
        parse_single=lambda _raw: next(parses),
        runner=_runner("## FILE ##"),
    )
    assert result.summary.startswith("Applied 1 fix(s);")
    assert any(e.get("advisory") for e in result.fixes_applied)


def test_fix_issues_one_at_a_time_preserves_braces_and_empty_source():
    captured: list[str] = []
    issue = _issue(source="", description="use {placeholder} carefully")
    runner = _runner("## RESOLVED ##\ntrue\n", on_prompt=captured.append)
    merged, fixes, unresolved = sh_ps._fix_issues_one_at_a_time_impl(
        llm=object(),
        actionable=[issue],
        current_files={"a.py": "return {x}"},
        lang_conv="PY",
        task_id="t1",
        single_issue_prompt=(
            "{source}|{severity}|{description}|{file_path}|{recommendation}|{current_code}"
        ),
        parse_single=lambda _raw: {"files": {}, "resolved": True},
        has_language_conventions=False,
        runner=runner,
    )
    assert not unresolved
    assert captured[0].startswith("|")
    assert "{placeholder}" in captured[0]
    assert "{x}" in captured[0]


def test_run_problem_solving_impl_handles_none_issues():
    current = {"a.py": "orig"}
    result = sh_ps.run_problem_solving_impl(
        llm=object(),
        task=_task(),
        review_result=SimpleNamespace(issues=None),
        current_files=current,
        language="python",
        repo_path="/tmp",
        tool_agents=None,
        profile=_BACKEND_PROFILE,
        models=be_models,
        single_issue_prompt="{source}{severity}{description}{file_path}{recommendation}{current_code}",
        parse_single=lambda _raw: {},
        runner=_runner("x"),
    )
    assert result.resolved is True
    assert result.files == current
    assert result.files is not current


def test_run_problem_solving_impl_combines_tool_and_fix_summaries():
    class _Agent:
        def problem_solve(self, _inp):
            return SimpleNamespace(
                files={"a.py": "x = 1\n"},
                recommendations=["r1"],
                summary="tool did work",
            )

    parses = iter(
        [
            {
                "files": {"a.py": "llm = 1\n"},
                "resolved": True,
                "summary": "llm fix",
                "root_cause": "",
            }
        ]
    )
    kind = be_models.ToolAgentKind.BUILD_SPECIALIST
    result = sh_ps.run_problem_solving_impl(
        llm=object(),
        task=_task(),
        review_result=SimpleNamespace(issues=[_issue()]),
        current_files={"a.py": "orig"},
        language="python",
        repo_path="/tmp",
        tool_agents={kind: _Agent()},
        profile=_BACKEND_PROFILE,
        models=be_models,
        single_issue_prompt="{language_conventions}{source}{severity}{description}{file_path}{recommendation}{current_code}",
        parse_single=lambda _raw: next(parses),
        runner=_runner("## FILE ##"),
    )
    assert "Applied" in result.summary
    assert "Tool" in result.summary
    assert any(
        set(e) >= {"source", "issue", "recommendation", "fix", "root_cause"}
        for e in result.fixes_applied
        if e.get("source") == kind.value
    )


def test_apply_tool_agents_files_only_updates_summary_and_schema():
    class _Agent:
        def problem_solve(self, _inp):
            return SimpleNamespace(files={"a.py": "x = 1\n"}, recommendations=[], summary="patched")

    kind = be_models.ToolAgentKind.GENERAL
    merged = {"a.py": "orig"}
    fixes: list = []
    parts: list = []
    sh_ps._apply_tool_agents_problem_solve(
        tool_agents={kind: _Agent()},
        phase_inp=SimpleNamespace(),
        merged=merged,
        fixes_applied=fixes,
        summary_parts=parts,
        task_id="t1",
        microtask_id="mt-1",
    )
    assert merged["a.py"].startswith("x")
    assert any("Tool" in p for p in parts)
    assert parts
    assert len(fixes) == 1
    assert fixes[0]["fix"].startswith("updated ")
    assert not fixes[0].get("advisory")


def test_apply_tool_agents_recommendation_schema_includes_llm_keys():
    class _Agent:
        def problem_solve(self, _inp):
            return SimpleNamespace(files={}, recommendations=["do X"], summary="s")

    kind = be_models.ToolAgentKind.GENERAL
    fixes: list = []
    parts: list = []
    sh_ps._apply_tool_agents_problem_solve(
        tool_agents={kind: _Agent()},
        phase_inp=SimpleNamespace(),
        merged={},
        fixes_applied=fixes,
        summary_parts=parts,
        task_id="t1",
        microtask_id="mt-1",
    )
    assert set(fixes[0]) >= {
        "source",
        "issue",
        "recommendation",
        "fix",
        "root_cause",
        "microtask",
    }
    assert fixes[0].get("advisory") is True


# --- deliberate wrapper duplication drift guard ------------------------------


@pytest.mark.parametrize("phase_module", ["documentation.py"])
def test_v2_team_phase_wrappers_stay_byte_identical(phase_module: str) -> None:
    """The backend and frontend copies of a v2 phase wrapper are byte-identical.

    The two teams deliberately keep a separate thin copy of
    ``phases/documentation.py``: it is the per-team monkeypatch / model-binding
    boundary that wires that team's models into the shared ``make_run_*``
    factories in ``shared/phases/``. It must stay separate from its counterpart —
    but they must also stay identical, so a one-sided edit (a fix applied to
    one team only) fails loudly here instead of silently forking the behavior.
    (``phases/deliver.py`` had the same pattern until ``make_run_deliver`` grew
    default ``git_ns``/``output_ns`` namespaces pointing at the real shared
    modules, letting tests monkeypatch those directly — no per-team wrapper
    file remains for it.)

    Preconditions:
        - Both team packages are importable (their ``__init__`` resolves).

    Postconditions:
        - Asserts the two wrapper files' raw bytes are equal; on failure the
          fix is to apply the same edit to both copies (or move the shared
          part into ``shared/phases/``).
    """
    import software_engineering_team.backend_code_v2_team as be_pkg
    import software_engineering_team.frontend_code_v2_team as fe_pkg

    be_file = Path(be_pkg.__file__).parent / "phases" / phase_module
    fe_file = Path(fe_pkg.__file__).parent / "phases" / phase_module
    assert be_file.read_bytes() == fe_file.read_bytes(), (
        f"{phase_module} has drifted between backend_code_v2_team and "
        "frontend_code_v2_team; these wrappers are deliberate duplicates and "
        "every edit must be applied to both copies"
    )
