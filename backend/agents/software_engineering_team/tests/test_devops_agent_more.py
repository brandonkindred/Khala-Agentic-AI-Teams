"""Deeper coverage for DevOpsExpertAgent and its helpers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from software_engineering_team.devops_agent import agent as devops_mod
from software_engineering_team.devops_agent.agent import (
    DevOpsExpertAgent,
    _build_error_signature,
    _gather_codebase_context,
    _validate_devops_output,
)
from software_engineering_team.devops_agent.models import (
    DevOpsInput,
    DevOpsOutput,
    TargetRepo,
)
from software_engineering_team.shared.models import ArchitectureComponent, SystemArchitecture

from .conftest import ConfigurableLLM


class _StubAgent:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def __call__(self, prompt):
        self.calls.append(prompt)
        if self.payloads:
            p = self.payloads.pop(0)
            if isinstance(p, Exception):
                raise p
            return json.dumps(p)
        return "{}"


def _make_agent(monkeypatch, payloads=None):
    stub = _StubAgent(payloads or [])
    monkeypatch.setattr(devops_mod, "Agent", lambda *a, **kw: stub)
    monkeypatch.setattr(devops_mod, "get_strands_model", lambda key=None: object())
    return stub


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Initialize a git repo at tmp_path so write_agent_output can commit."""
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=False)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True, check=False
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True, check=False
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=tmp_path, capture_output=True, check=False
    )
    return tmp_path


def test_build_error_signature() -> None:
    assert _build_error_signature("error abc\nmore") == "error abc\nmore"
    long = "x" * 1000
    sig = _build_error_signature(long)
    assert len(sig) == 1000


def test_validate_devops_output_no_outputs() -> None:
    valid, errors = _validate_devops_output(DevOpsOutput())
    assert not valid
    assert any("No files to write" in e for e in errors)


def test_validate_devops_output_good_dockerfile() -> None:
    out = DevOpsOutput(dockerfile="FROM python:3.11\nCMD python app.py")
    valid, errors = _validate_devops_output(out)
    assert valid
    assert errors == []


def test_validate_devops_output_dockerfile_missing_from() -> None:
    out = DevOpsOutput(dockerfile="CMD python app.py")
    valid, errors = _validate_devops_output(out)
    assert not valid
    assert any("FROM" in e for e in errors)


def test_validate_devops_output_dockerfile_missing_cmd_entrypoint() -> None:
    out = DevOpsOutput(dockerfile="FROM python:3.11")
    valid, errors = _validate_devops_output(out)
    assert not valid
    assert any("CMD or ENTRYPOINT" in e for e in errors)


def test_validate_devops_output_good_pipeline_yaml() -> None:
    out = DevOpsOutput(
        pipeline_yaml="name: CI\non:\n  push:\njobs:\n  test:\n    runs-on: ubuntu-latest"
    )
    valid, errors = _validate_devops_output(out)
    assert valid


def test_validate_devops_output_pipeline_empty() -> None:
    """Whitespace-only pipeline_yaml is treated as missing -> no_output error."""
    out = DevOpsOutput(pipeline_yaml="  ")
    valid, errors = _validate_devops_output(out)
    assert not valid


def test_validate_devops_output_pipeline_missing_jobs() -> None:
    out = DevOpsOutput(pipeline_yaml="name: CI\non:\n  push:")
    valid, errors = _validate_devops_output(out)
    assert not valid
    assert any("jobs" in e for e in errors)


def test_validate_devops_output_pipeline_bad_yaml() -> None:
    out = DevOpsOutput(pipeline_yaml="key: value:\n  - bad: [")
    valid, errors = _validate_devops_output(out)
    assert not valid
    assert any("YAML" in e for e in errors)


def test_validate_devops_output_docker_compose_good() -> None:
    out = DevOpsOutput(docker_compose="version: '3'\nservices:\n  api:\n    image: foo")
    valid, errors = _validate_devops_output(out)
    assert valid


def test_validate_devops_output_docker_compose_missing_services() -> None:
    out = DevOpsOutput(docker_compose="version: '3'")
    valid, errors = _validate_devops_output(out)
    assert not valid
    assert any("services" in e for e in errors)


def test_validate_devops_output_docker_compose_bad_yaml() -> None:
    out = DevOpsOutput(docker_compose="services:\n  - bad: [")
    valid, errors = _validate_devops_output(out)
    assert not valid


def test_validate_devops_output_iac_content() -> None:
    out = DevOpsOutput(iac_content="terraform { backend ... }")
    valid, _errors = _validate_devops_output(out)
    assert valid


