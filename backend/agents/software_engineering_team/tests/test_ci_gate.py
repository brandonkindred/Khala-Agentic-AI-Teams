"""Tests for CI gate and workflow seeding functions in quality_gate_tools."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from software_engineering_team.quality_gate_tools import (
    CIGateResult,
    _run_local_check,
    _run_local_ci,
    run_ci_gate,
    seed_ci_workflows,
)

# ---------------------------------------------------------------------------
# _run_local_check
# ---------------------------------------------------------------------------


class TestRunLocalCheck:
    def test_passing_command(self, tmp_path: Path) -> None:
        result = _run_local_check(["python", "-c", "print('ok')"], tmp_path, "test-pass")
        assert result["passed"] is True
        assert result["name"] == "test-pass"

    def test_failing_command(self, tmp_path: Path) -> None:
        result = _run_local_check(["python", "-c", "raise SystemExit(1)"], tmp_path, "test-fail")
        assert result["passed"] is False

    def test_missing_tool(self, tmp_path: Path) -> None:
        result = _run_local_check(["nonexistent-tool-xyz"], tmp_path, "missing")
        assert result["passed"] is True
        assert "not found" in result["output"]

    def test_timeout(self, tmp_path: Path) -> None:
        with patch("software_engineering_team.quality_gate_tools.subprocess.run") as mock_run:
            import subprocess

            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["x"], timeout=120)
            result = _run_local_check(["x"], tmp_path, "slow")
            assert result["passed"] is False
            assert "timed out" in result["output"]


# ---------------------------------------------------------------------------
# _run_local_ci
# ---------------------------------------------------------------------------


class TestRunLocalCI:
    def test_no_project_files(self, tmp_path: Path) -> None:
        result = _run_local_ci(tmp_path)
        assert result.passed is True
        assert result.local_fallback is True
        assert "No CI checks" in result.summary

    def test_python_project_all_pass(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("flask\n")
        with patch("software_engineering_team.quality_gate_tools._run_local_check") as mock:
            mock.return_value = {"name": "ruff-lint", "passed": True, "output": "ok"}
            result = _run_local_ci(tmp_path)
        assert result.passed is True
        assert result.local_fallback is True

    def test_python_project_lint_fails(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("flask\n")
        with patch("software_engineering_team.quality_gate_tools._run_local_check") as mock:
            mock.side_effect = [
                {"name": "ruff-lint", "passed": False, "output": "E501"},
                {"name": "ruff-format", "passed": True, "output": ""},
                {"name": "syntax-check", "passed": True, "output": ""},
            ]
            result = _run_local_ci(tmp_path)
        assert result.passed is False
        assert "ruff-lint" in result.summary

    def test_node_project(self, tmp_path: Path) -> None:
        pkg = {"scripts": {"lint": "eslint .", "test": "vitest", "build": "tsc"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        with patch("software_engineering_team.quality_gate_tools._run_local_check") as mock:
            mock.return_value = {"name": "npm-lint", "passed": True, "output": "ok"}
            result = _run_local_ci(tmp_path)
        assert result.passed is True


# ---------------------------------------------------------------------------
# run_ci_gate
# ---------------------------------------------------------------------------


class TestRunCIGate:
    def test_local_fallback_when_no_github(self, tmp_path: Path) -> None:
        with patch("software_engineering_team.quality_gate_tools._run_local_ci") as mock:
            mock.return_value = CIGateResult(passed=True, summary="ok", local_fallback=True)
            result = run_ci_gate(tmp_path)
        assert result.passed is True
        assert result.local_fallback is True

    def test_github_path(self, tmp_path: Path) -> None:
        with patch("software_engineering_team.quality_gate_tools._run_github_ci") as mock:
            mock.return_value = CIGateResult(passed=True, summary="CI passed (3 checks)")
            result = run_ci_gate(
                tmp_path,
                github_token="tok",
                owner="org",
                repo_name="repo",
                ref="abc123",
            )
        assert result.passed is True
        mock.assert_called_once()

    def test_github_failure_falls_back_to_local(self, tmp_path: Path) -> None:
        with (
            patch("software_engineering_team.quality_gate_tools._run_github_ci") as gh_mock,
            patch("software_engineering_team.quality_gate_tools._run_local_ci") as local_mock,
        ):
            gh_mock.side_effect = RuntimeError("network error")
            local_mock.return_value = CIGateResult(passed=True, summary="ok", local_fallback=True)
            result = run_ci_gate(
                tmp_path,
                github_token="tok",
                owner="org",
                repo_name="repo",
                ref="abc123",
            )
        assert result.local_fallback is True

    def test_disabled_via_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SE_CI_GATE_LOCAL_FALLBACK", "false")
        result = run_ci_gate(tmp_path)
        assert result.passed is True
        assert "skipped" in result.summary

    def test_timeout_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SE_CI_GATE_TIMEOUT_S", "60")
        with patch("software_engineering_team.quality_gate_tools._run_github_ci") as mock:
            mock.return_value = CIGateResult(passed=True, summary="ok")
            run_ci_gate(
                tmp_path,
                github_token="tok",
                owner="org",
                repo_name="repo",
                ref="abc",
            )
            call_args = mock.call_args
            assert call_args[0][3] == "tok"  # token
            assert call_args[0][4] == 60  # timeout_s

    def test_invalid_timeout_env_uses_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SE_CI_GATE_TIMEOUT_S", "not-a-number")
        with patch("software_engineering_team.quality_gate_tools._run_local_ci") as mock:
            mock.return_value = CIGateResult(passed=True, summary="ok", local_fallback=True)
            run_ci_gate(tmp_path)


# ---------------------------------------------------------------------------
# seed_ci_workflows
# ---------------------------------------------------------------------------


class TestSeedCIWorkflows:
    def test_seeds_backend_workflow(self, tmp_path: Path) -> None:
        result = seed_ci_workflows(tmp_path, has_backend=True)
        assert len(result) == 1
        assert ".github/workflows/backend.yml" in result[0]
        assert (tmp_path / ".github" / "workflows" / "backend.yml").exists()

    def test_seeds_frontend_workflow(self, tmp_path: Path) -> None:
        result = seed_ci_workflows(tmp_path, has_frontend=True)
        assert len(result) == 1
        assert ".github/workflows/frontend.yml" in result[0]
        assert (tmp_path / ".github" / "workflows" / "frontend.yml").exists()

    def test_seeds_both(self, tmp_path: Path) -> None:
        result = seed_ci_workflows(tmp_path, has_backend=True, has_frontend=True)
        assert len(result) == 2

    def test_seeds_nothing_when_no_stack(self, tmp_path: Path) -> None:
        result = seed_ci_workflows(tmp_path)
        assert result == []

    def test_commits_when_git_repo(self, tmp_path: Path) -> None:
        import subprocess

        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True
        )
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
        (tmp_path / "README.md").write_text("# test\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

        with patch(
            "software_engineering_team.shared.git_utils.write_files_and_commit"
        ) as mock_commit:
            seed_ci_workflows(tmp_path, has_backend=True)
            mock_commit.assert_called_once()

    def test_no_commit_without_git(self, tmp_path: Path) -> None:
        seed_ci_workflows(tmp_path, has_backend=True)
        assert (tmp_path / ".github" / "workflows" / "backend.yml").exists()

    def test_generated_yaml_is_valid(self, tmp_path: Path) -> None:
        import yaml

        seed_ci_workflows(tmp_path, has_backend=True, has_frontend=True)
        for name in ("backend.yml", "frontend.yml"):
            content = (tmp_path / ".github" / "workflows" / name).read_text()
            parsed = yaml.safe_load(content)
            assert "jobs" in parsed
