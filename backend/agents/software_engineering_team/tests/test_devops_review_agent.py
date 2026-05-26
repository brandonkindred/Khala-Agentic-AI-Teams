"""Tests for the DevOps Review agent.

Stubs ``self._agent`` (the Strands ``Agent`` instance) with a callable that
returns a canned JSON string so we don't need a live LLM.
"""

from __future__ import annotations

import json


def _make_agent_with_response(monkeypatch, response: dict):
    """Construct a ``DevOpsReviewAgent`` whose Strands Agent returns the
    given dict serialized as JSON."""
    from software_engineering_team.devops_review_agent.agent import DevOpsReviewAgent

    agent = DevOpsReviewAgent.__new__(DevOpsReviewAgent)

    class _StubAgent:
        def __call__(self, prompt: str) -> str:
            return json.dumps(response)

    agent._agent = _StubAgent()
    return agent


def test_devops_review_no_artifacts_returns_approved() -> None:
    from software_engineering_team.devops_review_agent.agent import DevOpsReviewAgent
    from software_engineering_team.devops_review_agent.models import DevOpsReviewInput

    agent = DevOpsReviewAgent.__new__(DevOpsReviewAgent)

    class _StubAgent:
        def __call__(self, prompt: str) -> str:
            raise AssertionError("Should not be called when no artifacts")

    agent._agent = _StubAgent()
    result = agent.run(DevOpsReviewInput(task_description="t", requirements="r"))
    assert result.approved is True
    assert result.issues == []
    assert "No artifacts" in result.summary


def test_devops_review_approved_no_issues(monkeypatch) -> None:
    from software_engineering_team.devops_review_agent.models import DevOpsReviewInput

    agent = _make_agent_with_response(
        monkeypatch,
        {"approved": True, "issues": [], "summary": "looks good"},
    )
    result = agent.run(
        DevOpsReviewInput(
            dockerfile="FROM python:3.11", task_description="t", requirements="r"
        )
    )
    assert result.approved is True
    assert result.summary == "looks good"


def test_devops_review_critical_issue_overrides_approval(monkeypatch) -> None:
    from software_engineering_team.devops_review_agent.models import DevOpsReviewInput

    agent = _make_agent_with_response(
        monkeypatch,
        {
            "approved": True,  # Even if LLM claims approved
            "issues": [
                {
                    "severity": "critical",
                    "artifact": "Dockerfile",
                    "description": "no FROM",
                    "suggestion": "add FROM",
                },
            ],
            "summary": "no",
        },
    )
    result = agent.run(
        DevOpsReviewInput(dockerfile="bad", task_description="t", requirements="r")
    )
    # Critical issue forces rejection regardless of approved=True from LLM
    assert result.approved is False
    assert len(result.issues) == 1


def test_devops_review_only_minor_issues_auto_approves(monkeypatch) -> None:
    """LLM rejects but only nit/minor issues → wrapper auto-approves."""
    from software_engineering_team.devops_review_agent.models import DevOpsReviewInput

    agent = _make_agent_with_response(
        monkeypatch,
        {
            "approved": False,
            "issues": [
                {
                    "severity": "minor",
                    "artifact": "Dockerfile",
                    "description": "missing comment",
                    "suggestion": "add",
                },
                {"severity": "nit", "artifact": "Dockerfile", "description": "fmt"},
            ],
            "summary": "minor stuff",
        },
    )
    result = agent.run(
        DevOpsReviewInput(dockerfile="ok", task_description="t", requirements="r")
    )
    assert result.approved is True


def test_devops_review_rejected_with_summary_synthesizes_issue(monkeypatch) -> None:
    """LLM rejects with no issues but a summary → wrapper synthesizes a major
    issue from the summary."""
    from software_engineering_team.devops_review_agent.models import DevOpsReviewInput

    agent = _make_agent_with_response(
        monkeypatch,
        {"approved": False, "issues": [], "summary": "Dockerfile has no FROM line"},
    )
    result = agent.run(
        DevOpsReviewInput(dockerfile="x", task_description="t", requirements="r")
    )
    assert result.approved is False
    assert len(result.issues) == 1
    assert "Dockerfile has no FROM line" in result.issues[0].description


def test_devops_review_rejected_with_nothing_auto_approves(monkeypatch) -> None:
    """LLM rejects with no issues, no summary → auto-approves (safety net)."""
    from software_engineering_team.devops_review_agent.models import DevOpsReviewInput

    agent = _make_agent_with_response(
        monkeypatch,
        {"approved": False, "issues": [], "summary": ""},
    )
    result = agent.run(
        DevOpsReviewInput(dockerfile="x", task_description="t", requirements="r")
    )
    assert result.approved is True


def test_devops_review_with_all_artifact_types(monkeypatch) -> None:
    from software_engineering_team.devops_review_agent.models import DevOpsReviewInput

    agent = _make_agent_with_response(
        monkeypatch,
        {"approved": True, "issues": [], "summary": ""},
    )
    result = agent.run(
        DevOpsReviewInput(
            dockerfile="FROM python:3.11",
            pipeline_yaml="name: ci",
            docker_compose="version: '3'",
            iac_content="resource {}",
            task_description="t",
            requirements="r",
            target_repo="backend",
        )
    )
    assert result.approved is True


def test_devops_review_drops_issues_without_description(monkeypatch) -> None:
    from software_engineering_team.devops_review_agent.models import DevOpsReviewInput

    agent = _make_agent_with_response(
        monkeypatch,
        {
            "approved": True,
            "issues": [
                {"severity": "major", "description": ""},
                {"severity": "minor", "description": "real one"},
                "garbage",  # non-dict entry skipped
            ],
            "summary": "",
        },
    )
    result = agent.run(
        DevOpsReviewInput(dockerfile="x", task_description="t", requirements="r")
    )
    # Only the entry with a non-empty description is preserved
    assert len(result.issues) == 1
    assert result.issues[0].description == "real one"


def test_devops_review_models_roundtrip() -> None:
    from software_engineering_team.devops_review_agent.models import (
        DevOpsReviewInput,
        DevOpsReviewIssue,
        DevOpsReviewOutput,
    )

    issue = DevOpsReviewIssue(severity="major", description="x", suggestion="y")
    assert issue.severity == "major"

    input_data = DevOpsReviewInput(dockerfile="FROM x", task_description="t")
    assert input_data.dockerfile == "FROM x"

    output = DevOpsReviewOutput(approved=True, issues=[issue], summary="ok")
    assert output.approved is True
    assert len(output.issues) == 1