def test_validate_devops_output_artifacts_yaml_good() -> None:
    out = DevOpsOutput(artifacts={"x.yml": "key: value"})
    valid, _errors = _validate_devops_output(out)
    assert valid


def test_validate_devops_output_artifacts_yaml_bad() -> None:
    out = DevOpsOutput(artifacts={"x.yml": "bad: [unclosed"})
    valid, errors = _validate_devops_output(out)
    assert not valid


def test_gather_codebase_context_empty_dir(tmp_path: Path) -> None:
    assert _gather_codebase_context(tmp_path) == ""


def test_gather_codebase_context_python_with_workflows(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("fastapi==0.100")
    (tmp_path / "pyproject.toml").write_text("[tool.poetry]\nname='x'")
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text("name: CI")
    (wf / "deploy.yaml").write_text("name: Deploy")
    (tmp_path / "main.py").write_text("app = ...")
    ctx = _gather_codebase_context(tmp_path)
    assert "requirements.txt" in ctx
    assert "fastapi" in ctx
    assert "pyproject.toml" in ctx
    assert "ci.yml" in ctx
    assert "deploy.yaml" in ctx
    assert "main.py" in ctx


def test_gather_codebase_context_node(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name": "app"}')
    ctx = _gather_codebase_context(tmp_path)
    assert "package.json" in ctx


def test_gather_codebase_context_alt_main(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("x")
    ctx = _gather_codebase_context(tmp_path)
    assert "app/main.py" in ctx


def test_devops_agent_init_with_llm() -> None:
    llm = ConfigurableLLM()
    a = DevOpsExpertAgent(llm_client=llm)
    assert a.llm is llm


def test_plan_task_returns_empty_on_invalid_json(monkeypatch, tmp_path: Path) -> None:
    """If LLM returns invalid JSON, the plan is dropped silently."""

    class _Bad:
        def __call__(self, prompt):
            return "not json"

    monkeypatch.setattr(devops_mod, "Agent", lambda *a, **kw: _Bad())
    monkeypatch.setattr(devops_mod, "get_strands_model", lambda key=None: object())
    a = DevOpsExpertAgent(llm_client=ConfigurableLLM())
    out = a._plan_task(task_description="t", requirements="r", repo_path=tmp_path)
    assert out == ""


def test_plan_task_includes_target_repo_enum(monkeypatch, tmp_path: Path) -> None:
    """target_repo with .value attribute is unwrapped."""
    stub = _make_agent(
        monkeypatch,
        [
            {
                "feature_intent": "deploy backend",
                "what_changes": ["Dockerfile"],
                "algorithms_data_structures": "",
                "tests_needed": "",
            }
        ],
    )

    class _Repo:
        value = "backend"

    a = DevOpsExpertAgent(llm_client=ConfigurableLLM())
    plan = a._plan_task(
        task_description="t",
        requirements="r",
        existing_pipeline="old pipeline",
        architecture=type(
            "A",
            (),
            {
                "overview": "arch",
                "components": [
                    type("C", (), {"name": "api", "type": "service", "technology": "py"})()
                ],
            },
        )(),
        target_repo=_Repo(),
        repo_path=tmp_path,
    )
    assert plan
    # Prompt included target_repo
    assert "target_repo=backend" in stub.calls[0]
    assert "old pipeline" in stub.calls[0]


def test_devops_run_handles_non_list_clarification(monkeypatch, tmp_path: Path) -> None:
    _make_agent(
        monkeypatch,
        [
            {
                "pipeline_yaml": "",
                "dockerfile": "",
                "summary": "ambiguous",
                "needs_clarification": True,
                "clarification_requests": "single string",
            },
        ],
    )
    a = DevOpsExpertAgent(llm_client=ConfigurableLLM())
    out = a.run(DevOpsInput(task_description="X", requirements="R"))
    assert out.needs_clarification is True
    assert out.clarification_requests == ["single string"]


def test_devops_run_with_build_errors_prewrite(monkeypatch) -> None:
    _make_agent(
        monkeypatch,
        [
            {
                "dockerfile": "FROM x\nCMD y",
                "summary": "ok",
                "needs_clarification": False,
                "clarification_requests": [],
            },
        ],
    )
    a = DevOpsExpertAgent(llm_client=ConfigurableLLM())
    out = a.run(
        DevOpsInput(
            task_description="T",
            requirements="R",
            build_errors="Pre-write validation failed:\nMissing FROM",
        )
    )
    assert out.dockerfile.startswith("FROM")


def test_devops_run_with_tech_stack_and_arch(monkeypatch) -> None:
    """All optional fields populate the prompt without error."""
    stub = _make_agent(
        monkeypatch,
        [
            {
                "dockerfile": "FROM x\nCMD y",
                "summary": "fine",
                "needs_clarification": False,
            }
        ],
    )

    arch = SystemArchitecture(
        overview="arch",
        components=[ArchitectureComponent(name="api", type="service", technology="fastapi")],
    )

    a = DevOpsExpertAgent(llm_client=ConfigurableLLM())
    a.run(
        DevOpsInput(
            task_description="T",
            requirements="R",
            architecture=arch,
            existing_pipeline="old",
            tech_stack=["py", "docker"],
            target_repo=TargetRepo.BACKEND,
            task_plan="my plan",
            build_errors="random error happened",
        )
    )
    prompt = stub.calls[0]
    assert "Tech Stack" in prompt
    assert "target_repo=backend" in prompt
    assert "Implementation plan" in prompt
    assert "Build/validation errors" in prompt


def test_devops_workflow_needs_clarification_returns_failure(monkeypatch, tmp_path: Path) -> None:
    _make_agent(
        monkeypatch,
        [
            # planning JSON
            {
                "feature_intent": "X",
                "what_changes": ["a"],
                "algorithms_data_structures": "",
                "tests_needed": "",
            },
            # run() returns clarification
            {
                "needs_clarification": True,
                "clarification_requests": ["please clarify Y"],
                "summary": "",
            },
        ],
    )
    a = DevOpsExpertAgent(llm_client=ConfigurableLLM())
    res = a.run_workflow(
        repo_path=tmp_path,
        task_description="t",
        requirements="r",
        build_verifier=lambda *a, **kw: (True, ""),
    )
    assert res.success is False
    assert "Clarification" in res.failure_reason


def test_devops_workflow_empty_output_repeats(monkeypatch, tmp_path: Path) -> None:
    """All run() outputs are empty -> validation fails repeatedly -> stop after MAX_SAME_BUILD_FAILURES."""
    # Patch MAX_SAME_BUILD_FAILURES to 2 to keep test fast
    monkeypatch.setattr(devops_mod, "MAX_SAME_BUILD_FAILURES", 2)
    payloads = [
        # planning
        {
            "feature_intent": "X",
            "what_changes": [],
            "algorithms_data_structures": "",
            "tests_needed": "",
        },
    ]
    # Then several empty-output runs
    for _ in range(5):
        payloads.append(
            {
                "dockerfile": "",
                "pipeline_yaml": "",
                "summary": "nothing",
                "needs_clarification": False,
            }
        )
    _make_agent(monkeypatch, payloads)
    a = DevOpsExpertAgent(llm_client=ConfigurableLLM())
    res = a.run_workflow(
        repo_path=tmp_path,
        task_description="t",
        requirements="r",
        build_verifier=lambda *a, **kw: (True, ""),
        max_iterations=10,
    )
    assert res.success is False
    assert "Validation failed" in res.failure_reason


def test_devops_workflow_success_on_first_iteration(monkeypatch, git_repo: Path) -> None:
    tmp_path = git_repo
    # Make plan and run() both succeed; build_verifier returns ok
    _make_agent(
        monkeypatch,
        [
            # planning
            {
                "feature_intent": "X",
                "what_changes": ["Dockerfile"],
                "algorithms_data_structures": "",
                "tests_needed": "",
            },
            {
                "dockerfile": "FROM python:3.11\nCMD x",
                "summary": "done",
                "needs_clarification": False,
            },
        ],
    )
    # Pre-create plan dir so the plan persistence branch runs
    (tmp_path / "plan").mkdir()
    a = DevOpsExpertAgent(llm_client=ConfigurableLLM())
    res = a.run_workflow(
        repo_path=tmp_path,
        task_description="t",
        requirements="r",
        build_verifier=lambda *a, **kw: (True, ""),
    )
    assert res.success is True
    # Verify plan file was written
    assert (tmp_path / "plan").glob("*.md")


def test_devops_workflow_build_fails_then_succeeds(monkeypatch, git_repo: Path) -> None:
    tmp_path = git_repo
    _make_agent(
        monkeypatch,
        [
            {
                "feature_intent": "X",
                "what_changes": [],
                "algorithms_data_structures": "",
                "tests_needed": "",
            },
            {"dockerfile": "FROM x\nCMD y", "summary": "ok", "needs_clarification": False},
            {"dockerfile": "FROM x\nCMD z", "summary": "ok2", "needs_clarification": False},
        ],
    )
    calls = {"n": 0}

    def _verify(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return (False, "error one")
        return (True, "")

    a = DevOpsExpertAgent(llm_client=ConfigurableLLM())
    res = a.run_workflow(
        repo_path=tmp_path,
        task_description="t",
        requirements="r",
        build_verifier=_verify,
        max_iterations=5,
    )
    assert res.success is True
    assert res.iterations == 2


def test_devops_workflow_same_build_error_repeats(monkeypatch, git_repo: Path) -> None:
    tmp_path = git_repo
    monkeypatch.setattr(devops_mod, "MAX_SAME_BUILD_FAILURES", 2)
    payloads = [
        # planning
        {
            "feature_intent": "X",
            "what_changes": [],
            "algorithms_data_structures": "",
            "tests_needed": "",
        }
    ]
    # Then many valid runs
    for _ in range(10):
        payloads.append(
            {"dockerfile": "FROM x\nCMD y", "summary": "ok", "needs_clarification": False}
        )
    _make_agent(monkeypatch, payloads)

    a = DevOpsExpertAgent(llm_client=ConfigurableLLM())
    res = a.run_workflow(
        repo_path=tmp_path,
        task_description="t",
        requirements="r",
        build_verifier=lambda *a, **kw: (False, "same error every time"),
        max_iterations=5,
    )
    assert res.success is False
    assert "Build failed" in res.failure_reason


def test_devops_workflow_max_iterations(monkeypatch, git_repo: Path) -> None:
    tmp_path = git_repo
    payloads = [
        {
            "feature_intent": "X",
            "what_changes": [],
            "algorithms_data_structures": "",
            "tests_needed": "",
        }
    ]
    counter = {"n": 0}
    for _ in range(10):
        payloads.append(
            {
                "dockerfile": f"FROM x\nCMD y\n# iter {counter['n']}",
                "summary": "ok",
                "needs_clarification": False,
            }
        )
        counter["n"] += 1
    _make_agent(monkeypatch, payloads)

    # Different error each time so we never hit the same-error short-circuit
    n = {"i": 0}

    def _verify(*a, **kw):
        n["i"] += 1
        return (False, f"unique error {n['i']}")

    a = DevOpsExpertAgent(llm_client=ConfigurableLLM())
    res = a.run_workflow(
        repo_path=tmp_path,
        task_description="t",
        requirements="r",
        build_verifier=_verify,
        max_iterations=3,
    )
    assert res.success is False
    assert "after 3 iterations" in res.failure_reason


def test_devops_workflow_with_review_agent_rejects(monkeypatch, git_repo: Path) -> None:
    tmp_path = git_repo
    """When devops_review_agent finds critical issues, we re-generate."""
    monkeypatch.setattr(devops_mod, "MAX_SAME_BUILD_FAILURES", 2)
    payloads = [
        {
            "feature_intent": "X",
            "what_changes": [],
            "algorithms_data_structures": "",
            "tests_needed": "",
        }
    ]
    for _ in range(10):
        payloads.append(
            {"dockerfile": "FROM x\nCMD y", "summary": "ok", "needs_clarification": False}
        )
    _make_agent(monkeypatch, payloads)

    class _Issue:
        severity = "critical"
        artifact = "Dockerfile"
        description = "bad"
        suggestion = "fix"

    class _Review:
        approved = False
        issues = [_Issue()]

    review_agent = MagicMock()
    review_agent.run.return_value = _Review()

    a = DevOpsExpertAgent(llm_client=ConfigurableLLM())
    res = a.run_workflow(
        repo_path=tmp_path,
        task_description="t",
        requirements="r",
        build_verifier=lambda *a, **kw: (True, ""),
        max_iterations=5,
        devops_review_agent=review_agent,
    )
    assert res.success is False
    assert "DevOps review failed" in res.failure_reason


def test_devops_workflow_with_review_agent_approves(monkeypatch, git_repo: Path) -> None:
    tmp_path = git_repo
    """When devops_review_agent approves, workflow proceeds to build_verifier."""
    _make_agent(
        monkeypatch,
        [
            {
                "feature_intent": "X",
                "what_changes": [],
                "algorithms_data_structures": "",
                "tests_needed": "",
            },
            {"dockerfile": "FROM x\nCMD y", "summary": "ok", "needs_clarification": False},
        ],
    )

    class _Review:
        approved = True
        issues = []

    review_agent = MagicMock()
    review_agent.run.return_value = _Review()

    a = DevOpsExpertAgent(llm_client=ConfigurableLLM())
    res = a.run_workflow(
        repo_path=tmp_path,
        task_description="t",
        requirements="r",
        build_verifier=lambda *a, **kw: (True, ""),
        devops_review_agent=review_agent,
    )
    assert res.success is True
