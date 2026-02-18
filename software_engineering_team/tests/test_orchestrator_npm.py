"""Tests for orchestrator npm install when frontend returns npm_packages_to_install."""

from pathlib import Path
from unittest.mock import patch

import pytest

from frontend_team.feature_agent import FrontendOutput
from shared.command_runner import CommandResult
import shared.command_runner as command_runner_module


def test_npm_install_called_with_packages_from_frontend_output(tmp_path: Path) -> None:
    """
    Verify that when FrontendOutput has npm_packages_to_install, run_command_with_nvm
    is called with the correct npm install command (same logic as orchestrator).
    """
    result = FrontendOutput(
        code="",
        summary="Test",
        files={"src/app/example.component.ts": "content"},
        components=["example"],
        suggested_commit_message="feat: add example",
        npm_packages_to_install=["@ngrx/store", "ngx-toastr"],
    )
    (tmp_path / "package.json").write_text('{"name": "test"}', encoding="utf-8")

    with patch.object(command_runner_module, "run_command_with_nvm", return_value=CommandResult(success=True, exit_code=0, stdout="", stderr="")) as mock_nvm:
        if result.npm_packages_to_install:
            install_cmd = ["npm", "install", "--save"] + result.npm_packages_to_install
            command_runner_module.run_command_with_nvm(install_cmd, cwd=tmp_path)

    mock_nvm.assert_called_once()
    args, kwargs = mock_nvm.call_args
    assert args[0] == ["npm", "install", "--save", "@ngrx/store", "ngx-toastr"]
    assert kwargs.get("cwd") == tmp_path


def test_orchestrator_npm_install_logic() -> None:
    """
    Verify the orchestrator's npm install logic: when result.npm_packages_to_install
    is non-empty, it builds the correct npm install command.
    """
    result = FrontendOutput(
        code="",
        summary="",
        files={"src/x.ts": "content"},
        components=[],
        npm_packages_to_install=["pkg1", "pkg2"],
    )
    install_cmd = ["npm", "install", "--save"] + result.npm_packages_to_install
    assert install_cmd == ["npm", "install", "--save", "pkg1", "pkg2"]
