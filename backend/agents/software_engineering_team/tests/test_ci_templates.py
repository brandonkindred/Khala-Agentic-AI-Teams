"""Tests for CI workflow template rendering."""

from __future__ import annotations

import yaml

from software_engineering_team.ci_templates import (
    BackendCIParams,
    FrontendCIParams,
    render_backend_ci,
    render_frontend_ci,
)

# ---------------------------------------------------------------------------
# Backend CI template
# ---------------------------------------------------------------------------


class TestRenderBackendCI:
    def test_default_params_produce_valid_yaml(self) -> None:
        result = render_backend_ci()
        parsed = yaml.safe_load(result)
        assert parsed["name"] == "Backend CI"
        assert "jobs" in parsed

    def test_contains_lint_and_test_jobs(self) -> None:
        parsed = yaml.safe_load(render_backend_ci())
        assert "lint" in parsed["jobs"]
        assert "test" in parsed["jobs"]

    def test_sast_job_present_when_enabled(self) -> None:
        parsed = yaml.safe_load(render_backend_ci(BackendCIParams(enable_sast=True)))
        assert "sast" in parsed["jobs"]

    def test_sast_job_absent_when_disabled(self) -> None:
        parsed = yaml.safe_load(render_backend_ci(BackendCIParams(enable_sast=False)))
        assert "sast" not in parsed["jobs"]

    def test_sca_job_present_when_enabled(self) -> None:
        parsed = yaml.safe_load(render_backend_ci(BackendCIParams(enable_sca=True)))
        assert "sca" in parsed["jobs"]

    def test_sca_job_absent_when_disabled(self) -> None:
        parsed = yaml.safe_load(render_backend_ci(BackendCIParams(enable_sca=False)))
        assert "sca" not in parsed["jobs"]

    def test_secrets_job_present_when_enabled(self) -> None:
        parsed = yaml.safe_load(render_backend_ci(BackendCIParams(enable_secrets_scan=True)))
        assert "secrets" in parsed["jobs"]

    def test_secrets_job_absent_when_disabled(self) -> None:
        parsed = yaml.safe_load(render_backend_ci(BackendCIParams(enable_secrets_scan=False)))
        assert "secrets" not in parsed["jobs"]

    def test_custom_python_version(self) -> None:
        result = render_backend_ci(BackendCIParams(python_version="3.12"))
        assert "3.12" in result

    def test_custom_test_command(self) -> None:
        result = render_backend_ci(BackendCIParams(test_command="python -m pytest -x"))
        assert "python -m pytest -x" in result

    def test_custom_lint_command(self) -> None:
        result = render_backend_ci(BackendCIParams(lint_command="flake8 ."))
        assert "flake8 ." in result

    def test_all_security_disabled(self) -> None:
        params = BackendCIParams(enable_sast=False, enable_sca=False, enable_secrets_scan=False)
        parsed = yaml.safe_load(render_backend_ci(params))
        assert "sast" not in parsed["jobs"]
        assert "sca" not in parsed["jobs"]
        assert "secrets" not in parsed["jobs"]
        assert "lint" in parsed["jobs"]
        assert "test" in parsed["jobs"]

    def test_commands_with_quotes_produce_valid_yaml(self) -> None:
        params = BackendCIParams(test_command='pytest -k "smoke"')
        result = render_backend_ci(params)
        parsed = yaml.safe_load(result)
        assert "jobs" in parsed

    def test_commands_with_colon_space_produce_valid_yaml(self) -> None:
        params = BackendCIParams(test_command='echo "key: value"')
        result = render_backend_ci(params)
        parsed = yaml.safe_load(result)
        assert "jobs" in parsed

    def test_deterministic(self) -> None:
        a = render_backend_ci(BackendCIParams())
        b = render_backend_ci(BackendCIParams())
        assert a == b

    def test_triggers_on_push_and_pr(self) -> None:
        parsed = yaml.safe_load(render_backend_ci())
        assert "push" in parsed[True]  # YAML parses `on:` as True
        assert "pull_request" in parsed[True]

    def test_permissions_read_only(self) -> None:
        parsed = yaml.safe_load(render_backend_ci())
        assert parsed["permissions"]["contents"] == "read"


