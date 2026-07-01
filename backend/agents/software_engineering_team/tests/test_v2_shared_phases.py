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

import pytest

from software_engineering_team.backend_code_v2_team import models as be_models
from software_engineering_team.shared.models import SystemArchitecture, Task, TaskStatus, TaskType
from software_engineering_team.shared.phases import execution as sh_exec
from software_engineering_team.shared.phases import planning as sh_plan
from software_engineering_team.shared.phases import problem_solving as sh_ps
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
from software_engineering_team.shared.strands_model import LlmRunner
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
    detect_language=lambda _p, _t: "python",
)

_FRONTEND_PROFILE = StackProfile(
    name="frontend",
    default_language="typescript",
    planning_language_label="Language/stack",
    planning_progress_label="stack",
    conventions_by_language={"_default": "TS"},
    has_language_conventions=False,
    detect_language=lambda _p, _t: "typescript",
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


def test_dedup_issues_removes_repeats():
    """Dedup issues removes repeats."""
    seen: set = set()
    a = SimpleNamespace(file_path="f.py", description="bug")
    b = SimpleNamespace(file_path="f.py", description="bug")
    c = SimpleNamespace(file_path="g.py", description="bug")
    first = sh_exec._dedup_issues([a, b, c], seen)
    assert len(first) == 2  # b is a dup of a
    # seen persists across calls
    assert sh_exec._dedup_issues([c], seen) == []


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
    sh_exec._write_microtask_files(tmp_path, {"/pkg/mod.py": "content", "top.txt": "t"})
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
    """Write microtask output helper: writes on a safe path, review-fails on unsafe."""
    mt = SimpleNamespace(id="mt-1", status="in_progress", notes="")
    review_failed_ids: set = set()
    all_files = {"kept.py": "k", "gen.py": "g"}

    # Safe path → writes and returns True.
    ok = sh_exec.write_microtask_output_or_fail(
        tmp_path,
        {"gen.py": "g2"},
        mt=mt,
        task_id="t1",
        review_failed_ids=review_failed_ids,
        all_files=all_files,
        microtask_file_keys={"gen.py"},
        review_failed_status="REVIEW_FAILED",
    )
    assert ok is True
    assert (tmp_path / "gen.py").read_text(encoding="utf-8") == "g2"
    assert not review_failed_ids

    # Unsafe path → no exception, marks review-failed, rolls back this microtask's keys.
    rejected = sh_exec.write_microtask_output_or_fail(
        tmp_path,
        {"../evil.py": "x"},
        mt=mt,
        task_id="t1",
        review_failed_ids=review_failed_ids,
        all_files=all_files,
        microtask_file_keys={"gen.py"},
        review_failed_status="REVIEW_FAILED",
    )
    assert rejected is False
    assert mt.status == "REVIEW_FAILED"
    assert "mt-1" in review_failed_ids
    assert (
        "gen.py" not in all_files and "kept.py" in all_files
    )  # only this microtask's keys rolled back
    assert not (tmp_path.parent / "evil.py").exists()


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
        repo_path="",
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
    ): "3d8e69ceda009a143a2af73a0ebbe9e44247d222a7bacf88553d558a2837d062",
    (
        "backend",
        "PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT",
    ): "be85517553575e102470ad987d8d18a19aa8e28e546a8ee1fe5877b85b1070ed",
    (
        "frontend",
        "PLANNING_PROMPT",
    ): "aaca2421d786e2f4c612b14030528f4a63a0ba714b67bf936af900bf59ad0a60",
    (
        "frontend",
        "EXECUTION_PROMPT",
    ): "790512ed71fb7a072d63f4473a1587a3076509cec86297c7d0fed3b9062f153f",
    (
        "frontend",
        "PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT",
    ): "b1e2f622a4f01011142e99086b9f7bb510372bd9a5a66ff8ac3bfea70ebac3d4",
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