# ---------------------------------------------------------------------------
# Frontend CI template
# ---------------------------------------------------------------------------


class TestRenderFrontendCI:
    def test_default_params_produce_valid_yaml(self) -> None:
        result = render_frontend_ci()
        parsed = yaml.safe_load(result)
        assert parsed["name"] == "Frontend CI"
        assert "jobs" in parsed

    def test_contains_lint_build_test_jobs(self) -> None:
        parsed = yaml.safe_load(render_frontend_ci())
        assert "lint" in parsed["jobs"]
        assert "build" in parsed["jobs"]
        assert "test" in parsed["jobs"]

    def test_sca_job_present_when_enabled(self) -> None:
        parsed = yaml.safe_load(render_frontend_ci(FrontendCIParams(enable_sca=True)))
        assert "sca" in parsed["jobs"]

    def test_sca_job_absent_when_disabled(self) -> None:
        parsed = yaml.safe_load(render_frontend_ci(FrontendCIParams(enable_sca=False)))
        assert "sca" not in parsed["jobs"]

    def test_secrets_job_present_when_enabled(self) -> None:
        parsed = yaml.safe_load(render_frontend_ci(FrontendCIParams(enable_secrets_scan=True)))
        assert "secrets" in parsed["jobs"]

    def test_secrets_job_absent_when_disabled(self) -> None:
        parsed = yaml.safe_load(render_frontend_ci(FrontendCIParams(enable_secrets_scan=False)))
        assert "secrets" not in parsed["jobs"]

    def test_custom_node_version(self) -> None:
        result = render_frontend_ci(FrontendCIParams(node_version="20"))
        assert "20" in result

    def test_custom_package_manager(self) -> None:
        result = render_frontend_ci(
            FrontendCIParams(package_manager="yarn", install_command="yarn install")
        )
        assert "yarn install" in result

    def test_custom_test_command(self) -> None:
        result = render_frontend_ci(FrontendCIParams(test_command="npx vitest run"))
        assert "npx vitest run" in result

    def test_all_security_disabled(self) -> None:
        params = FrontendCIParams(enable_sca=False, enable_secrets_scan=False)
        parsed = yaml.safe_load(render_frontend_ci(params))
        assert "sca" not in parsed["jobs"]
        assert "secrets" not in parsed["jobs"]
        assert "lint" in parsed["jobs"]
        assert "test" in parsed["jobs"]

    def test_deterministic(self) -> None:
        a = render_frontend_ci(FrontendCIParams())
        b = render_frontend_ci(FrontendCIParams())
        assert a == b

    def test_permissions_read_only(self) -> None:
        parsed = yaml.safe_load(render_frontend_ci())
        assert parsed["permissions"]["contents"] == "read"


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------


class TestModels:
    def test_backend_defaults(self) -> None:
        p = BackendCIParams()
        assert p.python_version == "3.10"
        assert p.enable_sast is True
        assert p.enable_sca is True
        assert p.enable_secrets_scan is True

    def test_frontend_defaults(self) -> None:
        p = FrontendCIParams()
        assert p.node_version == "22"
        assert p.package_manager == "npm"
        assert p.enable_sca is True
        assert p.enable_secrets_scan is True

    def test_backend_custom(self) -> None:
        p = BackendCIParams(python_version="3.12", enable_sast=False)
        assert p.python_version == "3.12"
        assert p.enable_sast is False

    def test_frontend_custom(self) -> None:
        p = FrontendCIParams(node_version="20", package_manager="pnpm")
        assert p.node_version == "20"
        assert p.package_manager == "pnpm"
